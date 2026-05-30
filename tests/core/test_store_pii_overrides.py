"""Tests for the operator-origin PII tag override store API.

Exercises:
  - `upsert_column_pii_tag_override` writes `origin='operator'` rows
    and replaces prior rows (heuristic OR operator) for the same key
  - `delete_column_pii_tag_override` only removes operator rows
    (won't accidentally wipe heuristic classifier output)
  - `list_column_pii_tags_with_origin` returns provenance + supports
    origin filter
"""

from __future__ import annotations

from pathlib import Path

from schemabrain.core.store import SQLiteStore

SRC = "test_source"
TABLE = "public.users"


def test_upsert_writes_operator_origin(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "sb.db")
    try:
        store.upsert_column_pii_tag_override(
            source_connection_id=SRC,
            qualified_table=TABLE,
            column_name="email",
            sensitivity="internal",
            categories=frozenset(),
        )
        rows = store.list_column_pii_tags_with_origin(source_connection_id=SRC)
        assert len(rows) == 1
        qt, col, sens, cats, origin = rows[0]
        assert qt == TABLE
        assert col == "email"
        assert sens == "internal"
        assert cats == frozenset()
        assert origin == "operator"
    finally:
        store.close()


def test_upsert_replaces_heuristic_row(tmp_path: Path) -> None:
    """An operator override REPLACES a heuristic row at the same PK.
    Documents the no-layering design called out in the DDL comment."""
    store = SQLiteStore(tmp_path / "sb.db")
    try:
        # Heuristic write — uses the bulk write path.
        store.write_column_pii_tags(
            source_connection_id=SRC,
            qualified_table=TABLE,
            tags={"email": ("pii", frozenset({"contact"}))},
        )
        # Operator override on the same column.
        store.upsert_column_pii_tag_override(
            source_connection_id=SRC,
            qualified_table=TABLE,
            column_name="email",
            sensitivity="internal",
            categories=frozenset(),
        )
        rows = store.list_column_pii_tags_with_origin(source_connection_id=SRC)
        assert len(rows) == 1
        _qt, _col, sens, cats, origin = rows[0]
        assert sens == "internal"
        assert cats == frozenset()
        assert origin == "operator"
    finally:
        store.close()


def test_upsert_idempotent_on_repeat(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "sb.db")
    try:
        for _ in range(3):
            store.upsert_column_pii_tag_override(
                source_connection_id=SRC,
                qualified_table=TABLE,
                column_name="email",
                sensitivity="internal",
                categories=frozenset(),
            )
        rows = store.list_column_pii_tags_with_origin(source_connection_id=SRC)
        assert len(rows) == 1
    finally:
        store.close()


def test_delete_removes_operator_row(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "sb.db")
    try:
        store.upsert_column_pii_tag_override(
            source_connection_id=SRC,
            qualified_table=TABLE,
            column_name="email",
            sensitivity="internal",
            categories=frozenset(),
        )
        deleted = store.delete_column_pii_tag_override(
            source_connection_id=SRC,
            qualified_table=TABLE,
            column_name="email",
        )
        assert deleted is True
        rows = store.list_column_pii_tags_with_origin(source_connection_id=SRC)
        assert rows == []
    finally:
        store.close()


def test_delete_returns_false_when_no_operator_row(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "sb.db")
    try:
        deleted = store.delete_column_pii_tag_override(
            source_connection_id=SRC,
            qualified_table=TABLE,
            column_name="email",
        )
        assert deleted is False
    finally:
        store.close()


def test_delete_does_not_touch_heuristic_row(tmp_path: Path) -> None:
    """Critical safety: clearing an override that doesn't exist must
    NOT wipe the heuristic row the operator hasn't asserted on."""
    store = SQLiteStore(tmp_path / "sb.db")
    try:
        store.write_column_pii_tags(
            source_connection_id=SRC,
            qualified_table=TABLE,
            tags={"email": ("pii", frozenset({"contact"}))},
        )
        deleted = store.delete_column_pii_tag_override(
            source_connection_id=SRC,
            qualified_table=TABLE,
            column_name="email",
        )
        assert deleted is False
        rows = store.list_column_pii_tags_with_origin(source_connection_id=SRC)
        assert len(rows) == 1
        _qt, _col, _sens, _cats, origin = rows[0]
        assert origin == "heuristic"
    finally:
        store.close()


def test_list_with_origin_filter(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "sb.db")
    try:
        store.write_column_pii_tags(
            source_connection_id=SRC,
            qualified_table=TABLE,
            tags={
                "email": ("pii", frozenset({"contact"})),
                "phone": ("pii", frozenset({"contact"})),
            },
        )
        store.upsert_column_pii_tag_override(
            source_connection_id=SRC,
            qualified_table="public.payment_methods",
            column_name="card_number_last4",
            sensitivity="internal",
            categories=frozenset(),
        )
        all_rows = store.list_column_pii_tags_with_origin(source_connection_id=SRC)
        assert len(all_rows) == 3

        operator_only = store.list_column_pii_tags_with_origin(
            source_connection_id=SRC, origin="operator"
        )
        assert len(operator_only) == 1
        assert operator_only[0][1] == "card_number_last4"

        heuristic_only = store.list_column_pii_tags_with_origin(
            source_connection_id=SRC, origin="heuristic"
        )
        assert len(heuristic_only) == 2
        assert {row[1] for row in heuristic_only} == {"email", "phone"}
    finally:
        store.close()


def test_list_orders_by_qualified_table_then_column(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "sb.db")
    try:
        store.upsert_column_pii_tag_override(
            source_connection_id=SRC,
            qualified_table="public.users",
            column_name="email",
            sensitivity="internal",
            categories=frozenset(),
        )
        store.upsert_column_pii_tag_override(
            source_connection_id=SRC,
            qualified_table="public.users",
            column_name="phone",
            sensitivity="internal",
            categories=frozenset(),
        )
        store.upsert_column_pii_tag_override(
            source_connection_id=SRC,
            qualified_table="public.payment_methods",
            column_name="card_number_last4",
            sensitivity="internal",
            categories=frozenset(),
        )
        rows = store.list_column_pii_tags_with_origin(source_connection_id=SRC)
        keys = [(row[0], row[1]) for row in rows]
        assert keys == sorted(keys)
    finally:
        store.close()
