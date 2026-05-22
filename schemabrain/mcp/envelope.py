"""MCP response envelope (Charter v1.2).

Every MCP tool wraps its return value in a `ToolResponse[T]`:

    {
      "status": "success" | "empty" | "partial" | "degraded" | "error" | "refused",
      "data": <T | None>,
      "error": <ToolError | None>,
      "confidence": "HIGH" | "MEDIUM" | "LOW" | None,
      "provenance": <Provenance | None>,
      "follow_up_hints": [<tool_name>, ...] | None,
      "charter_version": "1.2"
    }

The envelope is the public contract for Schema Brain's MCP surface. See
`docs/agent-ux-charter.md` for the full design rationale.

This module is intentionally free of FastMCP / transport concerns —
envelope construction must work in any context (tests, alternative
transports, in-process clients). The FastMCP wiring lives in
`server.py`.

Versioning: this module pins `CHARTER_VERSION = "1.2"` as an additive
minor bump over the v1.1 contract. Changes in 1.2 (all backward-
compatible with 1.0 / 1.1):

  - New `Provenance.inference_method` Literal: closed set
    `manually_authored | llm_suggested | fk_constraint | dbt_import |
    observed_in_query_log` — names HOW the fact was derived.
  - New `Provenance.validation_state` Literal: closed set
    `draft | applied | confirmed` — names HOW VALIDATED the fact is.
  - `confidence` is now a derivation of `(inference_method,
    validation_state)` (helper `derive_confidence`). The pre-1.2
    behaviour where every producer hardcoded `confidence="HIGH"`
    regardless of derivation conflated FK-derived metrics with
    LLM-guessed ones — the agent could not tell them apart.
    Charter v1.2 fixes that without removing the `confidence`
    field: old clients still read a meaningful 1D label; new
    clients can read the 2D signal directly.

Changes in 1.1 (preserved):
  - New ErrorKinds: `pii_blocked`, `policy_blocked`, `allowlist_violation`.
  - Reserved `refused` status — present in the Status literal so v2's
    refuse-before-execute primitives ship the type contract on day
    one. No current tool emits this status (charter_lint asserts).
  - New optional `Recovery` fields: `suggested_rewrite` and
    `widening_hint`, populated by v2's refuse-with-rewrite path.
"""

from __future__ import annotations

from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from schemabrain.pii.categories import PIICategory

# Pinned charter version. Bumped per the Versioning section of the
# charter; minor bumps are additive (new optional fields, new error
# kinds), major bumps remove or rename fields. v1.1 -> v1.2 adds
# `Provenance.inference_method` + `Provenance.validation_state` and
# changes `confidence` semantics from "always HIGH" to "derived from
# the 2D trust signal" — additive at the schema layer (both new
# fields are optional), but the value of `confidence` now varies in
# the wild for the first time.
CHARTER_VERSION = "1.2"

Status = Literal[
    "success",
    "empty",
    "partial",
    "degraded",
    "error",
    # Reserved in v1.1: no v0/v0.5/v1 tool emits `refused`. v2's
    # `execute` / `validate_query` are the first producers, refusing
    # queries that touch PII or violate allowlist scope before
    # execution. Surfaces `error` populated with a v1.1 ErrorKind
    # (`pii_blocked` / `policy_blocked` / `allowlist_violation`) and
    # optionally `Recovery.suggested_rewrite` or `widening_hint`.
    #
    # TODO(v2): when v2 ships, consider splitting into
    # `ProducerStatus` (without `refused`, for v0/v0.5/v1 tools) and
    # the full `Status` (for envelope deserialisation). The split
    # lets the type checker enforce the lint rule rather than relying
    # on the runtime check in `scripts/charter_lint.py`.
    "refused",
]
Confidence = Literal["HIGH", "MEDIUM", "LOW"]
ProvenanceSource = Literal["schema", "llm", "inferred"]

# v1.2 — 2D trust signal. `InferenceMethod` names HOW a fact was
# derived (FK constraint, LLM guess, hand-typed YAML, dbt import,
# query-log mining). `ValidationState` names HOW VALIDATED that fact
# is (draft = proposed-but-unapplied, applied = in store, confirmed
# = operator explicitly signed off). The two axes are orthogonal:
# an LLM-suggested join that an operator manually confirmed is
# (llm_suggested, confirmed) → HIGH. An FK-derived join that hasn't
# been applied yet is (fk_constraint, draft) → MEDIUM. Both Literals
# stay in lock-step with `schemabrain.core.entity.{InferenceMethod,
# ValidationState}` and the SQL CHECK constraints on the entities /
# metrics / canonical_joins tables.
InferenceMethod = Literal[
    "manually_authored",
    "llm_suggested",
    "fk_constraint",
    "dbt_import",
    "observed_in_query_log",
]
ValidationState = Literal["draft", "applied", "confirmed"]


def derive_confidence(
    inference_method: InferenceMethod, validation_state: ValidationState
) -> Confidence:
    """Compute the 1D `Confidence` label from the 2D trust signal.

    The matrix is intentionally conservative: an LLM-suggested
    entity / metric / join that the operator merely applied (without
    confirming) is not equivalent to a hand-authored or FK-derived
    one. Charter v1.2 makes that asymmetry visible by treating
    `llm_suggested` + `applied` as MEDIUM (the LLM proposed it, the
    operator clicked apply, but no human verified the substantive
    correctness beyond the apply gesture).

    Matrix:
      confirmed                                              → HIGH
      manually_authored, applied                             → HIGH
      fk_constraint, applied                                 → HIGH
      dbt_import, applied                                    → HIGH
      llm_suggested, applied                                 → MEDIUM
      observed_in_query_log, applied                         → MEDIUM
      manually_authored / fk_constraint / dbt_import, draft  → MEDIUM
      llm_suggested / observed_in_query_log, draft           → LOW
    """
    if validation_state == "confirmed":
        return "HIGH"
    if validation_state == "applied":
        if inference_method in {"manually_authored", "fk_constraint", "dbt_import"}:
            return "HIGH"
        return "MEDIUM"
    # validation_state == "draft"
    if inference_method in {"manually_authored", "fk_constraint", "dbt_import"}:
        return "MEDIUM"
    return "LOW"


def derive_provenance_source(inference_method: InferenceMethod) -> ProvenanceSource:
    """Map the v1.2 `InferenceMethod` to the v1.0 `ProvenanceSource`.

    Lets producers populate the new 2D signal AND keep the older 1D
    `source` field deterministic — old clients that only read
    `provenance.source` see a sensible value derived from the new
    signal rather than a hardcoded `"schema"`.

      manually_authored / fk_constraint / dbt_import → schema
      llm_suggested                                  → llm
      observed_in_query_log                          → inferred
    """
    if inference_method == "llm_suggested":
        return "llm"
    if inference_method == "observed_in_query_log":
        return "inferred"
    return "schema"
# v1.0 shipped 7 kinds. v1.1 adds 3 for the refuse-before-execute
# taxonomy. Additions are minor-version bumps.
ErrorKind = Literal[
    # v1.0 kinds:
    "unknown_name",
    "malformed_name",
    "missing_credential",
    "index_not_ready",
    "schema_drift",
    "cost_cap_exceeded",
    "internal_error",
    # v1.1 additions — refuse-before-execute:
    "pii_blocked",
    "policy_blocked",
    "allowlist_violation",
    # canonical-join resolution surface:
    "no_canonical_join",
    "ambiguous_join",
    "unknown_join_name",
    "join_name_mismatch",
    # metric surface:
    "unknown_metric",
    "unreachable_entity",
    "ambiguous_path",
    "unknown_via_join",
    "unknown_order_by_column",
    "unknown_group_by_column",
    "unknown_filter_column",
    "unknown_measure_column",
    "invalid_time_grain",
    "grain_mismatch",
    # MCP dispatch surface — used INTERNALLY by the strict-args
    # rejection path to record unknown-kwargs calls in the audit
    # table and event bus. The client sees a `FastMCPToolError`
    # (isError: true) at the protocol layer; this kind never appears
    # in a `ToolResponse` returned to the agent, only in audit rows
    # and bus events for ops visibility.
    "invalid_argument",
]

# Closed-grammar reasons for `status="degraded"` envelopes. Parallel
# to `ErrorKind` but for the partial-success path: the tool ran and
# returned `data`, but with a structured caveat the agent should
# surface to the user (or branch on programmatically). Today's set
# covers the two reasons surfaced by the 2026-05-21 smoke; new kinds
# are added by appending here AND wiring the producer at the
# degradation point. Closed grammar so agents can switch on the value
# without parsing free-form text.
DegradationReason = Literal[
    # `MetricPlan.fan_out_join_names` is non-empty: at least one
    # `one_to_many` / `many_to_many` (or unspecified-cardinality)
    # canonical join lies on the chain, so row counts in `data.rows`
    # may be inflated by the cross-product. Agent should treat
    # aggregates with caution and consider passing `count_distinct`
    # over an identity column instead of `count` / `sum`.
    "fan_out_join",
    # `limit` is set + `group_by` is non-empty + `order_by` is empty:
    # the LIMIT N slice is non-deterministic across runs. Agent should
    # pass `order_by=` to specify what "top N" means before relying on
    # the row order.
    "missing_order_by_with_limit",
]

# The subset of ErrorKinds that semantically support v1.1 Recovery
# additions (`suggested_rewrite` / `widening_hint`). A `ToolError` with
# `kind` outside this set may not carry either v1.1 Recovery field;
# setting one on (e.g.) an `unknown_name` error is a programming
# mistake the constructor rejects.
REFUSAL_KINDS: frozenset[str] = frozenset({"pii_blocked", "policy_blocked", "allowlist_violation"})

T = TypeVar("T")


class Provenance(BaseModel):
    """Per-response provenance annotation.

    `source="schema"` — facts came straight from the source database's
    information schema. No model field. `source="llm"` — generated by
    an LLM; `model` names which one. `source="inferred"` — derived from
    observed signals (query log, sampling); `observed_in` carries
    {count, first_seen, last_seen} or similar diagnostic metadata.

    v1.2 additions:

    - `inference_method` (`InferenceMethod | None`) — finer-grained
      derivation label. Replaces the 1D `source` for callers that
      need to distinguish FK-derived joins from LLM-guessed metrics.
      None for v1.1 envelopes; populated for v1.2 producers.
    - `validation_state` (`ValidationState | None`) — orthogonal
      validation axis. None for v1.1 envelopes; populated for v1.2
      producers.

    Producers should compute `confidence` via `derive_confidence(
    inference_method, validation_state)` and set `source` via
    `derive_provenance_source(inference_method)` so the 1D and 2D
    signals stay consistent.
    """

    model_config = ConfigDict(frozen=True)

    source: ProvenanceSource
    model: str | None = None
    observed_in: dict[str, Any] | None = None
    # v1.2 additions — optional for back-compat with v1.0 / v1.1
    # producers that haven't been migrated yet.
    inference_method: InferenceMethod | None = None
    validation_state: ValidationState | None = None


class SuggestedRewrite(BaseModel):
    """A suggested rewrite the agent can adopt after a refusal (v1.1).

    Used by the future refuse-before-execute path: when an agent's
    query is partially safe and partially refused, the executor can
    construct a safe version that strips or substitutes the unsafe
    fragment, and ship that rewrite through this shape so the agent
    can retry without round-tripping back to a human.

    `rewritten` is typically a SQL fragment but the shape doesn't pin
    that — it could be a tool-call args dict serialised as a string,
    an alternative SQL, or any other "here's what you can call instead"
    payload.

    TODO(v2): when the first `execute` producer lands and the rewrite
    vocabulary stabilises, consider promoting to a discriminated union
    `rewritten: str | dict[str, Any]` with a `kind: Literal["sql","args"]`
    discriminator so consumers can branch without parsing the string.
    """

    model_config = ConfigDict(frozen=True)

    # `min_length=1` rejects empty strings at construction; a v2
    # producer that accidentally hands the agent a blank rewrite
    # would otherwise look like a structurally valid recovery hint.
    rewritten: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class WideningHint(BaseModel):
    """A scope-widening hint after an `allowlist_violation` refusal (v1.1).

    Used when the executor's refusal could be unblocked by widening
    the caller's allowlist. The hint names the scope to request
    (e.g., `pii:contact` or `table:public.users`) and whether
    granting it would let the original tool call succeed unchanged.

    `would_unblock=False` means there's more than one barrier —
    granting the named scope is necessary but not sufficient.

    TODO(v2): when v2 allowlist scopes are designed, consider pinning
    `scope_requested` to a regex-validated `Annotated[str, Field(pattern=...)]`
    so malformed scope identifiers fail at construction rather than
    silently downstream.
    """

    model_config = ConfigDict(frozen=True)

    scope_requested: str = Field(min_length=1)
    would_unblock: bool
    reason: str = Field(min_length=1)


class Recovery(BaseModel):
    """Recovery hints — the agent's next move after an error or refusal.

    `suggested_tool` is the tool name to call next; `suggested_args` is
    the args dict to call it with. `fuzzy_matches` is a tuple of plausible
    alternatives — typically used for `unknown_name` errors where the
    caller's input was close to an existing entity.

    v1.1 additions:

    - `suggested_rewrite` (`SuggestedRewrite | None`) — populated by
      v2's refuse-with-rewrite path; offers a safe version of the
      original query the agent can retry.
    - `widening_hint` (`WideningHint | None`) — populated by v2's
      `allowlist_violation` path; names the scope that would unblock
      the original call.

    Both new fields are optional; v1.0 call sites that don't set them
    continue to construct unchanged.

    Stored as a tuple rather than a list for `fuzzy_matches` so the
    frozen invariant survives caller-side in-place mutation attempts;
    JSON serialization is identical.
    """

    model_config = ConfigDict(frozen=True)

    suggested_tool: str | None = None
    suggested_args: dict[str, Any] | None = None
    fuzzy_matches: tuple[str, ...] = ()
    suggested_rewrite: SuggestedRewrite | None = None
    widening_hint: WideningHint | None = None


class ToolError(BaseModel):
    """Structured error returned inside a `ToolResponse` with status='error'
    or status='refused' (v1.1).

    `kind` is one of the fourteen registered kinds (charter v1.1: 7
    from v1.0 + 3 refusal kinds + 4 canonical-join kinds added in a
    later release). `message` is a single human-readable sentence.
    `recovery`
    carries the agent's next-move hint — never empty in practice, though
    `Recovery()` with no fields is structurally valid for kinds where no
    recovery exists.

    Kind / Recovery coupling: the v1.1 fields `Recovery.suggested_rewrite`
    and `Recovery.widening_hint` are semantically tied to the three
    refusal kinds (`pii_blocked` / `policy_blocked` / `allowlist_violation`
    — see `REFUSAL_KINDS`). Setting either on a non-refusal kind
    (e.g. `unknown_name`) is a programming mistake: the rewrite/widening
    contract requires a refusal context. Enforced at construction.
    """

    model_config = ConfigDict(frozen=True)

    kind: ErrorKind
    message: str
    recovery: Recovery
    # Categories that triggered a refusal. Sorted tuple of
    # `PIICategory` Literal values so the wire form is deterministic
    # AND the type checker rejects arbitrary strings at construction.
    # Empty tuple when the refusal carries no PII context. Today only
    # `pii_blocked` populates this; the other two refusal kinds
    # (`policy_blocked`, `allowlist_violation`) and every non-refusal
    # kind keep `()`.
    pii_categories: tuple[PIICategory, ...] = ()

    @model_validator(mode="after")
    def _validate_recovery_fields_match_kind(self) -> ToolError:
        if self.kind not in REFUSAL_KINDS:
            if self.recovery.suggested_rewrite is not None:
                raise ValueError(
                    f"Recovery.suggested_rewrite is only valid for refusal "
                    f"kinds ({sorted(REFUSAL_KINDS)}); got kind={self.kind!r}"
                )
            if self.recovery.widening_hint is not None:
                raise ValueError(
                    f"Recovery.widening_hint is only valid for refusal "
                    f"kinds ({sorted(REFUSAL_KINDS)}); got kind={self.kind!r}"
                )
            if self.pii_categories:
                raise ValueError(
                    f"ToolError.pii_categories is only valid for refusal "
                    f"kinds ({sorted(REFUSAL_KINDS)}); got kind={self.kind!r}"
                )
        return self


class ToolResponse(BaseModel, Generic[T]):
    """The envelope every Schema Brain MCP tool returns.

    Invariants enforced by `_validate_status_data_invariant`:

    - `status="error"` requires `data is None` and a populated `error`.
    - `status="refused"` (v1.1) behaves identically to `"error"` for
      the structural invariant: `data is None` and `error` populated.
      Semantically distinct (tool ran cleanly and chose to refuse vs
      tool failed) but the shape contract is the same.
    - Any other status requires `error is None`. `data` must be
      populated for `success`, may be an empty container for `empty`.
    """

    model_config = ConfigDict(frozen=True)

    status: Status
    data: T | None = None
    error: ToolError | None = None
    confidence: Confidence | None = None
    provenance: Provenance | None = None
    # Tuple (not list) so the frozen invariant survives caller-side
    # in-place mutation attempts. Pydantic coerces list input
    # transparently — call sites that pass `follow_up_hints=[...]`
    # don't need to change.
    follow_up_hints: tuple[str, ...] | None = None
    # Structured reason for `status="degraded"`. Closed Literal so the
    # agent can switch on the value programmatically. `None` outside
    # the `degraded` status; validated by the invariant model
    # validator. Today's reasons: `fan_out_join`,
    # `missing_order_by_with_limit`. Adding new reasons requires
    # extending the `DegradationReason` Literal above AND wiring the
    # producer at the degradation point.
    degradation_reason: DegradationReason | None = None
    charter_version: str = CHARTER_VERSION

    @model_validator(mode="after")
    def _validate_status_data_invariant(self) -> ToolResponse[T]:
        if self.status in ("error", "refused"):
            if self.error is None:
                raise ValueError(f"status={self.status!r} requires an `error` object")
            if self.data is not None:
                raise ValueError(f"status={self.status!r} forbids non-None `data`")
            return self
        # Non-error / non-refused path
        if self.error is not None:
            raise ValueError(f"status={self.status!r} forbids non-None `error`")
        if self.status == "success" and self.data is None:
            raise ValueError("status='success' requires `data` to be populated")
        # `degradation_reason` is only meaningful when status='degraded'.
        # Permitting it on success / empty would let producers silently
        # signal an issue without using the degraded status — keep the
        # contract one-way (degraded ↔ reason; everything else: None).
        if self.status != "degraded" and self.degradation_reason is not None:
            raise ValueError(
                f"status={self.status!r} forbids `degradation_reason`; "
                f"the field is only meaningful for status='degraded'"
            )
        return self


__all__ = [
    "CHARTER_VERSION",
    "REFUSAL_KINDS",
    "Confidence",
    "DegradationReason",
    "ErrorKind",
    "InferenceMethod",
    "Provenance",
    "ProvenanceSource",
    "Recovery",
    "Status",
    "SuggestedRewrite",
    "ToolError",
    "ToolResponse",
    "ValidationState",
    "WideningHint",
    "derive_confidence",
    "derive_provenance_source",
]
