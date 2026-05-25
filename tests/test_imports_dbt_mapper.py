"""Tests for the dbt model → SchemaBrain Entity mapper.

Pins the third piece of the dbt import substrate. The mapper combines
the step-1 parser output (a `DbtModelNode`) with the step-2 identity
resolver (`resolve_dbt_identity`) to produce a `DbtImportedEntity` —
an envelope carrying:

  - The persisted-clean `Entity` (origin="dbt_import")
  - The dbt unique_id (for audit-log provenance)
  - The identity-resolution method (for audit-log bucketing)
  - The qualified upstream source-table names this model depends on

Mirrors the `EntityCandidate` envelope pattern from the LLM-suggest
pipeline: transient import-time metadata rides on the wrapper, the
persisted `Entity` itself stays clean. The wrapper's metadata flows
into the audit-log row, not into the entity YAML on disk.

Validation failures of any kind — bad identifier shape, bad
qualified-table shape, identity-resolution refusal — surface as
`DbtEntityMappingError` (for shape issues) or
`DbtIdentityResolutionError` (for identity issues). The driver layer
layer catches per-model and surfaces in the run summary.
"""

from __future__ import annotations

import dataclasses

import pytest

from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.imports.dbt import (
    DbtColumn,
    DbtColumnTests,
    DbtConstraint,
    DbtEntityMappingError,
    DbtIdentityResolutionError,
    DbtImportedEntity,
    DbtManifest,
    DbtModelNode,
    DbtSkipCounts,
    DbtSourceNode,
    dbt_model_to_entity,
)


def _pk_id_column() -> DbtColumn:
    """The default identity column for fixtures — `id` with primary_key."""
    return DbtColumn(
        name="id",
        data_type="integer",
        description="",
        constraints=(DbtConstraint(type="primary_key"),),
        tests=DbtColumnTests(),
    )


def _model(
    *,
    name: str = "customer_dim",
    project: str = "demo_project",
    database: str = "schemabrain_test",
    schema: str = "public",
    identifier: str | None = None,
    description: str = "",
    columns: tuple[DbtColumn, ...] = (),
    depends_on_sources: tuple[str, ...] = (),
) -> DbtModelNode:
    return DbtModelNode(
        unique_id=f"model.{project}.{name}",
        name=name,
        database=database,
        schema_name=schema,
        identifier=identifier or name,
        description=description,
        columns=columns or (_pk_id_column(),),
        depends_on_sources=depends_on_sources,
    )


def _manifest(
    *,
    models: tuple[DbtModelNode, ...] = (),
    sources: tuple[DbtSourceNode, ...] = (),
) -> DbtManifest:
    return DbtManifest(
        manifest_version=12,
        dbt_project_name="demo_project",
        models=models,
        sources_by_id={s.unique_id: s for s in sources},
        skipped=DbtSkipCounts(),
    )


# ----- happy path -----------------------------------------------------------


class TestMapHappyPath:
    def test_maps_minimal_model_to_entity(self) -> None:
        model = _model(name="customer_dim", description="Customer dimension")
        manifest = _manifest(models=(model,))

        result = dbt_model_to_entity(model, manifest)

        assert isinstance(result, DbtImportedEntity)
        assert isinstance(result.entity, Entity)
        assert result.entity.name == "customer_dim"
        assert result.entity.description == "Customer dimension"
        assert result.entity.origin == "dbt_import"

    def test_qualified_table_built_from_schema_and_identifier(self) -> None:
        # The binding uses the COMPILED physical table — schema +
        # `alias` (which is `identifier` after step-1 parsing). The
        # dbt model name and the physical table name can diverge
        # (alias overrides name).
        model = _model(name="customer_dim", schema="analytics", identifier="customer_dim_v2")
        manifest = _manifest(models=(model,))

        result = dbt_model_to_entity(model, manifest)

        assert isinstance(result.entity.binding, SingleTableBinding)
        assert result.entity.binding.qualified_table == "analytics.customer_dim_v2"

    def test_identity_carried_through_from_resolver(self) -> None:
        # Identity column resolved by step-2 logic flows into the
        # Entity's `identity` field.
        model = _model(
            columns=(
                DbtColumn(
                    name="customer_id",
                    data_type="integer",
                    description="",
                    constraints=(DbtConstraint(type="primary_key"),),
                    tests=DbtColumnTests(),
                ),
                DbtColumn(
                    name="email",
                    data_type="text",
                    description="",
                    constraints=(),
                    tests=DbtColumnTests(),
                ),
            ),
        )
        manifest = _manifest(models=(model,))

        result = dbt_model_to_entity(model, manifest)
        assert result.entity.identity == "customer_id"

    def test_dbt_unique_id_captured_on_envelope(self) -> None:
        # Audit-log surface: dbt_unique_id is what an operator
        # cross-references against `dbt ls` output to find the upstream
        # model.
        model = _model(name="customer_dim", project="my_project")
        manifest = _manifest(models=(model,))

        result = dbt_model_to_entity(model, manifest)
        assert result.dbt_unique_id == "model.my_project.customer_dim"

    def test_identity_resolution_method_captured(self) -> None:
        # The audit-log row records which priority tier matched.
        model = _model()  # uses _pk_id_column() — tier 1
        manifest = _manifest(models=(model,))

        result = dbt_model_to_entity(model, manifest)
        assert result.identity_resolution_method == "primary_key_constraint"


# ----- upstream source resolution -------------------------------------------


class TestUpstreamSourceResolution:
    def test_resolves_one_upstream_source(self) -> None:
        source = DbtSourceNode(
            unique_id="source.demo_project.raw.users",
            source_name="raw",
            name="users",
            database="schemabrain_test",
            schema_name="raw",
            identifier="users",
        )
        model = _model(name="customer_dim", depends_on_sources=("source.demo_project.raw.users",))
        manifest = _manifest(models=(model,), sources=(source,))

        result = dbt_model_to_entity(model, manifest)
        # Sources are recorded as qualified table names (schema.identifier),
        # not as opaque dbt unique IDs. SchemaBrain's domain is the
        # physical tables.
        assert result.upstream_sources == ("raw.users",)

    def test_resolves_multiple_upstream_sources_in_input_order(self) -> None:
        source_a = DbtSourceNode(
            unique_id="source.demo_project.raw.users",
            source_name="raw",
            name="users",
            database="schemabrain_test",
            schema_name="raw",
            identifier="users",
        )
        source_b = DbtSourceNode(
            unique_id="source.demo_project.raw.events",
            source_name="raw",
            name="events",
            database="schemabrain_test",
            schema_name="raw",
            identifier="events_archive",
        )
        model = _model(
            depends_on_sources=(
                "source.demo_project.raw.users",
                "source.demo_project.raw.events",
            ),
        )
        manifest = _manifest(models=(model,), sources=(source_a, source_b))

        result = dbt_model_to_entity(model, manifest)
        # Preserves input order — depends_on is already deterministic
        # in the manifest, and reordering would mask lineage drift in
        # the audit log.
        assert result.upstream_sources == ("raw.users", "raw.events_archive")

    def test_no_upstream_sources_yields_empty_tuple(self) -> None:
        # A model with no source dependencies (e.g. a pure-aggregate
        # from other models) produces an empty upstream tuple.
        model = _model(depends_on_sources=())
        manifest = _manifest(models=(model,))

        result = dbt_model_to_entity(model, manifest)
        assert result.upstream_sources == ()

    def test_unknown_source_ids_silently_dropped(self) -> None:
        # A `depends_on_sources` ID that has no entry in
        # `manifest.sources_by_id` indicates a malformed dbt manifest
        # (the parser should have caught it, but partial-build states
        # exist). Silently drop rather than crashing — better to import
        # the model without the missing-source provenance than to fail
        # the whole entity.
        model = _model(
            depends_on_sources=("source.demo_project.raw.does_not_exist",),
        )
        manifest = _manifest(models=(model,))  # no sources at all

        result = dbt_model_to_entity(model, manifest)
        assert result.upstream_sources == ()


# ----- error propagation ----------------------------------------------------


class TestErrorPropagation:
    def test_identity_resolution_failure_propagates(self) -> None:
        # No identity-tier match → DbtIdentityResolutionError surfaces
        # to the caller unwrapped. The driver layer (step 5) is the
        # natural catch site — it skips the model and adds it to the
        # run summary.
        model = _model(
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
        manifest = _manifest(models=(model,))

        with pytest.raises(DbtIdentityResolutionError):
            dbt_model_to_entity(model, manifest)

    def test_invalid_entity_name_wraps_as_mapping_error(self) -> None:
        # If dbt's model name contains characters Entity rejects (the
        # Entity dataclass enforces Postgres unquoted-identifier shape),
        # surface as a `DbtEntityMappingError` naming the dbt model so
        # the user knows which YAML to fix.
        model = _model(name="customer-dim")  # hyphen rejected by Entity
        manifest = _manifest(models=(model,))

        with pytest.raises(DbtEntityMappingError) as exc_info:
            dbt_model_to_entity(model, manifest)
        # Names the dbt unique_id so the user can cross-reference
        # against `dbt ls`.
        assert "model.demo_project.customer-dim" in str(exc_info.value)

    def test_invalid_schema_or_identifier_wraps_as_mapping_error(self) -> None:
        # If schema.identifier doesn't satisfy the qualified-table regex
        # (e.g. schema is empty), Entity construction raises ValueError
        # at the binding layer — wrap with dbt context.
        model = _model(name="customer_dim", schema="", identifier="users")
        manifest = _manifest(models=(model,))

        with pytest.raises(DbtEntityMappingError):
            dbt_model_to_entity(model, manifest)


# ----- DbtImportedEntity dataclass invariants -------------------------------


class TestDbtImportedEntityShape:
    def test_is_frozen(self) -> None:
        model = _model()
        manifest = _manifest(models=(model,))

        result = dbt_model_to_entity(model, manifest)
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.dbt_unique_id = "other"  # type: ignore[misc]

    def test_upstream_sources_is_a_tuple(self) -> None:
        # Tuple, not list — keeps the dataclass hashable and the
        # frozen invariant honest.
        model = _model()
        manifest = _manifest(models=(model,))

        result = dbt_model_to_entity(model, manifest)
        assert isinstance(result.upstream_sources, tuple)

    def test_two_equal_mappings_compare_equal(self) -> None:
        # Determinism: same dbt model + same manifest → equal envelope.
        model = _model()
        manifest = _manifest(models=(model,))
        a = dbt_model_to_entity(model, manifest)
        b = dbt_model_to_entity(model, manifest)
        assert a == b

    def test_rejects_invalid_identity_resolution_method(self) -> None:
        # Defense-in-depth: the upstream `DbtIdentityResolution`
        # constructor already validates the method label, but direct
        # construction of `DbtImportedEntity` (in tests, in future
        # callers) bypasses that guard. The envelope must enforce the
        # same closed-Literal check so the audit log can never carry
        # an unknown method value.
        with pytest.raises(ValueError, match="identity_resolution_method"):
            DbtImportedEntity(
                entity=Entity(
                    name="customer_dim",
                    description="",
                    binding=SingleTableBinding(qualified_table="public.customer_dim"),
                    identity="id",
                    origin="dbt_import",
                ),
                dbt_unique_id="model.demo.customer_dim",
                identity_resolution_method="some_made_up_method",  # type: ignore[arg-type]
                upstream_sources=(),
            )
