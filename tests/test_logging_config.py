"""Logging configuration tests.

The contract under test:

  - Schema Brain's logs ALWAYS go to stderr, never stdout. In MCP-stdio
    mode, stdout is the JSON-RPC wire — any byte written to stdout
    corrupts the protocol. CI catches this regression here.
  - Verbosity ladder: 0 → WARNING, 1 → INFO, 2+ → DEBUG.
  - `SCHEMABRAIN_LOG_LEVEL` env var is a fallback when no `-v` flag is
    passed (relevant for MCP-stdio where there are no CLI flags). The
    explicit flag always wins over the env var.
  - Idempotent: re-running `configure_logging()` does not stack handlers.
  - Third-party libraries (`mcp`, `anyio`, `httpx`, `httpcore`,
    `fastembed`) are pinned at WARNING even when our level drops to
    DEBUG, to keep `-vv` readable.
"""

from __future__ import annotations

import io
import logging

import pytest

from schemabrain.logging_config import configure_logging


@pytest.fixture(autouse=True)
def _reset_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clean up `schemabrain` logger state between tests.

    Without this, ordering between tests leaks state — a prior call
    leaves handlers on the logger that affect the next test.
    """
    monkeypatch.delenv("SCHEMABRAIN_LOG_LEVEL", raising=False)
    pkg = logging.getLogger("schemabrain")
    saved_level = pkg.level
    saved_propagate = pkg.propagate
    saved_handlers = list(pkg.handlers)
    pkg.handlers = []
    pkg.setLevel(logging.NOTSET)
    pkg.propagate = True
    yield
    pkg.handlers = saved_handlers
    pkg.setLevel(saved_level)
    pkg.propagate = saved_propagate


class TestVerbosityLadder:
    def test_default_is_warning(self) -> None:
        configure_logging()
        assert logging.getLogger("schemabrain").level == logging.WARNING

    def test_verbosity_zero_is_warning(self) -> None:
        configure_logging(verbosity=0)
        assert logging.getLogger("schemabrain").level == logging.WARNING

    def test_verbosity_one_is_info(self) -> None:
        configure_logging(verbosity=1)
        assert logging.getLogger("schemabrain").level == logging.INFO

    def test_verbosity_two_is_debug(self) -> None:
        configure_logging(verbosity=2)
        assert logging.getLogger("schemabrain").level == logging.DEBUG

    def test_verbosity_above_two_clamps_to_debug(self) -> None:
        # -vvvvv still means "as loud as possible", not an error.
        configure_logging(verbosity=10)
        assert logging.getLogger("schemabrain").level == logging.DEBUG


class TestEnvVarFallback:
    def test_env_var_used_when_no_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SCHEMABRAIN_LOG_LEVEL", "INFO")
        configure_logging()
        assert logging.getLogger("schemabrain").level == logging.INFO

    def test_env_var_case_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SCHEMABRAIN_LOG_LEVEL", "debug")
        configure_logging()
        assert logging.getLogger("schemabrain").level == logging.DEBUG

    def test_flag_overrides_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Flag set to INFO; env says DEBUG. Flag must win — the env var
        # is a fallback, not an override.
        monkeypatch.setenv("SCHEMABRAIN_LOG_LEVEL", "DEBUG")
        configure_logging(verbosity=1)
        assert logging.getLogger("schemabrain").level == logging.INFO

    def test_invalid_env_var_falls_back_to_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SCHEMABRAIN_LOG_LEVEL", "VERBOSE_PLEASE")
        configure_logging()
        assert logging.getLogger("schemabrain").level == logging.WARNING

    def test_invalid_env_var_emits_one_line_warning_to_stderr(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Claude Desktop deploys have no feedback path other than stderr.
        # A silent typo on SCHEMABRAIN_LOG_LEVEL leaves the operator
        # debugging blind — emit one informational line.
        monkeypatch.setenv("SCHEMABRAIN_LOG_LEVEL", "VERBOSE_PLEASE")
        configure_logging()
        err = capsys.readouterr().err
        assert "[schemabrain]" in err
        assert "VERBOSE_PLEASE" in err
        assert "SCHEMABRAIN_LOG_LEVEL" in err
        # Valid levels listed in the warning so the operator can copy-paste.
        assert "INFO" in err
        assert "DEBUG" in err

    def test_valid_env_var_emits_no_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Valid value should be silent — only invalid values warn.
        monkeypatch.setenv("SCHEMABRAIN_LOG_LEVEL", "INFO")
        configure_logging()
        err = capsys.readouterr().err
        assert "WARNING: unrecognised" not in err

    def test_env_var_warning_resolves_to_warning_level(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Exact-match round-trip on the env path: WARNING set explicitly
        # via env should still resolve to logging.WARNING. Catches a
        # regression where the env path returned a different default.
        monkeypatch.setenv("SCHEMABRAIN_LOG_LEVEL", "WARNING")
        configure_logging()
        assert logging.getLogger("schemabrain").level == logging.WARNING

    def test_empty_env_var_treated_as_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SCHEMABRAIN_LOG_LEVEL", "")
        configure_logging()
        assert logging.getLogger("schemabrain").level == logging.WARNING

    def test_empty_env_var_emits_no_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Unset/empty env is the default state; should not emit anything.
        monkeypatch.setenv("SCHEMABRAIN_LOG_LEVEL", "")
        configure_logging()
        assert "WARNING: unrecognised" not in capsys.readouterr().err


class TestStderrOnly:
    def test_handler_target_is_sys_stderr(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(verbosity=2)  # DEBUG so all levels emit
        logging.getLogger("schemabrain.test").error("smoke")
        captured = capsys.readouterr()
        # The output line must be on stderr, NOT stdout. In MCP-stdio
        # mode any byte on stdout breaks the JSON-RPC framing.
        assert "smoke" in captured.err
        assert captured.out == ""

    def test_warning_level_default_emits_on_warning(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging()
        logging.getLogger("schemabrain.x").info("should be filtered")
        logging.getLogger("schemabrain.x").warning("should appear")
        captured = capsys.readouterr()
        assert "should be filtered" not in captured.err
        assert "should appear" in captured.err

    def test_format_includes_level_logger_message(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(verbosity=2)
        logging.getLogger("schemabrain.fmt").error("bang")
        out = capsys.readouterr().err
        assert "ERROR" in out
        assert "schemabrain.fmt" in out
        assert "bang" in out

    def test_exception_traceback_is_logged(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging()
        try:
            raise RuntimeError("synthetic")
        except RuntimeError:
            logging.getLogger("schemabrain.exc").exception("caught")
        out = capsys.readouterr().err
        assert "caught" in out
        assert "RuntimeError: synthetic" in out
        assert "Traceback" in out


class TestIdempotency:
    def test_second_call_does_not_stack_handlers(self) -> None:
        configure_logging()
        configure_logging()
        configure_logging()
        # Only the one named handler should be attached, no matter how
        # many times configure_logging runs.
        our_handlers = [
            h
            for h in logging.getLogger("schemabrain").handlers
            if getattr(h, "name", "") == "schemabrain.stderr"
        ]
        assert len(our_handlers) == 1

    def test_does_not_remove_unrelated_handlers(self) -> None:
        # An operator may have wired their own handler before calling
        # the CLI as a library. Don't trample that.
        pkg = logging.getLogger("schemabrain")
        external = logging.StreamHandler(io.StringIO())
        external.name = "operator-custom"
        pkg.addHandler(external)
        configure_logging()
        assert external in pkg.handlers

    def test_reconfigure_with_new_verbosity_updates_level(self) -> None:
        configure_logging(verbosity=0)
        configure_logging(verbosity=2)
        assert logging.getLogger("schemabrain").level == logging.DEBUG


class TestThirdPartyQuieting:
    @pytest.mark.parametrize("name", ["mcp", "anyio", "httpx", "httpcore", "fastembed"])
    def test_third_party_loggers_capped_at_warning(self, name: str) -> None:
        # Even when we go full DEBUG, third-party libraries stay at
        # WARNING — `-vv` shouldn't drown the user in SDK chatter.
        configure_logging(verbosity=2)
        assert logging.getLogger(name).level == logging.WARNING


class TestNoPropagationToRoot:
    def test_propagate_disabled_after_configure(self) -> None:
        # If we propagated to root, the root's default handler
        # (lastResort) would also emit — duplicate output on stderr.
        configure_logging()
        assert logging.getLogger("schemabrain").propagate is False


class TestLiveStderrResolution:
    """The handler must re-resolve `sys.stderr` on every emit. Without
    this, configuring logging once at process start (CLI main()) would
    write to the original `sys.stderr` even when a later runtime (e.g.
    pytest's `capfd`, a redirect-stderr context manager, or any
    bootstrap that swaps `sys.stderr` after our config) replaces it.
    """

    def test_emit_follows_post_config_stderr_swap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sys

        # Configure first with a real sys.stderr in place.
        configure_logging()
        # Now swap sys.stderr to a fresh buffer and emit. The message
        # must land in the NEW buffer, not the original.
        new_stderr = io.StringIO()
        monkeypatch.setattr(sys, "stderr", new_stderr)
        logging.getLogger("schemabrain.swap").error("after-swap")
        assert "after-swap" in new_stderr.getvalue()
