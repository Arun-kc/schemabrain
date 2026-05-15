"""Tests for the SQLite-persisted cost ledger introduced in Phase A Commit 2.

The cost ledger is the durability layer for `EnrichmentPipeline`'s
`--max-cost` cap. Before this commit, cumulative spend lived in memory
on the pipeline instance — a process crash mid-enrichment lost the
counter, and a re-run started from $0 (the user could effectively
double-spend by re-running). The ledger persists the counter to SQLite
keyed on `source_connection_id`, so a re-run reads the prior total
back and refuses the next call if it has already breached the cap.

**Three responsibility lines covered here:**

1. **Read path (`get_spend_usd`)** — returns 0.0 when no row exists for
   the source; returns the persisted value otherwise.

2. **Atomic increment (`add_spend_usd`)** — adds an amount and returns
   the new total. The operation is atomic via SQLite's connection-level
   serialisation (single-process) or `BEGIN IMMEDIATE` (cross-process
   when `writer_lock=True`).

3. **Cross-process serialisation** — two `SQLiteStore` instances on the
   same file, both with `writer_lock=True`, concurrent
   `add_spend_usd` calls. The second BEGIN IMMEDIATE waits for the
   first to commit; both totals land correctly, no lost update.

Schema is bumped to `"3"` to add the `cost_ledger` table. Pre-alpha
contract: old stores raise `SchemaVersionMismatchError`; users
re-create. This matches the v2 → v3 contract precedent set when
`column_embeddings` landed in Slice 4-B.
"""

from __future__ import annotations

import math
import sqlite3
from pathlib import Path

import pytest

from schemabrain.core.store import SchemaVersionMismatchError, SQLiteStore


class TestGetSpendUsd:
    """Reads from the cost ledger.

    Zero-default semantics: a fresh source has no ledger row, but
    callers should not be required to special-case "first call." A
    `None` return would force every consumer to write `(get_spend(...) or 0.0)`
    forever; returning `0.0` keeps the arithmetic clean.
    """

    def test_returns_zero_when_no_row_exists(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "sb.db") as store:
            assert store.get_spend_usd(source_connection_id="sid-1") == 0.0

    def test_returns_persisted_value_after_add(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "sb.db") as store:
            store.add_spend_usd(source_connection_id="sid-1", amount_usd=0.0042)
            assert math.isclose(
                store.get_spend_usd(source_connection_id="sid-1"),
                0.0042,
                rel_tol=1e-9,
            )

    def test_separate_sources_have_separate_ledgers(self, tmp_path: Path) -> None:
        # Multi-source coexistence is the SQLiteStore contract — the
        # cost ledger must respect it too. A `--max-cost` on source A
        # must NOT see source B's spend.
        with SQLiteStore(tmp_path / "sb.db") as store:
            store.add_spend_usd(source_connection_id="sid-a", amount_usd=0.10)
            store.add_spend_usd(source_connection_id="sid-b", amount_usd=0.25)
            assert math.isclose(
                store.get_spend_usd(source_connection_id="sid-a"), 0.10, rel_tol=1e-9
            )
            assert math.isclose(
                store.get_spend_usd(source_connection_id="sid-b"), 0.25, rel_tol=1e-9
            )


class TestAddSpendUsd:
    """Atomic increment semantics on the cost ledger.

    `add_spend_usd` adds an amount to the running total and returns the
    new total. The operation is atomic; concurrent in-process callers
    on the same `SQLiteStore` cannot lose updates because Python's
    sqlite3 module serialises writes through the GIL on a single
    connection. Cross-process atomicity is provided by `writer_lock=True`
    which wraps the read-modify-write in `BEGIN IMMEDIATE`.
    """

    def test_first_add_returns_amount(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "sb.db") as store:
            total = store.add_spend_usd(source_connection_id="sid", amount_usd=0.0003)
            assert math.isclose(total, 0.0003, rel_tol=1e-9)

    def test_second_add_accumulates(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "sb.db") as store:
            store.add_spend_usd(source_connection_id="sid", amount_usd=0.0003)
            total = store.add_spend_usd(source_connection_id="sid", amount_usd=0.0007)
            assert math.isclose(total, 0.0010, rel_tol=1e-9)

    def test_zero_amount_is_a_no_op_in_value(self, tmp_path: Path) -> None:
        # `0.0` add must NOT change the total. This is the natural
        # arithmetic identity and the pipeline relies on it (e.g.,
        # cache hits with cached_input_tokens == input_tokens can
        # produce zero-cost responses).
        with SQLiteStore(tmp_path / "sb.db") as store:
            store.add_spend_usd(source_connection_id="sid", amount_usd=0.50)
            total = store.add_spend_usd(source_connection_id="sid", amount_usd=0.0)
            assert math.isclose(total, 0.50, rel_tol=1e-9)

    def test_negative_amount_raises(self, tmp_path: Path) -> None:
        # The pipeline's runtime guard already prevents negative costs
        # from reaching the ledger, but defence-in-depth: the ledger
        # itself rejects negatives so a future direct caller can't
        # corrupt the running total either.
        with (
            SQLiteStore(tmp_path / "sb.db") as store,
            pytest.raises(ValueError, match=r"negative|non-negative"),
        ):
            store.add_spend_usd(source_connection_id="sid", amount_usd=-0.01)

    def test_non_finite_amount_raises(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "sb.db") as store:
            with pytest.raises(ValueError, match="finite"):
                store.add_spend_usd(source_connection_id="sid", amount_usd=math.inf)
            with pytest.raises(ValueError, match="finite"):
                store.add_spend_usd(source_connection_id="sid", amount_usd=math.nan)

    def test_finite_input_overflowing_after_scaling_raises(self, tmp_path: Path) -> None:
        # `amount_usd` is finite, but `amount_usd * 1_000_000` overflows
        # to `inf` (this happens above ~1.8e302). `round(inf)` would
        # raise `OverflowError` — an undocumented exception type at
        # this layer. The guard converts to the same `ValueError`
        # shape the rest of the validators emit, so callers can catch
        # one type. Per security-reviewer audit 2026-05-15.
        with (
            SQLiteStore(tmp_path / "sb.db") as store,
            pytest.raises(ValueError, match=r"overflows to non-finite"),
        ):
            store.add_spend_usd(source_connection_id="sid", amount_usd=1e303)

    def test_micro_precision_preserved(self, tmp_path: Path) -> None:
        # Storage is integer micros (1 USD = 1_000_000 micros). 1 micro
        # USD = $0.000001 — much finer than any per-call LLM cost today
        # (Haiku cheapest call ~$0.0003 = 300 micros). Sub-micro precision
        # is documented as lost.
        with SQLiteStore(tmp_path / "sb.db") as store:
            total = store.add_spend_usd(source_connection_id="sid", amount_usd=0.000_001)
            assert math.isclose(total, 0.000_001, rel_tol=1e-9)

    def test_thousand_small_adds_accumulate_correctly(self, tmp_path: Path) -> None:
        # Float accumulation drift can lose pennies over many ops.
        # Integer-micros storage means 1000 x $0.001 = exactly $1.00.
        with SQLiteStore(tmp_path / "sb.db") as store:
            for _ in range(1000):
                store.add_spend_usd(source_connection_id="sid", amount_usd=0.001)
            assert math.isclose(
                store.get_spend_usd(source_connection_id="sid"), 1.000, rel_tol=1e-12
            )


class TestWriterLockSerializesLedgerWrites:
    """`writer_lock=True` makes ledger writes use `BEGIN IMMEDIATE`.

    Two `SQLiteStore` instances on the same DB file simulate two
    processes. Both with `writer_lock=True`. Concurrent `add_spend_usd`
    calls: the second `BEGIN IMMEDIATE` waits for the first to commit
    (busy_timeout). No update is lost — final total == sum of both
    deltas.

    Without `writer_lock=True`, the test would still pass on most
    platforms by accident (Python sqlite3 implicit transaction +
    busy_timeout). The point of the explicit BEGIN IMMEDIATE is that
    the SELECT-then-INSERT-OR-REPLACE sequence is atomic across the
    full read-modify-write, not just the write itself. Concurrent
    interleaving without BEGIN IMMEDIATE would produce a lost-update.
    """

    def test_two_processes_with_writer_lock_do_not_lose_updates(self, tmp_path: Path) -> None:
        # Simulate two processes via two separate SQLiteStore instances
        # on the same file. Initialise schema once.
        db_path = tmp_path / "sb.db"
        SQLiteStore(db_path).close()

        # Open both with writer_lock=True. Each adds to the ledger.
        store_a = SQLiteStore(db_path, writer_lock=True)
        store_b = SQLiteStore(db_path, writer_lock=True)
        try:
            # First add from A.
            store_a.add_spend_usd(source_connection_id="sid", amount_usd=0.10)
            # Second add from B reads A's commit, accumulates correctly.
            total = store_b.add_spend_usd(source_connection_id="sid", amount_usd=0.20)
            assert math.isclose(total, 0.30, rel_tol=1e-9)
            # A reads B's commit on next operation.
            assert math.isclose(
                store_a.get_spend_usd(source_connection_id="sid"), 0.30, rel_tol=1e-9
            )
        finally:
            store_a.close()
            store_b.close()

    def test_writer_lock_serialises_concurrent_ledger_writes(self, tmp_path: Path) -> None:
        # Two stores both holding writer_lock=True. Begin a long-running
        # IMMEDIATE on A, then attempt an add_spend_usd on B with a
        # tight busy_timeout. B should error with "locked" — proving
        # BEGIN IMMEDIATE is actually held during the read-modify-write.
        db_path = tmp_path / "sb.db"
        SQLiteStore(db_path).close()

        store_a = SQLiteStore(db_path, writer_lock=True)
        store_b = SQLiteStore(db_path, writer_lock=True)
        # Tighten busy_timeout on B for the test.
        store_b._require_conn().execute("PRAGMA busy_timeout = 50;")
        try:
            # Hold the write lock on A.
            store_a._require_conn().execute("BEGIN IMMEDIATE;")
            # B's add_spend_usd starts its own BEGIN IMMEDIATE, hits
            # A's lock, busy_timeout=50ms expires → OperationalError.
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                store_b.add_spend_usd(source_connection_id="sid", amount_usd=0.10)
        finally:
            store_a._require_conn().execute("ROLLBACK;")
            store_a.close()
            store_b.close()


class TestSchemaVersionBumpToV5:
    """The cost ledger lands in schema version `"3"`.

    Pre-alpha contract: a store written by an older Schema Brain version
    raises `SchemaVersionMismatchError` on open, with a message telling
    the user to delete the store and re-run `schemabrain index`. No
    migration framework yet (deferred per `project_deferred_decisions.md`).
    """

    def test_fresh_store_has_schema_version_5(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "sb.db") as store:
            stored = (
                store._require_conn()
                .execute("SELECT value FROM schemabrain_meta WHERE key = 'schema_version'")
                .fetchone()
            )
            assert stored["value"] == "5"

    def test_opening_a_v4_store_raises(self, tmp_path: Path) -> None:
        # Create a store, manually downgrade its version tag to "4",
        # close it, then re-open. The version check should refuse.
        db_path = tmp_path / "sb.db"
        store = SQLiteStore(db_path)
        store._require_conn().execute(
            "UPDATE schemabrain_meta SET value = '4' WHERE key = 'schema_version'"
        )
        store._require_conn().commit()
        store.close()
        with pytest.raises(SchemaVersionMismatchError, match=r"4.*5|5.*4"):
            SQLiteStore(db_path)


class TestLedgerSurvivesProcessRestart:
    """The ledger persists across `SQLiteStore` close/reopen cycles.

    This is the load-bearing property of the Commit 2 change. A
    process crash mid-enrichment must NOT let the next run start from
    $0 — the user has already been billed for the prior calls. After
    `close()`, a fresh `SQLiteStore` on the same file must see the
    same total.
    """

    def test_total_persists_across_close_and_reopen(self, tmp_path: Path) -> None:
        db_path = tmp_path / "sb.db"
        with SQLiteStore(db_path) as store:
            store.add_spend_usd(source_connection_id="sid", amount_usd=0.0050)
            store.add_spend_usd(source_connection_id="sid", amount_usd=0.0030)
        # Reopen, verify.
        with SQLiteStore(db_path) as store:
            assert math.isclose(
                store.get_spend_usd(source_connection_id="sid"),
                0.0080,
                rel_tol=1e-9,
            )

    def test_inmemory_store_loses_ledger_on_close(self) -> None:
        # Document the in-memory limitation explicitly. A `:memory:`
        # store has no file backing, so close() drops everything
        # including the ledger. Production paths use file-backed
        # stores; in-memory is for tests only.
        store = SQLiteStore(":memory:")
        store.add_spend_usd(source_connection_id="sid", amount_usd=0.10)
        assert math.isclose(store.get_spend_usd(source_connection_id="sid"), 0.10, rel_tol=1e-9)
        store.close()
        # A second :memory: store is a NEW database.
        store2 = SQLiteStore(":memory:")
        try:
            assert store2.get_spend_usd(source_connection_id="sid") == 0.0
        finally:
            store2.close()


class TestRollbackErrorSuppression:
    """If the inner write block raises AND `ROLLBACK` itself raises,
    the original exception still propagates.

    Defence-in-depth: a future SQLite edge case where the connection
    enters a state that rejects ROLLBACK must not swallow the
    underlying error. `contextlib.suppress(sqlite3.OperationalError)`
    is the right primitive — preserves the load-bearing exception
    while tolerating a fragile cleanup.
    """

    def test_inner_exception_propagates_through_rollback_suppression(self, tmp_path: Path) -> None:
        # Drop the `cost_ledger` table from the store underneath us.
        # `add_spend_usd` will BEGIN successfully, then SELECT raises
        # "no such table" — exercising the `except BaseException` +
        # ROLLBACK path. `sqlite3.Connection.execute` is read-only at
        # the C-API level so we can't monkeypatch it directly; mutating
        # the schema is the cleanest way to force an inner failure.
        store = SQLiteStore(tmp_path / "sb.db")
        try:
            # Detach the ledger from the schema so SELECT will fail.
            store._require_conn().execute("DROP TABLE cost_ledger")
            store._require_conn().commit()
            with pytest.raises(sqlite3.OperationalError, match=r"cost_ledger"):
                store.add_spend_usd(source_connection_id="sid", amount_usd=0.001)
        finally:
            store.close()


class TestPipelineConstructorValidation:
    """The `EnrichmentPipeline` constructor validates kwargs.

    The validators landed in Commit 2 alongside the async surface.
    Each rejects an impossible configuration before any LLM call
    happens — surfacing a programming error at the point of mistake,
    not on the first network round-trip.
    """

    def test_negative_default_concurrency_raises(self) -> None:
        from schemabrain.enrichment.llm import FakeLLMClient
        from schemabrain.enrichment.pipeline import EnrichmentPipeline

        with pytest.raises(ValueError, match=r"default_concurrency must be >= 1"):
            EnrichmentPipeline(
                client=FakeLLMClient(text_provider=lambda s, u: "x"),
                default_concurrency=0,
            )

    def test_negative_cryptic_concurrency_raises(self) -> None:
        from schemabrain.enrichment.llm import FakeLLMClient
        from schemabrain.enrichment.pipeline import EnrichmentPipeline

        with pytest.raises(ValueError, match=r"cryptic_concurrency must be >= 1"):
            EnrichmentPipeline(
                client=FakeLLMClient(text_provider=lambda s, u: "x"),
                cryptic_concurrency=0,
            )

    def test_store_without_source_connection_id_raises(self, tmp_path: Path) -> None:
        from schemabrain.enrichment.llm import FakeLLMClient
        from schemabrain.enrichment.pipeline import EnrichmentPipeline

        with (
            SQLiteStore(tmp_path / "sb.db") as store,
            pytest.raises(ValueError, match=r"both be supplied or both omitted"),
        ):
            EnrichmentPipeline(
                client=FakeLLMClient(text_provider=lambda s, u: "x"),
                store=store,
                source_connection_id=None,
            )

    def test_source_connection_id_without_store_raises(self) -> None:
        from schemabrain.enrichment.llm import FakeLLMClient
        from schemabrain.enrichment.pipeline import EnrichmentPipeline

        with pytest.raises(ValueError, match=r"both be supplied or both omitted"):
            EnrichmentPipeline(
                client=FakeLLMClient(text_provider=lambda s, u: "x"),
                source_connection_id="sid",
            )


class TestStoreProtocolExposesLedgerMethods:
    """The ledger methods are on the `Store` Protocol from this commit
    forward.

    Future store backends (in-memory mocks, alternative concretes) MUST
    implement them. This pins the public surface so a contributor can't
    silently land a new backend that's ledger-blind and let the cost
    cap silently degrade.
    """

    def test_get_spend_usd_on_protocol(self) -> None:
        from schemabrain.core.store_protocol import Store

        assert hasattr(Store, "get_spend_usd")

    def test_add_spend_usd_on_protocol(self) -> None:
        from schemabrain.core.store_protocol import Store

        assert hasattr(Store, "add_spend_usd")
