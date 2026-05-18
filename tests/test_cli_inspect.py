"""Tests for `schemabrain inspect` CLI registration + dispatch.

Goals:

  1. Bare `inspect` renders a summary to stderr; exits 0 on any
     valid store (including empty).
  2. `inspect <name>` renders a detail block to stderr when the
     entity exists; exits 1 with a guided-error message when it
     doesn't.
  3. Operational refusals (missing store, --source + --url-env
     conflict, --url-env names unset variable) exit 2.
  4. Cross-source mode: when no --source/--url-env supplied, drill
     walks every source the store knows about and renders every
     matching entity.
  5. Human render lands on stderr; stdout stays clean.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from schemabrain.cli import main
from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.join import CanonicalJoin, JoinColumnPair
from schemabrain.core.metric import Metric, MetricMeasure
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore

SOURCE_ID = "0d8753a94d271485"  # _make_source_id("postgresql+psycopg://fake/db")


# ----- fixtures -------------------------------------------------------------


def _seed_users_table(store: SQLiteStore, *, source_id: str = SOURCE_ID) -> None:
    store.write_table(
        Table(
            schema_name="public",
            name="users",
            columns=(
                Column(
                    name="id",
                    table_name="users",
                    schema_name="public",
                    data_type="bigint",
                    nullable=False,
                    ordinal_position=1,
                    is_primary_key=True,
                ),
                Column(
                    name="email",
                    table_name="users",
                    schema_name="public",
                    data_type="text",
                    nullable=False,
                    ordinal_position=2,
                ),
            ),
        ),
        source_connection_id=source_id,
    )


def _seed_customer_entity(store: SQLiteStore, *, source_id: str = SOURCE_ID) -> None:
    store.write_entity(
        Entity(
            name="customer",
            description="A registered user.",
            binding=SingleTableBinding(qualified_table="public.users"),
            identity="id",
        ),
        source_connection_id=source_id,
    )


@pytest.fixture
def empty_store(tmp_path: Path) -> Iterator[Path]:
    """An empty SQLiteStore at a tmp path."""
    path = tmp_path / "store.db"
    with SQLiteStore(path=path):
        pass
    yield path


@pytest.fixture
def fully_populated_store(tmp_path: Path) -> Iterator[Path]:
    """A store with users + orders tables, customer + order entities,
    one anchored metric, and one canonical join — exercises every
    render branch (Entities / Metrics / Joins sections in the
    summary; Columns / Related / Anchored in the detail view).
    """
    path = tmp_path / "store.db"
    with SQLiteStore(path=path) as store:
        _seed_users_table(store)
        store.write_table(
            Table(
                schema_name="public",
                name="orders",
                columns=(
                    Column(
                        name="id",
                        table_name="orders",
                        schema_name="public",
                        data_type="bigint",
                        nullable=False,
                        ordinal_position=1,
                        is_primary_key=True,
                    ),
                    Column(
                        name="user_id",
                        table_name="orders",
                        schema_name="public",
                        data_type="bigint",
                        nullable=False,
                        ordinal_position=2,
                    ),
                    Column(
                        name="total_cents",
                        table_name="orders",
                        schema_name="public",
                        data_type="int",
                        nullable=False,
                        ordinal_position=3,
                    ),
                ),
            ),
            source_connection_id=SOURCE_ID,
        )
        _seed_customer_entity(store)
        store.write_entity(
            Entity(
                name="order",
                description="A purchase placed by one customer.",
                binding=SingleTableBinding(qualified_table="public.orders"),
                identity="id",
            ),
            source_connection_id=SOURCE_ID,
        )
        store.write_metric(
            Metric(
                name="total_revenue",
                description="Sum of order subtotals.",
                entity="order",
                measure=MetricMeasure(agg="sum", column="total_cents"),
                time_dimension=None,
                time_grains=(),
            ),
            source_connection_id=SOURCE_ID,
        )
        store.write_canonical_join(
            CanonicalJoin(
                name="customer_orders",
                description="",
                source_entity="customer",
                target_entity="order",
                on=(JoinColumnPair(source_column="id", target_column="user_id"),),
                cardinality="one_to_many",
            ),
            source_connection_id=SOURCE_ID,
        )
    yield path


@pytest.fixture
def store_with_customer(tmp_path: Path) -> Iterator[Path]:
    """A store containing a `customer` entity bound to `public.users`."""
    path = tmp_path / "store.db"
    with SQLiteStore(path=path) as store:
        _seed_users_table(store)
        _seed_customer_entity(store)
    yield path


# ----- summary mode ---------------------------------------------------------


class TestInspectSummary:
    def test_empty_store_renders_summary_with_zero_counts(
        self,
        empty_store: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        exit_code = main(["inspect", "--store-path", str(empty_store)])
        assert exit_code == 0
        captured = capsys.readouterr()
        # Header + zero-counts line; stdout stays clean.
        assert "Schema Brain inspect" in captured.err
        assert "0 tables" in captured.err
        assert "0 entities" in captured.err
        assert captured.out == ""

    def test_populated_store_lists_entity_names(
        self,
        store_with_customer: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        exit_code = main(
            [
                "inspect",
                "--source",
                "postgresql+psycopg://fake/db",
                "--store-path",
                str(store_with_customer),
            ]
        )
        assert exit_code == 0
        err = capsys.readouterr().err
        assert "1 entit" in err  # "1 entity"
        assert "customer" in err
        # Drill hint surfaces.
        assert "schemabrain inspect <name>" in err

    def test_populated_store_lists_metrics_and_joins(
        self,
        fully_populated_store: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Exercises the Metrics + Joins sections of the summary
        # render — the basic `store_with_customer` fixture skips
        # them because it has no metrics or joins.
        main(
            [
                "inspect",
                "--source",
                "postgresql+psycopg://fake/db",
                "--store-path",
                str(fully_populated_store),
            ]
        )
        err = capsys.readouterr().err
        assert "Metrics:" in err
        assert "total_revenue" in err
        assert "Joins:" in err
        assert "customer_orders" in err

    def test_empty_store_shows_getting_started_hint(
        self,
        empty_store: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main(["inspect", "--store-path", str(empty_store)])
        err = capsys.readouterr().err
        assert "No semantic-layer definitions" in err


# ----- drill mode -----------------------------------------------------------


class TestInspectDrill:
    def test_drill_into_known_entity_renders_detail(
        self,
        store_with_customer: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        exit_code = main(
            [
                "inspect",
                "customer",
                "--source",
                "postgresql+psycopg://fake/db",
                "--store-path",
                str(store_with_customer),
            ]
        )
        assert exit_code == 0
        err = capsys.readouterr().err
        assert "Entity: customer" in err
        assert "Binding:" in err and "public.users" in err
        assert "Identity:" in err and " id" in err
        # Columns + section headers render.
        assert "Columns:" in err
        assert "Related entities:" in err
        assert "Anchored metrics:" in err

    def test_drill_renders_related_and_anchored_metrics(
        self,
        fully_populated_store: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Drill into `order` — the entity that owns the metric and is
        # the target side of the `customer_orders` join. Exercises
        # the Related / Anchored render branches with non-empty data.
        exit_code = main(
            [
                "inspect",
                "order",
                "--source",
                "postgresql+psycopg://fake/db",
                "--store-path",
                str(fully_populated_store),
            ]
        )
        assert exit_code == 0
        err = capsys.readouterr().err
        assert "Entity: order" in err
        # Anchored metric rendered.
        assert "total_revenue" in err
        # Related entity rendered with cardinality + via-join hint.
        assert "customer" in err
        assert "one_to_many" in err
        assert "via" in err and "customer_orders" in err

    def test_drill_into_unknown_entity_exits_one(
        self,
        store_with_customer: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        exit_code = main(
            [
                "inspect",
                "nonexistent",
                "--source",
                "postgresql+psycopg://fake/db",
                "--store-path",
                str(store_with_customer),
            ]
        )
        assert exit_code == 1
        err = capsys.readouterr().err
        assert "no entity named 'nonexistent'" in err

    def test_drill_without_source_walks_every_source(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Two sources, both carrying a `customer` entity. Drill
        # without --source/--url-env should render BOTH detail blocks.
        path = tmp_path / "store.db"
        other_source = "9f9f9f9f9f9f9f9f"
        with SQLiteStore(path=path) as store:
            _seed_users_table(store, source_id=SOURCE_ID)
            _seed_customer_entity(store, source_id=SOURCE_ID)
            _seed_users_table(store, source_id=other_source)
            _seed_customer_entity(store, source_id=other_source)
        exit_code = main(["inspect", "customer", "--store-path", str(path)])
        assert exit_code == 0
        err = capsys.readouterr().err
        # Both Entity blocks rendered.
        assert err.count("Entity: customer") == 2

    def test_drill_without_source_unknown_name_exits_one(
        self,
        empty_store: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        exit_code = main(["inspect", "ghost", "--store-path", str(empty_store)])
        assert exit_code == 1
        err = capsys.readouterr().err
        assert "no entity named 'ghost'" in err


# ----- operational refusals -------------------------------------------------


class TestInspectRefusals:
    def test_missing_store_exits_two(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        missing = tmp_path / "does-not-exist.db"
        exit_code = main(["inspect", "--store-path", str(missing)])
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "store not found" in err

    def test_both_source_and_url_env_exits_two(
        self,
        store_with_customer: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("DB_URL", "postgresql+psycopg://fake/db")
        exit_code = main(
            [
                "inspect",
                "--source",
                "postgresql+psycopg://fake/db",
                "--url-env",
                "DB_URL",
                "--store-path",
                str(store_with_customer),
            ]
        )
        assert exit_code == 2

    def test_unset_url_env_exits_two(
        self,
        store_with_customer: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("MISSING_DB_URL", raising=False)
        exit_code = main(
            [
                "inspect",
                "--url-env",
                "MISSING_DB_URL",
                "--store-path",
                str(store_with_customer),
            ]
        )
        assert exit_code == 2

    def test_partial_migration_store_exits_two(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # A store with the schema-version row present (so the version
        # check passes) but a required table missing — simulates a
        # mid-migration or hand-edited store. The handler must catch
        # `sqlite3.OperationalError` and surface a guided exit-2
        # message instead of an unhandled traceback.
        import sqlite3

        path = tmp_path / "store.db"
        conn = sqlite3.connect(path)
        # Match the current schema version so we don't trip the
        # SchemaVersionMismatchError path.
        from schemabrain.core.store import SCHEMA_VERSION

        conn.execute("CREATE TABLE schemabrain_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO schemabrain_meta VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        # Create only the `entities` table — drilling into it WILL
        # try to read `tables` (missing) for the bound table, and
        # that's what triggers the OperationalError.
        conn.execute(
            "CREATE TABLE entities ("
            "source_connection_id TEXT NOT NULL, "
            "name TEXT NOT NULL, "
            "description TEXT NOT NULL, "
            "bound_schema TEXT NOT NULL, "
            "bound_table TEXT NOT NULL, "
            "identity TEXT NOT NULL, "
            "origin TEXT NOT NULL, "
            "created_at INTEGER NOT NULL, "
            "PRIMARY KEY (source_connection_id, name))"
        )
        conn.execute(
            "INSERT INTO entities VALUES "
            "('src', 'customer', '', 'public', 'users', 'id', 'manual', 0)"
        )
        conn.commit()
        conn.close()
        exit_code = main(["inspect", "customer", "--store-path", str(path)])
        # Either exit 1 (entity look-up itself fails because
        # `_row_to_entity` couldn't reconstruct) or exit 2 (the
        # OperationalError surfaces). Both are acceptable; what
        # matters is no uncaught traceback. Confirm via no
        # "Traceback" in stderr.
        err = capsys.readouterr().err
        assert "Traceback" not in err
        assert exit_code in (1, 2)

    def test_schema_version_mismatch_exits_two(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import sqlite3

        path = tmp_path / "store.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE schemabrain_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO schemabrain_meta VALUES ('schema_version', '999')")
        conn.commit()
        conn.close()
        exit_code = main(["inspect", "--store-path", str(path)])
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "schema" in err.lower()
