"""Tests for the enrichment-freshness read methods that back the Drift
surface (`latest_enriched_at` + `has_stale_prompt_version`).

These are pure, per-source reads over `column_descriptions`:

  - `latest_enriched_at` → MAX(generated_at) epoch seconds, or None when
    the source has no descriptions (never enriched / `--no-enrich`).
  - `has_stale_prompt_version` → True iff any persisted description was
    generated with a prompt version other than the live one; False when
    every description matches or the source has none.

Both are isolated per source_connection_id (no cross-source bleed).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import schemabrain.core.store as store_module
from schemabrain.core.description import ColumnDescription
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore

SOURCE_A = "src_a"
SOURCE_B = "src_b"
LIVE_PROMPT = "2026-06-08-1"


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


def _desc(prompt_version: str) -> ColumnDescription:
    return ColumnDescription(
        text="a primary key",
        model="claude-haiku-4-5",
        prompt_version=prompt_version,
        input_tokens=10,
        cached_input_tokens=0,
        output_tokens=5,
        cost_usd=0.0001,
    )


def _seed_descriptions(
    store: SQLiteStore,
    *,
    source_connection_id: str,
    table: str,
    prompt_version: str,
) -> None:
    store.write_table(_table(table), source_connection_id=source_connection_id)
    store.write_table_descriptions(
        "public",
        table,
        source_connection_id=source_connection_id,
        descriptions={"id": _desc(prompt_version)},
    )


class TestLatestEnrichedAt:
    def test_none_when_no_descriptions(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            assert store.latest_enriched_at(source_connection_id=SOURCE_A) is None

    def test_none_when_table_indexed_without_enrichment(self, tmp_path: Path) -> None:
        # `index --no-enrich` writes the table but zero description rows.
        with SQLiteStore(tmp_path / "store.db") as store:
            store.write_table(_table("users"), source_connection_id=SOURCE_A)
            assert store.latest_enriched_at(source_connection_id=SOURCE_A) is None

    def test_returns_generated_at_epoch_seconds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(store_module.time, "time", lambda: 1_700_000_000.0)
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_descriptions(
                store, source_connection_id=SOURCE_A, table="users", prompt_version=LIVE_PROMPT
            )
            assert store.latest_enriched_at(source_connection_id=SOURCE_A) == 1_700_000_000

    def test_returns_max_across_write_batches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = {"now": 1_700_000_000.0}
        monkeypatch.setattr(store_module.time, "time", lambda: clock["now"])
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_descriptions(
                store, source_connection_id=SOURCE_A, table="users", prompt_version=LIVE_PROMPT
            )
            clock["now"] = 1_700_000_500.0
            _seed_descriptions(
                store, source_connection_id=SOURCE_A, table="orders", prompt_version=LIVE_PROMPT
            )
            assert store.latest_enriched_at(source_connection_id=SOURCE_A) == 1_700_000_500

    def test_isolated_per_source(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = {"now": 1_700_000_000.0}
        monkeypatch.setattr(store_module.time, "time", lambda: clock["now"])
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_descriptions(
                store, source_connection_id=SOURCE_A, table="users", prompt_version=LIVE_PROMPT
            )
            clock["now"] = 1_700_009_999.0
            _seed_descriptions(
                store, source_connection_id=SOURCE_B, table="users", prompt_version=LIVE_PROMPT
            )
            assert store.latest_enriched_at(source_connection_id=SOURCE_A) == 1_700_000_000
            assert store.latest_enriched_at(source_connection_id=SOURCE_B) == 1_700_009_999


class TestHasStalePromptVersion:
    def test_false_when_no_descriptions(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            assert (
                store.has_stale_prompt_version(
                    source_connection_id=SOURCE_A, current_prompt_version=LIVE_PROMPT
                )
                is False
            )

    def test_false_when_all_descriptions_match(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_descriptions(
                store, source_connection_id=SOURCE_A, table="users", prompt_version=LIVE_PROMPT
            )
            assert (
                store.has_stale_prompt_version(
                    source_connection_id=SOURCE_A, current_prompt_version=LIVE_PROMPT
                )
                is False
            )

    def test_true_when_a_description_predates_live_prompt(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_descriptions(
                store, source_connection_id=SOURCE_A, table="users", prompt_version="2026-01-01-1"
            )
            assert (
                store.has_stale_prompt_version(
                    source_connection_id=SOURCE_A, current_prompt_version=LIVE_PROMPT
                )
                is True
            )

    def test_isolated_per_source(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_descriptions(
                store, source_connection_id=SOURCE_A, table="users", prompt_version="2026-01-01-1"
            )
            _seed_descriptions(
                store, source_connection_id=SOURCE_B, table="users", prompt_version=LIVE_PROMPT
            )
            assert (
                store.has_stale_prompt_version(
                    source_connection_id=SOURCE_A, current_prompt_version=LIVE_PROMPT
                )
                is True
            )
            assert (
                store.has_stale_prompt_version(
                    source_connection_id=SOURCE_B, current_prompt_version=LIVE_PROMPT
                )
                is False
            )
