"""Tests for the entity-suggest eval harness.

Pins three concerns:
  1. `EntityEvalFixture` dataclass shape — schema + expected entity bindings
  2. `score_entity_candidates` — pure precision/recall function
  3. `run_entity_eval` — driver that loops fixtures + pipeline + scoring

Bundled fixtures (ecommerce / library / blog) live in the same module
as Python factories — small enough to construct in-code, no need for
a separate fixture-file loader at this release. A BIRD-SQL adapter
can land later as a parallel module producing the same
`EntityEvalFixture` shape, sourced from upstream SQL files.
"""

from __future__ import annotations

import pytest

from schemabrain.enrichment.llm import FakeLLMClient
from schemabrain.entities.suggest import EntitySuggestionPipeline
from schemabrain.eval.entity_harness import (
    EntityEvalFixture,
    EntityEvalResult,
    blog_fixture,
    ecommerce_fixture,
    library_fixture,
    run_entity_eval,
    score_entity_candidates,
)

# ----- score_entity_candidates: pure scoring math ---------------------------


class TestScoreEntityCandidates:
    def test_perfect_match_scores_1_0(self) -> None:
        result = score_entity_candidates(
            fixture_name="test",
            actual=("public.users", "public.orders", "public.products"),
            expected=frozenset({"public.users", "public.orders", "public.products"}),
            top_k=3,
        )
        assert result.precision_at_k == pytest.approx(1.0)
        assert result.recall == pytest.approx(1.0)

    def test_zero_matches_scores_0_0(self) -> None:
        result = score_entity_candidates(
            fixture_name="test",
            actual=("public.foo", "public.bar"),
            expected=frozenset({"public.users", "public.orders"}),
            top_k=3,
        )
        assert result.precision_at_k == pytest.approx(0.0)
        assert result.recall == pytest.approx(0.0)

    def test_partial_match_in_top_k(self) -> None:
        # 2 of 3 top-3 are in expected → precision@3 = 2/3.
        result = score_entity_candidates(
            fixture_name="test",
            actual=("public.users", "public.orders", "public.junk"),
            expected=frozenset({"public.users", "public.orders", "public.products"}),
            top_k=3,
        )
        assert result.precision_at_k == pytest.approx(2 / 3)
        # Recall is 2 expected found out of 3 expected, so 2/3.
        assert result.recall == pytest.approx(2 / 3)

    def test_top_k_truncates_actual_for_precision(self) -> None:
        # 5 actual but top_k=3 — precision only counts the first 3.
        # Top-3 has 2 hits → precision@3 = 2/3 (not 3/5).
        result = score_entity_candidates(
            fixture_name="test",
            actual=(
                "public.users",
                "public.junk",
                "public.orders",
                "public.products",
                "public.more_junk",
            ),
            expected=frozenset({"public.users", "public.orders", "public.products"}),
            top_k=3,
        )
        assert result.precision_at_k == pytest.approx(2 / 3)
        # Recall walks the full actual list — finds all 3.
        assert result.recall == pytest.approx(1.0)

    def test_more_expected_than_actual(self) -> None:
        # 1 of 1 actual is right → precision@3 = 1/3 (denominator is k).
        # Wait — what should precision@k be when actual is shorter than k?
        # Standard definition: |top_k(actual) ∩ expected| / min(k, |actual|)
        # so a 1-of-1 perfect match scores precision=1.0, not 0.33.
        result = score_entity_candidates(
            fixture_name="test",
            actual=("public.users",),
            expected=frozenset({"public.users", "public.orders", "public.products"}),
            top_k=3,
        )
        assert result.precision_at_k == pytest.approx(1.0)
        # Recall: 1 of 3 expected found.
        assert result.recall == pytest.approx(1 / 3)

    def test_empty_actual_scores_zero_precision_zero_recall(self) -> None:
        # An LLM run that returned no candidates is unambiguously bad.
        # Both precision@k and recall are 0.
        result = score_entity_candidates(
            fixture_name="test",
            actual=(),
            expected=frozenset({"public.users"}),
            top_k=3,
        )
        assert result.precision_at_k == pytest.approx(0.0)
        assert result.recall == pytest.approx(0.0)

    def test_rejects_empty_expected_set(self) -> None:
        # An empty expected set means "anything goes" — useless for
        # scoring. Surface as a programming error so the fixture
        # author re-curates the ground truth.
        with pytest.raises(ValueError, match="expected"):
            score_entity_candidates(
                fixture_name="test",
                actual=("public.users",),
                expected=frozenset(),
                top_k=3,
            )

    def test_rejects_non_positive_top_k(self) -> None:
        with pytest.raises(ValueError, match="top_k"):
            score_entity_candidates(
                fixture_name="test",
                actual=("public.users",),
                expected=frozenset({"public.users"}),
                top_k=0,
            )


# ----- bundled fixtures: well-formed shape ----------------------------------


class TestBundledFixtures:
    def test_ecommerce_fixture_well_formed(self) -> None:
        fixture = ecommerce_fixture()
        assert fixture.name == "ecommerce"
        # Sanity: at least 3 tables (users, orders, products) and at
        # least 3 expected entities, all binding to existing tables.
        assert len(fixture.tables) >= 3
        assert len(fixture.expected_entity_tables) >= 3
        _assert_bindings_resolve(fixture)

    def test_library_fixture_well_formed(self) -> None:
        fixture = library_fixture()
        assert fixture.name == "library"
        assert len(fixture.tables) >= 3
        assert len(fixture.expected_entity_tables) >= 3
        _assert_bindings_resolve(fixture)

    def test_blog_fixture_well_formed(self) -> None:
        fixture = blog_fixture()
        assert fixture.name == "blog"
        assert len(fixture.tables) >= 3
        assert len(fixture.expected_entity_tables) >= 3
        _assert_bindings_resolve(fixture)

    def test_all_three_fixtures_have_distinct_names(self) -> None:
        # Sanity — the eval driver groups results by fixture name.
        names = {ecommerce_fixture().name, library_fixture().name, blog_fixture().name}
        assert len(names) == 3


def _assert_bindings_resolve(fixture: EntityEvalFixture) -> None:
    """Every expected entity binding must point at a table in the fixture."""
    table_names = {t.qualified_name for t in fixture.tables}
    for expected in fixture.expected_entity_tables:
        assert expected in table_names, (
            f"fixture {fixture.name!r}: expected entity {expected!r} "
            f"has no matching table in the schema"
        )


# ----- run_entity_eval: end-to-end with FakeLLMClient -----------------------


def _build_canned_yaml(*tables: str) -> str:
    """Construct a candidates YAML body binding to the given tables."""
    lines = ["candidates:"]
    for t in tables:
        name = t.split(".")[-1].rstrip("s")  # crude singular from table name
        lines.extend(
            [
                f"  - name: {name}",
                "    binding:",
                f"      single_table: {t}",
                "    identity: id",
                "    confidence: high",
                "    rationale: r",
                "    pii_hints: {}",
            ]
        )
    return "\n".join(lines)


class TestRunEntityEval:
    def test_perfect_recall_produces_passing_results(self) -> None:
        # Stub LLM that returns each fixture's expected entities exactly.
        def stub(_system: str, user: str) -> str:
            if "public.users" in user and "public.orders" in user:
                # ecommerce schema
                return _build_canned_yaml("public.users", "public.orders", "public.products")
            if "public.members" in user:
                # library schema
                return _build_canned_yaml("public.members", "public.books", "public.loans")
            if "public.authors" in user:
                # blog schema
                return _build_canned_yaml("public.authors", "public.posts", "public.comments")
            return "candidates: []"

        client = FakeLLMClient(text_provider=stub)
        pipeline = EntitySuggestionPipeline(llm=client)

        fixtures = [ecommerce_fixture(), library_fixture(), blog_fixture()]
        results = run_entity_eval(fixtures, pipeline, top_k=3)

        assert len(results) == 3
        for result in results:
            assert isinstance(result, EntityEvalResult)
            assert result.precision_at_k == pytest.approx(1.0), (
                f"fixture {result.fixture_name!r} expected perfect precision"
            )

    def test_each_result_carries_its_fixture_name(self) -> None:
        client = FakeLLMClient(text_provider=lambda _s, _u: "candidates: []")
        pipeline = EntitySuggestionPipeline(llm=client)

        fixtures = [ecommerce_fixture(), library_fixture()]
        results = run_entity_eval(fixtures, pipeline, top_k=3)

        names = {r.fixture_name for r in results}
        assert names == {"ecommerce", "library"}

    def test_empty_fixtures_returns_empty_results(self) -> None:
        # Defensive: an empty fixture list shouldn't crash; it should
        # just return an empty result tuple. Useful for parametrising
        # the harness over a filtered subset.
        client = FakeLLMClient(text_provider=lambda _s, _u: "candidates: []")
        pipeline = EntitySuggestionPipeline(llm=client)

        results = run_entity_eval([], pipeline, top_k=3)
        assert results == ()

    def test_aggregate_precision_is_a_property_on_result(self) -> None:
        # The headline metric is per-fixture precision@3. The result
        # also exposes a `passed(threshold)` convenience that the
        # gate consumes.
        client = FakeLLMClient(
            text_provider=lambda _s, _u: _build_canned_yaml(
                "public.users", "public.orders", "public.products"
            )
        )
        pipeline = EntitySuggestionPipeline(llm=client)

        results = run_entity_eval([ecommerce_fixture()], pipeline, top_k=3)
        result = results[0]
        assert result.passed(threshold=0.7) is True
        assert result.passed(threshold=1.01) is False


# ----- EntityEvalResult.passed: threshold gating ----------------------------


class TestEntityEvalResultPassed:
    def test_passed_uses_precision_at_k(self) -> None:
        # The gate is on precision@k, not recall. This pins which
        # metric the threshold applies to so a future contributor
        # can't accidentally flip it to recall.
        result = score_entity_candidates(
            fixture_name="test",
            actual=("public.users", "public.orders", "public.junk"),
            expected=frozenset({"public.users", "public.orders", "public.products"}),
            top_k=3,
        )
        eval_result = EntityEvalResult(
            fixture_name="test",
            actual_tables=("public.users", "public.orders", "public.junk"),
            expected_tables=frozenset({"public.users", "public.orders", "public.products"}),
            top_k=3,
            precision_at_k=result.precision_at_k,
            recall=result.recall,
        )
        # precision@3 is 2/3 ≈ 0.667 → fails 0.7 threshold.
        assert eval_result.passed(threshold=0.7) is False
        # But passes 0.5.
        assert eval_result.passed(threshold=0.5) is True
