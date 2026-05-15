"""Tests for `EnrichmentPipeline.enrich_columns_async`.

Three responsibility lines covered here:

1. **Async batch enrichment** — `enrich_columns_async(...)` accepts a
   list of columns from one table and runs LLM calls concurrently via
   `asyncio.gather`. Each task wraps the sync `LLMClient.complete` call
   in `asyncio.to_thread` (no Protocol refactor required). Per-tier
   concurrency is bounded by `asyncio.Semaphore` (separate limits for
   Haiku-tier and Sonnet-tier clients).

2. **Cost ledger persistence** — when a `Store` and a
   `source_connection_id` are wired into the pipeline at construction,
   cumulative spend is persisted to the ledger after each successful
   call. A process crash mid-enrichment doesn't reset the counter;
   re-runs see the prior total and refuse calls that would breach the
   cap. Without a store, the pipeline falls back to in-memory-only
   tracking (legacy mode for tests).

3. **Runtime guard on `cost_usd` return value** — if
   `client.cost_usd(usage)` returns a non-finite or negative value,
   the pipeline raises `RuntimeError` BEFORE the value can corrupt
   `_spent_usd` or the ledger.

The two-tier routing (Haiku for clear-name columns, Sonnet for cryptic
ones) continues to work the same in async mode — the routing decision
happens per-task before semaphore acquisition.
"""

from __future__ import annotations

import asyncio
import math
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.enrichment.llm import FakeLLMClient, LLMResponse, LLMUsage
from schemabrain.enrichment.pipeline import (
    CostCapExceeded,
    EnrichmentPipeline,
)


def _table_with_columns(*column_names: str) -> Table:
    """Construct a Table with the given column names, all BIGINT NOT NULL."""
    return Table(
        name="t",
        schema_name="public",
        columns=tuple(
            Column(
                name=col_name,
                table_name="t",
                schema_name="public",
                data_type="BIGINT",
                nullable=False,
                ordinal_position=i + 1,
                default=None,
                is_primary_key=False,
            )
            for i, col_name in enumerate(column_names)
        ),
        foreign_keys=(),
    )


class _InstrumentedFakeClient:
    """`LLMClient` test double that records in-flight count for semaphore tests.

    Counts how many `complete()` calls are simultaneously inside the
    method body. Used to assert per-tier semaphore bounds — the
    pipeline should never let in-flight > limit.

    Also supports an optional `sleep_seconds` delay to simulate
    network latency so the test can observe concurrency in wall-clock
    terms (N tasks at 100ms each should finish in ~100ms with
    concurrency >= N, not 100*N ms).
    """

    def __init__(self, *, model: str = "claude-haiku-4-5", sleep_seconds: float = 0.0) -> None:
        from schemabrain.enrichment.llm import anthropic_cost_fn_for_model

        self._cost_fn = anthropic_cost_fn_for_model(model)
        self._model = model
        self._sleep_seconds = sleep_seconds
        self._in_flight = 0
        self._max_in_flight = 0
        self._lock = threading.Lock()
        self.calls: list[tuple[str, str]] = []

    @property
    def model(self) -> str:
        return self._model

    @property
    def max_in_flight(self) -> int:
        return self._max_in_flight

    def complete(self, *, system: str, user: str) -> LLMResponse:
        with self._lock:
            self._in_flight += 1
            self._max_in_flight = max(self._max_in_flight, self._in_flight)
        try:
            if self._sleep_seconds > 0:
                time.sleep(self._sleep_seconds)
            self.calls.append((system, user))
            return LLMResponse(
                text="desc",
                model=self._model,
                usage=LLMUsage(input_tokens=50, cached_input_tokens=0, output_tokens=10),
            )
        finally:
            with self._lock:
                self._in_flight -= 1

    def cost_usd(self, usage: LLMUsage) -> float:
        return self._cost_fn(usage)


class TestEnrichColumnsAsyncBasic:
    """Smoke tests: the async method returns the right shape and content.

    `enrich_columns_async` should match the serial path's output for
    the same inputs — the optimisation is transparent to callers.
    """

    def test_returns_dict_keyed_by_column_name(self) -> None:
        client = FakeLLMClient(text_provider=lambda s, u: "a description")
        pipeline = EnrichmentPipeline(client=client)
        table = _table_with_columns("id", "email", "name")
        result = asyncio.run(
            pipeline.enrich_columns_async(
                table=table,
                columns=list(table.columns),
                stats_by_col={},
                fk_targets_by_col={},
            )
        )
        assert set(result.keys()) == {"id", "email", "name"}
        for desc in result.values():
            assert desc.text == "a description"

    def test_empty_columns_returns_empty_dict(self) -> None:
        client = FakeLLMClient(text_provider=lambda s, u: "x")
        pipeline = EnrichmentPipeline(client=client)
        table = _table_with_columns("id")
        result = asyncio.run(
            pipeline.enrich_columns_async(
                table=table,
                columns=[],
                stats_by_col={},
                fk_targets_by_col={},
            )
        )
        assert result == {}
        assert pipeline.spent_usd == 0.0

    def test_parity_with_serial_enrich_column(self) -> None:
        # Same client, same column → both paths produce identical
        # ColumnDescription text + cost. Pins the contract that async
        # is a pure optimisation, not a behaviour change.
        client = FakeLLMClient(text_provider=lambda s, u: "parity description")
        pipeline_serial = EnrichmentPipeline(client=client)
        table = _table_with_columns("id")
        serial_desc = pipeline_serial.enrich_column(
            table=table, column=table.columns[0], stats=None, fk_targets=()
        )

        client2 = FakeLLMClient(text_provider=lambda s, u: "parity description")
        pipeline_async = EnrichmentPipeline(client=client2)
        async_result = asyncio.run(
            pipeline_async.enrich_columns_async(
                table=table,
                columns=list(table.columns),
                stats_by_col={},
                fk_targets_by_col={},
            )
        )
        async_desc = async_result["id"]
        assert serial_desc.text == async_desc.text
        assert serial_desc.cost_usd == async_desc.cost_usd
        assert serial_desc.model == async_desc.model


class TestConcurrentExecutionSpeedsUp:
    """N concurrent tasks should complete in roughly `per_call_time`,
    not `N x per_call_time`.

    Wall-clock test: 4 tasks at 100ms simulated latency should take
    ~100-200ms with default concurrency (8), not ~400ms. Bound the
    upper limit loosely to absorb CI jitter.
    """

    def test_four_columns_complete_concurrently(self) -> None:
        client = _InstrumentedFakeClient(sleep_seconds=0.1)
        pipeline = EnrichmentPipeline(client=client, default_concurrency=8)
        table = _table_with_columns("c1", "c2", "c3", "c4")
        started = time.monotonic()
        asyncio.run(
            pipeline.enrich_columns_async(
                table=table,
                columns=list(table.columns),
                stats_by_col={},
                fk_targets_by_col={},
            )
        )
        elapsed = time.monotonic() - started
        # 4 serial calls would be ~400ms. Concurrent should be <250ms
        # even with thread-pool spin-up and CI jitter.
        assert elapsed < 0.25, (
            f"expected <250ms for 4 concurrent 100ms calls, got {elapsed * 1000:.0f}ms"
        )
        # Concurrency was actually exercised.
        assert client.max_in_flight >= 2


class TestPerTierSemaphoreBounds:
    """The Haiku-tier semaphore must NEVER allow > N concurrent Haiku
    calls. Same for Sonnet. Pinned via the instrumented `max_in_flight`
    counter.
    """

    def test_default_tier_in_flight_never_exceeds_limit(self) -> None:
        client = _InstrumentedFakeClient(sleep_seconds=0.05)
        pipeline = EnrichmentPipeline(client=client, default_concurrency=3)
        # 10 columns, concurrency=3 → max_in_flight should be exactly 3
        # at the peak, never higher.
        table = _table_with_columns(*[f"c{i}" for i in range(10)])
        asyncio.run(
            pipeline.enrich_columns_async(
                table=table,
                columns=list(table.columns),
                stats_by_col={},
                fk_targets_by_col={},
            )
        )
        assert client.max_in_flight <= 3, (
            f"semaphore breached: max_in_flight={client.max_in_flight}, limit=3"
        )

    def test_cryptic_tier_has_separate_semaphore(self) -> None:
        # Cryptic columns route to the Sonnet-tier client; the
        # Sonnet semaphore is independent of the Haiku one.
        haiku_client = _InstrumentedFakeClient(model="claude-haiku-4-5", sleep_seconds=0.05)
        sonnet_client = _InstrumentedFakeClient(model="claude-sonnet-4-6", sleep_seconds=0.05)
        pipeline = EnrichmentPipeline(
            client=haiku_client,
            cryptic_client=sonnet_client,
            default_concurrency=2,
            cryptic_concurrency=2,
        )
        # Mix of clear + cryptic names (heuristic from routing.py:
        # >=3 underscore-separated segments + avg seg len < 3.5).
        table = _table_with_columns(
            "id",
            "email",
            "acct_dim_v3",  # cryptic
            "pmt_fct_h",  # cryptic
            "name",
            "age",
        )
        asyncio.run(
            pipeline.enrich_columns_async(
                table=table,
                columns=list(table.columns),
                stats_by_col={},
                fk_targets_by_col={},
            )
        )
        # Each tier had its own semaphore bound.
        assert haiku_client.max_in_flight <= 2
        assert sonnet_client.max_in_flight <= 2
        # Both tiers actually got traffic (otherwise the test asserts nothing).
        assert len(haiku_client.calls) > 0
        assert len(sonnet_client.calls) > 0


class TestCostCapEnforcedConcurrently:
    """Cap enforcement under concurrency.

    Pre-call check refuses to dispatch a task if `_spent_usd >= cap`.
    Post-call overshoot is bounded by `concurrency x max_per_call_cost`
    because in-flight tasks at the moment the cap trips will still
    complete — the cap blocks the NEXT scheduling decision, not
    already-running calls.
    """

    def test_cost_cap_blocks_when_pre_call_total_exceeds(self) -> None:
        # `concurrency=1` makes per-task cap enforcement strict: task A
        # completes, spent_usd > cap, task B's pre-call check refuses.
        # Without this serialisation (default concurrency=8), the cap is
        # only approximately enforced under concurrent dispatch — up to
        # (concurrency - 1) in-flight tasks at the moment of cap-trip
        # will still complete. The strict-cap test exercises the
        # enforcement semantic; the "documented overshoot" test below
        # exercises the concurrent-overshoot bound.
        client = FakeLLMClient(text_provider=lambda s, u: "x")
        pipeline = EnrichmentPipeline(client=client, max_cost_usd=0.00001, default_concurrency=1)
        table = _table_with_columns("c1", "c2", "c3", "c4")
        with pytest.raises(CostCapExceeded):
            asyncio.run(
                pipeline.enrich_columns_async(
                    table=table,
                    columns=list(table.columns),
                    stats_by_col={},
                    fk_targets_by_col={},
                )
            )
        # The first call DID complete (cap trips post-call); subsequent
        # calls were refused. Pins the "one call may sneak through the
        # cap" semantic that the sync `enrich_column` also exhibits.
        assert len(client.calls) == 1

    def test_concurrent_overshoot_bounded_by_concurrency(self) -> None:
        # Under high concurrency, the cap is enforced approximately.
        # All in-flight tasks at the moment of cap-trip complete their
        # API calls before the next task's pre-call check refuses.
        # Documents the trade-off: concurrency x max_per_call_cost is
        # the worst-case overshoot above the cap. For Haiku at ~$0.0003
        # per call with concurrency=8, overshoot is bounded to ~$0.0024.
        client = FakeLLMClient(text_provider=lambda s, u: "x")
        pipeline = EnrichmentPipeline(client=client, max_cost_usd=0.00001, default_concurrency=8)
        # 16 columns with concurrency=8: first 8 acquire the semaphore
        # immediately and all see spent_usd=0 at their cap check (race);
        # they all proceed. The remaining 8 block at the semaphore. By
        # the time they acquire it, spent_usd >> cap, and their cap
        # check trips. The CostCapExceeded comes from the second batch.
        table = _table_with_columns(*[f"c{i}" for i in range(16)])
        with pytest.raises(CostCapExceeded):
            asyncio.run(
                pipeline.enrich_columns_async(
                    table=table,
                    columns=list(table.columns),
                    stats_by_col={},
                    fk_targets_by_col={},
                )
            )
        # Between 1 and 8 calls completed before the cap blocked the
        # rest. The exact number depends on scheduling but is bounded
        # by the concurrency limit.
        assert 1 <= len(client.calls) <= 8

    def test_spent_usd_reflects_all_completed_calls(self) -> None:
        client = FakeLLMClient(text_provider=lambda s, u: "x")
        pipeline = EnrichmentPipeline(client=client)
        table = _table_with_columns("c1", "c2", "c3", "c4")
        asyncio.run(
            pipeline.enrich_columns_async(
                table=table,
                columns=list(table.columns),
                stats_by_col={},
                fk_targets_by_col={},
            )
        )
        # All four calls should be reflected in spent_usd. Per-call
        # cost depends on token approximation but must be > 0 for all.
        assert pipeline.spent_usd > 0
        assert len(client.calls) == 4


class TestRuntimeGuardOnCostUsd:
    """`client.cost_usd(...)` MUST return a non-negative finite USD value.

    Without this guard, a buggy adapter could silently corrupt
    `_spent_usd` (e.g., return `-1.0` and let spend escape the cap, or
    return `nan` and break all subsequent comparisons).
    """

    def _custom_client_returning(self, value: float) -> Any:
        # Inline `LLMClient` whose cost_usd returns a chosen value.
        class _CustomClient:
            model = "custom"

            def __init__(self) -> None:
                # Per-instance list — avoids the RUF012 mutable-default
                # class-attribute footgun where two `_CustomClient`
                # instances would share the same `calls` list.
                self.calls: list[tuple[str, str]] = []

            def complete(self, *, system: str, user: str) -> LLMResponse:
                self.calls.append((system, user))
                return LLMResponse(
                    text="x",
                    model="custom",
                    usage=LLMUsage(input_tokens=1, cached_input_tokens=0, output_tokens=1),
                )

            def cost_usd(self, usage: LLMUsage) -> float:
                return value

        return _CustomClient()

    def test_negative_cost_raises_runtime_error_sync(self) -> None:
        # Sync path covered too — same guard, both paths.
        client = self._custom_client_returning(-1.0)
        pipeline = EnrichmentPipeline(client=client)
        table = _table_with_columns("c1")
        with pytest.raises(RuntimeError, match=r"non-finite|negative"):
            pipeline.enrich_column(table=table, column=table.columns[0], stats=None, fk_targets=())

    def test_negative_cost_raises_runtime_error_async(self) -> None:
        client = self._custom_client_returning(-0.01)
        pipeline = EnrichmentPipeline(client=client)
        table = _table_with_columns("c1")
        with pytest.raises(RuntimeError, match=r"non-finite|negative"):
            asyncio.run(
                pipeline.enrich_columns_async(
                    table=table,
                    columns=list(table.columns),
                    stats_by_col={},
                    fk_targets_by_col={},
                )
            )

    def test_nan_cost_raises_runtime_error(self) -> None:
        client = self._custom_client_returning(math.nan)
        pipeline = EnrichmentPipeline(client=client)
        table = _table_with_columns("c1")
        with pytest.raises(RuntimeError, match=r"non-finite|negative"):
            pipeline.enrich_column(table=table, column=table.columns[0], stats=None, fk_targets=())

    def test_infinity_cost_raises_runtime_error(self) -> None:
        client = self._custom_client_returning(math.inf)
        pipeline = EnrichmentPipeline(client=client)
        table = _table_with_columns("c1")
        with pytest.raises(RuntimeError, match=r"non-finite|negative"):
            pipeline.enrich_column(table=table, column=table.columns[0], stats=None, fk_targets=())

    def test_spent_usd_unchanged_after_guard_trips(self) -> None:
        # Belt-and-braces: guard must trip BEFORE `_spent_usd` is updated.
        # Otherwise an attacker (or buggy adapter) could push spent_usd
        # negative to escape the cap.
        client = self._custom_client_returning(-1.0)
        pipeline = EnrichmentPipeline(client=client)
        table = _table_with_columns("c1")
        with pytest.raises(RuntimeError):
            pipeline.enrich_column(table=table, column=table.columns[0], stats=None, fk_targets=())
        assert pipeline.spent_usd == 0.0


class TestStoreLedgerPersistsSpend:
    """When a `Store` + `source_connection_id` are wired into the
    pipeline at construction, cumulative spend persists to the ledger.

    This is the load-bearing property: a crash mid-enrichment doesn't
    let the next run start from $0 and re-incur the prior spend.
    """

    def test_async_writes_to_ledger(self, tmp_path: Path) -> None:
        client = FakeLLMClient(text_provider=lambda s, u: "x")
        with SQLiteStore(tmp_path / "sb.db", writer_lock=True) as store:
            pipeline = EnrichmentPipeline(
                client=client,
                store=store,
                source_connection_id="sid",
            )
            table = _table_with_columns("c1", "c2", "c3")
            asyncio.run(
                pipeline.enrich_columns_async(
                    table=table,
                    columns=list(table.columns),
                    stats_by_col={},
                    fk_targets_by_col={},
                )
            )
            # Ledger reflects the cumulative spend.
            persisted = store.get_spend_usd(source_connection_id="sid")
            assert persisted > 0
            assert math.isclose(persisted, pipeline.spent_usd, rel_tol=1e-6)

    def test_sync_path_also_writes_to_ledger(self, tmp_path: Path) -> None:
        # Both code paths must persist — otherwise tests using the sync
        # path bypass the durability story silently.
        client = FakeLLMClient(text_provider=lambda s, u: "x")
        with SQLiteStore(tmp_path / "sb.db", writer_lock=True) as store:
            pipeline = EnrichmentPipeline(
                client=client,
                store=store,
                source_connection_id="sid",
            )
            table = _table_with_columns("c1")
            pipeline.enrich_column(table=table, column=table.columns[0], stats=None, fk_targets=())
            assert store.get_spend_usd(source_connection_id="sid") > 0

    def test_pipeline_reads_initial_spend_from_ledger(self, tmp_path: Path) -> None:
        # Simulates a re-run after a previous run already spent some money.
        # The new pipeline should see the prior total and enforce the cap
        # against it — not start from $0.
        with SQLiteStore(tmp_path / "sb.db", writer_lock=True) as store:
            # Seed the ledger with a prior spend that's already over a
            # tight cap.
            store.add_spend_usd(source_connection_id="sid", amount_usd=0.10)
            client = FakeLLMClient(text_provider=lambda s, u: "x")
            pipeline = EnrichmentPipeline(
                client=client,
                store=store,
                source_connection_id="sid",
                max_cost_usd=0.05,  # already breached by the seeded spend
            )
            table = _table_with_columns("c1")
            with pytest.raises(CostCapExceeded):
                asyncio.run(
                    pipeline.enrich_columns_async(
                        table=table,
                        columns=list(table.columns),
                        stats_by_col={},
                        fk_targets_by_col={},
                    )
                )
            # No call was made (cap tripped pre-call).
            assert len(client.calls) == 0


class TestExceptionsPropagate:
    """`asyncio.gather(*, return_exceptions=False)` — first failure
    propagates immediately; pending tasks are cancelled.

    The pipeline does NOT swallow exceptions or fall back to partial
    results. Either every requested column gets a description or the
    caller sees an exception. This matches the existing sync-path
    contract (one failure tanks the batch) so the indexer's invariant
    "fingerprints written iff descriptions written" still holds.
    """

    def test_exception_in_one_call_propagates(self) -> None:
        # Match on `Column: failing_one` — the rendered prompt has the
        # column name on its own line, so this matches exactly one
        # task instead of leaking via the "Other columns" siblings
        # listing where any column's name appears in every other
        # column's prompt. With concurrency=1 we deterministically
        # surface a single RuntimeError (TaskGroup unwraps single-
        # exception groups back to the original exception).
        class _FailingClient:
            model = "claude-haiku-4-5"

            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            def complete(self, *, system: str, user: str) -> LLMResponse:
                self.calls.append((system, user))
                if "Column: failing_one" in user:
                    raise RuntimeError("simulated provider failure")
                return LLMResponse(
                    text="x",
                    model=self.model,
                    usage=LLMUsage(input_tokens=10, cached_input_tokens=0, output_tokens=5),
                )

            def cost_usd(self, usage: LLMUsage) -> float:
                from schemabrain.enrichment.llm import haiku_45_cost_usd

                return haiku_45_cost_usd(usage)

        client = _FailingClient()
        # concurrency=1: deterministic ordering, exactly one failure.
        pipeline = EnrichmentPipeline(client=client, default_concurrency=1)
        table = _table_with_columns("c1", "failing_one", "c3", "c4")
        with pytest.raises(RuntimeError, match=r"simulated"):
            asyncio.run(
                pipeline.enrich_columns_async(
                    table=table,
                    columns=list(table.columns),
                    stats_by_col={},
                    fk_targets_by_col={},
                )
            )


class TestProtocolReadOnlyModel:
    """Defence-in-depth: an inline `LLMClient` impl satisfies the
    Protocol AND works inside `enrich_columns_async`.

    Pins that the async path doesn't accidentally depend on any
    Anthropic-specific attribute beyond `complete` + `cost_usd`.
    """

    def test_custom_client_works_inside_async_batch(self) -> None:
        class _CustomClient:
            model = "future-provider-v1"

            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            def complete(self, *, system: str, user: str) -> LLMResponse:
                self.calls.append((system, user))
                return LLMResponse(
                    text="custom",
                    model=self.model,
                    usage=LLMUsage(input_tokens=10, cached_input_tokens=0, output_tokens=5),
                )

            def cost_usd(self, usage: LLMUsage) -> float:
                return 0.001  # flat-rate non-Anthropic pricing

        client = _CustomClient()
        pipeline = EnrichmentPipeline(client=client)
        table = _table_with_columns("c1", "c2")
        result = asyncio.run(
            pipeline.enrich_columns_async(
                table=table,
                columns=list(table.columns),
                stats_by_col={},
                fk_targets_by_col={},
            )
        )
        assert len(result) == 2
        for desc in result.values():
            assert desc.cost_usd == 0.001
        assert math.isclose(pipeline.spent_usd, 0.002, rel_tol=1e-9)
