"""Tests for the two extensibility seams.

These seams lift pricing onto the `LLMClient` Protocol (no central
`cost_usd_for` Anthropic-only dispatch) and switch non-CLI callsites
from the concrete `SQLiteStore` type annotation to the `Store`
Protocol, so a contributor can add a new store backend (or a test
double) without touching the indexer, the eval retriever, or the
four MCP tool modules.

Without these two seams, every new LLM provider would have to extend
`cost_usd_for` (a central function inside the abstraction file), and
every new store backend would have to find/replace 10+ concrete type
annotations.

Why this lives in its own test file: the invariants here are about the
*shape of the seams*, not about the Anthropic adapter, the SQLite
store, or any other concrete. Splitting these tests out from
`test_enrichment_llm.py` / `test_core_store_protocol.py` makes the
"can the seam swap?" property impossible to silently lose in a future
provider-specific refactor — a grep for this file's tests surfaces the
invariants directly.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import pytest

from schemabrain.core.description import ColumnDescription
from schemabrain.core.embedding import ColumnEmbedding
from schemabrain.core.entity import Entity
from schemabrain.core.example_query import ExampleQuery
from schemabrain.core.join import CanonicalJoin
from schemabrain.core.metric import Metric
from schemabrain.core.models import Column, ForeignKey, IncomingForeignKey, Table
from schemabrain.core.store_protocol import Store
from schemabrain.enrichment.anthropic_client import (
    anthropic_haiku_45_client,
    anthropic_sonnet_46_client,
)
from schemabrain.enrichment.llm import (
    FakeLLMClient,
    LLMClient,
    LLMResponse,
    LLMUsage,
    anthropic_cost_fn_for_model,
    haiku_45_cost_usd,
    sonnet_46_cost_usd,
)
from schemabrain.enrichment.pipeline import EnrichmentPipeline


class _FakeAnthropicMessage:
    """Minimal stand-in for an Anthropic SDK message used in construction
    tests. The real SDK isn't needed; we just want the AnthropicClient
    ctor path to execute without touching the network.
    """

    pass


def _users_table() -> Table:
    return Table(
        name="users",
        schema_name="public",
        columns=(
            Column(
                name="id",
                table_name="users",
                schema_name="public",
                data_type="bigint",
                nullable=False,
                ordinal_position=1,
                default=None,
                is_primary_key=True,
            ),
        ),
        foreign_keys=(),
    )


class TestLLMClientCostUsdProtocol:
    """The `LLMClient` Protocol must carry a `cost_usd(usage)` method.

    This is the provider-agnostic cost seam. Before this commit, the
    pipeline called a module-level `cost_usd_for(model, usage)` central
    dispatch that hardcoded Anthropic model regexes. After this commit
    each client owns its pricing: a new provider drops in by
    implementing `cost_usd` on its adapter, no changes to the pipeline
    or to a central pricing table.
    """

    def test_protocol_exposes_cost_usd(self) -> None:
        # Pin the method name on the Protocol so a future rename
        # surfaces here, not as "Protocol doesn't match" in the aggregate.
        assert hasattr(LLMClient, "cost_usd"), (
            "LLMClient Protocol must expose cost_usd(usage) for provider-agnostic cost tracking"
        )

    def test_fake_client_satisfies_protocol(self) -> None:
        # Runtime-checkable Protocol — `isinstance` is structural.
        client = FakeLLMClient(text_provider=lambda s, u: "x")
        assert isinstance(client, LLMClient)


class TestAnthropicClientCostUsd:
    """`AnthropicClient.cost_usd` returns the family rate for its model.

    The Anthropic adapter is constructed per-tier (Haiku factory vs
    Sonnet factory). It knows its model at construction time, so cost
    dispatch is deterministic and the response.model string is never
    consulted for pricing — eliminating the V5-class false-positive
    risk that `cost_usd_for` had to guard against with anchored regex.
    """

    def test_haiku_cost_usd_matches_haiku_rate(self) -> None:
        client = anthropic_haiku_45_client(client=_FakeAnthropicMessage())
        usage = LLMUsage(input_tokens=1_000_000, cached_input_tokens=0, output_tokens=0)
        assert math.isclose(client.cost_usd(usage), 0.80, rel_tol=1e-9)

    def test_sonnet_cost_usd_matches_sonnet_rate(self) -> None:
        client = anthropic_sonnet_46_client(client=_FakeAnthropicMessage())
        usage = LLMUsage(input_tokens=1_000_000, cached_input_tokens=0, output_tokens=0)
        assert math.isclose(client.cost_usd(usage), 3.00, rel_tol=1e-9)

    def test_haiku_with_date_suffix_resolves_at_construction(self) -> None:
        # The real Anthropic API stamps a date on the resolved model
        # (e.g. `claude-haiku-4-5-20251001`). Our ctor must accept that
        # AND keep Haiku pricing — the date stamp doesn't change the
        # pricing family.
        from schemabrain.enrichment.anthropic_client import AnthropicClient

        client = AnthropicClient(
            model="claude-haiku-4-5-20251001",
            client=_FakeAnthropicMessage(),
        )
        usage = LLMUsage(input_tokens=1_000_000, cached_input_tokens=0, output_tokens=0)
        assert math.isclose(client.cost_usd(usage), 0.80, rel_tol=1e-9)

    def test_sonnet_with_date_suffix_resolves_at_construction(self) -> None:
        from schemabrain.enrichment.anthropic_client import AnthropicClient

        client = AnthropicClient(
            model="claude-sonnet-4-6-20260301",
            client=_FakeAnthropicMessage(),
        )
        usage = LLMUsage(input_tokens=1_000_000, cached_input_tokens=0, output_tokens=0)
        assert math.isclose(client.cost_usd(usage), 3.00, rel_tol=1e-9)


class TestAnthropicClientConstructionValidation:
    """Unknown models fail at construction, not at first API call.

    Before this commit, a programming error (routing to a model without
    a pricing entry) surfaced AFTER an LLM call succeeded — wasting
    money and leaving the cost-counter inconsistent. Fail-fast at
    construction eliminates that window.
    """

    def test_unknown_model_raises_at_construction(self) -> None:
        from schemabrain.enrichment.anthropic_client import AnthropicClient

        with pytest.raises(ValueError, match=r"unknown|pricing"):
            AnthropicClient(
                model="claude-opus-4-5",
                client=_FakeAnthropicMessage(),
            )

    def test_empty_model_raises_at_construction(self) -> None:
        from schemabrain.enrichment.anthropic_client import AnthropicClient

        with pytest.raises(ValueError):
            AnthropicClient(model="", client=_FakeAnthropicMessage())

    def test_numeric_extension_does_not_false_positive_haiku(self) -> None:
        # `claude-haiku-4-50` is a hypothetical successor; the anchored
        # regex must reject it so we don't silently mis-bill at Haiku
        # rates. Validated at construction, not at response time.
        from schemabrain.enrichment.anthropic_client import AnthropicClient

        with pytest.raises(ValueError, match=r"unknown|pricing"):
            AnthropicClient(
                model="claude-haiku-4-50",
                client=_FakeAnthropicMessage(),
            )

    def test_numeric_extension_does_not_false_positive_sonnet(self) -> None:
        from schemabrain.enrichment.anthropic_client import AnthropicClient

        with pytest.raises(ValueError, match=r"unknown|pricing"):
            AnthropicClient(
                model="claude-sonnet-4-60",
                client=_FakeAnthropicMessage(),
            )

    def test_substring_with_no_dash_does_not_false_positive(self) -> None:
        from schemabrain.enrichment.anthropic_client import AnthropicClient

        with pytest.raises(ValueError, match=r"unknown|pricing"):
            AnthropicClient(
                model="claude-haiku-4-5xyz",
                client=_FakeAnthropicMessage(),
            )


class TestFakeLLMClientCostUsd:
    """`FakeLLMClient.cost_usd` mirrors AnthropicClient semantics.

    The test double has to behave like the production adapter for
    cost dispatch — otherwise tests that pass with the fake might fail
    with the real client. Same pattern: family resolved at construction
    via the model field; cost_usd uses that.
    """

    def test_default_haiku_cost(self) -> None:
        client = FakeLLMClient(text_provider=lambda s, u: "x")  # model defaults to Haiku
        usage = LLMUsage(input_tokens=1_000_000, cached_input_tokens=0, output_tokens=0)
        assert math.isclose(client.cost_usd(usage), 0.80, rel_tol=1e-9)

    def test_sonnet_cost_when_overridden(self) -> None:
        client = FakeLLMClient(text_provider=lambda s, u: "x", model="claude-sonnet-4-6")
        usage = LLMUsage(input_tokens=1_000_000, cached_input_tokens=0, output_tokens=0)
        assert math.isclose(client.cost_usd(usage), 3.00, rel_tol=1e-9)

    def test_unknown_model_raises_at_construction(self) -> None:
        # The fake must fail-fast in the same shape as AnthropicClient.
        # Without this, a test could pass with FakeLLMClient(model="x")
        # and fail in production once a real ctor validation tripped.
        with pytest.raises(ValueError, match=r"unknown|pricing"):
            FakeLLMClient(text_provider=lambda s, u: "x", model="some-other-model")


class TestPipelineUsesClientCostUsd:
    """`EnrichmentPipeline.enrich_column` calls `client.cost_usd(usage)`,
    not a central `cost_usd_for(model, usage)` dispatch.

    This is the proof that the pipeline binds to the Protocol seam and
    not to Anthropic-specific shape. A custom LLMClient with completely
    different pricing must flow through the pipeline unchanged.
    """

    def test_custom_client_with_flat_rate_pricing_flows_through_pipeline(self) -> None:
        # A hypothetical "future provider" client with a flat $0.01 per
        # call regardless of usage. Proves cost dispatch is owned by
        # the client, not by a central Anthropic-shaped function.
        @dataclass
        class FlatRateClient:
            calls: list[tuple[str, str]]

            def complete(self, *, system: str, user: str) -> LLMResponse:
                self.calls.append((system, user))
                return LLMResponse(
                    text="hello",
                    model="future-provider-v1",  # Not Anthropic-shaped.
                    usage=LLMUsage(input_tokens=10, cached_input_tokens=0, output_tokens=2),
                )

            def cost_usd(self, usage: LLMUsage) -> float:
                # Pricing the central dispatch never knew about.
                return 0.01

        client = FlatRateClient(calls=[])
        pipeline = EnrichmentPipeline(client=client, max_cost_usd=0.05)
        table = _users_table()
        desc = pipeline.enrich_column(
            table=table, column=table.columns[0], stats=None, fk_targets=()
        )
        # The custom client computed the cost — not a central regex.
        assert math.isclose(desc.cost_usd, 0.01, rel_tol=1e-9)
        assert math.isclose(pipeline.spent_usd, 0.01, rel_tol=1e-9)
        assert len(client.calls) == 1

    def test_custom_client_cost_dispatch_is_what_pipeline_records(self) -> None:
        # If the client returns a specific cost, the pipeline records
        # that exact amount — proves no central dispatch sneaks in.
        @dataclass
        class FixedCostClient:
            cost: float
            calls: list[tuple[str, str]]

            def complete(self, *, system: str, user: str) -> LLMResponse:
                self.calls.append((system, user))
                return LLMResponse(
                    text="x",
                    model="any-name",
                    usage=LLMUsage(input_tokens=1, cached_input_tokens=0, output_tokens=1),
                )

            def cost_usd(self, usage: LLMUsage) -> float:
                return self.cost

        client = FixedCostClient(cost=0.0042, calls=[])
        pipeline = EnrichmentPipeline(client=client, max_cost_usd=1.0)
        table = _users_table()
        desc = pipeline.enrich_column(
            table=table, column=table.columns[0], stats=None, fk_targets=()
        )
        assert desc.cost_usd == 0.0042

    def test_client_cost_raises_propagates(self) -> None:
        # When the client's cost_usd raises, the pipeline propagates the
        # exception. Replaces the old "unknown model in response"
        # invariant — the new shape makes that case impossible at the
        # adapter (validated at construction), but a client whose
        # cost_usd raises for runtime reasons (e.g. provider returns a
        # malformed usage payload) must still surface cleanly.
        @dataclass
        class BrokenCostClient:
            calls: list[tuple[str, str]]

            def complete(self, *, system: str, user: str) -> LLMResponse:
                self.calls.append((system, user))
                return LLMResponse(
                    text="x",
                    model="custom",
                    usage=LLMUsage(input_tokens=1, cached_input_tokens=0, output_tokens=1),
                )

            def cost_usd(self, usage: LLMUsage) -> float:
                raise RuntimeError("malformed usage from provider")

        client = BrokenCostClient(calls=[])
        pipeline = EnrichmentPipeline(client=client, max_cost_usd=1.0)
        table = _users_table()
        with pytest.raises(RuntimeError, match="malformed"):
            pipeline.enrich_column(table=table, column=table.columns[0], stats=None, fk_targets=())
        # The LLM call DID happen; only cost-recording failed.
        assert len(client.calls) == 1
        # spent_usd not advanced past failure — same shape as before.
        assert pipeline.spent_usd == 0.0


class TestStoreProtocolSeamUsable:
    """Non-CLI code paths accept ANY `Store`-Protocol-conforming object.

    Before this commit, `indexer.index()`, `EmbeddingRetriever`, and the
    four MCP tool modules typed their `store` parameter as the concrete
    `SQLiteStore`. The `Store` Protocol existed (introduced in Commit 1)
    but wasn't used by anything except the test for the Protocol itself.

    After this commit, the type annotations on those 10 callsites are
    `Store`, so a contributor adding a new backend (or a test double)
    can drop in without re-typing the entire pipeline.

    This test pins the property: a minimal in-memory Store impl can be
    passed where the production code expects a Store.
    """

    def test_minimal_in_memory_store_satisfies_protocol(self) -> None:
        # The simplest possible Store impl — no real persistence, just
        # the method shapes the Protocol demands. The test proves the
        # Protocol's structural check accepts any conformant class.
        class _InMemoryStore:
            writer_lock = False

            def __enter__(self) -> _InMemoryStore:
                return self

            def __exit__(self, *args: object) -> None:
                pass

            def close(self) -> None:
                pass

            def write_table(self, table: Table, *, source_connection_id: str) -> None:
                pass

            def get_table(
                self, schema_name: str, name: str, *, source_connection_id: str
            ) -> Table | None:
                return None

            def delete_table(
                self, schema_name: str, name: str, *, source_connection_id: str
            ) -> None:
                pass

            def list_tables(
                self, *, source_connection_id: str | None = None
            ) -> list[tuple[str, str]]:
                return []

            def get_table_fingerprints(
                self, schema_name: str, name: str, *, source_connection_id: str
            ) -> dict[str, tuple[str, str]]:
                return {}

            def write_table_fingerprints(
                self,
                schema_name: str,
                name: str,
                *,
                source_connection_id: str,
                fingerprints: dict[str, tuple[str, str]],
            ) -> None:
                pass

            def get_table_descriptions(
                self, schema_name: str, name: str, *, source_connection_id: str
            ) -> dict[str, ColumnDescription]:
                return {}

            def write_table_descriptions(
                self,
                schema_name: str,
                name: str,
                *,
                source_connection_id: str,
                descriptions: dict[str, ColumnDescription],
            ) -> None:
                pass

            def get_foreign_keys_targeting(
                self,
                target_schema: str,
                target_table: str,
                target_column: str,
                *,
                source_connection_id: str,
            ) -> list[IncomingForeignKey]:
                return []

            def list_all_foreign_keys(
                self, *, source_connection_id: str
            ) -> list[tuple[str, str, ForeignKey]]:
                return []

            def get_table_embeddings(
                self, schema_name: str, name: str, *, source_connection_id: str
            ) -> dict[str, ColumnEmbedding]:
                return {}

            def write_table_embeddings(
                self,
                schema_name: str,
                name: str,
                *,
                source_connection_id: str,
                embeddings: dict[str, ColumnEmbedding],
            ) -> None:
                pass

            def search_embeddings_topk(
                self,
                query_vector: list[float],
                *,
                source_connection_id: str,
                k: int,
            ) -> list[tuple[str, str, str, float]]:
                return []

            def write_example_queries(
                self,
                rows: list[ExampleQuery],
                *,
                source_connection_id: str,
            ) -> int:
                return len(rows)

            def list_example_queries(
                self,
                schema: str,
                table: str,
                *,
                source_connection_id: str,
                limit: int,
            ) -> list[ExampleQuery]:
                return []

            def list_all_example_queries(
                self,
                *,
                source_connection_id: str,
            ) -> list[ExampleQuery]:
                return []

            def get_spend_usd(self, *, source_connection_id: str) -> float:
                return 0.0

            def add_spend_usd(self, *, source_connection_id: str, amount_usd: float) -> float:
                return amount_usd

            def write_entity(self, entity: Entity, *, source_connection_id: str) -> None:
                pass

            def get_entity(self, name: str, *, source_connection_id: str) -> Entity | None:
                return None

            def list_entities(self, *, source_connection_id: str | None = None) -> list[Entity]:
                return []

            def write_canonical_join(
                self, join: CanonicalJoin, *, source_connection_id: str
            ) -> None:
                pass

            def get_canonical_join(
                self, name: str, *, source_connection_id: str
            ) -> CanonicalJoin | None:
                return None

            def list_canonical_joins(
                self, *, source_connection_id: str | None = None
            ) -> list[CanonicalJoin]:
                return []

            def resolve_canonical_joins(
                self,
                entity_a: str,
                entity_b: str,
                *,
                source_connection_id: str,
            ) -> list[CanonicalJoin]:
                return []

            def write_metric(self, metric: Metric, *, source_connection_id: str) -> None:
                pass

            def get_metric(self, name: str, *, source_connection_id: str) -> Metric | None:
                return None

            def list_metrics(self, *, source_connection_id: str | None = None) -> list[Metric]:
                return []

            def write_column_pii_tags(
                self,
                *,
                source_connection_id: str,
                qualified_table: str,
                tags: Mapping[str, tuple[str, frozenset[str]]],
            ) -> None:
                pass

            def get_column_pii_tags(
                self,
                *,
                source_connection_id: str,
                qualified_table: str,
                columns: Iterable[str],
            ) -> dict[str, tuple[str, frozenset[str]]]:
                return {}

        store = _InMemoryStore()
        assert isinstance(store, Store), (
            "A minimal in-memory Store impl must satisfy the Protocol "
            "via structural isinstance — proves the seam is real and "
            "any contributor can swap in a new backend by satisfying "
            "the same surface."
        )


class TestAnthropicResolverWhitespaceTolerance:
    """`anthropic_cost_fn_for_model` strips whitespace from model strings.

    Anthropic API responses have been observed returning model names with
    formatting artifacts (leading or trailing whitespace). Without
    stripping, a benign whitespace artifact would raise `ValueError` at
    adapter construction — silently breaking production while passing
    every test that uses literal-string model names.
    """

    def test_strips_leading_whitespace(self) -> None:
        cost_fn = anthropic_cost_fn_for_model("  claude-haiku-4-5")
        usage = LLMUsage(input_tokens=1_000_000, cached_input_tokens=0, output_tokens=0)
        assert math.isclose(cost_fn(usage), 0.80, rel_tol=1e-9)

    def test_strips_trailing_whitespace(self) -> None:
        cost_fn = anthropic_cost_fn_for_model("claude-sonnet-4-6  ")
        usage = LLMUsage(input_tokens=1_000_000, cached_input_tokens=0, output_tokens=0)
        assert math.isclose(cost_fn(usage), 3.00, rel_tol=1e-9)

    def test_strips_both_ends_with_date_suffix(self) -> None:
        # Real-world shape: API stamps a date AND wraps in whitespace.
        cost_fn = anthropic_cost_fn_for_model("  claude-haiku-4-5-20251001  ")
        usage = LLMUsage(input_tokens=1_000_000, cached_input_tokens=0, output_tokens=0)
        assert math.isclose(cost_fn(usage), 0.80, rel_tol=1e-9)


class TestFakeLLMClientReadOnlyModel:
    """`FakeLLMClient.model` is read-only.

    Changing `model` after construction would leave `_cost_fn` stale and
    silently mis-price subsequent calls. `AnthropicClient` already solves
    this with a read-only `@property` backed by `self._model`. The test
    double must match — otherwise tests that pass with the fake would
    fail with the real client.
    """

    def test_model_setter_raises_attribute_error(self) -> None:
        client = FakeLLMClient(text_provider=lambda s, u: "x")
        with pytest.raises(AttributeError):
            client.model = "claude-sonnet-4-6"  # type: ignore[misc]

    def test_model_remains_value_passed_at_construction(self) -> None:
        client = FakeLLMClient(text_provider=lambda s, u: "x", model="claude-sonnet-4-6")
        assert client.model == "claude-sonnet-4-6"

    def test_cost_usd_uses_construction_time_model(self) -> None:
        # Belt-and-braces: even if a future contributor adds a setter,
        # `cost_usd` must reflect the construction-time pricing — not a
        # post-construction mutation. This pins the invariant from the
        # other side.
        client = FakeLLMClient(text_provider=lambda s, u: "x", model="claude-sonnet-4-6")
        usage = LLMUsage(input_tokens=1_000_000, cached_input_tokens=0, output_tokens=0)
        # Sonnet rate at 1M tokens = $3.00 — proves the Sonnet
        # cost function was bound, not Haiku's.
        assert math.isclose(client.cost_usd(usage), 3.00, rel_tol=1e-9)


class TestUsageCostFunctionsRemainExported:
    """`haiku_45_cost_usd` and `sonnet_46_cost_usd` stay as public
    functions in `llm.py`.

    The central `cost_usd_for(model, usage)` dispatch is gone (that
    WAS the leak — central pricing dispatch + provider-name parsing
    inside the abstraction file). The two per-family pure functions
    stay public because they're how adapters compose their own
    `cost_usd` method. A new provider's adapter does NOT need to
    re-derive Anthropic pricing math; it just needs its own per-model
    functions.
    """

    def test_haiku_cost_usd_callable(self) -> None:
        usage = LLMUsage(input_tokens=100, cached_input_tokens=0, output_tokens=10)
        cost = haiku_45_cost_usd(usage)
        assert cost > 0.0

    def test_sonnet_cost_usd_callable(self) -> None:
        usage = LLMUsage(input_tokens=100, cached_input_tokens=0, output_tokens=10)
        cost = sonnet_46_cost_usd(usage)
        assert cost > 0.0

    def test_cost_usd_for_central_dispatch_is_removed(self) -> None:
        # Document the removal. If someone re-adds `cost_usd_for` as a
        # central Anthropic-only dispatch, this test fails — protecting
        # the seam invariant.
        import schemabrain.enrichment.llm as llm_module

        assert not hasattr(llm_module, "cost_usd_for"), (
            "cost_usd_for was the Anthropic-only central dispatch leak. "
            "Per-client cost_usd(usage) is the seam now; re-adding the "
            "central function would re-introduce the leak the architect "
            "flagged on 2026-05-15."
        )
