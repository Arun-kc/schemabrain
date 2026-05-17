"""Tests for `@instrument` extended with `audit_writer`.

Kept in a separate file from `test_observability_instrument.py` to keep
each test file focused; the audit-extension surface is large enough that
mixing it with the bus-only tests would obscure both. Covers:

- Audit writer is wired in: each tool call writes one row.
- MetricResult.fingerprint is injected with the audit row's hex.
- Without an audit writer the decorator behaves identically to PR #34.
- Audit failures don't block the bus emit; bus failures don't block
  the audit write.
- Argument-pair validation: audit_writer requires source_connection_id.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from schemabrain.audit.writer import AuditWriter
from schemabrain.observability.bus import NullEventBus
from schemabrain.observability.event import Event
from schemabrain.observability.instrument import instrument
from schemabrain.observability.redactor import EventRedactor

_SESSION = "11111111-2222-3333-4444-555555555555"
_SOURCE = "src-test-1"


class _CapturingBus:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)

    def close(self) -> None:
        pass


@dataclass
class _FakeResponse:
    status: str
    data: Any = None


class _ResultWithFingerprint(BaseModel):
    """Mirrors `MetricResult`'s frozen-Pydantic + fingerprint field
    contract just enough that the decorator's injection path exercises
    the real model_copy seam."""

    model_config = ConfigDict(frozen=True)

    fingerprint: str
    value: int


class _PydanticResponse(BaseModel):
    """Mirrors `ToolResponse[MetricResult]` so model_copy can re-wrap
    `data` with the injected fingerprint."""

    model_config = ConfigDict(frozen=True)

    status: str
    data: _ResultWithFingerprint | None = None


class TestArgumentValidation:
    def test_audit_writer_without_source_id_rejected(self, tmp_path: Path) -> None:
        writer = AuditWriter(tmp_path / "s.db")
        try:
            with pytest.raises(ValueError, match="source_connection_id"):
                instrument(
                    tool_name="describe_table",
                    bus=NullEventBus(),
                    redactor=EventRedactor(),
                    server_session_id=_SESSION,
                    audit_writer=writer,
                    # source_connection_id intentionally omitted
                )
        finally:
            writer.close()

    def test_no_audit_writer_no_source_id_required(self) -> None:
        """The legacy contract (PR #34) — without an audit writer the
        decorator works without source_connection_id."""
        instrument(
            tool_name="describe_table",
            bus=NullEventBus(),
            redactor=EventRedactor(),
            server_session_id=_SESSION,
        )


class TestAuditWritesOneRow:
    def test_each_call_writes_a_row(self, tmp_path: Path) -> None:
        writer = AuditWriter(tmp_path / "s.db")
        bus = _CapturingBus()
        try:

            @instrument(
                tool_name="describe_table",
                bus=bus,
                redactor=EventRedactor(),
                server_session_id=_SESSION,
                audit_writer=writer,
                source_connection_id=_SOURCE,
            )
            def fake_tool() -> _FakeResponse:
                return _FakeResponse(status="success")

            fake_tool()
            fake_tool()
            fake_tool()

            # Bus saw 3 events; audit table got 3 rows.
            assert len(bus.events) == 3
            conn = writer._require_conn()
            count = conn.execute("SELECT count(*) AS n FROM mcp_audit").fetchone()
            assert count["n"] == 3
        finally:
            writer.close()

    def test_audit_row_carries_call_context(self, tmp_path: Path) -> None:
        writer = AuditWriter(tmp_path / "s.db")
        try:

            @instrument(
                tool_name="get_metric",
                bus=NullEventBus(),
                redactor=EventRedactor(),
                server_session_id=_SESSION,
                audit_writer=writer,
                source_connection_id=_SOURCE,
            )
            def fake_tool() -> _FakeResponse:
                return _FakeResponse(status="degraded")

            fake_tool()

            conn = writer._require_conn()
            row = conn.execute("SELECT * FROM mcp_audit WHERE id = 1").fetchone()
            assert row["tool_name"] == "get_metric"
            assert row["status"] == "degraded"
            assert row["source_connection_id"] == _SOURCE
        finally:
            writer.close()


class TestFingerprintInjection:
    def test_metric_result_fingerprint_replaced_with_audit_hex(self, tmp_path: Path) -> None:
        writer = AuditWriter(tmp_path / "s.db")
        try:

            @instrument(
                tool_name="get_metric",
                bus=NullEventBus(),
                redactor=EventRedactor(),
                server_session_id=_SESSION,
                audit_writer=writer,
                source_connection_id=_SOURCE,
            )
            def fake_tool() -> _PydanticResponse:
                return _PydanticResponse(
                    status="success",
                    data=_ResultWithFingerprint(fingerprint="fp-unset", value=7),
                )

            response = fake_tool()
            # data.fingerprint should now be a 64-char hex, not the
            # placeholder.
            assert response.data is not None
            assert response.data.fingerprint != "fp-unset"
            assert len(response.data.fingerprint) == 64
            # value preserved unchanged
            assert response.data.value == 7

            # That hex must match the row's fingerprint hex.
            conn = writer._require_conn()
            row = conn.execute("SELECT fingerprint FROM mcp_audit WHERE id = 1").fetchone()
            assert response.data.fingerprint == bytes(row["fingerprint"]).hex()
        finally:
            writer.close()

    def test_response_without_fingerprint_field_passes_through(self, tmp_path: Path) -> None:
        """Tools whose response data has no `fingerprint` field
        (everything except `get_metric` in v1) get the audit write but
        no envelope mutation."""
        writer = AuditWriter(tmp_path / "s.db")
        try:

            @instrument(
                tool_name="describe_table",
                bus=NullEventBus(),
                redactor=EventRedactor(),
                server_session_id=_SESSION,
                audit_writer=writer,
                source_connection_id=_SOURCE,
            )
            def fake_tool() -> _FakeResponse:
                return _FakeResponse(status="success", data={"some": "data"})

            response = fake_tool()
            assert response.data == {"some": "data"}
        finally:
            writer.close()

    def test_response_with_none_data_passes_through(self, tmp_path: Path) -> None:
        writer = AuditWriter(tmp_path / "s.db")
        try:

            @instrument(
                tool_name="describe_table",
                bus=NullEventBus(),
                redactor=EventRedactor(),
                server_session_id=_SESSION,
                audit_writer=writer,
                source_connection_id=_SOURCE,
            )
            def fake_tool() -> _FakeResponse:
                return _FakeResponse(status="empty", data=None)

            response = fake_tool()
            assert response.data is None
        finally:
            writer.close()

    def test_data_with_fingerprint_but_no_model_copy_passes_through(self, tmp_path: Path) -> None:
        """A plain object that happens to expose `.fingerprint` but no
        `.model_copy` (not a Pydantic model) must not be mutated — the
        decorator falls through cleanly."""
        writer = AuditWriter(tmp_path / "s.db")

        class _PlainData:
            fingerprint = "fp-unset"

        try:

            @instrument(
                tool_name="get_metric",
                bus=NullEventBus(),
                redactor=EventRedactor(),
                server_session_id=_SESSION,
                audit_writer=writer,
                source_connection_id=_SOURCE,
            )
            def fake_tool() -> _FakeResponse:
                return _FakeResponse(status="success", data=_PlainData())

            response = fake_tool()
            assert response.data.fingerprint == "fp-unset"
        finally:
            writer.close()

    def test_pydantic_data_but_dataclass_response_envelope_passes_through(
        self, tmp_path: Path
    ) -> None:
        """A Pydantic data with fingerprint, but the OUTER envelope is
        a plain dataclass (no `.model_copy`). The decorator can't
        rebuild the envelope, so it returns the original — better to
        skip injection than to mis-shape the envelope."""
        writer = AuditWriter(tmp_path / "s.db")
        try:

            @instrument(
                tool_name="get_metric",
                bus=NullEventBus(),
                redactor=EventRedactor(),
                server_session_id=_SESSION,
                audit_writer=writer,
                source_connection_id=_SOURCE,
            )
            def fake_tool() -> _FakeResponse:
                return _FakeResponse(
                    status="success",
                    data=_ResultWithFingerprint(fingerprint="fp-unset", value=1),
                )

            response = fake_tool()
            # Outer envelope is _FakeResponse (no model_copy) -> not
            # re-wrapped. The original data instance is preserved.
            assert response.data.fingerprint == "fp-unset"
        finally:
            writer.close()


class TestFailureIsolation:
    def test_audit_failure_does_not_block_bus(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """If the audit writer raises OSError mid-write, the bus emit
        still happens and the tool's response is returned unchanged."""
        writer = AuditWriter(tmp_path / "s.db")
        # Sabotage the writer post-construction.
        original_write = writer.write

        def _explode(draft: object) -> None:
            raise OSError("disk full")

        writer.write = _explode  # type: ignore[assignment]
        bus = _CapturingBus()
        try:

            @instrument(
                tool_name="describe_table",
                bus=bus,
                redactor=EventRedactor(),
                server_session_id=_SESSION,
                audit_writer=writer,
                source_connection_id=_SOURCE,
            )
            def fake_tool() -> _FakeResponse:
                return _FakeResponse(status="success")

            response = fake_tool()
            assert response.status == "success"
            # Bus saw the event.
            assert len(bus.events) == 1
            # Stderr carries the once-per-tool drop notice.
            captured = capsys.readouterr()
            assert "audit" in captured.err
            assert "OSError" in captured.err
        finally:
            writer.write = original_write  # type: ignore[assignment]
            writer.close()

    def test_audit_failure_skips_fingerprint_injection(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """If the audit write fails, the response's fingerprint stays
        at whatever the tool set (`"fp-unset"`) — the decorator must
        NOT inject a hex that doesn't correspond to a persisted row."""
        writer = AuditWriter(tmp_path / "s.db")

        def _explode(draft: object) -> None:
            raise OSError("disk full")

        writer.write = _explode  # type: ignore[assignment]
        try:

            @instrument(
                tool_name="get_metric",
                bus=NullEventBus(),
                redactor=EventRedactor(),
                server_session_id=_SESSION,
                audit_writer=writer,
                source_connection_id=_SOURCE,
            )
            def fake_tool() -> _PydanticResponse:
                return _PydanticResponse(
                    status="success",
                    data=_ResultWithFingerprint(fingerprint="fp-unset", value=7),
                )

            response = fake_tool()
            assert response.data is not None
            assert response.data.fingerprint == "fp-unset"
        finally:
            writer.close()

    def test_audit_programming_bug_logs_every_time(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Non-OSError failures (write returns wrong type, attribute
        missing, etc.) are programming bugs — log every occurrence so
        a regression doesn't silently silence after the first call."""
        writer = AuditWriter(tmp_path / "s.db")

        def _explode(draft: object) -> None:
            raise RuntimeError("writer corrupted")

        writer.write = _explode  # type: ignore[assignment]
        try:

            @instrument(
                tool_name="describe_table",
                bus=NullEventBus(),
                redactor=EventRedactor(),
                server_session_id=_SESSION,
                audit_writer=writer,
                source_connection_id=_SOURCE,
            )
            def fake_tool() -> _FakeResponse:
                return _FakeResponse(status="success")

            fake_tool()
            fake_tool()
            fake_tool()
            captured = capsys.readouterr()
            # 3 calls -> 3 BUG log lines (no dedup for programming bugs).
            assert captured.err.count("RuntimeError") == 3
            assert captured.err.count("audit BUG in describe_table") == 3
        finally:
            writer.close()


class TestNoAuditWriter:
    def test_decorator_without_audit_writer_is_legacy_pr34_behavior(self) -> None:
        """Without an audit writer the decorator must behave exactly
        as before PR #35 — no audit writes, no fingerprint mutation,
        same bus emission."""
        bus = _CapturingBus()

        @instrument(
            tool_name="describe_table",
            bus=bus,
            redactor=EventRedactor(),
            server_session_id=_SESSION,
        )
        def fake_tool() -> _PydanticResponse:
            return _PydanticResponse(
                status="success",
                data=_ResultWithFingerprint(fingerprint="fp-unset", value=42),
            )

        response = fake_tool()
        assert response.data is not None
        # Without an audit writer the response is returned untouched.
        assert response.data.fingerprint == "fp-unset"
        assert response.data.value == 42
        assert len(bus.events) == 1
