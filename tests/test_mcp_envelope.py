"""MCP response envelope (Charter v1.1) shape tests.

The envelope is the public contract every MCP tool returns. The tool
*logic* tests in `test_mcp_tools.py` cover what each tool computes;
this file covers the envelope itself — status enum, error kinds,
recovery shape, and the data/error invariants.

Wiring-level tests (where envelopes are produced from `*_impl` results
and propagated through FastMCP) live in `test_mcp_server.py`.

Tests for v1.1-specific additions (new ErrorKinds, reserved `refused`
status, `SuggestedRewrite` / `WideningHint` on `Recovery`) live in the
test classes at the bottom of this file, grouped under the comment
"Charter v1.1 additions".
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from pydantic import BaseModel, ValidationError

from schemabrain.mcp.envelope import (
    CHARTER_VERSION,
    Provenance,
    Recovery,
    ToolError,
    ToolResponse,
)
from schemabrain.mcp.shapes import TableHit


class _Payload(BaseModel):
    """Minimal Pydantic model used as a stand-in `data` payload."""

    text: str


class TestRecovery:
    def test_default_recovery_has_no_suggested_tool(self) -> None:
        r = Recovery()
        assert r.suggested_tool is None
        assert r.suggested_args is None
        # fuzzy_matches is a tuple so the frozen invariant survives
        # caller-side in-place mutation attempts.
        assert r.fuzzy_matches == ()

    def test_recovery_round_trips(self) -> None:
        r = Recovery(
            suggested_tool="find_relevant_tables",
            suggested_args={"query": "user"},
            fuzzy_matches=("users", "user_profiles"),
        )
        assert r.suggested_tool == "find_relevant_tables"
        assert r.suggested_args == {"query": "user"}
        assert r.fuzzy_matches == ("users", "user_profiles")

    def test_recovery_coerces_list_input_to_tuple(self) -> None:
        # Pydantic accepts list input on a tuple field; coerces to tuple
        # at construction. The frozen invariant is preserved either way.
        r = Recovery(fuzzy_matches=["users", "user_profiles"])
        assert r.fuzzy_matches == ("users", "user_profiles")
        assert isinstance(r.fuzzy_matches, tuple)

    def test_recovery_is_frozen(self) -> None:
        r = Recovery()
        with pytest.raises(ValidationError):
            r.suggested_tool = "something_else"  # type: ignore[misc]


class TestProvenance:
    def test_schema_source_minimal(self) -> None:
        p = Provenance(source="schema")
        assert p.source == "schema"
        assert p.model is None
        assert p.observed_in is None

    def test_llm_source_with_model(self) -> None:
        p = Provenance(source="llm", model="claude-haiku-4-5")
        assert p.source == "llm"
        assert p.model == "claude-haiku-4-5"

    def test_inferred_source_with_observed_in(self) -> None:
        p = Provenance(
            source="inferred",
            observed_in={"count": 12, "first_seen": "2026-01-01"},
        )
        assert p.source == "inferred"
        assert p.observed_in == {"count": 12, "first_seen": "2026-01-01"}

    def test_unknown_source_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Provenance(source="rumor")  # type: ignore[arg-type]

    def test_provenance_is_frozen(self) -> None:
        p = Provenance(source="schema")
        with pytest.raises(ValidationError):
            p.source = "llm"  # type: ignore[misc]


class TestToolError:
    def test_minimal_error_with_recovery(self) -> None:
        err = ToolError(
            kind="unknown_name",
            message="Table 'user' not found in the indexed schema.",
            recovery=Recovery(suggested_tool="find_relevant_tables"),
        )
        assert err.kind == "unknown_name"
        assert err.message.startswith("Table 'user'")
        assert err.recovery.suggested_tool == "find_relevant_tables"

    def test_all_v10_error_kinds_accepted(self) -> None:
        """The charter v1.0 ships with these seven kinds; v1.1 adds
        three more (covered separately below)."""
        kinds = [
            "unknown_name",
            "malformed_name",
            "missing_credential",
            "index_not_ready",
            "schema_drift",
            "cost_cap_exceeded",
            "internal_error",
        ]
        for k in kinds:
            err = ToolError(kind=k, message="x", recovery=Recovery())  # type: ignore[arg-type]
            assert err.kind == k

    def test_unknown_kind_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ToolError(kind="bogus", message="x", recovery=Recovery())  # type: ignore[arg-type]

    def test_tool_error_is_frozen(self) -> None:
        err = ToolError(kind="unknown_name", message="x", recovery=Recovery())
        with pytest.raises(ValidationError):
            err.message = "y"  # type: ignore[misc]


class TestToolResponseSuccess:
    def test_success_requires_data(self) -> None:
        with pytest.raises(ValidationError) as exc:
            ToolResponse[_Payload](status="success")
        assert "data" in str(exc.value).lower()

    def test_success_with_data_is_valid(self) -> None:
        resp = ToolResponse[_Payload](
            status="success",
            data=_Payload(text="hello"),
        )
        assert resp.status == "success"
        assert resp.data is not None
        assert resp.data.text == "hello"
        assert resp.error is None
        assert resp.charter_version == CHARTER_VERSION

    def test_success_forbids_non_none_error(self) -> None:
        with pytest.raises(ValidationError) as exc:
            ToolResponse[_Payload](
                status="success",
                data=_Payload(text="hi"),
                error=ToolError(kind="internal_error", message="x", recovery=Recovery()),
            )
        assert "error" in str(exc.value).lower()


class TestToolResponseEmpty:
    def test_empty_with_empty_list_data(self) -> None:
        # `find_relevant_tables` returns list[TableHit]; empty results
        # land here as status="empty" + data=[].
        resp = ToolResponse[list[TableHit]](
            status="empty",
            data=[],
            follow_up_hints=("describe_table",),
        )
        assert resp.status == "empty"
        assert resp.data == []
        assert resp.follow_up_hints == ("describe_table",)
        assert resp.error is None

    def test_empty_forbids_error_object(self) -> None:
        with pytest.raises(ValidationError):
            ToolResponse[_Payload](
                status="empty",
                data=_Payload(text="x"),
                error=ToolError(kind="internal_error", message="x", recovery=Recovery()),
            )


class TestToolResponseError:
    def test_error_status_requires_error_object(self) -> None:
        with pytest.raises(ValidationError) as exc:
            ToolResponse[_Payload](status="error")
        assert "error" in str(exc.value).lower()

    def test_error_status_forbids_non_none_data(self) -> None:
        with pytest.raises(ValidationError) as exc:
            ToolResponse[_Payload](
                status="error",
                data=_Payload(text="hi"),
                error=ToolError(kind="unknown_name", message="x", recovery=Recovery()),
            )
        assert "data" in str(exc.value).lower()

    def test_error_envelope_round_trips(self) -> None:
        resp = ToolResponse[_Payload](
            status="error",
            error=ToolError(
                kind="unknown_name",
                message="Table 'user' not found in the indexed schema.",
                recovery=Recovery(
                    suggested_tool="find_relevant_tables",
                    suggested_args={"query": "user"},
                    fuzzy_matches=("users", "user_profiles"),
                ),
            ),
        )
        assert resp.status == "error"
        assert resp.data is None
        assert resp.error is not None
        assert resp.error.kind == "unknown_name"
        assert resp.error.recovery.fuzzy_matches == ("users", "user_profiles")


class TestToolResponsePartialAndDegraded:
    def test_partial_status_with_data(self) -> None:
        resp = ToolResponse[_Payload](
            status="partial",
            data=_Payload(text="some of it"),
            confidence="MEDIUM",
        )
        assert resp.status == "partial"
        assert resp.confidence == "MEDIUM"

    def test_degraded_status_with_data(self) -> None:
        resp = ToolResponse[_Payload](
            status="degraded",
            data=_Payload(text="via fallback"),
            confidence="LOW",
        )
        assert resp.status == "degraded"
        assert resp.confidence == "LOW"


class TestToolResponseStatusEnum:
    def test_unknown_status_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ToolResponse[_Payload](
                status="maybe",  # type: ignore[arg-type]
                data=_Payload(text="hi"),
            )

    def test_boolean_ok_not_accepted_as_status(self) -> None:
        # Defensive — Charter Principle 1 explicitly bans a boolean
        # ok/error split. True/False should fail the Literal validator.
        with pytest.raises(ValidationError):
            ToolResponse[_Payload](status=True, data=_Payload(text="hi"))  # type: ignore[arg-type]


class TestToolResponseConfidence:
    def test_confidence_buckets(self) -> None:
        for bucket in ["HIGH", "MEDIUM", "LOW"]:
            resp = ToolResponse[_Payload](
                status="success",
                data=_Payload(text="x"),
                confidence=bucket,  # type: ignore[arg-type]
            )
            assert resp.confidence == bucket

    def test_confidence_none_is_valid(self) -> None:
        resp = ToolResponse[_Payload](status="success", data=_Payload(text="x"), confidence=None)
        assert resp.confidence is None

    def test_unknown_confidence_bucket_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ToolResponse[_Payload](
                status="success",
                data=_Payload(text="x"),
                confidence="MAYBE",  # type: ignore[arg-type]
            )

    def test_raw_float_confidence_rejected(self) -> None:
        # Charter Principle 4: API surface is buckets only. A leaked
        # internal float would be a charter violation.
        with pytest.raises(ValidationError):
            ToolResponse[_Payload](
                status="success",
                data=_Payload(text="x"),
                confidence=0.84,  # type: ignore[arg-type]
            )


class TestToolResponseFollowUpHints:
    def test_follow_up_hints_default_none(self) -> None:
        resp = ToolResponse[_Payload](status="success", data=_Payload(text="x"))
        assert resp.follow_up_hints is None

    def test_follow_up_hints_tuple_of_tool_names(self) -> None:
        # Tuple (not list) so the frozen invariant survives caller-side
        # in-place mutation attempts.
        resp = ToolResponse[_Payload](
            status="success",
            data=_Payload(text="x"),
            follow_up_hints=("describe_table", "suggest_joins"),
        )
        assert resp.follow_up_hints == ("describe_table", "suggest_joins")
        assert isinstance(resp.follow_up_hints, tuple)

    def test_follow_up_hints_coerces_list_input_to_tuple(self) -> None:
        # Pydantic accepts list input on a tuple field. All existing call
        # sites that pass `follow_up_hints=[...]` don't need to change.
        resp = ToolResponse[_Payload](
            status="success",
            data=_Payload(text="x"),
            follow_up_hints=["describe_table", "suggest_joins"],
        )
        assert resp.follow_up_hints == ("describe_table", "suggest_joins")
        assert isinstance(resp.follow_up_hints, tuple)


class TestToolResponseFrozen:
    def test_response_is_frozen(self) -> None:
        resp = ToolResponse[_Payload](status="success", data=_Payload(text="x"))
        # Pydantic frozen models raise ValidationError on assignment,
        # not FrozenInstanceError. Accept either to stay portable.
        with pytest.raises((ValidationError, FrozenInstanceError)):
            resp.status = "error"  # type: ignore[misc]


class TestToolResponseSerialization:
    def test_round_trip_through_json(self) -> None:
        resp = ToolResponse[_Payload](
            status="success",
            data=_Payload(text="hello"),
            confidence="HIGH",
            provenance=Provenance(source="llm", model="claude-haiku-4-5"),
            follow_up_hints=["describe_table"],
        )
        serialized = resp.model_dump_json()
        rebuilt = ToolResponse[_Payload].model_validate_json(serialized)
        assert rebuilt.status == "success"
        assert rebuilt.data is not None
        assert rebuilt.data.text == "hello"
        assert rebuilt.confidence == "HIGH"
        assert rebuilt.provenance is not None
        assert rebuilt.provenance.source == "llm"
        # JSON deserialization runs the same coercion as construction:
        # `[...]` in the JSON becomes a tuple on the rebuilt model.
        assert rebuilt.follow_up_hints == ("describe_table",)
        assert rebuilt.charter_version == CHARTER_VERSION

    def test_charter_version_pinned_in_payload(self) -> None:
        # MCP clients reading raw JSON should see the version as a
        # top-level key, not buried in metadata.
        resp = ToolResponse[_Payload](status="success", data=_Payload(text="x"))
        dumped = resp.model_dump()
        assert dumped["charter_version"] == CHARTER_VERSION


# --- Charter v1.1 additions (additive minor bump) ----------------------------

# v1.1 is an additive minor bump per the Versioning section of the charter.
# Three additions:
#   1. New ErrorKinds: `pii_blocked`, `policy_blocked`, `allowlist_violation`
#   2. Reserved `refused` status — present in the Status literal but not
#      emitted by any current tool. v2's `execute` / `validate_query` will
#      be the first producers.
#   3. New optional Recovery fields: `suggested_rewrite` (SuggestedRewrite),
#      `widening_hint` (WideningHint).
#
# The wire `charter_version` field bumps from "1.0" to "1.1". MCP clients
# that pin `"1.0"` continue to deserialize cleanly — the additions are
# backward-compatible.


class TestCharterV11WireVersion:
    def test_charter_version_is_eleven(self) -> None:
        assert CHARTER_VERSION == "1.1"


class TestStatusV11Refused:
    """`refused` joins the Status literal but no current tool emits it.
    Reserved so v2 refuse-before-execute primitives ship the type
    contract on day one, not retrofitted later."""

    def test_refused_status_in_literal(self) -> None:
        """A ToolResponse with status='refused' must construct without
        ValidationError on the Status literal itself. The status / data
        invariant is tested separately below.
        """
        from schemabrain.mcp.envelope import ToolError

        resp = ToolResponse[_Payload](
            status="refused",
            data=None,
            error=ToolError(
                kind="pii_blocked",
                message="Query touches `pii:contact` columns not in allowlist.",
                recovery=Recovery(),
            ),
        )
        assert resp.status == "refused"

    def test_refused_requires_error_populated(self) -> None:
        """`refused` behaves like `error` for the structural invariant:
        the agent must always see WHY the refusal happened."""
        with pytest.raises(ValidationError) as exc_info:
            ToolResponse[_Payload](status="refused", data=None, error=None)
        assert "refused" in str(exc_info.value).lower() or "error" in str(exc_info.value).lower()

    def test_refused_forbids_non_none_data(self) -> None:
        """A refused response must not leak partial data through `data`.
        If the executor was able to compute a safe partial, it should
        return `status='partial'` with a populated `data` instead."""
        from schemabrain.mcp.envelope import ToolError

        with pytest.raises(ValidationError) as exc_info:
            ToolResponse[_Payload](
                status="refused",
                data=_Payload(text="partial"),  # NOT allowed under refused
                error=ToolError(
                    kind="pii_blocked",
                    message="Refused.",
                    recovery=Recovery(),
                ),
            )
        assert "data" in str(exc_info.value).lower() or "refused" in str(exc_info.value).lower()


class TestErrorKindV11Additions:
    """Three new ErrorKinds land in v1.1. They cover v2's refuse-
    before-execute taxonomy: PII surface, generic policy, and
    allowlist scope."""

    @pytest.mark.parametrize(
        "kind",
        ["pii_blocked", "policy_blocked", "allowlist_violation"],
    )
    def test_new_kind_accepted(self, kind: str) -> None:
        err = ToolError(kind=kind, message="x", recovery=Recovery())  # type: ignore[arg-type]
        assert err.kind == kind

    def test_all_ten_v11_kinds_round_trip(self) -> None:
        """Sanity: enumerate the full v1.1 kind registry and verify each
        constructs. Pins the registry — a typo or accidental removal of
        an existing kind during the v1.1 minor bump is caught here."""
        all_kinds = [
            # v1.0
            "unknown_name",
            "malformed_name",
            "missing_credential",
            "index_not_ready",
            "schema_drift",
            "cost_cap_exceeded",
            "internal_error",
            # v1.1 additions
            "pii_blocked",
            "policy_blocked",
            "allowlist_violation",
        ]
        for k in all_kinds:
            err = ToolError(kind=k, message="x", recovery=Recovery())  # type: ignore[arg-type]
            assert err.kind == k


class TestSuggestedRewrite:
    """`SuggestedRewrite` rides on `Recovery` and tells the agent how
    to retry after a refusal. Used by v2's sub-query refusal path:
    "this column was PII-blocked, here's the same query without it."
    """

    def test_construct_and_round_trip(self) -> None:
        from schemabrain.mcp.envelope import SuggestedRewrite

        rewrite = SuggestedRewrite(
            rewritten="SELECT id, total FROM orders WHERE created_at > '2025-01-01'",
            reason="`email` column was PII-blocked; dropped from SELECT list.",
        )
        assert rewrite.rewritten.startswith("SELECT id, total")
        assert "PII" in rewrite.reason or "pii" in rewrite.reason

    def test_suggested_rewrite_is_frozen(self) -> None:
        from schemabrain.mcp.envelope import SuggestedRewrite

        rewrite = SuggestedRewrite(rewritten="SELECT 1", reason="trivial")
        with pytest.raises((ValidationError, TypeError, FrozenInstanceError)):
            rewrite.rewritten = "SELECT 2"  # type: ignore[misc]


class TestWideningHint:
    """`WideningHint` rides on `Recovery` after an `allowlist_violation`
    refusal. It tells the agent (or operator) what scope to request to
    unblock the original call.
    """

    def test_construct_with_unblock_true(self) -> None:
        from schemabrain.mcp.envelope import WideningHint

        hint = WideningHint(
            scope_requested="pii:contact",
            would_unblock=True,
            reason="Granting `pii:contact` allowlist would let this SELECT through unchanged.",
        )
        assert hint.scope_requested == "pii:contact"
        assert hint.would_unblock is True

    def test_construct_with_unblock_false(self) -> None:
        from schemabrain.mcp.envelope import WideningHint

        hint = WideningHint(
            scope_requested="pii:contact",
            would_unblock=False,
            reason="Query also touches `pii:government_id` which requires a separate grant.",
        )
        assert hint.would_unblock is False

    def test_widening_hint_is_frozen(self) -> None:
        from schemabrain.mcp.envelope import WideningHint

        hint = WideningHint(scope_requested="x", would_unblock=True, reason="y")
        with pytest.raises((ValidationError, TypeError, FrozenInstanceError)):
            hint.scope_requested = "z"  # type: ignore[misc]


class TestRecoveryV11Extensions:
    """`Recovery` gains two optional fields in v1.1. Existing call
    sites (which don't pass these) construct unchanged."""

    def test_existing_recovery_shape_unchanged(self) -> None:
        """Backward compatibility: a v1.0-shaped Recovery construction
        path must still work."""
        rec = Recovery(suggested_tool="find_relevant_tables")
        assert rec.suggested_tool == "find_relevant_tables"
        # New fields default to None.
        assert rec.suggested_rewrite is None
        assert rec.widening_hint is None

    def test_recovery_carries_suggested_rewrite(self) -> None:
        from schemabrain.mcp.envelope import SuggestedRewrite

        rewrite = SuggestedRewrite(rewritten="SELECT 1", reason="trivial")
        rec = Recovery(suggested_rewrite=rewrite)
        assert rec.suggested_rewrite is rewrite

    def test_recovery_carries_widening_hint(self) -> None:
        from schemabrain.mcp.envelope import WideningHint

        hint = WideningHint(
            scope_requested="pii:contact",
            would_unblock=True,
            reason="grant unblocks the call",
        )
        rec = Recovery(widening_hint=hint)
        assert rec.widening_hint is hint

    def test_both_new_fields_simultaneously(self) -> None:
        """A refused query might offer both a rewrite AND a widening
        hint ("you could retry without the PII column OR request the
        scope"). Both fields populated at once must be valid."""
        from schemabrain.mcp.envelope import SuggestedRewrite, WideningHint

        rewrite = SuggestedRewrite(rewritten="SELECT 1", reason="r")
        hint = WideningHint(scope_requested="pii:contact", would_unblock=True, reason="h")
        rec = Recovery(suggested_rewrite=rewrite, widening_hint=hint)
        assert rec.suggested_rewrite is rewrite
        assert rec.widening_hint is hint


class TestRecoveryKindCoupling:
    """v1.1 invariant: `suggested_rewrite` / `widening_hint` are only
    valid on `ToolError`s whose `kind` is in `REFUSAL_KINDS`. Catching
    misuse at construction prevents producers from leaking these
    fields onto unrelated error kinds (e.g. `unknown_name`)."""

    def test_suggested_rewrite_on_unknown_name_rejected(self) -> None:
        from schemabrain.mcp.envelope import SuggestedRewrite

        rewrite = SuggestedRewrite(rewritten="SELECT 1", reason="r")
        with pytest.raises(ValidationError) as exc_info:
            ToolError(
                kind="unknown_name",
                message="Table not found.",
                recovery=Recovery(suggested_rewrite=rewrite),
            )
        assert "suggested_rewrite" in str(exc_info.value)

    def test_widening_hint_on_unknown_name_rejected(self) -> None:
        from schemabrain.mcp.envelope import WideningHint

        hint = WideningHint(scope_requested="pii:contact", would_unblock=True, reason="r")
        with pytest.raises(ValidationError) as exc_info:
            ToolError(
                kind="unknown_name",
                message="Table not found.",
                recovery=Recovery(widening_hint=hint),
            )
        assert "widening_hint" in str(exc_info.value)

    @pytest.mark.parametrize("kind", ["pii_blocked", "policy_blocked", "allowlist_violation"])
    def test_suggested_rewrite_accepted_on_refusal_kinds(self, kind: str) -> None:
        from schemabrain.mcp.envelope import SuggestedRewrite

        rewrite = SuggestedRewrite(rewritten="SELECT 1", reason="r")
        err = ToolError(
            kind=kind,  # type: ignore[arg-type]
            message="Refused.",
            recovery=Recovery(suggested_rewrite=rewrite),
        )
        assert err.recovery.suggested_rewrite is rewrite

    @pytest.mark.parametrize("kind", ["pii_blocked", "policy_blocked", "allowlist_violation"])
    def test_widening_hint_accepted_on_refusal_kinds(self, kind: str) -> None:
        from schemabrain.mcp.envelope import WideningHint

        hint = WideningHint(scope_requested="pii:contact", would_unblock=True, reason="r")
        err = ToolError(
            kind=kind,  # type: ignore[arg-type]
            message="Refused.",
            recovery=Recovery(widening_hint=hint),
        )
        assert err.recovery.widening_hint is hint


class TestMinLengthGuards:
    """v1.1 constructor guards: `SuggestedRewrite` and `WideningHint`
    required string fields reject empty input. Empty strings would
    look like structurally valid recovery hints but be semantically
    useless to the agent — better to fail at the construction site."""

    def test_suggested_rewrite_rejects_empty_rewritten(self) -> None:
        from schemabrain.mcp.envelope import SuggestedRewrite

        with pytest.raises(ValidationError):
            SuggestedRewrite(rewritten="", reason="ok")

    def test_suggested_rewrite_rejects_empty_reason(self) -> None:
        from schemabrain.mcp.envelope import SuggestedRewrite

        with pytest.raises(ValidationError):
            SuggestedRewrite(rewritten="SELECT 1", reason="")

    def test_widening_hint_rejects_empty_scope(self) -> None:
        from schemabrain.mcp.envelope import WideningHint

        with pytest.raises(ValidationError):
            WideningHint(scope_requested="", would_unblock=True, reason="ok")

    def test_widening_hint_rejects_empty_reason(self) -> None:
        from schemabrain.mcp.envelope import WideningHint

        with pytest.raises(ValidationError):
            WideningHint(scope_requested="pii:contact", would_unblock=False, reason="")
