"""YAML grammar parser for v1 metric files.

The grammar is a flat mapping with a small fixed schema:

    version: 1                            # required, int, must equal 1
    name: total_revenue                   # required, identifier-shaped
    description: "..."                    # optional, defaults to ""
    entity: order                         # required, identifier-shaped
    measure:                              # required
      agg: sum                            # required, closed-grammar AggFunction
      column: total_amount                # XOR with `expression`; identifier-shaped
      expression: unit_price * quantity   # XOR with `column`; whitelist arithmetic
    time_dimension: order.created_at      # optional; entity.column form
    time_grains: [day, week, month]       # required iff time_dimension set
    origin: manual                        # optional, defaults "manual"

Exactly one of `measure.column` or `measure.expression` is required.
The bare-column form is the v1 shape; the expression form is the v2
composite-arithmetic shape (parsed via the strict whitelist in
`semantic.compiler.measure_expression` — see that module for the
allowed operators/literals).

Parse errors raise `MetricYamlError` (a `ValueError` subclass) with a
message that names the offending field and shows the value when
relevant. The CLI (`schemabrain metrics apply <path>`) prints the
message verbatim and exits 1.

Strict-keys policy: unknown top-level keys are rejected at parse time
so a typo like `entities:` (note plural) doesn't silently become a
no-op. Same policy applies inside `measure`.

Mirrors `entities/yaml_grammar.py` + `joins/yaml_grammar.py` in style
— version field required, identifier validation deferred to dataclass
`__post_init__`, errors wrap with field context, `dbt_import` origin
refused at parse-time so hand-authored YAML can't bypass the
dbt-guard pattern.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from schemabrain.core.metric import (
    _VALID_AGGS,
    _VALID_GRAINS,
    _VALID_ORIGINS,
    Metric,
    MetricMeasure,
    MetricOrigin,
    TimeGrain,
)

# Closed sets used for strict-keys validation. Drift here without a
# schema/version bump silently changes the parser surface, so the
# frozensets are the single source of truth.
_ALLOWED_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {
        "version",
        "name",
        "description",
        "entity",
        "measure",
        "time_dimension",
        "time_grains",
        "origin",
    }
)
_REQUIRED_TOP_LEVEL_KEYS: frozenset[str] = frozenset({"version", "name", "entity", "measure"})

_ALLOWED_MEASURE_KEYS: frozenset[str] = frozenset({"agg", "column", "expression"})
# `agg` is required; exactly one of `column` / `expression` must be
# present (XOR). The XOR check is done in `_parse_measure` after the
# allowed-key check, so an "unknown measure key" error has precedence
# over an "exactly one of" error.
_REQUIRED_MEASURE_KEYS: frozenset[str] = frozenset({"agg"})

_SUPPORTED_VERSION: int = 1


class MetricYamlError(ValueError):
    """Raised when a metric YAML document fails to parse or validate.

    Subclass of `ValueError` so callers that just want to catch "bad
    input" can do so without importing this module. The CLI catches
    the specific type to keep its exit-code contract narrow.
    """


def parse_metric_yaml(text: str) -> Metric:
    """Parse `text` (raw YAML) into a `Metric`.

    Raises `MetricYamlError` on any structural, schema, or value
    violation. The error message is user-facing — keep it short and
    name the field that's wrong.
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise MetricYamlError(f"failed to parse metric YAML: {exc}") from exc

    if data is None:
        raise MetricYamlError("metric YAML is empty")
    if not isinstance(data, dict):
        raise MetricYamlError(
            f"metric YAML must be a mapping at the top level (got {type(data).__name__})"
        )

    _check_keys(
        data,
        allowed=_ALLOWED_TOP_LEVEL_KEYS,
        required=_REQUIRED_TOP_LEVEL_KEYS,
        scope="top-level",
    )
    _check_version(data["version"])

    name = _require_str(data, "name")
    description = _optional_str(data, "description", default="")
    entity = _require_str(data, "entity")
    measure = _parse_measure(data["measure"])
    time_dimension, time_grains = _parse_time(
        data.get("time_dimension"),
        data.get("time_grains"),
        has_time_dimension="time_dimension" in data,
        has_time_grains="time_grains" in data,
    )
    origin = _parse_origin(data.get("origin", "manual"))

    # `Metric.__post_init__` runs identifier-shape + paired-emptiness +
    # canonical-grain-order + origin-enum checks. Re-wrap any failure
    # as MetricYamlError so the CLI surface stays uniform; the prefix
    # distinguishes "metric validation failed" from mid-parse errors
    # so a user reading the CLI message can tell where the check fired.
    try:
        return Metric(
            name=name,
            description=description,
            entity=entity,
            measure=measure,
            time_dimension=time_dimension,
            time_grains=time_grains,
            origin=origin,
        )
    except ValueError as exc:
        raise MetricYamlError(f"metric validation failed: {exc}") from exc


def metric_to_yaml(metric: Metric) -> str:
    """Serialise a `Metric` into the v1 metric YAML body.

    Inverse of `parse_metric_yaml`: any `Metric` that round-trips
    through this function then `parse_metric_yaml` is value-equal to
    the input (modulo the two store-derived fields below).

    Trust-signal fields (`inference_method`, `validation_state`) are
    deliberately NOT emitted. The grammar parser does not accept them
    — they are store-side derived at apply time. Round-tripping an
    LLM-suggested metric through export → edit → apply correctly
    resets the trust signal to manually-authored, because the operator
    DID author the edit.

    `measure.column` and `measure.expression` are XOR (enforced by the
    `MetricMeasure` dataclass); the serialiser emits exactly the
    populated one. `time_dimension` and `time_grains` are paired (both
    set or both absent — also a dataclass invariant); the serialiser
    emits both when set, neither when not.

    `description` is omitted when empty so the YAML body stays compact
    for minimal metrics — the parser fills the field with the empty
    string by default.

    Uses `yaml.safe_dump` for scalar values so an LLM-supplied
    description containing YAML-special characters is properly quoted/
    escaped. Hand-rolled string concatenation would open a YAML-
    injection path where a malicious description fragments the
    document on re-parse.
    """
    body: dict[str, object] = {
        "version": _SUPPORTED_VERSION,
        "name": metric.name,
    }
    if metric.description:
        body["description"] = metric.description
    body["entity"] = metric.entity
    measure_body: dict[str, object] = {"agg": metric.measure.agg}
    if metric.measure.column is not None:
        measure_body["column"] = metric.measure.column
    else:
        # XOR invariant: when `column` is None, `expression` is set.
        measure_body["expression"] = metric.measure.expression
    body["measure"] = measure_body
    if metric.time_dimension is not None:
        body["time_dimension"] = metric.time_dimension
        body["time_grains"] = list(metric.time_grains)
    body["origin"] = metric.origin
    return yaml.safe_dump(
        body,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    ).rstrip()


def parse_metric_yaml_file(path: Path) -> Metric:
    """Read `path` and parse its contents as a metric YAML document.

    `FileNotFoundError` / `IsADirectoryError` propagate verbatim so
    the caller can distinguish "missing file" from "malformed YAML."
    `PermissionError` and `UnicodeDecodeError` (from a non-UTF-8 file
    masquerading as YAML) are wrapped as `MetricYamlError` so the
    CLI surface stays uniform — both are user-fixable conditions,
    not "missing file."
    Everything else surfaces as `MetricYamlError`.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError):
        raise
    except PermissionError as exc:
        raise MetricYamlError(
            f"cannot read {path}: permission denied ({exc.strerror or exc})"
        ) from exc
    except UnicodeDecodeError as exc:
        raise MetricYamlError(
            f"{path} is not a valid UTF-8 text file (metric YAML must be UTF-8): {exc.reason}"
        ) from exc
    return parse_metric_yaml(text)


# ----- field validators -------------------------------------------------------


def _check_keys(
    data: dict[str, Any],
    *,
    allowed: frozenset[str],
    required: frozenset[str],
    scope: str,
) -> None:
    keys = set(data.keys())
    missing = required - keys
    if missing:
        raise MetricYamlError(f"missing required {scope} field(s): {sorted(missing)}")
    unknown = keys - allowed
    if unknown:
        raise MetricYamlError(f"unknown {scope} keys: {sorted(unknown)}")


def _check_version(value: Any) -> None:
    # YAML's `version: 1` parses as int. We reject the string form
    # `version: "1"` and the float form `version: 1.0` strictly —
    # both indicate a hand-typed file that drifted from the grammar,
    # and accepting them now would create ambiguity at v2 when
    # versioned dispatch arrives. The bool case (`version: true` →
    # Python `True`, an int subclass) is caught explicitly because
    # `True == 1` would otherwise silently accept it.
    if not isinstance(value, int) or isinstance(value, bool):
        raise MetricYamlError(
            f"version must be the integer 1 (got {value!r}, type {type(value).__name__})"
        )
    if value != _SUPPORTED_VERSION:
        raise MetricYamlError(
            f"version must be {_SUPPORTED_VERSION} (got {value}); "
            f"this parser only supports v1 metric files"
        )


def _require_str(data: dict[str, Any], field: str) -> str:
    value = data[field]
    if not isinstance(value, str):
        raise MetricYamlError(f"{field} must be a string (got {type(value).__name__}: {value!r})")
    if not value:
        raise MetricYamlError(f"{field} must be non-empty")
    return value


def _optional_str(data: dict[str, Any], field: str, *, default: str) -> str:
    if field not in data:
        return default
    value = data[field]
    if not isinstance(value, str):
        raise MetricYamlError(f"{field} must be a string (got {type(value).__name__}: {value!r})")
    return value


def _parse_measure(raw: Any) -> MetricMeasure:
    if not isinstance(raw, dict):
        raise MetricYamlError(f"measure must be a mapping (got {type(raw).__name__}: {raw!r})")
    _check_keys(
        raw,
        allowed=_ALLOWED_MEASURE_KEYS,
        required=_REQUIRED_MEASURE_KEYS,
        scope="measure",
    )

    agg_raw = raw["agg"]
    if not isinstance(agg_raw, str):
        raise MetricYamlError(
            f"measure.agg must be a string (got {type(agg_raw).__name__}: {agg_raw!r})"
        )
    if agg_raw not in _VALID_AGGS:
        raise MetricYamlError(f"measure.agg must be one of {sorted(_VALID_AGGS)} (got {agg_raw!r})")

    has_column = "column" in raw
    has_expression = "expression" in raw
    # XOR: surface the wrong-shape mistake here with a clear message
    # naming both fields, before the dataclass's `__post_init__` would
    # produce a less helpful "exactly one of column or expression" wrap.
    if has_column == has_expression:
        if has_column:
            raise MetricYamlError(
                "measure must set exactly one of `column` or `expression`, not both"
            )
        raise MetricYamlError(
            "measure must set one of `column` (bare-column measure) or "
            "`expression` (composite arithmetic over multiple columns)"
        )

    column_raw: str | None = None
    expression_raw: str | None = None
    if has_column:
        column_raw = raw["column"]
        if not isinstance(column_raw, str):
            raise MetricYamlError(
                f"measure.column must be a string (got {type(column_raw).__name__}: {column_raw!r})"
            )
    else:
        expression_raw = raw["expression"]
        if not isinstance(expression_raw, str):
            raise MetricYamlError(
                f"measure.expression must be a string (got "
                f"{type(expression_raw).__name__}: {expression_raw!r})"
            )

    try:
        # The Literal narrowing on `agg` is via the runtime check above.
        return MetricMeasure(
            agg=agg_raw,  # type: ignore[arg-type]
            column=column_raw,
            expression=expression_raw,
        )
    except ValueError as exc:
        raise MetricYamlError(f"measure: {exc}") from exc


def _parse_time(
    dim_raw: Any,
    grains_raw: Any,
    *,
    has_time_dimension: bool,
    has_time_grains: bool,
) -> tuple[str | None, tuple[TimeGrain, ...]]:
    # The dataclass enforces parallel-emptiness, but surfacing the
    # error at parse-time with a field name makes the CLI message
    # clearer than a downstream "validation failed" wrap.
    if has_time_dimension and not has_time_grains:
        raise MetricYamlError(
            "time_dimension is set but time_grains is missing; either set both or neither"
        )
    if has_time_grains and not has_time_dimension:
        raise MetricYamlError(
            "time_grains is set but time_dimension is missing; either set both or neither"
        )
    if not has_time_dimension and not has_time_grains:
        return None, ()

    if not isinstance(dim_raw, str):
        raise MetricYamlError(
            f"time_dimension must be a string (got {type(dim_raw).__name__}: {dim_raw!r})"
        )

    if not isinstance(grains_raw, list):
        raise MetricYamlError(
            f"time_grains must be a list (got {type(grains_raw).__name__}: {grains_raw!r})"
        )
    if not grains_raw:
        raise MetricYamlError("time_grains must be a non-empty list when time_dimension is set")
    typed_grains: list[TimeGrain] = []
    for item in grains_raw:
        if not isinstance(item, str):
            raise MetricYamlError(
                f"time_grains items must be strings (got {type(item).__name__}: {item!r})"
            )
        if item not in _VALID_GRAINS:
            raise MetricYamlError(
                f"time_grains items must be one of {sorted(_VALID_GRAINS)} (got {item!r})"
            )
        # Literal narrowing via runtime check.
        typed_grains.append(item)  # type: ignore[arg-type]

    return dim_raw, tuple(typed_grains)


def _parse_origin(raw: Any) -> MetricOrigin:
    # Mirrors entity/join origin parsing — the runtime check
    # narrows `str` to the `Literal`, and `dbt_import` is RESERVED
    # for hand-authored YAML even though the dbt-metrics importer
    # ships in the same PR. Refusing here keeps the dbt-guard
    # pattern coherent: the importer is the only producer that
    # writes that origin.
    if not isinstance(raw, str):
        raise MetricYamlError(f"origin must be a string (got {type(raw).__name__}: {raw!r})")
    if raw == "dbt_import":
        raise MetricYamlError(
            "origin: dbt_import is reserved for the dbt-metrics importer; "
            "hand-authored metric files use 'manual' or 'suggested'. "
            "Run `schemabrain import dbt <manifest> --include-metrics` "
            "to import dbt-owned metrics instead."
        )
    if raw not in _VALID_ORIGINS:
        raise MetricYamlError(f"origin must be one of {sorted(_VALID_ORIGINS)} (got {raw!r})")
    # Literal narrowing via runtime check.
    return raw  # type: ignore[return-value]
