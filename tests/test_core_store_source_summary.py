"""Tests for SQLiteStore.summarize_sources — the per-source rollup that
backs the dashboard source selector (engine/state are added by the
sidecar; the store owns the counts + last-indexed timestamp).

Contract locked here:
  - empty store → []
  - one entry per distinct source_connection_id, in the same sorted
    order as `list_distinct_source_connection_ids`
  - table_count / entity_count isolated per source (no cross-bleed)
  - last_indexed_at is the MAX(tables.indexed_at) for the source
"""

from __future__ import annotations

from pathlib import Path

from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SourceSummary, SQLiteStore

SOURCE_A = "src_a"
SOURCE_B = "src_b"


def _table(name: str) -> Table:
    return Table(
        name=name,
        schema_name="public",
        columns=(
            Column(
                name="id",
                table_name=name,
                schema_name="public",
                data_type="bigint",
                nullable=False,
                ordinal_position=1,
                is_primary_key=True,
            ),
        ),
    )


def _entity(name: str, qualified_table: str) -> Entity:
    return Entity(
        name=name,
        description="",
        binding=SingleTableBinding(qualified_table=qualified_table),
        identity="id",
        origin="manual",
    )


class TestSummarizeSources:
    def test_empty_store_returns_empty_list(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            assert store.summarize_sources() == []

    def test_single_source_counts_tables_and_entities(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            store.write_table(_table("users"), source_connection_id=SOURCE_A)
            store.write_table(_table("orders"), source_connection_id=SOURCE_A)
            store.write_entity(_entity("customer", "public.users"), source_connection_id=SOURCE_A)

            summaries = store.summarize_sources()

        assert len(summaries) == 1
        only = summaries[0]
        assert isinstance(only, SourceSummary)
        assert only.source_connection_id == SOURCE_A
        assert only.table_count == 2
        assert only.entity_count == 1
        assert isinstance(only.last_indexed_at, int)

    def test_counts_are_isolated_per_source_and_sorted(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            # Source A: two tables, one entity.
            store.write_table(_table("users"), source_connection_id=SOURCE_A)
            store.write_table(_table("orders"), source_connection_id=SOURCE_A)
            store.write_entity(_entity("customer", "public.users"), source_connection_id=SOURCE_A)
            # Source B: one table, no entities.
            store.write_table(_table("events"), source_connection_id=SOURCE_B)

            summaries = store.summarize_sources()

        # Sorted by source id → A before B, matching list_distinct_*.
        assert [s.source_connection_id for s in summaries] == [SOURCE_A, SOURCE_B]
        by_id = {s.source_connection_id: s for s in summaries}
        assert by_id[SOURCE_A].table_count == 2
        assert by_id[SOURCE_A].entity_count == 1
        assert by_id[SOURCE_B].table_count == 1
        assert by_id[SOURCE_B].entity_count == 0
