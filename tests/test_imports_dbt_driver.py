"""Tests for the dbt import diff + write driver.

Pins the fifth piece of the dbt import substrate. The driver
orchestrates steps 1-4 against a real Store and a live DataSource,
producing a two-phase pipeline:

  1. `plan_dbt_import(manifest, source, store, ...)` — pure planning.
     Walks the manifest, calls map + verify per model, classifies
     each result against existing store state into `to_add` /
     `to_update` / `to_take_ownership` / `skipped` buckets. Discovers
     orphans (existing `dbt_import` rows in store with no matching
     manifest entry). NO writes.

  2. `apply_dbt_import_plan(plan, store, ...)` — executes the
     classified plan. Walks `to_add` + `to_update` + `to_take_
     ownership`, calls `store.write_entity()` per envelope. Catches
     `sqlite3.IntegrityError` from missing-table FK violations and
     surfaces as `DbtWriteFailure`. Does NOT mutate orphans.

Test setup uses real `SQLiteStore` with `tmp_path` so the dbt-guard
contract (store-level enforcement) is exercised end-to-end. The
DataSource is faked since the verify call surface is narrow.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from pathlib import Path

import pytest

from schemabrain.connectors.errors import TableNotFoundError
from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.imports.dbt import (
    DbtColumn,
    DbtColumnTests,
    DbtConstraint,
    DbtImportPlan,
    DbtImportResult,
    DbtImportSkip,
    DbtManifest,
    DbtModelNode,
    DbtSkipCounts,
    DbtSourceNode,
    DbtWriteFailure,
    apply_dbt_import_plan,
    plan_dbt_import,
)

_SOURCE_ID = "test_source"


# ----- shared fixture helpers -----------------------------------------------


class _FakeDataSource:
    """DataSource shape — verify call surface only."""

    def __init__(self, tables: Mapping[tuple[str, str], Table]) -> None:
        self._tables = dict(tables)

    def list_tables(self, schema: str | None = None) -> list[tuple[str, str]]:
        return list(self._tables.keys())

    def get_table(self, name: str, schema: str) -> Table:
        try:
            return self._tables[(schema, name)]
        except KeyError as exc:
            raise TableNotFoundError(f"table {schema}.{name} not found") from exc

    def close(self) -> None:  # pragma: no cover — driver never closes
        pass


def _live_column(
    name: str,
    *,
    table: str,
    schema: str = "public",
    data_type: str = "integer",
    nullable: bool = False,
    ordinal: int = 1,
    is_primary_key: bool = False,
) -> Column:
    return Column(
        name=name,
        table_name=table,
        schema_name=schema,
        data_type=data_type,
        nullable=nullable,
        ordinal_position=ordinal,
        is_primary_key=is_primary_key,
    )


def _live_table(name: str, *columns: Column, schema: str = "public") -> Table:
    return Table(name=name, schema_name=schema, columns=columns, foreign_keys=())


def _dbt_model(
    *,
    name: str,
    project: str = "demo_project",
    schema: str = "public",
    identifier: str | None = None,
    description: str = "",
    columns: tuple[DbtColumn, ...] | None = None,
) -> DbtModelNode:
    if columns is None:
        columns = (
            DbtColumn(
                name="id",
                data_type="integer",
                description="",
                constraints=(DbtConstraint(type="primary_key"),),
                tests=DbtColumnTests(),
            ),
        )
    return DbtModelNode(
        unique_id=f"model.{project}.{name}",
        name=name,
        database="schemabrain_test",
        schema_name=schema,
        identifier=identifier or name,
        description=description,
        columns=columns,
        depends_on_sources=(),
    )


def _manifest(
    *models: DbtModelNode,
    sources: tuple[DbtSourceNode, ...] = (),
    project: str = "demo_project",
    skip_counts: DbtSkipCounts | None = None,
) -> DbtManifest:
    return DbtManifest(
        manifest_version=12,
        dbt_project_name=project,
        models=models,
        sources_by_id={s.unique_id: s for s in sources},
        skipped=skip_counts or DbtSkipCounts(),
    )


def _seed_store_with_table(tmp_path: Path, name: str = "customer_dim") -> Path:
    """Seed the SQLite store with one table so entity FK is satisfied."""
    store_path = tmp_path / "store.db"
    table = _live_table(
        name,
        _live_column("id", table=name, nullable=False, is_primary_key=True),
    )
    with SQLiteStore(store_path) as store:
        store.write_table(table, source_connection_id=_SOURCE_ID)
    return store_path


def _seed_store_with_tables(tmp_path: Path, names: tuple[str, ...]) -> Path:
    store_path = tmp_path / "store.db"
    with SQLiteStore(store_path) as store:
        for name in names:
            table = _live_table(
                name,
                _live_column("id", table=name, nullable=False, is_primary_key=True),
            )
            store.write_table(table, source_connection_id=_SOURCE_ID)
    return store_path


def _seed_existing_entity(
    store_path: Path,
    name: str,
    *,
    qualified_table: str,
    origin: str,
) -> None:
    """Seed an existing entity row so the driver classifies overwrites."""
    entity = Entity(
        name=name,
        description="seeded",
        binding=SingleTableBinding(qualified_table=qualified_table),
        identity="id",
        origin=origin,  # type: ignore[arg-type]
    )
    with SQLiteStore(store_path) as store:
        store.write_entity(entity, source_connection_id=_SOURCE_ID)


# ----- plan: classification buckets -----------------------------------------


class TestPlanClassifiesAdded:
    def test_single_model_with_no_existing_entity_is_added(self, tmp_path: Path) -> None:
        # Model has no matching entity in the store → it lands in `to_add`.
        model = _dbt_model(name="customer_dim")
        manifest = _manifest(model)
        store_path = _seed_store_with_table(tmp_path)

        live = _live_table(
            "customer_dim",
            _live_column("id", table="customer_dim", nullable=False, is_primary_key=True),
        )
        source = _FakeDataSource({("public", "customer_dim"): live})

        with SQLiteStore(store_path) as store:
            plan = plan_dbt_import(manifest, source, store, source_connection_id=_SOURCE_ID)

        assert len(plan.to_add) == 1
        assert plan.to_add[0].entity.name == "customer_dim"
        assert plan.to_add[0].entity.origin == "dbt_import"
        assert plan.to_update == ()
        assert plan.to_take_ownership == ()
        assert plan.dbt_project_name == "demo_project"


class TestPlanClassifiesUpdated:
    def test_existing_dbt_import_entity_classified_as_update(self, tmp_path: Path) -> None:
        # Re-import path: an existing entity with origin=dbt_import that
        # matches a manifest model lands in `to_update` (idempotent).
        store_path = _seed_store_with_table(tmp_path)
        _seed_existing_entity(
            store_path,
            "customer_dim",
            qualified_table="public.customer_dim",
            origin="dbt_import",
        )

        model = _dbt_model(name="customer_dim")
        manifest = _manifest(model)
        live = _live_table(
            "customer_dim",
            _live_column("id", table="customer_dim", nullable=False, is_primary_key=True),
        )
        source = _FakeDataSource({("public", "customer_dim"): live})

        with SQLiteStore(store_path) as store:
            plan = plan_dbt_import(manifest, source, store, source_connection_id=_SOURCE_ID)

        assert plan.to_add == ()
        assert len(plan.to_update) == 1
        assert plan.to_update[0].entity.name == "customer_dim"
        assert plan.to_take_ownership == ()


class TestPlanClassifiesOwnershipTransfer:
    @pytest.mark.parametrize("previous_origin", ["manual", "suggested"])
    def test_existing_non_dbt_entity_is_ownership_transfer(
        self, tmp_path: Path, previous_origin: str
    ) -> None:
        # When a manual or suggested entity shares a name with a
        # manifest model, the import takes ownership (dbt-guard
        # contract allows dbt to overwrite non-dbt origins). The plan
        # surfaces this as a separate bucket so the stderr breadcrumb
        # can name the transfer for the user.
        store_path = _seed_store_with_table(tmp_path)
        _seed_existing_entity(
            store_path,
            "customer_dim",
            qualified_table="public.customer_dim",
            origin=previous_origin,
        )

        model = _dbt_model(name="customer_dim")
        manifest = _manifest(model)
        live = _live_table(
            "customer_dim",
            _live_column("id", table="customer_dim", nullable=False, is_primary_key=True),
        )
        source = _FakeDataSource({("public", "customer_dim"): live})

        with SQLiteStore(store_path) as store:
            plan = plan_dbt_import(manifest, source, store, source_connection_id=_SOURCE_ID)

        assert plan.to_add == ()
        assert plan.to_update == ()
        assert len(plan.to_take_ownership) == 1
        envelope, prior = plan.to_take_ownership[0]
        assert envelope.entity.name == "customer_dim"
        assert prior == previous_origin


# ----- plan: orphan detection -----------------------------------------------


class TestPlanOrphans:
    def test_dbt_import_entity_not_in_manifest_is_orphan(self, tmp_path: Path) -> None:
        # An entity with origin=dbt_import that exists in the store but
        # no longer corresponds to a manifest model is an orphan — the
        # plan surfaces it by name (stderr breadcrumb material). The
        # driver does NOT auto-delete (v1 decision; revisit when
        # `entities delete` lands).
        store_path = _seed_store_with_tables(tmp_path, ("customer_dim", "old_dim"))
        _seed_existing_entity(
            store_path,
            "old_dim",
            qualified_table="public.old_dim",
            origin="dbt_import",
        )

        # Manifest only contains customer_dim — old_dim is orphaned
        model = _dbt_model(name="customer_dim")
        manifest = _manifest(model)
        live = _live_table(
            "customer_dim",
            _live_column("id", table="customer_dim", nullable=False, is_primary_key=True),
        )
        source = _FakeDataSource({("public", "customer_dim"): live})

        with SQLiteStore(store_path) as store:
            plan = plan_dbt_import(manifest, source, store, source_connection_id=_SOURCE_ID)

        assert plan.orphans == ("old_dim",)

    def test_manual_entities_are_not_orphans(self, tmp_path: Path) -> None:
        # An entity with origin=manual that exists but isn't in the
        # manifest is NOT an orphan — it's an unrelated user-curated
        # entity that this dbt project never produced.
        store_path = _seed_store_with_tables(tmp_path, ("customer_dim", "manual_dim"))
        _seed_existing_entity(
            store_path,
            "manual_dim",
            qualified_table="public.manual_dim",
            origin="manual",
        )

        model = _dbt_model(name="customer_dim")
        manifest = _manifest(model)
        live = _live_table(
            "customer_dim",
            _live_column("id", table="customer_dim", nullable=False, is_primary_key=True),
        )
        source = _FakeDataSource({("public", "customer_dim"): live})

        with SQLiteStore(store_path) as store:
            plan = plan_dbt_import(manifest, source, store, source_connection_id=_SOURCE_ID)

        assert plan.orphans == ()


# ----- plan: skip classification --------------------------------------------


class TestPlanSkipsModel:
    def test_identity_resolution_failure_buckets_as_identity_resolution(
        self, tmp_path: Path
    ) -> None:
        # Model with no resolvable identity tier → skipped with reason
        # "identity_resolution". The run continues.
        model = _dbt_model(
            name="customer_dim",
            columns=(
                # No primary_key constraint, no unique+not_null tests
                DbtColumn(
                    name="email",
                    data_type="text",
                    description="",
                    constraints=(),
                    tests=DbtColumnTests(),
                ),
            ),
        )
        manifest = _manifest(model)
        store_path = _seed_store_with_table(tmp_path)
        source = _FakeDataSource({})  # verify never reached

        with SQLiteStore(store_path) as store:
            plan = plan_dbt_import(manifest, source, store, source_connection_id=_SOURCE_ID)

        assert plan.to_add == ()
        assert len(plan.skipped) == 1
        assert plan.skipped[0].reason == "identity_resolution"
        assert plan.skipped[0].dbt_unique_id == "model.demo_project.customer_dim"

    def test_invalid_entity_name_buckets_as_mapping(self, tmp_path: Path) -> None:
        # Hyphenated model name fails Entity's identifier regex → skip
        # with reason "mapping".
        model = _dbt_model(name="customer-dim")
        manifest = _manifest(model)
        store_path = _seed_store_with_table(tmp_path)
        source = _FakeDataSource({})

        with SQLiteStore(store_path) as store:
            plan = plan_dbt_import(manifest, source, store, source_connection_id=_SOURCE_ID)

        assert len(plan.skipped) == 1
        assert plan.skipped[0].reason == "mapping"

    def test_live_schema_drift_buckets_as_schema_drift(self, tmp_path: Path) -> None:
        # Live table doesn't exist → verify raises DbtSchemaDriftError
        # → skip with reason "schema_drift".
        model = _dbt_model(name="customer_dim")
        manifest = _manifest(model)
        store_path = _seed_store_with_table(tmp_path)
        source = _FakeDataSource({})  # nothing live

        with SQLiteStore(store_path) as store:
            plan = plan_dbt_import(manifest, source, store, source_connection_id=_SOURCE_ID)

        assert plan.to_add == ()
        assert len(plan.skipped) == 1
        assert plan.skipped[0].reason == "schema_drift"

    def test_skip_message_is_populated(self, tmp_path: Path) -> None:
        # The skip message is the underlying exception's text — so the
        # CLI's stderr breadcrumb can name WHY each skip happened.
        model = _dbt_model(
            name="customer_dim",
            columns=(
                DbtColumn(
                    name="email",
                    data_type="text",
                    description="",
                    constraints=(),
                    tests=DbtColumnTests(),
                ),
            ),
        )
        manifest = _manifest(model)
        store_path = _seed_store_with_table(tmp_path)
        source = _FakeDataSource({})

        with SQLiteStore(store_path) as store:
            plan = plan_dbt_import(manifest, source, store, source_connection_id=_SOURCE_ID)

        assert "identity" in plan.skipped[0].message.lower()


# ----- plan: skip_counts surface --------------------------------------------


class TestPlanSurfacesSkipCounts:
    def test_skip_counts_carries_through_from_manifest(self, tmp_path: Path) -> None:
        # The non-model resource counts from the parser ride on the
        # plan so the CLI can print the deferred-resource breadcrumb.
        model = _dbt_model(name="customer_dim")
        manifest = _manifest(model, skip_counts=DbtSkipCounts(metrics=3, snapshots=1))
        store_path = _seed_store_with_table(tmp_path)
        live = _live_table(
            "customer_dim",
            _live_column("id", table="customer_dim", nullable=False, is_primary_key=True),
        )
        source = _FakeDataSource({("public", "customer_dim"): live})

        with SQLiteStore(store_path) as store:
            plan = plan_dbt_import(manifest, source, store, source_connection_id=_SOURCE_ID)

        assert plan.skip_counts == DbtSkipCounts(metrics=3, snapshots=1)


# ----- plan: determinism + ordering -----------------------------------------


class TestPlanDeterminism:
    def test_multiple_models_sort_deterministically(self, tmp_path: Path) -> None:
        # The driver sorts each bucket by entity name so the CLI's
        # stderr output and the audit log are stable across runs.
        store_path = _seed_store_with_tables(
            tmp_path, ("customer_dim", "orders_fact", "products_dim")
        )
        models = (
            _dbt_model(name="orders_fact"),
            _dbt_model(name="customer_dim"),
            _dbt_model(name="products_dim"),
        )
        manifest = _manifest(*models)
        live_tables: dict[tuple[str, str], Table] = {}
        for n in ("customer_dim", "orders_fact", "products_dim"):
            live_tables[("public", n)] = _live_table(
                n, _live_column("id", table=n, nullable=False, is_primary_key=True)
            )
        source = _FakeDataSource(live_tables)

        with SQLiteStore(store_path) as store:
            plan = plan_dbt_import(manifest, source, store, source_connection_id=_SOURCE_ID)

        names = [env.entity.name for env in plan.to_add]
        assert names == sorted(names)


# ----- apply: writes ---------------------------------------------------------


class TestApplyWritesAdded:
    def test_added_entities_land_in_store_with_dbt_origin(self, tmp_path: Path) -> None:
        model = _dbt_model(name="customer_dim")
        manifest = _manifest(model)
        store_path = _seed_store_with_table(tmp_path)
        live = _live_table(
            "customer_dim",
            _live_column("id", table="customer_dim", nullable=False, is_primary_key=True),
        )
        source = _FakeDataSource({("public", "customer_dim"): live})

        with SQLiteStore(store_path) as store:
            plan = plan_dbt_import(manifest, source, store, source_connection_id=_SOURCE_ID)
            result = apply_dbt_import_plan(plan, store, source_connection_id=_SOURCE_ID)
            assert isinstance(result, DbtImportResult)
            assert result.write_failures == ()

            persisted = store.get_entity("customer_dim", source_connection_id=_SOURCE_ID)
        assert persisted is not None
        assert persisted.origin == "dbt_import"


class TestApplyWritesUpdates:
    def test_idempotent_re_run_preserves_origin(self, tmp_path: Path) -> None:
        # Apply twice with the same manifest. Second run should still
        # succeed and leave origin=dbt_import (idempotent re-import).
        model = _dbt_model(name="customer_dim", description="v1")
        manifest = _manifest(model)
        store_path = _seed_store_with_table(tmp_path)
        live = _live_table(
            "customer_dim",
            _live_column("id", table="customer_dim", nullable=False, is_primary_key=True),
        )
        source = _FakeDataSource({("public", "customer_dim"): live})

        with SQLiteStore(store_path) as store:
            plan1 = plan_dbt_import(manifest, source, store, source_connection_id=_SOURCE_ID)
            apply_dbt_import_plan(plan1, store, source_connection_id=_SOURCE_ID)

            plan2 = plan_dbt_import(manifest, source, store, source_connection_id=_SOURCE_ID)
            assert plan2.to_add == ()
            assert len(plan2.to_update) == 1
            result2 = apply_dbt_import_plan(plan2, store, source_connection_id=_SOURCE_ID)
            assert result2.write_failures == ()

            persisted = store.get_entity("customer_dim", source_connection_id=_SOURCE_ID)
        assert persisted is not None
        assert persisted.origin == "dbt_import"

    def test_updated_description_flows_through(self, tmp_path: Path) -> None:
        # Re-running with a changed manifest description must replace
        # the stored description (this is what makes import "live").
        store_path = _seed_store_with_table(tmp_path)
        first_model = _dbt_model(name="customer_dim", description="initial")
        live = _live_table(
            "customer_dim",
            _live_column("id", table="customer_dim", nullable=False, is_primary_key=True),
        )
        source = _FakeDataSource({("public", "customer_dim"): live})

        with SQLiteStore(store_path) as store:
            plan1 = plan_dbt_import(
                _manifest(first_model), source, store, source_connection_id=_SOURCE_ID
            )
            apply_dbt_import_plan(plan1, store, source_connection_id=_SOURCE_ID)

            second_model = _dbt_model(name="customer_dim", description="updated")
            plan2 = plan_dbt_import(
                _manifest(second_model), source, store, source_connection_id=_SOURCE_ID
            )
            apply_dbt_import_plan(plan2, store, source_connection_id=_SOURCE_ID)

            persisted = store.get_entity("customer_dim", source_connection_id=_SOURCE_ID)
        assert persisted is not None
        assert persisted.description == "updated"


class TestApplyOwnershipTransfer:
    def test_ownership_transfer_flips_origin_to_dbt(self, tmp_path: Path) -> None:
        store_path = _seed_store_with_table(tmp_path)
        _seed_existing_entity(
            store_path,
            "customer_dim",
            qualified_table="public.customer_dim",
            origin="manual",
        )

        model = _dbt_model(name="customer_dim", description="dbt-owned")
        manifest = _manifest(model)
        live = _live_table(
            "customer_dim",
            _live_column("id", table="customer_dim", nullable=False, is_primary_key=True),
        )
        source = _FakeDataSource({("public", "customer_dim"): live})

        with SQLiteStore(store_path) as store:
            plan = plan_dbt_import(manifest, source, store, source_connection_id=_SOURCE_ID)
            result = apply_dbt_import_plan(plan, store, source_connection_id=_SOURCE_ID)
            assert result.write_failures == ()

            persisted = store.get_entity("customer_dim", source_connection_id=_SOURCE_ID)
        assert persisted is not None
        assert persisted.origin == "dbt_import"
        assert persisted.description == "dbt-owned"


class TestApplyOrphans:
    def test_orphans_left_untouched(self, tmp_path: Path) -> None:
        # The driver does NOT auto-delete orphans at v1. Verify the
        # orphan entity is still present after apply.
        store_path = _seed_store_with_tables(tmp_path, ("customer_dim", "old_dim"))
        _seed_existing_entity(
            store_path,
            "old_dim",
            qualified_table="public.old_dim",
            origin="dbt_import",
        )

        model = _dbt_model(name="customer_dim")
        manifest = _manifest(model)
        live = _live_table(
            "customer_dim",
            _live_column("id", table="customer_dim", nullable=False, is_primary_key=True),
        )
        source = _FakeDataSource({("public", "customer_dim"): live})

        with SQLiteStore(store_path) as store:
            plan = plan_dbt_import(manifest, source, store, source_connection_id=_SOURCE_ID)
            apply_dbt_import_plan(plan, store, source_connection_id=_SOURCE_ID)
            orphan = store.get_entity("old_dim", source_connection_id=_SOURCE_ID)

        assert orphan is not None
        assert orphan.origin == "dbt_import"


# ----- apply: write failures ------------------------------------------------


class TestApplyWriteFailures:
    def test_missing_bound_table_surfaces_as_write_failure(self, tmp_path: Path) -> None:
        # If the bound table isn't in the store (user forgot to run
        # `schemabrain index` first), Store.write_entity raises
        # IntegrityError — the driver catches it per-entity and lists
        # it in write_failures, the rest of the apply continues.
        # NOTE: this scenario only arises if verify passed against a
        # DIFFERENT live source than the one the store was indexed
        # against — verify uses live tables, write_entity uses stored
        # tables. We simulate by seeding NO table in the store but
        # making verify happy with a live one.
        store_path = tmp_path / "store.db"
        SQLiteStore(store_path).close()  # creates the schema but no tables
        live = _live_table(
            "customer_dim",
            _live_column("id", table="customer_dim", nullable=False, is_primary_key=True),
        )
        source = _FakeDataSource({("public", "customer_dim"): live})

        model = _dbt_model(name="customer_dim")
        manifest = _manifest(model)

        with SQLiteStore(store_path) as store:
            plan = plan_dbt_import(manifest, source, store, source_connection_id=_SOURCE_ID)
            result = apply_dbt_import_plan(plan, store, source_connection_id=_SOURCE_ID)

        assert len(result.write_failures) == 1
        failure = result.write_failures[0]
        assert isinstance(failure, DbtWriteFailure)
        assert failure.entity_name == "customer_dim"
        # Catches sqlite3.IntegrityError (FK violation against `tables`)
        assert "table" in failure.message.lower() or "FK" in failure.message

    def test_one_failure_does_not_stop_other_writes(self, tmp_path: Path) -> None:
        # Two models — one with seeded table, one without. The good
        # one lands; the bad one surfaces as write_failure.
        store_path = _seed_store_with_tables(tmp_path, ("customer_dim",))

        good = _dbt_model(name="customer_dim")
        bad = _dbt_model(name="orders_fact")  # no table seeded
        manifest = _manifest(good, bad)
        live_tables = {
            ("public", "customer_dim"): _live_table(
                "customer_dim",
                _live_column("id", table="customer_dim", nullable=False, is_primary_key=True),
            ),
            ("public", "orders_fact"): _live_table(
                "orders_fact",
                _live_column("id", table="orders_fact", nullable=False, is_primary_key=True),
            ),
        }
        source = _FakeDataSource(live_tables)

        with SQLiteStore(store_path) as store:
            plan = plan_dbt_import(manifest, source, store, source_connection_id=_SOURCE_ID)
            result = apply_dbt_import_plan(plan, store, source_connection_id=_SOURCE_ID)
            good_persisted = store.get_entity("customer_dim", source_connection_id=_SOURCE_ID)
            bad_persisted = store.get_entity("orders_fact", source_connection_id=_SOURCE_ID)

        # Good entity wrote successfully
        assert good_persisted is not None
        # Bad entity didn't land
        assert bad_persisted is None
        # Failure was captured
        assert len(result.write_failures) == 1
        assert result.write_failures[0].entity_name == "orders_fact"

    def test_fk_failure_message_names_source_connection_id(self, tmp_path: Path) -> None:
        # A multi-source user re-indexing the wrong source is the
        # common cause of FK violations. The message must name the
        # source ID so the user knows which `schemabrain index`
        # invocation needs to re-run.
        store_path = tmp_path / "store.db"
        SQLiteStore(store_path).close()
        live = _live_table(
            "customer_dim",
            _live_column("id", table="customer_dim", nullable=False, is_primary_key=True),
        )
        source = _FakeDataSource({("public", "customer_dim"): live})

        model = _dbt_model(name="customer_dim")
        manifest = _manifest(model)

        with SQLiteStore(store_path) as store:
            plan = plan_dbt_import(manifest, source, store, source_connection_id=_SOURCE_ID)
            result = apply_dbt_import_plan(plan, store, source_connection_id=_SOURCE_ID)

        assert len(result.write_failures) == 1
        msg = result.write_failures[0].message
        assert _SOURCE_ID in msg
        assert "same source url" in msg.lower() or "same source-url" in msg.lower()

    def test_dbt_owned_entity_error_caught_as_write_failure(self, tmp_path: Path) -> None:
        # TOCTOU defense: between plan and apply, a concurrent writer
        # could flip a row to `origin=dbt_import`. The planner
        # classified the entity as `to_add` (existing was None) or
        # `to_take_ownership` (existing was manual/suggested), but
        # by apply time the row is now dbt_import — and the writer
        # in this test is also dbt_import, so the store guard ALLOWS
        # the write. To force a DbtOwnedEntityError at apply time,
        # we simulate the inverse: a concurrent process flips a row
        # to manual/suggested AFTER the planner classifies it as
        # to_take_ownership. To exercise the catch path directly,
        # we build a plan that mismatches the live store state.
        from schemabrain.core.entity import Entity, SingleTableBinding
        from schemabrain.imports.dbt import (
            DbtImportedEntity,
            DbtImportPlan,
            DbtSkipCounts,
        )

        store_path = _seed_store_with_table(tmp_path)
        # Pre-seed dbt_import row in store.
        _seed_existing_entity(
            store_path,
            "customer_dim",
            qualified_table="public.customer_dim",
            origin="dbt_import",
        )

        # Construct a plan that classifies "customer_dim" as
        # to_take_ownership with previous_origin=manual — a stale
        # plan reflecting the state BEFORE another writer made it
        # dbt_import. The envelope.entity has origin=manual to
        # trigger the store guard.
        stale_envelope = DbtImportedEntity(
            entity=Entity(
                name="customer_dim",
                description="stale plan",
                binding=SingleTableBinding(qualified_table="public.customer_dim"),
                identity="id",
                origin="manual",
            ),
            dbt_unique_id="model.demo.customer_dim",
            identity_resolution_method="primary_key_constraint",
            upstream_sources=(),
        )
        stale_plan = DbtImportPlan(
            dbt_project_name="demo_project",
            to_add=(),
            to_update=(),
            to_take_ownership=((stale_envelope, "manual"),),
            orphans=(),
            skipped=(),
            skip_counts=DbtSkipCounts(),
        )

        with SQLiteStore(store_path) as store:
            result = apply_dbt_import_plan(stale_plan, store, source_connection_id=_SOURCE_ID)

        # The guard fires; the write becomes a captured failure
        # (not an uncaught exception killing the apply).
        assert len(result.write_failures) == 1
        assert result.write_failures[0].entity_name == "customer_dim"
        assert "dbt_import" in result.write_failures[0].message.lower()


# ----- dataclass invariants -------------------------------------------------


class TestDataclassShapes:
    def test_dbt_import_skip_is_frozen(self) -> None:
        skip = DbtImportSkip(
            dbt_unique_id="model.demo.foo",
            reason="identity_resolution",
            message="no id",
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            skip.reason = "mapping"  # type: ignore[misc]

    def test_dbt_import_skip_rejects_invalid_reason(self) -> None:
        # `reason` is a closed Literal — the audit log buckets imports
        # by it. Typo → runtime validator catches.
        with pytest.raises(ValueError, match="reason"):
            DbtImportSkip(
                dbt_unique_id="model.demo.foo",
                reason="some_made_up_reason",  # type: ignore[arg-type]
                message="...",
            )

    def test_dbt_write_failure_is_frozen(self) -> None:
        failure = DbtWriteFailure(entity_name="customer", message="FK violation")
        with pytest.raises(dataclasses.FrozenInstanceError):
            failure.entity_name = "other"  # type: ignore[misc]

    def test_dbt_import_plan_is_frozen(self) -> None:
        plan = DbtImportPlan(
            dbt_project_name="demo",
            to_add=(),
            to_update=(),
            to_take_ownership=(),
            orphans=(),
            skipped=(),
            skip_counts=DbtSkipCounts(),
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            plan.orphans = ("x",)  # type: ignore[misc]
