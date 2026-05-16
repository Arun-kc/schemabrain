"""Tests for dbt metric import (`imports/dbt_metrics.py`).

Locks the dbt-metric-import contract:

  - `type: "simple"` metrics with a bare-column measure import cleanly
  - `ratio`, `derived`, `cumulative` types skip with reason
  - Measures whose `expr` is not a bare column skip with reason
  - Metrics whose semantic_model has no primary entity skip with reason
  - Metrics whose anchor entity isn't in the import-time entity set
    skip with reason (the entity import didn't include the anchor)
  - Time granularity from the metric or semantic_model maps to the
    Schema Brain time_grains tuple, canonical-sorted
  - Non-temporal metrics (no time dim on the semantic_model) import
    with `time_dimension=None`, `time_grains=()`
  - dbt agg names map to Schema Brain agg names
  - Origin is always `dbt_import`
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from schemabrain.core.metric import Metric
from schemabrain.imports.dbt_metrics import (
    DbtMetricImportError,
    DbtMetricSkip,
    parse_dbt_metrics,
)

# ----- fixtures --------------------------------------------------------------


def _write_manifest(tmp_path: Path, body: dict) -> Path:
    target = tmp_path / "manifest.json"
    target.write_text(json.dumps(body), encoding="utf-8")
    return target


def _semantic_model(
    *,
    name: str = "orders",
    measures: list[dict] | None = None,
    entities: list[dict] | None = None,
    dimensions: list[dict] | None = None,
) -> dict:
    return {
        "resource_type": "semantic_model",
        "name": name,
        "node_relation": {"schema_name": "public", "alias": "orders"},
        "measures": measures
        or [
            {"name": "revenue_measure", "agg": "sum", "expr": "total_amount"},
        ],
        "entities": entities
        if entities is not None
        else [{"name": "order", "type": "primary", "expr": "id"}],
        "dimensions": dimensions
        if dimensions is not None
        else [
            {
                "name": "ordered_at",
                "type": "time",
                "type_params": {"time_granularity": "day"},
            }
        ],
    }


def _metric_node(
    *,
    name: str = "total_revenue",
    metric_type: str = "simple",
    measure_name: str = "revenue_measure",
    description: str = "",
) -> dict:
    return {
        "resource_type": "metric",
        "name": name,
        "label": name.replace("_", " ").title(),
        "description": description,
        "type": metric_type,
        "type_params": {"measure": {"name": measure_name, "filter": None}},
        "filter": None,
    }


def _manifest_with(
    *,
    metrics: dict[str, dict],
    semantic_models: dict[str, dict] | None = None,
) -> dict:
    return {
        "metadata": {
            "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
            "project_name": "demo",
            "dbt_version": "1.7.0",
        },
        "nodes": {},
        "sources": {},
        "metrics": metrics,
        "semantic_models": semantic_models
        if semantic_models is not None
        else {"semantic_model.demo.orders": _semantic_model()},
    }


# ----- happy paths -----------------------------------------------------------


class TestSimpleMetricImport:
    def test_simple_sum_metric_imports(self, tmp_path: Path) -> None:
        manifest_path = _write_manifest(
            tmp_path,
            _manifest_with(metrics={"metric.demo.total_revenue": _metric_node()}),
        )
        metrics, skipped = parse_dbt_metrics(manifest_path, imported_entity_names={"order"})
        assert len(metrics) == 1
        assert skipped == ()
        metric = metrics[0]
        assert isinstance(metric, Metric)
        assert metric.name == "total_revenue"
        assert metric.entity == "order"
        assert metric.measure.agg == "sum"
        assert metric.measure.column == "total_amount"
        assert metric.time_dimension == "order.ordered_at"
        assert metric.time_grains == ("day",)
        assert metric.origin == "dbt_import"

    @pytest.mark.parametrize(
        "dbt_agg, sb_agg",
        [
            ("sum", "sum"),
            ("count", "count"),
            ("count_distinct", "count_distinct"),
            ("average", "avg"),
            ("min", "min"),
            ("max", "max"),
        ],
    )
    def test_agg_mapping(self, tmp_path: Path, dbt_agg: str, sb_agg: str) -> None:
        manifest_path = _write_manifest(
            tmp_path,
            _manifest_with(
                metrics={"metric.demo.m": _metric_node()},
                semantic_models={
                    "semantic_model.demo.orders": _semantic_model(
                        measures=[
                            {
                                "name": "revenue_measure",
                                "agg": dbt_agg,
                                "expr": "total_amount",
                            }
                        ]
                    )
                },
            ),
        )
        metrics, _skipped = parse_dbt_metrics(manifest_path, imported_entity_names={"order"})
        assert metrics[0].measure.agg == sb_agg

    def test_description_carries_through(self, tmp_path: Path) -> None:
        manifest_path = _write_manifest(
            tmp_path,
            _manifest_with(
                metrics={
                    "metric.demo.total_revenue": _metric_node(
                        description="Sum of completed order totals."
                    )
                }
            ),
        )
        metrics, _skipped = parse_dbt_metrics(manifest_path, imported_entity_names={"order"})
        assert metrics[0].description == "Sum of completed order totals."

    def test_subday_primary_grain_falls_through_to_options(self, tmp_path: Path) -> None:
        # dbt accepts `hour`/`minute` but Schema Brain starts at `day`.
        # When `time_granularity` is sub-day, the importer ignores it
        # and falls back to `granularity_options` for valid grains.
        manifest_path = _write_manifest(
            tmp_path,
            _manifest_with(
                metrics={"metric.demo.m": _metric_node()},
                semantic_models={
                    "semantic_model.demo.orders": _semantic_model(
                        dimensions=[
                            {
                                "name": "ordered_at",
                                "type": "time",
                                "type_params": {
                                    "time_granularity": "hour",
                                    "granularity_options": ["day", "week"],
                                },
                            }
                        ]
                    )
                },
            ),
        )
        metrics, _skipped = parse_dbt_metrics(manifest_path, imported_entity_names={"order"})
        assert metrics[0].time_grains == ("day", "week")

    def test_time_grains_canonical_sorted_from_dimension(self, tmp_path: Path) -> None:
        # When the semantic_model declares multiple time grains
        # (granularity_options in dbt), the result is canonical-sorted.
        manifest_path = _write_manifest(
            tmp_path,
            _manifest_with(
                metrics={"metric.demo.m": _metric_node()},
                semantic_models={
                    "semantic_model.demo.orders": _semantic_model(
                        dimensions=[
                            {
                                "name": "ordered_at",
                                "type": "time",
                                "type_params": {
                                    "time_granularity": "day",
                                    "granularity_options": [
                                        "month",
                                        "day",
                                        "week",
                                    ],
                                },
                            }
                        ]
                    )
                },
            ),
        )
        metrics, _skipped = parse_dbt_metrics(manifest_path, imported_entity_names={"order"})
        assert metrics[0].time_grains == ("day", "week", "month")


class TestNonTemporalMetric:
    def test_no_time_dimension_yields_non_temporal(self, tmp_path: Path) -> None:
        manifest_path = _write_manifest(
            tmp_path,
            _manifest_with(
                metrics={"metric.demo.m": _metric_node()},
                semantic_models={"semantic_model.demo.orders": _semantic_model(dimensions=[])},
            ),
        )
        metrics, _skipped = parse_dbt_metrics(manifest_path, imported_entity_names={"order"})
        assert len(metrics) == 1
        assert metrics[0].time_dimension is None
        assert metrics[0].time_grains == ()


# ----- skip paths ------------------------------------------------------------


class TestSkipNonSimpleTypes:
    @pytest.mark.parametrize("metric_type", ["ratio", "derived", "cumulative", "conversion"])
    def test_non_simple_type_skipped_with_reason(self, tmp_path: Path, metric_type: str) -> None:
        manifest_path = _write_manifest(
            tmp_path,
            _manifest_with(
                metrics={"metric.demo.m": _metric_node(name="m", metric_type=metric_type)}
            ),
        )
        metrics, skipped = parse_dbt_metrics(manifest_path, imported_entity_names={"order"})
        assert metrics == ()
        assert len(skipped) == 1
        assert skipped[0].reason == "non_simple_type"
        assert skipped[0].metric_name == "m"
        assert metric_type in skipped[0].message


class TestSkipExpressions:
    def test_non_bare_column_expr_skipped(self, tmp_path: Path) -> None:
        manifest_path = _write_manifest(
            tmp_path,
            _manifest_with(
                metrics={"metric.demo.m": _metric_node()},
                semantic_models={
                    "semantic_model.demo.orders": _semantic_model(
                        measures=[
                            {
                                "name": "revenue_measure",
                                "agg": "sum",
                                "expr": "case when status='completed' then total_amount end",
                            }
                        ]
                    )
                },
            ),
        )
        metrics, skipped = parse_dbt_metrics(manifest_path, imported_entity_names={"order"})
        assert metrics == ()
        assert len(skipped) == 1
        assert skipped[0].reason == "non_column_expr"

    def test_unmapped_agg_skipped(self, tmp_path: Path) -> None:
        # dbt has aggs like `percentile`, `median` that Schema Brain
        # doesn't support at v1.
        manifest_path = _write_manifest(
            tmp_path,
            _manifest_with(
                metrics={"metric.demo.m": _metric_node()},
                semantic_models={
                    "semantic_model.demo.orders": _semantic_model(
                        measures=[
                            {
                                "name": "revenue_measure",
                                "agg": "percentile",
                                "expr": "total_amount",
                            }
                        ]
                    )
                },
            ),
        )
        metrics, skipped = parse_dbt_metrics(manifest_path, imported_entity_names={"order"})
        assert metrics == ()
        assert len(skipped) == 1
        assert skipped[0].reason == "unsupported_agg"


class TestSkipEntityMapping:
    def test_no_primary_entity_skipped(self, tmp_path: Path) -> None:
        manifest_path = _write_manifest(
            tmp_path,
            _manifest_with(
                metrics={"metric.demo.m": _metric_node()},
                semantic_models={"semantic_model.demo.orders": _semantic_model(entities=[])},
            ),
        )
        metrics, skipped = parse_dbt_metrics(manifest_path, imported_entity_names={"order"})
        assert metrics == ()
        assert len(skipped) == 1
        assert skipped[0].reason == "no_primary_entity"

    def test_anchor_entity_not_in_imported_set_skipped(self, tmp_path: Path) -> None:
        # The metric anchors on `order`, but the entity import didn't
        # include `order`. Refuse — anchoring a metric on an entity
        # the user hasn't confirmed would silently land with broken
        # FK at write time.
        manifest_path = _write_manifest(
            tmp_path,
            _manifest_with(metrics={"metric.demo.m": _metric_node()}),
        )
        metrics, skipped = parse_dbt_metrics(
            manifest_path,
            imported_entity_names=set(),  # no entities
        )
        assert metrics == ()
        assert len(skipped) == 1
        assert skipped[0].reason == "anchor_entity_not_imported"

    def test_primary_entity_without_name_yields_no_primary_entity(self, tmp_path: Path) -> None:
        # A semantic_model with a primary entity entry that lacks a
        # `name` field is a malformed manifest. The importer must skip
        # with `no_primary_entity` rather than mis-attribute as
        # `anchor_entity_not_imported`.
        manifest_path = _write_manifest(
            tmp_path,
            _manifest_with(
                metrics={"metric.demo.m": _metric_node(name="m")},
                semantic_models={
                    "semantic_model.demo.orders": _semantic_model(
                        entities=[{"type": "primary"}]  # no "name"
                    )
                },
            ),
        )
        metrics, skipped = parse_dbt_metrics(manifest_path, imported_entity_names={"order"})
        assert metrics == ()
        assert len(skipped) == 1
        assert skipped[0].reason == "no_primary_entity"
        assert "malformed" in skipped[0].message

    def test_non_dict_measure_ref_yields_measure_not_found(self, tmp_path: Path) -> None:
        # A malformed `type_params.measure` (non-dict truthy value)
        # would otherwise crash the importer with AttributeError. The
        # importer must skip with `measure_not_found` instead.
        manifest_path = _write_manifest(
            tmp_path,
            _manifest_with(
                metrics={
                    "metric.demo.m": {
                        "resource_type": "metric",
                        "name": "m",
                        "label": "M",
                        "description": "",
                        "type": "simple",
                        "type_params": {"measure": "not_a_dict"},
                        "filter": None,
                    }
                }
            ),
        )
        metrics, skipped = parse_dbt_metrics(manifest_path, imported_entity_names={"order"})
        assert metrics == ()
        assert len(skipped) == 1
        assert skipped[0].reason == "measure_not_found"
        assert "mapping" in skipped[0].message

    def test_measure_not_found_in_semantic_models_skipped(self, tmp_path: Path) -> None:
        # Metric references a measure name that doesn't exist in any
        # semantic_model. Bad manifest — skip rather than crash.
        manifest_path = _write_manifest(
            tmp_path,
            _manifest_with(metrics={"metric.demo.m": _metric_node(measure_name="ghost_measure")}),
        )
        metrics, skipped = parse_dbt_metrics(manifest_path, imported_entity_names={"order"})
        assert metrics == ()
        assert len(skipped) == 1
        assert skipped[0].reason == "measure_not_found"


class TestManifestErrors:
    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(DbtMetricImportError, match="not found"):
            parse_dbt_metrics(tmp_path / "absent.json", imported_entity_names=set())

    def test_malformed_json_raises(self, tmp_path: Path) -> None:
        target = tmp_path / "bad.json"
        target.write_text("{not valid json", encoding="utf-8")
        with pytest.raises(DbtMetricImportError, match="JSON"):
            parse_dbt_metrics(target, imported_entity_names=set())

    def test_manifest_without_metrics_returns_empty(self, tmp_path: Path) -> None:
        # A manifest with no `metrics` key is valid — just means the
        # dbt project doesn't define metrics yet. Return empty, no
        # skip, no error.
        manifest_path = _write_manifest(
            tmp_path,
            {
                "metadata": {
                    "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
                    "project_name": "demo",
                    "dbt_version": "1.7.0",
                },
                "nodes": {},
                "sources": {},
            },
        )
        metrics, skipped = parse_dbt_metrics(manifest_path, imported_entity_names={"order"})
        assert metrics == ()
        assert skipped == ()

    def test_manifest_with_old_schema_skips_metrics(self, tmp_path: Path) -> None:
        # Manifest schema versions before 1.6 (v9) didn't have
        # semantic_models in the format we parse. Skip with a
        # version-specific reason.
        manifest_path = _write_manifest(
            tmp_path,
            {
                "metadata": {
                    "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v9.json",
                    "project_name": "demo",
                    "dbt_version": "1.5.0",
                },
                "nodes": {},
                "sources": {},
            },
        )
        with pytest.raises(DbtMetricImportError, match="schema version"):
            parse_dbt_metrics(manifest_path, imported_entity_names={"order"})


class TestDbtMetricSkip:
    def test_skip_dataclass_invariant(self) -> None:
        skip = DbtMetricSkip(
            metric_name="m",
            reason="non_simple_type",
            message="...",
        )
        assert skip.metric_name == "m"

    def test_unknown_reason_rejected(self) -> None:
        with pytest.raises(ValueError, match="reason"):
            DbtMetricSkip(
                metric_name="m",
                reason="bogus",  # type: ignore[arg-type]
                message="",
            )
