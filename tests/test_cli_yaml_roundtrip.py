"""Tests for the store ↔ YAML round-trip CLI workflow.

Covers the four new CLI surfaces:

  * `entities export <name>` / `metrics export <name>` / `joins export <name>`
    — single-row store → YAML, stdout default, `--out PATH` writes to disk
  * `entities export-all --dir PATH` (and metric / join mirrors)
    — bulk export, one file per row, refuses on existing files + collisions
  * `schemabrain apply [PROJECT_DIR]`
    — walk a project tree (entities/, metrics/, joins/) and apply each
  * `schemabrain diff [PROJECT_DIR]`
    — drift check, CI-friendly exit codes

The cross-source disambiguation tests use stable, hand-derived source
ids so re-runs are deterministic.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from schemabrain.cli import _make_source_id
from schemabrain.cli import main as cli_main
from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.join import CanonicalJoin, JoinColumnPair
from schemabrain.core.metric import Metric, MetricMeasure
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore

_PG_URL = "postgresql+psycopg://u:p@localhost:5432/db"
_PG_URL_B = "postgresql+psycopg://u:p@localhost:5432/db_b"


@pytest.fixture
def populated_store(tmp_path: Path) -> Iterator[Path]:
    """Build a store with one source containing a users table + a
    customer entity + a customer_count metric + (separately) an
    orders table for the joins tests."""
    store_path = tmp_path / "store.db"
    sid = _make_source_id(_PG_URL)
    store = SQLiteStore(store_path)
    try:
        for tname, cols in (
            ("users", [("id", True)]),
            ("orders", [("id", True), ("user_id", False)]),
        ):
            store.write_table(
                Table(
                    name=tname,
                    schema_name="public",
                    columns=tuple(
                        Column(
                            name=c,
                            table_name=tname,
                            schema_name="public",
                            data_type="bigint",
                            nullable=False,
                            ordinal_position=i + 1,
                            is_primary_key=is_pk,
                        )
                        for i, (c, is_pk) in enumerate(cols)
                    ),
                ),
                source_connection_id=sid,
            )
        store.write_entity(
            Entity(
                name="customer",
                description="A buyer",
                binding=SingleTableBinding(qualified_table="public.users"),
                identity="id",
            ),
            source_connection_id=sid,
        )
        store.write_entity(
            Entity(
                name="order",
                description="",
                binding=SingleTableBinding(qualified_table="public.orders"),
                identity="id",
            ),
            source_connection_id=sid,
        )
        store.write_metric(
            Metric(
                name="customer_count",
                description="",
                entity="customer",
                measure=MetricMeasure(agg="count", column="id"),
                time_dimension=None,
                time_grains=(),
            ),
            source_connection_id=sid,
        )
        store.write_canonical_join(
            CanonicalJoin(
                name="order_to_customer",
                description="",
                source_entity="order",
                target_entity="customer",
                on=(JoinColumnPair(source_column="user_id", target_column="id"),),
                cardinality="many_to_one",
            ),
            source_connection_id=sid,
        )
    finally:
        store.close()
    yield store_path


class TestEntitiesExportSingle:
    def test_export_to_stdout(
        self, populated_store: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cli_main(["entities", "export", "customer", "--store-path", str(populated_store)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "name: customer" in out
        assert "single_table: public.users" in out

    def test_export_to_file(
        self, populated_store: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out_file = tmp_path / "out.yaml"
        rc = cli_main(
            [
                "entities",
                "export",
                "customer",
                "--out",
                str(out_file),
                "--store-path",
                str(populated_store),
            ]
        )
        assert rc == 0
        # File received the body; stdout was clean.
        assert "name: customer" in out_file.read_text()
        captured = capsys.readouterr()
        assert captured.out == ""
        # Confirmation on stderr.
        assert "wrote" in captured.err

    def test_unknown_name_exits_one(
        self, populated_store: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cli_main(["entities", "export", "nope", "--store-path", str(populated_store)])
        assert rc == 1
        assert "no entity named" in capsys.readouterr().err

    def test_cross_source_collision_exits_two(
        self, populated_store: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Two sources holding entities with the same name → diff
        cannot disambiguate without --source/--url-env. The handler
        must error with exit 2 (operational refusal), not 1 (not
        found), so CI scripts can branch correctly.
        """
        # Add a second source with the SAME entity name.
        sid_b = _make_source_id(_PG_URL_B)
        store = SQLiteStore(populated_store)
        try:
            store.write_table(
                Table(
                    name="users",
                    schema_name="public",
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
                    ),
                ),
                source_connection_id=sid_b,
            )
            store.write_entity(
                Entity(
                    name="customer",
                    description="A different buyer",
                    binding=SingleTableBinding(qualified_table="public.users"),
                    identity="id",
                ),
                source_connection_id=sid_b,
            )
        finally:
            store.close()

        rc = cli_main(["entities", "export", "customer", "--store-path", str(populated_store)])
        assert rc == 2
        err = capsys.readouterr().err
        assert "defined in 2 sources" in err
        assert "--source/--url-env" in err


class TestEntitiesExportAll:
    def test_writes_one_yaml_per_entity(self, populated_store: Path, tmp_path: Path) -> None:
        out = tmp_path / "ents"
        rc = cli_main(
            [
                "entities",
                "export-all",
                "--dir",
                str(out),
                "--store-path",
                str(populated_store),
            ]
        )
        assert rc == 0
        filenames = sorted(p.name for p in out.iterdir())
        assert filenames == ["customer.yaml", "order.yaml"]

    def test_refuses_to_overwrite_existing_files(
        self, populated_store: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = tmp_path / "ents"
        out.mkdir()
        (out / "customer.yaml").write_text("hand-edited content")
        rc = cli_main(
            [
                "entities",
                "export-all",
                "--dir",
                str(out),
                "--store-path",
                str(populated_store),
            ]
        )
        assert rc == 2
        err = capsys.readouterr().err
        assert "refusing to overwrite" in err
        assert "customer.yaml" in err
        # Hand-edited file was NOT touched.
        assert (out / "customer.yaml").read_text() == "hand-edited content"

    def test_empty_store_succeeds(self, tmp_path: Path) -> None:
        empty_store = tmp_path / "empty.db"
        SQLiteStore(empty_store).close()
        rc = cli_main(
            [
                "entities",
                "export-all",
                "--dir",
                str(tmp_path / "out"),
                "--store-path",
                str(empty_store),
            ]
        )
        assert rc == 0

    def test_cross_source_name_collision_refused(
        self, populated_store: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Two sources, both with a `customer` entity.
        sid_b = _make_source_id(_PG_URL_B)
        store = SQLiteStore(populated_store)
        try:
            store.write_table(
                Table(
                    name="users",
                    schema_name="public",
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
                    ),
                ),
                source_connection_id=sid_b,
            )
            store.write_entity(
                Entity(
                    name="customer",
                    description="A different buyer",
                    binding=SingleTableBinding(qualified_table="public.users"),
                    identity="id",
                ),
                source_connection_id=sid_b,
            )
        finally:
            store.close()

        rc = cli_main(
            [
                "entities",
                "export-all",
                "--dir",
                str(tmp_path / "out"),
                "--store-path",
                str(populated_store),
            ]
        )
        assert rc == 2
        assert "collide across sources" in capsys.readouterr().err


class TestMetricsAndJoinsExport:
    """The metrics + joins export commands share the helper layer
    with entities; spot-check both surfaces work without re-running
    the full matrix above.
    """

    def test_metric_export_to_stdout(
        self, populated_store: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cli_main(["metrics", "export", "customer_count", "--store-path", str(populated_store)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "name: customer_count" in out
        assert "agg: count" in out

    def test_join_export_to_stdout(
        self, populated_store: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cli_main(
            ["joins", "export", "order_to_customer", "--store-path", str(populated_store)]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "name: order_to_customer" in out
        # The `"on":` key is force-quoted in the output so a re-parse
        # under YAML 1.1 boolean coercion does not turn it into True.
        assert '"on":' in out

    def test_metric_export_all_writes_one_per_metric(
        self, populated_store: Path, tmp_path: Path
    ) -> None:
        out = tmp_path / "metrics"
        rc = cli_main(
            [
                "metrics",
                "export-all",
                "--dir",
                str(out),
                "--store-path",
                str(populated_store),
            ]
        )
        assert rc == 0
        assert (out / "customer_count.yaml").exists()

    def test_join_export_all_writes_one_per_join(
        self, populated_store: Path, tmp_path: Path
    ) -> None:
        out = tmp_path / "joins"
        rc = cli_main(
            ["joins", "export-all", "--dir", str(out), "--store-path", str(populated_store)]
        )
        assert rc == 0
        assert (out / "order_to_customer.yaml").exists()


class TestApplyAndDiffRoundTrip:
    """End-to-end: export everything to a project tree, edit a YAML,
    diff (shows drift), apply (re-applies), diff again (in sync).

    Verifies the full export → edit → diff → apply → diff loop the
    workflow is designed around.
    """

    def _build_project_tree(self, populated_store: Path, project_dir: Path) -> None:
        for kind in ("entities", "metrics", "joins"):
            rc = cli_main(
                [
                    kind,
                    "export-all",
                    "--dir",
                    str(project_dir / kind),
                    "--store-path",
                    str(populated_store),
                ]
            )
            assert rc == 0

    def test_diff_reports_in_sync_after_export(
        self,
        populated_store: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        proj = tmp_path / "proj"
        self._build_project_tree(populated_store, proj)
        monkeypatch.setenv("DBURL", _PG_URL)
        rc = cli_main(
            [
                "diff",
                str(proj),
                "--url-env",
                "DBURL",
                "--store-path",
                str(populated_store),
            ]
        )
        assert rc == 0
        assert "in sync" in capsys.readouterr().out

    def test_diff_reports_drift_after_edit(
        self,
        populated_store: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        proj = tmp_path / "proj"
        self._build_project_tree(populated_store, proj)
        # Edit one entity's description on disk; diff should flag it.
        ent_path = proj / "entities" / "customer.yaml"
        ent_path.write_text(ent_path.read_text().replace("A buyer", "A regular shopper"))
        monkeypatch.setenv("DBURL", _PG_URL)
        rc = cli_main(
            [
                "diff",
                str(proj),
                "--url-env",
                "DBURL",
                "--store-path",
                str(populated_store),
            ]
        )
        assert rc == 1
        out = capsys.readouterr().out
        assert "value mismatch" in out
        assert "customer" in out
        # Hint mentions the right CLI family (entities, not entitys).
        assert "entities export customer" in out

    def test_apply_re_syncs_after_edit(
        self,
        populated_store: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        proj = tmp_path / "proj"
        self._build_project_tree(populated_store, proj)
        ent_path = proj / "entities" / "customer.yaml"
        ent_path.write_text(ent_path.read_text().replace("A buyer", "A regular shopper"))
        monkeypatch.setenv("DBURL", _PG_URL)
        # Apply the edit.
        rc = cli_main(
            [
                "apply",
                str(proj),
                "--url-env",
                "DBURL",
                "--store-path",
                str(populated_store),
            ]
        )
        assert rc == 0
        # Diff is back to in-sync.
        rc = cli_main(
            [
                "diff",
                str(proj),
                "--url-env",
                "DBURL",
                "--store-path",
                str(populated_store),
            ]
        )
        assert rc == 0

    def test_apply_missing_subdirs_is_clean(
        self,
        populated_store: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A project tree with only entities/ (no metrics/, no joins/)
        applies cleanly — the walker skips missing subdirs without
        complaint. Otherwise an operator who only curates entities
        would see two spurious 'skipped' errors per apply.
        """
        proj = tmp_path / "proj-partial"
        (proj / "entities").mkdir(parents=True)
        # Empty entities dir — nothing to apply.
        monkeypatch.setenv("DBURL", _PG_URL)
        rc = cli_main(
            [
                "apply",
                str(proj),
                "--url-env",
                "DBURL",
                "--store-path",
                str(populated_store),
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "metrics/: skipped (subdirectory missing)" in out
        assert "joins/: skipped (subdirectory missing)" in out
        assert "entities/: skipped (no YAML files)" in out

    def test_apply_missing_project_dir_returns_two(
        self,
        tmp_path: Path,
        populated_store: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("DBURL", _PG_URL)
        rc = cli_main(
            [
                "apply",
                str(tmp_path / "no-such-dir"),
                "--url-env",
                "DBURL",
                "--store-path",
                str(populated_store),
            ]
        )
        assert rc == 2
        assert "not found" in capsys.readouterr().err

    def test_diff_only_on_disk_drift(
        self,
        populated_store: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A YAML for a not-yet-applied entity shows up as only-on-disk
        drift. Exit 1 + the message hints at apply.
        """
        proj = tmp_path / "proj"
        self._build_project_tree(populated_store, proj)
        # Author a NEW entity YAML directly on disk; never applied.
        (proj / "entities" / "vendor.yaml").write_text(
            "version: 1\nname: vendor\nbinding:\n  single_table: public.users\nidentity: id\n"
        )
        monkeypatch.setenv("DBURL", _PG_URL)
        rc = cli_main(
            [
                "diff",
                str(proj),
                "--url-env",
                "DBURL",
                "--store-path",
                str(populated_store),
            ]
        )
        assert rc == 1
        out = capsys.readouterr().out
        assert "vendor: only on disk" in out

    def test_diff_only_in_store_drift(
        self,
        populated_store: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An entity in the store with no corresponding YAML file is
        only-in-store drift — informational, NOT auto-deleted by apply.
        """
        proj = tmp_path / "proj"
        self._build_project_tree(populated_store, proj)
        # Delete one of the YAML files.
        (proj / "entities" / "order.yaml").unlink()
        monkeypatch.setenv("DBURL", _PG_URL)
        rc = cli_main(
            [
                "diff",
                str(proj),
                "--url-env",
                "DBURL",
                "--store-path",
                str(populated_store),
            ]
        )
        assert rc == 1
        assert "order: only in store" in capsys.readouterr().out


class TestExportExplicitSourceMisses:
    """The explicit `--source URL` / `--url-env XX` branches in export
    exit 1 with a source-named error when the name does not exist in
    that source — distinct from the cross-source not-found path which
    walks every source first.
    """

    def test_entities_export_explicit_source_not_found(
        self,
        populated_store: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # populated_store has entities under _PG_URL. Pass a DIFFERENT
        # URL so the explicit lookup misses.
        monkeypatch.setenv("DBURL2", _PG_URL_B)
        rc = cli_main(
            [
                "entities",
                "export",
                "customer",
                "--url-env",
                "DBURL2",
                "--store-path",
                str(populated_store),
            ]
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "no entity named 'customer' for source" in err

    def test_metrics_export_explicit_source_not_found(
        self,
        populated_store: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("DBURL2", _PG_URL_B)
        rc = cli_main(
            [
                "metrics",
                "export",
                "customer_count",
                "--url-env",
                "DBURL2",
                "--store-path",
                str(populated_store),
            ]
        )
        assert rc == 1
        assert "no metric named 'customer_count' for source" in capsys.readouterr().err

    def test_joins_export_explicit_source_not_found(
        self,
        populated_store: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("DBURL2", _PG_URL_B)
        rc = cli_main(
            [
                "joins",
                "export",
                "order_to_customer",
                "--url-env",
                "DBURL2",
                "--store-path",
                str(populated_store),
            ]
        )
        assert rc == 1
        assert "no canonical join named 'order_to_customer' for source" in capsys.readouterr().err

    def test_metrics_export_cross_source_collision(
        self,
        populated_store: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Same disambiguation contract as entities — two sources
        holding the same metric name → exit 2, not 1."""
        sid_b = _make_source_id(_PG_URL_B)
        store = SQLiteStore(populated_store)
        try:
            store.write_table(
                Table(
                    name="users",
                    schema_name="public",
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
                    ),
                ),
                source_connection_id=sid_b,
            )
            store.write_entity(
                Entity(
                    name="customer",
                    description="",
                    binding=SingleTableBinding(qualified_table="public.users"),
                    identity="id",
                ),
                source_connection_id=sid_b,
            )
            store.write_metric(
                Metric(
                    name="customer_count",
                    description="",
                    entity="customer",
                    measure=MetricMeasure(agg="count", column="id"),
                    time_dimension=None,
                    time_grains=(),
                ),
                source_connection_id=sid_b,
            )
        finally:
            store.close()

        rc = cli_main(["metrics", "export", "customer_count", "--store-path", str(populated_store)])
        assert rc == 2
        err = capsys.readouterr().err
        assert "defined in 2 sources" in err

    def test_joins_export_cross_source_collision(
        self,
        populated_store: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        sid_b = _make_source_id(_PG_URL_B)
        store = SQLiteStore(populated_store)
        try:
            for tname, cols in (
                ("users", [("id", True)]),
                ("orders", [("id", True), ("user_id", False)]),
            ):
                store.write_table(
                    Table(
                        name=tname,
                        schema_name="public",
                        columns=tuple(
                            Column(
                                name=c,
                                table_name=tname,
                                schema_name="public",
                                data_type="bigint",
                                nullable=False,
                                ordinal_position=i + 1,
                                is_primary_key=is_pk,
                            )
                            for i, (c, is_pk) in enumerate(cols)
                        ),
                    ),
                    source_connection_id=sid_b,
                )
            store.write_entity(
                Entity(
                    name="customer",
                    description="",
                    binding=SingleTableBinding(qualified_table="public.users"),
                    identity="id",
                ),
                source_connection_id=sid_b,
            )
            store.write_entity(
                Entity(
                    name="order",
                    description="",
                    binding=SingleTableBinding(qualified_table="public.orders"),
                    identity="id",
                ),
                source_connection_id=sid_b,
            )
            store.write_canonical_join(
                CanonicalJoin(
                    name="order_to_customer",
                    description="",
                    source_entity="order",
                    target_entity="customer",
                    on=(JoinColumnPair(source_column="user_id", target_column="id"),),
                    cardinality="many_to_one",
                ),
                source_connection_id=sid_b,
            )
        finally:
            store.close()

        rc = cli_main(
            ["joins", "export", "order_to_customer", "--store-path", str(populated_store)]
        )
        assert rc == 2
        err = capsys.readouterr().err
        assert "defined in 2 sources" in err


class TestExportAllCrossSourceCollisions:
    """`export-all` without --source refuses when two sources share a
    name; mirrors the entities test for metrics + joins."""

    def test_metrics_export_all_cross_source_collision(
        self,
        populated_store: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        sid_b = _make_source_id(_PG_URL_B)
        store = SQLiteStore(populated_store)
        try:
            store.write_table(
                Table(
                    name="users",
                    schema_name="public",
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
                    ),
                ),
                source_connection_id=sid_b,
            )
            store.write_entity(
                Entity(
                    name="customer",
                    description="",
                    binding=SingleTableBinding(qualified_table="public.users"),
                    identity="id",
                ),
                source_connection_id=sid_b,
            )
            store.write_metric(
                Metric(
                    name="customer_count",
                    description="",
                    entity="customer",
                    measure=MetricMeasure(agg="count", column="id"),
                    time_dimension=None,
                    time_grains=(),
                ),
                source_connection_id=sid_b,
            )
        finally:
            store.close()

        rc = cli_main(
            [
                "metrics",
                "export-all",
                "--dir",
                str(tmp_path / "out"),
                "--store-path",
                str(populated_store),
            ]
        )
        assert rc == 2
        assert "collide across sources" in capsys.readouterr().err


class TestApplyAndDiffEdgeCases:
    """Defensive paths the project-walker has to handle gracefully:
    project path is a file, missing project dir for diff, malformed
    YAML in the tree (parse-error drift), inner-apply nonzero rc
    propagating to the walker's exit code.
    """

    def test_apply_project_path_is_a_file(
        self,
        tmp_path: Path,
        populated_store: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        not_a_dir = tmp_path / "regular.txt"
        not_a_dir.write_text("not a directory")
        monkeypatch.setenv("DBURL", _PG_URL)
        rc = cli_main(
            [
                "apply",
                str(not_a_dir),
                "--url-env",
                "DBURL",
                "--store-path",
                str(populated_store),
            ]
        )
        assert rc == 2
        assert "not a directory" in capsys.readouterr().err

    def test_diff_project_path_missing(
        self,
        tmp_path: Path,
        populated_store: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("DBURL", _PG_URL)
        rc = cli_main(
            [
                "diff",
                str(tmp_path / "no-such-dir"),
                "--url-env",
                "DBURL",
                "--store-path",
                str(populated_store),
            ]
        )
        assert rc == 2
        assert "not found" in capsys.readouterr().err

    def test_diff_project_path_is_a_file(
        self,
        tmp_path: Path,
        populated_store: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        not_a_dir = tmp_path / "regular.txt"
        not_a_dir.write_text("not a directory")
        monkeypatch.setenv("DBURL", _PG_URL)
        rc = cli_main(
            [
                "diff",
                str(not_a_dir),
                "--url-env",
                "DBURL",
                "--store-path",
                str(populated_store),
            ]
        )
        assert rc == 2
        assert "not a directory" in capsys.readouterr().err

    def test_diff_ignores_non_yaml_files_in_subdir(
        self,
        populated_store: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A `.txt` README or `.json` sidecar in the subdir must be
        skipped without surfacing as drift. Only `.yaml` / `.yml`
        files are considered.
        """
        proj = tmp_path / "proj"
        (proj / "entities").mkdir(parents=True)
        # A non-yaml file the operator might keep alongside the YAMLs.
        (proj / "entities" / "README.txt").write_text("notes about the entity dir\n")
        monkeypatch.setenv("DBURL", _PG_URL)
        rc = cli_main(
            [
                "diff",
                str(proj),
                "--url-env",
                "DBURL",
                "--store-path",
                str(populated_store),
            ]
        )
        # Only the two store-side entities surface as only-in-store
        # drift; the README is invisible to the diff.
        assert rc == 1
        out = capsys.readouterr().out
        assert "README" not in out

    def test_diff_malformed_yaml_is_parse_error_drift(
        self,
        populated_store: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A malformed YAML file in the tree surfaces as a parse-error
        drift entry — counted in the drift total, not raised. So a
        diff against a tree with one broken file still surfaces every
        other drift before failing.
        """
        proj = tmp_path / "proj"
        (proj / "entities").mkdir(parents=True)
        # Garbage YAML — version is the wrong type.
        (proj / "entities" / "broken.yaml").write_text("version: not-a-number\nname: oops\n")
        monkeypatch.setenv("DBURL", _PG_URL)
        rc = cli_main(
            [
                "diff",
                str(proj),
                "--url-env",
                "DBURL",
                "--store-path",
                str(populated_store),
            ]
        )
        assert rc == 1
        out = capsys.readouterr().out
        assert "PARSE ERROR" in out
        assert "broken.yaml" in out

    def test_apply_inner_handler_rc_propagates(
        self,
        populated_store: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A malformed YAML inside an `apply` walk causes the per-
        resource handler to return non-zero; the top-level walker
        must propagate the highest rc instead of silently returning 0.
        """
        proj = tmp_path / "proj"
        (proj / "entities").mkdir(parents=True)
        (proj / "entities" / "broken.yaml").write_text("version: 1\nname:\n")
        monkeypatch.setenv("DBURL", _PG_URL)
        rc = cli_main(
            [
                "apply",
                str(proj),
                "--url-env",
                "DBURL",
                "--store-path",
                str(populated_store),
            ]
        )
        # Per-resource apply returns 1 on parse failure; walker
        # propagates the max rc across all subdirs.
        assert rc == 1
        out = capsys.readouterr().out
        assert "applied with errors" in out


class TestEmitYamlProjectionFailure:
    """The post-wizard emit hook propagates a non-zero rc from any
    inner export-all call. Sanity-checks the wiring so a partial
    on-disk projection doesn't masquerade as a successful init."""

    def test_emit_failure_propagates(
        self,
        populated_store: Path,
        tmp_path: Path,
    ) -> None:
        from schemabrain.cli import _emit_yaml_projection

        # Pre-create one file in the target subdir so the entities
        # export-all refuses (existing-file collision).
        out = tmp_path / "emit"
        (out / "entities").mkdir(parents=True)
        (out / "entities" / "customer.yaml").write_text("pre-existing edit")

        rc = _emit_yaml_projection(
            base_dir=str(out),
            store_path=str(populated_store),
            source_url=_PG_URL,
        )
        assert rc == 2
        # Pre-existing file untouched.
        assert (out / "entities" / "customer.yaml").read_text() == "pre-existing edit"


class TestExportSourceResolveFailure:
    """Every export handler runs `--source`/`--url-env` through
    `_resolve_source_id_or_walk`; when the flag points at a non-
    existent env var or an otherwise unresolvable URL, the handler
    must exit 2 cleanly instead of crashing or silently walking all
    sources. Tested per handler so each early-return is exercised.
    """

    @pytest.mark.parametrize(
        "argv_template",
        [
            ["entities", "export", "customer"],
            ["metrics", "export", "customer_count"],
            ["joins", "export", "order_to_customer"],
        ],
    )
    def test_single_export_unresolvable_source(
        self,
        populated_store: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        argv_template: list[str],
    ) -> None:
        monkeypatch.delenv("NO_SUCH_VAR", raising=False)
        rc = cli_main(
            [
                *argv_template,
                "--url-env",
                "NO_SUCH_VAR",
                "--store-path",
                str(populated_store),
            ]
        )
        assert rc == 2
        # A guided / stderr error was rendered (don't pin the exact
        # message because `_resolve_url_source` owns it; just confirm
        # we got SOMETHING and the handler exited cleanly).
        assert capsys.readouterr().err != ""

    @pytest.mark.parametrize(
        "argv_template",
        [
            ["entities", "export-all"],
            ["metrics", "export-all"],
            ["joins", "export-all"],
        ],
    )
    def test_export_all_unresolvable_source(
        self,
        populated_store: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        argv_template: list[str],
    ) -> None:
        monkeypatch.delenv("NO_SUCH_VAR", raising=False)
        rc = cli_main(
            [
                *argv_template,
                "--dir",
                str(tmp_path / "out"),
                "--url-env",
                "NO_SUCH_VAR",
                "--store-path",
                str(populated_store),
            ]
        )
        assert rc == 2


class TestExportCrossSourceNotFound:
    """The cross-source not-found branch (without `--source`) for
    metrics + joins — the entities counterpart is covered by
    `TestEntitiesExportSingle.test_unknown_name_exits_one` since
    `populated_store` has only one source.
    """

    def test_metrics_export_unknown_name(
        self,
        populated_store: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = cli_main(["metrics", "export", "no_such_metric", "--store-path", str(populated_store)])
        assert rc == 1
        assert "no metric named" in capsys.readouterr().err

    def test_joins_export_unknown_name(
        self,
        populated_store: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = cli_main(["joins", "export", "no_such_join", "--store-path", str(populated_store)])
        assert rc == 1
        assert "no canonical join named" in capsys.readouterr().err


class TestApplyAndDiffSourceFailures:
    """Top-level apply/diff also fail-fast on missing/unresolvable
    source URLs — covers the early-return branches symmetric with
    the export handlers above.
    """

    def test_apply_missing_source_returns_two(
        self,
        populated_store: Path,
        tmp_path: Path,
    ) -> None:
        proj = tmp_path / "proj"
        proj.mkdir()
        rc = cli_main(["apply", str(proj), "--store-path", str(populated_store)])
        assert rc == 2

    def test_diff_missing_source_returns_two(
        self,
        populated_store: Path,
        tmp_path: Path,
    ) -> None:
        proj = tmp_path / "proj"
        proj.mkdir()
        rc = cli_main(["diff", str(proj), "--store-path", str(populated_store)])
        assert rc == 2


class TestInitEmitYamlDir:
    """`init --emit-yaml-dir PATH` writes the YAML projection of the
    just-populated store under PATH/{entities,metrics,joins}. Reuses
    the per-resource export-all handlers so the file format and
    collision contract match.

    We don't run the full wizard here (it requires Postgres
    connectivity) — instead we exercise the `_emit_yaml_projection`
    helper directly to verify the wiring + the per-resource handler
    invocation.
    """

    def test_emit_yaml_projection_writes_three_subdirs(
        self,
        populated_store: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from schemabrain.cli import _emit_yaml_projection

        out = tmp_path / "emit"
        rc = _emit_yaml_projection(
            base_dir=str(out),
            store_path=str(populated_store),
            source_url=_PG_URL,
        )
        assert rc == 0
        # All three subdirs populated.
        assert (out / "entities" / "customer.yaml").exists()
        assert (out / "metrics" / "customer_count.yaml").exists()
        assert (out / "joins" / "order_to_customer.yaml").exists()
        # Confirmation line on stderr.
        assert "emitted YAML projection" in capsys.readouterr().err
