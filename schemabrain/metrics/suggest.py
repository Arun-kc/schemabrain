"""LLM-driven metric suggestion pipeline.

Reads the indexed schema + entity bindings, asks an LLM to propose
Metric candidates anchored on existing entities, and returns them
wrapped in a `MetricSuggestionResult` envelope. The persisted `Metric`
is clean (no inference artifacts); confidence/rationale ride only on
the surrounding `MetricCandidate` envelope.

The seam to the LLM is the same `LLMClient` Protocol used by
`schemabrain/entities/suggest.py` — same cost dispatch, same test
double, same adapter shape. The suggest pipeline programs against the
Protocol, not against any concrete adapter, so a future Bedrock or
Vertex adapter drops in by implementing `complete` + `cost_usd`.

Module layout:
  - `MetricCandidate` — one suggestion + transient inference metadata
  - `MetricSuggestionResult` — the run output: candidates + cost + model
  - `MetricSuggestionPipeline` — orchestration class
  - `parse_suggestions` — LLM YAML output → list[MetricCandidate]
  - `serialize_entities_for_llm` — Entity + Table list → compact text
    the LLM can reason about, plus the list of entity names that
    survived (i.e., whose bound table was present in the table set).

Unlike entity suggestion (which reads raw tables) and join suggestion
(which mines FK + query log evidence), metric suggestion is anchored on
existing entities — the v1 metric model requires every metric to bind
to one entity, so the suggester refuses with a guided error when the
entity table is empty.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast, get_args

import yaml

from schemabrain.core.entity import Entity
from schemabrain.core.metric import (
    AggFunction,
    Metric,
    MetricMeasure,
    TimeGrain,
)
from schemabrain.core.models import Table
from schemabrain.enrichment.llm import LLMClient, LLMResponse

_logger = logging.getLogger(__name__)

# Closed set of confidence levels the LLM is asked to return. Mirrors
# the `Literal` declaration on `MetricCandidate.confidence` — the
# runtime check in `__post_init__` catches malformed YAML or untyped
# callers that bypass the static check.
Confidence = Literal["high", "medium", "low"]
_VALID_CONFIDENCE: frozenset[str] = frozenset(get_args(Confidence))

# Validation frozensets derived from the public `AggFunction` and
# `TimeGrain` Literals in `core/metric.py`. Deriving locally (rather
# than importing the private `_VALID_AGGS` / `_VALID_GRAINS` sentinels)
# keeps this module's coupling to the public type aliases — if a future
# refactor renames the private sentinels, this module still compiles.
_VALID_AGGS: frozenset[str] = frozenset(get_args(AggFunction))
_VALID_GRAINS: frozenset[str] = frozenset(get_args(TimeGrain))


@dataclass(frozen=True)
class MetricCandidate:
    """One LLM-suggested metric, plus the inference metadata that
    surrounds it during review.

    The wrapped `Metric` is the persisted-clean shape — once a user
    runs `metrics suggest --apply`, only the `Metric` flows into the
    store (with `origin="suggested"`). The fields below this comment
    are transient: they appear in dry-run output but never in the
    canonical metric YAML written to disk.

    Fields:
      metric: The persisted-clean Metric. Its own __post_init__ has
          already validated name shape, entity binding, measure shape,
          paired time_dimension/time_grains, canonical grain ordering,
          and origin. The pipeline writes `origin="suggested"` before
          constructing.
      confidence: One of "high", "medium", "low". Categorical (not
          float) because LLMs do not have calibrated probability
          awareness — a 0.87 from a generation model is theater.
      rationale: The LLM's own reasoning, surfaced verbatim to the
          human reviewing candidates. May be empty if the LLM omitted
          it; the CLI displays "(no rationale provided)" in that case.
    """

    metric: Metric
    confidence: Confidence
    rationale: str

    def __post_init__(self) -> None:
        if self.confidence not in _VALID_CONFIDENCE:
            raise ValueError(
                f"confidence must be one of {sorted(_VALID_CONFIDENCE)} (got {self.confidence!r})"
            )


@dataclass(frozen=True)
class MetricSuggestionResult:
    """Aggregate output of one suggest-pipeline run.

    `candidates` is a tuple (not list) so the dataclass stays hashable
    and a caller can't mutate the result under the pipeline's nose.
    """

    candidates: tuple[MetricCandidate, ...]
    total_cost_usd: float
    llm_model: str

    def __post_init__(self) -> None:
        if self.total_cost_usd < 0:
            raise ValueError(f"total_cost_usd must be non-negative (got {self.total_cost_usd!r})")
        if not self.llm_model:
            raise ValueError("llm_model must be a non-empty string")


class MetricSuggestionParseError(ValueError):
    """Raised when the LLM's response cannot be parsed into candidates.

    Subclass of `ValueError` so callers that just want to catch "bad
    LLM output" can do so without importing this module. The CLI
    catches the specific type for its exit-code contract.
    """


# Closed sets used for strict candidate-key validation. Mirrors the
# defensive policy in `metrics/yaml_grammar._check_keys` — an unknown
# key is almost always LLM hallucination we shouldn't silently absorb.
_CANDIDATE_REQUIRED_KEYS: frozenset[str] = frozenset(
    {
        "name",
        "entity",
        "measure",
        "confidence",
    }
)
_CANDIDATE_ALLOWED_KEYS: frozenset[str] = _CANDIDATE_REQUIRED_KEYS | {
    "description",
    "rationale",
    "time_dimension",
    "time_grains",
}
_TOP_LEVEL_ALLOWED_KEYS: frozenset[str] = frozenset({"candidates"})

# Measure sub-mapping shape. Mirrors `_ALLOWED_MEASURE_KEYS` from
# `metrics/yaml_grammar.py` — drift here would silently change what the
# parser accepts compared to hand-authored YAML.
_MEASURE_REQUIRED_KEYS: frozenset[str] = frozenset({"agg", "column"})
_MEASURE_ALLOWED_KEYS: frozenset[str] = _MEASURE_REQUIRED_KEYS


# ----- system prompt ---------------------------------------------------------
#
# The system prompt names the output grammar so the model emits YAML
# we can parse without negotiation. It mirrors the structure pinned by
# `parse_suggestions` below — drift here without parser changes would
# silently degrade precision. Keep them in lock-step.

_SYSTEM_PROMPT = """You are a semantic-layer architect proposing business metrics.

A METRIC is a named, validator-backed business measure anchored on one
ENTITY. It carries a single aggregation over a column on that entity's
table, plus an optional time dimension for bucketing.

You are given:
  - A list of ENTITIES, each with its bound physical table and column
    list (with types, NULL/PK markers, and FK pointers).
  - The full set of physical columns the entity touches.

Output STRICT YAML with this shape (no markdown fences, no commentary
outside YAML):

candidates:
  - name: total_revenue                       # snake_case identifier
    description: One sentence about it
    entity: order                             # one of the listed entity names
    measure:
      agg: sum                                # closed: sum|count|count_distinct|avg|min|max
      column: total_cents                     # column on the entity's bound table
    time_dimension: order.placed_at           # OPTIONAL; entity.column form
    time_grains: [day, week, month]           # REQUIRED iff time_dimension set
    confidence: high                          # one of: high | medium | low
    rationale: One sentence on why this is a useful metric

Rules:
  - `name` must match ^[A-Za-z_][A-Za-z0-9_$]*$ and be unique across
    your candidates.
  - `entity` must be one of the entity names listed in the user prompt.
    Anchoring on an unknown entity will fail the store-side foreign
    key check at apply time.
  - `measure.column` must be a column that exists on the entity's
    bound table.
  - `measure.agg` must be one of: sum, count, count_distinct, avg,
    min, max. Use `count` for row counts (column can be the entity's
    identity column); `count_distinct` for unique-value counts; sum/
    avg/min/max for numeric columns only.
  - `time_dimension` is optional. When set, it MUST be
    `<entity>.<column>` form where `<entity>` is the same as `entity`
    above (cross-entity time dimensions deferred to v2), and
    `<column>` is a timestamp/datetime/date column on that entity's
    bound table.
  - `time_grains` is REQUIRED when `time_dimension` is set and FORBIDDEN
    when it isn't. Values are a subset of [day, week, month, quarter,
    year]. Include grains the metric is genuinely useful at — don't
    pad. List them in canonical order day → year.
  - DO NOT propose metrics on identity columns with sum/avg (an `id`
    column's sum is meaningless); use count or count_distinct instead.
  - DO NOT propose more than 2 metrics per entity unless the entity is
    genuinely metric-rich (e.g., `order` may warrant 3-4: revenue,
    item-count, average-order-value, distinct-customer-count).
  - SKIP junction tables and pure-audit/log entities — they don't host
    useful business metrics.
  - Confidence: `high` if the column name+type cleanly imply the
    aggregation (e.g., `total_cents` + sum, `placed_at` + time
    dimension); `medium` if the inference required a small leap (e.g.,
    `created_at` as a time dimension when the entity is something
    other than its creation event); `low` if you're unsure.
"""


# ----- schema serialization --------------------------------------------------


def serialize_entities_for_llm(
    entities: Iterable[Entity],
    tables: Iterable[Table],
) -> tuple[str, list[str]]:
    """Render `entities` + `tables` into compact text the LLM can reason about.

    Returns the serialized text AND the list of entity names that
    survived serialization (those whose bound table was present in
    `tables`). The user-prompt builder uses the surviving-names list
    so the LLM is only invited to anchor on entities it has schema
    blocks for. Without that coupling, a partial entity/table mismatch
    (5 entities, 4 backing tables) would let the LLM hallucinate
    column names for the 5th entity.

    Format (one block per surviving entity, blank line between):

        entity: customer
        bound to: public.users
        identity: id
        columns:
          id bigint NOT NULL PK
          email text
          created_at timestamp NOT NULL
        foreign_keys:
          (none)

        entity: order
        bound to: public.orders
        identity: id
        columns:
          id bigint NOT NULL PK
          user_id bigint NOT NULL FK -> public.users(id)
          total_cents int NOT NULL
          placed_at timestamp NOT NULL
        foreign_keys:
          user_id -> public.users(id)

    Entities whose bound table isn't in `tables` are dropped from the
    output AND logged at WARNING so an operator debugging "why isn't
    my order entity getting metrics?" sees the drop in stderr. The CLI
    surface refuses upstream when this set comes out entirely empty.
    """
    table_by_qn: dict[str, Table] = {t.qualified_name: t for t in tables}
    blocks: list[str] = []
    surviving_names: list[str] = []
    for entity in entities:
        table = table_by_qn.get(entity.binding.qualified_table)
        if table is None:
            # Operationally rare — would mean the entity's bound table
            # wasn't indexed (or was dropped after the entity was
            # written). Warn loudly so a partial mismatch doesn't
            # silently narrow the LLM's view of the schema; the
            # operator sees which entity got dropped and from which
            # table.
            _logger.warning(
                "metric suggester dropping entity %r: bound table %r not in indexed table set",
                entity.name,
                entity.binding.qualified_table,
            )
            continue
        lines: list[str] = [
            f"entity: {entity.name}",
            f"bound to: {entity.binding.qualified_table}",
            f"identity: {entity.identity}",
            "columns:",
        ]
        for col in table.columns:
            modifiers: list[str] = []
            if not col.nullable:
                modifiers.append("NOT NULL")
            if col.is_primary_key:
                modifiers.append("PK")
            suffix = " " + " ".join(modifiers) if modifiers else ""
            lines.append(f"  {col.name} {col.data_type}{suffix}")
        if table.foreign_keys:
            lines.append("foreign_keys:")
            for fk in table.foreign_keys:
                src = ",".join(fk.source_columns)
                tgt = f"{fk.target_schema}.{fk.target_table}({','.join(fk.target_columns)})"
                lines.append(f"  {src} -> {tgt}")
        else:
            lines.append("foreign_keys:")
            lines.append("  (none)")
        blocks.append("\n".join(lines))
        surviving_names.append(entity.name)
    return "\n\n".join(blocks), surviving_names


def _build_user_prompt(
    entities: Sequence[Entity],
    tables: Sequence[Table],
    *,
    top_k: int,
) -> str:
    """User-side prompt: the serialized entity+table set + the top-k cap.

    The cap rides on the user prompt (not the system prompt) so a
    caller varying `top_k` doesn't bust the system-prompt cache for
    Anthropic adapters that mark the system prefix as ephemeral.

    The entity-name list shown to the LLM is derived from the entities
    that survived serialization (i.e., whose bound table was present),
    not from the raw input. Without that coupling, the LLM is invited
    to anchor on entities it has no schema block for, then either
    hallucinates a column or silently drops them.
    """
    schema_text, surviving_names = serialize_entities_for_llm(entities, tables)
    return (
        f"Return at most {top_k} metric candidates, ordered by confidence "
        f"(highest first). Anchor each candidate on one of these entities: "
        f"{', '.join(sorted(surviving_names))}.\n\n"
        f"Schema follows:\n\n{schema_text}"
    )


# ----- parser ----------------------------------------------------------------


def parse_suggestions(text: str) -> list[MetricCandidate]:
    """Parse an LLM YAML response into a list of `MetricCandidate`.

    Strict-keys, strict-shape — a single malformed candidate fails
    the whole batch. Partial acceptance would silently let the LLM
    dictate schema drift (e.g. typo'd `confidance` becoming a new
    optional key), which we explicitly do not want.

    Raises `MetricSuggestionParseError` (a `ValueError` subclass) on
    any structural or validation failure. The message names the
    offending field so the CLI surface can render it verbatim.
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise MetricSuggestionParseError(f"failed to parse YAML: {exc}") from exc

    if data is None:
        raise MetricSuggestionParseError("suggestion YAML is empty")
    if not isinstance(data, dict):
        raise MetricSuggestionParseError(
            f"suggestion YAML must be a mapping at the top level (got {type(data).__name__})"
        )

    keys = set(data.keys())
    if "candidates" not in keys:
        raise MetricSuggestionParseError("missing required top-level key 'candidates'")
    unknown = keys - _TOP_LEVEL_ALLOWED_KEYS
    if unknown:
        raise MetricSuggestionParseError(f"unknown top-level keys: {sorted(unknown)}")

    raw_candidates = data["candidates"]
    if not isinstance(raw_candidates, list):
        raise MetricSuggestionParseError(
            f"candidates must be a list (got {type(raw_candidates).__name__})"
        )

    return [_parse_candidate(item, index=i) for i, item in enumerate(raw_candidates)]


def _parse_candidate(raw: Any, *, index: int) -> MetricCandidate:
    """Parse one item from the `candidates` list. `index` is for error context."""
    if not isinstance(raw, dict):
        raise MetricSuggestionParseError(
            f"candidate #{index} must be a mapping (got {type(raw).__name__})"
        )

    keys = set(raw.keys())
    missing = _CANDIDATE_REQUIRED_KEYS - keys
    if missing:
        raise MetricSuggestionParseError(
            f"candidate #{index} missing required field(s): {sorted(missing)}"
        )
    unknown = keys - _CANDIDATE_ALLOWED_KEYS
    if unknown:
        raise MetricSuggestionParseError(
            f"candidate #{index} has unknown field(s): {sorted(unknown)}"
        )

    name = _require_str(raw, "name", index=index)
    description = _optional_str(raw, "description", default="", index=index)
    entity = _require_str(raw, "entity", index=index)
    rationale = _optional_str(raw, "rationale", default="", index=index)
    confidence = _parse_confidence(raw["confidence"], index=index)
    measure = _parse_measure(raw["measure"], index=index)

    time_dimension, time_grains = _parse_time(
        raw.get("time_dimension"),
        raw.get("time_grains"),
        has_time_dimension="time_dimension" in raw,
        has_time_grains="time_grains" in raw,
        index=index,
    )

    # Construct the Metric — its __post_init__ runs identifier-shape +
    # paired-emptiness + canonical-grain-order + origin-enum checks.
    # Re-wrap as MetricSuggestionParseError so the CLI surface stays
    # uniform.
    try:
        metric = Metric(
            name=name,
            description=description,
            entity=entity,
            measure=measure,
            time_dimension=time_dimension,
            time_grains=time_grains,
            origin="suggested",
        )
    except ValueError as exc:
        raise MetricSuggestionParseError(f"candidate #{index}: {exc}") from exc

    # By the time we reach here, `_parse_confidence` has already validated
    # the envelope's confidence field — `MetricCandidate.__post_init__`
    # would only re-check what that helper enforces, so construction
    # cannot raise. Keep this direct (no try/except) so a future
    # contributor who weakens the helper sees the change surface in a
    # test, not get masked by an unreachable except.
    return MetricCandidate(
        metric=metric,
        confidence=confidence,
        rationale=rationale,
    )


def _require_str(data: dict[str, Any], field: str, *, index: int) -> str:
    value = data[field]
    if not isinstance(value, str):
        raise MetricSuggestionParseError(
            f"candidate #{index}.{field} must be a string (got {type(value).__name__}: {value!r})"
        )
    if not value:
        raise MetricSuggestionParseError(f"candidate #{index}.{field} must be non-empty")
    return value


def _optional_str(data: dict[str, Any], field: str, *, default: str, index: int) -> str:
    if field not in data:
        return default
    value = data[field]
    if not isinstance(value, str):
        raise MetricSuggestionParseError(
            f"candidate #{index}.{field} must be a string (got {type(value).__name__}: {value!r})"
        )
    return value


def _parse_confidence(raw: Any, *, index: int) -> Confidence:
    if not isinstance(raw, str):
        raise MetricSuggestionParseError(
            f"candidate #{index}.confidence must be a string (got {type(raw).__name__}: {raw!r})"
        )
    if raw not in _VALID_CONFIDENCE:
        raise MetricSuggestionParseError(
            f"candidate #{index}.confidence must be one of "
            f"{sorted(_VALID_CONFIDENCE)} (got {raw!r})"
        )
    # The `raw not in _VALID_CONFIDENCE` guard above narrowed `str` to the
    # closed Literal at runtime. `cast` documents that intent for the
    # static checker without changing behavior — strictly preferable to
    # `# type: ignore`, which suppresses the entire line including any
    # future real mismatch a refactor might introduce.
    return cast("Confidence", raw)


def _parse_measure(raw: Any, *, index: int) -> MetricMeasure:
    if not isinstance(raw, dict):
        raise MetricSuggestionParseError(
            f"candidate #{index}.measure must be a mapping (got {type(raw).__name__})"
        )
    keys = set(raw.keys())
    missing = _MEASURE_REQUIRED_KEYS - keys
    if missing:
        raise MetricSuggestionParseError(
            f"candidate #{index}.measure missing required field(s): {sorted(missing)}"
        )
    unknown = keys - _MEASURE_ALLOWED_KEYS
    if unknown:
        raise MetricSuggestionParseError(
            f"candidate #{index}.measure has unknown field(s): {sorted(unknown)}"
        )

    agg_raw = raw["agg"]
    if not isinstance(agg_raw, str):
        raise MetricSuggestionParseError(
            f"candidate #{index}.measure.agg must be a string "
            f"(got {type(agg_raw).__name__}: {agg_raw!r})"
        )
    if agg_raw not in _VALID_AGGS:
        raise MetricSuggestionParseError(
            f"candidate #{index}.measure.agg must be one of {sorted(_VALID_AGGS)} (got {agg_raw!r})"
        )

    column_raw = raw["column"]
    if not isinstance(column_raw, str):
        raise MetricSuggestionParseError(
            f"candidate #{index}.measure.column must be a string "
            f"(got {type(column_raw).__name__}: {column_raw!r})"
        )

    try:
        # `agg_raw not in _VALID_AGGS` rejected non-Literal values above;
        # `cast` documents that narrowing for the type checker.
        return MetricMeasure(agg=cast("AggFunction", agg_raw), column=column_raw)
    except ValueError as exc:
        raise MetricSuggestionParseError(f"candidate #{index}.measure: {exc}") from exc


def _parse_time(
    dim_raw: Any,
    grains_raw: Any,
    *,
    has_time_dimension: bool,
    has_time_grains: bool,
    index: int,
) -> tuple[str | None, tuple[TimeGrain, ...]]:
    # The dataclass enforces parallel-emptiness, but surfacing the
    # error at parse-time with a field name makes the CLI message
    # clearer than a downstream "validation failed" wrap.
    if has_time_dimension and not has_time_grains:
        raise MetricSuggestionParseError(
            f"candidate #{index}: time_dimension is set but time_grains "
            "is missing; either set both or neither"
        )
    if has_time_grains and not has_time_dimension:
        raise MetricSuggestionParseError(
            f"candidate #{index}: time_grains is set but time_dimension "
            "is missing; either set both or neither"
        )
    if not has_time_dimension and not has_time_grains:
        return None, ()

    if not isinstance(dim_raw, str):
        raise MetricSuggestionParseError(
            f"candidate #{index}.time_dimension must be a string "
            f"(got {type(dim_raw).__name__}: {dim_raw!r})"
        )

    if not isinstance(grains_raw, list):
        raise MetricSuggestionParseError(
            f"candidate #{index}.time_grains must be a list "
            f"(got {type(grains_raw).__name__}: {grains_raw!r})"
        )
    if not grains_raw:
        raise MetricSuggestionParseError(
            f"candidate #{index}.time_grains must be a non-empty list when time_dimension is set"
        )
    typed_grains: list[TimeGrain] = []
    for item in grains_raw:
        if not isinstance(item, str):
            raise MetricSuggestionParseError(
                f"candidate #{index}.time_grains items must be strings "
                f"(got {type(item).__name__}: {item!r})"
            )
        if item not in _VALID_GRAINS:
            raise MetricSuggestionParseError(
                f"candidate #{index}.time_grains items must be one of "
                f"{sorted(_VALID_GRAINS)} (got {item!r})"
            )
        # `item not in _VALID_GRAINS` rejected non-Literal values above;
        # `cast` documents that narrowing for the type checker.
        typed_grains.append(cast("TimeGrain", item))

    return dim_raw, tuple(typed_grains)


# ----- pipeline orchestration ------------------------------------------------


class MetricSuggestionPipeline:
    """Orchestrates LLM-driven metric suggestion against indexed entities.

    The pipeline holds a reference to an `LLMClient` (the existing
    Protocol from `enrichment/llm.py`) and threads `complete` calls
    through it. All cost reporting routes through `llm.cost_usd(usage)`
    so the pipeline never embeds pricing knowledge — a future provider
    adapter prices its own calls.

    `propose_from_entities` is the public entry point: entities + tables
    + top_k in, `MetricSuggestionResult` out. The same `CostCeilingGuard`
    used by `entities suggest` layers on top — `_invoke_llm` is the
    seam the guard wraps.
    """

    def __init__(self, llm: LLMClient) -> None:
        # Private attribute: the LLM client is an implementation
        # detail, not part of the pipeline's public surface. A future
        # refactor that wraps `llm` (e.g., add retry, observability)
        # must not be silently bypassable by callers holding a
        # reference to `.llm`. Tests access `_llm` directly via the
        # name-mangled-not-applied single-underscore convention.
        self._llm = llm

    def propose_from_entities(
        self,
        entities: Sequence[Entity],
        tables: Sequence[Table],
        *,
        top_k: int = 10,
    ) -> MetricSuggestionResult:
        """Generate metric candidates from a list of entities + tables.

        Single LLM call per run — no retries, no chunking at v1. The
        caller (CLI) is responsible for sizing the input appropriately;
        a 500-entity schema in one prompt is the user's problem until
        we ship chunking.

        Args:
          entities: Non-empty list of indexed entities to propose metrics for.
          tables: Non-empty list of physical tables backing those entities.
          top_k: Maximum number of candidates to keep. The cap is also
              communicated to the LLM via the user prompt, but enforced
              defensively after parse in case the LLM over-produces.

        Raises:
          ValueError: if `entities` or `tables` is empty, or `top_k <= 0`.
          MetricSuggestionParseError: if the LLM's response is unparseable.
        """
        if not entities:
            raise ValueError("propose_from_entities requires at least one entity")
        if not tables:
            raise ValueError("propose_from_entities requires at least one table")
        if top_k <= 0:
            raise ValueError(f"top_k must be a positive integer (got {top_k!r})")

        user_prompt = _build_user_prompt(entities, tables, top_k=top_k)
        response = self._invoke_llm(system=_SYSTEM_PROMPT, user=user_prompt)
        cost = self._cost_for(response)
        candidates = parse_suggestions(response.text)
        truncated = candidates[:top_k]
        return MetricSuggestionResult(
            candidates=tuple(truncated),
            total_cost_usd=cost,
            llm_model=response.model,
        )

    def _invoke_llm(self, *, system: str, user: str) -> LLMResponse:
        """Single-turn completion via the wrapped client.

        The single round-trip point for LLM calls. The cost-ceiling
        guard wraps this in a follow-up step — keeping the round-trip
        in one place keeps the ceiling check tractable.
        """
        return self._llm.complete(system=system, user=user)

    def _cost_for(self, response: LLMResponse) -> float:
        """USD cost for one `LLMResponse`, dispatched through the client.

        The pipeline does not compute cost itself. `LLMClient.cost_usd`
        is the seam's contractual single source of truth for pricing
        — corruption here would silently break the cost-ceiling guard
        that layers on top of this pipeline.
        """
        return self._llm.cost_usd(response.usage)


# ----- public re-exports for testing -----------------------------------------
#
# The CLI imports these names from the module. Grouped at the bottom so
# the public surface is visible at a glance.

__all__ = [
    "Confidence",
    "MetricCandidate",
    "MetricSuggestionParseError",
    "MetricSuggestionPipeline",
    "MetricSuggestionResult",
    "parse_suggestions",
    "serialize_entities_for_llm",
]
