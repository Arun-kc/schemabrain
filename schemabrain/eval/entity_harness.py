"""Entity-suggest eval harness — precision@k scorer + bundled fixtures.

Mirrors the shape of `runner.py` (the find_relevant_tables eval) but
for the entity-suggest pipeline. Three concerns:

  1. `EntityEvalFixture` — a small schema (Table list) plus the
     ground-truth set of qualified-table bindings that a competent
     entity-suggest run should produce.

  2. `score_entity_candidates` — pure function: actual candidates
     (ordered) vs expected (set), returns precision@k + recall.

  3. `run_entity_eval` — driver. Loops fixtures, runs the pipeline,
     extracts qualified_table bindings, scores against the expected
     set, returns one `EntityEvalResult` per fixture.

Bundled fixtures (`ecommerce_fixture`, `library_fixture`,
`blog_fixture`) ship as Python factories — small enough to inline,
no need for a YAML loader at v1. A BIRD-SQL adapter slots in later as
a separate module producing the same `EntityEvalFixture` shape.

Headline metric: per-fixture precision@k. The gate (currently 0.7
top-3 in CI smoke runs) is `EntityEvalResult.passed(threshold=0.7)`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from schemabrain.core.models import Column, ForeignKey, Table
from schemabrain.entities.suggest import EntitySuggestionPipeline


@dataclass(frozen=True)
class EntityEvalFixture:
    """One eval scenario: schema + expected entity bindings.

    `expected_entity_tables` is the *set* of qualified-table bindings
    a correct suggest run should produce — order is not pinned, so
    "users, orders, products" and "products, orders, users" both
    count as perfect recall.
    """

    name: str
    tables: tuple[Table, ...]
    expected_entity_tables: frozenset[str]


@dataclass(frozen=True)
class EntityEvalResult:
    """One fixture's score plus enough context to render a report.

    `passed(threshold)` is the gate convenience the CLI consumes —
    a future contributor flipping the metric to recall would have to
    edit the method body, not silently re-bind the threshold.
    """

    fixture_name: str
    actual_tables: tuple[str, ...]
    expected_tables: frozenset[str]
    top_k: int
    precision_at_k: float
    recall: float

    def passed(self, *, threshold: float) -> bool:
        """Did this fixture clear the precision@k gate?

        Headline metric is precision@k — pinned here so a future
        contributor can't silently swap to recall.
        """
        return self.precision_at_k >= threshold


def score_entity_candidates(
    *,
    fixture_name: str,
    actual: Sequence[str],
    expected: frozenset[str],
    top_k: int,
) -> EntityEvalResult:
    """Score one fixture's suggestion run.

    `actual` is the ordered list of qualified-table bindings the LLM
    returned. `expected` is the ground-truth set. `top_k` is the cap
    the pipeline asked for; precision@k considers only the first k
    entries of `actual`. `fixture_name` flows through to the returned
    `EntityEvalResult` so a downstream report can group results by
    fixture without re-wrapping; required to avoid a "two-phase
    construction" footgun where a caller forgets to set the name.

    Precision@k = |actual[:k] ∩ expected| / min(k, |actual|)

    The denominator is `min(k, |actual|)` (not `k`) so a 1-of-1
    perfect match scores precision=1.0 instead of 1/3 — the latter
    would penalise the LLM for *not* hallucinating extra wrong
    candidates to fill the slate, which is the wrong incentive.

    Recall = |actual ∩ expected| / |expected|. Walks the full
    `actual` list, not just top-k.

    Raises:
      ValueError: if `expected` is empty or `top_k <= 0`.
    """
    if not expected:
        raise ValueError("expected entity set must be non-empty for scoring")
    if top_k <= 0:
        raise ValueError(f"top_k must be a positive integer (got {top_k!r})")

    top_k_actual = tuple(actual[:top_k])
    top_k_hits = sum(1 for t in top_k_actual if t in expected)
    precision_at_k = top_k_hits / len(top_k_actual) if top_k_actual else 0.0

    actual_set = set(actual)
    full_hits = sum(1 for t in expected if t in actual_set)
    recall = full_hits / len(expected)

    return EntityEvalResult(
        fixture_name=fixture_name,
        actual_tables=tuple(actual),
        expected_tables=expected,
        top_k=top_k,
        precision_at_k=precision_at_k,
        recall=recall,
    )


def run_entity_eval(
    fixtures: Sequence[EntityEvalFixture],
    pipeline: EntitySuggestionPipeline,
    *,
    top_k: int,
) -> tuple[EntityEvalResult, ...]:
    """Loop fixtures, score each with `pipeline`, return per-fixture results.

    The pipeline is invoked once per fixture; cost ceiling (if the
    pipeline wraps a `CostCeilingGuard`) is enforced across all
    fixtures in the same run.

    **All-or-nothing semantics.** If any fixture's pipeline call
    raises (`SuggestionParseError`, `CostCeilingExceededError`,
    network error, etc.), the exception propagates immediately —
    results from fixtures already scored in the same run are NOT
    returned to the caller. Callers needing per-fixture failure
    isolation must wrap the per-fixture loop themselves. This
    deliberately keeps the eval harness pure (no error envelopes,
    no Result-monad shape) so a CI gate failure is a hard stop, not
    a silent partial-pass.
    """
    results: list[EntityEvalResult] = []
    for fixture in fixtures:
        suggestion = pipeline.propose_from_tables(fixture.tables, top_k=top_k)
        actual_tables = tuple(c.entity.qualified_table for c in suggestion.candidates)
        results.append(
            score_entity_candidates(
                fixture_name=fixture.name,
                actual=actual_tables,
                expected=fixture.expected_entity_tables,
                top_k=top_k,
            )
        )
    return tuple(results)


# ----- bundled fixtures ------------------------------------------------------
#
# Three small schemas with curated ground-truth entity bindings.
# Picked for variety in shape (ecommerce: classic users + orders + FK,
# library: members + books + many-to-many loans, blog: hierarchical
# authors -> posts -> comments) to cover the patterns a real-world
# suggest pipeline should handle.


def _col(
    name: str,
    table: str,
    *,
    data_type: str = "bigint",
    nullable: bool = True,
    is_pk: bool = False,
    ordinal: int = 1,
    schema: str = "public",
) -> Column:
    """Compact Column constructor for fixture authoring."""
    return Column(
        name=name,
        table_name=table,
        schema_name=schema,
        data_type=data_type,
        nullable=nullable,
        ordinal_position=ordinal,
        is_primary_key=is_pk,
    )


def ecommerce_fixture() -> EntityEvalFixture:
    """Classic ecommerce: users + orders + products. Mirrors the
    bundled YAML demo fixtures, so a suggest run against this schema
    should propose exactly the 3 demo entities.
    """
    users = Table(
        name="users",
        schema_name="public",
        columns=(
            _col("id", "users", is_pk=True, nullable=False, ordinal=1),
            _col("email", "users", data_type="text", nullable=False, ordinal=2),
            _col("created_at", "users", data_type="timestamp", nullable=False, ordinal=3),
        ),
    )
    orders = Table(
        name="orders",
        schema_name="public",
        columns=(
            _col("id", "orders", is_pk=True, nullable=False, ordinal=1),
            _col("user_id", "orders", nullable=False, ordinal=2),
            _col("status", "orders", data_type="text", nullable=False, ordinal=3),
            _col("placed_at", "orders", data_type="timestamp", nullable=False, ordinal=4),
        ),
        foreign_keys=(
            ForeignKey(
                name="orders_user_id_fkey",
                source_columns=("user_id",),
                target_schema="public",
                target_table="users",
                target_columns=("id",),
            ),
        ),
    )
    products = Table(
        name="products",
        schema_name="public",
        columns=(
            _col("id", "products", is_pk=True, nullable=False, ordinal=1),
            _col("sku", "products", data_type="text", nullable=False, ordinal=2),
            _col("name", "products", data_type="text", nullable=False, ordinal=3),
            _col("price_cents", "products", data_type="integer", nullable=False, ordinal=4),
        ),
    )
    return EntityEvalFixture(
        name="ecommerce",
        tables=(users, orders, products),
        expected_entity_tables=frozenset(
            {
                "public.users",
                "public.orders",
                "public.products",
            }
        ),
    )


def library_fixture() -> EntityEvalFixture:
    """Library checkout system: members borrow books, loans track each
    transaction. Tests whether the LLM correctly treats `loans` as an
    entity (it has its own identity + transactional attributes) vs as
    a pure junction table (it isn't — has its own attributes).
    """
    members = Table(
        name="members",
        schema_name="public",
        columns=(
            _col("id", "members", is_pk=True, nullable=False, ordinal=1),
            _col("name", "members", data_type="text", nullable=False, ordinal=2),
            _col("email", "members", data_type="text", nullable=False, ordinal=3),
        ),
    )
    books = Table(
        name="books",
        schema_name="public",
        columns=(
            _col("id", "books", is_pk=True, nullable=False, ordinal=1),
            _col("isbn", "books", data_type="text", nullable=False, ordinal=2),
            _col("title", "books", data_type="text", nullable=False, ordinal=3),
        ),
    )
    loans = Table(
        name="loans",
        schema_name="public",
        columns=(
            _col("id", "loans", is_pk=True, nullable=False, ordinal=1),
            _col("member_id", "loans", nullable=False, ordinal=2),
            _col("book_id", "loans", nullable=False, ordinal=3),
            _col("borrowed_at", "loans", data_type="timestamp", nullable=False, ordinal=4),
            _col("returned_at", "loans", data_type="timestamp", nullable=True, ordinal=5),
        ),
        foreign_keys=(
            ForeignKey(
                name="loans_member_id_fkey",
                source_columns=("member_id",),
                target_schema="public",
                target_table="members",
                target_columns=("id",),
            ),
            ForeignKey(
                name="loans_book_id_fkey",
                source_columns=("book_id",),
                target_schema="public",
                target_table="books",
                target_columns=("id",),
            ),
        ),
    )
    return EntityEvalFixture(
        name="library",
        tables=(members, books, loans),
        expected_entity_tables=frozenset(
            {
                "public.members",
                "public.books",
                "public.loans",
            }
        ),
    )


def blog_fixture() -> EntityEvalFixture:
    """Blog content tree: authors write posts, readers leave comments.
    Tests whether the LLM picks up on the author -> post -> comment
    hierarchy as three distinct entities.
    """
    authors = Table(
        name="authors",
        schema_name="public",
        columns=(
            _col("id", "authors", is_pk=True, nullable=False, ordinal=1),
            _col("handle", "authors", data_type="text", nullable=False, ordinal=2),
            _col("display_name", "authors", data_type="text", nullable=False, ordinal=3),
        ),
    )
    posts = Table(
        name="posts",
        schema_name="public",
        columns=(
            _col("id", "posts", is_pk=True, nullable=False, ordinal=1),
            _col("author_id", "posts", nullable=False, ordinal=2),
            _col("title", "posts", data_type="text", nullable=False, ordinal=3),
            _col("body", "posts", data_type="text", nullable=False, ordinal=4),
            _col("published_at", "posts", data_type="timestamp", nullable=True, ordinal=5),
        ),
        foreign_keys=(
            ForeignKey(
                name="posts_author_id_fkey",
                source_columns=("author_id",),
                target_schema="public",
                target_table="authors",
                target_columns=("id",),
            ),
        ),
    )
    comments = Table(
        name="comments",
        schema_name="public",
        columns=(
            _col("id", "comments", is_pk=True, nullable=False, ordinal=1),
            _col("post_id", "comments", nullable=False, ordinal=2),
            _col("author_id", "comments", nullable=False, ordinal=3),
            _col("body", "comments", data_type="text", nullable=False, ordinal=4),
        ),
        foreign_keys=(
            ForeignKey(
                name="comments_post_id_fkey",
                source_columns=("post_id",),
                target_schema="public",
                target_table="posts",
                target_columns=("id",),
            ),
            ForeignKey(
                name="comments_author_id_fkey",
                source_columns=("author_id",),
                target_schema="public",
                target_table="authors",
                target_columns=("id",),
            ),
        ),
    )
    return EntityEvalFixture(
        name="blog",
        tables=(authors, posts, comments),
        expected_entity_tables=frozenset(
            {
                "public.authors",
                "public.posts",
                "public.comments",
            }
        ),
    )
