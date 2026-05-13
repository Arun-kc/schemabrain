"""MCP response envelope (Charter v1.0) shape tests.

The envelope is the public contract every MCP tool returns. The tool
*logic* tests in `test_mcp_tools.py` cover what each tool computes;
this file covers the envelope itself — status enum, error kinds,
recovery shape, and the data/error invariants.

Wiring-level tests (where envelopes are produced from `*_impl` results
and propagated through FastMCP) live in `test_mcp_server.py`.
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

    def test_all_seven_v1_error_kinds_accepted(self) -> None:
        # The charter v1.0 ships with exactly these seven kinds.
        # Additions are minor bumps.
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
