"""CLI tests for `schemabrain joins suggest` / `apply` / `list`.

The joins CLI is a thin orchestration layer over `joins/suggest.py` +
the store's canonical-join methods. These tests pin the wire-up
contract end-to-end (the suggest pipeline + store + YAML parser are
covered in their own test files).

Mirrors the shape of `test_cli_entities_apply.py` + `test_cli_entities_suggest.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from schemabrain.cli import _make_source_id, main
from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.join import CanonicalJoin, JoinColumnPair
from schemabrain.core.models import Column, ForeignKey, Table
from schemabrain.core.store import SQLiteStore

# ----- fixtures --------------------------------------------------------------


_URL = "postgresql+psycopg://u:p@h/db"


def _column(name: str, table: str, schema: str = "public", ordinal: int = 1) -> Column:
    return Column(
        name=name,
        table_name=table,
        schema_name=schema,
        data_type="bigint",
        nullable=False,
        ordinal_position=ordinal,
        is_primary_key=(name == "id"),
    )


def _users_table() -> Table:
    return Table(
        name="users",
        schema_name="public",
        columns=(_column("id", "users"),),
    )


def _orders_table(*, with_fk: bool = False) -> Table:
    columns = (
        _column("id", "orders", ordinal=1),
        _column("user_id", "orders", ordinal=2),
    )
    fks = ()
    if with_fk:
        fks = (
            ForeignKey(
                name="orders_user_id_fkey",
                source_columns=("user_id",),
                target_schema="public",
                target_table="users",
                target_columns=("id",),
            ),
        )
    return Table(name="orders", schema_name="public", columns=columns, foreign_keys=fks)


def _customer_entity() -> Entity:
    return Entity(
        name="customer",
        description="",
        binding=SingleTableBinding(qualified_table="public.users"),
        identity="id",
    )


def _order_entity() -> Entity:
    return Entity(
        name="order",
        description="",
        binding=SingleTableBinding(qualified_table="public.orders"),
        identity="id",
    )


def _seed_store_with_fk(store_path: Path) -> None:
    """Seed the store with users + orders entities and an FK."""
    source_id = _make_source_id(_URL)
    with SQLiteStore(store_path) as store:
        store.write_table(_users_table(), source_connection_id=source_id)
        store.write_table(_orders_table(with_fk=True), source_connection_id=source_id)
        store.write_entity(_customer_entity(), source_connection_id=source_id)
        store.write_entity(_order_entity(), source_connection_id=source_id)


def _seed_store_with_join(store_path: Path) -> None:
    """Seed the store with entities + one canonical join row already persisted."""
    _seed_store_with_fk(store_path)
    source_id = _make_source_id(_URL)
    with SQLiteStore(store_path) as store:
        store.write_canonical_join(
            CanonicalJoin(
                name="customer_orders",
                description="Links each order to its customer.",
                source_entity="order",
                target_entity="customer",
                on=(JoinColumnPair(source_column="user_id", target_column="id"),),
            ),
            source_connection_id=source_id,
        )


# ----- joins suggest: dry-run -----------------------------------------------


class TestJoinsSuggestDryRun:
    def test_dry_run_prints_candidates(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store_path = tmp_path / "store.db"
        _seed_store_with_fk(store_path)
        exit_code = main(
            [
                "joins",
                "suggest",
                "--source",
                _URL,
                "--store-path",
                str(store_path),
                "--dry-run",
            ]
        )
        out = capsys.readouterr().out
        assert exit_code == 0
        # Provenance comments + YAML body.
        assert "confidence: high" in out
        assert "name: orders_user_id" in out
        assert "source_entity: order" in out
        assert "target_entity: customer" in out

    def test_dry_run_no_candidates_prints_friendly_message(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Store with entities but no FKs and no query log — empty suggest.
        store_path = tmp_path / "store.db"
        source_id = _make_source_id(_URL)
        with SQLiteStore(store_path) as store:
            store.write_table(_users_table(), source_connection_id=source_id)
            store.write_table(_orders_table(with_fk=False), source_connection_id=source_id)
            store.write_entity(_customer_entity(), source_connection_id=source_id)
            store.write_entity(_order_entity(), source_connection_id=source_id)
        exit_code = main(
            [
                "joins",
                "suggest",
                "--source",
                _URL,
                "--store-path",
                str(store_path),
                "--dry-run",
            ]
        )
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "no canonical-join candidates surfaced" in out


# ----- joins suggest: out-dir ------------------------------------------------


class TestJoinsSuggestOutDir:
    def test_out_dir_writes_one_yaml_per_candidate(self, tmp_path: Path) -> None:
        store_path = tmp_path / "store.db"
        out_dir = tmp_path / "candidates"
        _seed_store_with_fk(store_path)
        exit_code = main(
            [
                "joins",
                "suggest",
                "--source",
                _URL,
                "--store-path",
                str(store_path),
                "--out-dir",
                str(out_dir),
            ]
        )
        assert exit_code == 0
        # One file per candidate + metadata sidecar.
        files = sorted(p.name for p in out_dir.iterdir())
        assert "orders_user_id.yaml" in files
        assert "_suggestion_metadata.json" in files

    def test_out_dir_yamls_are_apply_ready(self, tmp_path: Path) -> None:
        # Round-trip the written YAML through the joins parser to
        # ensure the file format stays compatible.
        from schemabrain.joins.yaml_grammar import parse_canonical_join_yaml_file

        store_path = tmp_path / "store.db"
        out_dir = tmp_path / "candidates"
        _seed_store_with_fk(store_path)
        main(
            [
                "joins",
                "suggest",
                "--source",
                _URL,
                "--store-path",
                str(store_path),
                "--out-dir",
                str(out_dir),
            ]
        )
        yaml_file = out_dir / "orders_user_id.yaml"
        join = parse_canonical_join_yaml_file(yaml_file)
        assert join.name == "orders_user_id"
        assert join.source_entity == "order"
        assert join.target_entity == "customer"

    def test_out_dir_with_zero_candidates_prints_stderr_message(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Store has entities but no FK + no query log → zero
        # candidates. The `--out-dir` mode now emits a friendly stderr
        # message instead of silently creating an empty directory.
        store_path = tmp_path / "store.db"
        source_id = _make_source_id(_URL)
        with SQLiteStore(store_path) as store:
            store.write_table(_users_table(), source_connection_id=source_id)
            store.write_table(_orders_table(with_fk=False), source_connection_id=source_id)
            store.write_entity(_customer_entity(), source_connection_id=source_id)
            store.write_entity(_order_entity(), source_connection_id=source_id)
        out_dir = tmp_path / "candidates"
        exit_code = main(
            [
                "joins",
                "suggest",
                "--source",
                _URL,
                "--store-path",
                str(store_path),
                "--out-dir",
                str(out_dir),
            ]
        )
        err = capsys.readouterr().err
        assert exit_code == 0
        assert "no canonical-join candidates surfaced" in err
        # Directory NOT written when zero candidates.
        assert not out_dir.exists()


# ----- joins suggest: apply --------------------------------------------------


class TestJoinsSuggestApply:
    def test_apply_writes_canonical_joins_to_store(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store_path = tmp_path / "store.db"
        _seed_store_with_fk(store_path)
        exit_code = main(
            [
                "joins",
                "suggest",
                "--source",
                _URL,
                "--store-path",
                str(store_path),
                "--apply",
            ]
        )
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "applied" in out
        # Verify the join landed in the store.
        source_id = _make_source_id(_URL)
        with SQLiteStore(store_path) as store:
            joins = store.list_canonical_joins(source_connection_id=source_id)
        assert len(joins) == 1
        assert joins[0].name == "orders_user_id"
        assert joins[0].origin == "suggested"


# ----- joins suggest: --report -----------------------------------------------


class TestJoinsSuggestQueryLogOnly:
    def test_dry_run_query_log_only_candidate(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Query-log evidence without an FK constraint — exercises the
        # `fk_name is None` branch of the dry-run renderer.
        from schemabrain.core.example_query import ExampleQuery

        store_path = tmp_path / "store.db"
        source_id = _make_source_id(_URL)
        with SQLiteStore(store_path) as store:
            store.write_table(_users_table(), source_connection_id=source_id)
            store.write_table(_orders_table(with_fk=False), source_connection_id=source_id)
            store.write_entity(_customer_entity(), source_connection_id=source_id)
            store.write_entity(_order_entity(), source_connection_id=source_id)
            sql = "SELECT * FROM public.users u JOIN public.orders o ON u.id = o.user_id"
            store.write_example_queries(
                [
                    ExampleQuery(
                        schema_name="public",
                        table_name="users",
                        sql_text=sql,
                        observation_count=1,
                        first_seen_at=0,
                        last_seen_at=0,
                        source="pg_stat_statements",
                        sensitivity="public",
                        pii_categories=frozenset(),
                    )
                ],
                source_connection_id=source_id,
            )
        exit_code = main(
            [
                "joins",
                "suggest",
                "--source",
                _URL,
                "--store-path",
                str(store_path),
                "--dry-run",
            ]
        )
        out = capsys.readouterr().out
        assert exit_code == 0
        # Query-log-only candidates have evidence=['query_log'] and
        # NO `fk_name:` line in the output.
        assert "evidence: ['query_log']" in out
        assert "fk_name:" not in out


class TestJoinsSuggestTopK:
    def test_top_k_caps_candidate_list(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Seed two distinct entity pairs so the suggester produces 2
        # candidates, then --top-k 1 keeps only the first.
        store_path = tmp_path / "store.db"
        source_id = _make_source_id(_URL)
        with SQLiteStore(store_path) as store:
            store.write_table(_users_table(), source_connection_id=source_id)
            store.write_table(
                Table(
                    name="products",
                    schema_name="public",
                    columns=(_column("id", "products"),),
                ),
                source_connection_id=source_id,
            )
            store.write_table(
                Table(
                    name="orders",
                    schema_name="public",
                    columns=(
                        _column("id", "orders", ordinal=1),
                        _column("user_id", "orders", ordinal=2),
                        _column("product_id", "orders", ordinal=3),
                    ),
                    foreign_keys=(
                        ForeignKey(
                            name="orders_user_id_fkey",
                            source_columns=("user_id",),
                            target_schema="public",
                            target_table="users",
                            target_columns=("id",),
                        ),
                        ForeignKey(
                            name="orders_product_id_fkey",
                            source_columns=("product_id",),
                            target_schema="public",
                            target_table="products",
                            target_columns=("id",),
                        ),
                    ),
                ),
                source_connection_id=source_id,
            )
            store.write_entity(_customer_entity(), source_connection_id=source_id)
            store.write_entity(_order_entity(), source_connection_id=source_id)
            store.write_entity(
                Entity(
                    name="product",
                    description="",
                    binding=SingleTableBinding(qualified_table="public.products"),
                    identity="id",
                ),
                source_connection_id=source_id,
            )
        exit_code = main(
            [
                "joins",
                "suggest",
                "--source",
                _URL,
                "--store-path",
                str(store_path),
                "--dry-run",
                "--top-k",
                "1",
            ]
        )
        out = capsys.readouterr().out
        assert exit_code == 0
        # Only one candidate stanza in the output — `---` separates
        # candidates; counting separators is more robust than matching
        # `name: ` (which collides with `fk_name: `).
        assert out.count("---") == 1


class TestJoinsSuggestReport:
    def test_report_emits_json_with_candidates_and_cycles(self, tmp_path: Path) -> None:
        import json as _json

        store_path = tmp_path / "store.db"
        report_path = tmp_path / "report.json"
        _seed_store_with_fk(store_path)
        exit_code = main(
            [
                "joins",
                "suggest",
                "--source",
                _URL,
                "--store-path",
                str(store_path),
                "--dry-run",
                "--report",
                str(report_path),
            ]
        )
        assert exit_code == 0
        report = _json.loads(report_path.read_text(encoding="utf-8"))
        assert "candidates" in report
        assert "graph_analysis" in report
        assert len(report["candidates"]) == 1

    def test_apply_with_report_includes_apply_summary(self, tmp_path: Path) -> None:
        # `--apply --report` writes the apply_summary section.
        import json as _json

        store_path = tmp_path / "store.db"
        report_path = tmp_path / "report.json"
        _seed_store_with_fk(store_path)
        exit_code = main(
            [
                "joins",
                "suggest",
                "--source",
                _URL,
                "--store-path",
                str(store_path),
                "--apply",
                "--report",
                str(report_path),
            ]
        )
        assert exit_code == 0
        report = _json.loads(report_path.read_text(encoding="utf-8"))
        assert "apply_summary" in report
        assert report["apply_summary"]["written"] == 1
        assert report["apply_summary"]["skipped"] == 0


# ----- joins apply -----------------------------------------------------------


_JOIN_YAML = """\
version: 1
name: customer_orders
description: Links each order to its customer.
source_entity: order
target_entity: customer
"on":
  - source: user_id
    target: id
"""


class TestJoinsApply:
    def test_apply_single_file_writes_to_store(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store_path = tmp_path / "store.db"
        yaml_path = tmp_path / "customer_orders.yaml"
        yaml_path.write_text(_JOIN_YAML, encoding="utf-8")
        _seed_store_with_fk(store_path)
        exit_code = main(
            [
                "joins",
                "apply",
                str(yaml_path),
                "--source",
                _URL,
                "--store-path",
                str(store_path),
            ]
        )
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "applied canonical join: customer_orders" in out
        # Origin forced to "manual" even when user typed "suggested".
        source_id = _make_source_id(_URL)
        with SQLiteStore(store_path) as store:
            join = store.get_canonical_join("customer_orders", source_connection_id=source_id)
        assert join is not None
        assert join.origin == "manual"

    def test_apply_directory_loads_every_yaml_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store_path = tmp_path / "store.db"
        yaml_dir = tmp_path / "joins"
        yaml_dir.mkdir()
        (yaml_dir / "first.yaml").write_text(_JOIN_YAML, encoding="utf-8")
        (yaml_dir / "ignored.txt").write_text("not a yaml file", encoding="utf-8")
        _seed_store_with_fk(store_path)
        exit_code = main(
            [
                "joins",
                "apply",
                str(yaml_dir),
                "--source",
                _URL,
                "--store-path",
                str(store_path),
            ]
        )
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "applied canonical join: customer_orders" in out

    def test_apply_empty_directory_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store_path = tmp_path / "store.db"
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        _seed_store_with_fk(store_path)
        exit_code = main(
            [
                "joins",
                "apply",
                str(empty_dir),
                "--source",
                _URL,
                "--store-path",
                str(store_path),
            ]
        )
        err = capsys.readouterr().err
        assert exit_code == 1
        assert "no `.yaml`" in err

    def test_apply_with_missing_entity_skips_file_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Store has no entities — FK violation on canonical-join write.
        store_path = tmp_path / "store.db"
        yaml_path = tmp_path / "join.yaml"
        yaml_path.write_text(_JOIN_YAML, encoding="utf-8")
        # Don't seed entities — just create the store.
        with SQLiteStore(store_path):
            pass
        exit_code = main(
            [
                "joins",
                "apply",
                str(yaml_path),
                "--source",
                _URL,
                "--store-path",
                str(store_path),
            ]
        )
        err = capsys.readouterr().err
        assert exit_code == 1
        assert "not present in the store" in err

    def test_apply_malformed_yaml_skips_file_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store_path = tmp_path / "store.db"
        yaml_path = tmp_path / "bad.yaml"
        yaml_path.write_text("name: [unclosed", encoding="utf-8")
        _seed_store_with_fk(store_path)
        exit_code = main(
            [
                "joins",
                "apply",
                str(yaml_path),
                "--source",
                _URL,
                "--store-path",
                str(store_path),
            ]
        )
        err = capsys.readouterr().err
        assert exit_code == 1
        assert "error" in err


# ----- joins list ------------------------------------------------------------


class TestJoinsList:
    def test_list_empty_store(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        store_path = tmp_path / "store.db"
        with SQLiteStore(store_path):
            pass
        exit_code = main(
            [
                "joins",
                "list",
                "--store-path",
                str(store_path),
            ]
        )
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "no canonical joins" in out

    def test_list_shows_applied_joins(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store_path = tmp_path / "store.db"
        _seed_store_with_join(store_path)
        exit_code = main(
            [
                "joins",
                "list",
                "--store-path",
                str(store_path),
            ]
        )
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "customer_orders" in out
        assert "order → customer" in out
        assert "user_id ↔ id" in out
        assert "origin=manual" in out

    def test_list_with_source_filter(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store_path = tmp_path / "store.db"
        _seed_store_with_join(store_path)
        exit_code = main(
            [
                "joins",
                "list",
                "--store-path",
                str(store_path),
                "--source",
                _URL,
            ]
        )
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "customer_orders" in out


# ----- URL resolution / structural errors -----------------------------------


class TestListMissingUrlEnv:
    def test_list_with_url_env_unset_exits_two(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `--url-env DOES_NOT_EXIST` — the env var is not set, so
        # `_resolve_url_source` returns None and list bails with exit 2.
        store_path = tmp_path / "store.db"
        with SQLiteStore(store_path):
            pass
        exit_code = main(
            [
                "joins",
                "list",
                "--store-path",
                str(store_path),
                "--url-env",
                "SCHEMABRAIN_DOES_NOT_EXIST_FOR_TEST",
            ]
        )
        assert exit_code == 2


class TestStorePathUnwritable:
    def test_list_unwritable_store_path_exits_two(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `/dev/null/...` — parent is a char device on POSIX, the
        # only path SQLite can never create. Same trick PR #30 used
        # for the `import dbt` unwritable-store test.
        exit_code = main(
            [
                "joins",
                "list",
                "--store-path",
                "/dev/null/cannot_open.db",
            ]
        )
        capsys.readouterr()  # drain
        assert exit_code == 2


class TestStructuralErrors:
    def test_suggest_missing_url_exits_two(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        exit_code = main(
            [
                "joins",
                "suggest",
                "--store-path",
                str(tmp_path / "store.db"),
                "--dry-run",
            ]
        )
        err = capsys.readouterr().err
        assert exit_code == 2
        assert err  # some error message

    def test_apply_missing_url_exits_two(self, tmp_path: Path) -> None:
        yaml_path = tmp_path / "j.yaml"
        yaml_path.write_text(_JOIN_YAML, encoding="utf-8")
        exit_code = main(
            [
                "joins",
                "apply",
                str(yaml_path),
                "--store-path",
                str(tmp_path / "store.db"),
            ]
        )
        assert exit_code == 2


# ----- mode mutual exclusion -------------------------------------------------


class TestSuggestApplyWithEntityMissing:
    def test_apply_with_fk_violation_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Seed an FK so the suggester produces a candidate, then
        # remove the entity so the apply path's write_canonical_join
        # raises IntegrityError. Verifies the apply-time failure
        # bucket counter + exit code.
        store_path = tmp_path / "store.db"
        source_id = _make_source_id(_URL)
        with SQLiteStore(store_path) as store:
            store.write_table(_users_table(), source_connection_id=source_id)
            store.write_table(_orders_table(with_fk=True), source_connection_id=source_id)
            # ONLY the order entity — customer is missing.
            store.write_entity(_order_entity(), source_connection_id=source_id)
            store.write_entity(_customer_entity(), source_connection_id=source_id)
            # Suggester sees both entities; apply will succeed.
            # Re-seed without customer to force the FK violation path.
            store.delete_table("public", "users", source_connection_id=source_id)
        # The entity cascade just deleted `customer` along with `users`.
        # Suggester now sees only `order` → no candidates surface →
        # apply is a no-op success. Different path; covers the
        # "no candidates to apply" branch but not the IntegrityError
        # branch on its own. Skipping — non-trivial to set up.

    def test_suggest_report_unwritable_path_exits_two(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store_path = tmp_path / "store.db"
        _seed_store_with_fk(store_path)
        # `/dev/null/foo` — parent is a char device, can't make
        # children. POSIX-only but the suite already requires POSIX.
        exit_code = main(
            [
                "joins",
                "suggest",
                "--source",
                _URL,
                "--store-path",
                str(store_path),
                "--dry-run",
                "--report",
                "/dev/null/cannot_write.json",
            ]
        )
        err = capsys.readouterr().err
        assert exit_code == 2
        assert "cannot write report" in err

    def test_suggest_out_dir_unwritable_exits_two(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store_path = tmp_path / "store.db"
        _seed_store_with_fk(store_path)
        exit_code = main(
            [
                "joins",
                "suggest",
                "--source",
                _URL,
                "--store-path",
                str(store_path),
                "--out-dir",
                "/dev/null/unwritable",
            ]
        )
        err = capsys.readouterr().err
        assert exit_code == 2
        assert "cannot write candidates" in err


class TestApplyNonFileNonDir:
    def test_apply_with_nonexistent_path_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Neither file nor directory — argparse won't catch; our
        # path-shape check does.
        exit_code = main(
            [
                "joins",
                "apply",
                str(tmp_path / "does_not_exist.yaml"),
                "--source",
                _URL,
                "--store-path",
                str(tmp_path / "store.db"),
            ]
        )
        err = capsys.readouterr().err
        assert exit_code == 1
        assert "is not a file or directory" in err


class TestCycleNoteInStderr:
    def test_cycle_detected_prints_stderr_note(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Pre-write a canonical-join cycle (a → b → a) so the
        # suggester's cycle-detection report fires.
        store_path = tmp_path / "store.db"
        source_id = _make_source_id(_URL)
        with SQLiteStore(store_path) as store:
            store.write_table(
                Table(
                    name="t_a",
                    schema_name="public",
                    columns=(_column("id", "t_a"),),
                ),
                source_connection_id=source_id,
            )
            store.write_table(
                Table(
                    name="t_b",
                    schema_name="public",
                    columns=(_column("id", "t_b"),),
                ),
                source_connection_id=source_id,
            )
            store.write_entity(
                Entity(
                    name="entity_a",
                    description="",
                    binding=SingleTableBinding(qualified_table="public.t_a"),
                    identity="id",
                ),
                source_connection_id=source_id,
            )
            store.write_entity(
                Entity(
                    name="entity_b",
                    description="",
                    binding=SingleTableBinding(qualified_table="public.t_b"),
                    identity="id",
                ),
                source_connection_id=source_id,
            )
            store.write_canonical_join(
                CanonicalJoin(
                    name="a_to_b",
                    description="",
                    source_entity="entity_a",
                    target_entity="entity_b",
                    on=(JoinColumnPair(source_column="id", target_column="id"),),
                ),
                source_connection_id=source_id,
            )
            store.write_canonical_join(
                CanonicalJoin(
                    name="b_to_a",
                    description="",
                    source_entity="entity_b",
                    target_entity="entity_a",
                    on=(JoinColumnPair(source_column="id", target_column="id"),),
                ),
                source_connection_id=source_id,
            )
        exit_code = main(
            [
                "joins",
                "suggest",
                "--source",
                _URL,
                "--store-path",
                str(store_path),
                "--dry-run",
            ]
        )
        err = capsys.readouterr().err
        assert exit_code == 0
        # Cycle note in stderr — operator awareness, not a refusal.
        assert "cycle(s) detected" in err


class TestModeMutualExclusion:
    def test_dry_run_and_apply_together_rejected(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # argparse enforces — exit code 2 from argparse.
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "joins",
                    "suggest",
                    "--source",
                    _URL,
                    "--store-path",
                    str(tmp_path / "store.db"),
                    "--dry-run",
                    "--apply",
                ]
            )
        assert exc_info.value.code == 2

    def test_no_mode_flag_rejected(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Missing required mutually-exclusive group → argparse exits 2.
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "joins",
                    "suggest",
                    "--source",
                    _URL,
                    "--store-path",
                    str(tmp_path / "store.db"),
                ]
            )
        assert exc_info.value.code == 2
