"""Tests for the dbt manifest.json parser — pure JSON → typed shape.

Pins the foundational substrate of the dbt import write-path:

  - `DbtManifest` / `DbtModelNode` / `DbtSourceNode` / `DbtColumn` /
    `DbtConstraint` / `DbtColumnTests` / `DbtSkipCounts` dataclass shapes
  - `parse_dbt_manifest(path)` — file → `DbtManifest` with model nodes
    filtered, source nodes indexed by unique_id, tests aggregated
    per-column, non-model resource types counted in `DbtSkipCounts`
  - Manifest schema version detection from `metadata.dbt_schema_version`
    URL — we accept v10+ (the version that introduced the `constraints`
    syntax). Older versions raise `DbtManifestParseError` with a guided
    error explaining how to re-run `dbt compile`.

Identity resolution (which combines the column constraints + aggregated
tests captured here into a single `identity` choice) lands in step 2.
This file is the parsing seam only.

All test manifests are inline JSON strings — no fixtures-on-disk
dependency for the parser unit. The bundled real-dbt manifest fixture
arrives in step 7.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from schemabrain.imports.dbt import (
    DbtColumnTests,
    DbtConstraint,
    DbtManifest,
    DbtManifestParseError,
    DbtModelNode,
    DbtSkipCounts,
    DbtSourceNode,
    parse_dbt_manifest,
)

# ----- helpers --------------------------------------------------------------


def _write_manifest(tmp_path: Path, body: dict) -> Path:
    """Serialize a manifest dict and write it under tmp_path."""
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(body))
    return path


def _minimal_metadata(version: int = 12, project: str = "demo_project") -> dict:
    """Build the metadata block dbt emits — schema URL + project name."""
    return {
        "dbt_schema_version": f"https://schemas.getdbt.com/dbt/manifest/v{version}.json",
        "dbt_version": "1.8.0",
        "project_name": project,
        "adapter_type": "postgres",
    }


def _model_node(
    *,
    name: str,
    project: str = "demo_project",
    database: str = "schemabrain_test",
    schema: str = "public",
    identifier: str | None = None,
    description: str = "",
    columns: dict[str, dict] | None = None,
    depends_on_nodes: list[str] | None = None,
) -> dict:
    """Build a dbt model-node entry as it appears under `manifest.nodes`."""
    unique_id = f"model.{project}.{name}"
    return unique_id, {
        "resource_type": "model",
        "unique_id": unique_id,
        "name": name,
        "database": database,
        "schema": schema,
        "alias": identifier or name,
        "description": description,
        "columns": columns or {},
        "depends_on": {"nodes": depends_on_nodes or []},
    }


def _source_entry(
    *,
    source_name: str,
    name: str,
    project: str = "demo_project",
    database: str = "schemabrain_test",
    schema: str | None = None,
    identifier: str | None = None,
) -> dict:
    """Build a dbt source entry as it appears under `manifest.sources`."""
    unique_id = f"source.{project}.{source_name}.{name}"
    return unique_id, {
        "unique_id": unique_id,
        "source_name": source_name,
        "name": name,
        "database": database,
        "schema": schema or source_name,
        "identifier": identifier or name,
    }


def _test_node(
    *,
    test_name: str,
    attached_unique_id: str,
    column_name: str,
    project: str = "demo_project",
) -> dict:
    """Build a dbt test node — separate from its target model under `nodes`."""
    # dbt test unique_ids include a content hash; we use a stable suffix for
    # determinism in tests. The parser must not rely on the suffix shape.
    unique_id = (
        f"test.{project}.{test_name}_{attached_unique_id.replace('.', '_')}_{column_name}.abc"
    )
    return unique_id, {
        "resource_type": "test",
        "unique_id": unique_id,
        "name": test_name,
        "test_metadata": {"name": test_name},
        "column_name": column_name,
        "attached_node": attached_unique_id,
    }


# ----- DbtManifest happy-path -----------------------------------------------


class TestParseHappyPath:
    def test_parses_minimal_manifest_with_one_model(self, tmp_path: Path) -> None:
        unique_id, node = _model_node(
            name="customer_dim",
            description="Customer dimension model",
            columns={
                "id": {
                    "name": "id",
                    "data_type": "integer",
                    "description": "Primary key",
                    "constraints": [{"type": "primary_key"}],
                },
                "email": {
                    "name": "email",
                    "data_type": "text",
                    "description": "Email",
                    "constraints": [],
                },
            },
        )
        manifest_path = _write_manifest(
            tmp_path,
            {
                "metadata": _minimal_metadata(),
                "nodes": {unique_id: node},
                "sources": {},
            },
        )

        manifest = parse_dbt_manifest(manifest_path)

        assert isinstance(manifest, DbtManifest)
        assert manifest.manifest_version == 12
        assert manifest.dbt_project_name == "demo_project"
        assert len(manifest.models) == 1
        assert manifest.models[0].name == "customer_dim"
        assert manifest.models[0].description == "Customer dimension model"
        assert manifest.models[0].database == "schemabrain_test"
        assert manifest.models[0].schema_name == "public"
        assert manifest.models[0].identifier == "customer_dim"

    def test_returns_models_as_tuple_not_list(self, tmp_path: Path) -> None:
        # Frozen-dataclass discipline: collections must be tuples so the
        # manifest stays hashable + immutable. A list would mutate under
        # the caller's nose.
        unique_id, node = _model_node(name="customer_dim")
        manifest_path = _write_manifest(
            tmp_path,
            {
                "metadata": _minimal_metadata(),
                "nodes": {unique_id: node},
                "sources": {},
            },
        )

        manifest = parse_dbt_manifest(manifest_path)
        assert isinstance(manifest.models, tuple)
        for model in manifest.models:
            assert isinstance(model.columns, tuple)
            assert isinstance(model.depends_on_sources, tuple)

    def test_parses_column_constraints(self, tmp_path: Path) -> None:
        unique_id, node = _model_node(
            name="customer_dim",
            columns={
                "id": {
                    "name": "id",
                    "data_type": "integer",
                    "constraints": [{"type": "primary_key"}],
                },
                "email": {
                    "name": "email",
                    "constraints": [
                        {"type": "not_null"},
                        {"type": "unique", "name": "uq_customer_dim_email"},
                    ],
                },
            },
        )
        manifest_path = _write_manifest(
            tmp_path,
            {
                "metadata": _minimal_metadata(),
                "nodes": {unique_id: node},
                "sources": {},
            },
        )

        manifest = parse_dbt_manifest(manifest_path)
        by_name = {c.name: c for c in manifest.models[0].columns}

        assert by_name["id"].constraints == (DbtConstraint(type="primary_key", name=None),)
        assert by_name["email"].constraints == (
            DbtConstraint(type="not_null", name=None),
            DbtConstraint(type="unique", name="uq_customer_dim_email"),
        )

    def test_parses_column_description_defaults_to_empty_string(self, tmp_path: Path) -> None:
        # dbt allows omitting `description` per-column; the parser should
        # not require it. Empty string is the canonical default — matches
        # SchemaBrain's Entity dataclass shape.
        unique_id, node = _model_node(
            name="customer_dim",
            columns={"id": {"name": "id"}},
        )
        manifest_path = _write_manifest(
            tmp_path,
            {
                "metadata": _minimal_metadata(),
                "nodes": {unique_id: node},
                "sources": {},
            },
        )

        manifest = parse_dbt_manifest(manifest_path)
        assert manifest.models[0].columns[0].description == ""

    def test_parses_column_data_type_defaults_to_none(self, tmp_path: Path) -> None:
        # dbt allows columns without declared `data_type`. None is the
        # honest signal — the live-schema verify step (step 4) will
        # populate from the actual database.
        unique_id, node = _model_node(
            name="customer_dim",
            columns={"id": {"name": "id"}},
        )
        manifest_path = _write_manifest(
            tmp_path,
            {
                "metadata": _minimal_metadata(),
                "nodes": {unique_id: node},
                "sources": {},
            },
        )

        manifest = parse_dbt_manifest(manifest_path)
        assert manifest.models[0].columns[0].data_type is None

    def test_alias_overrides_name_for_identifier(self, tmp_path: Path) -> None:
        # The physical table name dbt writes to is `alias` if set, else
        # `name`. SchemaBrain binds to the physical table, so we use
        # `alias` (the `identifier` field on DbtModelNode).
        unique_id, node = _model_node(
            name="customer_dim",
            identifier="customer_dimension_v2",
        )
        manifest_path = _write_manifest(
            tmp_path,
            {
                "metadata": _minimal_metadata(),
                "nodes": {unique_id: node},
                "sources": {},
            },
        )

        manifest = parse_dbt_manifest(manifest_path)
        # `name` is the dbt model name (logical); `identifier` is the
        # physical table — what we'll bind to.
        assert manifest.models[0].name == "customer_dim"
        assert manifest.models[0].identifier == "customer_dimension_v2"


# ----- DbtSourceNode + depends_on resolution --------------------------------


class TestSourcesAndDependsOn:
    def test_sources_indexed_by_unique_id(self, tmp_path: Path) -> None:
        source_id, source = _source_entry(source_name="raw", name="users")
        manifest_path = _write_manifest(
            tmp_path,
            {
                "metadata": _minimal_metadata(),
                "nodes": {},
                "sources": {source_id: source},
            },
        )

        manifest = parse_dbt_manifest(manifest_path)
        assert source_id in manifest.sources_by_id
        node = manifest.sources_by_id[source_id]
        assert isinstance(node, DbtSourceNode)
        assert node.source_name == "raw"
        assert node.name == "users"
        assert node.database == "schemabrain_test"
        assert node.schema_name == "raw"
        assert node.identifier == "users"

    def test_model_depends_on_sources_carries_through(self, tmp_path: Path) -> None:
        source_id, source = _source_entry(source_name="raw", name="users")
        model_id, model = _model_node(
            name="customer_dim",
            depends_on_nodes=[source_id, "model.demo_project.upstream"],
        )
        manifest_path = _write_manifest(
            tmp_path,
            {
                "metadata": _minimal_metadata(),
                "nodes": {model_id: model},
                "sources": {source_id: source},
            },
        )

        manifest = parse_dbt_manifest(manifest_path)
        # Only source.* IDs are captured as `depends_on_sources`. Model
        # refs are out of scope for v1 — they're for lineage, not
        # provenance, and SchemaBrain doesn't yet model model→model
        # lineage.
        assert manifest.models[0].depends_on_sources == (source_id,)

    def test_sources_mapping_is_readonly(self, tmp_path: Path) -> None:
        # `sources_by_id` is a Mapping (not a mutable dict) so a caller
        # can't poison the index after construction. Matches the
        # discipline used by `pii_hints` on the LLM-suggest envelope.
        source_id, source = _source_entry(source_name="raw", name="users")
        manifest_path = _write_manifest(
            tmp_path,
            {
                "metadata": _minimal_metadata(),
                "nodes": {},
                "sources": {source_id: source},
            },
        )

        manifest = parse_dbt_manifest(manifest_path)
        with pytest.raises(TypeError):
            manifest.sources_by_id[source_id] = source  # type: ignore[index]


# ----- DbtColumnTests aggregation ------------------------------------------


class TestColumnTestAggregation:
    def test_aggregates_unique_test_per_column(self, tmp_path: Path) -> None:
        model_id, model = _model_node(
            name="customer_dim",
            columns={"id": {"name": "id"}},
        )
        test_id, test_node = _test_node(
            test_name="unique",
            attached_unique_id=model_id,
            column_name="id",
        )
        manifest_path = _write_manifest(
            tmp_path,
            {
                "metadata": _minimal_metadata(),
                "nodes": {model_id: model, test_id: test_node},
                "sources": {},
            },
        )

        manifest = parse_dbt_manifest(manifest_path)
        col = manifest.models[0].columns[0]
        assert col.tests == DbtColumnTests(is_unique=True, is_not_null=False)

    def test_aggregates_unique_plus_not_null_for_identity_eligible_column(
        self, tmp_path: Path
    ) -> None:
        # The identity-tier-3 case: a column with both
        # `unique` and `not_null` tests qualifies as an identity in the
        # absence of explicit constraints.
        model_id, model = _model_node(
            name="customer_dim",
            columns={"id": {"name": "id"}},
        )
        unique_id, unique_test = _test_node(
            test_name="unique", attached_unique_id=model_id, column_name="id"
        )
        not_null_id, not_null_test = _test_node(
            test_name="not_null", attached_unique_id=model_id, column_name="id"
        )
        manifest_path = _write_manifest(
            tmp_path,
            {
                "metadata": _minimal_metadata(),
                "nodes": {
                    model_id: model,
                    unique_id: unique_test,
                    not_null_id: not_null_test,
                },
                "sources": {},
            },
        )

        manifest = parse_dbt_manifest(manifest_path)
        col = manifest.models[0].columns[0]
        assert col.tests == DbtColumnTests(is_unique=True, is_not_null=True)

    def test_unrelated_tests_are_ignored(self, tmp_path: Path) -> None:
        # Tests other than `unique` / `not_null` (e.g. `accepted_values`,
        # `relationships`) don't feed identity resolution. They're noise
        # for the parser and must not silently become identity signals.
        model_id, model = _model_node(
            name="customer_dim",
            columns={"id": {"name": "id"}},
        )
        accepted_id, accepted_test = _test_node(
            test_name="accepted_values", attached_unique_id=model_id, column_name="id"
        )
        manifest_path = _write_manifest(
            tmp_path,
            {
                "metadata": _minimal_metadata(),
                "nodes": {model_id: model, accepted_id: accepted_test},
                "sources": {},
            },
        )

        manifest = parse_dbt_manifest(manifest_path)
        col = manifest.models[0].columns[0]
        assert col.tests == DbtColumnTests(is_unique=False, is_not_null=False)

    def test_tests_for_unknown_model_are_silently_dropped(self, tmp_path: Path) -> None:
        # A test attached to a model that isn't in the manifest (could
        # happen mid-development when dbt projects are partially built)
        # must NOT crash the parser. The orphan test is dropped.
        test_id, test_node = _test_node(
            test_name="unique",
            attached_unique_id="model.demo_project.does_not_exist",
            column_name="id",
        )
        manifest_path = _write_manifest(
            tmp_path,
            {
                "metadata": _minimal_metadata(),
                "nodes": {test_id: test_node},
                "sources": {},
            },
        )

        manifest = parse_dbt_manifest(manifest_path)
        assert manifest.models == ()

    def test_test_node_without_attached_node_or_column_is_ignored(self, tmp_path: Path) -> None:
        # Generic tests (e.g. dbt's `dbt_utils.equal_rowcount`) attach
        # to a model but carry no `column_name`; some custom tests
        # carry no `attached_node` at all. Either shape must NOT crash
        # the parser — drop the test silently rather than letting it
        # poison a (model, "") or ("", col) aggregate key.
        model_id, model = _model_node(name="customer_dim", columns={"id": {"name": "id"}})
        manifest_path = _write_manifest(
            tmp_path,
            {
                "metadata": _minimal_metadata(),
                "nodes": {
                    model_id: model,
                    "test.demo_project.generic_no_column": {
                        "resource_type": "test",
                        "unique_id": "test.demo_project.generic_no_column",
                        "name": "unique",
                        "test_metadata": {"name": "unique"},
                        "attached_node": model_id,
                        # column_name omitted on purpose
                    },
                    "test.demo_project.no_attached_node": {
                        "resource_type": "test",
                        "unique_id": "test.demo_project.no_attached_node",
                        "name": "unique",
                        "test_metadata": {"name": "unique"},
                        "column_name": "id",
                        # attached_node omitted on purpose
                    },
                },
                "sources": {},
            },
        )

        manifest = parse_dbt_manifest(manifest_path)
        col = manifest.models[0].columns[0]
        # Both bogus tests were dropped; the model's column stays at
        # the default (no unique, no not_null).
        assert col.tests == DbtColumnTests(is_unique=False, is_not_null=False)

    def test_tests_default_to_zero_aggregates(self, tmp_path: Path) -> None:
        # A column with no `test.*` nodes attached defaults to
        # DbtColumnTests(False, False). Empty test set is the common case.
        model_id, model = _model_node(name="customer_dim", columns={"id": {"name": "id"}})
        manifest_path = _write_manifest(
            tmp_path,
            {
                "metadata": _minimal_metadata(),
                "nodes": {model_id: model},
                "sources": {},
            },
        )

        manifest = parse_dbt_manifest(manifest_path)
        col = manifest.models[0].columns[0]
        assert col.tests == DbtColumnTests(is_unique=False, is_not_null=False)


# ----- Node-type filter + skip counts ---------------------------------------


class TestNodeTypeFilter:
    def test_skip_counts_track_each_resource_type(self, tmp_path: Path) -> None:
        # The skip counts surface in `DbtSkipCounts` so the CLI can print
        # an end-of-run breadcrumb naming what was deferred / explicitly
        # out of scope at v1.
        model_id, model = _model_node(name="customer_dim")
        manifest_path = _write_manifest(
            tmp_path,
            {
                "metadata": _minimal_metadata(),
                "nodes": {
                    model_id: model,
                    "metric.demo_project.revenue": {
                        "resource_type": "metric",
                        "unique_id": "metric.demo_project.revenue",
                    },
                    "snapshot.demo_project.users_snap": {
                        "resource_type": "snapshot",
                        "unique_id": "snapshot.demo_project.users_snap",
                    },
                    "seed.demo_project.countries": {
                        "resource_type": "seed",
                        "unique_id": "seed.demo_project.countries",
                    },
                    "analysis.demo_project.ad_hoc": {
                        "resource_type": "analysis",
                        "unique_id": "analysis.demo_project.ad_hoc",
                    },
                    "operation.demo_project.on-run-end.0": {
                        "resource_type": "operation",
                        "unique_id": "operation.demo_project.on-run-end.0",
                    },
                    "exposure.demo_project.dashboard": {
                        "resource_type": "exposure",
                        "unique_id": "exposure.demo_project.dashboard",
                    },
                },
                "sources": {},
            },
        )

        manifest = parse_dbt_manifest(manifest_path)
        assert manifest.skipped == DbtSkipCounts(
            metrics=1, snapshots=1, seeds=1, analyses=1, operations=1, exposures=1
        )

    def test_unknown_resource_types_count_in_other_bucket(self, tmp_path: Path) -> None:
        # Future dbt versions introduce new resource_type values (e.g.
        # `semantic_model` in dbt 1.6+, `unit_test` in 1.8+). The
        # parser must NOT crash AND must surface the count via the
        # `other` bucket — silent truncation would mask upgrade-time
        # regressions where new node types vanish without trace.
        model_id, model = _model_node(name="customer_dim")
        manifest_path = _write_manifest(
            tmp_path,
            {
                "metadata": _minimal_metadata(),
                "nodes": {
                    model_id: model,
                    "semantic_model.demo_project.customers": {
                        "resource_type": "semantic_model",
                        "unique_id": "semantic_model.demo_project.customers",
                    },
                    "unit_test.demo_project.foo": {
                        "resource_type": "unit_test",
                        "unique_id": "unit_test.demo_project.foo",
                    },
                },
                "sources": {},
            },
        )

        manifest = parse_dbt_manifest(manifest_path)
        assert len(manifest.models) == 1
        # Both unknowns land in `other` so the upgrade-time count is
        # surfaceable in the CLI breadcrumb.
        assert manifest.skipped.other == 2

    def test_only_models_appear_in_models_tuple(self, tmp_path: Path) -> None:
        # Concrete invariant: nothing other than resource_type=="model"
        # makes it into `manifest.models`. Sources go to `sources_by_id`;
        # tests get folded into column aggregates; everything else is
        # counted in `DbtSkipCounts`.
        model_id, model = _model_node(name="customer_dim")
        source_id, source = _source_entry(source_name="raw", name="users")
        manifest_path = _write_manifest(
            tmp_path,
            {
                "metadata": _minimal_metadata(),
                "nodes": {
                    model_id: model,
                    "metric.demo_project.revenue": {
                        "resource_type": "metric",
                        "unique_id": "metric.demo_project.revenue",
                    },
                },
                "sources": {source_id: source},
            },
        )

        manifest = parse_dbt_manifest(manifest_path)
        names = [m.name for m in manifest.models]
        assert names == ["customer_dim"]


# ----- Manifest version validation ------------------------------------------


class TestManifestVersionValidation:
    def test_accepts_v10(self, tmp_path: Path) -> None:
        # v10 is the minimum supported version — first dbt schema with
        # the `constraints` syntax used by identity resolution tier 1.
        manifest_path = _write_manifest(
            tmp_path,
            {
                "metadata": _minimal_metadata(version=10),
                "nodes": {},
                "sources": {},
            },
        )

        manifest = parse_dbt_manifest(manifest_path)
        assert manifest.manifest_version == 10

    def test_accepts_v11_and_v12(self, tmp_path: Path) -> None:
        for version in (11, 12):
            manifest_path = _write_manifest(
                tmp_path,
                {
                    "metadata": _minimal_metadata(version=version),
                    "nodes": {},
                    "sources": {},
                },
            )
            manifest = parse_dbt_manifest(manifest_path)
            assert manifest.manifest_version == version

    @pytest.mark.parametrize("version", [1, 2, 7, 9])
    def test_rejects_versions_below_v10_with_guided_error(
        self, tmp_path: Path, version: int
    ) -> None:
        manifest_path = _write_manifest(
            tmp_path,
            {
                "metadata": _minimal_metadata(version=version),
                "nodes": {},
                "sources": {},
            },
        )

        with pytest.raises(DbtManifestParseError) as exc_info:
            parse_dbt_manifest(manifest_path)
        # Guided error names the version it received and the minimum
        # supported. Helps the user know to upgrade dbt-core.
        assert "v10" in str(exc_info.value)
        assert f"v{version}" in str(exc_info.value)

    def test_rejects_malformed_schema_url(self, tmp_path: Path) -> None:
        manifest_path = _write_manifest(
            tmp_path,
            {
                "metadata": {
                    "dbt_schema_version": "not-a-recognized-url",
                    "project_name": "demo_project",
                },
                "nodes": {},
                "sources": {},
            },
        )

        with pytest.raises(DbtManifestParseError, match="dbt_schema_version"):
            parse_dbt_manifest(manifest_path)


# ----- Error paths ----------------------------------------------------------


class TestParseErrors:
    def test_nonexistent_path_raises_parse_error(self, tmp_path: Path) -> None:
        missing = tmp_path / "does_not_exist.json"
        with pytest.raises(DbtManifestParseError, match=r"does_not_exist\.json"):
            parse_dbt_manifest(missing)

    def test_directory_path_raises_parse_error(self, tmp_path: Path) -> None:
        # Directories pretending to be a manifest are a footgun — explicit
        # error instead of an opaque IsADirectoryError from `open()`.
        with pytest.raises(DbtManifestParseError, match="not a regular file"):
            parse_dbt_manifest(tmp_path)

    def test_invalid_json_raises_parse_error_with_path(self, tmp_path: Path) -> None:
        path = tmp_path / "manifest.json"
        path.write_text("{ this is not valid json")

        with pytest.raises(DbtManifestParseError) as exc_info:
            parse_dbt_manifest(path)
        assert "manifest.json" in str(exc_info.value)

    def test_missing_metadata_raises_parse_error(self, tmp_path: Path) -> None:
        manifest_path = _write_manifest(tmp_path, {"nodes": {}, "sources": {}})
        with pytest.raises(DbtManifestParseError, match="metadata"):
            parse_dbt_manifest(manifest_path)

    def test_missing_nodes_raises_parse_error(self, tmp_path: Path) -> None:
        manifest_path = _write_manifest(tmp_path, {"metadata": _minimal_metadata(), "sources": {}})
        with pytest.raises(DbtManifestParseError, match="nodes"):
            parse_dbt_manifest(manifest_path)

    def test_missing_sources_raises_parse_error(self, tmp_path: Path) -> None:
        # `sources` may be empty but the key must exist — its absence
        # signals a malformed manifest.
        manifest_path = _write_manifest(tmp_path, {"metadata": _minimal_metadata(), "nodes": {}})
        with pytest.raises(DbtManifestParseError, match="sources"):
            parse_dbt_manifest(manifest_path)

    def test_model_node_missing_name_raises_parse_error_with_unique_id(
        self, tmp_path: Path
    ) -> None:
        # A model node missing the required `name` field (truncated
        # manifest, third-party plugin output) must surface as a
        # `DbtManifestParseError` naming the offending `unique_id` —
        # not as a raw KeyError traceback.
        manifest_path = _write_manifest(
            tmp_path,
            {
                "metadata": _minimal_metadata(),
                "nodes": {
                    "model.demo_project.broken": {
                        "resource_type": "model",
                        "unique_id": "model.demo_project.broken",
                        # `name` field deliberately omitted
                        "database": "schemabrain_test",
                        "schema": "public",
                        "columns": {},
                        "depends_on": {"nodes": []},
                    },
                },
                "sources": {},
            },
        )
        with pytest.raises(DbtManifestParseError) as exc_info:
            parse_dbt_manifest(manifest_path)
        assert "model.demo_project.broken" in str(exc_info.value)
        assert "name" in str(exc_info.value).lower()

    def test_missing_project_name_raises_parse_error(self, tmp_path: Path) -> None:
        manifest_path = _write_manifest(
            tmp_path,
            {
                "metadata": {
                    "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
                    # No project_name
                },
                "nodes": {},
                "sources": {},
            },
        )
        with pytest.raises(DbtManifestParseError, match="project_name"):
            parse_dbt_manifest(manifest_path)


# ----- Dataclass invariants -------------------------------------------------


class TestDataclassShapes:
    def test_dbt_manifest_is_frozen(self, tmp_path: Path) -> None:
        manifest_path = _write_manifest(
            tmp_path,
            {
                "metadata": _minimal_metadata(),
                "nodes": {},
                "sources": {},
            },
        )
        manifest = parse_dbt_manifest(manifest_path)
        import dataclasses

        with pytest.raises(dataclasses.FrozenInstanceError):
            manifest.manifest_version = 99  # type: ignore[misc]

    def test_dbt_model_node_is_frozen(self) -> None:
        import dataclasses

        node = DbtModelNode(
            unique_id="model.demo.customer_dim",
            name="customer_dim",
            database="schemabrain_test",
            schema_name="public",
            identifier="customer_dim",
            description="",
            columns=(),
            depends_on_sources=(),
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            node.name = "other"  # type: ignore[misc]

    def test_dbt_constraint_compares_by_value(self) -> None:
        a = DbtConstraint(type="primary_key", name=None)
        b = DbtConstraint(type="primary_key", name=None)
        assert a == b

    def test_dbt_skip_counts_defaults_to_zero(self) -> None:
        # All fields default to 0 so the parser can construct a count
        # set with only the non-zero values supplied. Cleaner than
        # spreading zero-init across the parser body.
        counts = DbtSkipCounts()
        assert counts == DbtSkipCounts(
            metrics=0, snapshots=0, seeds=0, analyses=0, operations=0, exposures=0
        )

    @pytest.mark.parametrize(
        "bad_field",
        ["metrics", "snapshots", "seeds", "analyses", "operations", "exposures"],
    )
    def test_dbt_skip_counts_rejects_negative(self, bad_field: str) -> None:
        # The parser only increments from zero, so a negative count is
        # always a construction bug. Reject loudly rather than letting
        # a "skipped -3 metrics" line reach a user.
        kwargs = {bad_field: -1}
        with pytest.raises(ValueError, match=bad_field):
            DbtSkipCounts(**kwargs)

    def test_dbt_column_tests_defaults_to_false(self) -> None:
        tests = DbtColumnTests()
        assert tests.is_unique is False
        assert tests.is_not_null is False
