"""Tests for live-schema verification of a dbt-imported entity.

Pins the fourth piece of the dbt import substrate. The verify helper
takes a `DbtImportedEntity` + the originating `DbtModelNode` + a
read-only `DataSource` (existing connector seam), and confirms that
the dbt manifest's claims about the model's physical materialization
hold against the running source database:

  1. The bound table exists in live schema (else live `get_table`
     raises `TableNotFoundError`, surfaced as `DbtSchemaDriftError`).
  2. Every column dbt declared in the manifest still exists in the
     live table. Extra columns in live are NOT drift — dbt is the
     curator of its own column list; live can carry unmodeled
     columns (e.g. dbt-managed audit columns added post-compile).
  3. The identity column is NOT NULL in the live schema. A nullable
     identity can't reliably anchor metrics; refuse before the
     entity lands in the store rather than discovering it at query
     time.

The verify step uses ZERO LLM cost — pure source-introspection. The
driver catches each `DbtSchemaDriftError` and adds the model to the
run summary's "skipped" bucket.
"""

from __future__ import annotations

import pytest

from schemabrain.connectors.errors import TableNotFoundError
from schemabrain.core.models import Column, Table
from schemabrain.imports.dbt import (
    DbtColumn,
    DbtColumnTests,
    DbtConstraint,
    DbtImportedEntity,
    DbtManifest,
    DbtModelNode,
    DbtSchemaDriftError,
    DbtSkipCounts,
    dbt_model_to_entity,
    verify_against_live_schema,
)

# ----- test doubles ---------------------------------------------------------


class _FakeDataSource:
    """Minimal `DataSource` shape — only what verify uses."""

    def __init__(self, tables: dict[tuple[str, str], Table]) -> None:
        self._tables = tables
        self.get_table_calls: list[tuple[str, str]] = []

    def list_tables(self, schema: str | None = None) -> list[tuple[str, str]]:
        # Unused by verify, but the Protocol requires it. Returning the
        # keys keeps `runtime_checkable` happy + lets us assert no
        # accidental list_tables calls leak into verify.
        return list(self._tables.keys())

    def get_table(self, name: str, schema: str) -> Table:
        self.get_table_calls.append((schema, name))
        try:
            return self._tables[(schema, name)]
        except KeyError as exc:
            raise TableNotFoundError(f"table {schema}.{name} not found") from exc

    def close(self) -> None:  # pragma: no cover — verify never closes
        pass


def _live_column(
    name: str,
    *,
    table: str = "customer_dim",
    schema: str = "public",
    data_type: str = "integer",
    nullable: bool = True,
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


def _live_table(*columns: Column, name: str = "customer_dim", schema: str = "public") -> Table:
    return Table(name=name, schema_name=schema, columns=columns, foreign_keys=())


def _dbt_column(name: str) -> DbtColumn:
    return DbtColumn(
        name=name,
        data_type=None,
        description="",
        constraints=(),
        tests=DbtColumnTests(),
    )


def _dbt_model(
    *,
    name: str = "customer_dim",
    schema: str = "public",
    identifier: str | None = None,
    columns: tuple[DbtColumn, ...] = (),
) -> DbtModelNode:
    pk = DbtColumn(
        name="id",
        data_type="integer",
        description="",
        constraints=(DbtConstraint(type="primary_key"),),
        tests=DbtColumnTests(),
    )
    return DbtModelNode(
        unique_id=f"model.demo.{name}",
        name=name,
        database="schemabrain_test",
        schema_name=schema,
        identifier=identifier or name,
        description="",
        columns=columns or (pk,),
        depends_on_sources=(),
    )


def _empty_manifest(*models: DbtModelNode) -> DbtManifest:
    return DbtManifest(
        manifest_version=12,
        dbt_project_name="demo",
        models=models,
        sources_by_id={},
        skipped=DbtSkipCounts(),
    )


def _import(model: DbtModelNode) -> DbtImportedEntity:
    """Convenience — run the step-3 mapper to get an envelope for verify."""
    return dbt_model_to_entity(model, _empty_manifest(model))


# ----- happy path -----------------------------------------------------------


class TestVerifyHappyPath:
    def test_matching_schema_returns_normally(self) -> None:
        model = _dbt_model()
        imported = _import(model)
        live = _live_table(
            _live_column("id", nullable=False, is_primary_key=True),
        )
        source = _FakeDataSource({("public", "customer_dim"): live})

        # No exception = success. Returning None keeps the side-effect-
        # free contract clean — callers don't pattern-match on a return.
        result = verify_against_live_schema(imported, model, source)
        assert result is None

    def test_extra_live_columns_not_in_dbt_are_not_drift(self) -> None:
        # dbt is the curator of its own column list. The live table
        # may carry unmodeled columns (e.g. dbt audit columns added
        # post-compile, or columns from an in-flight schema migration).
        # Extra columns in live ARE NOT drift — verify only enforces
        # the subset rule (dbt's columns must exist in live).
        model = _dbt_model()
        imported = _import(model)
        live = _live_table(
            _live_column("id", nullable=False, is_primary_key=True),
            _live_column("created_at", data_type="timestamp", nullable=False, ordinal=2),
            _live_column("updated_at", data_type="timestamp", nullable=True, ordinal=3),
        )
        source = _FakeDataSource({("public", "customer_dim"): live})

        verify_against_live_schema(imported, model, source)  # no raise

    def test_uses_compiled_identifier_not_dbt_name(self) -> None:
        # `alias` overrides the model name → live lookup must hit the
        # alias-named table, NOT the dbt name. Catches a regression
        # where verify would use model.name (logical) instead of
        # model.identifier (physical).
        model = _dbt_model(name="customer_dim", identifier="customer_dim_v2")
        imported = _import(model)
        live = _live_table(
            _live_column("id", table="customer_dim_v2", nullable=False),
            name="customer_dim_v2",
        )
        source = _FakeDataSource({("public", "customer_dim_v2"): live})

        verify_against_live_schema(imported, model, source)  # no raise
        # Confirms the lookup used the compiled identifier
        assert source.get_table_calls == [("public", "customer_dim_v2")]


# ----- drift cases ----------------------------------------------------------


class TestDriftCases:
    def test_missing_table_surfaces_as_drift_error(self) -> None:
        model = _dbt_model(name="customer_dim")
        imported = _import(model)
        # Empty source — table doesn't exist
        source = _FakeDataSource({})

        with pytest.raises(DbtSchemaDriftError) as exc_info:
            verify_against_live_schema(imported, model, source)
        msg = str(exc_info.value)
        # Names the qualified table the user can look up + the dbt
        # unique_id so they can correlate with their manifest.
        assert "public.customer_dim" in msg
        assert "model.demo.customer_dim" in msg

    def test_dbt_declared_column_missing_in_live(self) -> None:
        # dbt declares `id` + `email`; live only has `id`. Drift.
        model = _dbt_model(
            columns=(
                DbtColumn(
                    name="id",
                    data_type="integer",
                    description="",
                    constraints=(DbtConstraint(type="primary_key"),),
                    tests=DbtColumnTests(),
                ),
                _dbt_column("email"),
            ),
        )
        imported = _import(model)
        live = _live_table(
            _live_column("id", nullable=False, is_primary_key=True),
        )
        source = _FakeDataSource({("public", "customer_dim"): live})

        with pytest.raises(DbtSchemaDriftError) as exc_info:
            verify_against_live_schema(imported, model, source)
        # Names the missing column AND the table.
        assert "email" in str(exc_info.value)
        assert "public.customer_dim" in str(exc_info.value)

    def test_lists_all_missing_columns_at_once(self) -> None:
        # Drift report should be complete, not first-failure-only.
        # If three columns are missing, the error names all three so
        # the user can fix them in one round-trip.
        model = _dbt_model(
            columns=(
                DbtColumn(
                    name="id",
                    data_type="integer",
                    description="",
                    constraints=(DbtConstraint(type="primary_key"),),
                    tests=DbtColumnTests(),
                ),
                _dbt_column("email"),
                _dbt_column("phone"),
                _dbt_column("created_at"),
            ),
        )
        imported = _import(model)
        live = _live_table(
            _live_column("id", nullable=False, is_primary_key=True),
        )
        source = _FakeDataSource({("public", "customer_dim"): live})

        with pytest.raises(DbtSchemaDriftError) as exc_info:
            verify_against_live_schema(imported, model, source)
        msg = str(exc_info.value)
        assert "email" in msg
        assert "phone" in msg
        assert "created_at" in msg

    def test_identity_column_nullable_in_live_is_drift(self) -> None:
        # dbt's primary_key constraint is a declaration; live schema
        # may still allow NULLs (e.g. dbt's incremental materialization
        # didn't enforce a hard constraint). Refuse — a nullable
        # identity can't anchor metrics.
        model = _dbt_model()
        imported = _import(model)
        live = _live_table(
            _live_column("id", nullable=True),  # NULLABLE — drift!
        )
        source = _FakeDataSource({("public", "customer_dim"): live})

        with pytest.raises(DbtSchemaDriftError) as exc_info:
            verify_against_live_schema(imported, model, source)
        msg = str(exc_info.value)
        # Names the identity column AND the table.
        assert "id" in msg
        assert "public.customer_dim" in msg
        assert "nullable" in msg.lower() or "not null" in msg.lower()

    def test_identity_column_missing_in_live_is_drift(self) -> None:
        # The identity column not appearing in live at all is caught
        # by an explicit identity-not-in-live drift check + the
        # missing-columns path. Either path is sufficient; the
        # explicit identity check ensures an identity not in live
        # can never slip through (e.g. a constraint-only identity
        # not listed in the columns block).
        model = _dbt_model()
        imported = _import(model)
        live = _live_table(
            _live_column("email", nullable=False, ordinal=1),
        )
        source = _FakeDataSource({("public", "customer_dim"): live})

        with pytest.raises(DbtSchemaDriftError) as exc_info:
            verify_against_live_schema(imported, model, source)
        msg = str(exc_info.value)
        # Names both the identity column and the table.
        assert "id" in msg
        assert "public.customer_dim" in msg
        # Surfaces the metric-anchor concern so the user knows what
        # downstream functionality is at risk.
        assert "metric" in msg.lower() or "anchor" in msg.lower() or "query" in msg.lower()

    def test_identity_column_constraint_declared_but_not_in_columns_block(
        self,
    ) -> None:
        # Defense-in-depth: a programmatically-constructed envelope
        # whose Entity.identity references a column NOT in
        # `model.columns` (e.g. dbt allows declaring a primary_key
        # constraint without listing the column in the columns
        # block). The missing-columns check at the dbt-declared level
        # cannot catch this — the identity isn't in dbt_column_names.
        # The explicit identity-not-in-live check is the safety net.
        from schemabrain.core.entity import Entity, SingleTableBinding
        from schemabrain.imports.dbt import DbtImportedEntity

        # Construct envelope with identity="customer_uuid" — but the
        # underlying dbt model only declares an "email" column.
        model = DbtModelNode(
            unique_id="model.demo.customer_dim",
            name="customer_dim",
            database="schemabrain_test",
            schema_name="public",
            identifier="customer_dim",
            description="",
            columns=(_dbt_column("email"),),
            depends_on_sources=(),
        )
        envelope = DbtImportedEntity(
            entity=Entity(
                name="customer_dim",
                description="",
                binding=SingleTableBinding(qualified_table="public.customer_dim"),
                identity="customer_uuid",
                origin="dbt_import",
            ),
            dbt_unique_id=model.unique_id,
            identity_resolution_method="primary_key_constraint",
            upstream_sources=(),
        )
        # Live has the dbt-declared column (email) but NOT the
        # identity column (customer_uuid). The missing-columns check
        # at the dbt level passes (email is in live); the explicit
        # identity check must still fire.
        live = _live_table(
            _live_column("email", nullable=False, ordinal=1),
        )
        source = _FakeDataSource({("public", "customer_dim"): live})

        with pytest.raises(DbtSchemaDriftError) as exc_info:
            verify_against_live_schema(envelope, model, source)
        assert "customer_uuid" in str(exc_info.value)


# ----- error metadata -------------------------------------------------------


class TestErrorMessaging:
    def test_missing_table_error_suggests_dbt_run(self) -> None:
        # Guided error — most common cause of "table not found" after
        # a successful `dbt compile` is that the user hasn't `dbt run`
        # yet (the model is compiled but not materialized).
        model = _dbt_model()
        imported = _import(model)
        source = _FakeDataSource({})

        with pytest.raises(DbtSchemaDriftError) as exc_info:
            verify_against_live_schema(imported, model, source)
        assert "dbt run" in str(exc_info.value).lower()

    def test_missing_columns_error_suggests_dbt_run(self) -> None:
        # Same guided message applies to column-level drift — likely
        # the YAML is ahead of the materialization.
        model = _dbt_model(columns=(_dbt_column("id"), _dbt_column("email")))
        # `id` lacks PK constraint so identity resolution would fail;
        # use tier-3 tests instead so we hit the column-drift path.
        model = DbtModelNode(
            unique_id="model.demo.customer_dim",
            name="customer_dim",
            database="schemabrain_test",
            schema_name="public",
            identifier="customer_dim",
            description="",
            columns=(
                DbtColumn(
                    name="id",
                    data_type=None,
                    description="",
                    constraints=(),
                    tests=DbtColumnTests(is_unique=True, is_not_null=True),
                ),
                _dbt_column("email"),
            ),
            depends_on_sources=(),
        )
        imported = _import(model)
        live = _live_table(_live_column("id", nullable=False))
        source = _FakeDataSource({("public", "customer_dim"): live})

        with pytest.raises(DbtSchemaDriftError) as exc_info:
            verify_against_live_schema(imported, model, source)
        assert "dbt run" in str(exc_info.value).lower()


# ----- introspection efficiency --------------------------------------------


class TestIntrospectionEfficiency:
    def test_calls_get_table_exactly_once(self) -> None:
        # Verify performs ONE source round-trip per model. Multiple
        # calls would burn connection time on large dbt projects and
        # mask N+1-style regressions.
        model = _dbt_model()
        imported = _import(model)
        live = _live_table(
            _live_column("id", nullable=False, is_primary_key=True),
        )
        source = _FakeDataSource({("public", "customer_dim"): live})

        verify_against_live_schema(imported, model, source)
        assert len(source.get_table_calls) == 1
