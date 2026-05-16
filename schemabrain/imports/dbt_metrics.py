"""dbt metric import — `manifest.json` → Schema Brain `Metric` instances.

The third dbt-import surface (after entities + the deferred relationships).
Scope at v1:

  - `type: "simple"` metrics only — sum/count/avg/min/max/count_distinct
    over one bare-column measure. dbt's `ratio`, `derived`, `cumulative`,
    `conversion` types skip with structured reason.

  - dbt `average` maps to Schema Brain `avg`; the other agg names
    (`sum`, `count`, `count_distinct`, `min`, `max`) match directly.

  - The metric's anchor entity is the primary entity declared on the
    semantic_model that owns the metric's measure. Schemabrain entities
    are written first by the entity importer; if the anchor isn't in
    that set, the metric skips with `anchor_entity_not_imported`.

  - Time grains come from the semantic_model's time dimension. If the
    dimension declares `granularity_options`, those become the metric's
    `time_grains` (canonical-sorted). Otherwise the single
    `time_granularity` becomes a one-element tuple. Non-temporal
    metrics (semantic_model with no time dimension) import with
    `time_dimension=None, time_grains=()`.

  - `time_dimension` is rendered as `<anchor_entity>.<time_column>` —
    same form the Schema Brain metric YAML grammar accepts.

This module is intentionally self-contained — it doesn't reuse the
`parse_dbt_manifest` parser machinery from `imports/dbt.py` because
metric extraction reads from `manifest.metrics` + `manifest.semantic_
models`, which `parse_dbt_manifest` ignores. Sharing the JSON-read +
error-wrap helpers would be premature coupling at this stage.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, get_args

from schemabrain.core.metric import (
    _TIME_GRAIN_RANK,
    AggFunction,
    Metric,
    MetricMeasure,
    TimeGrain,
)

# Closed set of skip reasons. Each maps to one structural shape the
# importer can't handle at v1; the CLI surfaces the reason in the
# end-of-run stderr breadcrumb. Adding a value here requires updating
# the `_VALID_REASONS` frozenset below.
DbtMetricSkipReason = Literal[
    "non_simple_type",
    "non_column_expr",
    "unsupported_agg",
    "no_primary_entity",
    "anchor_entity_not_imported",
    "measure_not_found",
]
_VALID_REASONS: frozenset[str] = frozenset(get_args(DbtMetricSkipReason))

# dbt's measure `expr` can be any SQL expression. v1 only handles bare
# column references; anything that doesn't pass this identifier shape
# skips with `non_column_expr`. Matches the same alphabet Schema Brain
# uses for column identifiers elsewhere.
_BARE_COLUMN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")

# dbt's agg names → Schema Brain agg names. dbt uses `average`;
# Schema Brain uses `avg`. The other names match directly. Open dbt
# aggs (e.g. `percentile`, `median`) aren't in this map → skip.
_AGG_MAP: dict[str, AggFunction] = {
    "sum": "sum",
    "count": "count",
    "count_distinct": "count_distinct",
    "average": "avg",
    "min": "min",
    "max": "max",
}

# Closed set of dbt time granularities the importer recognises. Maps
# directly to Schema Brain `TimeGrain` since both use the same lowercase
# names. dbt also accepts `hour` etc., but Schema Brain v1 starts at
# `day` — those would skip the dimension entirely (treated as
# non-temporal at v1).
_VALID_DBT_GRANULARITIES: frozenset[str] = frozenset(get_args(TimeGrain))

# Canonical sort order + rank are imported from `core.metric` (the
# single source of truth). Earlier revisions copy-pasted these locally
# — if a future grain (e.g. `hour`) is added to `core.metric._TIME_
# GRAIN_ORDER`, a copy here would silently drift and produce unsorted
# tuples that crash the `Metric` invariant check. Import keeps them
# in lock-step.

# dbt schema version → metric support. Versions before v11 (dbt-core
# 1.6) didn't have semantic_models in this shape. We refuse to attempt
# metric extraction below that.
_MINIMUM_MANIFEST_VERSION = 11


class DbtMetricImportError(ValueError):
    """Raised when `parse_dbt_metrics` can't even attempt extraction —
    missing file, malformed JSON, unsupported manifest schema version.

    Per-metric mapping failures (non-simple type, bad expr, etc.)
    don't raise — they emit a `DbtMetricSkip` so the caller can decide
    whether to continue.
    """


@dataclass(frozen=True)
class DbtMetricSkip:
    """One dbt metric that couldn't be imported, with the reason.

    Mirrors `DbtImportSkip` from `imports/dbt.py` — closed-Literal
    reason for audit-log bucketing, plus a free-text message for the
    stderr breadcrumb.
    """

    metric_name: str
    reason: DbtMetricSkipReason
    message: str

    def __post_init__(self) -> None:
        if self.reason not in _VALID_REASONS:
            raise ValueError(
                f"reason must be one of {sorted(_VALID_REASONS)} (got {self.reason!r})"
            )


def parse_dbt_metrics(
    manifest_path: Path,
    *,
    imported_entity_names: set[str],
) -> tuple[tuple[Metric, ...], tuple[DbtMetricSkip, ...]]:
    """Parse `manifest.json` and extract Schema Brain `Metric` instances.

    `imported_entity_names` is the set of entity names the entity
    importer just wrote (or already existed) — used to refuse metrics
    whose anchor entity isn't in the user's store. Without this guard,
    a metric anchored on an entity outside the user's import would
    silently land with a broken FK at write time.

    Returns `(metrics, skipped)`:
      - `metrics`: tuple of `Metric` ready for `store.write_metric`
      - `skipped`: tuple of `DbtMetricSkip` for the CLI breadcrumb

    Raises `DbtMetricImportError` on structural manifest problems
    (missing file, bad JSON, unsupported version). Per-metric mapping
    failures don't raise — they emit `DbtMetricSkip`.
    """
    raw = _read_manifest(manifest_path)
    _check_schema_version(raw, manifest_path)

    metrics_raw: dict[str, Any] = raw.get("metrics") or {}
    semantic_models_raw: dict[str, Any] = raw.get("semantic_models") or {}

    # Pre-build the (model, measure_name) → (semantic_model, measure)
    # index so each metric's measure lookup is O(1).
    measure_index = _build_measure_index(semantic_models_raw)

    metrics: list[Metric] = []
    skipped: list[DbtMetricSkip] = []
    for _unique_id, metric_node in metrics_raw.items():
        result = _map_one_metric(
            metric_node,
            measure_index=measure_index,
            imported_entity_names=imported_entity_names,
        )
        if isinstance(result, DbtMetricSkip):
            skipped.append(result)
        else:
            metrics.append(result)

    return tuple(metrics), tuple(skipped)


# ----- internals ------------------------------------------------------------


def _read_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise DbtMetricImportError(f"dbt manifest not found at {path}; run `dbt compile` first.")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover — guarded by exists()
        raise DbtMetricImportError(f"cannot read {path}: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise DbtMetricImportError(
            f"invalid JSON in {path}: {exc.msg} (line {exc.lineno} column {exc.colno})"
        ) from exc


def _check_schema_version(raw: dict[str, Any], path: Path) -> None:
    """Refuse manifest schemas before v11 (dbt-core 1.6). Older versions
    didn't have `semantic_models` in the format we parse here.

    The metadata key is `dbt_schema_version`; the value is a URL like
    `https://schemas.getdbt.com/dbt/manifest/v12.json`. We extract the
    integer after `v` and before `.json`.
    """
    metadata = raw.get("metadata") or {}
    schema_url = metadata.get("dbt_schema_version", "")
    match = re.search(r"manifest/v(\d+)\.json", schema_url)
    if match is None:  # pragma: no cover — real manifests always carry this; defensive
        raise DbtMetricImportError(
            f"could not parse dbt_schema_version in {path}; "
            f"expected URL ending in `/manifest/vN.json`"
        )
    version = int(match.group(1))
    if version < _MINIMUM_MANIFEST_VERSION:
        raise DbtMetricImportError(
            f"dbt metric import requires manifest schema version >= "
            f"{_MINIMUM_MANIFEST_VERSION} (dbt-core 1.6+); got v{version}. "
            f"Upgrade dbt-core to enable metric import, or omit "
            f"`--include-metrics` to skip metric extraction."
        )


@dataclass(frozen=True)
class _MeasureBinding:
    """Internal record: links a dbt measure to the semantic_model that
    owns it. The lookup index is `measure_name → _MeasureBinding`."""

    agg: str
    expr: str
    semantic_model: dict[str, Any]


def _build_measure_index(semantic_models_raw: dict[str, Any]) -> dict[str, _MeasureBinding]:
    """Index every measure across all semantic_models by name.

    A duplicate measure name across two semantic_models is allowed by
    dbt but rare in practice; if it happens, the LATER one wins per
    dict-insertion semantics. The caller's metric will pick whichever
    binding the index returned; we document this and move on.
    """
    index: dict[str, _MeasureBinding] = {}
    for sm_node in semantic_models_raw.values():
        if not isinstance(sm_node, dict):  # pragma: no cover — defensive shape guard
            continue
        for measure in sm_node.get("measures") or []:
            if not isinstance(measure, dict):  # pragma: no cover — defensive
                continue
            name = measure.get("name")
            agg = measure.get("agg")
            expr = measure.get("expr")
            if not isinstance(name, str) or not isinstance(
                agg, str
            ):  # pragma: no cover — defensive
                continue
            if not isinstance(expr, str):  # pragma: no cover — defensive
                continue
            index[name] = _MeasureBinding(agg=agg, expr=expr, semantic_model=sm_node)
    return index


def _map_one_metric(
    metric_node: dict[str, Any],
    *,
    measure_index: dict[str, _MeasureBinding],
    imported_entity_names: set[str],
) -> Metric | DbtMetricSkip:
    """Map one dbt metric node to a Schema Brain `Metric` or skip.

    Returns either a `Metric` (ready for `store.write_metric`) or a
    `DbtMetricSkip` carrying the reason this metric couldn't be mapped.
    """
    name = metric_node.get("name", "<unknown>")
    metric_type = metric_node.get("type", "")
    if metric_type != "simple":
        return DbtMetricSkip(
            metric_name=name,
            reason="non_simple_type",
            message=(
                f"dbt metric type {metric_type!r} is not supported at v1 "
                f"(only `simple` metrics import; ratio/derived/cumulative "
                f"defer to v2's expression layer)."
            ),
        )

    type_params = metric_node.get("type_params") or {}
    measure_ref = type_params.get("measure") or {}
    # Defensive: a malformed manifest where `measure` is a non-dict
    # truthy value (e.g., a string) would otherwise raise
    # AttributeError on `.get("name")` — crashing the whole import
    # instead of skipping the one bad metric.
    if not isinstance(measure_ref, dict):
        return DbtMetricSkip(
            metric_name=name,
            reason="measure_not_found",
            message=(
                f"metric.type_params.measure is not a mapping "
                f"(got {type(measure_ref).__name__}); manifest is malformed."
            ),
        )
    measure_name = measure_ref.get("name", "")
    binding = measure_index.get(measure_name)
    if binding is None:
        return DbtMetricSkip(
            metric_name=name,
            reason="measure_not_found",
            message=(
                f"metric references measure {measure_name!r} but no semantic_model declares it."
            ),
        )

    # Map the dbt agg to a Schema Brain agg.
    sb_agg = _AGG_MAP.get(binding.agg)
    if sb_agg is None:
        return DbtMetricSkip(
            metric_name=name,
            reason="unsupported_agg",
            message=(
                f"dbt agg {binding.agg!r} is not supported at v1; "
                f"supported aggs: {sorted(_AGG_MAP.keys())}"
            ),
        )

    # The measure `expr` must be a bare column at v1 — expression
    # measures defer to v2.
    if not _BARE_COLUMN_RE.fullmatch(binding.expr):
        return DbtMetricSkip(
            metric_name=name,
            reason="non_column_expr",
            message=(
                f"measure {measure_name!r} has expression {binding.expr!r}; "
                f"v1 only supports bare-column measures (expressions defer "
                f"to v2)."
            ),
        )

    # Anchor entity: the semantic_model's primary entity.
    entities = binding.semantic_model.get("entities") or []
    primary = next(
        (e for e in entities if isinstance(e, dict) and e.get("type") == "primary"),
        None,
    )
    if primary is None:
        return DbtMetricSkip(
            metric_name=name,
            reason="no_primary_entity",
            message=(
                f"semantic_model owning measure {measure_name!r} has no "
                f"primary entity; the metric can't be anchored."
            ),
        )
    anchor_entity = primary.get("name")
    if not isinstance(anchor_entity, str) or not anchor_entity:
        # Primary entity node exists but has no usable `name` — a
        # malformed manifest, distinct from the "anchor entity not
        # imported" case. Surface as a `no_primary_entity` skip so the
        # diagnostic points at the manifest, not at the import set.
        return DbtMetricSkip(
            metric_name=name,
            reason="no_primary_entity",
            message=(
                f"semantic_model owning measure {measure_name!r} has a "
                f"primary entity entry with no usable 'name' field; "
                f"the manifest is malformed."
            ),
        )
    if anchor_entity not in imported_entity_names:
        return DbtMetricSkip(
            metric_name=name,
            reason="anchor_entity_not_imported",
            message=(
                f"metric anchors on entity {anchor_entity!r} but this "
                f"entity was not imported. Run the entity import first or "
                f"add the anchor model to your dbt project."
            ),
        )

    time_dimension, time_grains = _extract_time(binding.semantic_model, anchor_entity=anchor_entity)

    return Metric(
        name=name,
        description=metric_node.get("description") or "",
        entity=anchor_entity,
        measure=MetricMeasure(agg=sb_agg, column=binding.expr),
        time_dimension=time_dimension,
        time_grains=time_grains,
        origin="dbt_import",
    )


def _extract_time(
    semantic_model: dict[str, Any],
    *,
    anchor_entity: str,
) -> tuple[str | None, tuple[TimeGrain, ...]]:
    """Pull (time_dimension, time_grains) from a semantic_model.

    Returns `(None, ())` when the semantic_model declares no time
    dimension or when the declared dimension's granularity is finer
    than `day` (Schema Brain v1 starts at day).

    `time_dimension` is rendered as `<anchor_entity>.<column_name>`
    so the Schema Brain `Metric` invariant holds.

    `time_grains` honours the semantic_model's `granularity_options`
    list if present (canonical-sorted), or falls back to the single
    `time_granularity` value.
    """
    for dim in semantic_model.get("dimensions") or []:
        if not isinstance(dim, dict) or dim.get("type") != "time":  # pragma: no cover — defensive
            continue
        dim_name = dim.get("name", "")
        if not isinstance(dim_name, str) or not _BARE_COLUMN_RE.fullmatch(
            dim_name
        ):  # pragma: no cover — defensive
            continue
        type_params = dim.get("type_params") or {}
        primary_grain = type_params.get("time_granularity")
        granularity_options = type_params.get("granularity_options") or []

        # Collect candidate grains; drop anything outside the closed
        # Schema Brain `TimeGrain` set (sub-day grains, custom names).
        candidates: list[str] = []
        if isinstance(primary_grain, str) and primary_grain in _VALID_DBT_GRANULARITIES:
            candidates.append(primary_grain)
        if isinstance(granularity_options, list):  # pragma: no branch — always a list in our reads
            for opt in granularity_options:
                if (
                    isinstance(opt, str)
                    and opt in _VALID_DBT_GRANULARITIES
                    and opt not in candidates
                ):
                    candidates.append(opt)
        if not candidates:  # pragma: no cover — a time dim with no recognised grain is rare; treated as non-temporal
            continue

        # Canonical sort by rank — Schema Brain `Metric` dataclass
        # rejects unsorted time_grains.
        sorted_grains = tuple(sorted(set(candidates), key=lambda g: _TIME_GRAIN_RANK[g]))
        return f"{anchor_entity}.{dim_name}", sorted_grains  # type: ignore[return-value]

    return None, ()
