"""SQLiteStore tests for column_pii_tags (schema v12)."""

from __future__ import annotations

from pathlib import Path

from schemabrain.core.store import SQLiteStore

SRC = "src_a"
TABLE = "public.users"


class TestRoundTrip:
    def test_write_then_get_returns_same_tags(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "sb.db") as store:
            store.write_column_pii_tags(
                source_connection_id=SRC,
                qualified_table=TABLE,
                tags={
                    "email": ("pii", frozenset({"contact"})),
                    "amount": ("pii", frozenset({"financial"})),
                    "id": ("public", frozenset()),
                },
            )
            result = store.get_column_pii_tags(
                source_connection_id=SRC,
                qualified_table=TABLE,
                columns=["email", "amount", "id"],
            )
            assert result == {
                "email": ("pii", frozenset({"contact"})),
                "amount": ("pii", frozenset({"financial"})),
                "id": ("public", frozenset()),
            }

    def test_multi_category_round_trip(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "sb.db") as store:
            store.write_column_pii_tags(
                source_connection_id=SRC,
                qualified_table=TABLE,
                tags={
                    "email_password": ("pii", frozenset({"contact", "credential"})),
                },
            )
            result = store.get_column_pii_tags(
                source_connection_id=SRC,
                qualified_table=TABLE,
                columns=["email_password"],
            )
            assert result["email_password"] == (
                "pii",
                frozenset({"contact", "credential"}),
            )


class TestAtomicReplace:
    def test_second_write_replaces_first(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "sb.db") as store:
            store.write_column_pii_tags(
                source_connection_id=SRC,
                qualified_table=TABLE,
                tags={
                    "email": ("pii", frozenset({"contact"})),
                    "old_column": ("pii", frozenset({"credential"})),
                },
            )
            # Second write — `old_column` should not survive.
            store.write_column_pii_tags(
                source_connection_id=SRC,
                qualified_table=TABLE,
                tags={
                    "email": ("pii", frozenset({"contact"})),
                    "phone": ("pii", frozenset({"contact"})),
                },
            )
            result = store.get_column_pii_tags(
                source_connection_id=SRC,
                qualified_table=TABLE,
                columns=["email", "old_column", "phone"],
            )
            assert "old_column" not in result
            assert set(result.keys()) == {"email", "phone"}

    def test_empty_tags_wipes_table(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "sb.db") as store:
            store.write_column_pii_tags(
                source_connection_id=SRC,
                qualified_table=TABLE,
                tags={"email": ("pii", frozenset({"contact"}))},
            )
            # `--no-pii-classify` opt-out shape: empty mapping clears
            # all rows for the table without writing replacements.
            store.write_column_pii_tags(
                source_connection_id=SRC,
                qualified_table=TABLE,
                tags={},
            )
            result = store.get_column_pii_tags(
                source_connection_id=SRC,
                qualified_table=TABLE,
                columns=["email"],
            )
            assert result == {}

    def test_other_tables_unaffected(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "sb.db") as store:
            store.write_column_pii_tags(
                source_connection_id=SRC,
                qualified_table="public.users",
                tags={"email": ("pii", frozenset({"contact"}))},
            )
            store.write_column_pii_tags(
                source_connection_id=SRC,
                qualified_table="public.orders",
                tags={"amount": ("pii", frozenset({"financial"}))},
            )
            # Re-writing one table doesn't touch the other.
            store.write_column_pii_tags(
                source_connection_id=SRC,
                qualified_table="public.users",
                tags={},
            )
            orders = store.get_column_pii_tags(
                source_connection_id=SRC,
                qualified_table="public.orders",
                columns=["amount"],
            )
            assert orders["amount"] == ("pii", frozenset({"financial"}))

    def test_other_sources_unaffected(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "sb.db") as store:
            store.write_column_pii_tags(
                source_connection_id="src_a",
                qualified_table=TABLE,
                tags={"email": ("pii", frozenset({"contact"}))},
            )
            store.write_column_pii_tags(
                source_connection_id="src_b",
                qualified_table=TABLE,
                tags={"phone": ("pii", frozenset({"contact"}))},
            )
            # Wipe src_a; src_b survives.
            store.write_column_pii_tags(
                source_connection_id="src_a",
                qualified_table=TABLE,
                tags={},
            )
            b_result = store.get_column_pii_tags(
                source_connection_id="src_b",
                qualified_table=TABLE,
                columns=["phone"],
            )
            assert b_result["phone"] == ("pii", frozenset({"contact"}))


class TestGetColumnPiiTags:
    def test_missing_columns_omitted_from_result(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "sb.db") as store:
            store.write_column_pii_tags(
                source_connection_id=SRC,
                qualified_table=TABLE,
                tags={"email": ("pii", frozenset({"contact"}))},
            )
            result = store.get_column_pii_tags(
                source_connection_id=SRC,
                qualified_table=TABLE,
                columns=["email", "never_classified", "also_missing"],
            )
            # Caller treats absence as ("public", frozenset()).
            assert set(result.keys()) == {"email"}

    def test_empty_columns_returns_empty_dict(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "sb.db") as store:
            result = store.get_column_pii_tags(
                source_connection_id=SRC,
                qualified_table=TABLE,
                columns=[],
            )
            assert result == {}

    def test_duplicate_columns_deduplicated(self, tmp_path: Path) -> None:
        # A metric whose measure column also appears in group_by would
        # legitimately pass the same column twice. The result must be a
        # single mapping entry, not raise on duplicate parameters.
        with SQLiteStore(tmp_path / "sb.db") as store:
            store.write_column_pii_tags(
                source_connection_id=SRC,
                qualified_table=TABLE,
                tags={"email": ("pii", frozenset({"contact"}))},
            )
            result = store.get_column_pii_tags(
                source_connection_id=SRC,
                qualified_table=TABLE,
                columns=["email", "email", "email"],
            )
            assert result == {"email": ("pii", frozenset({"contact"}))}


class TestStorageEncoding:
    def test_categories_stored_in_sorted_csv_form(self, tmp_path: Path) -> None:
        # The storage convention (sorted CSV) is the same one
        # `example_queries.pii_categories` uses. Two writes that
        # produce the same conceptual set must produce the same
        # bytes — important for `mcp_audit` grep convenience.
        with SQLiteStore(tmp_path / "sb.db") as store:
            store.write_column_pii_tags(
                source_connection_id=SRC,
                qualified_table=TABLE,
                tags={
                    "col_a": ("pii", frozenset({"contact", "financial"})),
                    # Same conceptual set, frozenset construction order
                    # has no observable effect on the storage form.
                    "col_b": ("pii", frozenset({"financial", "contact"})),
                },
            )
            row_a, row_b = (
                store._require_conn()
                .execute(
                    "SELECT column_name, categories FROM column_pii_tags "
                    "WHERE qualified_table = ? ORDER BY column_name",
                    (TABLE,),
                )
                .fetchall()
            )
            assert row_a["categories"] == "contact,financial"
            assert row_b["categories"] == "contact,financial"

    def test_empty_categories_stored_as_empty_string(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "sb.db") as store:
            store.write_column_pii_tags(
                source_connection_id=SRC,
                qualified_table=TABLE,
                tags={"id": ("public", frozenset())},
            )
            row = (
                store._require_conn()
                .execute("SELECT categories FROM column_pii_tags WHERE column_name = 'id'")
                .fetchone()
            )
            assert row["categories"] == ""

    def test_default_origin_is_heuristic(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "sb.db") as store:
            store.write_column_pii_tags(
                source_connection_id=SRC,
                qualified_table=TABLE,
                tags={"email": ("pii", frozenset({"contact"}))},
            )
            row = (
                store._require_conn()
                .execute("SELECT origin FROM column_pii_tags WHERE column_name = 'email'")
                .fetchone()
            )
            assert row["origin"] == "heuristic"


class TestGetColumnPiiConfidence:
    """v15 per-column PII confidence reader (band + raw score).

    At launch the only WRITER is `write_column_pii_tags`, which does NOT
    populate `pii_confidence` / `pii_confidence_score` (the index-time
    score writer lands in a follow-up). So the reachable behaviour today
    is `(None, None)` for every classified column; the raw-UPDATE-seeded
    test pins the read path itself against the day the writer exists.
    """

    def test_returns_none_when_unpopulated(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "sb.db") as store:
            store.write_column_pii_tags(
                source_connection_id=SRC,
                qualified_table=TABLE,
                tags={"email": ("pii", frozenset({"contact"}))},
            )
            result = store.get_column_pii_confidence(
                source_connection_id=SRC,
                qualified_table=TABLE,
                columns=["email"],
            )
            assert result == {"email": (None, None)}

    def test_reads_raw_seeded_band_and_score(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "sb.db") as store:
            store.write_column_pii_tags(
                source_connection_id=SRC,
                qualified_table=TABLE,
                tags={"email": ("pii", frozenset({"contact"}))},
            )
            # Seed the band + score directly — stands in for the future
            # index-time score writer so the read path is locked now.
            store._require_conn().execute(
                "UPDATE column_pii_tags SET pii_confidence = ?, pii_confidence_score = ? "
                "WHERE source_connection_id = ? AND qualified_table = ? AND column_name = ?",
                ("high", 0.97, SRC, TABLE, "email"),
            )
            store._require_conn().commit()
            result = store.get_column_pii_confidence(
                source_connection_id=SRC,
                qualified_table=TABLE,
                columns=["email"],
            )
            assert result == {"email": ("high", 0.97)}

    def test_missing_columns_omitted_from_result(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "sb.db") as store:
            store.write_column_pii_tags(
                source_connection_id=SRC,
                qualified_table=TABLE,
                tags={"email": ("pii", frozenset({"contact"}))},
            )
            result = store.get_column_pii_confidence(
                source_connection_id=SRC,
                qualified_table=TABLE,
                columns=["email", "never_classified"],
            )
            assert set(result.keys()) == {"email"}

    def test_empty_columns_returns_empty_dict(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "sb.db") as store:
            result = store.get_column_pii_confidence(
                source_connection_id=SRC,
                qualified_table=TABLE,
                columns=[],
            )
            assert result == {}

    def test_duplicate_columns_deduplicated(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "sb.db") as store:
            store.write_column_pii_tags(
                source_connection_id=SRC,
                qualified_table=TABLE,
                tags={"email": ("pii", frozenset({"contact"}))},
            )
            result = store.get_column_pii_confidence(
                source_connection_id=SRC,
                qualified_table=TABLE,
                columns=["email", "email", "email"],
            )
            assert result == {"email": (None, None)}
