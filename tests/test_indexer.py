"""Unit tests for the indexer.

Uses fakes (FakeDataSource, CountingProfiler) instead of a real Postgres
so we can verify the central no-op-rerun invariant — that an unchanged
schema re-index does ZERO profile queries — by counting calls.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from schemabrain.connectors.base import DataSource
from schemabrain.core.models import Column, ForeignKey, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.indexer import IndexReporter, IndexResult, NullReporter, dry_run_index, index
from schemabrain.profiler.base import Profiler
from schemabrain.profiler.stats import ColumnStats

SOURCE_ID = "test_source"


def _column(
    name: str,
    *,
    table_name: str,
    schema_name: str = "public",
    data_type: str = "TEXT",
    nullable: bool = True,
    ordinal_position: int = 1,
    is_primary_key: bool = False,
) -> Column:
    return Column(
        name=name,
        table_name=table_name,
        schema_name=schema_name,
        data_type=data_type,
        nullable=nullable,
        ordinal_position=ordinal_position,
        is_primary_key=is_primary_key,
    )


def _users_table(*, columns: tuple[Column, ...] | None = None) -> Table:
    if columns is None:
        columns = (
            _column(
                "id",
                table_name="users",
                data_type="BIGINT",
                nullable=False,
                ordinal_position=1,
                is_primary_key=True,
            ),
            _column(
                "email",
                table_name="users",
                data_type="TEXT",
                nullable=False,
                ordinal_position=2,
            ),
        )
    return Table(name="users", schema_name="public", columns=columns)


def _orders_table() -> Table:
    return Table(
        name="orders",
        schema_name="public",
        columns=(
            _column(
                "id",
                table_name="orders",
                data_type="BIGINT",
                nullable=False,
                ordinal_position=1,
                is_primary_key=True,
            ),
            _column(
                "user_id",
                table_name="orders",
                data_type="BIGINT",
                nullable=False,
                ordinal_position=2,
            ),
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


def _stats_for(table: Table) -> dict[str, ColumnStats]:
    return {
        col.name: ColumnStats(
            column_name=col.name,
            total_rows=10,
            null_count=0,
            distinct_count=10,
            sample_values=("a", "b"),
        )
        for col in table.columns
    }


class FakeDataSource:
    def __init__(self, tables: list[Table]) -> None:
        self._tables = {(t.schema_name, t.name): t for t in tables}
        self.list_tables_calls: int = 0
        self.get_table_calls: list[tuple[str, str]] = []

    def list_tables(self, schema: str | None = None) -> list[tuple[str, str]]:
        self.list_tables_calls += 1
        keys = list(self._tables.keys())
        if schema is None:
            return keys
        return [k for k in keys if k[0] == schema]

    def get_table(self, name: str, schema: str) -> Table:
        self.get_table_calls.append((schema, name))
        return self._tables[(schema, name)]

    def close(self) -> None:
        pass

    def set_tables(self, tables: list[Table]) -> None:
        self._tables = {(t.schema_name, t.name): t for t in tables}


class CountingProfiler:
    def __init__(
        self, stats_provider: Callable[[Table], dict[str, ColumnStats]] = _stats_for
    ) -> None:
        self.profile_calls: list[Table] = []
        self._stats_provider = stats_provider

    def profile_table(self, table: Table) -> dict[str, ColumnStats]:
        self.profile_calls.append(table)
        return self._stats_provider(table)


class TestFreshRun:
    def test_first_run_profiles_every_table(self) -> None:
        source = FakeDataSource([_users_table(), _orders_table()])
        profiler = CountingProfiler()
        store = SQLiteStore(":memory:")

        result = index(
            source=source, profiler=profiler, store=store, source_connection_id=SOURCE_ID
        )

        assert len(profiler.profile_calls) == 2
        assert result.tables_seen == 2
        assert result.tables_changed == 2
        assert result.tables_unchanged == 0
        assert result.tables_removed == 0
        # Two new tables → all their columns counted as added.
        assert result.columns_added == 4
        assert result.columns_changed == 0
        assert result.columns_removed == 0
        store.close()

    def test_first_run_writes_table_rows_and_fingerprints(self) -> None:
        source = FakeDataSource([_users_table()])
        profiler = CountingProfiler()
        store = SQLiteStore(":memory:")

        index(source=source, profiler=profiler, store=store, source_connection_id=SOURCE_ID)

        assert store.list_tables(source_connection_id=SOURCE_ID) == [("public", "users")]
        fps = store.get_table_fingerprints("public", "users", source_connection_id=SOURCE_ID)
        assert set(fps.keys()) == {"id", "email"}
        # Each entry must be (structural, semantic) — both 64-char hex.
        for structural, semantic in fps.values():
            assert len(structural) == 64
            assert len(semantic) == 64
        store.close()


class TestNoOpRerun:
    def test_unchanged_schema_does_not_call_profiler(self) -> None:
        # THE central no-op-rerun invariant.
        source = FakeDataSource([_users_table(), _orders_table()])
        profiler = CountingProfiler()
        store = SQLiteStore(":memory:")

        index(source=source, profiler=profiler, store=store, source_connection_id=SOURCE_ID)
        calls_after_first = len(profiler.profile_calls)

        result = index(
            source=source, profiler=profiler, store=store, source_connection_id=SOURCE_ID
        )

        assert len(profiler.profile_calls) == calls_after_first, (
            "Re-indexing an unchanged schema must not call the profiler again"
        )
        assert result.tables_unchanged == 2
        assert result.tables_changed == 0
        assert result.columns_added == 0
        assert result.columns_changed == 0
        assert result.columns_removed == 0
        store.close()

    def test_zero_column_table_is_never_profiled(self) -> None:
        # A table with no columns produces empty new+cached fingerprint
        # sets, which fall through the diff as "no change" — even on the
        # very first run. The profiler is never called, the table is
        # classified `unchanged` on every run, and the table row is
        # never persisted (since the changed-path is what writes it).
        # This is acceptable because a zero-column table contains no
        # signal worth caching; if a future change ever needs to
        # surface them in MCP responses we'll revisit. Locking the
        # current behavior in so a regression that starts profiling
        # them gets caught.
        empty_users = Table(name="users", schema_name="public", columns=())
        source = FakeDataSource([empty_users])
        profiler = CountingProfiler()
        store = SQLiteStore(":memory:")

        first = index(source=source, profiler=profiler, store=store, source_connection_id=SOURCE_ID)
        # First-run classification: seen, never-changed, never-profiled,
        # never-written.
        assert first.tables_seen == 1
        assert first.tables_changed == 0
        assert first.tables_unchanged == 1
        assert profiler.profile_calls == []
        assert store.list_tables(source_connection_id=SOURCE_ID) == []

        second = index(
            source=source, profiler=profiler, store=store, source_connection_id=SOURCE_ID
        )
        # Second run: identical classification.
        assert second == first
        assert profiler.profile_calls == []
        store.close()

    def test_unchanged_schema_returns_zero_changed(self) -> None:
        source = FakeDataSource([_users_table()])
        profiler = CountingProfiler()
        store = SQLiteStore(":memory:")

        index(source=source, profiler=profiler, store=store, source_connection_id=SOURCE_ID)
        result = index(
            source=source, profiler=profiler, store=store, source_connection_id=SOURCE_ID
        )

        assert result == IndexResult(
            tables_seen=1,
            tables_changed=0,
            tables_unchanged=1,
            tables_removed=0,
            columns_added=0,
            columns_changed=0,
            columns_removed=0,
        )
        store.close()


class TestStructuralChange:
    def test_added_table_profiled_only(self) -> None:
        source = FakeDataSource([_users_table()])
        profiler = CountingProfiler()
        store = SQLiteStore(":memory:")
        index(source=source, profiler=profiler, store=store, source_connection_id=SOURCE_ID)
        first_call_count = len(profiler.profile_calls)

        # Add a second table; users is unchanged.
        source.set_tables([_users_table(), _orders_table()])
        result = index(
            source=source, profiler=profiler, store=store, source_connection_id=SOURCE_ID
        )

        # Only the new orders table got profiled.
        new_calls = profiler.profile_calls[first_call_count:]
        assert len(new_calls) == 1
        assert new_calls[0].name == "orders"
        assert result.tables_unchanged == 1
        assert result.tables_changed == 1
        assert result.columns_added == 2  # orders has 2 columns
        store.close()

    def test_removed_table_deleted_from_store(self) -> None:
        source = FakeDataSource([_users_table(), _orders_table()])
        profiler = CountingProfiler()
        store = SQLiteStore(":memory:")
        index(source=source, profiler=profiler, store=store, source_connection_id=SOURCE_ID)

        # Remove orders.
        source.set_tables([_users_table()])
        result = index(
            source=source, profiler=profiler, store=store, source_connection_id=SOURCE_ID
        )

        assert ("public", "orders") not in store.list_tables(source_connection_id=SOURCE_ID)
        assert result.tables_removed == 1
        # The remaining users table is unchanged.
        assert result.tables_unchanged == 1
        store.close()

    def test_column_type_change_triggers_table_reprofile(self) -> None:
        source = FakeDataSource([_users_table()])
        profiler = CountingProfiler()
        store = SQLiteStore(":memory:")
        index(source=source, profiler=profiler, store=store, source_connection_id=SOURCE_ID)
        first_call_count = len(profiler.profile_calls)

        # Mutate users.email type.
        mutated_columns = (
            _column(
                "id",
                table_name="users",
                data_type="BIGINT",
                nullable=False,
                ordinal_position=1,
                is_primary_key=True,
            ),
            _column(
                "email",
                table_name="users",
                data_type="VARCHAR(255)",  # changed from TEXT
                nullable=False,
                ordinal_position=2,
            ),
        )
        source.set_tables([_users_table(columns=mutated_columns)])
        result = index(
            source=source, profiler=profiler, store=store, source_connection_id=SOURCE_ID
        )

        assert len(profiler.profile_calls) == first_call_count + 1, (
            "type change must trigger a re-profile of the affected table"
        )
        assert result.tables_changed == 1
        assert result.columns_changed == 1
        assert result.columns_added == 0
        store.close()

    def test_added_column_counted_as_added(self) -> None:
        source = FakeDataSource([_users_table()])
        profiler = CountingProfiler()
        store = SQLiteStore(":memory:")
        index(source=source, profiler=profiler, store=store, source_connection_id=SOURCE_ID)

        extended_columns = (
            _column(
                "id",
                table_name="users",
                data_type="BIGINT",
                nullable=False,
                ordinal_position=1,
                is_primary_key=True,
            ),
            _column(
                "email",
                table_name="users",
                data_type="TEXT",
                nullable=False,
                ordinal_position=2,
            ),
            _column(
                "signup_date",
                table_name="users",
                data_type="DATE",
                ordinal_position=3,
            ),
        )
        source.set_tables([_users_table(columns=extended_columns)])
        result = index(
            source=source, profiler=profiler, store=store, source_connection_id=SOURCE_ID
        )

        assert result.columns_added == 1
        assert result.columns_changed == 0
        store.close()

    def test_removed_column_counted_as_removed(self) -> None:
        source = FakeDataSource([_users_table()])
        profiler = CountingProfiler()
        store = SQLiteStore(":memory:")
        index(source=source, profiler=profiler, store=store, source_connection_id=SOURCE_ID)

        slimmed_columns = (
            _column(
                "id",
                table_name="users",
                data_type="BIGINT",
                nullable=False,
                ordinal_position=1,
                is_primary_key=True,
            ),
        )
        source.set_tables([_users_table(columns=slimmed_columns)])
        result = index(
            source=source, profiler=profiler, store=store, source_connection_id=SOURCE_ID
        )

        assert result.columns_removed == 1
        assert result.columns_added == 0
        store.close()


class TestFakesSatisfyProtocols:
    """Make sure the in-memory fakes still implement the Protocols the
    indexer programs against. Otherwise this test file could pass while
    the real CLI flow breaks because of a Protocol divergence.

    Note: `runtime_checkable` only verifies method *names*, not their
    signatures or types. A fake whose `profile_table` was renamed (typo
    or refactor) gets caught here; a fake whose `profile_table` dropped
    its `table` parameter does not. This is a known limitation of
    Python's runtime-checkable Protocol — it's still worth more than no
    check at all.
    """

    def test_fake_data_source_satisfies_protocol(self) -> None:
        assert isinstance(FakeDataSource([]), DataSource)

    def test_counting_profiler_satisfies_protocol(self) -> None:
        assert isinstance(CountingProfiler(), Profiler)


class TestEmpty:
    def test_empty_source(self) -> None:
        source = FakeDataSource([])
        profiler = CountingProfiler()
        store = SQLiteStore(":memory:")
        result = index(
            source=source, profiler=profiler, store=store, source_connection_id=SOURCE_ID
        )
        assert result == IndexResult(
            tables_seen=0,
            tables_changed=0,
            tables_unchanged=0,
            tables_removed=0,
            columns_added=0,
            columns_changed=0,
            columns_removed=0,
        )
        assert profiler.profile_calls == []
        store.close()


class TestEnrichmentIntegration:
    def _pipeline(self, *, max_cost_usd: float = 10.0):
        from schemabrain.enrichment.llm import FakeLLMClient
        from schemabrain.enrichment.pipeline import EnrichmentPipeline

        client = FakeLLMClient(text_provider=lambda system, user: "fake description")
        return EnrichmentPipeline(client=client, max_cost_usd=max_cost_usd), client

    def test_no_pipeline_means_no_descriptions_persisted(self) -> None:
        source = FakeDataSource([_users_table()])
        profiler = CountingProfiler()
        store = SQLiteStore(":memory:")

        result = index(
            source=source, profiler=profiler, store=store, source_connection_id=SOURCE_ID
        )
        descs = store.get_table_descriptions("public", "users", source_connection_id=SOURCE_ID)
        assert descs == {}
        assert result.descriptions_generated == 0
        assert result.llm_cost_usd == 0.0
        store.close()

    def test_with_pipeline_writes_descriptions_for_every_column(self) -> None:
        source = FakeDataSource([_users_table()])
        profiler = CountingProfiler()
        store = SQLiteStore(":memory:")
        pipeline, client = self._pipeline()

        result = index(
            source=source,
            profiler=profiler,
            store=store,
            source_connection_id=SOURCE_ID,
            pipeline=pipeline,
        )

        descs = store.get_table_descriptions("public", "users", source_connection_id=SOURCE_ID)
        assert set(descs.keys()) == {"id", "email"}
        assert all(d.text == "fake description" for d in descs.values())
        assert result.descriptions_generated == 2
        assert result.llm_cost_usd > 0.0
        # One LLM call per column.
        assert len(client.calls) == 2
        store.close()

    def test_unchanged_table_does_not_call_llm(self) -> None:
        # The no-op-rerun invariant extends to LLM cost: a
        # second index on an unchanged schema must not make any new
        # LLM calls.
        source = FakeDataSource([_users_table()])
        profiler = CountingProfiler()
        store = SQLiteStore(":memory:")
        pipeline, client = self._pipeline()

        # First run — fills cache + descriptions.
        index(
            source=source,
            profiler=profiler,
            store=store,
            source_connection_id=SOURCE_ID,
            pipeline=pipeline,
        )
        calls_after_first = len(client.calls)

        # Second run — must be a no-op.
        result = index(
            source=source,
            profiler=profiler,
            store=store,
            source_connection_id=SOURCE_ID,
            pipeline=pipeline,
        )

        assert len(client.calls) == calls_after_first, "no-op rerun must not call the LLM"
        assert result.descriptions_generated == 0

    def test_changed_table_re_enriches_only_that_table(self) -> None:
        source = FakeDataSource([_users_table(), _orders_table()])
        profiler = CountingProfiler()
        store = SQLiteStore(":memory:")
        pipeline, client = self._pipeline()
        index(
            source=source,
            profiler=profiler,
            store=store,
            source_connection_id=SOURCE_ID,
            pipeline=pipeline,
        )
        first_call_count = len(client.calls)

        # Mutate users.email type.
        mutated_columns = (
            _column(
                "id",
                table_name="users",
                data_type="BIGINT",
                nullable=False,
                ordinal_position=1,
                is_primary_key=True,
            ),
            _column(
                "email",
                table_name="users",
                data_type="VARCHAR(255)",
                nullable=False,
                ordinal_position=2,
            ),
        )
        source.set_tables([_users_table(columns=mutated_columns), _orders_table()])
        result = index(
            source=source,
            profiler=profiler,
            store=store,
            source_connection_id=SOURCE_ID,
            pipeline=pipeline,
        )

        # Only `users` was re-enriched (2 columns), not `orders`.
        assert len(client.calls) == first_call_count + 2
        assert result.descriptions_generated == 2
        assert result.tables_unchanged == 1
        assert result.tables_changed == 1

    def test_cost_cap_during_enrichment_leaves_cache_unchanged_for_that_table(
        self,
    ) -> None:
        # Regression for the order-of-operations bug: if CostCapExceeded
        # fires mid-enrichment, NOTHING for that table should be in the
        # cache, so the next run sees the table as still-changed and
        # retries. Otherwise we'd stamp fingerprints without ever writing
        # descriptions and the table would be permanently un-described
        # until a real schema change occurred.
        from schemabrain.enrichment.llm import FakeLLMClient
        from schemabrain.enrichment.pipeline import (
            CostCapExceeded,
            EnrichmentPipeline,
        )

        source = FakeDataSource([_users_table()])  # 2 columns
        profiler = CountingProfiler()
        store = SQLiteStore(":memory:")
        # Cap that allows the first call but trips on the second.
        # Each huge response costs ~$5; cap of $2 lets call 1 through
        # and triggers the cap on entry to call 2. `default_concurrency=1`
        # makes the per-task cap check strict (cap trips deterministically
        # after the first call completes; the second is refused). Under
        # default concurrency=8 the pre-call check races, all in-flight
        # tasks complete, and the cap is approximately enforced — fine
        # for production but not for this regression test which pins
        # exact ordering.
        client = FakeLLMClient(text_provider=lambda s, u: "x" * 5_000_000)
        pipeline = EnrichmentPipeline(client=client, max_cost_usd=2.0, default_concurrency=1)

        with pytest.raises(CostCapExceeded):
            index(
                source=source,
                profiler=profiler,
                store=store,
                source_connection_id=SOURCE_ID,
                pipeline=pipeline,
            )

        # After the failed run, NOTHING for users should be cached:
        # not the table row, not fingerprints, not descriptions.
        assert store.list_tables(source_connection_id=SOURCE_ID) == []
        assert store.get_table_fingerprints("public", "users", source_connection_id=SOURCE_ID) == {}
        store.close()

    def test_cost_cap_exception_propagates(self) -> None:
        # Pipeline configured with a tiny cap that the first call breaks.
        from schemabrain.enrichment.pipeline import CostCapExceeded

        source = FakeDataSource([_users_table()])
        profiler = CountingProfiler()
        store = SQLiteStore(":memory:")
        # Build a pipeline whose response is huge to trip the cap fast.
        from schemabrain.enrichment.llm import FakeLLMClient
        from schemabrain.enrichment.pipeline import EnrichmentPipeline

        client = FakeLLMClient(text_provider=lambda s, u: "x" * 5_000_000)
        # `default_concurrency=1` — see cap-test rationale above.
        pipeline = EnrichmentPipeline(client=client, max_cost_usd=0.5, default_concurrency=1)

        with pytest.raises(CostCapExceeded):
            index(
                source=source,
                profiler=profiler,
                store=store,
                source_connection_id=SOURCE_ID,
                pipeline=pipeline,
            )
        store.close()

    def test_descriptions_cleared_on_table_drop(self) -> None:
        source = FakeDataSource([_users_table(), _orders_table()])
        profiler = CountingProfiler()
        store = SQLiteStore(":memory:")
        pipeline, _ = self._pipeline()
        index(
            source=source,
            profiler=profiler,
            store=store,
            source_connection_id=SOURCE_ID,
            pipeline=pipeline,
        )
        # Drop orders.
        source.set_tables([_users_table()])
        index(
            source=source,
            profiler=profiler,
            store=store,
            source_connection_id=SOURCE_ID,
            pipeline=pipeline,
        )
        orders_descs = store.get_table_descriptions(
            "public", "orders", source_connection_id=SOURCE_ID
        )
        assert orders_descs == {}
        store.close()


class TestEmbeddingIntegration:
    def _pipeline(self):
        from schemabrain.enrichment.llm import FakeLLMClient
        from schemabrain.enrichment.pipeline import EnrichmentPipeline

        client = FakeLLMClient(text_provider=lambda system, user: "fake description")
        return EnrichmentPipeline(client=client), client

    def _embedder(self, dimension: int = 8):
        from schemabrain.enrichment.embeddings import FakeEmbedder

        return FakeEmbedder(dimension=dimension)

    def test_no_embedder_means_no_embeddings_persisted(self) -> None:
        source = FakeDataSource([_users_table()])
        profiler = CountingProfiler()
        store = SQLiteStore(":memory:")
        pipeline, _ = self._pipeline()

        result = index(
            source=source,
            profiler=profiler,
            store=store,
            source_connection_id=SOURCE_ID,
            pipeline=pipeline,
        )
        embs = store.get_table_embeddings("public", "users", source_connection_id=SOURCE_ID)
        assert embs == {}
        assert result.embeddings_generated == 0
        store.close()

    def test_embedder_without_pipeline_is_no_op(self) -> None:
        # Embedding a column requires a description; if no pipeline ran,
        # there's nothing to embed. Pipeline-less indexing must not call
        # the embedder.
        source = FakeDataSource([_users_table()])
        profiler = CountingProfiler()
        store = SQLiteStore(":memory:")
        embedder = self._embedder()

        result = index(
            source=source,
            profiler=profiler,
            store=store,
            source_connection_id=SOURCE_ID,
            embedder=embedder,
        )

        assert embedder.calls == []
        assert result.embeddings_generated == 0
        embs = store.get_table_embeddings("public", "users", source_connection_id=SOURCE_ID)
        assert embs == {}
        store.close()

    def test_with_embedder_persists_one_embedding_per_described_column(self) -> None:
        source = FakeDataSource([_users_table()])
        profiler = CountingProfiler()
        store = SQLiteStore(":memory:")
        pipeline, _ = self._pipeline()
        embedder = self._embedder(dimension=8)

        result = index(
            source=source,
            profiler=profiler,
            store=store,
            source_connection_id=SOURCE_ID,
            pipeline=pipeline,
            embedder=embedder,
        )

        embs = store.get_table_embeddings("public", "users", source_connection_id=SOURCE_ID)
        assert set(embs.keys()) == {"id", "email"}
        assert all(e.dimension == 8 for e in embs.values())
        assert all(e.model == "fake-embedder" for e in embs.values())
        # 2 columns => 2 embedder calls and 2 stored embeddings.
        assert len(embedder.calls) == 2
        assert result.embeddings_generated == 2
        store.close()

    def test_unchanged_table_does_not_call_embedder(self) -> None:
        source = FakeDataSource([_users_table()])
        profiler = CountingProfiler()
        store = SQLiteStore(":memory:")
        pipeline, _ = self._pipeline()
        embedder = self._embedder()

        index(
            source=source,
            profiler=profiler,
            store=store,
            source_connection_id=SOURCE_ID,
            pipeline=pipeline,
            embedder=embedder,
        )
        calls_after_first = len(embedder.calls)

        result = index(
            source=source,
            profiler=profiler,
            store=store,
            source_connection_id=SOURCE_ID,
            pipeline=pipeline,
            embedder=embedder,
        )

        assert len(embedder.calls) == calls_after_first
        assert result.embeddings_generated == 0

    def test_changed_table_re_embeds_only_that_table(self) -> None:
        source = FakeDataSource([_users_table(), _orders_table()])
        profiler = CountingProfiler()
        store = SQLiteStore(":memory:")
        pipeline, _ = self._pipeline()
        embedder = self._embedder()
        index(
            source=source,
            profiler=profiler,
            store=store,
            source_connection_id=SOURCE_ID,
            pipeline=pipeline,
            embedder=embedder,
        )
        calls_after_first = len(embedder.calls)

        # Mutate users.email type — only users should re-embed.
        mutated_columns = (
            _column(
                "id",
                table_name="users",
                data_type="BIGINT",
                nullable=False,
                ordinal_position=1,
                is_primary_key=True,
            ),
            _column(
                "email",
                table_name="users",
                data_type="VARCHAR(255)",
                nullable=False,
                ordinal_position=2,
            ),
        )
        source.set_tables([_users_table(columns=mutated_columns), _orders_table()])
        result = index(
            source=source,
            profiler=profiler,
            store=store,
            source_connection_id=SOURCE_ID,
            pipeline=pipeline,
            embedder=embedder,
        )

        assert len(embedder.calls) == calls_after_first + 2
        assert result.embeddings_generated == 2

    def test_embeddings_cleared_on_table_drop(self) -> None:
        source = FakeDataSource([_users_table(), _orders_table()])
        profiler = CountingProfiler()
        store = SQLiteStore(":memory:")
        pipeline, _ = self._pipeline()
        embedder = self._embedder()
        index(
            source=source,
            profiler=profiler,
            store=store,
            source_connection_id=SOURCE_ID,
            pipeline=pipeline,
            embedder=embedder,
        )
        source.set_tables([_users_table()])
        index(
            source=source,
            profiler=profiler,
            store=store,
            source_connection_id=SOURCE_ID,
            pipeline=pipeline,
            embedder=embedder,
        )
        orders_embs = store.get_table_embeddings("public", "orders", source_connection_id=SOURCE_ID)
        assert orders_embs == {}
        store.close()

    def test_cost_cap_during_enrichment_leaves_embeddings_untouched_for_table(self) -> None:
        # Mirrors the description cost-cap test: if enrichment fails
        # mid-table, NO embeddings for that table should be persisted.
        from schemabrain.enrichment.llm import FakeLLMClient
        from schemabrain.enrichment.pipeline import (
            CostCapExceeded,
            EnrichmentPipeline,
        )

        source = FakeDataSource([_users_table()])
        profiler = CountingProfiler()
        store = SQLiteStore(":memory:")
        client = FakeLLMClient(text_provider=lambda s, u: "x" * 5_000_000)
        # `default_concurrency=1` — see cap-test rationale in the
        # description-side cost-cap test.
        pipeline = EnrichmentPipeline(client=client, max_cost_usd=2.0, default_concurrency=1)
        embedder = self._embedder()

        with pytest.raises(CostCapExceeded):
            index(
                source=source,
                profiler=profiler,
                store=store,
                source_connection_id=SOURCE_ID,
                pipeline=pipeline,
                embedder=embedder,
            )

        assert store.get_table_embeddings("public", "users", source_connection_id=SOURCE_ID) == {}
        # Embedder is only called AFTER all descriptions for a table
        # complete — cost cap fires inside enrichment, so no embeddings
        # were attempted at all.
        assert embedder.calls == []
        store.close()


class TestIndexResultSummary:
    def test_summary_shows_all_counts(self) -> None:
        result = IndexResult(
            tables_seen=5,
            tables_changed=2,
            tables_unchanged=2,
            tables_removed=1,
            columns_added=3,
            columns_changed=1,
            columns_removed=0,
        )
        summary = result.summary()
        assert "5" in summary
        assert "2 changed" in summary
        assert "2 unchanged" in summary
        assert "1 removed" in summary
        assert "+3" in summary
        assert "~1" in summary
        assert "-0" in summary
        # No LLM section when no enrichment happened.
        assert "LLM:" not in summary
        assert "Embeddings:" not in summary

    def test_summary_includes_llm_section_when_descriptions_generated(self) -> None:
        result = IndexResult(
            tables_seen=1,
            tables_changed=1,
            tables_unchanged=0,
            tables_removed=0,
            columns_added=3,
            columns_changed=0,
            columns_removed=0,
            descriptions_generated=3,
            llm_cost_usd=0.0042,
        )
        summary = result.summary()
        assert "LLM:" in summary
        assert "3 descriptions" in summary
        assert "$0.0042" in summary

    def test_summary_includes_embeddings_section_when_generated(self) -> None:
        result = IndexResult(
            tables_seen=1,
            tables_changed=1,
            tables_unchanged=0,
            tables_removed=0,
            columns_added=2,
            columns_changed=0,
            columns_removed=0,
            descriptions_generated=2,
            llm_cost_usd=0.001,
            embeddings_generated=2,
        )
        summary = result.summary()
        assert "Embeddings: 2" in summary

    def test_summary_dry_run_flips_verb_and_labels(self) -> None:
        # Same counts, different framing — the dry_run flag MUST NOT
        # produce a string a reader could confuse with a real run.
        result = IndexResult(
            tables_seen=5,
            tables_changed=2,
            tables_unchanged=2,
            tables_removed=1,
            columns_added=3,
            columns_changed=1,
            columns_removed=0,
            descriptions_generated=4,
            llm_cost_usd=0.0012,
            embeddings_generated=4,
        )
        summary = result.summary(dry_run=True)
        assert summary.startswith("Would index 5 table(s)")
        assert "Estimated LLM" in summary
        assert "Estimated embeddings" in summary
        assert "No changes made to the store." in summary
        # The verbatim "Indexed N table(s)" string MUST NOT appear —
        # otherwise CI log grepping for real runs would pick this up.
        assert "Indexed 5 table(s)" not in summary

    def test_summary_default_is_unchanged_real_run(self) -> None:
        # Sibling of the dry-run test — confirm `dry_run=False` (the
        # default) keeps the existing format byte-for-byte.
        result = IndexResult(
            tables_seen=1,
            tables_changed=1,
            tables_unchanged=0,
            tables_removed=0,
            columns_added=2,
            columns_changed=0,
            columns_removed=0,
            descriptions_generated=2,
            llm_cost_usd=0.001,
            embeddings_generated=2,
        )
        summary = result.summary()
        assert summary.startswith("Indexed 1 table(s)")
        assert "Would index" not in summary
        assert "Estimated" not in summary
        assert "No changes made" not in summary


class RecordingReporter:
    """Captures every reporter event for ordering + payload assertions."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def on_start(self, *, total_tables: int) -> None:
        self.events.append(("start", {"total_tables": total_tables}))

    def on_tables_removed(self, *, removed: int) -> None:
        self.events.append(("tables_removed", {"removed": removed}))

    def on_table_unchanged(self, *, schema: str, name: str) -> None:
        self.events.append(("table_unchanged", {"schema": schema, "name": name}))

    def on_table_start(self, *, schema: str, name: str, columns_to_enrich: int) -> None:
        self.events.append(
            (
                "table_start",
                {
                    "schema": schema,
                    "name": name,
                    "columns_to_enrich": columns_to_enrich,
                },
            )
        )

    def on_column_enriched(self, *, schema: str, name: str, column: str, spent_usd: float) -> None:
        self.events.append(
            (
                "column_enriched",
                {
                    "schema": schema,
                    "name": name,
                    "column": column,
                    "spent_usd": spent_usd,
                },
            )
        )

    def on_table_done(self, *, schema: str, name: str) -> None:
        self.events.append(("table_done", {"schema": schema, "name": name}))

    def on_finish(self, *, result: IndexResult) -> None:
        self.events.append(("finish", {"result": result}))

    def close(self) -> None:
        self.events.append(("close", {}))


class TestReporter:
    """Reporter callbacks fire in the documented order with correct payloads."""

    def _pipeline(self):
        from schemabrain.enrichment.llm import FakeLLMClient
        from schemabrain.enrichment.pipeline import EnrichmentPipeline

        client = FakeLLMClient(text_provider=lambda system, user: "fake description")
        return EnrichmentPipeline(client=client, max_cost_usd=10.0)

    def test_default_reporter_is_no_op_and_does_not_crash(self) -> None:
        source = FakeDataSource([_users_table()])
        profiler = CountingProfiler()
        store = SQLiteStore(":memory:")
        # `reporter=None` (omitted) must be safe — the default path.
        index(
            source=source,
            profiler=profiler,
            store=store,
            source_connection_id=SOURCE_ID,
        )
        store.close()

    def test_null_reporter_satisfies_protocol_and_is_no_op(self) -> None:
        source = FakeDataSource([_users_table()])
        profiler = CountingProfiler()
        store = SQLiteStore(":memory:")
        reporter: IndexReporter = NullReporter()
        result = index(
            source=source,
            profiler=profiler,
            store=store,
            source_connection_id=SOURCE_ID,
            reporter=reporter,
        )
        # If the call returned, NullReporter doesn't break the contract.
        assert result.tables_seen == 1
        store.close()

    def test_fresh_run_emits_start_then_table_start_then_done_then_finish(
        self,
    ) -> None:
        source = FakeDataSource([_users_table()])
        profiler = CountingProfiler()
        store = SQLiteStore(":memory:")
        reporter = RecordingReporter()

        index(
            source=source,
            profiler=profiler,
            store=store,
            source_connection_id=SOURCE_ID,
            reporter=reporter,
        )

        kinds = [kind for kind, _ in reporter.events]
        assert kinds == [
            "start",
            "tables_removed",
            "table_start",
            "table_done",
            "finish",
        ]
        assert reporter.events[0][1]["total_tables"] == 1
        assert reporter.events[1][1]["removed"] == 0
        assert reporter.events[2][1] == {
            "schema": "public",
            "name": "users",
            "columns_to_enrich": 0,  # no pipeline
        }
        store.close()

    def test_unchanged_table_emits_table_unchanged_event(self) -> None:
        source = FakeDataSource([_users_table()])
        profiler = CountingProfiler()
        store = SQLiteStore(":memory:")

        index(source=source, profiler=profiler, store=store, source_connection_id=SOURCE_ID)

        reporter = RecordingReporter()
        index(
            source=source,
            profiler=profiler,
            store=store,
            source_connection_id=SOURCE_ID,
            reporter=reporter,
        )

        kinds = [kind for kind, _ in reporter.events]
        assert "table_unchanged" in kinds
        assert "table_start" not in kinds
        assert "table_done" not in kinds
        store.close()

    def test_removed_table_emits_tables_removed_with_count(self) -> None:
        source = FakeDataSource([_users_table(), _orders_table()])
        profiler = CountingProfiler()
        store = SQLiteStore(":memory:")

        index(source=source, profiler=profiler, store=store, source_connection_id=SOURCE_ID)

        # Drop orders from the source — next pass should report 1 removal.
        source.set_tables([_users_table()])
        reporter = RecordingReporter()
        index(
            source=source,
            profiler=profiler,
            store=store,
            source_connection_id=SOURCE_ID,
            reporter=reporter,
        )

        removed_events = [e for kind, e in reporter.events if kind == "tables_removed"]
        assert len(removed_events) == 1
        assert removed_events[0]["removed"] == 1
        store.close()

    def test_pipeline_emits_one_column_enriched_per_column_with_rising_cost(
        self,
    ) -> None:
        source = FakeDataSource([_users_table()])
        profiler = CountingProfiler()
        store = SQLiteStore(":memory:")
        pipeline = self._pipeline()
        reporter = RecordingReporter()

        index(
            source=source,
            profiler=profiler,
            store=store,
            source_connection_id=SOURCE_ID,
            pipeline=pipeline,
            reporter=reporter,
        )

        column_events = [payload for kind, payload in reporter.events if kind == "column_enriched"]
        # users has 2 columns → 2 column_enriched events.
        assert len(column_events) == 2
        # spent_usd must be monotonically non-decreasing across events.
        spends = [e["spent_usd"] for e in column_events]
        assert spends == sorted(spends)
        # And every reported spend must match the final pipeline tally
        # at-or-before that point — last event equals final cost.
        assert spends[-1] == pytest.approx(pipeline.spent_usd)

        # table_start announces 2 columns to enrich.
        table_start = next(e for kind, e in reporter.events if kind == "table_start")
        assert table_start["columns_to_enrich"] == 2
        store.close()

    def test_finish_event_carries_final_result(self) -> None:
        source = FakeDataSource([_users_table()])
        profiler = CountingProfiler()
        store = SQLiteStore(":memory:")
        reporter = RecordingReporter()

        returned = index(
            source=source,
            profiler=profiler,
            store=store,
            source_connection_id=SOURCE_ID,
            reporter=reporter,
        )

        finish_event = next(payload for kind, payload in reporter.events if kind == "finish")
        # Same IndexResult that's returned to the caller.
        assert finish_event["result"] is returned

    def test_cost_cap_aborts_run_without_emitting_on_finish(self) -> None:
        # When `CostCapExceeded` propagates out of `index()`, the
        # reporter never sees `on_finish` — that's the contract the CLI
        # relies on to call `reporter.close()` in its `finally` block.
        # If a future refactor accidentally swallows the exception and
        # calls `on_finish` anyway, the CLI would print "done." on top
        # of an error message. Pin the no-on_finish invariant here.
        from schemabrain.enrichment.llm import FakeLLMClient
        from schemabrain.enrichment.pipeline import CostCapExceeded, EnrichmentPipeline

        source = FakeDataSource([_users_table()])
        profiler = CountingProfiler()
        store = SQLiteStore(":memory:")
        # max_cost_usd=0 trips the cap on the very first call.
        client = FakeLLMClient(text_provider=lambda system, user: "fake")
        pipeline = EnrichmentPipeline(client=client, max_cost_usd=0.0)
        reporter = RecordingReporter()

        with pytest.raises(CostCapExceeded):
            index(
                source=source,
                profiler=profiler,
                store=store,
                source_connection_id=SOURCE_ID,
                pipeline=pipeline,
                reporter=reporter,
            )

        kinds = [kind for kind, _ in reporter.events]
        assert "start" in kinds
        assert "finish" not in kinds
        # Caller is responsible for the teardown (CLI does this via the
        # `finally reporter.close()` block); the indexer itself never
        # calls close.
        assert "close" not in kinds
        store.close()

    def test_reporter_raising_propagates_and_does_not_call_on_finish(self) -> None:
        # The Protocol docstring says reporters MUST NOT raise; this
        # test pins the behavior when that contract is violated, so a
        # future caller knows the indexer offers NO transactional
        # rollback. The store may be partially written (the table row
        # is persisted at line ~325, BEFORE on_table_done at line ~333).
        # That asymmetry is acceptable, but it should be deliberate, not
        # accidental — this test is the place to revisit if we ever
        # decide to wrap reporter calls in try/except.
        class BrokenOnTableDone(RecordingReporter):
            def on_table_done(self, *, schema: str, name: str) -> None:
                super().on_table_done(schema=schema, name=name)
                raise RuntimeError("reporter blew up")

        source = FakeDataSource([_users_table()])
        profiler = CountingProfiler()
        store = SQLiteStore(":memory:")
        reporter = BrokenOnTableDone()

        with pytest.raises(RuntimeError, match="reporter blew up"):
            index(
                source=source,
                profiler=profiler,
                store=store,
                source_connection_id=SOURCE_ID,
                reporter=reporter,
            )

        kinds = [kind for kind, _ in reporter.events]
        assert "table_done" in kinds  # the raising call still ran
        assert "finish" not in kinds  # but we never reached finish
        # Documenting current behavior: store HAS been written, since
        # store.write_table runs before on_table_done. If this assertion
        # ever changes, the docstring on `IndexReporter` needs a matching
        # update.
        assert store.list_tables(source_connection_id=SOURCE_ID) == [("public", "users")]
        store.close()

    def test_null_reporter_has_close_method(self) -> None:
        # close() is part of the Protocol surface; the CLI calls it
        # unconditionally in `finally`. A NullReporter missing this
        # method would crash every `--quiet` or non-TTY run.
        reporter = NullReporter()
        reporter.close()
        reporter.close()  # idempotent


class TestDryRunIndex:
    """Contract for `dry_run_index`: same diff as `index()`, zero side
    effects, reporter events fire as if a real run were happening.

    The cost-estimate constant is part of the user-facing contract — a
    change that moves it would change every `--dry-run` invocation's
    output, so we pin both shape (descriptions x rate) and the rate
    itself indirectly via integer-column tests.
    """

    def test_fresh_run_against_empty_cache_estimates_full_enrichment(self) -> None:
        source = FakeDataSource([_users_table(), _orders_table()])
        store = SQLiteStore(":memory:")

        result = dry_run_index(
            source=source,
            store=store,
            source_connection_id=SOURCE_ID,
            will_enrich=True,
            will_embed=True,
        )

        # 2 tables, 4 columns total (users has 2, orders has 2).
        assert result.tables_seen == 2
        assert result.tables_changed == 2
        assert result.tables_unchanged == 0
        assert result.tables_removed == 0
        assert result.columns_added == 4
        assert result.descriptions_generated == 4
        assert result.embeddings_generated == 4
        # Cost = 4 cols x $0.0003/col = $0.0012
        assert result.llm_cost_usd == pytest.approx(4 * 0.0003)
        store.close()

    def test_writes_nothing_to_store(self) -> None:
        # The central invariant — dry_run MUST NOT touch the store.
        source = FakeDataSource([_users_table()])
        store = SQLiteStore(":memory:")

        dry_run_index(
            source=source,
            store=store,
            source_connection_id=SOURCE_ID,
            will_enrich=True,
            will_embed=True,
        )

        # No tables persisted.
        assert store.list_tables(source_connection_id=SOURCE_ID) == []
        store.close()

    def test_does_not_call_profiler_or_enrich_or_embed(self) -> None:
        # A real `index()` would profile + enrich + embed the changed
        # table. dry_run takes NONE of those collaborators — confirming
        # the function signature alone forces the no-side-effects
        # discipline. This test pins the surface contract.
        import inspect

        sig = inspect.signature(dry_run_index)
        params = set(sig.parameters)
        assert "profiler" not in params
        assert "pipeline" not in params
        assert "embedder" not in params

    def test_unchanged_table_after_real_run_reports_zero_changes(self) -> None:
        # End-to-end: real index → dry_run. The dry-run sees no diff
        # so reports 0 changed / N unchanged / 0 cost.
        source = FakeDataSource([_users_table(), _orders_table()])
        profiler = CountingProfiler()
        store = SQLiteStore(":memory:")

        # Seed the cache with a real run (no enrichment, just fingerprints).
        index(source=source, profiler=profiler, store=store, source_connection_id=SOURCE_ID)

        # Dry-run on the same schema: every table is cached.
        result = dry_run_index(
            source=source,
            store=store,
            source_connection_id=SOURCE_ID,
            will_enrich=True,
            will_embed=True,
        )
        assert result.tables_changed == 0
        assert result.tables_unchanged == 2
        assert result.descriptions_generated == 0
        assert result.llm_cost_usd == 0.0
        store.close()

    def test_will_enrich_false_zeroes_cost(self) -> None:
        source = FakeDataSource([_users_table()])
        store = SQLiteStore(":memory:")

        result = dry_run_index(
            source=source,
            store=store,
            source_connection_id=SOURCE_ID,
            will_enrich=False,
            will_embed=False,
        )
        # Table is "changed" (fresh cache) but no enrichment would happen
        # → no descriptions, no cost, no embeddings.
        assert result.tables_changed == 1
        assert result.descriptions_generated == 0
        assert result.llm_cost_usd == 0.0
        assert result.embeddings_generated == 0
        store.close()

    def test_will_enrich_true_but_will_embed_false_counts_descriptions_only(self) -> None:
        # `--no-embed` without `--no-enrich`: LLM call would still
        # happen, but the result wouldn't be embedded. Costs are
        # description-only; embedding count stays at 0.
        source = FakeDataSource([_users_table()])
        store = SQLiteStore(":memory:")

        result = dry_run_index(
            source=source,
            store=store,
            source_connection_id=SOURCE_ID,
            will_enrich=True,
            will_embed=False,
        )
        assert result.descriptions_generated == 2  # users has 2 columns
        assert result.llm_cost_usd == pytest.approx(2 * 0.0003)
        assert result.embeddings_generated == 0
        store.close()

    def test_removed_tables_counted_but_not_deleted(self) -> None:
        # Seed cache with [users, orders], then "drop" orders from
        # source. Real index() would delete orders; dry_run reports
        # the removal count but the cache row stays.
        source = FakeDataSource([_users_table(), _orders_table()])
        profiler = CountingProfiler()
        store = SQLiteStore(":memory:")

        index(source=source, profiler=profiler, store=store, source_connection_id=SOURCE_ID)
        assert ("public", "orders") in store.list_tables(source_connection_id=SOURCE_ID)

        source.set_tables([_users_table()])
        result = dry_run_index(
            source=source,
            store=store,
            source_connection_id=SOURCE_ID,
            will_enrich=False,
            will_embed=False,
        )

        assert result.tables_removed == 1
        # The cache STILL has the orders row — dry_run is read-only.
        assert ("public", "orders") in store.list_tables(source_connection_id=SOURCE_ID)
        store.close()

    def test_emits_reporter_lifecycle_events_in_order(self) -> None:
        source = FakeDataSource([_users_table()])
        store = SQLiteStore(":memory:")
        reporter = RecordingReporter()

        dry_run_index(
            source=source,
            store=store,
            source_connection_id=SOURCE_ID,
            will_enrich=True,
            will_embed=True,
            reporter=reporter,
        )

        kinds = [kind for kind, _ in reporter.events]
        # Same sequence as a real fresh run: start → tables_removed
        # → table_start → 2xcolumn_enriched (users has 2 cols)
        # → table_done → finish.
        assert kinds == [
            "start",
            "tables_removed",
            "table_start",
            "column_enriched",
            "column_enriched",
            "table_done",
            "finish",
        ]
        store.close()

    def test_column_enriched_events_carry_monotonic_estimated_cost(self) -> None:
        source = FakeDataSource([_users_table(), _orders_table()])
        store = SQLiteStore(":memory:")
        reporter = RecordingReporter()

        dry_run_index(
            source=source,
            store=store,
            source_connection_id=SOURCE_ID,
            will_enrich=True,
            will_embed=True,
            reporter=reporter,
        )

        column_events = [payload for kind, payload in reporter.events if kind == "column_enriched"]
        # 4 cols total across 2 tables.
        assert len(column_events) == 4
        spends = [e["spent_usd"] for e in column_events]
        # Cumulative + monotonic, matching the real-run convention.
        assert spends == sorted(spends)
        # Final estimate equals 4 x $0.0003 = $0.0012.
        assert spends[-1] == pytest.approx(4 * 0.0003)
        store.close()

    def test_custom_cost_constant_overrides_default(self) -> None:
        # The `avg_cost_per_column_usd` argument lets a caller substitute
        # their own measurement (e.g. for Sonnet routing, or a future
        # prompt with different costs) without monkeypatching.
        source = FakeDataSource([_users_table()])
        store = SQLiteStore(":memory:")

        result = dry_run_index(
            source=source,
            store=store,
            source_connection_id=SOURCE_ID,
            will_enrich=True,
            will_embed=True,
            avg_cost_per_column_usd=0.005,
        )
        assert result.llm_cost_usd == pytest.approx(2 * 0.005)
        store.close()

    def test_default_reporter_is_no_op_and_does_not_crash(self) -> None:
        # Sibling of the same test for `index()` — reporter=None must
        # default to NullReporter without raising.
        source = FakeDataSource([_users_table()])
        store = SQLiteStore(":memory:")

        result = dry_run_index(
            source=source,
            store=store,
            source_connection_id=SOURCE_ID,
            will_enrich=False,
            will_embed=False,
        )
        assert result.tables_seen == 1
        store.close()
