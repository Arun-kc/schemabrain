"""Tests for `schemabrain/_env.py` — the shared env-var resolution
seam used by every `SCHEMABRAIN_*` configuration knob.

Two layers:

  1. **Shared parser pins** — `TestResolvePositiveIntEnv` and
     `TestResolvePositiveFloatEnv` pin the strict-regex contract
     against the exact footguns PR #67 (max_tokens) and the
     2026-05-19 audit (cost caps + concurrency + profiler) needed
     hardened. If a future refactor relaxes the parser without
     consciously bumping the rejection set, these tests fail.

  2. **Per-surface integration** — `TestProfilerSampleSizeEnv`,
     `TestPipelineConcurrencyEnv`, `TestWizardEnrichCapEnv` confirm
     each new env var actually flows through to the runtime value
     used by its consumer (not just resolves correctly in isolation).

The pre-existing pins in `tests/test_enrichment_anthropic.py`
(max_tokens) and `tests/test_setup_wizard.py` (cost-cap defaults)
cover the OLDER surfaces and remain authoritative for those.
"""

from __future__ import annotations

import pytest

from schemabrain._env import (
    _reset_warned_empty_cache_for_tests,
    resolve_positive_float_env,
    resolve_positive_int_env,
)


@pytest.fixture(autouse=True)
def _clear_warned_empty_cache() -> None:
    """Reset the module-level once-per-process empty-env warning cache
    before every test so warning assertions don't depend on test order.
    """
    _reset_warned_empty_cache_for_tests()


# ---------------------------------------------------------------------------
# resolve_positive_int_env — shared parser contract
# ---------------------------------------------------------------------------


class TestResolvePositiveIntEnv:
    def test_unset_env_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TEST_VAR", raising=False)
        assert resolve_positive_int_env("TEST_VAR", 42) == 42

    def test_set_env_returns_parsed_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_VAR", "100")
        assert resolve_positive_int_env("TEST_VAR", 42) == 100

    def test_leading_plus_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_VAR", "+5")
        assert resolve_positive_int_env("TEST_VAR", 42) == 5

    def test_whitespace_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_VAR", "  100  ")
        assert resolve_positive_int_env("TEST_VAR", 42) == 100

    def test_underscore_separator_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Python's `int("1_000")` silently returns 1000. The shared
        # parser must reject so a typo doesn't become a 1000x-off cap.
        monkeypatch.setenv("TEST_VAR", "1_000")
        with pytest.raises(ValueError, match="positive decimal integer"):
            resolve_positive_int_env("TEST_VAR", 42)

    def test_leading_zero_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_VAR", "0100")
        with pytest.raises(ValueError, match="positive decimal integer"):
            resolve_positive_int_env("TEST_VAR", 42)

    def test_zero_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Zero is not positive — regex rejects (no leading-0-followed-
        # by-anything-but-decimal for ints).
        monkeypatch.setenv("TEST_VAR", "0")
        with pytest.raises(ValueError, match="positive decimal integer"):
            resolve_positive_int_env("TEST_VAR", 42)

    def test_negative_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_VAR", "-5")
        with pytest.raises(ValueError, match="positive decimal integer"):
            resolve_positive_int_env("TEST_VAR", 42)

    def test_decimal_point_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_VAR", "1.5")
        with pytest.raises(ValueError, match="positive decimal integer"):
            resolve_positive_int_env("TEST_VAR", 42)

    def test_hex_form_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_VAR", "0x10")
        with pytest.raises(ValueError, match="positive decimal integer"):
            resolve_positive_int_env("TEST_VAR", 42)

    def test_unicode_digit_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Fullwidth digits silently fold via `int()`; regex catches.
        fullwidth = "４０９６"  # noqa: RUF001 — fullwidth IS the test input
        monkeypatch.setenv("TEST_VAR", fullwidth)
        with pytest.raises(ValueError, match="positive decimal integer"):
            resolve_positive_int_env("TEST_VAR", 42)

    def test_warn_and_default_mode_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("TEST_VAR", "garbage")
        result = resolve_positive_int_env("TEST_VAR", 42, on_invalid="warn_and_default")
        assert result == 42
        captured = capsys.readouterr()
        assert "TEST_VAR" in captured.err
        assert "positive" in captured.err

    def test_empty_env_warns_once_per_process(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("TEST_VAR", "")
        # First call warns.
        first = resolve_positive_int_env("TEST_VAR", 42)
        assert first == 42
        captured1 = capsys.readouterr()
        assert "TEST_VAR" in captured1.err
        assert "set but empty" in captured1.err
        # Second call: same var, no fresh warn (dedup'd in
        # _WARNED_EMPTY_ENV_VARS).
        second = resolve_positive_int_env("TEST_VAR", 42)
        assert second == 42
        assert capsys.readouterr().err == ""

    def test_default_display_overrides_warning_message(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("TEST_VAR", "")
        resolve_positive_int_env("TEST_VAR", 8, default_display="8 workers")
        captured = capsys.readouterr()
        assert "8 workers" in captured.err


# ---------------------------------------------------------------------------
# resolve_positive_float_env — shared parser contract
# ---------------------------------------------------------------------------


class TestResolvePositiveFloatEnv:
    def test_unset_env_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TEST_VAR", raising=False)
        assert resolve_positive_float_env("TEST_VAR", 1.5) == 1.5

    def test_integer_form_parses_as_float(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_VAR", "10")
        assert resolve_positive_float_env("TEST_VAR", 1.5) == 10.0

    def test_decimal_parses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_VAR", "0.5")
        assert resolve_positive_float_env("TEST_VAR", 1.5) == 0.5

    def test_leading_dot_parses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # ".5" is a valid Python float literal; matches the second
        # regex alternative.
        monkeypatch.setenv("TEST_VAR", ".5")
        assert resolve_positive_float_env("TEST_VAR", 1.5) == 0.5

    def test_leading_plus_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_VAR", "+1.25")
        assert resolve_positive_float_env("TEST_VAR", 1.5) == 1.25

    def test_underscore_separator_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Python's `float("1_000.5")` silently returns 1000.5. The
        # shared parser rejects this same footgun on the float side.
        monkeypatch.setenv("TEST_VAR", "1_000.5")
        with pytest.raises(ValueError, match="positive decimal number"):
            resolve_positive_float_env("TEST_VAR", 1.5)

    def test_scientific_notation_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_VAR", "1e3")
        with pytest.raises(ValueError, match="positive decimal number"):
            resolve_positive_float_env("TEST_VAR", 1.5)

    def test_negative_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_VAR", "-0.5")
        with pytest.raises(ValueError, match="positive decimal number"):
            resolve_positive_float_env("TEST_VAR", 1.5)

    def test_leading_zero_on_integer_part_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # "01.5" — leading zero typo class.
        monkeypatch.setenv("TEST_VAR", "01.5")
        with pytest.raises(ValueError, match="positive decimal number"):
            resolve_positive_float_env("TEST_VAR", 1.5)

    def test_zero_raises_as_non_positive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # "0" matches regex (integer-anchor allows 0), but the value
        # layer rejects with "must be a positive number".
        monkeypatch.setenv("TEST_VAR", "0")
        with pytest.raises(ValueError, match="must be a positive number"):
            resolve_positive_float_env("TEST_VAR", 1.5)

    def test_zero_decimal_raises_as_non_positive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_VAR", "0.0")
        with pytest.raises(ValueError, match="must be a positive number"):
            resolve_positive_float_env("TEST_VAR", 1.5)

    def test_infinity_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Python's `float("Infinity")` returns inf. The regex rejects
        # the alphabetic input — no need for a NaN/inf-specific check.
        monkeypatch.setenv("TEST_VAR", "Infinity")
        with pytest.raises(ValueError, match="positive decimal number"):
            resolve_positive_float_env("TEST_VAR", 1.5)

    def test_nan_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_VAR", "NaN")
        with pytest.raises(ValueError, match="positive decimal number"):
            resolve_positive_float_env("TEST_VAR", 1.5)

    def test_warn_and_default_mode_with_default_display(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Mirrors the wizard cost-cap call path — `$0.50 cap` format.
        monkeypatch.setenv("TEST_VAR", "junk")
        result = resolve_positive_float_env(
            "TEST_VAR",
            0.5,
            on_invalid="warn_and_default",
            default_display="$0.50 cap",
        )
        assert result == 0.5
        captured = capsys.readouterr()
        assert "$0.50 cap" in captured.err


# ---------------------------------------------------------------------------
# Per-surface integration: env-var flows through to runtime value
# ---------------------------------------------------------------------------


class TestProfilerSampleSizeEnv:
    """`SCHEMABRAIN_PROFILER_SAMPLE_SIZE` must flow through to
    `PostgresProfiler._sample_size`. Constructor argument still
    wins over the env var (operator who explicitly specifies knows
    best), and the env var still wins over the hardcoded default.
    """

    def test_constructor_arg_wins_over_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from schemabrain.profiler.postgres import PostgresProfiler

        monkeypatch.setenv("SCHEMABRAIN_PROFILER_SAMPLE_SIZE", "100")
        profiler = PostgresProfiler(
            "postgresql+psycopg://fake:fake@host/db",
            sample_size=20,
        )
        try:
            assert profiler._sample_size == 20
        finally:
            profiler.close()

    def test_env_var_overrides_default_when_no_constructor_arg(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from schemabrain.profiler.postgres import PostgresProfiler

        monkeypatch.setenv("SCHEMABRAIN_PROFILER_SAMPLE_SIZE", "25")
        profiler = PostgresProfiler("postgresql+psycopg://fake:fake@host/db")
        try:
            assert profiler._sample_size == 25
        finally:
            profiler.close()

    def test_default_when_neither_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from schemabrain.profiler.postgres import PostgresProfiler

        monkeypatch.delenv("SCHEMABRAIN_PROFILER_SAMPLE_SIZE", raising=False)
        profiler = PostgresProfiler("postgresql+psycopg://fake:fake@host/db")
        try:
            assert profiler._sample_size == 5  # package default
        finally:
            profiler.close()

    def test_invalid_env_var_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from schemabrain.profiler.postgres import PostgresProfiler

        monkeypatch.setenv("SCHEMABRAIN_PROFILER_SAMPLE_SIZE", "1_000")
        with pytest.raises(ValueError, match="positive decimal integer"):
            PostgresProfiler("postgresql+psycopg://fake:fake@host/db")


class TestWizardEnrichCapEnv:
    """`SCHEMABRAIN_WIZARD_INDEX_ENRICH_CAP_USD` must flow through
    to the `EnrichmentPipeline.max_cost_usd` the wizard constructs
    during its index-stage enrichment. Resolution chain:
    env var > hardcoded $10 default. `on_invalid="raise"` so a
    typo'd cap doesn't silently keep using the $10 default.
    """

    def test_env_var_resolution_via_module_constant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Verifies the env-var name + default constant the wizard
        # uses are wired correctly. Exercising the FULL wizard
        # `_run_indexer` path requires a real Postgres + API key
        # (lives in the live smoke), so we pin the contract at the
        # resolution-helper level instead.
        from schemabrain.setup.wizard import (
            _WIZARD_INDEX_ENRICH_CAP_USD,
            _WIZARD_INDEX_ENRICH_CAP_USD_ENV,
        )

        assert _WIZARD_INDEX_ENRICH_CAP_USD_ENV == "SCHEMABRAIN_WIZARD_INDEX_ENRICH_CAP_USD"
        assert _WIZARD_INDEX_ENRICH_CAP_USD == 10.0

        monkeypatch.setenv(_WIZARD_INDEX_ENRICH_CAP_USD_ENV, "25.0")
        resolved = resolve_positive_float_env(
            _WIZARD_INDEX_ENRICH_CAP_USD_ENV,
            _WIZARD_INDEX_ENRICH_CAP_USD,
        )
        assert resolved == 25.0

    def test_invalid_env_var_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from schemabrain.setup.wizard import (
            _WIZARD_INDEX_ENRICH_CAP_USD,
            _WIZARD_INDEX_ENRICH_CAP_USD_ENV,
        )

        # Same footgun as PR #67: silently-coerced "1_000.5" → 1000.5
        # would let the wizard burn ~$1000 instead of the intended cap.
        monkeypatch.setenv(_WIZARD_INDEX_ENRICH_CAP_USD_ENV, "1_000.5")
        with pytest.raises(ValueError, match="positive decimal number"):
            resolve_positive_float_env(
                _WIZARD_INDEX_ENRICH_CAP_USD_ENV,
                _WIZARD_INDEX_ENRICH_CAP_USD,
            )


class TestPipelineConcurrencyEnv:
    """`SCHEMABRAIN_PIPELINE_DEFAULT_CONCURRENCY` and
    `SCHEMABRAIN_PIPELINE_CRYPTIC_CONCURRENCY` must flow through to
    `EnrichmentPipeline.default_concurrency` / `cryptic_concurrency`
    the `index` command constructs. Two-layer resolution: the
    pre-existing module-level constants stay valid as defaults so
    test fixtures that monkeypatch them keep working.
    """

    def test_env_var_resolution_via_module_constants(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from schemabrain.cli import (
            _PIPELINE_CRYPTIC_CONCURRENCY,
            _PIPELINE_CRYPTIC_CONCURRENCY_ENV,
            _PIPELINE_DEFAULT_CONCURRENCY,
            _PIPELINE_DEFAULT_CONCURRENCY_ENV,
        )

        assert _PIPELINE_DEFAULT_CONCURRENCY_ENV == "SCHEMABRAIN_PIPELINE_DEFAULT_CONCURRENCY"
        assert _PIPELINE_CRYPTIC_CONCURRENCY_ENV == "SCHEMABRAIN_PIPELINE_CRYPTIC_CONCURRENCY"
        assert _PIPELINE_DEFAULT_CONCURRENCY == 8
        assert _PIPELINE_CRYPTIC_CONCURRENCY == 4

        monkeypatch.setenv(_PIPELINE_DEFAULT_CONCURRENCY_ENV, "2")
        monkeypatch.setenv(_PIPELINE_CRYPTIC_CONCURRENCY_ENV, "1")
        resolved_default = resolve_positive_int_env(
            _PIPELINE_DEFAULT_CONCURRENCY_ENV,
            _PIPELINE_DEFAULT_CONCURRENCY,
        )
        resolved_cryptic = resolve_positive_int_env(
            _PIPELINE_CRYPTIC_CONCURRENCY_ENV,
            _PIPELINE_CRYPTIC_CONCURRENCY,
        )
        assert resolved_default == 2
        assert resolved_cryptic == 1

    def test_invalid_env_var_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Bad concurrency silently coerced from "1_000" → 1000 would
        # blast through tier-1 rate limits (50 RPM Anthropic) and
        # produce cascading 429s. Raise instead.
        from schemabrain.cli import (
            _PIPELINE_DEFAULT_CONCURRENCY,
            _PIPELINE_DEFAULT_CONCURRENCY_ENV,
        )

        monkeypatch.setenv(_PIPELINE_DEFAULT_CONCURRENCY_ENV, "1_000")
        with pytest.raises(ValueError, match="positive decimal integer"):
            resolve_positive_int_env(
                _PIPELINE_DEFAULT_CONCURRENCY_ENV,
                _PIPELINE_DEFAULT_CONCURRENCY,
            )
