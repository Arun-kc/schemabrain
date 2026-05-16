"""End-to-end test of the dbt import pipeline against the bundled fixture.

Pins the full pipeline (steps 1-6) wired together against the bundled
`schemabrain/imports/fixtures/ecommerce_manifest.json` artifact:

  parse manifest → resolve identity → map to Entity → verify against
  live schema → diff against store → apply with origin=dbt_import →
  CLI summary + JSON report

The bundled fixture demonstrates the ownership-transfer story
explicitly: each dbt model `alias`es to the same physical table the
existing bundled entity YAMLs target (`public.users` /
`public.orders` / `public.products`). A user who has already run
`entities apply` against the YAML fixtures, then runs `import dbt`
against this manifest, sees dbt take ownership of all three entities
— exercising the dbt-guard contract end-to-end.

This test uses the same `_FakeDataSource` shape as the parser /
mapper / verify / driver / CLI tests rather than testcontainers;
the goal here is wiring verification, not a Postgres-against-dbt
cross-product. A real-Postgres integration test is tracked as a
follow-up.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from schemabrain.cli import _cmd_import_dbt, _make_source_id
from schemabrain.connectors.errors import TableNotFoundError
from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.eval.bundled import resolve_bundled_path
from schemabrain.imports.dbt import (
    apply_dbt_import_plan,
    parse_dbt_manifest,
    plan_dbt_import,
)

_SOURCE_URL = "postgresql+psycopg://user:pw@localhost:5432/db"


# ----- shared helpers -------------------------------------------------------


class _FakeDataSource:
    def __init__(self, tables: Mapping[tuple[str, str], Table]) -> None:
        self._tables = dict(tables)

    def list_tables(self, schema: str | None = None) -> list[tuple[str, str]]:
        return list(self._tables.keys())

    def get_table(self, name: str, schema: str) -> Table:
        try:
            return self._tables[(schema, name)]
        except KeyError as exc:
            raise TableNotFoundError(f"table {schema}.{name} not found") from exc

    def close(self) -> None:  # pragma: no cover — never closed
        pass

    def __enter__(self) -> _FakeDataSource:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        pass


def _column(
    name: str,
    table: str,
    *,
    data_type: str = "bigint",
    nullable: bool = False,
    ordinal: int = 1,
    is_primary_key: bool = False,
) -> Column:
    return Column(
        name=name,
        table_name=table,
        schema_name="public",
        data_type=data_type,
        nullable=nullable,
        ordinal_position=ordinal,
        is_primary_key=is_primary_key,
    )


def _ecommerce_live_source() -> _FakeDataSource:
    """Live schema matching `schemabrain/eval/fixtures/ecommerce.sql`.

    The bundled manifest's three dbt models alias to `users` /
    `orders` / `products`; the live tables must exist with the
    declared columns + NOT NULL identity. The columns provided here
    are a superset of what the manifest declares (verify enforces
    subset, not equality).
    """
    users = Table(
        name="users",
        schema_name="public",
        columns=(
            _column("id", "users", is_primary_key=True),
            _column("email", "users", data_type="text", ordinal=2),
            _column("created_at", "users", data_type="timestamp", ordinal=3),
        ),
        foreign_keys=(),
    )
    orders = Table(
        name="orders",
        schema_name="public",
        columns=(
            _column("id", "orders", is_primary_key=True),
            _column("user_id", "orders", ordinal=2),
            _column("total_cents", "orders", data_type="integer", ordinal=3),
            _column("status", "orders", data_type="text", ordinal=4),
        ),
        foreign_keys=(),
    )
    products = Table(
        name="products",
        schema_name="public",
        columns=(
            _column("id", "products", is_primary_key=True),
            _column("sku", "products", data_type="text", ordinal=2),
            _column("name", "products", data_type="text", ordinal=3),
        ),
        foreign_keys=(),
    )
    return _FakeDataSource(
        {
            ("public", "users"): users,
            ("public", "orders"): orders,
            ("public", "products"): products,
        }
    )


def _seed_ecommerce_tables(tmp_path: Path) -> Path:
    """Seed the SQLite store with the same 3 tables the live source has."""
    store_path = tmp_path / "store.db"
    source_id = _make_source_id(_SOURCE_URL)
    with SQLiteStore(store_path) as store:
        for table_name in ("users", "orders", "products"):
            table = Table(
                name=table_name,
                schema_name="public",
                columns=(_column("id", table_name, is_primary_key=True),),
                foreign_keys=(),
            )
            store.write_table(table, source_connection_id=source_id)
    return store_path


def _factory_for(source: _FakeDataSource):
    def factory(url: str) -> _FakeDataSource:
        return source

    return factory


# ----- bundled fixture itself -----------------------------------------------


class TestBundledFixtureWellFormed:
    def test_fixture_is_resolvable_via_resolve_bundled_path(self) -> None:
        # `resolve_bundled_path` is the user-facing surface
        # (`schemabrain fixture-path ecommerce_manifest.json`); the
        # bundled dbt manifest must be reachable through it.
        path = resolve_bundled_path("ecommerce_manifest.json")
        assert path.is_file()
        assert path.name == "ecommerce_manifest.json"

    def test_fixture_parses_with_three_models(self) -> None:
        # Sanity: the bundled fixture loads through the same parser
        # the CLI uses. Catches a regression where someone edits the
        # JSON and breaks the shape.
        path = resolve_bundled_path("ecommerce_manifest.json")
        manifest = parse_dbt_manifest(path)
        assert manifest.dbt_project_name == "schemabrain_demo"
        assert len(manifest.models) == 3
        names = {m.name for m in manifest.models}
        assert names == {"customer", "order", "product"}

    def test_fixture_aliases_to_ecommerce_tables(self) -> None:
        # The fixture's identifiers MUST match the bundled
        # ecommerce.sql tables so the demo flow connects:
        # `psql < ecommerce.sql` → `schemabrain index` → `import dbt`
        # → entities land bound to the indexed tables.
        path = resolve_bundled_path("ecommerce_manifest.json")
        manifest = parse_dbt_manifest(path)
        identifiers = {m.identifier for m in manifest.models}
        assert identifiers == {"users", "orders", "products"}

    def test_fixture_carries_skipped_resources(self) -> None:
        # The fixture intentionally includes one metric, one snapshot,
        # one seed — so the bundled demo exercises the "non-model
        # resources deferred" breadcrumb path.
        path = resolve_bundled_path("ecommerce_manifest.json")
        manifest = parse_dbt_manifest(path)
        assert manifest.skipped.metrics == 1
        assert manifest.skipped.snapshots == 1
        assert manifest.skipped.seeds == 1

    def test_fixture_has_a_source_for_lineage(self) -> None:
        # At least one source so depends_on_sources resolution is
        # exercised end-to-end against the bundled fixture.
        path = resolve_bundled_path("ecommerce_manifest.json")
        manifest = parse_dbt_manifest(path)
        assert len(manifest.sources_by_id) >= 1


# ----- E2E pipeline: bundled fixture → store -------------------------------


class TestEndToEndImport:
    def test_full_pipeline_writes_three_dbt_import_entities(self, tmp_path: Path) -> None:
        # The headline E2E: parse → identify → map → verify → diff →
        # apply, against the bundled fixture + the bundled live
        # schema, against a fresh store. All 3 models land with
        # origin=dbt_import.
        manifest_path = resolve_bundled_path("ecommerce_manifest.json")
        store_path = _seed_ecommerce_tables(tmp_path)
        source = _ecommerce_live_source()

        exit_code = _cmd_import_dbt(
            manifest_path=str(manifest_path),
            positional_url=_SOURCE_URL,
            url_env=None,
            store_path=str(store_path),
            dry_run=False,
            report_path=None,
            _source_factory=_factory_for(source),
        )
        assert exit_code == 0

        source_id = _make_source_id(_SOURCE_URL)
        with SQLiteStore(store_path) as store:
            customer = store.get_entity("customer", source_connection_id=source_id)
            order = store.get_entity("order", source_connection_id=source_id)
            product = store.get_entity("product", source_connection_id=source_id)

        for entity in (customer, order, product):
            assert entity is not None
            assert entity.origin == "dbt_import"

        # Each entity binds to its compiled physical table.
        assert customer.binding.qualified_table == "public.users"
        assert order.binding.qualified_table == "public.orders"
        assert product.binding.qualified_table == "public.products"

    def test_idempotent_re_import(self, tmp_path: Path) -> None:
        # Running the import twice against the same fixture is a
        # no-op on the entity side — second run produces an empty
        # `to_add` and a full `to_update` bucket; final state is
        # identical to first run.
        manifest_path = resolve_bundled_path("ecommerce_manifest.json")
        store_path = _seed_ecommerce_tables(tmp_path)
        source = _ecommerce_live_source()
        factory = _factory_for(source)

        # First import
        _cmd_import_dbt(
            manifest_path=str(manifest_path),
            positional_url=_SOURCE_URL,
            url_env=None,
            store_path=str(store_path),
            dry_run=False,
            report_path=None,
            _source_factory=factory,
        )
        # Second import (same args)
        manifest = parse_dbt_manifest(manifest_path)
        with SQLiteStore(store_path) as store:
            plan = plan_dbt_import(
                manifest, source, store, source_connection_id=_make_source_id(_SOURCE_URL)
            )
        # Second-run classification: all 3 land in to_update; nothing
        # in to_add or to_take_ownership.
        assert len(plan.to_update) == 3
        assert plan.to_add == ()
        assert plan.to_take_ownership == ()

    def test_ownership_transfer_from_manual_entities(self, tmp_path: Path) -> None:
        # The bundled demo's killer feature: a user who has already
        # run `entities apply` against the bundled YAMLs (which
        # write origin=manual) and then runs `import dbt` against
        # this fixture sees dbt take ownership of all 3 entities.
        store_path = _seed_ecommerce_tables(tmp_path)
        source_id = _make_source_id(_SOURCE_URL)

        # Pre-seed manual entities matching the dbt fixture names +
        # bindings.
        with SQLiteStore(store_path) as store:
            for entity_name, table_name in (
                ("customer", "users"),
                ("order", "orders"),
                ("product", "products"),
            ):
                store.write_entity(
                    Entity(
                        name=entity_name,
                        description="manually authored",
                        binding=SingleTableBinding(qualified_table=f"public.{table_name}"),
                        identity="id",
                        origin="manual",
                    ),
                    source_connection_id=source_id,
                )

        manifest_path = resolve_bundled_path("ecommerce_manifest.json")
        source = _ecommerce_live_source()

        manifest = parse_dbt_manifest(manifest_path)
        with SQLiteStore(store_path) as store:
            plan = plan_dbt_import(manifest, source, store, source_connection_id=source_id)
            # All 3 should classify as ownership-transfer (manual → dbt).
            assert plan.to_add == ()
            assert plan.to_update == ()
            assert len(plan.to_take_ownership) == 3
            for _envelope, prior in plan.to_take_ownership:
                assert prior == "manual"

            apply_dbt_import_plan(plan, store, source_connection_id=source_id)

            # After apply, origins are all dbt_import.
            customer = store.get_entity("customer", source_connection_id=source_id)
            order = store.get_entity("order", source_connection_id=source_id)
            product = store.get_entity("product", source_connection_id=source_id)
        assert customer is not None and customer.origin == "dbt_import"
        assert order is not None and order.origin == "dbt_import"
        assert product is not None and product.origin == "dbt_import"
        # And the descriptions match the dbt manifest's, not the
        # original "manually authored" strings — dbt is the source of
        # truth after ownership transfer.
        assert "shopper" in customer.description.lower()

    def test_dbt_guard_blocks_manual_writes_after_import(self, tmp_path: Path) -> None:
        # The dbt-guard contract: after `import dbt` puts an entity at
        # origin=dbt_import, a subsequent `entities apply` writing
        # origin=manual to the same name is refused by the store. The
        # E2E test pins this contract from the import side — once dbt
        # owns the row, it stays owned until another dbt import
        # changes it.
        from schemabrain.core.entity import DbtOwnedEntityError

        manifest_path = resolve_bundled_path("ecommerce_manifest.json")
        store_path = _seed_ecommerce_tables(tmp_path)
        source = _ecommerce_live_source()

        _cmd_import_dbt(
            manifest_path=str(manifest_path),
            positional_url=_SOURCE_URL,
            url_env=None,
            store_path=str(store_path),
            dry_run=False,
            report_path=None,
            _source_factory=_factory_for(source),
        )

        # Now try to overwrite with origin=manual — must refuse.
        source_id = _make_source_id(_SOURCE_URL)
        with SQLiteStore(store_path) as store, pytest.raises(DbtOwnedEntityError):
            store.write_entity(
                Entity(
                    name="customer",
                    description="hand-edited overrides",
                    binding=SingleTableBinding(qualified_table="public.users"),
                    identity="id",
                    origin="manual",
                ),
                source_connection_id=source_id,
            )

    def test_json_report_round_trip(self, tmp_path: Path) -> None:
        # E2E with --report: the JSON report can be re-read by a CI
        # consumer and carries the same field shape as the dataclasses.
        manifest_path = resolve_bundled_path("ecommerce_manifest.json")
        store_path = _seed_ecommerce_tables(tmp_path)
        report_path = tmp_path / "report.json"
        source = _ecommerce_live_source()

        _cmd_import_dbt(
            manifest_path=str(manifest_path),
            positional_url=_SOURCE_URL,
            url_env=None,
            store_path=str(store_path),
            dry_run=False,
            report_path=str(report_path),
            _source_factory=_factory_for(source),
        )

        body = json.loads(report_path.read_text())
        assert body["dbt_project_name"] == "schemabrain_demo"
        assert body["counts"]["to_add"] == 3
        assert set(body["to_add"]) == {"customer", "order", "product"}
        assert body["counts"]["orphans"] == 0
        assert body["counts"]["skipped"] == 0
        assert body["skip_counts"]["metrics"] == 1
        assert body["skip_counts"]["snapshots"] == 1
        assert body["skip_counts"]["seeds"] == 1


# ----- regression guard for fixture-path CLI --------------------------------


class TestFixturePathCLI:
    def test_fixture_path_command_prints_bundled_manifest_path(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `schemabrain fixture-path ecommerce_manifest.json` should
        # return the file path so README/docs can use it via shell
        # substitution (`schemabrain import dbt $(schemabrain
        # fixture-path ecommerce_manifest.json)`).
        from schemabrain.cli import main

        exit_code = main(["fixture-path", "ecommerce_manifest.json"])
        assert exit_code == 0
        out = capsys.readouterr().out.strip()
        assert out.endswith("ecommerce_manifest.json")
        # Sanity: path is real
        assert Path(out).is_file()
