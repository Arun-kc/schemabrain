"""LLM-driven entity suggestion pipeline.

Reads indexed schema, asks an LLM to propose Entity candidates, and
returns them wrapped in a `SuggestionResult` envelope. The persisted
`Entity` is clean (no inference artifacts); confidence/rationale/PII
hints ride only on the surrounding `EntityCandidate` envelope.

The seam to the LLM is the existing `LLMClient` Protocol from
`schemabrain/enrichment/llm.py` — same Protocol the enrichment
pipeline uses, same cost-dispatch shape, same test double
(`FakeLLMClient`). The suggest pipeline programs against the Protocol,
not against any concrete adapter, so a future Bedrock or Vertex
adapter drops in by implementing `complete` + `cost_usd`.

Module layout:
  - `EntityCandidate` — one suggestion + transient inference metadata
  - `SuggestionResult` — the run output: candidates + cost + model
  - `EntitySuggestionPipeline` — orchestration class
  - `parse_suggestions` — LLM YAML output → list[EntityCandidate]
  - `_serialize_schema` — Table list → compact text for the LLM
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Literal, get_args

import yaml

from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.models import Table
from schemabrain.enrichment.llm import LLMClient, LLMResponse, LLMUsage
from schemabrain.pii.categories import SENSITIVITIES, Sensitivity

# Closed set of confidence levels the LLM is asked to return. Mirrors
# the `Literal` declaration on `EntityCandidate.confidence` — the
# runtime check in `__post_init__` catches malformed YAML or untyped
# callers that bypass the static check.
Confidence = Literal["high", "medium", "low"]
_VALID_CONFIDENCE: frozenset[str] = frozenset(get_args(Confidence))

# Closed set of valid Sensitivity values for pii_hints. Sourced from
# the canonical `SENSITIVITIES` tuple in `schemabrain/pii/categories.py`
# so a future addition to the taxonomy lands in one place.
_VALID_SENSITIVITIES: frozenset[str] = frozenset(SENSITIVITIES)


@dataclass(frozen=True)
class EntityCandidate:
    """One LLM-suggested entity, plus the inference metadata that
    surrounds it during review.

    The wrapped `Entity` is the persisted-clean shape — once a user
    runs `entities suggest --apply`, only the `Entity` flows into the
    store (with `origin="suggested"`). The fields below this comment
    are transient: they appear in dry-run output and the audit log,
    but never in the canonical entity YAML written to disk.

    Fields:
      entity: The persisted-clean Entity. Its own __post_init__ has
          already validated name shape, binding form, identity, and
          origin. The pipeline writes `origin="suggested"` before
          constructing.
      confidence: One of "high", "medium", "low". Categorical (not
          float) because LLMs do not have calibrated probability
          awareness — a 0.87 from a generation model is theater.
      rationale: The LLM's own reasoning, surfaced verbatim to the
          human reviewing candidates. May be empty if the LLM omitted
          it; the CLI displays "(no rationale provided)" in that case.
      pii_hints: column-name -> proposed Sensitivity. Forward-
          compatible bookkeeping — when a future PII validator lands,
          a one-shot migration can promote these hints to actual
          `Entity.pii_sensitivity` values. Until then, the persisted
          Entity continues to report `pii_sensitivity="public"` (the
          current hardcoded value on every persisted entity).
    """

    entity: Entity
    confidence: Confidence
    rationale: str
    # Declared as `Mapping` (read-only view), not `dict`. `frozen=True`
    # blocks reassignment but does not block in-place mutation of a
    # dict-typed field — callers holding a reference could mutate the
    # underlying dict and bypass the validation in __post_init__. We
    # accept a dict at construction (ergonomic for callers) and wrap
    # it in a `MappingProxyType` (read-only view) via the
    # `object.__setattr__` escape hatch frozen dataclasses provide
    # for __post_init__ mutation.
    pii_hints: Mapping[str, Sensitivity]

    def __post_init__(self) -> None:
        if self.confidence not in _VALID_CONFIDENCE:
            raise ValueError(
                f"confidence must be one of {sorted(_VALID_CONFIDENCE)} (got {self.confidence!r})"
            )
        for column, sensitivity in self.pii_hints.items():
            if sensitivity not in _VALID_SENSITIVITIES:
                raise ValueError(
                    f"pii_hints[{column!r}] must be one of "
                    f"{sorted(_VALID_SENSITIVITIES)} (got {sensitivity!r})"
                )
        # Wrap with MappingProxyType so the caller can't mutate
        # pii_hints after construction. dict() copy first so a later
        # mutation of the source dict doesn't leak in either.
        object.__setattr__(self, "pii_hints", MappingProxyType(dict(self.pii_hints)))

    def to_persisted_entity(self) -> Entity:
        """Return the Entity to persist on the direct `--apply` path.

        The wrapped `entity` is the persisted-clean shape (no inference
        artifacts). v15 adds two store columns — `bind_confidence` and
        `rationale` — that capture the model's self-rating of the
        binding. This is the one seam that threads the transient
        envelope fields onto the persisted Entity, so the apply path
        (`_render_apply`) can't silently drop them or drift in how it
        maps them.

        `confidence` is already validated against the same `{high,
        medium, low}` set as `Entity.bind_confidence`, so the `replace`
        re-running `Entity.__post_init__` is a no-op guard, not a risk.

        Note the asymmetry with the file-review workflow: `entities
        suggest` (without `--apply`) writes YAML whose envelope fields
        are *comments*, and `entity_to_yaml` never emits these two
        fields, so an export→edit→apply round-trip resets them to NULL.
        That is intentional — once an operator edits the YAML they have
        authored the binding, and the model's transient self-rating no
        longer applies (the same reset `inference_method` /
        `validation_state` already get).
        """
        return replace(
            self.entity,
            bind_confidence=self.confidence,
            rationale=self.rationale,
        )


@dataclass(frozen=True)
class SuggestionResult:
    """Aggregate output of one suggest-pipeline run.

    `candidates` is a tuple (not list) so the dataclass stays hashable
    and a caller can't mutate the result under the pipeline's nose.
    """

    candidates: tuple[EntityCandidate, ...]
    total_cost_usd: float
    llm_model: str

    def __post_init__(self) -> None:
        if self.total_cost_usd < 0:
            raise ValueError(f"total_cost_usd must be non-negative (got {self.total_cost_usd!r})")
        if not self.llm_model:
            raise ValueError("llm_model must be a non-empty string")


class SuggestionParseError(ValueError):
    """Raised when the LLM's response cannot be parsed into candidates.

    Subclass of `ValueError` so callers that just want to catch "bad
    LLM output" can do so without importing this module. The CLI
    catches the specific type for its exit-code contract.
    """


# Closed sets used for strict candidate-key validation. Mirrors the
# defensive policy in `yaml_grammar._check_keys` — an unknown key is
# almost always LLM hallucination we shouldn't silently absorb.
_CANDIDATE_REQUIRED_KEYS: frozenset[str] = frozenset(
    {
        "name",
        "binding",
        "identity",
        "confidence",
    }
)
_CANDIDATE_ALLOWED_KEYS: frozenset[str] = _CANDIDATE_REQUIRED_KEYS | {
    "description",
    "rationale",
    "pii_hints",
}
_TOP_LEVEL_ALLOWED_KEYS: frozenset[str] = frozenset({"candidates"})

# Binding shapes the parser *recognises*. Same set as
# `yaml_grammar._KNOWN_BINDING_KEYS` — `multi_table` is recognised so
# its presence triggers a "deferred to v2" error rather than a generic
# "unknown shape" message.
_KNOWN_BINDING_KEYS: frozenset[str] = frozenset({"single_table", "multi_table"})


# ----- system prompt ---------------------------------------------------------
#
# The system prompt names the output grammar so the model emits YAML
# we can parse without negotiation. It mirrors the structure pinned by
# `parse_suggestions` below — drift here without parser changes would
# silently degrade precision. Keep them in lock-step.

_SYSTEM_PROMPT = """You are a semantic-layer architect analyzing a database schema.

Identify domain entities the schema represents. An ENTITY is a named,
single-table concept that:
  - Maps to exactly one physical table (multi-table entities are out of scope)
  - Has an identity column that uniquely identifies one instance
  - Represents a real domain concept derived from the schema's own
    vocabulary (e.g. patient/encounter/medication in a clinical schema,
    account/transaction/security in a financial schema, customer/order
    in a commerce schema, case/filing/party in a legal schema) —
    NOT a technical artifact (audit log, association/junction table)

Output STRICT YAML with this shape (no markdown fences, no commentary
outside YAML):

candidates:
  - name: <entity_name>                     # snake_case identifier derived from the table's domain meaning
    description: One sentence about it
    binding:
      single_table: <schema>.<table>        # schema.table — must exist in the schema
    identity: <pk_column>                   # PK column on the bound table
    confidence: high                        # one of: high | medium | low
    rationale: One sentence on why this is an entity
    pii_hints:                              # OPTIONAL: column -> sensitivity
      <column_name>: pii                    # one of: public | internal | confidential | pii

Rules:
  - `name` and `identity` must match ^[A-Za-z_][A-Za-z0-9_$]*$
  - `binding.single_table` must be exactly "schema.table" (one dot)
  - Skip junction tables (composite PK whose every column is part of an FK)
  - Skip pure-audit/log tables
  - For pii_hints, infer from column names + types. The taxonomy is
    domain-agnostic — apply it to whatever schema you see:
      Direct personal identifiers (email, phone, full_name, ssn,
        national_id, dob, mrn, patient_id, wallet_address, ip) -> pii
      Operational tenancy keys (tenant_id, organization_id,
        workspace_id) -> internal
      Aggregates and metadata (timestamps, booleans, counts,
        flags) -> public
  - Confidence: `high` if PK + multiple referencing FKs;
    `medium` if PK + clear semantic name; `low` otherwise.
"""


# ----- schema serialization --------------------------------------------------


def _serialize_schema(tables: Iterable[Table]) -> str:
    """Render `tables` into compact text the LLM can reason about.

    Format (one block per table, blank line between):

        public.users
          id bigint NOT NULL PRIMARY KEY
          email text NOT NULL
          nick text
          FK: user_id -> public.users(id)

    Structural facts only — no sample rows, no enrichment descriptions
    at v1. Adding them multiplies input cost without clear precision
    gain at this stage; revisit when eval shows ambiguity on names.
    """
    blocks: list[str] = []
    for table in tables:
        lines: list[str] = [table.qualified_name]
        for col in table.columns:
            modifiers: list[str] = []
            if not col.nullable:
                modifiers.append("NOT NULL")
            if col.is_primary_key:
                modifiers.append("PRIMARY KEY")
            suffix = " " + " ".join(modifiers) if modifiers else ""
            lines.append(f"  {col.name} {col.data_type}{suffix}")
        for fk in table.foreign_keys:
            src = ",".join(fk.source_columns)
            target = f"{fk.target_schema}.{fk.target_table}({','.join(fk.target_columns)})"
            lines.append(f"  FK: {src} -> {target}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _build_user_prompt(tables: Iterable[Table], *, top_k: int) -> str:
    """User-side prompt: the serialized schema + the top-k cap.

    The cap rides on the user prompt (not the system prompt) so a
    caller varying `top_k` doesn't bust the system-prompt cache for
    Anthropic adapters that mark the system prefix as ephemeral.
    """
    schema_text = _serialize_schema(tables)
    return (
        f"Return at most {top_k} entity candidates, ordered by confidence "
        f"(highest first). Schema follows:\n\n{schema_text}"
    )


# ----- parser ----------------------------------------------------------------


def parse_suggestions(text: str) -> list[EntityCandidate]:
    """Parse an LLM YAML response into a list of `EntityCandidate`.

    Strict-keys, strict-shape — a single malformed candidate fails
    the whole batch. Partial acceptance would silently let the LLM
    dictate schema drift (e.g. typo'd `confidance` becoming a new
    optional key), which we explicitly do not want.

    Raises `SuggestionParseError` (a `ValueError` subclass) on any
    structural or validation failure. The message names the offending
    field so the CLI surface can render it verbatim.
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SuggestionParseError(f"failed to parse YAML: {exc}") from exc

    if data is None:
        raise SuggestionParseError("suggestion YAML is empty")
    if not isinstance(data, dict):
        raise SuggestionParseError(
            f"suggestion YAML must be a mapping at the top level (got {type(data).__name__})"
        )

    keys = set(data.keys())
    if "candidates" not in keys:
        raise SuggestionParseError("missing required top-level key 'candidates'")
    unknown = keys - _TOP_LEVEL_ALLOWED_KEYS
    if unknown:
        raise SuggestionParseError(f"unknown top-level keys: {sorted(unknown)}")

    raw_candidates = data["candidates"]
    if not isinstance(raw_candidates, list):
        raise SuggestionParseError(
            f"candidates must be a list (got {type(raw_candidates).__name__})"
        )

    return [_parse_candidate(item, index=i) for i, item in enumerate(raw_candidates)]


def _parse_candidate(raw: Any, *, index: int) -> EntityCandidate:
    """Parse one item from the `candidates` list. `index` is for error context."""
    if not isinstance(raw, dict):
        raise SuggestionParseError(
            f"candidate #{index} must be a mapping (got {type(raw).__name__})"
        )

    keys = set(raw.keys())
    missing = _CANDIDATE_REQUIRED_KEYS - keys
    if missing:
        raise SuggestionParseError(
            f"candidate #{index} missing required field(s): {sorted(missing)}"
        )
    unknown = keys - _CANDIDATE_ALLOWED_KEYS
    if unknown:
        raise SuggestionParseError(f"candidate #{index} has unknown field(s): {sorted(unknown)}")

    name = _require_str(raw, "name", index=index)
    identity = _require_str(raw, "identity", index=index)
    description = _optional_str(raw, "description", default="", index=index)
    rationale = _optional_str(raw, "rationale", default="", index=index)
    confidence = _parse_confidence(raw["confidence"], index=index)
    binding = _parse_binding(raw["binding"], index=index)
    pii_hints = _parse_pii_hints(raw.get("pii_hints", {}), index=index)

    # Construct the Entity — its __post_init__ runs identifier-shape +
    # origin-enum checks. Re-wrap as SuggestionParseError so the CLI
    # surface stays uniform. `inference_method="llm_suggested"` is the
    # load-bearing 2D trust-signal classification: every candidate
    # parsed here came from an LLM's YAML output, so the entity must
    # ride the `llm_suggested` rail (→ MEDIUM confidence) rather than
    # the dataclass-default `manually_authored` (→ HIGH). Without this,
    # `derive_confidence(method, state)` collapses to HIGH everywhere
    # on the wizard happy path and the 2D trust signal silently
    # degrades to a flat 1D HIGH that operators can't reason about.
    try:
        entity = Entity(
            name=name,
            description=description,
            binding=binding,
            identity=identity,
            origin="suggested",
            inference_method="llm_suggested",
        )
    except ValueError as exc:
        raise SuggestionParseError(f"candidate #{index}: {exc}") from exc

    # By the time we reach here, `_parse_confidence` and `_parse_pii_hints`
    # have already validated the envelope fields — EntityCandidate's own
    # __post_init__ would only re-check what those helpers enforce, so
    # construction cannot raise. Keep this direct (no try/except) so the
    # error path stays honest: a future contributor who weakens the
    # validators above will see the change surface in a test, not get
    # masked by an unreachable except.
    return EntityCandidate(
        entity=entity,
        confidence=confidence,
        rationale=rationale,
        pii_hints=pii_hints,
    )


def _require_str(data: dict[str, Any], field: str, *, index: int) -> str:
    value = data[field]
    if not isinstance(value, str):
        raise SuggestionParseError(
            f"candidate #{index}.{field} must be a string (got {type(value).__name__}: {value!r})"
        )
    if not value:
        raise SuggestionParseError(f"candidate #{index}.{field} must be non-empty")
    return value


def _optional_str(data: dict[str, Any], field: str, *, default: str, index: int) -> str:
    if field not in data:
        return default
    value = data[field]
    if not isinstance(value, str):
        raise SuggestionParseError(
            f"candidate #{index}.{field} must be a string (got {type(value).__name__}: {value!r})"
        )
    return value


def _parse_confidence(raw: Any, *, index: int) -> Confidence:
    if not isinstance(raw, str):
        raise SuggestionParseError(
            f"candidate #{index}.confidence must be a string (got {type(raw).__name__}: {raw!r})"
        )
    if raw not in _VALID_CONFIDENCE:
        raise SuggestionParseError(
            f"candidate #{index}.confidence must be one of "
            f"{sorted(_VALID_CONFIDENCE)} (got {raw!r})"
        )
    return raw  # type: ignore[return-value]


def _parse_binding(raw: Any, *, index: int) -> SingleTableBinding:
    if not isinstance(raw, dict):
        raise SuggestionParseError(
            f"candidate #{index}.binding must be a mapping (got {type(raw).__name__})"
        )
    if not raw:
        raise SuggestionParseError(
            f"candidate #{index}.binding must specify exactly one shape; got empty mapping"
        )

    keys = set(raw.keys())
    unknown = keys - _KNOWN_BINDING_KEYS
    if unknown:
        raise SuggestionParseError(
            f"candidate #{index}.binding has unknown shape(s): {sorted(unknown)}; "
            f"allowed: {sorted(_KNOWN_BINDING_KEYS)}"
        )
    if "multi_table" in keys:
        # multi_table is a v2 concern. Same rejection as yaml_grammar.
        raise SuggestionParseError(
            f"candidate #{index}.binding: multi_table is deferred to v2; "
            "use single_table for v1 entities"
        )
    if "single_table" not in keys:  # pragma: no cover — defensive
        # Today this branch is unreachable because `_KNOWN_BINDING_KEYS
        # = {"single_table", "multi_table"}`, the unknown-key check
        # rejected anything outside that set, and the multi_table
        # branch above caught the only other option. But adding a 3rd
        # binding key to `_KNOWN_BINDING_KEYS` without a parallel
        # rejection branch would silently fall through to a KeyError
        # below. This guard turns that future maintenance mistake into
        # a clear parse error rather than a stack trace.
        raise SuggestionParseError(
            f"candidate #{index}.binding must specify single_table; got keys {sorted(keys)}"
        )

    qualified_table = raw["single_table"]
    if not isinstance(qualified_table, str):
        raise SuggestionParseError(
            f"candidate #{index}.binding.single_table must be a string "
            f"(got {type(qualified_table).__name__}: {qualified_table!r})"
        )
    try:
        return SingleTableBinding(qualified_table=qualified_table)
    except ValueError as exc:
        raise SuggestionParseError(f"candidate #{index}.binding: {exc}") from exc


def _parse_pii_hints(raw: Any, *, index: int) -> dict[str, Sensitivity]:
    if not isinstance(raw, dict):
        raise SuggestionParseError(
            f"candidate #{index}.pii_hints must be a mapping (got {type(raw).__name__})"
        )
    hints: dict[str, Sensitivity] = {}
    for column, sensitivity in raw.items():
        if not isinstance(column, str) or not column:
            raise SuggestionParseError(
                f"candidate #{index}.pii_hints keys must be non-empty strings (got {column!r})"
            )
        if not isinstance(sensitivity, str) or sensitivity not in _VALID_SENSITIVITIES:
            raise SuggestionParseError(
                f"candidate #{index}.pii_hints[{column!r}] must be one of "
                f"{sorted(_VALID_SENSITIVITIES)} (got {sensitivity!r})"
            )
        hints[column] = sensitivity  # type: ignore[assignment]
    return hints


# ----- pipeline orchestration ------------------------------------------------


class EntitySuggestionPipeline:
    """Orchestrates LLM-driven entity suggestion against an indexed schema.

    The pipeline holds a reference to an `LLMClient` (the existing
    Protocol from `enrichment/llm.py`) and threads `complete` calls
    through it. All cost reporting routes through `llm.cost_usd(usage)`
    so the pipeline never embeds pricing knowledge — a future provider
    adapter prices its own calls.

    `propose_from_tables` is the public entry point: schema + top_k in,
    `SuggestionResult` out. A cost-ceiling guard layers on top of this
    in the cost-ceiling step — `_invoke_llm` is the seam the guard
    will wrap.
    """

    def __init__(self, llm: LLMClient) -> None:
        # Private attribute: the LLM client is an implementation
        # detail, not part of the pipeline's public surface. A future
        # refactor that wraps `llm` (e.g., add retry, observability)
        # must not be silently bypassable by callers holding a
        # reference to `.llm`. Tests access `_llm` directly via the
        # name-mangled-not-applied single-underscore convention.
        self._llm = llm

    def propose_from_tables(
        self,
        tables: Sequence[Table],
        *,
        top_k: int = 10,
    ) -> SuggestionResult:
        """Generate entity candidates from a list of indexed tables.

        Single LLM call per run — no retries, no chunking at v1. The
        caller (CLI) is responsible for sizing `tables` appropriately;
        a 500-table schema in one prompt is the user's problem until
        we ship chunking.

        Args:
          tables: Non-empty list of physical tables to analyze.
          top_k: Maximum number of candidates to keep. The cap is also
              communicated to the LLM via the user prompt, but
              enforced defensively after parse in case the LLM
              over-produces.

        Raises:
          ValueError: if `tables` is empty or `top_k <= 0`.
          SuggestionParseError: if the LLM's response is unparseable.
        """
        if not tables:
            raise ValueError("propose_from_tables requires at least one table")
        if top_k <= 0:
            raise ValueError(f"top_k must be a positive integer (got {top_k!r})")

        user_prompt = _build_user_prompt(tables, top_k=top_k)
        response = self._invoke_llm(system=_SYSTEM_PROMPT, user=user_prompt)
        cost = self._cost_for(response)
        candidates = parse_suggestions(response.text)
        truncated = candidates[:top_k]
        return SuggestionResult(
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
        that layers on next.
        """
        return self._llm.cost_usd(response.usage)


# ----- cost ceiling guard ----------------------------------------------------

# Pre-flight token estimate: characters per token. The Anthropic
# tokenizer averages ~3.5 to 4 chars/token across English prose; 4 is the
# standard rough estimate used elsewhere in this codebase (FakeLLMClient,
# enrichment pipeline). Picking a low number here would over-estimate
# input cost and reject valid runs; picking a high number would
# under-estimate and let runs slip past the ceiling. 4 splits the
# difference and matches the rest of the codebase's behavior.
_CHARS_PER_TOKEN_ESTIMATE: int = 4


class CostCeilingExceededError(RuntimeError):
    """Raised by `CostCeilingGuard` when a call would breach the ceiling.

    Carries three diagnostic fields so the CLI can render a guided
    error: "you spent $X.XX of $Y.YY; the next call would add $Z.ZZ.
    Re-run with --max-cost-usd N to raise the cap."

    Subclass of `RuntimeError` (not `ValueError`) because it's a
    runtime resource-exhaustion condition, not a programming error.
    The user fixes it by re-running with a different ceiling, not by
    correcting their input.
    """

    def __init__(
        self,
        *,
        cumulative_cost_usd: float,
        next_call_estimate_usd: float,
        max_cost_usd: float,
    ) -> None:
        super().__init__(
            f"cost ceiling exceeded: spent ${cumulative_cost_usd:.4f} of "
            f"${max_cost_usd:.4f}; next call would add ~${next_call_estimate_usd:.4f}. "
            "Re-run with a higher --max-cost-usd or set the "
            "SCHEMABRAIN_MAX_LLM_COST_USD environment variable."
        )
        self.cumulative_cost_usd = cumulative_cost_usd
        self.next_call_estimate_usd = next_call_estimate_usd
        self.max_cost_usd = max_cost_usd


class CostCeilingGuard:
    """Wrap an `LLMClient` with a hard cost ceiling.

    Two enforcement points:
      1. Pre-flight: estimate the next call's input cost from prompt
         length. If `cumulative + estimate > ceiling`, refuse without
         calling the inner client.
      2. Post-call: add the real cost (from `inner.cost_usd(usage)`)
         to the running total. Subsequent calls compare against the
         updated cumulative.

    Pre-flight estimates INPUT only (not output) because output is
    LLM-controlled and unpredictable. For single-call workflows (the
    suggest pipeline at this release), this means one call can exceed
    the ceiling by the output cost — but a runaway loop is bounded
    immediately, which is the actual protection we want. The synthetic
    usage built by `_estimate_input_cost` passes `output_tokens=0`
    through the inner adapter's `cost_usd`; every existing adapter
    handles a zero-output cost path correctly, and a hypothetical
    adapter that didn't would surface immediately rather than miscount.

    The guard implements `LLMClient` itself (Decorator pattern), so
    callers that take any `LLMClient` accept it without branching.

    **Failed-inner-call semantics.** If `self._inner.complete(...)`
    raises (network error, rate-limit, provider exception), control
    unwinds before `_cumulative_cost_usd` is updated. The guard does
    NOT accrue cost for failed calls — `cumulative_cost_usd` reports
    only successful spend. A caller that wraps the pipeline in retry
    logic should be aware: the provider may have billed input tokens
    before the failure (some providers bill on receipt), and the
    guard cannot account for that. The ceiling can therefore be
    exceeded by up to one call's worth of un-accrued failed input
    tokens. For single-call workflows this is bounded; for
    retry-wrapped callers, treat the ceiling as best-effort.
    """

    def __init__(self, *, inner: LLMClient, max_cost_usd: float) -> None:
        if max_cost_usd <= 0:
            raise ValueError(f"max_cost_usd must be a positive value (got {max_cost_usd!r})")
        self._inner = inner
        self._max_cost_usd = max_cost_usd
        self._cumulative_cost_usd = 0.0

    @property
    def max_cost_usd(self) -> float:
        return self._max_cost_usd

    @property
    def cumulative_cost_usd(self) -> float:
        return self._cumulative_cost_usd

    def complete(self, *, system: str, user: str) -> LLMResponse:
        # Pre-flight: refuse before burning a single API call if the
        # rough estimate would breach the ceiling.
        estimate = self._estimate_input_cost(system=system, user=user)
        projected = self._cumulative_cost_usd + estimate
        if projected > self._max_cost_usd:
            raise CostCeilingExceededError(
                cumulative_cost_usd=self._cumulative_cost_usd,
                next_call_estimate_usd=estimate,
                max_cost_usd=self._max_cost_usd,
            )

        response = self._inner.complete(system=system, user=user)
        # Post-call: accrue real cost. The estimate was input-only and
        # rough; the real cost includes output and any cache effects.
        self._cumulative_cost_usd += self._inner.cost_usd(response.usage)
        return response

    def cost_usd(self, usage: LLMUsage) -> float:
        # Transparent pass-through. Re-pricing here would create two
        # sources of truth — the guard would lie about cost while the
        # adapter reports the real number.
        return self._inner.cost_usd(usage)

    def _estimate_input_cost(self, *, system: str, user: str) -> float:
        """Rough pre-flight estimate using the inner client's pricing.

        Input-only — output is LLM-controlled and not estimable from
        the prompt alone. Builds a synthetic `LLMUsage` and routes it
        through the inner client's `cost_usd` so the estimate matches
        whatever pricing family the inner adapter uses (Anthropic,
        Bedrock, etc.) without the guard hard-coding any rates.
        """
        approx_input_tokens = max(1, (len(system) + len(user)) // _CHARS_PER_TOKEN_ESTIMATE)
        synthetic = LLMUsage(
            input_tokens=approx_input_tokens,
            cached_input_tokens=0,
            output_tokens=0,
        )
        return self._inner.cost_usd(synthetic)
