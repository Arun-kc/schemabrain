"""Tests for the v0.5 `example_queries` writer.

`SQLiteStore.write_example_queries` is the writer half of tool #5's
storage primitive (the read half landed with PR #20). The query log
mining pipeline (`schemabrain.mining`) is the only production caller;
the writer is exposed on `SQLiteStore` and the `Store` Protocol so a
future alternative miner (e.g. a hosted dashboard pipeline) can drop
in without subclassing.

The write semantics are UPSERT on `(source_connection_id, schema_name,
table_name, sql_text)`:
  - First write inserts; `first_seen_at` and `last_seen_at` both take
    the value passed by the caller.
  - Re-write of the same 4-tuple updates `observation_count`,
    `last_seen_at`, and `sensitivity`/`pii_categories` to whatever the
    caller now believes is true (pg_stat_statements is cumulative since
    last reset; the caller passes the absolute count).
  - `first_seen_at` is preserved across re-writes so a row's first
    observation timestamp survives re-mining.

A new UNIQUE INDEX `idx_example_queries_unique` enforces the conflict
target; schema version bumps 6 → 7 to land it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from schemabrain.core.example_query import ExampleQuery
from schemabrain.core.store import SchemaVersionMismatchError, SQLiteStore
from tests._example_query_helpers import seed_table_for_examples as _seed_table


def _eq(
    *,
    schema: str = "public",
    table: str = "orders",
    sql_text: str = "SELECT id FROM orders",
    observation_count: int = 1,
    first_seen_at: int = 1_700_000_000,
    last_seen_at: int = 1_700_000_000,
    source: str = "pg_stat_statements",
    sensitivity: str = "public",
    pii_categories: frozenset[str] | None = None,
) -> ExampleQuery:
    """Compact constructor for test ExampleQuery rows."""
    return ExampleQuery(
        schema_name=schema,
        table_name=table,
        sql_text=sql_text,
        observation_count=observation_count,
        first_seen_at=first_seen_at,
        last_seen_at=last_seen_at,
        source=source,  # type: ignore[arg-type]
        sensitivity=sensitivity,  # type: ignore[arg-type]
        pii_categories=frozenset() if pii_categories is None else pii_categories,  # type: ignore[arg-type]
    )


class TestSchemaVersionBumpToV16:
    """v13→v16 migration-chain contract.

    Fresh stores stamp `schema_version='16'`; v15 stores migrate in-place
    via `_migrate_v15_to_v16` (the `graph_edges.cardinality` ALTER); v14
    stores chain 14→15→16; v13 stores chain 13→14→15→16 (Option B — each
    `_migrate_vN_to_vN+1` in a single open, with `existing_version`
    reassigned between legs); pre-v13 stores still raise
    `SchemaVersionMismatchError` — the pre-alpha cliff does NOT move under
    chaining.
    """

    # Columns/tables each version added — dropped here to fabricate an
    # older on-disk shape from the current (v15-aware) codebase. `"group"`
    # is pre-quoted because GROUP is a reserved keyword.
    _V14_TRUST_COLS = ("inference_method", "validation_state")
    _V15_ENTITY_COLS = ('"group"', "bind_confidence", "rationale")
    _V15_TABLES_COLS = ("estimated_row_count",)
    _V15_PII_COLS = ("pii_confidence", "pii_confidence_score")
    _V15_DESC_COLS = ("semantic_type", "meaning", "col_confidence")

    @classmethod
    def _downgrade_to_v14(cls, conn: sqlite3.Connection) -> None:
        """Strip the v15 additions so the store looks like a v14 store."""
        conn.execute("DROP TABLE IF EXISTS graph_edges")
        conn.execute("DROP TABLE IF EXISTS graph_nodes")
        for col in cls._V15_ENTITY_COLS:
            conn.execute(f"ALTER TABLE entities DROP COLUMN {col}")
        for col in cls._V15_TABLES_COLS:
            conn.execute(f"ALTER TABLE tables DROP COLUMN {col}")
        for col in cls._V15_PII_COLS:
            conn.execute(f"ALTER TABLE column_pii_tags DROP COLUMN {col}")
        for col in cls._V15_DESC_COLS:
            conn.execute(f"ALTER TABLE column_descriptions DROP COLUMN {col}")

    @classmethod
    def _downgrade_to_v13(cls, conn: sqlite3.Connection) -> None:
        """Strip v15 AND v14 additions so the store looks like a v13 store."""
        cls._downgrade_to_v14(conn)
        for table in ("entities", "metrics", "canonical_joins"):
            for col in cls._V14_TRUST_COLS:
                conn.execute(f"ALTER TABLE {table} DROP COLUMN {col}")

    @staticmethod
    def _seed_semantic_rows(conn: sqlite3.Connection) -> None:
        """Seed all four semantic surfaces honouring the FK chain.

        Uses the minimal v13/v14 column shape (no trust / v15 columns)
        so the same seed works against either downgraded shape — the
        omitted columns either don't exist yet or take their DEFAULT.
        """
        for name in ("orders", "users"):
            conn.execute(
                "INSERT INTO tables (schema_name, name, source_connection_id, indexed_at) "
                "VALUES (?, ?, ?, ?)",
                ("public", name, "src", 1_700_000_000),
            )
        conn.execute(
            "INSERT INTO entities ("
            "source_connection_id, name, description, binding_schema, "
            "binding_table, identity, origin, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("src", "order", "", "public", "orders", "id", "suggested", 0, 0),
        )
        conn.execute(
            "INSERT INTO entities ("
            "source_connection_id, name, description, binding_schema, "
            "binding_table, identity, origin, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("src", "user", "", "public", "users", "id", "manual", 0, 0),
        )
        conn.execute(
            "INSERT INTO metrics ("
            "source_connection_id, name, description, entity, measure_agg, "
            "measure_column, measure_expression, time_dimension, "
            "time_grains, origin, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("src", "order_count", "", "order", "count", "id", None, None, "", "suggested", 0, 0),
        )
        conn.execute(
            "INSERT INTO canonical_joins ("
            "source_connection_id, name, description, source_entity, "
            "target_entity, on_columns_json, origin, cardinality, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "src",
                "order_to_user",
                "",
                "order",
                "user",
                '[["user_id", "id"]]',
                "manual",
                "many_to_one",
                0,
                0,
            ),
        )
        conn.execute(
            "INSERT INTO column_pii_tags ("
            "source_connection_id, qualified_table, column_name, sensitivity, "
            "categories, origin, classified_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("src", "public.users", "email", "pii", "", "heuristic", 0),
        )
        conn.commit()

    @staticmethod
    def _semantic_counts(conn: sqlite3.Connection) -> dict[str, int]:
        return {
            table: conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
            for table in ("entities", "metrics", "canonical_joins", "column_pii_tags")
        }

    @classmethod
    def _downgrade_to_v15(cls, conn: sqlite3.Connection) -> None:
        """Strip only the v16 addition so the store looks like a v15 store
        (graph_edges present, but WITHOUT the cardinality column)."""
        conn.execute("ALTER TABLE graph_edges DROP COLUMN cardinality")

    def test_fresh_store_has_schema_version_16(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "sb.db") as store:
            row = (
                store._require_conn()
                .execute("SELECT value FROM schemabrain_meta WHERE key = 'schema_version'")
                .fetchone()
            )
            assert row["value"] == "16"

    def test_v15_store_migrates_to_v16(self, tmp_path: Path) -> None:
        # Fabricate a genuine v15 store: graph_edges present but WITHOUT
        # the v16 cardinality column, carrying a real projected edge.
        # Re-open with v16 code and assert (a) version bumped to 16, (b)
        # the ALTER added the cardinality column, (c) the pre-existing edge
        # survived with a NULL cardinality (backfill, never fabricated).
        db_path = tmp_path / "sb.db"
        with SQLiteStore(db_path) as store:
            conn = store._require_conn()
            self._seed_semantic_rows(conn)  # entities + canonical_joins for the edge FK
            self._downgrade_to_v15(conn)
            conn.execute(
                "INSERT INTO graph_edges (source_connection_id, join_name, source_entity, "
                "target_entity, edge_origin, canonical_path_rank, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("src", "order_to_user", "order", "user", "declared", 1, 0),
            )
            conn.execute("UPDATE schemabrain_meta SET value = '15' WHERE key = 'schema_version'")
            conn.commit()

        with SQLiteStore(db_path) as store:
            conn = store._require_conn()
            assert (
                conn.execute(
                    "SELECT value FROM schemabrain_meta WHERE key = 'schema_version'"
                ).fetchone()["value"]
                == "16"
            )
            edge_cols = {r["name"] for r in conn.execute("PRAGMA table_info(graph_edges)")}
            assert "cardinality" in edge_cols
            row = conn.execute(
                "SELECT cardinality FROM graph_edges WHERE join_name = 'order_to_user'"
            ).fetchone()
            assert row["cardinality"] is None  # backfilled NULL, never fabricated

    def test_v14_store_chains_to_v16(self, tmp_path: Path) -> None:
        # Fabricate a v14 store (drop the v15 columns + graph tables),
        # seed all four semantic surfaces, re-open with v16 code, and
        # verify (a) version chained to 16, (b) the v15 columns exist and
        # carry the NULL/default backfill, (c) every seeded row across
        # entities / metrics / canonical_joins / column_pii_tags
        # survives, (d) the graph tables were (re)created EMPTY by the DDL
        # loop (the 15→16 leg's ALTER is skipped because graph_edges does
        # not exist yet at migration time), already carrying the v16
        # cardinality column.
        db_path = tmp_path / "sb.db"
        with SQLiteStore(db_path) as store:
            conn = store._require_conn()
            self._downgrade_to_v14(conn)
            conn.execute("UPDATE schemabrain_meta SET value = '14' WHERE key = 'schema_version'")
            self._seed_semantic_rows(conn)
            before = self._semantic_counts(conn)

        with SQLiteStore(db_path) as store:
            conn = store._require_conn()
            assert (
                conn.execute(
                    "SELECT value FROM schemabrain_meta WHERE key = 'schema_version'"
                ).fetchone()["value"]
                == "16"
            )
            # (b) new columns present + backfilled to NULL/default.
            ent = conn.execute(
                'SELECT bind_confidence, rationale, "group" FROM entities WHERE name = ?',
                ("order",),
            ).fetchone()
            assert ent["bind_confidence"] is None
            assert ent["rationale"] == ""
            assert ent["group"] == "other"
            assert (
                conn.execute(
                    "SELECT estimated_row_count FROM tables WHERE name = 'orders'"
                ).fetchone()["estimated_row_count"]
                is None
            )
            pii = conn.execute(
                "SELECT pii_confidence, pii_confidence_score FROM column_pii_tags"
            ).fetchone()
            assert pii["pii_confidence"] is None
            assert pii["pii_confidence_score"] is None
            desc_cols = {
                r["name"] for r in conn.execute("PRAGMA table_info(column_descriptions)").fetchall()
            }
            assert {"semantic_type", "meaning", "col_confidence"} <= desc_cols
            # (c) four-way round-trip preservation — no row lost.
            assert self._semantic_counts(conn) == before
            assert before == {
                "entities": 2,
                "metrics": 1,
                "canonical_joins": 1,
                "column_pii_tags": 1,
            }
            # (d) graph tables (re)exist + are EMPTY on a migrated store
            # (the projection only runs on the next `index`), and the
            # freshly-created graph_edges already carries the v16 column.
            assert conn.execute("SELECT count(*) AS n FROM graph_nodes").fetchone()["n"] == 0
            assert conn.execute("SELECT count(*) AS n FROM graph_edges").fetchone()["n"] == 0
            assert "cardinality" in {
                r["name"] for r in conn.execute("PRAGMA table_info(graph_edges)")
            }

    def test_v13_store_chains_to_v16(self, tmp_path: Path) -> None:
        # Option B: a v13 store migrates 13→14→15→16 in one open.
        # Fabricate a v13 shape (drop BOTH the v14 trust columns and the
        # v15 columns), seed, re-open, and assert (a) version == 16,
        # (b) the v14 leg ran (origin backfill landed the trust signal),
        # (c) the v15 leg ran (the `group` column is present + default).
        db_path = tmp_path / "sb.db"
        with SQLiteStore(db_path) as store:
            conn = store._require_conn()
            self._downgrade_to_v13(conn)
            conn.execute("UPDATE schemabrain_meta SET value = '13' WHERE key = 'schema_version'")
            self._seed_semantic_rows(conn)
            before = self._semantic_counts(conn)

        with SQLiteStore(db_path) as store:
            conn = store._require_conn()
            assert (
                conn.execute(
                    "SELECT value FROM schemabrain_meta WHERE key = 'schema_version'"
                ).fetchone()["value"]
                == "16"
            )
            row = conn.execute(
                'SELECT inference_method, validation_state, "group" FROM entities '
                "WHERE name = 'order'"
            ).fetchone()
            # v14 leg: `suggested` origin backfilled to (llm_suggested, applied).
            assert (row["inference_method"], row["validation_state"]) == (
                "llm_suggested",
                "applied",
            )
            # v15 leg: the `group` column exists with its default.
            assert row["group"] == "other"
            # Four-way preservation across BOTH legs — the v13→v14 leg
            # UPDATE-backfills three tables, so the chain touches strictly
            # more data than the v14→v15 ALTER-only leg; no row may drop.
            assert self._semantic_counts(conn) == before
            assert before == {
                "entities": 2,
                "metrics": 1,
                "canonical_joins": 1,
                "column_pii_tags": 1,
            }

    def test_opening_a_v12_store_raises(self, tmp_path: Path) -> None:
        # The pre-v13 cliff does NOT move under Option B — a v12 store
        # has no migration path and must still raise.
        db_path = tmp_path / "sb.db"
        store = SQLiteStore(db_path)
        store._require_conn().execute(
            "UPDATE schemabrain_meta SET value = '12' WHERE key = 'schema_version'"
        )
        store._require_conn().commit()
        store.close()
        with pytest.raises(SchemaVersionMismatchError, match=r"12.*16|16.*12"):
            SQLiteStore(db_path)

    def test_graph_tables_and_index_exist(self, tmp_path: Path) -> None:
        # The v15 graph projection tables + the edge-lookup index are
        # created by `_DDL_STATEMENTS` on a fresh store.
        with SQLiteStore(tmp_path / "sb.db") as store:
            names = {
                r["name"]
                for r in store._require_conn()
                .execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE name IN ('graph_nodes', 'graph_edges', 'idx_graph_edges_by_pair')"
                )
                .fetchall()
            }
        assert names == {"graph_nodes", "graph_edges", "idx_graph_edges_by_pair"}

    def test_migration_rolls_back_atomically_on_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A failure ANYWHERE inside `_init_schema` must roll back the
        # WHOLE migration — DDL included. Under the legacy
        # `isolation_level=''`, sqlite3 autocommits DDL, so a bare
        # `with conn:` would leave each migration ALTER committed
        # individually: a crash between two ALTERs would persist a
        # half-migrated shape with the version row behind, and the next
        # open would re-enter the migration and raise "duplicate column
        # name" — permanently bricking the store. `_init_schema` guards
        # this with an explicit BEGIN/COMMIT; this test proves it.
        db_path = tmp_path / "sb.db"
        with SQLiteStore(db_path) as store:
            conn = store._require_conn()
            self._downgrade_to_v14(conn)
            conn.execute("UPDATE schemabrain_meta SET value = '14' WHERE key = 'schema_version'")
            self._seed_semantic_rows(conn)

        # Inject a failure at the last step inside the transaction
        # (`ensure_audit_schema`), AFTER the nine migration ALTERs have
        # run. `_init_schema` imports it locally from `schemabrain.audit.
        # ddl`, so patching the module attribute is picked up on open.
        def _boom(_conn: sqlite3.Connection) -> None:
            raise RuntimeError("simulated crash mid-init")

        monkeypatch.setattr("schemabrain.audit.ddl.ensure_audit_schema", _boom)
        with pytest.raises(RuntimeError, match="simulated crash"):
            SQLiteStore(db_path)
        monkeypatch.undo()

        # The failed open must have ROLLED BACK the migration ALTERs, not
        # half-applied them: version still '14', `group` column gone.
        import gc

        gc.collect()  # drop the failed open's leaked connection
        raw = sqlite3.connect(db_path)
        raw.row_factory = sqlite3.Row
        try:
            stranded_version = raw.execute(
                "SELECT value FROM schemabrain_meta WHERE key = 'schema_version'"
            ).fetchone()["value"]
            entity_cols = {r["name"] for r in raw.execute("PRAGMA table_info(entities)")}
        finally:
            raw.close()
        assert stranded_version == "14"
        assert "group" not in entity_cols

        # And the store re-opens + migrates cleanly — no "duplicate
        # column name" (the proof the rollback was complete). Seeded rows
        # survive the now-fresh migration.
        with SQLiteStore(db_path) as store:
            conn = store._require_conn()
            assert (
                conn.execute(
                    "SELECT value FROM schemabrain_meta WHERE key = 'schema_version'"
                ).fetchone()["value"]
                == "16"
            )
            assert "group" in {r["name"] for r in conn.execute("PRAGMA table_info(entities)")}
            assert self._semantic_counts(conn) == {
                "entities": 2,
                "metrics": 1,
                "canonical_joins": 1,
                "column_pii_tags": 1,
            }

    def test_unique_index_exists(self, tmp_path: Path) -> None:
        # Without the UNIQUE index, the UPSERT's ON CONFLICT target is
        # invalid SQL — the index existence is load-bearing.
        with SQLiteStore(tmp_path / "sb.db") as store:
            row = (
                store._require_conn()
                .execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'index' AND name = 'idx_example_queries_unique'"
                )
                .fetchone()
            )
            assert row is not None


class TestWriteExampleQueriesEmpty:
    def test_empty_list_writes_zero_rows(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            written = store.write_example_queries([], source_connection_id="src")
            assert written == 0
            rows = store.list_example_queries(
                schema="public",
                table="orders",
                source_connection_id="src",
                limit=10,
            )
            assert rows == []


class TestWriteExampleQueriesInsert:
    def test_single_insert_round_trips(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            row = _eq(
                sql_text="SELECT id, total FROM public.orders WHERE user_id = $1",
                observation_count=12,
                first_seen_at=1_700_000_000,
                last_seen_at=1_700_009_000,
            )
            written = store.write_example_queries([row], source_connection_id="src")
            assert written == 1
            stored = store.list_example_queries(
                schema="public",
                table="orders",
                source_connection_id="src",
                limit=10,
            )
            assert len(stored) == 1
            assert stored[0].sql_text == row.sql_text
            assert stored[0].observation_count == 12
            assert stored[0].first_seen_at == 1_700_000_000
            assert stored[0].last_seen_at == 1_700_009_000

    def test_pii_fields_round_trip(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="users")
            row = _eq(
                schema="public",
                table="users",
                sql_text="SELECT email, phone FROM users",
                sensitivity="pii",
                pii_categories=frozenset({"contact", "online_identifier"}),
            )
            store.write_example_queries([row], source_connection_id="src")
            stored = store.list_example_queries(
                schema="public",
                table="users",
                source_connection_id="src",
                limit=10,
            )
            assert stored[0].sensitivity == "pii"
            assert stored[0].pii_categories == frozenset({"contact", "online_identifier"})

    def test_empty_pii_categories_round_trips_to_empty_string(self, tmp_path: Path) -> None:
        # Storage shape: frozenset() → '' (not 'frozenset()' or anything
        # else literal). Round-trips cleanly via _decode_pii_categories.
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            row = _eq(pii_categories=frozenset())
            store.write_example_queries([row], source_connection_id="src")
            raw = (
                store._require_conn()
                .execute("SELECT pii_categories FROM example_queries")
                .fetchone()
            )
            assert raw["pii_categories"] == ""

    def test_pii_categories_stored_in_sorted_order(self, tmp_path: Path) -> None:
        # Storage canonicalises the frozenset to a sorted CSV so two
        # callers writing the same conceptual set always produce the
        # same row — load-bearing for the UPSERT identity (the sql_text
        # is the unique-tuple element, but the PII storage must not
        # drift across re-writes that happen to compute the set in a
        # different iteration order).
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="users")
            row = _eq(
                schema="public",
                table="users",
                sensitivity="pii",
                pii_categories=frozenset({"online_identifier", "contact"}),
            )
            store.write_example_queries([row], source_connection_id="src")
            raw = (
                store._require_conn()
                .execute("SELECT pii_categories FROM example_queries")
                .fetchone()
            )
            assert raw["pii_categories"] == "contact,online_identifier"

    def test_batch_insert_returns_correct_count(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            _seed_table(store, source_id="src", schema="public", table="users")
            rows = [
                _eq(sql_text="q1", table="orders"),
                _eq(sql_text="q2", table="orders"),
                _eq(sql_text="q3", table="users"),
            ]
            written = store.write_example_queries(rows, source_connection_id="src")
            assert written == 3


class TestWriteExampleQueriesUpsert:
    """Re-writing the same (source_id, schema, table, sql_text) tuple
    UPSERTs: count and last_seen_at update, first_seen_at is preserved.
    """

    def test_rewrite_uses_max_of_old_and_new_observation_count(self, tmp_path: Path) -> None:
        # pg_stat_statements.calls is cumulative since the last reset.
        # `pg_stat_statements_reset()` between mining runs would
        # otherwise let a smaller post-reset value overwrite a higher
        # historical count. MAX(...) preserves the higher value.
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            first = _eq(
                sql_text="SELECT * FROM orders",
                observation_count=100,
            )
            store.write_example_queries([first], source_connection_id="src")
            # Second mining: lower count (simulates a reset between runs).
            second = _eq(
                sql_text="SELECT * FROM orders",
                observation_count=10,
            )
            store.write_example_queries([second], source_connection_id="src")
            stored = store.list_example_queries(
                schema="public",
                table="orders",
                source_connection_id="src",
                limit=10,
            )
            # The 100 must survive — the lower 10 doesn't clobber it.
            assert stored[0].observation_count == 100

    def test_rewrite_preserves_first_seen_at(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            first = _eq(
                sql_text="SELECT * FROM orders",
                observation_count=10,
                first_seen_at=1_700_000_000,
                last_seen_at=1_700_000_500,
            )
            store.write_example_queries([first], source_connection_id="src")
            # Second mining run sees the same SQL with a higher count
            # and a later last_seen_at. The first_seen_at the second
            # call passes is IGNORED — the original sticks.
            second = _eq(
                sql_text="SELECT * FROM orders",
                observation_count=42,
                first_seen_at=1_700_100_000,  # different but ignored
                last_seen_at=1_700_100_500,
            )
            store.write_example_queries([second], source_connection_id="src")
            stored = store.list_example_queries(
                schema="public",
                table="orders",
                source_connection_id="src",
                limit=10,
            )
            assert len(stored) == 1
            assert stored[0].observation_count == 42
            assert stored[0].first_seen_at == 1_700_000_000  # preserved
            assert stored[0].last_seen_at == 1_700_100_500  # updated

    def test_rewrite_updates_sensitivity_and_pii_categories(self, tmp_path: Path) -> None:
        # Mining initially classifies a query as `public`; after the
        # PII classifier ships, a re-mining of the same row reclassifies
        # it. The UPSERT must update those columns (not preserve them
        # like first_seen_at).
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="users")
            first = _eq(
                schema="public",
                table="users",
                sql_text="SELECT email FROM users",
                sensitivity="public",
                pii_categories=frozenset(),
            )
            store.write_example_queries([first], source_connection_id="src")
            second = _eq(
                schema="public",
                table="users",
                sql_text="SELECT email FROM users",
                sensitivity="pii",
                pii_categories=frozenset({"contact"}),
            )
            store.write_example_queries([second], source_connection_id="src")
            stored = store.list_example_queries(
                schema="public",
                table="users",
                source_connection_id="src",
                limit=10,
            )
            assert len(stored) == 1
            assert stored[0].sensitivity == "pii"
            assert stored[0].pii_categories == frozenset({"contact"})


class TestWriteExampleQueriesIsolation:
    def test_source_isolation(self, tmp_path: Path) -> None:
        # Same sql_text under two sources is two distinct rows — the
        # UPSERT conflict target includes source_connection_id.
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src_a", schema="public", table="orders")
            _seed_table(store, source_id="src_b", schema="public", table="orders")
            row = _eq(sql_text="SELECT 1 FROM orders")
            store.write_example_queries([row], source_connection_id="src_a")
            store.write_example_queries([row], source_connection_id="src_b")
            rows_a = store.list_example_queries(
                schema="public",
                table="orders",
                source_connection_id="src_a",
                limit=10,
            )
            rows_b = store.list_example_queries(
                schema="public",
                table="orders",
                source_connection_id="src_b",
                limit=10,
            )
            assert len(rows_a) == 1
            assert len(rows_b) == 1

    def test_same_sql_under_two_tables_writes_two_rows(self, tmp_path: Path) -> None:
        # Same SQL touching multiple tables (a JOIN) produces multiple
        # example_queries rows — one per touched table. The conflict
        # tuple includes table_name so this is two distinct rows.
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            _seed_table(store, source_id="src", schema="public", table="users")
            join_sql = "SELECT * FROM orders JOIN users ON users.id = orders.user_id"
            store.write_example_queries(
                [
                    _eq(table="orders", sql_text=join_sql),
                    _eq(table="users", sql_text=join_sql),
                ],
                source_connection_id="src",
            )
            orders_rows = store.list_example_queries(
                schema="public",
                table="orders",
                source_connection_id="src",
                limit=10,
            )
            users_rows = store.list_example_queries(
                schema="public",
                table="users",
                source_connection_id="src",
                limit=10,
            )
            assert len(orders_rows) == 1
            assert len(users_rows) == 1


class TestWriteExampleQueriesForeignKeyContract:
    def test_write_for_unknown_table_raises_integrity_error(self, tmp_path: Path) -> None:
        # Caller is expected to filter to indexed tables before calling
        # the writer (the mining pipeline does). But if it doesn't, the
        # FK constraint catches it.
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            row = _eq(table="nope")  # not seeded
            with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
                store.write_example_queries([row], source_connection_id="src")


class TestWriteExampleQueriesAtomicity:
    """Within a single `write_example_queries` call, either every row
    lands or none do — the writer wraps the batch in a transaction.
    """

    def test_one_bad_row_rolls_back_the_batch(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            good = _eq(table="orders", sql_text="good")
            bad = _eq(table="nope", sql_text="bad")  # FK violation
            with pytest.raises(sqlite3.IntegrityError):
                store.write_example_queries([good, bad], source_connection_id="src")
            # The good row must NOT have been committed.
            rows = store.list_example_queries(
                schema="public",
                table="orders",
                source_connection_id="src",
                limit=10,
            )
            assert rows == []


class TestWriteExampleQueriesCheckConstraintsHonoured:
    """The SQL CHECK constraints on `source` and `sensitivity` continue
    to fire under the new writer — defense-in-depth alongside the
    Python-side Literal types.
    """

    def test_python_invariant_catches_bad_pii_combination_before_sql(self, tmp_path: Path) -> None:
        # `ExampleQuery.__post_init__` enforces the cross-layer
        # invariant; the writer never sees a violating row.
        with pytest.raises(ValueError, match="at least one pii_category"):
            _eq(sensitivity="pii", pii_categories=frozenset())
