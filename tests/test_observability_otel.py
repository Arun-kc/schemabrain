"""Tests for `schemabrain.observability.otel`.

Three test groups:

1. `is_otel_available` — module-level flag is honest about install state.
2. `init_tracer_from_env` — env-driven activation; off when env unset;
   off + warn-once when env set but extra missing; on when both present.
3. `set_tool_span_attributes` — gen_ai.* + schemabrain.* mapping;
   status mapping for OK / ERROR; numeric vs non-numeric result keys;
   no-op when OTel not installed.

The integration path (instrument decorator + tracer → span in
InMemorySpanExporter) lives in `test_observability_instrument_otel.py`.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from schemabrain.observability import otel as otel_module


@pytest.fixture(autouse=True)
def _reset_warn_flag() -> None:
    """Reset the one-shot warn flag between tests."""
    otel_module._warned_extra_missing = False


class TestIsOtelAvailable:
    def test_returns_module_flag(self) -> None:
        # The flag reflects whether the import at the top of otel.py
        # succeeded. The test environment installs the [otel] extra
        # via uv sync, so the flag should be True locally.
        assert otel_module.is_otel_available() is otel_module._OTEL_AVAILABLE

    def test_returns_false_when_monkeypatched_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(otel_module, "_OTEL_AVAILABLE", False)
        assert otel_module.is_otel_available() is False


class TestInitTracerFromEnv:
    def test_returns_none_when_env_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        assert otel_module.init_tracer_from_env() is None

    def test_returns_none_when_env_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
        assert otel_module.init_tracer_from_env() is None

    def test_warns_once_when_env_set_but_extra_missing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(otel_module, "_OTEL_AVAILABLE", False)
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")

        # First call warns
        result1 = otel_module.init_tracer_from_env()
        captured1 = capsys.readouterr()
        assert result1 is None
        assert "schemabrain[otel]" in captured1.err
        assert "OTEL_EXPORTER_OTLP_ENDPOINT" in captured1.err

        # Second call is silent — one-shot warning
        result2 = otel_module.init_tracer_from_env()
        captured2 = capsys.readouterr()
        assert result2 is None
        assert captured2.err == ""

    @pytest.mark.skipif(
        not otel_module.is_otel_available(),
        reason="`schemabrain[otel]` extra not installed",
    )
    def test_returns_tracer_when_both_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
        tracer = otel_module.init_tracer_from_env()
        assert tracer is not None
        # The tracer must support `start_as_current_span` — that's the
        # interface @instrument relies on.
        assert hasattr(tracer, "start_as_current_span")

    @pytest.mark.skipif(
        not otel_module.is_otel_available(),
        reason="`schemabrain[otel]` extra not installed",
    )
    def test_returns_none_when_exporter_construct_raises(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")

        class _BoomExporter:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                raise RuntimeError("forced exporter init failure")

        monkeypatch.setattr(otel_module, "OTLPSpanExporter", _BoomExporter)
        result = otel_module.init_tracer_from_env()
        assert result is None
        captured = capsys.readouterr()
        assert "OTel tracer init failed" in captured.err


class TestSetToolSpanAttributes:
    @pytest.mark.skipif(
        not otel_module.is_otel_available(),
        reason="`schemabrain[otel]` extra not installed",
    )
    def test_sets_core_gen_ai_attributes_on_success(self) -> None:
        span = MagicMock()
        otel_module.set_tool_span_attributes(
            span,
            tool_name="find_relevant_tables",
            server_session_id="sess-1",
            status="success",
            duration_ms=42.5,
            error_kind=None,
            fingerprint_hex=None,
            result_summary={"matches": 3},
        )

        attrs = dict(call.args for call in span.set_attribute.call_args_list)
        assert attrs["gen_ai.system"] == "schemabrain"
        assert attrs["gen_ai.tool.name"] == "find_relevant_tables"
        assert attrs["schemabrain.session.id"] == "sess-1"
        assert attrs["schemabrain.duration_ms"] == 42.5
        assert attrs["schemabrain.status"] == "success"
        assert attrs["schemabrain.result.matches"] == 3
        # No fingerprint, no error
        assert "gen_ai.tool.call.id" not in attrs
        assert "schemabrain.error_kind" not in attrs
        # Status mapped to OK
        assert span.set_status.called

    @pytest.mark.skipif(
        not otel_module.is_otel_available(),
        reason="`schemabrain[otel]` extra not installed",
    )
    def test_attaches_fingerprint_when_present(self) -> None:
        span = MagicMock()
        otel_module.set_tool_span_attributes(
            span,
            tool_name="get_metric",
            server_session_id="sess-2",
            status="success",
            duration_ms=12.0,
            error_kind=None,
            fingerprint_hex="abc123def456" + "0" * 52,
            result_summary={"rows": 100, "fingerprint": "abc123def456" + "0" * 52},
        )
        attrs = dict(call.args for call in span.set_attribute.call_args_list)
        assert attrs["gen_ai.tool.call.id"] == "abc123def456" + "0" * 52

    @pytest.mark.skipif(
        not otel_module.is_otel_available(),
        reason="`schemabrain[otel]` extra not installed",
    )
    def test_maps_error_status_with_error_kind_description(self) -> None:
        span = MagicMock()
        otel_module.set_tool_span_attributes(
            span,
            tool_name="get_metric",
            server_session_id="sess-3",
            status="error",
            duration_ms=5.0,
            error_kind="unknown_metric",
            fingerprint_hex=None,
            result_summary=None,
        )
        attrs = dict(call.args for call in span.set_attribute.call_args_list)
        assert attrs["schemabrain.status"] == "error"
        assert attrs["schemabrain.error_kind"] == "unknown_metric"
        # The status passed to span.set_status carries the error_kind
        # as its description for dashboard grouping.
        assert span.set_status.called
        status_arg = span.set_status.call_args.args[0]
        # Status object's description attribute carries the kind.
        assert status_arg.description == "unknown_metric"

    @pytest.mark.skipif(
        not otel_module.is_otel_available(),
        reason="`schemabrain[otel]` extra not installed",
    )
    def test_maps_refused_status_to_error(self) -> None:
        span = MagicMock()
        otel_module.set_tool_span_attributes(
            span,
            tool_name="get_metric",
            server_session_id="sess-4",
            status="refused",
            duration_ms=2.0,
            error_kind="pii_blocked",
            fingerprint_hex=None,
            result_summary=None,
        )
        attrs = dict(call.args for call in span.set_attribute.call_args_list)
        assert attrs["schemabrain.status"] == "refused"
        assert attrs["schemabrain.error_kind"] == "pii_blocked"
        assert span.set_status.called

    @pytest.mark.skipif(
        not otel_module.is_otel_available(),
        reason="`schemabrain[otel]` extra not installed",
    )
    def test_skips_unsupported_result_value_types(self) -> None:
        """List / dict / None values in result_summary must be skipped
        silently rather than crash the exporter."""
        span = MagicMock()
        otel_module.set_tool_span_attributes(
            span,
            tool_name="suggest_joins",
            server_session_id="sess-5",
            status="success",
            duration_ms=10.0,
            error_kind=None,
            fingerprint_hex=None,
            result_summary={
                "paths": 4,  # int — included
                "extra_dict": {"nested": True},  # dict — skipped
                "extra_list": [1, 2, 3],  # list — skipped
            },
        )
        attrs = dict(call.args for call in span.set_attribute.call_args_list)
        assert attrs["schemabrain.result.paths"] == 4
        assert "schemabrain.result.extra_dict" not in attrs
        assert "schemabrain.result.extra_list" not in attrs

    @pytest.mark.skipif(
        not otel_module.is_otel_available(),
        reason="`schemabrain[otel]` extra not installed",
    )
    def test_no_result_summary_emits_no_result_attrs(self) -> None:
        span = MagicMock()
        otel_module.set_tool_span_attributes(
            span,
            tool_name="describe_table",
            server_session_id="sess-6",
            status="success",
            duration_ms=1.0,
            error_kind=None,
            fingerprint_hex=None,
            result_summary=None,
        )
        attrs = [c.args[0] for c in span.set_attribute.call_args_list]
        # No `schemabrain.result.*` attribute landed.
        assert not any(k.startswith("schemabrain.result.") for k in attrs)

    @pytest.mark.skipif(
        not otel_module.is_otel_available(),
        reason="`schemabrain[otel]` extra not installed",
    )
    def test_unknown_status_sets_no_span_status(self) -> None:
        """A status value outside the OK / ERROR sets — possible if
        Charter adds a new status value before the OTel adapter
        catches up — emits attributes but DOES NOT call set_status.
        This is fail-soft: dashboards see the attribute but the span
        carries OTel's default status."""
        span = MagicMock()
        otel_module.set_tool_span_attributes(
            span,
            tool_name="describe_table",
            server_session_id="sess-7",
            status="unrecognised_status_kind",
            duration_ms=1.0,
            error_kind=None,
            fingerprint_hex=None,
            result_summary=None,
        )
        attrs = dict(call.args for call in span.set_attribute.call_args_list)
        assert attrs["schemabrain.status"] == "unrecognised_status_kind"
        # set_status NOT called for an unknown status enum value.
        assert not span.set_status.called

    def test_no_op_when_otel_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(otel_module, "_OTEL_AVAILABLE", False)
        span = MagicMock()
        otel_module.set_tool_span_attributes(
            span,
            tool_name="find_relevant_tables",
            server_session_id="sess-8",
            status="success",
            duration_ms=1.0,
            error_kind=None,
            fingerprint_hex=None,
            result_summary={"matches": 3},
        )
        # Function returned early — no calls landed on the span mock.
        assert not span.set_attribute.called
        assert not span.set_status.called
