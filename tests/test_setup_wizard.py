"""Tests for `schemabrain.setup.wizard`.

Covers:

  - `StageOutcome` validation invariants
  - `WizardResult.aborted_at` property
  - `run_wizard` state machine (happy path + abort-on-fail per stage +
    continue-on-fail at stages 3 and 4)
  - Production stage handlers (`_stage_source_check`, `_stage_index`,
    `_stage_entities`, `_stage_metrics`, `_stage_wire_host`,
    `_stage_next_step`) via monkeypatched dependencies — no real
    database is required.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from schemabrain.errors import GuidedError
from schemabrain.setup import wizard
from schemabrain.setup.hosts import SchemabrainSnippet
from schemabrain.setup.init_flow import InitRefusal, InitResult
from schemabrain.setup.wizard import (
    DEFAULT_STAGES,
    StageOutcome,
    WizardConfig,
    WizardContext,
    WizardResult,
    WizardStage,
    run_default_wizard,
    run_wizard,
)

# ----- fixtures ------------------------------------------------------------


@pytest.fixture
def base_config(tmp_path: Path) -> WizardConfig:
    """Minimal valid `WizardConfig`. Tests override individual fields."""
    return WizardConfig(
        source_url="sqlite:///:memory:",
        store_path=tmp_path / "wizard.db",
        host="manual",
        env_var_name="SCHEMABRAIN_DATABASE_URL",
        skip_index=False,
        no_entities=False,
        enrich=False,
        entities_max_cost_usd=None,
        assume_yes=False,
    )


def _ok_stage(stage_num: int, name: str, *, abort: bool = True) -> WizardStage:
    """Build a fake stage that always returns a `done` outcome."""
    return WizardStage(
        stage=stage_num,
        name=name,
        abort_on_fail=abort,
        handler=lambda _ctx, _s=stage_num, _n=name: StageOutcome(
            stage=_s, name=_n, status="done", message=f"{_n} ok"
        ),
    )


def _failing_stage(stage_num: int, name: str, *, abort: bool = True) -> WizardStage:
    """Build a fake stage that always returns a `failed` outcome."""
    return WizardStage(
        stage=stage_num,
        name=name,
        abort_on_fail=abort,
        handler=lambda _ctx, _s=stage_num, _n=name: StageOutcome(
            stage=_s,
            name=_n,
            status="failed",
            message=f"{_n} blew up",
            next_step=f"fix {_n} and re-run",
        ),
    )


# ----- StageOutcome --------------------------------------------------------


class TestStageOutcome:
    def test_accepts_valid_fields(self) -> None:
        outcome = StageOutcome(stage=1, name="source_check", status="done", message="all good")
        assert outcome.stage == 1
        assert outcome.next_step is None

    def test_optional_next_step_is_preserved(self) -> None:
        outcome = StageOutcome(
            stage=2,
            name="index",
            status="skipped",
            message="nothing to do",
            next_step="run `schemabrain index` later",
        )
        assert outcome.next_step == "run `schemabrain index` later"

    def test_rejects_zero_stage(self) -> None:
        with pytest.raises(ValueError, match="stage must be >= 1"):
            StageOutcome(stage=0, name="x", status="done", message="m")

    def test_rejects_negative_stage(self) -> None:
        with pytest.raises(ValueError, match="stage must be >= 1"):
            StageOutcome(stage=-3, name="x", status="done", message="m")

    def test_rejects_empty_name(self) -> None:
        with pytest.raises(ValueError, match="non-empty identifier"):
            StageOutcome(stage=1, name="", status="done", message="m")

    def test_rejects_empty_message(self) -> None:
        with pytest.raises(ValueError, match="non-empty string"):
            StageOutcome(stage=1, name="x", status="done", message="")

    def test_rejects_unknown_status(self) -> None:
        with pytest.raises(ValueError, match="status must be one of"):
            StageOutcome(
                stage=1,
                name="x",
                status="banana",  # type: ignore[arg-type]
                message="m",
            )

    def test_failed_outcome_requires_next_step(self) -> None:
        # The dead-end-failure invariant: every `failed` outcome must
        # carry a recovery hint so the renderer's dim next-step line
        # always points the user somewhere actionable.
        with pytest.raises(ValueError, match="must include next_step"):
            StageOutcome(
                stage=1,
                name="x",
                status="failed",
                message="boom",
            )

    def test_failed_outcome_with_empty_next_step_rejected(self) -> None:
        with pytest.raises(ValueError, match="must include next_step"):
            StageOutcome(
                stage=1,
                name="x",
                status="failed",
                message="boom",
                next_step="",
            )

    def test_duration_s_defaults_to_zero(self) -> None:
        # Default 0.0 keeps every existing StageOutcome construction
        # backward-compatible — tests that don't care about timing can
        # ignore the field entirely.
        outcome = StageOutcome(stage=1, name="x", status="done", message="m")
        assert outcome.duration_s == 0.0

    def test_duration_s_accepts_positive_value(self) -> None:
        outcome = StageOutcome(stage=1, name="x", status="done", message="m", duration_s=2.5)
        assert outcome.duration_s == 2.5

    def test_duration_s_rejects_negative(self) -> None:
        # A negative duration would mean perf_counter measurement went
        # backwards — an orchestrator bug, not a user error. Fail fast.
        with pytest.raises(ValueError, match="duration_s must be >= 0"):
            StageOutcome(stage=1, name="x", status="done", message="m", duration_s=-0.1)


class TestWizardConfigInvariants:
    def _build(self, **overrides: object) -> WizardConfig:
        fields: dict[str, object] = {
            "source_url": "sqlite:///:memory:",
            "store_path": Path("/tmp/test.db"),  # nosec B108 — never opened
            "host": "manual",
            "env_var_name": "SCHEMABRAIN_DATABASE_URL",
            "skip_index": False,
            "no_entities": False,
            "enrich": False,
            "entities_max_cost_usd": None,
            "assume_yes": False,
        }
        fields.update(overrides)
        return WizardConfig(**fields)  # type: ignore[arg-type]

    def test_rejects_unknown_host(self) -> None:
        with pytest.raises(ValueError, match="host must be one of"):
            self._build(host="anthropic-desktop")  # type: ignore[arg-type]

    def test_accepts_each_valid_host(self) -> None:
        for host in ("claude-desktop", "claude-code", "manual"):
            cfg = self._build(host=host)
            assert cfg.host == host

    def test_rejects_zero_entities_max_cost(self) -> None:
        with pytest.raises(ValueError, match="entities_max_cost_usd"):
            self._build(entities_max_cost_usd=0)

    def test_rejects_negative_entities_max_cost(self) -> None:
        with pytest.raises(ValueError, match="entities_max_cost_usd"):
            self._build(entities_max_cost_usd=-1.0)

    def test_accepts_none_entities_max_cost(self) -> None:
        cfg = self._build(entities_max_cost_usd=None)
        assert cfg.entities_max_cost_usd is None

    def test_accepts_positive_entities_max_cost(self) -> None:
        cfg = self._build(entities_max_cost_usd=0.5)
        assert cfg.entities_max_cost_usd == 0.5

    def test_rejects_zero_metrics_max_cost(self) -> None:
        with pytest.raises(ValueError, match="metrics_max_cost_usd"):
            self._build(metrics_max_cost_usd=0)

    def test_rejects_negative_metrics_max_cost(self) -> None:
        with pytest.raises(ValueError, match="metrics_max_cost_usd"):
            self._build(metrics_max_cost_usd=-1.0)

    def test_accepts_none_metrics_max_cost(self) -> None:
        cfg = self._build(metrics_max_cost_usd=None)
        assert cfg.metrics_max_cost_usd is None

    def test_accepts_positive_metrics_max_cost(self) -> None:
        cfg = self._build(metrics_max_cost_usd=0.25)
        assert cfg.metrics_max_cost_usd == 0.25

    def test_no_metrics_defaults_to_false(self) -> None:
        # New optional field — existing positional + keyword callers
        # should not need to know it exists. Default must be `False`
        # to preserve the prior wizard behaviour (run the stage).
        cfg = self._build()
        assert cfg.no_metrics is False


# ----- WizardResult.aborted_at --------------------------------------------


class TestWizardResultAbortedAt:
    def test_aborted_at_none_when_not_aborted(self) -> None:
        result = WizardResult(
            outcomes=(
                StageOutcome(stage=1, name="a", status="done", message="ok"),
                StageOutcome(stage=2, name="b", status="skipped", message="skip"),
            ),
            aborted=False,
        )
        assert result.aborted_at is None

    def test_aborted_at_returns_first_failure(self) -> None:
        first_fail = StageOutcome(
            stage=2,
            name="b",
            status="failed",
            message="boom",
            next_step="re-run",
        )
        result = WizardResult(
            outcomes=(
                StageOutcome(stage=1, name="a", status="done", message="ok"),
                first_fail,
            ),
            aborted=True,
        )
        assert result.aborted_at is first_fail


# ----- run_wizard state machine --------------------------------------------


class TestRunWizardStateMachine:
    def test_happy_path_runs_all_stages(self, base_config: WizardConfig) -> None:
        stages = [_ok_stage(i, f"stage{i}") for i in range(1, 6)]
        result = run_wizard(base_config, stages=stages)

        assert result.aborted is False
        assert result.aborted_at is None
        assert len(result.outcomes) == 5
        assert [o.stage for o in result.outcomes] == [1, 2, 3, 4, 5]
        assert all(o.status == "done" for o in result.outcomes)

    def test_aborts_on_stage_1_failure(self, base_config: WizardConfig) -> None:
        stages = [
            _failing_stage(1, "source_check"),
            _ok_stage(2, "stage2"),
            _ok_stage(3, "stage3"),
        ]
        result = run_wizard(base_config, stages=stages)

        assert result.aborted is True
        assert len(result.outcomes) == 1
        assert result.aborted_at is not None
        assert result.aborted_at.name == "source_check"

    def test_aborts_on_stage_2_failure(self, base_config: WizardConfig) -> None:
        stages = [
            _ok_stage(1, "stage1"),
            _failing_stage(2, "index"),
            _ok_stage(3, "stage3"),
        ]
        result = run_wizard(base_config, stages=stages)

        assert result.aborted is True
        # Stage 1 ran + recorded; stage 2 failed; stage 3 was not reached.
        assert [o.name for o in result.outcomes] == ["stage1", "index"]

    def test_continues_through_stage_3_failure(self, base_config: WizardConfig) -> None:
        # Stage 3's `abort_on_fail=False` is the contract: entity suggestion
        # is aspirational, and the wizard should still wire the host.
        stages = [
            _ok_stage(1, "stage1"),
            _ok_stage(2, "stage2"),
            _failing_stage(3, "entities", abort=False),
            _ok_stage(4, "stage4"),
            _ok_stage(5, "stage5"),
        ]
        result = run_wizard(base_config, stages=stages)

        assert result.aborted is False
        assert [o.status for o in result.outcomes] == [
            "done",
            "done",
            "failed",
            "done",
            "done",
        ]

    def test_aborts_on_stage_4_failure(self, base_config: WizardConfig) -> None:
        stages = [
            _ok_stage(1, "stage1"),
            _ok_stage(2, "stage2"),
            _ok_stage(3, "stage3"),
            _failing_stage(4, "wire_host"),
            _ok_stage(5, "stage5"),
        ]
        result = run_wizard(base_config, stages=stages)

        assert result.aborted is True
        assert len(result.outcomes) == 4

    def test_default_stage_list_uses_canonical_handlers(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `run_default_wizard` delegates to `run_wizard` with `DEFAULT_STAGES`.
        # We monkeypatch all the substrate calls so the wizard runs end-to-end
        # without a live database or a real host config — the assertion is that
        # the default stage list reaches stage 7 with this stubbing in place.
        # `skip_index=True` short-circuits the stage-2 Postgres-only refusal
        # without us having to stub the whole indexer pipeline.
        cfg = WizardConfig(
            source_url=base_config.source_url,
            store_path=base_config.store_path,
            host=base_config.host,
            env_var_name=base_config.env_var_name,
            skip_index=True,
            no_entities=base_config.no_entities,
            enrich=base_config.enrich,
            entities_max_cost_usd=base_config.entities_max_cost_usd,
            assume_yes=base_config.assume_yes,
        )
        monkeypatch.setattr(wizard, "_validate_source_reachable", lambda _url: None)
        monkeypatch.setattr(wizard, "_validate_source_read_only", lambda _url: None)

        canned_snippet = SchemabrainSnippet(
            command="uvx",
            args=(
                "schemabrain==0.2.0a1",
                "serve",
                "--url-env",
                "SCHEMABRAIN_DATABASE_URL",
                "--store-path",
                str(base_config.store_path.resolve()),
            ),
            env={"SCHEMABRAIN_DATABASE_URL": "sqlite:///:memory:"},
        )
        canned = InitResult(
            host="manual",
            snippet=canned_snippet,
            state="printed_only",
        )
        monkeypatch.setattr(wizard, "init", lambda **_kw: canned)

        result = run_default_wizard(cfg)
        assert result.aborted is False
        assert len(result.outcomes) == 7
        assert [o.name for o in result.outcomes] == [
            "source_check",
            "index",
            "entities",
            "metrics",
            "joins",
            "wire_host",
            "next_step",
        ]
        assert result.host_install_result is canned

    def test_aborted_outcome_is_terminal(self, base_config: WizardConfig) -> None:
        # After an abort, downstream stages MUST NOT have been called.
        # This protects against future regressions where someone wires
        # an `if outcome.status != "failed": continue` style bug.
        @dataclass
        class CallTracker:
            seen: list[str] = field(default_factory=list)

        tracker = CallTracker()

        def _tracked(stage_num: int, name: str, status: wizard.StageStatus) -> WizardStage:
            def handler(_ctx: WizardContext, _n: str = name) -> StageOutcome:
                tracker.seen.append(_n)
                return StageOutcome(
                    stage=stage_num,
                    name=_n,
                    status=status,
                    message=f"{_n} {status}",
                    next_step="recover" if status == "failed" else None,
                )

            return WizardStage(stage=stage_num, name=name, handler=handler, abort_on_fail=True)

        stages = [
            _tracked(1, "stage1", "done"),
            _tracked(2, "stage2", "failed"),
            _tracked(3, "stage3", "done"),
        ]
        run_wizard(base_config, stages=stages)
        assert tracker.seen == ["stage1", "stage2"]

    def test_run_wizard_captures_per_stage_duration(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The orchestrator measures perf_counter around each handler
        # call and threads the elapsed seconds into the outcome via
        # `dataclasses.replace`, even when the handler itself returned
        # `duration_s=0.0`. Tests pin the perf_counter via monkeypatch
        # so the timing is deterministic.
        ticks = iter([0.0, 0.42, 0.42, 0.5, 0.5, 1.6, 1.6, 1.65, 1.65, 1.7])

        def fake_perf_counter() -> float:
            return next(ticks)

        monkeypatch.setattr(wizard.time, "perf_counter", fake_perf_counter)

        stages = [_ok_stage(i, f"stage{i}") for i in range(1, 6)]
        result = run_wizard(base_config, stages=stages)

        # Per the fake clock: 0.42, 0.08, 1.1, 0.05, 0.05 seconds.
        durations = [round(o.duration_s, 2) for o in result.outcomes]
        assert durations == [0.42, 0.08, 1.10, 0.05, 0.05]

    def test_stage_context_wraps_every_handler(self, base_config: WizardConfig) -> None:
        # The orchestrator MUST invoke the `stage_context` factory for
        # every stage (not just the slow ones) — the CLI's factory
        # internally decides whether to render a spinner. Centralising
        # that decision avoids leaking spinner-stage knowledge into
        # the orchestrator.
        import contextlib
        from collections.abc import Iterator

        seen: list[str] = []

        @contextlib.contextmanager
        def _recording_context(stage: wizard.WizardStage) -> Iterator[None]:
            seen.append(stage.name)
            yield

        stages = [_ok_stage(i, f"stage{i}") for i in range(1, 6)]
        run_wizard(base_config, stages=stages, stage_context=_recording_context)

        assert seen == ["stage1", "stage2", "stage3", "stage4", "stage5"]

    def test_stage_context_default_is_no_op_passthrough(self, base_config: WizardConfig) -> None:
        # Omitting `stage_context` keeps the wizard backward-compatible —
        # handlers run exactly as before.
        stages = [_ok_stage(i, f"stage{i}") for i in range(1, 6)]
        result = run_wizard(base_config, stages=stages)
        assert result.aborted is False
        assert len(result.outcomes) == 5

    def test_run_wizard_preserves_duration_on_abort(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Even when a stage aborts the wizard, the failed outcome
        # records its own elapsed time so the renderer can show the
        # user how long the broken stage spent before giving up.
        ticks = iter([0.0, 0.2, 0.2, 2.5])

        def fake_perf_counter() -> float:
            return next(ticks)

        monkeypatch.setattr(wizard.time, "perf_counter", fake_perf_counter)

        stages = [
            _ok_stage(1, "stage1"),
            _failing_stage(2, "index"),
            _ok_stage(3, "stage3"),
        ]
        result = run_wizard(base_config, stages=stages)

        assert result.aborted is True
        assert len(result.outcomes) == 2
        assert round(result.outcomes[0].duration_s, 2) == 0.20
        assert round(result.outcomes[1].duration_s, 2) == 2.30


# ----- _stage_source_check (production handler) ----------------------------


class TestStageSourceCheck:
    def test_done_for_sqlite_source(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # SQLite source: reachable validator runs, read-only check skipped.
        called: list[str] = []
        monkeypatch.setattr(wizard, "_validate_source_reachable", lambda url: called.append(url))

        def _ro_should_not_run(_url: str) -> None:  # pragma: no cover — guard
            raise AssertionError("read-only check ran for SQLite source")

        monkeypatch.setattr(wizard, "_validate_source_read_only", _ro_should_not_run)

        ctx = WizardContext(config=base_config)
        outcome = wizard._stage_source_check(ctx)

        assert outcome.status == "done"
        assert outcome.stage == 1
        assert called == ["sqlite:///:memory:"]
        # SQLite source has no session-level read-only concept — message
        # acknowledges the difference rather than implying it.
        assert outcome.message == "source reachable"

    def test_done_for_postgres_source(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Postgres source: BOTH validators run.
        pg_config = WizardConfig(
            source_url="postgresql+psycopg://u:p@localhost/db",
            store_path=base_config.store_path,
            host=base_config.host,
            env_var_name=base_config.env_var_name,
            skip_index=base_config.skip_index,
            no_entities=base_config.no_entities,
            enrich=base_config.enrich,
            entities_max_cost_usd=base_config.entities_max_cost_usd,
            assume_yes=base_config.assume_yes,
        )
        reach_calls: list[str] = []
        ro_calls: list[str] = []
        monkeypatch.setattr(
            wizard, "_validate_source_reachable", lambda url: reach_calls.append(url)
        )
        monkeypatch.setattr(wizard, "_validate_source_read_only", lambda url: ro_calls.append(url))

        ctx = WizardContext(config=pg_config)
        outcome = wizard._stage_source_check(ctx)

        assert outcome.status == "done"
        assert reach_calls == ["postgresql+psycopg://u:p@localhost/db"]
        assert ro_calls == ["postgresql+psycopg://u:p@localhost/db"]
        # Postgres-specific phrasing — the `·` separator matches the
        # rest of the wizard's visual vocabulary.
        assert outcome.message == "Postgres reachable · session is read-only"

    def test_failed_when_source_unreachable(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        err = GuidedError(
            kind="init_source_unreachable",
            message="could not reach the source database",
            why="connection refused",
            fix="verify the URL",
            next_step="run `schemabrain doctor --source $URL`",
        )

        def _raise(_url: str) -> None:
            raise InitRefusal(err)

        monkeypatch.setattr(wizard, "_validate_source_reachable", _raise)

        ctx = WizardContext(config=base_config)
        outcome = wizard._stage_source_check(ctx)

        assert outcome.status == "failed"
        assert outcome.message == "could not reach the source database"
        assert outcome.next_step == "run `schemabrain doctor --source $URL`"

    def test_failed_when_source_not_read_only(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pg_config = WizardConfig(
            source_url="postgresql+psycopg://u:p@localhost/db",
            store_path=base_config.store_path,
            host=base_config.host,
            env_var_name=base_config.env_var_name,
            skip_index=base_config.skip_index,
            no_entities=base_config.no_entities,
            enrich=base_config.enrich,
            entities_max_cost_usd=base_config.entities_max_cost_usd,
            assume_yes=base_config.assume_yes,
        )
        err = GuidedError(
            kind="init_source_not_read_only",
            message="source session reports default_transaction_read_only='off'",
            why="Schema Brain requires a read-only session",
            fix="grant the role permission to SET read-only",
            next_step=None,
        )

        monkeypatch.setattr(wizard, "_validate_source_reachable", lambda _url: None)

        def _raise(_url: str) -> None:
            raise InitRefusal(err)

        monkeypatch.setattr(wizard, "_validate_source_read_only", _raise)

        ctx = WizardContext(config=pg_config)
        outcome = wizard._stage_source_check(ctx)

        assert outcome.status == "failed"
        # `_failed_from_refusal` substitutes the default diagnostic
        # hint when the GuidedError carries `next_step=None`, because
        # `failed` outcomes are required to expose a recovery path.
        assert outcome.next_step is not None
        assert "schemabrain doctor" in outcome.next_step


class TestStageSourceCheckDbtDetection:
    """PR C: stage 1 populates `ctx.dbt_manifest_path` when either
    `cfg.from_dbt` is set explicitly or auto-detection finds a manifest.

    Tests the integration; the unit-level filesystem-probe tests live
    in `TestAutoDetectDbtManifest`.
    """

    def _pg_config(self, base_config: WizardConfig, **overrides: object) -> WizardConfig:
        fields: dict[str, object] = {
            "source_url": "postgresql+psycopg://u:p@localhost/db",
            "store_path": base_config.store_path,
            "host": base_config.host,
            "env_var_name": base_config.env_var_name,
            "skip_index": base_config.skip_index,
            "no_entities": base_config.no_entities,
            "enrich": base_config.enrich,
            "entities_max_cost_usd": base_config.entities_max_cost_usd,
            "assume_yes": base_config.assume_yes,
        }
        fields.update(overrides)
        return WizardConfig(**fields)  # type: ignore[arg-type]

    def test_explicit_from_dbt_with_valid_path_populates_ctx(
        self,
        base_config: WizardConfig,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manifest = tmp_path / "manifest.json"
        manifest.write_text("{}")
        cfg = self._pg_config(base_config, from_dbt=manifest)
        monkeypatch.setattr(wizard, "_validate_source_reachable", lambda _url: None)
        monkeypatch.setattr(wizard, "_validate_source_read_only", lambda _url: None)

        ctx = WizardContext(config=cfg)
        outcome = wizard._stage_source_check(ctx)

        assert outcome.status == "done"
        assert ctx.dbt_manifest_path == manifest
        assert "dbt manifest detected" in outcome.message
        assert outcome.next_step is not None
        assert "stages 3 and 4 will import" in outcome.next_step

    def test_explicit_from_dbt_with_missing_path_fails_stage(
        self,
        base_config: WizardConfig,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        missing = tmp_path / "does_not_exist.json"
        cfg = self._pg_config(base_config, from_dbt=missing)
        monkeypatch.setattr(wizard, "_validate_source_reachable", lambda _url: None)
        monkeypatch.setattr(wizard, "_validate_source_read_only", lambda _url: None)

        ctx = WizardContext(config=cfg)
        outcome = wizard._stage_source_check(ctx)

        assert outcome.status == "failed"
        assert outcome.stage == 1
        assert "does not exist" in outcome.message
        assert outcome.next_step is not None
        assert "dbt compile" in outcome.next_step
        # Context should not have a manifest path on failure.
        assert ctx.dbt_manifest_path is None

    def test_explicit_from_dbt_with_sqlite_source_fails_stage(
        self,
        base_config: WizardConfig,
        tmp_path: Path,
    ) -> None:
        # The dbt importer requires Postgres for live schema verification.
        # Surface that as a stage-1 failure rather than a stage-3 surprise.
        manifest = tmp_path / "manifest.json"
        manifest.write_text("{}")
        # base_config has SQLite source.
        cfg = WizardConfig(
            source_url=base_config.source_url,
            store_path=base_config.store_path,
            host=base_config.host,
            env_var_name=base_config.env_var_name,
            skip_index=base_config.skip_index,
            no_entities=base_config.no_entities,
            enrich=base_config.enrich,
            entities_max_cost_usd=base_config.entities_max_cost_usd,
            assume_yes=base_config.assume_yes,
            from_dbt=manifest,
        )

        ctx = WizardContext(config=cfg)
        outcome = wizard._stage_source_check(ctx)

        assert outcome.status == "failed"
        assert "Postgres" in outcome.message
        assert ctx.dbt_manifest_path is None

    def test_auto_detect_runs_for_postgres_source(
        self,
        base_config: WizardConfig,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # When --from-dbt is not set, auto-detect kicks in. Inject a
        # canned manifest via the `_auto_detect_dbt_manifest` seam so
        # we don't depend on the test's actual cwd.
        manifest = tmp_path / "auto_manifest.json"
        manifest.write_text("{}")
        cfg = self._pg_config(base_config)
        monkeypatch.setattr(wizard, "_validate_source_reachable", lambda _url: None)
        monkeypatch.setattr(wizard, "_validate_source_read_only", lambda _url: None)
        monkeypatch.setattr(wizard, "_auto_detect_dbt_manifest", lambda: manifest)

        ctx = WizardContext(config=cfg)
        outcome = wizard._stage_source_check(ctx)

        assert outcome.status == "done"
        assert ctx.dbt_manifest_path == manifest
        assert "dbt manifest detected" in outcome.message

    def test_auto_detect_skipped_for_sqlite_source(
        self,
        base_config: WizardConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # SQLite source: even if a manifest is "detectable", the
        # wizard skips dbt mode because the dbt importer needs a live
        # Postgres connection for column verification.
        monkeypatch.setattr(wizard, "_validate_source_reachable", lambda _url: None)
        sentinel_called: list[bool] = []

        def _should_not_run() -> None:  # pragma: no cover — guard
            sentinel_called.append(True)
            raise AssertionError("auto-detect ran for SQLite source")

        monkeypatch.setattr(wizard, "_auto_detect_dbt_manifest", _should_not_run)

        ctx = WizardContext(config=base_config)
        outcome = wizard._stage_source_check(ctx)

        assert outcome.status == "done"
        assert ctx.dbt_manifest_path is None
        assert sentinel_called == []

    def test_auto_detect_returns_none_leaves_ctx_unset(
        self,
        base_config: WizardConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg = self._pg_config(base_config)
        monkeypatch.setattr(wizard, "_validate_source_reachable", lambda _url: None)
        monkeypatch.setattr(wizard, "_validate_source_read_only", lambda _url: None)
        monkeypatch.setattr(wizard, "_auto_detect_dbt_manifest", lambda: None)

        ctx = WizardContext(config=cfg)
        outcome = wizard._stage_source_check(ctx)

        assert outcome.status == "done"
        assert ctx.dbt_manifest_path is None
        # No dbt-related next_step when no manifest detected.
        assert outcome.next_step is None
        # Base message preserved — no `dbt manifest detected` suffix.
        assert "dbt manifest" not in outcome.message


# ----- _stage_index --------------------------------------------------------


def _pg_config(base: WizardConfig, **overrides: object) -> WizardConfig:
    """Build a Postgres-flavoured config based on `base` with field overrides."""
    fields: dict[str, object] = {
        "source_url": "postgresql+psycopg://u:p@localhost/db",
        "store_path": base.store_path,
        "host": base.host,
        "env_var_name": base.env_var_name,
        "skip_index": base.skip_index,
        "no_entities": base.no_entities,
        "enrich": base.enrich,
        "entities_max_cost_usd": base.entities_max_cost_usd,
        "assume_yes": base.assume_yes,
    }
    fields.update(overrides)
    return WizardConfig(**fields)  # type: ignore[arg-type]


class TestStageIndex:
    def test_skipped_when_skip_index_flag_set(self, base_config: WizardConfig) -> None:
        cfg = _pg_config(base_config, skip_index=True)
        outcome = wizard._stage_index(WizardContext(config=cfg))

        assert outcome.stage == 2
        assert outcome.status == "skipped"
        assert "--skip-index" in outcome.message

    def test_skipped_for_non_postgres_source(self, base_config: WizardConfig) -> None:
        # base_config uses sqlite:///:memory:. Indexing only supports
        # Postgres today, but we surface that as `skipped` rather
        # than `failed` so the wizard still wires the host for the
        # non-Postgres user.
        outcome = wizard._stage_index(WizardContext(config=base_config))

        assert outcome.status == "skipped"
        assert "Postgres" in outcome.message
        assert outcome.next_step is not None
        assert "Postgres URL" in outcome.next_step

    def test_skipped_when_store_already_has_tables(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _pg_config(base_config)
        # Materialise the store so the existence check passes.
        cfg.store_path.touch()
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")
        monkeypatch.setattr(wizard, "_peek_store_table_count", lambda _p, _sid: 7)

        # Guard: even if the store has tables, the indexer must NOT
        # have been invoked.
        monkeypatch.setattr(
            wizard,
            "_run_indexer",
            lambda **_kw: pytest.fail("indexer ran despite idempotent skip"),
        )

        outcome = wizard._stage_index(WizardContext(config=cfg))

        assert outcome.status == "skipped"
        assert "already indexed" in outcome.message
        assert "7 table" in outcome.message

    def test_failed_on_schema_version_mismatch(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from schemabrain.core.store import SchemaVersionMismatchError

        cfg = _pg_config(base_config)
        cfg.store_path.touch()

        mismatch = SchemaVersionMismatchError(
            "store created with schema v10 but installed schemabrain expects v12"
        )
        peek_failure = StageOutcome(
            stage=2,
            name="index",
            status="failed",
            message=str(mismatch),
            next_step=f"delete {cfg.store_path} and re-run",
        )
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")
        monkeypatch.setattr(wizard, "_peek_store_table_count", lambda _p, _sid: peek_failure)

        outcome = wizard._stage_index(WizardContext(config=cfg))

        assert outcome.status == "failed"
        assert outcome.message == str(mismatch)
        assert outcome.next_step is not None
        assert "delete" in outcome.next_step

    def test_failed_when_enrich_set_without_api_key(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _pg_config(base_config, enrich=True)
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        outcome = wizard._stage_index(WizardContext(config=cfg))

        assert outcome.status == "failed"
        assert "ANTHROPIC_API_KEY" in outcome.message
        assert outcome.next_step is not None
        assert "--enrich" in outcome.next_step

    def test_done_on_successful_index(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from schemabrain.indexer import IndexResult

        cfg = _pg_config(base_config)
        canned = IndexResult(
            tables_seen=12,
            tables_changed=12,
            tables_unchanged=0,
            tables_removed=0,
            columns_added=80,
            columns_changed=0,
            columns_removed=0,
        )
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")
        monkeypatch.setattr(wizard, "_run_indexer", lambda **_kw: canned)

        outcome = wizard._stage_index(WizardContext(config=cfg))

        assert outcome.status == "done"
        assert "12 tables" in outcome.message
        assert "80 columns" in outcome.message

    def test_done_when_store_exists_but_empty_for_this_source(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Store file exists (e.g. from a different source) but has zero
        # tables for the current `source_id`. The wizard must fall
        # through to the indexer, not short-circuit.
        from schemabrain.indexer import IndexResult

        cfg = _pg_config(base_config)
        cfg.store_path.touch()
        canned = IndexResult(
            tables_seen=4,
            tables_changed=4,
            tables_unchanged=0,
            tables_removed=0,
            columns_added=30,
            columns_changed=0,
            columns_removed=0,
        )
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")
        monkeypatch.setattr(wizard, "_peek_store_table_count", lambda _p, _sid: 0)
        monkeypatch.setattr(wizard, "_run_indexer", lambda **_kw: canned)

        outcome = wizard._stage_index(WizardContext(config=cfg))

        assert outcome.status == "done"
        assert "4 tables" in outcome.message

    def test_done_message_includes_enrichment_cost(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from schemabrain.indexer import IndexResult

        cfg = _pg_config(base_config, enrich=True)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        canned = IndexResult(
            tables_seen=3,
            tables_changed=3,
            tables_unchanged=0,
            tables_removed=0,
            columns_added=20,
            columns_changed=0,
            columns_removed=0,
            descriptions_generated=20,
            llm_cost_usd=0.0123,
        )
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")
        monkeypatch.setattr(wizard, "_run_indexer", lambda **_kw: canned)

        outcome = wizard._stage_index(WizardContext(config=cfg))

        assert outcome.status == "done"
        assert "enriched 20" in outcome.message
        assert "$0.0123" in outcome.message

    def test_failed_on_operational_error(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sqlalchemy.exc import OperationalError

        cfg = _pg_config(base_config)
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")

        def _raise(**_kw: object) -> None:
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))

        monkeypatch.setattr(wizard, "_run_indexer", _raise)

        outcome = wizard._stage_index(WizardContext(config=cfg))

        assert outcome.status == "failed"
        assert "source unreachable" in outcome.message

    def test_failed_on_cost_cap_exceeded(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from schemabrain.enrichment.pipeline import CostCapExceeded

        cfg = _pg_config(base_config, enrich=True)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")

        def _raise(**_kw: object) -> None:
            raise CostCapExceeded(spent=1.50, cap=1.00)

        monkeypatch.setattr(wizard, "_run_indexer", _raise)

        outcome = wizard._stage_index(WizardContext(config=cfg))

        assert outcome.status == "failed"
        assert "spend" in outcome.message.lower() or "cap" in outcome.message.lower()
        assert outcome.next_step is not None
        assert "--max-cost-usd" in outcome.next_step

    def test_failed_on_os_error(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _pg_config(base_config)
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")

        def _raise(**_kw: object) -> None:
            raise OSError("permission denied")

        monkeypatch.setattr(wizard, "_run_indexer", _raise)

        outcome = wizard._stage_index(WizardContext(config=cfg))

        assert outcome.status == "failed"
        assert "store unwritable" in outcome.message


class TestSourceIdFor:
    def test_delegates_to_cli_make_source_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The function is a thin lazy-import wrapper. Stub the cli
        # function and verify the wrapper just returns its output.
        import schemabrain.cli as cli_module

        monkeypatch.setattr(cli_module, "_make_source_id", lambda _url: "deadbeefcafebabe")
        assert wizard._source_id_for("postgresql://u@h/d") == "deadbeefcafebabe"


class TestPeekStoreTableCount:
    def test_returns_count_for_existing_store(self, tmp_path: Path) -> None:
        # Use a real SQLiteStore so we exercise the actual lookup path
        # rather than mocking the indirection away.
        from schemabrain.core.store import SQLiteStore

        store_path = tmp_path / "peek.db"
        with SQLiteStore(store_path) as store:
            # No tables for this source_id yet.
            count = wizard._peek_store_table_count(store_path, "missing_source")
            assert count == 0
            # Sanity: the store opened cleanly.
            assert store.list_tables(source_connection_id="missing_source") == []

    def test_returns_failed_outcome_on_schema_mismatch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from schemabrain.core.store import SchemaVersionMismatchError

        store_path = tmp_path / "mismatch.db"
        store_path.touch()

        def _raise_mismatch(*_a: object, **_kw: object) -> None:
            raise SchemaVersionMismatchError("v10 != v12")

        monkeypatch.setattr("schemabrain.core.store.SQLiteStore.__init__", _raise_mismatch)

        outcome = wizard._peek_store_table_count(store_path, "src")

        assert isinstance(outcome, StageOutcome)
        assert outcome.status == "failed"
        assert "v10" in outcome.message

    def test_returns_failed_outcome_on_os_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # SQLiteStore's `mkdir(parents=True, exist_ok=True)` on the
        # parent can raise OSError on a read-only filesystem. The
        # peek must not let that escape — stage handlers MUST NOT
        # raise.
        store_path = tmp_path / "unreadable.db"
        store_path.touch()

        def _raise_os(*_a: object, **_kw: object) -> None:
            raise OSError("read-only filesystem")

        monkeypatch.setattr("schemabrain.core.store.SQLiteStore.__init__", _raise_os)

        outcome = wizard._peek_store_table_count(store_path, "src")

        assert isinstance(outcome, StageOutcome)
        assert outcome.status == "failed"
        assert "store unreadable" in outcome.message
        assert outcome.stage == 2


class TestRunIndexerSmoke:
    def test_runs_against_sqlite_store_with_postgres_stubs(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Patch the imports inside `_run_indexer` so we exercise the
        # function's wiring without standing up Postgres.
        from schemabrain.indexer import IndexResult

        cfg = _pg_config(base_config)

        canned_result = IndexResult(
            tables_seen=2,
            tables_changed=2,
            tables_unchanged=0,
            tables_removed=0,
            columns_added=10,
            columns_changed=0,
            columns_removed=0,
        )
        captured: dict[str, object] = {}

        class _CtxStub:
            def __init__(self, *_a: object, **_kw: object) -> None:
                pass

            def __enter__(self) -> _CtxStub:
                return self

            def __exit__(self, *_a: object) -> None:
                return None

        def _fake_index(**kwargs: object) -> IndexResult:
            captured.update(kwargs)
            return canned_result

        monkeypatch.setattr("schemabrain.connectors.postgres.PostgresDataSource", _CtxStub)
        monkeypatch.setattr("schemabrain.profiler.postgres.PostgresProfiler", _CtxStub)
        monkeypatch.setattr("schemabrain.core.store.SQLiteStore", _CtxStub)
        monkeypatch.setattr("schemabrain.indexer.index", _fake_index)

        result = wizard._run_indexer(cfg=cfg, source_id="abcd1234", api_key=None)
        assert result is canned_result
        assert captured["source_connection_id"] == "abcd1234"
        assert captured["pipeline"] is None  # enrich=False, no pipeline built
        assert captured["embedder"] is None
        assert captured["no_pii_classify"] is False

    def test_builds_pipeline_when_enrich_and_api_key_present(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from schemabrain.indexer import IndexResult

        cfg = _pg_config(base_config, enrich=True)

        captured: dict[str, object] = {}

        class _CtxStub:
            def __init__(self, *_a: object, **_kw: object) -> None:
                pass

            def __enter__(self) -> _CtxStub:
                return self

            def __exit__(self, *_a: object) -> None:
                return None

        class _PipelineStub:
            def __init__(self, **kwargs: object) -> None:
                captured["pipeline_kwargs"] = kwargs

        def _fake_index(**kwargs: object) -> IndexResult:
            captured.update(kwargs)
            return IndexResult(
                tables_seen=1,
                tables_changed=1,
                tables_unchanged=0,
                tables_removed=0,
                columns_added=5,
                columns_changed=0,
                columns_removed=0,
                descriptions_generated=5,
                llm_cost_usd=0.001,
            )

        monkeypatch.setattr("schemabrain.connectors.postgres.PostgresDataSource", _CtxStub)
        monkeypatch.setattr("schemabrain.profiler.postgres.PostgresProfiler", _CtxStub)
        monkeypatch.setattr("schemabrain.core.store.SQLiteStore", _CtxStub)
        monkeypatch.setattr("schemabrain.enrichment.pipeline.EnrichmentPipeline", _PipelineStub)
        monkeypatch.setattr(
            "schemabrain.enrichment.anthropic_client.anthropic_haiku_45_client",
            lambda **_kw: object(),
        )
        monkeypatch.setattr("schemabrain.enrichment.embeddings.fastembed_default", lambda: object())
        monkeypatch.setattr("schemabrain.indexer.index", _fake_index)

        wizard._run_indexer(cfg=cfg, source_id="abcd1234", api_key="sk-ant-test")
        assert captured["pipeline"] is not None
        assert captured["embedder"] is not None
        # Pipeline received the wizard's bundled enrich cap + concurrency knobs.
        pkw = captured["pipeline_kwargs"]
        assert isinstance(pkw, dict)
        assert pkw["max_cost_usd"] == wizard._WIZARD_INDEX_ENRICH_CAP_USD
        assert pkw["default_concurrency"] == wizard._WIZARD_INDEX_CONCURRENCY
        assert pkw["cryptic_concurrency"] == wizard._WIZARD_INDEX_CRYPTIC_CONCURRENCY


# ----- _stage_entities -----------------------------------------------------


class TestStageEntities:
    def test_skipped_when_no_entities_flag_set(self, base_config: WizardConfig) -> None:
        cfg = _pg_config(base_config, no_entities=True)
        outcome = wizard._stage_entities(WizardContext(config=cfg))

        assert outcome.status == "skipped"
        assert "--no-entities" in outcome.message
        assert "entities suggest" in (outcome.next_step or "")

    def test_skipped_when_skip_index_set(self, base_config: WizardConfig) -> None:
        cfg = _pg_config(base_config, skip_index=True)
        outcome = wizard._stage_entities(WizardContext(config=cfg))

        assert outcome.status == "skipped"
        assert "--skip-index" in outcome.message

    def test_skipped_for_non_postgres_source(self, base_config: WizardConfig) -> None:
        # base_config has sqlite:///:memory:
        outcome = wizard._stage_entities(WizardContext(config=base_config))

        assert outcome.status == "skipped"
        assert "Postgres" in outcome.message

    def test_skipped_when_anthropic_key_missing(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Stage 3 is best-effort — missing key is `skipped`, not `failed`.
        cfg = _pg_config(base_config)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        outcome = wizard._stage_entities(WizardContext(config=cfg))

        assert outcome.status == "skipped"
        assert "ANTHROPIC_API_KEY" in outcome.message

    def test_skipped_when_entities_already_present(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _pg_config(base_config)
        cfg.store_path.touch()
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")
        monkeypatch.setattr(wizard, "_peek_entity_count", lambda _p, _sid: 5)
        monkeypatch.setattr(
            wizard,
            "_run_entity_suggestion",
            lambda **_kw: pytest.fail("pipeline ran despite idempotent skip"),
        )

        outcome = wizard._stage_entities(WizardContext(config=cfg))

        assert outcome.status == "skipped"
        assert "5 entit" in outcome.message  # "5 entity" or "5 entities"

    def test_failed_on_schema_version_mismatch(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _pg_config(base_config)
        cfg.store_path.touch()
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")

        peek_failure = StageOutcome(
            stage=3,
            name="entities",
            status="failed",
            message="store created with schema v10 but installed schemabrain expects v12",
            next_step=f"delete {cfg.store_path} and re-run",
        )
        monkeypatch.setattr(wizard, "_peek_entity_count", lambda _p, _sid: peek_failure)

        outcome = wizard._stage_entities(WizardContext(config=cfg))

        assert outcome.status == "failed"
        assert "v10" in outcome.message

    def test_done_on_successful_apply(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _pg_config(base_config)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")
        canned = wizard._EntityApplyResult(applied_count=8, cost_usd=0.0125, llm_model="sonnet-4.6")
        captured: dict[str, object] = {}

        def _fake_run(**kwargs: object) -> wizard._EntityApplyResult:
            captured.update(kwargs)
            return canned

        monkeypatch.setattr(wizard, "_run_entity_suggestion", _fake_run)

        outcome = wizard._stage_entities(WizardContext(config=cfg))

        assert outcome.status == "done"
        assert "8 entit" in outcome.message
        assert "$0.0125" in outcome.message
        # Confirm the wizard's cost cap was passed through.
        assert isinstance(captured["max_cost_usd"], float)

    def test_runs_pipeline_when_store_exists_but_no_entities(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Store file exists (e.g. from indexing) but zero entities
        # for this source_id — wizard must fall through to the
        # pipeline.
        cfg = _pg_config(base_config)
        cfg.store_path.touch()
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")
        monkeypatch.setattr(wizard, "_peek_entity_count", lambda _p, _sid: 0)
        canned = wizard._EntityApplyResult(applied_count=3, cost_usd=0.005, llm_model="sonnet-4.6")
        monkeypatch.setattr(wizard, "_run_entity_suggestion", lambda **_kw: canned)

        outcome = wizard._stage_entities(WizardContext(config=cfg))

        assert outcome.status == "done"
        assert "3 entit" in outcome.message

    def test_failed_on_cost_ceiling(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _pg_config(base_config)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")

        def _raise(**_kw: object) -> None:
            raise wizard._CostCeilingExceededAtWizard("cost ceiling exceeded")

        monkeypatch.setattr(wizard, "_run_entity_suggestion", _raise)

        outcome = wizard._stage_entities(WizardContext(config=cfg))

        assert outcome.status == "failed"
        assert outcome.next_step is not None
        assert "--entities-max-cost-usd" in outcome.next_step

    def test_failed_on_parse_error(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _pg_config(base_config)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")

        def _raise(**_kw: object) -> None:
            raise wizard._SuggestionParseAtWizard("invalid YAML")

        monkeypatch.setattr(wizard, "_run_entity_suggestion", _raise)

        outcome = wizard._stage_entities(WizardContext(config=cfg))

        assert outcome.status == "failed"
        assert "unparseable" in outcome.message

    def test_skipped_on_empty_schema(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _pg_config(base_config)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")

        def _raise(**_kw: object) -> None:
            raise wizard._EmptySchemaAtWizard()

        monkeypatch.setattr(wizard, "_run_entity_suggestion", _raise)

        outcome = wizard._stage_entities(WizardContext(config=cfg))

        assert outcome.status == "skipped"
        assert "no indexed tables" in outcome.message

    def test_failed_when_pipeline_returns_zero_candidates(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _pg_config(base_config)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")

        empty = wizard._EntityApplyResult(
            applied_count=0,
            cost_usd=0.0021,
            llm_model="x",
            candidates_proposed=0,
        )
        monkeypatch.setattr(wizard, "_run_entity_suggestion", lambda **_kw: empty)

        outcome = wizard._stage_entities(WizardContext(config=cfg))

        assert outcome.status == "failed"
        assert "0 candidates" in outcome.message

    def test_failed_when_all_candidates_rejected(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # All candidates collided with existing entities — applied=0
        # but candidates_proposed > 0. Message must surface that the
        # LLM did produce candidates so the user understands the failure.
        cfg = _pg_config(base_config)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")

        result = wizard._EntityApplyResult(
            applied_count=0,
            cost_usd=0.01,
            llm_model="x",
            candidates_proposed=5,
            skip_reason="DbtOwnedEntityError at 'orders'",
        )
        monkeypatch.setattr(wizard, "_run_entity_suggestion", lambda **_kw: result)

        outcome = wizard._stage_entities(WizardContext(config=cfg))

        assert outcome.status == "failed"
        assert "5 candidates" in outcome.message
        assert "none could be applied" in outcome.message

    def test_partial_apply_surfaces_count_and_reason(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The C-1 silent-failure fix: when writes started failing
        # mid-loop, the stage outcome must report both the partial
        # count and the reason the rest were dropped.
        cfg = _pg_config(base_config)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")

        result = wizard._EntityApplyResult(
            applied_count=3,
            cost_usd=0.008,
            llm_model="sonnet-4.6",
            candidates_proposed=8,
            skip_reason="IntegrityError at 'ghost'",
        )
        monkeypatch.setattr(wizard, "_run_entity_suggestion", lambda **_kw: result)

        outcome = wizard._stage_entities(WizardContext(config=cfg))

        assert outcome.status == "done"
        assert "3 of 8" in outcome.message
        assert "5 skipped" in outcome.message
        assert "IntegrityError at 'ghost'" in outcome.message
        assert outcome.next_step is not None

    def test_partial_apply_with_no_skip_reason(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Defensive: applied < proposed with no skip_reason recorded
        # still surfaces partial state (loop ran out cleanly somehow).
        cfg = _pg_config(base_config)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")

        result = wizard._EntityApplyResult(
            applied_count=2,
            cost_usd=0.003,
            llm_model="x",
            candidates_proposed=4,
            skip_reason=None,
        )
        monkeypatch.setattr(wizard, "_run_entity_suggestion", lambda **_kw: result)

        outcome = wizard._stage_entities(WizardContext(config=cfg))

        assert outcome.status == "done"
        assert "2 of 4" in outcome.message
        # No skip_reason → no suffix in parentheses beyond cost.
        assert "skipped:" not in outcome.message

    def test_singular_entity_pluralization(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `applied_count == 1` produces "1 entity" not "1 entities".
        cfg = _pg_config(base_config)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")

        result = wizard._EntityApplyResult(
            applied_count=1,
            cost_usd=0.001,
            llm_model="x",
            candidates_proposed=1,
        )
        monkeypatch.setattr(wizard, "_run_entity_suggestion", lambda **_kw: result)

        outcome = wizard._stage_entities(WizardContext(config=cfg))

        assert outcome.status == "done"
        assert "1 entity created" in outcome.message


class TestStageEntitiesLlmConfirmation:
    """Pre-LLM confirmation pause PR: `_stage_entities` pauses for
    user confirmation in interactive sessions before calling the
    LLM. Tests cover the three branch outcomes:

      1. `cfg.skip_llm_confirm=True` → prompt fully bypassed
      2. TTY + user proceeds → LLM call happens (regression test for
         existing behaviour)
      3. TTY + user cancels → outcome is `skipped` with "user
         cancelled the LLM call" message

    The dbt branch sits BEFORE the api-key check (and BEFORE the
    confirmation prompt), so dbt mode does NOT trigger the prompt —
    verified in a separate test.
    """

    def test_skip_llm_confirm_bypasses_prompt(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _pg_config(base_config, skip_llm_confirm=True)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")
        canned = wizard._EntityApplyResult(applied_count=3, cost_usd=0.005, llm_model="sonnet-4.6")
        monkeypatch.setattr(wizard, "_run_entity_suggestion", lambda **_kw: canned)

        # Force-isatty to ensure the prompt-helper would normally
        # fire — `skip_llm_confirm=True` must short-circuit ahead of
        # it. If the bypass leaks, this test would hang on input().
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

        outcome = wizard._stage_entities(WizardContext(config=cfg))

        assert outcome.status == "done"
        assert "3 entities created" in outcome.message

    def test_prompt_fires_in_tty_and_proceeds_on_enter(
        self,
        base_config: WizardConfig,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Default `skip_llm_confirm=False`. TTY + user-pressed-Enter
        # → LLM call happens.
        cfg = _pg_config(base_config)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")
        canned = wizard._EntityApplyResult(applied_count=2, cost_usd=0.003, llm_model="sonnet-4.6")
        monkeypatch.setattr(wizard, "_run_entity_suggestion", lambda **_kw: canned)
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda: "")

        outcome = wizard._stage_entities(WizardContext(config=cfg))

        assert outcome.status == "done"
        assert "2 entities created" in outcome.message
        # The prompt actually rendered.
        captured = capsys.readouterr()
        assert "Anthropic" in captured.err
        assert "entities" in captured.err

    def test_user_cancels_with_ctrl_c(
        self,
        base_config: WizardConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg = _pg_config(base_config)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")
        monkeypatch.setattr(
            wizard,
            "_run_entity_suggestion",
            lambda **_kw: pytest.fail("LLM call happened despite user cancelling"),
        )
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

        def _raise_ki() -> str:
            raise KeyboardInterrupt

        monkeypatch.setattr("builtins.input", _raise_ki)

        outcome = wizard._stage_entities(WizardContext(config=cfg))

        assert outcome.status == "skipped"
        assert outcome.stage == 3
        assert outcome.name == "entities"
        assert "user cancelled" in outcome.message
        assert outcome.next_step is not None
        assert "--yes" in outcome.next_step

    def test_non_tty_auto_bypass_keeps_llm_path_running(
        self,
        base_config: WizardConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The default pytest stdin is non-TTY, so the prompt helper
        # returns True without reading input. The LLM call should
        # still happen. This is the contract that keeps every
        # pre-existing test green.
        cfg = _pg_config(base_config)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")
        canned = wizard._EntityApplyResult(applied_count=4, cost_usd=0.01, llm_model="sonnet-4.6")
        monkeypatch.setattr(wizard, "_run_entity_suggestion", lambda **_kw: canned)
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        monkeypatch.setattr(
            "builtins.input",
            lambda: pytest.fail("input() should not be called in non-TTY mode"),
        )

        outcome = wizard._stage_entities(WizardContext(config=cfg))

        assert outcome.status == "done"
        assert "4 entities created" in outcome.message

    def test_dbt_branch_does_not_trigger_prompt(
        self,
        base_config: WizardConfig,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # dbt path sits BEFORE the api-key check and BEFORE the
        # confirmation prompt. With `ctx.dbt_manifest_path` set, the
        # prompt must not fire (no LLM call → no consent needed).
        cfg = _pg_config(base_config)
        cfg.store_path.touch()
        ctx = WizardContext(config=cfg)
        ctx.dbt_manifest_path = tmp_path / "manifest.json"
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")
        canned = wizard._EntityApplyResult(
            applied_count=5,
            cost_usd=0.0,
            llm_model="dbt-import",
            candidates_proposed=5,
            source="dbt",
        )
        monkeypatch.setattr(wizard, "_run_entities_from_dbt", lambda **_kw: canned)
        # Force TTY + sentinel-input: would hang or fail if the
        # prompt fired on the dbt branch.
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr(
            "builtins.input",
            lambda: pytest.fail("prompt fired on the dbt branch"),
        )

        outcome = wizard._stage_entities(ctx)

        assert outcome.status == "done"
        assert "5 entities imported from dbt" in outcome.message


class TestStageEntitiesDbtBranch:
    """PR C: stage 3 routes through `_run_entities_from_dbt` when
    `ctx.dbt_manifest_path` is set.

    The dbt branch sits BEFORE the API-key check, so these tests
    intentionally do NOT set `ANTHROPIC_API_KEY` to confirm the
    dbt path is reached without an API key.
    """

    def _ctx_with_dbt(
        self,
        base_config: WizardConfig,
        manifest_path: Path,
    ) -> WizardContext:
        cfg = _pg_config(base_config)
        cfg.store_path.touch()
        ctx = WizardContext(config=cfg)
        ctx.dbt_manifest_path = manifest_path
        return ctx

    def test_done_on_successful_dbt_import(
        self,
        base_config: WizardConfig,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ctx = self._ctx_with_dbt(base_config, tmp_path / "manifest.json")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")

        canned = wizard._EntityApplyResult(
            applied_count=8,
            cost_usd=0.0,
            llm_model="dbt-import",
            candidates_proposed=8,
            skip_reason=None,
            source="dbt",
        )
        captured: dict[str, object] = {}

        def _fake_run(**kwargs: object) -> wizard._EntityApplyResult:
            captured.update(kwargs)
            return canned

        monkeypatch.setattr(wizard, "_run_entities_from_dbt", _fake_run)

        outcome = wizard._stage_entities(ctx)

        assert outcome.status == "done"
        assert "8 entities imported from dbt" in outcome.message
        # No cost suffix on dbt path.
        assert "cost" not in outcome.message
        # Confirm the manifest path was threaded through.
        assert captured["manifest_path"] == tmp_path / "manifest.json"

    def test_done_singular_entity_uses_y_suffix(
        self,
        base_config: WizardConfig,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ctx = self._ctx_with_dbt(base_config, tmp_path / "manifest.json")
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")
        canned = wizard._EntityApplyResult(
            applied_count=1,
            cost_usd=0.0,
            llm_model="dbt-import",
            candidates_proposed=1,
            source="dbt",
        )
        monkeypatch.setattr(wizard, "_run_entities_from_dbt", lambda **_kw: canned)

        outcome = wizard._stage_entities(ctx)

        assert outcome.status == "done"
        assert "1 entity imported from dbt" in outcome.message

    def test_partial_success_reports_skip_count_and_reason(
        self,
        base_config: WizardConfig,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ctx = self._ctx_with_dbt(base_config, tmp_path / "manifest.json")
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")
        partial = wizard._EntityApplyResult(
            applied_count=5,
            cost_usd=0.0,
            llm_model="dbt-import",
            candidates_proposed=7,
            skip_reason="ghost_model: bound table not in store",
            source="dbt",
        )
        monkeypatch.setattr(wizard, "_run_entities_from_dbt", lambda **_kw: partial)

        outcome = wizard._stage_entities(ctx)

        assert outcome.status == "done"
        assert "5 of 7 entities imported from dbt" in outcome.message
        assert "2 skipped" in outcome.message
        assert "ghost_model" in outcome.message
        assert outcome.next_step is not None
        assert "audit log" in outcome.next_step

    def test_failed_when_zero_applied_with_candidates_proposed(
        self,
        base_config: WizardConfig,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ctx = self._ctx_with_dbt(base_config, tmp_path / "manifest.json")
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")
        result = wizard._EntityApplyResult(
            applied_count=0,
            cost_usd=0.0,
            llm_model="dbt-import",
            candidates_proposed=4,
            source="dbt",
        )
        monkeypatch.setattr(wizard, "_run_entities_from_dbt", lambda **_kw: result)

        outcome = wizard._stage_entities(ctx)

        assert outcome.status == "failed"
        assert "planned 4 entities" in outcome.message
        assert "none could be written" in outcome.message
        assert outcome.next_step is not None
        assert "omit --from-dbt" in outcome.next_step

    def test_failed_when_zero_candidates_proposed(
        self,
        base_config: WizardConfig,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ctx = self._ctx_with_dbt(base_config, tmp_path / "manifest.json")
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")
        result = wizard._EntityApplyResult(
            applied_count=0,
            cost_usd=0.0,
            llm_model="dbt-import",
            candidates_proposed=0,
            source="dbt",
        )
        monkeypatch.setattr(wizard, "_run_entities_from_dbt", lambda **_kw: result)

        outcome = wizard._stage_entities(ctx)

        assert outcome.status == "failed"
        assert "0 importable models" in outcome.message

    def test_failed_when_dbt_import_raises_helper_exception(
        self,
        base_config: WizardConfig,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ctx = self._ctx_with_dbt(base_config, tmp_path / "manifest.json")
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")

        def _raise(**_kw: object) -> None:
            raise wizard._DbtImportFailedAtWizard(
                "dbt manifest unreadable: missing file",
                next_step="run `dbt compile` then re-run",
            )

        monkeypatch.setattr(wizard, "_run_entities_from_dbt", _raise)

        outcome = wizard._stage_entities(ctx)

        assert outcome.status == "failed"
        assert "manifest unreadable" in outcome.message
        assert outcome.next_step == "run `dbt compile` then re-run"

    def test_dbt_branch_bypassed_when_already_curated(
        self,
        base_config: WizardConfig,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Already-curated check runs BEFORE the dbt branch. An
        # idempotent re-run on a store with entities short-circuits
        # whether or not dbt mode is active.
        ctx = self._ctx_with_dbt(base_config, tmp_path / "manifest.json")
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")
        monkeypatch.setattr(wizard, "_peek_entity_count", lambda _p, _sid: 5)
        monkeypatch.setattr(
            wizard,
            "_run_entities_from_dbt",
            lambda **_kw: pytest.fail("dbt path ran despite already-curated"),
        )

        outcome = wizard._stage_entities(ctx)

        assert outcome.status == "skipped"
        assert "already curated" in outcome.message


class TestResolveEntitiesCostCap:
    def test_explicit_flag_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SCHEMABRAIN_MAX_LLM_COST_USD", "5.0")
        assert wizard._resolve_entities_cost_cap(0.25) == 0.25

    def test_env_used_when_flag_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SCHEMABRAIN_MAX_LLM_COST_USD", "0.75")
        assert wizard._resolve_entities_cost_cap(None) == 0.75

    def test_default_used_when_env_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SCHEMABRAIN_MAX_LLM_COST_USD", raising=False)
        assert (
            wizard._resolve_entities_cost_cap(None) == wizard._WIZARD_ENTITIES_DEFAULT_COST_CAP_USD
        )

    def test_malformed_env_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SCHEMABRAIN_MAX_LLM_COST_USD", "not-a-float")
        assert (
            wizard._resolve_entities_cost_cap(None) == wizard._WIZARD_ENTITIES_DEFAULT_COST_CAP_USD
        )

    def test_malformed_env_emits_stderr_warning(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("SCHEMABRAIN_MAX_LLM_COST_USD", "junk")
        wizard._resolve_entities_cost_cap(None)
        captured = capsys.readouterr()
        assert "SCHEMABRAIN_MAX_LLM_COST_USD" in captured.err
        assert "not a valid number" in captured.err

    def test_zero_env_falls_back_to_default_with_warning(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("SCHEMABRAIN_MAX_LLM_COST_USD", "0")
        result = wizard._resolve_entities_cost_cap(None)
        assert result == wizard._WIZARD_ENTITIES_DEFAULT_COST_CAP_USD
        captured = capsys.readouterr()
        assert "must be positive" in captured.err

    def test_negative_env_falls_back_to_default_with_warning(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("SCHEMABRAIN_MAX_LLM_COST_USD", "-0.5")
        result = wizard._resolve_entities_cost_cap(None)
        assert result == wizard._WIZARD_ENTITIES_DEFAULT_COST_CAP_USD
        captured = capsys.readouterr()
        assert "must be positive" in captured.err


class TestPeekEntityCount:
    def test_returns_zero_for_fresh_store(self, tmp_path: Path) -> None:
        from schemabrain.core.store import SQLiteStore

        store_path = tmp_path / "peek_entity.db"
        # Touch the store via context manager so it exists with schema v12.
        with SQLiteStore(store_path):
            pass

        assert wizard._peek_entity_count(store_path, "missing_source") == 0

    def test_returns_failed_outcome_on_schema_mismatch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from schemabrain.core.store import SchemaVersionMismatchError

        store_path = tmp_path / "mismatch_entity.db"
        store_path.touch()

        def _raise_mismatch(*_a: object, **_kw: object) -> None:
            raise SchemaVersionMismatchError("v10 != v12")

        monkeypatch.setattr("schemabrain.core.store.SQLiteStore.__init__", _raise_mismatch)

        outcome = wizard._peek_entity_count(store_path, "src")

        assert isinstance(outcome, StageOutcome)
        assert outcome.status == "failed"
        assert outcome.stage == 3

    def test_returns_failed_outcome_on_os_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store_path = tmp_path / "unreadable_entity.db"
        store_path.touch()

        def _raise_os(*_a: object, **_kw: object) -> None:
            raise OSError("read-only filesystem")

        monkeypatch.setattr("schemabrain.core.store.SQLiteStore.__init__", _raise_os)

        outcome = wizard._peek_entity_count(store_path, "src")

        assert isinstance(outcome, StageOutcome)
        assert outcome.status == "failed"
        assert "store unreadable" in outcome.message
        assert outcome.stage == 3


class TestRunEntitySuggestionSmoke:
    def test_pipeline_invoked_and_writes_applied(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Substitute every external dependency so we can verify the
        # function's wiring without standing up Anthropic or Postgres.
        from schemabrain.entities.suggest import SuggestionResult

        cfg = _pg_config(base_config)

        # Build a fake suggestion result with two candidates.
        class _FakeEntity:
            def __init__(self, name: str) -> None:
                self.name = name

        class _FakeCandidate:
            def __init__(self, name: str) -> None:
                self.entity = _FakeEntity(name=name)

        canned = SuggestionResult(
            candidates=(_FakeCandidate("orders"), _FakeCandidate("users")),  # type: ignore[arg-type]
            total_cost_usd=0.0123,
            llm_model="sonnet-4.6",
        )

        applied_writes: list[str] = []

        class _FakeStore:
            def __init__(self, *_a: object, **_kw: object) -> None:
                pass

            def __enter__(self) -> _FakeStore:
                return self

            def __exit__(self, *_a: object) -> None:
                return None

            def list_tables(self, *, source_connection_id: str) -> list[tuple[str, str]]:
                return [("public", "orders"), ("public", "users")]

            def get_table(self, _schema: str, name: str, *, source_connection_id: str) -> object:
                return object()

            def write_entity(self, entity: object, *, source_connection_id: str) -> None:
                applied_writes.append(entity.name)  # type: ignore[attr-defined]

        class _FakePipeline:
            def __init__(self, *, llm: object) -> None:
                pass

            def propose_from_tables(self, _tables: object) -> SuggestionResult:
                return canned

        class _FakeGuard:
            def __init__(self, *, inner: object, max_cost_usd: float) -> None:
                pass

        monkeypatch.setattr("schemabrain.core.store.SQLiteStore", _FakeStore)
        monkeypatch.setattr("schemabrain.entities.suggest.EntitySuggestionPipeline", _FakePipeline)
        monkeypatch.setattr("schemabrain.entities.suggest.CostCeilingGuard", _FakeGuard)
        monkeypatch.setattr(
            "schemabrain.enrichment.anthropic_client.anthropic_sonnet_46_client",
            lambda *, api_key: object(),
        )

        result = wizard._run_entity_suggestion(
            cfg=cfg, source_id="abcd1234", api_key="sk-ant-test", max_cost_usd=0.5
        )

        assert result.applied_count == 2
        assert result.cost_usd == 0.0123
        assert result.llm_model == "sonnet-4.6"
        assert applied_writes == ["orders", "users"]

    def test_raises_empty_schema_when_no_tables(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _pg_config(base_config)

        class _FakeStore:
            def __init__(self, *_a: object, **_kw: object) -> None:
                pass

            def __enter__(self) -> _FakeStore:
                return self

            def __exit__(self, *_a: object) -> None:
                return None

            def list_tables(self, *, source_connection_id: str) -> list[tuple[str, str]]:
                return []

        monkeypatch.setattr("schemabrain.core.store.SQLiteStore", _FakeStore)
        monkeypatch.setattr(
            "schemabrain.enrichment.anthropic_client.anthropic_sonnet_46_client",
            lambda *, api_key: object(),
        )

        with pytest.raises(wizard._EmptySchemaAtWizard):
            wizard._run_entity_suggestion(
                cfg=cfg, source_id="abcd1234", api_key="sk-ant-test", max_cost_usd=0.5
            )

    def test_raises_empty_schema_when_tables_unloadable(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `list_tables` returns names but `get_table` returns None for
        # each (the cache row exists but the table fingerprints are
        # missing). The function should treat this as empty-schema.
        cfg = _pg_config(base_config)

        class _FakeStore:
            def __init__(self, *_a: object, **_kw: object) -> None:
                pass

            def __enter__(self) -> _FakeStore:
                return self

            def __exit__(self, *_a: object) -> None:
                return None

            def list_tables(self, *, source_connection_id: str) -> list[tuple[str, str]]:
                return [("public", "ghost")]

            def get_table(self, *_a: object, **_kw: object) -> None:
                return None

        monkeypatch.setattr("schemabrain.core.store.SQLiteStore", _FakeStore)
        monkeypatch.setattr(
            "schemabrain.enrichment.anthropic_client.anthropic_sonnet_46_client",
            lambda *, api_key: object(),
        )

        with pytest.raises(wizard._EmptySchemaAtWizard):
            wizard._run_entity_suggestion(
                cfg=cfg, source_id="abcd1234", api_key="sk-ant-test", max_cost_usd=0.5
            )

    def test_translates_cost_ceiling_error(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from schemabrain.entities.suggest import CostCeilingExceededError

        cfg = _pg_config(base_config)

        class _FakeStore:
            def __init__(self, *_a: object, **_kw: object) -> None:
                pass

            def __enter__(self) -> _FakeStore:
                return self

            def __exit__(self, *_a: object) -> None:
                return None

            def list_tables(self, *, source_connection_id: str) -> list[tuple[str, str]]:
                return [("public", "t")]

            def get_table(self, *_a: object, **_kw: object) -> object:
                return object()

        class _RaisingPipeline:
            def __init__(self, *, llm: object) -> None:
                pass

            def propose_from_tables(self, _tables: object) -> object:
                raise CostCeilingExceededError(
                    cumulative_cost_usd=0.5,
                    next_call_estimate_usd=0.6,
                    max_cost_usd=1.0,
                )

        class _FakeGuard:
            def __init__(self, **_kw: object) -> None:
                pass

        monkeypatch.setattr("schemabrain.core.store.SQLiteStore", _FakeStore)
        monkeypatch.setattr(
            "schemabrain.entities.suggest.EntitySuggestionPipeline", _RaisingPipeline
        )
        monkeypatch.setattr("schemabrain.entities.suggest.CostCeilingGuard", _FakeGuard)
        monkeypatch.setattr(
            "schemabrain.enrichment.anthropic_client.anthropic_sonnet_46_client",
            lambda *, api_key: object(),
        )

        with pytest.raises(wizard._CostCeilingExceededAtWizard):
            wizard._run_entity_suggestion(
                cfg=cfg, source_id="abcd1234", api_key="sk-ant-test", max_cost_usd=1.0
            )

    def test_translates_parse_error(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from schemabrain.entities.suggest import SuggestionParseError

        cfg = _pg_config(base_config)

        class _FakeStore:
            def __init__(self, *_a: object, **_kw: object) -> None:
                pass

            def __enter__(self) -> _FakeStore:
                return self

            def __exit__(self, *_a: object) -> None:
                return None

            def list_tables(self, *, source_connection_id: str) -> list[tuple[str, str]]:
                return [("public", "t")]

            def get_table(self, *_a: object, **_kw: object) -> object:
                return object()

        class _RaisingPipeline:
            def __init__(self, *, llm: object) -> None:
                pass

            def propose_from_tables(self, _tables: object) -> object:
                raise SuggestionParseError("bad YAML")

        class _FakeGuard:
            def __init__(self, **_kw: object) -> None:
                pass

        monkeypatch.setattr("schemabrain.core.store.SQLiteStore", _FakeStore)
        monkeypatch.setattr(
            "schemabrain.entities.suggest.EntitySuggestionPipeline", _RaisingPipeline
        )
        monkeypatch.setattr("schemabrain.entities.suggest.CostCeilingGuard", _FakeGuard)
        monkeypatch.setattr(
            "schemabrain.enrichment.anthropic_client.anthropic_sonnet_46_client",
            lambda *, api_key: object(),
        )

        with pytest.raises(wizard._SuggestionParseAtWizard):
            wizard._run_entity_suggestion(
                cfg=cfg, source_id="abcd1234", api_key="sk-ant-test", max_cost_usd=1.0
            )

    def test_partial_apply_breaks_on_integrity_error(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Verify the `except (DbtOwnedEntityError, IntegrityError): break`
        # path: write candidate 0 succeeds, candidate 1 raises, applied=1.
        from sqlite3 import IntegrityError

        from schemabrain.entities.suggest import SuggestionResult

        cfg = _pg_config(base_config)

        class _FakeEntity:
            def __init__(self, name: str) -> None:
                self.name = name

        class _FakeCandidate:
            def __init__(self, name: str) -> None:
                self.entity = _FakeEntity(name=name)

        canned = SuggestionResult(
            candidates=(_FakeCandidate("orders"), _FakeCandidate("ghost")),  # type: ignore[arg-type]
            total_cost_usd=0.01,
            llm_model="sonnet-4.6",
        )

        applied: list[str] = []

        class _FakeStore:
            def __init__(self, *_a: object, **_kw: object) -> None:
                pass

            def __enter__(self) -> _FakeStore:
                return self

            def __exit__(self, *_a: object) -> None:
                return None

            def list_tables(self, *, source_connection_id: str) -> list[tuple[str, str]]:
                return [("public", "orders"), ("public", "ghost")]

            def get_table(self, *_a: object, **_kw: object) -> object:
                return object()

            def write_entity(self, entity: object, *, source_connection_id: str) -> None:
                if entity.name == "ghost":  # type: ignore[attr-defined]
                    raise IntegrityError("table not in store")
                applied.append(entity.name)  # type: ignore[attr-defined]

        class _FakePipeline:
            def __init__(self, *, llm: object) -> None:
                pass

            def propose_from_tables(self, _tables: object) -> SuggestionResult:
                return canned

        class _FakeGuard:
            def __init__(self, **_kw: object) -> None:
                pass

        monkeypatch.setattr("schemabrain.core.store.SQLiteStore", _FakeStore)
        monkeypatch.setattr("schemabrain.entities.suggest.EntitySuggestionPipeline", _FakePipeline)
        monkeypatch.setattr("schemabrain.entities.suggest.CostCeilingGuard", _FakeGuard)
        monkeypatch.setattr(
            "schemabrain.enrichment.anthropic_client.anthropic_sonnet_46_client",
            lambda *, api_key: object(),
        )

        result = wizard._run_entity_suggestion(
            cfg=cfg, source_id="abcd1234", api_key="sk-ant-test", max_cost_usd=1.0
        )

        assert result.applied_count == 1
        assert applied == ["orders"]


# ----- _stage_metrics ------------------------------------------------------


class TestStageMetrics:
    """Tests for `_stage_metrics`.

    Mirror of `TestStageEntities`. The metrics stage adds one branch
    on top of the entity tree: an empty entity store skips the stage
    with a pointer at entity curation first. Tests cover all six
    skip branches + the four LLM-result branches (done /
    cost-ceiling / parse-error / empty-schema / zero-candidates /
    partial-success).
    """

    def test_skipped_when_no_metrics_flag_set(self, base_config: WizardConfig) -> None:
        cfg = _pg_config(base_config, no_metrics=True)
        outcome = wizard._stage_metrics(WizardContext(config=cfg))

        assert outcome.status == "skipped"
        assert outcome.stage == 4
        assert outcome.name == "metrics"
        assert "--no-metrics" in outcome.message
        assert "metrics suggest" in (outcome.next_step or "")

    def test_skipped_when_skip_index_set(self, base_config: WizardConfig) -> None:
        cfg = _pg_config(base_config, skip_index=True)
        outcome = wizard._stage_metrics(WizardContext(config=cfg))

        assert outcome.status == "skipped"
        assert "--skip-index" in outcome.message

    def test_skipped_for_non_postgres_source(self, base_config: WizardConfig) -> None:
        # base_config has sqlite:///:memory:
        outcome = wizard._stage_metrics(WizardContext(config=base_config))

        assert outcome.status == "skipped"
        assert "Postgres" in outcome.message

    def test_skipped_when_anthropic_key_missing(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Stage 4 is best-effort — missing key is `skipped`, not `failed`.
        cfg = _pg_config(base_config)
        cfg.store_path.touch()
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")
        # Pretend the store has zero metrics AND one entity so we
        # reach the API-key check (the empty-entity guard would short-
        # circuit earlier otherwise).
        monkeypatch.setattr(wizard, "_peek_metric_count", lambda _p, _sid: 0)
        monkeypatch.setattr(wizard, "_peek_entity_count", lambda _p, _sid: 3)

        outcome = wizard._stage_metrics(WizardContext(config=cfg))

        assert outcome.status == "skipped"
        assert "ANTHROPIC_API_KEY" in outcome.message

    def test_skipped_when_metrics_already_present(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _pg_config(base_config)
        cfg.store_path.touch()
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")
        monkeypatch.setattr(wizard, "_peek_metric_count", lambda _p, _sid: 7)
        monkeypatch.setattr(
            wizard,
            "_run_metric_suggestion",
            lambda **_kw: pytest.fail("pipeline ran despite idempotent skip"),
        )

        outcome = wizard._stage_metrics(WizardContext(config=cfg))

        assert outcome.status == "skipped"
        assert "7 metric" in outcome.message

    def test_skipped_when_entity_store_empty(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The cross-stage dependency PR A introduces: metrics anchor
        # on entities, and an empty entity store means the pipeline
        # has nothing to anchor on. The stage must short-circuit
        # cleanly with a pointer at entity curation first — instead
        # of letting the pipeline raise `ValueError`.
        cfg = _pg_config(base_config)
        cfg.store_path.touch()
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")
        monkeypatch.setattr(wizard, "_peek_metric_count", lambda _p, _sid: 0)
        monkeypatch.setattr(wizard, "_peek_entity_count", lambda _p, _sid: 0)
        monkeypatch.setattr(
            wizard,
            "_run_metric_suggestion",
            lambda **_kw: pytest.fail("pipeline ran despite empty entity store"),
        )

        outcome = wizard._stage_metrics(WizardContext(config=cfg))

        assert outcome.status == "skipped"
        assert "entity store is empty" in outcome.message
        assert outcome.next_step is not None
        assert "schemabrain entities suggest --apply" in outcome.next_step

    def test_skipped_when_store_file_missing(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No store file at all → treat as empty entity store. Stage 2
        # was skipped AND no prior session indexed; there are no
        # entities to anchor on.
        cfg = _pg_config(base_config)
        # Do NOT touch the store path — it should not exist.
        assert not cfg.store_path.exists()
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")

        outcome = wizard._stage_metrics(WizardContext(config=cfg))

        assert outcome.status == "skipped"
        assert "entity store is empty" in outcome.message

    def test_failed_on_schema_version_mismatch(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _pg_config(base_config)
        cfg.store_path.touch()
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")

        peek_failure = StageOutcome(
            stage=4,
            name="metrics",
            status="failed",
            message="store created with schema v10 but installed schemabrain expects v12",
            next_step=f"delete {cfg.store_path} and re-run",
        )
        monkeypatch.setattr(wizard, "_peek_metric_count", lambda _p, _sid: peek_failure)

        outcome = wizard._stage_metrics(WizardContext(config=cfg))

        assert outcome.status == "failed"
        assert "v10" in outcome.message

    def test_failed_when_entity_peek_returns_outcome(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `_peek_entity_count` may return a StageOutcome on a schema
        # mismatch or filesystem error. The metrics stage must
        # translate that stage-3-shaped outcome into a stage-4-shaped
        # one so the renderer routes the line correctly.
        cfg = _pg_config(base_config)
        cfg.store_path.touch()
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")
        monkeypatch.setattr(wizard, "_peek_metric_count", lambda _p, _sid: 0)

        entity_peek_failure = StageOutcome(
            stage=3,
            name="entities",
            status="failed",
            message="entity-side schema mismatch",
            next_step=f"delete {cfg.store_path} and re-run",
        )
        monkeypatch.setattr(wizard, "_peek_entity_count", lambda _p, _sid: entity_peek_failure)

        outcome = wizard._stage_metrics(WizardContext(config=cfg))

        assert outcome.status == "failed"
        assert outcome.stage == 4
        assert outcome.name == "metrics"
        assert "entity-side schema mismatch" in outcome.message

    def test_done_on_successful_apply(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _pg_config(base_config)
        cfg.store_path.touch()
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")
        monkeypatch.setattr(wizard, "_peek_metric_count", lambda _p, _sid: 0)
        monkeypatch.setattr(wizard, "_peek_entity_count", lambda _p, _sid: 5)
        canned = wizard._MetricApplyResult(applied_count=6, cost_usd=0.0080, llm_model="sonnet-4.6")
        captured: dict[str, object] = {}

        def _fake_run(**kwargs: object) -> wizard._MetricApplyResult:
            captured.update(kwargs)
            return canned

        monkeypatch.setattr(wizard, "_run_metric_suggestion", _fake_run)

        outcome = wizard._stage_metrics(WizardContext(config=cfg))

        assert outcome.status == "done"
        assert "6 metric" in outcome.message
        assert "$0.0080" in outcome.message
        # Confirm the wizard's cost cap was passed through.
        assert isinstance(captured["max_cost_usd"], float)

    def test_failed_on_cost_ceiling(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _pg_config(base_config)
        cfg.store_path.touch()
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")
        monkeypatch.setattr(wizard, "_peek_metric_count", lambda _p, _sid: 0)
        monkeypatch.setattr(wizard, "_peek_entity_count", lambda _p, _sid: 5)

        def _raise(**_kw: object) -> None:
            raise wizard._CostCeilingExceededAtWizard("cost ceiling exceeded")

        monkeypatch.setattr(wizard, "_run_metric_suggestion", _raise)

        outcome = wizard._stage_metrics(WizardContext(config=cfg))

        assert outcome.status == "failed"
        assert outcome.next_step is not None
        assert "--metrics-max-cost-usd" in outcome.next_step

    def test_failed_on_parse_error(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _pg_config(base_config)
        cfg.store_path.touch()
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")
        monkeypatch.setattr(wizard, "_peek_metric_count", lambda _p, _sid: 0)
        monkeypatch.setattr(wizard, "_peek_entity_count", lambda _p, _sid: 5)

        def _raise(**_kw: object) -> None:
            raise wizard._SuggestionParseAtWizard("invalid YAML")

        monkeypatch.setattr(wizard, "_run_metric_suggestion", _raise)

        outcome = wizard._stage_metrics(WizardContext(config=cfg))

        assert outcome.status == "failed"
        assert "unparseable" in outcome.message

    def test_skipped_on_empty_schema(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _pg_config(base_config)
        cfg.store_path.touch()
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")
        monkeypatch.setattr(wizard, "_peek_metric_count", lambda _p, _sid: 0)
        monkeypatch.setattr(wizard, "_peek_entity_count", lambda _p, _sid: 5)

        def _raise(**_kw: object) -> None:
            raise wizard._EmptySchemaAtWizard()

        monkeypatch.setattr(wizard, "_run_metric_suggestion", _raise)

        outcome = wizard._stage_metrics(WizardContext(config=cfg))

        assert outcome.status == "skipped"
        assert "no indexed tables" in outcome.message

    def test_failed_when_pipeline_returns_zero_candidates(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _pg_config(base_config)
        cfg.store_path.touch()
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")
        monkeypatch.setattr(wizard, "_peek_metric_count", lambda _p, _sid: 0)
        monkeypatch.setattr(wizard, "_peek_entity_count", lambda _p, _sid: 5)

        empty = wizard._MetricApplyResult(
            applied_count=0,
            cost_usd=0.0021,
            llm_model="x",
            candidates_proposed=0,
        )
        monkeypatch.setattr(wizard, "_run_metric_suggestion", lambda **_kw: empty)

        outcome = wizard._stage_metrics(WizardContext(config=cfg))

        assert outcome.status == "failed"
        assert "0 candidates" in outcome.message

    def test_failed_when_all_candidates_rejected(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # All candidates collided with dbt-imported metrics — applied=0
        # but candidates_proposed > 0.
        cfg = _pg_config(base_config)
        cfg.store_path.touch()
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")
        monkeypatch.setattr(wizard, "_peek_metric_count", lambda _p, _sid: 0)
        monkeypatch.setattr(wizard, "_peek_entity_count", lambda _p, _sid: 5)

        rejected = wizard._MetricApplyResult(
            applied_count=0,
            cost_usd=0.0050,
            llm_model="x",
            candidates_proposed=4,
            skip_reason="DbtOwnedMetricError at 'revenue'",
        )
        monkeypatch.setattr(wizard, "_run_metric_suggestion", lambda **_kw: rejected)

        outcome = wizard._stage_metrics(WizardContext(config=cfg))

        assert outcome.status == "failed"
        assert "4 candidates" in outcome.message
        assert "none could be applied" in outcome.message

    def test_partial_success_reports_skipped_count(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Some metrics applied, others collided mid-loop. Surface the
        # count + the skip reason so the user can act on the partial.
        cfg = _pg_config(base_config)
        cfg.store_path.touch()
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")
        monkeypatch.setattr(wizard, "_peek_metric_count", lambda _p, _sid: 0)
        monkeypatch.setattr(wizard, "_peek_entity_count", lambda _p, _sid: 5)

        partial = wizard._MetricApplyResult(
            applied_count=3,
            cost_usd=0.0150,
            llm_model="x",
            candidates_proposed=5,
            skip_reason="DbtOwnedMetricError at 'aov'",
        )
        monkeypatch.setattr(wizard, "_run_metric_suggestion", lambda **_kw: partial)

        outcome = wizard._stage_metrics(WizardContext(config=cfg))

        assert outcome.status == "done"
        assert "3 of 5" in outcome.message
        assert "DbtOwnedMetricError at 'aov'" in outcome.message
        assert outcome.next_step is not None
        assert "metrics suggest --dry-run" in outcome.next_step


class TestStageMetricsLlmConfirmation:
    """Pre-LLM confirmation pause PR: `_stage_metrics` parallels
    `_stage_entities`. Same three branches: skip_llm_confirm bypass,
    TTY+Enter proceeds, TTY+Ctrl-C cancels.
    """

    def _ready_cfg(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch, **overrides: object
    ) -> WizardConfig:
        cfg = _pg_config(base_config, **overrides)
        cfg.store_path.touch()
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")
        monkeypatch.setattr(wizard, "_peek_metric_count", lambda _p, _sid: 0)
        monkeypatch.setattr(wizard, "_peek_entity_count", lambda _p, _sid: 4)
        return cfg

    def test_skip_llm_confirm_bypasses_prompt(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = self._ready_cfg(base_config, monkeypatch, skip_llm_confirm=True)
        canned = wizard._MetricApplyResult(applied_count=3, cost_usd=0.005, llm_model="sonnet-4.6")
        monkeypatch.setattr(wizard, "_run_metric_suggestion", lambda **_kw: canned)
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

        outcome = wizard._stage_metrics(WizardContext(config=cfg))

        assert outcome.status == "done"
        assert "3 metrics created" in outcome.message

    def test_prompt_fires_in_tty_and_proceeds_on_enter(
        self,
        base_config: WizardConfig,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        cfg = self._ready_cfg(base_config, monkeypatch)
        canned = wizard._MetricApplyResult(applied_count=2, cost_usd=0.003, llm_model="sonnet-4.6")
        monkeypatch.setattr(wizard, "_run_metric_suggestion", lambda **_kw: canned)
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda: "")

        outcome = wizard._stage_metrics(WizardContext(config=cfg))

        assert outcome.status == "done"
        captured = capsys.readouterr()
        assert "metrics" in captured.err
        assert "Anthropic" in captured.err

    def test_user_cancels_with_ctrl_c(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = self._ready_cfg(base_config, monkeypatch)
        monkeypatch.setattr(
            wizard,
            "_run_metric_suggestion",
            lambda **_kw: pytest.fail("LLM call happened despite user cancelling"),
        )
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

        def _raise_ki() -> str:
            raise KeyboardInterrupt

        monkeypatch.setattr("builtins.input", _raise_ki)

        outcome = wizard._stage_metrics(WizardContext(config=cfg))

        assert outcome.status == "skipped"
        assert outcome.stage == 4
        assert outcome.name == "metrics"
        assert "user cancelled" in outcome.message
        assert outcome.next_step is not None
        assert "--yes" in outcome.next_step

    def test_dbt_branch_does_not_trigger_prompt(
        self,
        base_config: WizardConfig,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg = _pg_config(base_config)
        cfg.store_path.touch()
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")
        monkeypatch.setattr(wizard, "_peek_metric_count", lambda _p, _sid: 0)
        monkeypatch.setattr(wizard, "_peek_entity_count", lambda _p, _sid: 4)
        ctx = WizardContext(config=cfg)
        ctx.dbt_manifest_path = tmp_path / "manifest.json"
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        canned = wizard._MetricApplyResult(
            applied_count=6,
            cost_usd=0.0,
            llm_model="dbt-import",
            candidates_proposed=6,
            source="dbt",
        )
        monkeypatch.setattr(wizard, "_run_metrics_from_dbt", lambda **_kw: canned)
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr(
            "builtins.input",
            lambda: pytest.fail("prompt fired on the dbt branch"),
        )

        outcome = wizard._stage_metrics(ctx)

        assert outcome.status == "done"
        assert "6 metrics imported from dbt" in outcome.message


class TestStageMetricsDbtBranch:
    """PR C: stage 4 routes through `_run_metrics_from_dbt` when
    `ctx.dbt_manifest_path` is set. Sits AFTER the entity-empty
    cross-stage check (metrics still need entities to anchor on,
    whether imported from dbt or LLM-suggested) and BEFORE the
    API-key check.
    """

    def _ctx_with_dbt_and_entities(
        self,
        base_config: WizardConfig,
        manifest_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> WizardContext:
        cfg = _pg_config(base_config)
        cfg.store_path.touch()
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")
        monkeypatch.setattr(wizard, "_peek_metric_count", lambda _p, _sid: 0)
        monkeypatch.setattr(wizard, "_peek_entity_count", lambda _p, _sid: 4)
        ctx = WizardContext(config=cfg)
        ctx.dbt_manifest_path = manifest_path
        return ctx

    def test_done_on_successful_dbt_metric_import(
        self,
        base_config: WizardConfig,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ctx = self._ctx_with_dbt_and_entities(base_config, tmp_path / "manifest.json", monkeypatch)
        # No API key set — confirms the dbt branch runs ahead of the
        # api-key check.
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        canned = wizard._MetricApplyResult(
            applied_count=6,
            cost_usd=0.0,
            llm_model="dbt-import",
            candidates_proposed=6,
            source="dbt",
        )
        captured: dict[str, object] = {}

        def _fake_run(**kwargs: object) -> wizard._MetricApplyResult:
            captured.update(kwargs)
            return canned

        monkeypatch.setattr(wizard, "_run_metrics_from_dbt", _fake_run)

        outcome = wizard._stage_metrics(ctx)

        assert outcome.status == "done"
        assert "6 metrics imported from dbt" in outcome.message
        assert "cost" not in outcome.message
        assert captured["manifest_path"] == tmp_path / "manifest.json"

    def test_done_singular_metric_no_plural_s(
        self,
        base_config: WizardConfig,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ctx = self._ctx_with_dbt_and_entities(base_config, tmp_path / "manifest.json", monkeypatch)
        canned = wizard._MetricApplyResult(
            applied_count=1,
            cost_usd=0.0,
            llm_model="dbt-import",
            candidates_proposed=1,
            source="dbt",
        )
        monkeypatch.setattr(wizard, "_run_metrics_from_dbt", lambda **_kw: canned)

        outcome = wizard._stage_metrics(ctx)

        assert outcome.status == "done"
        assert "1 metric imported from dbt" in outcome.message
        # Make sure the plural-s is absent.
        assert "1 metrics" not in outcome.message

    def test_partial_success_reports_skip_count_and_reason(
        self,
        base_config: WizardConfig,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ctx = self._ctx_with_dbt_and_entities(base_config, tmp_path / "manifest.json", monkeypatch)
        partial = wizard._MetricApplyResult(
            applied_count=4,
            cost_usd=0.0,
            llm_model="dbt-import",
            candidates_proposed=6,
            skip_reason="DbtOwnedMetricError at 'revenue'",
            source="dbt",
        )
        monkeypatch.setattr(wizard, "_run_metrics_from_dbt", lambda **_kw: partial)

        outcome = wizard._stage_metrics(ctx)

        assert outcome.status == "done"
        assert "4 of 6 metrics imported from dbt" in outcome.message
        assert "2 skipped" in outcome.message
        assert "DbtOwnedMetricError" in outcome.message

    def test_failed_when_zero_applied_with_candidates_proposed(
        self,
        base_config: WizardConfig,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ctx = self._ctx_with_dbt_and_entities(base_config, tmp_path / "manifest.json", monkeypatch)
        result = wizard._MetricApplyResult(
            applied_count=0,
            cost_usd=0.0,
            llm_model="dbt-import",
            candidates_proposed=3,
            source="dbt",
        )
        monkeypatch.setattr(wizard, "_run_metrics_from_dbt", lambda **_kw: result)

        outcome = wizard._stage_metrics(ctx)

        assert outcome.status == "failed"
        assert "planned 3 metrics" in outcome.message
        assert "none could be written" in outcome.message

    def test_failed_when_zero_candidates_proposed(
        self,
        base_config: WizardConfig,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ctx = self._ctx_with_dbt_and_entities(base_config, tmp_path / "manifest.json", monkeypatch)
        result = wizard._MetricApplyResult(
            applied_count=0,
            cost_usd=0.0,
            llm_model="dbt-import",
            candidates_proposed=0,
            source="dbt",
        )
        monkeypatch.setattr(wizard, "_run_metrics_from_dbt", lambda **_kw: result)

        outcome = wizard._stage_metrics(ctx)

        assert outcome.status == "failed"
        assert "0 importable simple metrics" in outcome.message

    def test_failed_when_dbt_import_raises_helper_exception(
        self,
        base_config: WizardConfig,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ctx = self._ctx_with_dbt_and_entities(base_config, tmp_path / "manifest.json", monkeypatch)

        def _raise(**_kw: object) -> None:
            raise wizard._DbtImportFailedAtWizard(
                "dbt metric import failed: bad shape",
                next_step="run `dbt compile` then re-run",
            )

        monkeypatch.setattr(wizard, "_run_metrics_from_dbt", _raise)

        outcome = wizard._stage_metrics(ctx)

        assert outcome.status == "failed"
        assert "metric import failed" in outcome.message
        assert outcome.next_step == "run `dbt compile` then re-run"

    def test_skipped_when_empty_schema_race(
        self,
        base_config: WizardConfig,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Defensive: entities were dropped between the peek and the
        # dbt-metrics open. The dbt branch catches _EmptySchemaAtWizard
        # and emits a clean skipped outcome rather than crashing.
        ctx = self._ctx_with_dbt_and_entities(base_config, tmp_path / "manifest.json", monkeypatch)

        def _raise(**_kw: object) -> None:
            raise wizard._EmptySchemaAtWizard()

        monkeypatch.setattr(wizard, "_run_metrics_from_dbt", _raise)

        outcome = wizard._stage_metrics(ctx)

        assert outcome.status == "skipped"
        assert "empty between checks" in outcome.message

    def test_dbt_branch_bypassed_when_entity_store_empty(
        self,
        base_config: WizardConfig,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Cross-stage dep check runs BEFORE the dbt branch. Empty
        # entity store → skipped with entity-curation pointer.
        cfg = _pg_config(base_config)
        cfg.store_path.touch()
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")
        monkeypatch.setattr(wizard, "_peek_metric_count", lambda _p, _sid: 0)
        monkeypatch.setattr(wizard, "_peek_entity_count", lambda _p, _sid: 0)
        monkeypatch.setattr(
            wizard,
            "_run_metrics_from_dbt",
            lambda **_kw: pytest.fail("dbt path ran despite empty entity store"),
        )
        ctx = WizardContext(config=cfg)
        ctx.dbt_manifest_path = tmp_path / "manifest.json"

        outcome = wizard._stage_metrics(ctx)

        assert outcome.status == "skipped"
        assert "entity store is empty" in outcome.message


# ----- _resolve_metrics_cost_cap -------------------------------------------


class TestResolveMetricsCostCap:
    def test_uses_explicit_flag_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SCHEMABRAIN_MAX_LLM_COST_USD", "5.0")
        assert wizard._resolve_metrics_cost_cap(0.25) == 0.25

    def test_falls_back_to_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SCHEMABRAIN_MAX_LLM_COST_USD", "1.25")
        assert wizard._resolve_metrics_cost_cap(None) == 1.25

    def test_falls_back_to_package_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SCHEMABRAIN_MAX_LLM_COST_USD", raising=False)
        assert wizard._resolve_metrics_cost_cap(None) == wizard._WIZARD_METRICS_DEFAULT_COST_CAP_USD

    def test_invalid_env_var_warns_and_uses_default(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("SCHEMABRAIN_MAX_LLM_COST_USD", "not-a-number")
        result = wizard._resolve_metrics_cost_cap(None)
        captured = capsys.readouterr()
        assert result == wizard._WIZARD_METRICS_DEFAULT_COST_CAP_USD
        assert "not a valid number" in captured.err

    def test_non_positive_env_var_warns_and_uses_default(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("SCHEMABRAIN_MAX_LLM_COST_USD", "-1.0")
        result = wizard._resolve_metrics_cost_cap(None)
        captured = capsys.readouterr()
        assert result == wizard._WIZARD_METRICS_DEFAULT_COST_CAP_USD
        assert "must be positive" in captured.err


# ----- _peek_metric_count --------------------------------------------------


class TestPeekMetricCount:
    """Mirror of `TestPeekEntityCount`."""

    def test_returns_zero_for_fresh_store(self, tmp_path: Path) -> None:
        from schemabrain.core.store import SQLiteStore

        store_path = tmp_path / "peek_metric.db"
        with SQLiteStore(store_path):
            pass

        assert wizard._peek_metric_count(store_path, "missing_source") == 0

    def test_returns_failed_outcome_on_schema_mismatch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from schemabrain.core.store import SchemaVersionMismatchError

        store_path = tmp_path / "mismatch_metric.db"
        store_path.touch()

        def _raise_mismatch(*_a: object, **_kw: object) -> None:
            raise SchemaVersionMismatchError("v10 != v12")

        monkeypatch.setattr("schemabrain.core.store.SQLiteStore.__init__", _raise_mismatch)

        outcome = wizard._peek_metric_count(store_path, "src")

        assert isinstance(outcome, StageOutcome)
        assert outcome.status == "failed"
        assert outcome.stage == 4
        assert outcome.name == "metrics"

    def test_returns_failed_outcome_on_os_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store_path = tmp_path / "unreadable_metric.db"
        store_path.touch()

        def _raise_os(*_a: object, **_kw: object) -> None:
            raise OSError("read-only filesystem")

        monkeypatch.setattr("schemabrain.core.store.SQLiteStore.__init__", _raise_os)

        outcome = wizard._peek_metric_count(store_path, "src")

        assert isinstance(outcome, StageOutcome)
        assert outcome.status == "failed"
        assert "store unreadable" in outcome.message
        assert outcome.stage == 4


# ----- _run_metric_suggestion ----------------------------------------------


class TestRunMetricSuggestionSmoke:
    """Mirror of `TestRunEntitySuggestionSmoke`. Substitutes every
    external dependency (LLM client, pipeline, cost guard, SQLite
    store) so the function's wiring is verified without standing up
    Anthropic or Postgres.
    """

    def test_pipeline_invoked_and_writes_applied(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from schemabrain.metrics.suggest import MetricSuggestionResult

        cfg = _pg_config(base_config)

        class _FakeMetric:
            def __init__(self, name: str) -> None:
                self.name = name

        class _FakeCandidate:
            def __init__(self, name: str) -> None:
                self.metric = _FakeMetric(name=name)

        canned = MetricSuggestionResult(
            candidates=(_FakeCandidate("revenue"), _FakeCandidate("orders_placed")),  # type: ignore[arg-type]
            total_cost_usd=0.0080,
            llm_model="sonnet-4.6",
        )

        applied_writes: list[str] = []

        class _FakeStore:
            def __init__(self, *_a: object, **_kw: object) -> None:
                pass

            def __enter__(self) -> _FakeStore:
                return self

            def __exit__(self, *_a: object) -> None:
                return None

            def list_entities(self, *, source_connection_id: str) -> list[object]:
                return [object(), object()]

            def list_tables(self, *, source_connection_id: str) -> list[tuple[str, str]]:
                return [("public", "orders"), ("public", "users")]

            def get_table(self, _schema: str, name: str, *, source_connection_id: str) -> object:
                return object()

            def write_metric(self, metric: object, *, source_connection_id: str) -> None:
                applied_writes.append(metric.name)  # type: ignore[attr-defined]

        class _FakePipeline:
            def __init__(self, *, llm: object) -> None:
                pass

            def propose_from_entities(
                self, _entities: object, _tables: object
            ) -> MetricSuggestionResult:
                return canned

        class _FakeGuard:
            def __init__(self, *, inner: object, max_cost_usd: float) -> None:
                pass

        monkeypatch.setattr("schemabrain.core.store.SQLiteStore", _FakeStore)
        monkeypatch.setattr("schemabrain.metrics.suggest.MetricSuggestionPipeline", _FakePipeline)
        monkeypatch.setattr("schemabrain.entities.suggest.CostCeilingGuard", _FakeGuard)
        monkeypatch.setattr(
            "schemabrain.enrichment.anthropic_client.anthropic_sonnet_46_client",
            lambda *, api_key: object(),
        )

        result = wizard._run_metric_suggestion(
            cfg=cfg, source_id="abcd1234", api_key="sk-ant-test", max_cost_usd=0.5
        )

        assert result.applied_count == 2
        assert result.cost_usd == 0.0080
        assert result.llm_model == "sonnet-4.6"
        assert applied_writes == ["revenue", "orders_placed"]

    def test_raises_empty_schema_when_no_entities(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Defensive race: caller already gated on `_peek_entity_count
        # == 0`, but entities were dropped between the peek and the
        # open. Treat the same as empty schema.
        cfg = _pg_config(base_config)

        class _FakeStore:
            def __init__(self, *_a: object, **_kw: object) -> None:
                pass

            def __enter__(self) -> _FakeStore:
                return self

            def __exit__(self, *_a: object) -> None:
                return None

            def list_entities(self, *, source_connection_id: str) -> list[object]:
                return []

        monkeypatch.setattr("schemabrain.core.store.SQLiteStore", _FakeStore)
        monkeypatch.setattr(
            "schemabrain.enrichment.anthropic_client.anthropic_sonnet_46_client",
            lambda *, api_key: object(),
        )

        with pytest.raises(wizard._EmptySchemaAtWizard):
            wizard._run_metric_suggestion(
                cfg=cfg, source_id="abcd1234", api_key="sk-ant-test", max_cost_usd=0.5
            )

    def test_raises_empty_schema_when_no_tables(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _pg_config(base_config)

        class _FakeStore:
            def __init__(self, *_a: object, **_kw: object) -> None:
                pass

            def __enter__(self) -> _FakeStore:
                return self

            def __exit__(self, *_a: object) -> None:
                return None

            def list_entities(self, *, source_connection_id: str) -> list[object]:
                return [object()]

            def list_tables(self, *, source_connection_id: str) -> list[tuple[str, str]]:
                return []

        monkeypatch.setattr("schemabrain.core.store.SQLiteStore", _FakeStore)
        monkeypatch.setattr(
            "schemabrain.enrichment.anthropic_client.anthropic_sonnet_46_client",
            lambda *, api_key: object(),
        )

        with pytest.raises(wizard._EmptySchemaAtWizard):
            wizard._run_metric_suggestion(
                cfg=cfg, source_id="abcd1234", api_key="sk-ant-test", max_cost_usd=0.5
            )

    def test_raises_empty_schema_when_tables_unloadable(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `list_tables` returns names but `get_table` returns None for
        # each — the cache row exists but the table fingerprints are
        # missing. Treat as empty-schema.
        cfg = _pg_config(base_config)

        class _FakeStore:
            def __init__(self, *_a: object, **_kw: object) -> None:
                pass

            def __enter__(self) -> _FakeStore:
                return self

            def __exit__(self, *_a: object) -> None:
                return None

            def list_entities(self, *, source_connection_id: str) -> list[object]:
                return [object()]

            def list_tables(self, *, source_connection_id: str) -> list[tuple[str, str]]:
                return [("public", "ghost")]

            def get_table(self, *_a: object, **_kw: object) -> None:
                return None

        monkeypatch.setattr("schemabrain.core.store.SQLiteStore", _FakeStore)
        monkeypatch.setattr(
            "schemabrain.enrichment.anthropic_client.anthropic_sonnet_46_client",
            lambda *, api_key: object(),
        )

        with pytest.raises(wizard._EmptySchemaAtWizard):
            wizard._run_metric_suggestion(
                cfg=cfg, source_id="abcd1234", api_key="sk-ant-test", max_cost_usd=0.5
            )

    def test_translates_cost_ceiling_error(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from schemabrain.entities.suggest import CostCeilingExceededError

        cfg = _pg_config(base_config)

        class _FakeStore:
            def __init__(self, *_a: object, **_kw: object) -> None:
                pass

            def __enter__(self) -> _FakeStore:
                return self

            def __exit__(self, *_a: object) -> None:
                return None

            def list_entities(self, *, source_connection_id: str) -> list[object]:
                return [object()]

            def list_tables(self, *, source_connection_id: str) -> list[tuple[str, str]]:
                return [("public", "t")]

            def get_table(self, *_a: object, **_kw: object) -> object:
                return object()

        class _RaisingPipeline:
            def __init__(self, *, llm: object) -> None:
                pass

            def propose_from_entities(self, _entities: object, _tables: object) -> object:
                raise CostCeilingExceededError(
                    cumulative_cost_usd=0.5,
                    next_call_estimate_usd=0.6,
                    max_cost_usd=1.0,
                )

        class _FakeGuard:
            def __init__(self, **_kw: object) -> None:
                pass

        monkeypatch.setattr("schemabrain.core.store.SQLiteStore", _FakeStore)
        monkeypatch.setattr(
            "schemabrain.metrics.suggest.MetricSuggestionPipeline", _RaisingPipeline
        )
        monkeypatch.setattr("schemabrain.entities.suggest.CostCeilingGuard", _FakeGuard)
        monkeypatch.setattr(
            "schemabrain.enrichment.anthropic_client.anthropic_sonnet_46_client",
            lambda *, api_key: object(),
        )

        with pytest.raises(wizard._CostCeilingExceededAtWizard):
            wizard._run_metric_suggestion(
                cfg=cfg, source_id="abcd1234", api_key="sk-ant-test", max_cost_usd=1.0
            )

    def test_translates_parse_error(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from schemabrain.entities.suggest import SuggestionParseError

        cfg = _pg_config(base_config)

        class _FakeStore:
            def __init__(self, *_a: object, **_kw: object) -> None:
                pass

            def __enter__(self) -> _FakeStore:
                return self

            def __exit__(self, *_a: object) -> None:
                return None

            def list_entities(self, *, source_connection_id: str) -> list[object]:
                return [object()]

            def list_tables(self, *, source_connection_id: str) -> list[tuple[str, str]]:
                return [("public", "t")]

            def get_table(self, *_a: object, **_kw: object) -> object:
                return object()

        class _RaisingPipeline:
            def __init__(self, *, llm: object) -> None:
                pass

            def propose_from_entities(self, _entities: object, _tables: object) -> object:
                raise SuggestionParseError("bad YAML")

        class _FakeGuard:
            def __init__(self, **_kw: object) -> None:
                pass

        monkeypatch.setattr("schemabrain.core.store.SQLiteStore", _FakeStore)
        monkeypatch.setattr(
            "schemabrain.metrics.suggest.MetricSuggestionPipeline", _RaisingPipeline
        )
        monkeypatch.setattr("schemabrain.entities.suggest.CostCeilingGuard", _FakeGuard)
        monkeypatch.setattr(
            "schemabrain.enrichment.anthropic_client.anthropic_sonnet_46_client",
            lambda *, api_key: object(),
        )

        with pytest.raises(wizard._SuggestionParseAtWizard):
            wizard._run_metric_suggestion(
                cfg=cfg, source_id="abcd1234", api_key="sk-ant-test", max_cost_usd=1.0
            )

    def test_partial_apply_breaks_on_dbt_owned_error(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Verify the `except (DbtOwnedMetricError, IntegrityError): break`
        # path: write 0 succeeds, write 1 raises, applied=1.
        from schemabrain.core.store import DbtOwnedMetricError
        from schemabrain.metrics.suggest import MetricSuggestionResult

        cfg = _pg_config(base_config)

        class _FakeMetric:
            def __init__(self, name: str) -> None:
                self.name = name

        class _FakeCandidate:
            def __init__(self, name: str) -> None:
                self.metric = _FakeMetric(name=name)

        canned = MetricSuggestionResult(
            candidates=(_FakeCandidate("revenue"), _FakeCandidate("aov")),  # type: ignore[arg-type]
            total_cost_usd=0.01,
            llm_model="sonnet-4.6",
        )

        applied: list[str] = []

        class _FakeStore:
            def __init__(self, *_a: object, **_kw: object) -> None:
                pass

            def __enter__(self) -> _FakeStore:
                return self

            def __exit__(self, *_a: object) -> None:
                return None

            def list_entities(self, *, source_connection_id: str) -> list[object]:
                return [object()]

            def list_tables(self, *, source_connection_id: str) -> list[tuple[str, str]]:
                return [("public", "orders")]

            def get_table(self, *_a: object, **_kw: object) -> object:
                return object()

            def write_metric(self, metric: object, *, source_connection_id: str) -> None:
                if metric.name == "aov":  # type: ignore[attr-defined]
                    raise DbtOwnedMetricError("aov is owned by dbt")
                applied.append(metric.name)  # type: ignore[attr-defined]

        class _FakePipeline:
            def __init__(self, *, llm: object) -> None:
                pass

            def propose_from_entities(
                self, _entities: object, _tables: object
            ) -> MetricSuggestionResult:
                return canned

        class _FakeGuard:
            def __init__(self, **_kw: object) -> None:
                pass

        monkeypatch.setattr("schemabrain.core.store.SQLiteStore", _FakeStore)
        monkeypatch.setattr("schemabrain.metrics.suggest.MetricSuggestionPipeline", _FakePipeline)
        monkeypatch.setattr("schemabrain.entities.suggest.CostCeilingGuard", _FakeGuard)
        monkeypatch.setattr(
            "schemabrain.enrichment.anthropic_client.anthropic_sonnet_46_client",
            lambda *, api_key: object(),
        )

        result = wizard._run_metric_suggestion(
            cfg=cfg, source_id="abcd1234", api_key="sk-ant-test", max_cost_usd=1.0
        )

        assert result.applied_count == 1
        assert applied == ["revenue"]
        assert result.skip_reason is not None
        assert "DbtOwnedMetricError" in result.skip_reason
        assert "aov" in result.skip_reason


# ----- _stage_joins --------------------------------------------------------


class TestStageJoins:
    """Tests for `_stage_joins`.

    Unlike entities and metrics, the join suggester is NOT
    LLM-driven — `suggest_canonical_joins` mines FK constraints +
    query-log evidence and returns a deterministic list. So this
    test class has no API-key branch, no cost-cap branch, no
    parse-error branch. Five skip branches + four outcome branches
    (done / empty-evidence / partial-success / failed-on-mismatch).
    """

    def test_skipped_when_no_joins_flag_set(self, base_config: WizardConfig) -> None:
        cfg = _pg_config(base_config, no_joins=True)
        outcome = wizard._stage_joins(WizardContext(config=cfg))

        assert outcome.status == "skipped"
        assert outcome.stage == 5
        assert outcome.name == "joins"
        assert "--no-joins" in outcome.message
        assert "joins suggest" in (outcome.next_step or "")

    def test_skipped_when_skip_index_set(self, base_config: WizardConfig) -> None:
        cfg = _pg_config(base_config, skip_index=True)
        outcome = wizard._stage_joins(WizardContext(config=cfg))

        assert outcome.status == "skipped"
        assert "--skip-index" in outcome.message

    def test_skipped_for_non_postgres_source(self, base_config: WizardConfig) -> None:
        # base_config has sqlite:///:memory:
        outcome = wizard._stage_joins(WizardContext(config=base_config))

        assert outcome.status == "skipped"
        assert "Postgres" in outcome.message

    def test_skipped_when_joins_already_present(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _pg_config(base_config)
        cfg.store_path.touch()
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")
        monkeypatch.setattr(wizard, "_peek_join_count", lambda _p, _sid: 4)
        monkeypatch.setattr(
            wizard,
            "_run_join_suggestion",
            lambda **_kw: pytest.fail("pipeline ran despite idempotent skip"),
        )

        outcome = wizard._stage_joins(WizardContext(config=cfg))

        assert outcome.status == "skipped"
        assert "4 canonical join" in outcome.message

    def test_skipped_when_entity_store_empty(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Same cross-stage dependency as `_stage_metrics`: joins
        # anchor on entities. The pipeline silently returns [] when
        # entities are empty; we surface the missing prerequisite
        # instead.
        cfg = _pg_config(base_config)
        cfg.store_path.touch()
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")
        monkeypatch.setattr(wizard, "_peek_join_count", lambda _p, _sid: 0)
        monkeypatch.setattr(wizard, "_peek_entity_count", lambda _p, _sid: 0)
        monkeypatch.setattr(
            wizard,
            "_run_join_suggestion",
            lambda **_kw: pytest.fail("pipeline ran despite empty entity store"),
        )

        outcome = wizard._stage_joins(WizardContext(config=cfg))

        assert outcome.status == "skipped"
        assert "entity store is empty" in outcome.message
        assert outcome.next_step is not None
        assert "schemabrain entities suggest --apply" in outcome.next_step

    def test_skipped_when_store_file_missing(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No store file at all → treat as empty entity store.
        cfg = _pg_config(base_config)
        assert not cfg.store_path.exists()
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")

        outcome = wizard._stage_joins(WizardContext(config=cfg))

        assert outcome.status == "skipped"
        assert "entity store is empty" in outcome.message

    def test_failed_on_schema_version_mismatch(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _pg_config(base_config)
        cfg.store_path.touch()
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")

        peek_failure = StageOutcome(
            stage=5,
            name="joins",
            status="failed",
            message="store created with schema v10 but installed schemabrain expects v12",
            next_step=f"delete {cfg.store_path} and re-run",
        )
        monkeypatch.setattr(wizard, "_peek_join_count", lambda _p, _sid: peek_failure)

        outcome = wizard._stage_joins(WizardContext(config=cfg))

        assert outcome.status == "failed"
        assert "v10" in outcome.message

    def test_failed_when_entity_peek_returns_outcome(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `_peek_entity_count` may return a StageOutcome on a schema
        # mismatch — the joins stage must re-shape it to stage=5,
        # name="joins" so the renderer routes correctly.
        cfg = _pg_config(base_config)
        cfg.store_path.touch()
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")
        monkeypatch.setattr(wizard, "_peek_join_count", lambda _p, _sid: 0)

        entity_peek_failure = StageOutcome(
            stage=3,
            name="entities",
            status="failed",
            message="entity-side schema mismatch",
            next_step=f"delete {cfg.store_path} and re-run",
        )
        monkeypatch.setattr(wizard, "_peek_entity_count", lambda _p, _sid: entity_peek_failure)

        outcome = wizard._stage_joins(WizardContext(config=cfg))

        assert outcome.status == "failed"
        assert outcome.stage == 5
        assert outcome.name == "joins"
        assert "entity-side schema mismatch" in outcome.message

    def test_done_on_successful_apply(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _pg_config(base_config)
        cfg.store_path.touch()
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")
        monkeypatch.setattr(wizard, "_peek_join_count", lambda _p, _sid: 0)
        monkeypatch.setattr(wizard, "_peek_entity_count", lambda _p, _sid: 4)
        canned = wizard._JoinApplyResult(applied_count=3, candidates_proposed=3)

        captured: dict[str, object] = {}

        def _fake_run(**kwargs: object) -> wizard._JoinApplyResult:
            captured.update(kwargs)
            return canned

        monkeypatch.setattr(wizard, "_run_join_suggestion", _fake_run)

        outcome = wizard._stage_joins(WizardContext(config=cfg))

        assert outcome.status == "done"
        assert "3 canonical join" in outcome.message
        assert "FK + query-log evidence" in outcome.message
        # Confirm the source_id was passed through.
        assert captured["source_id"] == "abcd1234"

    def test_skipped_when_no_evidence(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The suggester returned zero candidates — no FK constraints
        # AND no query-log evidence. Skipped, not failed, because this
        # is a legitimate "nothing to suggest" state, not a bug.
        cfg = _pg_config(base_config)
        cfg.store_path.touch()
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")
        monkeypatch.setattr(wizard, "_peek_join_count", lambda _p, _sid: 0)
        monkeypatch.setattr(wizard, "_peek_entity_count", lambda _p, _sid: 4)
        empty = wizard._JoinApplyResult(applied_count=0, candidates_proposed=0)
        monkeypatch.setattr(wizard, "_run_join_suggestion", lambda **_kw: empty)

        outcome = wizard._stage_joins(WizardContext(config=cfg))

        assert outcome.status == "skipped"
        assert "no canonical joins surfaced" in outcome.message
        assert outcome.next_step is not None
        assert "joins apply" in outcome.next_step

    def test_partial_success_reports_skipped_count(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Some joins applied, others tripped on a TOCTOU
        # IntegrityError. Surface the count + skip reason so the
        # user can act on the partial.
        cfg = _pg_config(base_config)
        cfg.store_path.touch()
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")
        monkeypatch.setattr(wizard, "_peek_join_count", lambda _p, _sid: 0)
        monkeypatch.setattr(wizard, "_peek_entity_count", lambda _p, _sid: 4)
        partial = wizard._JoinApplyResult(
            applied_count=2,
            candidates_proposed=4,
            skip_reason="IntegrityError at 'order_customer'",
        )
        monkeypatch.setattr(wizard, "_run_join_suggestion", lambda **_kw: partial)

        outcome = wizard._stage_joins(WizardContext(config=cfg))

        assert outcome.status == "done"
        assert "2 of 4" in outcome.message
        assert "IntegrityError at 'order_customer'" in outcome.message
        assert outcome.next_step is not None
        assert "joins suggest --dry-run" in outcome.next_step

    def test_skipped_when_pipeline_raises_empty_schema(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Defensive: `_run_join_suggestion` may raise
        # `_EmptySchemaAtWizard` if a future refactor adds that path.
        cfg = _pg_config(base_config)
        cfg.store_path.touch()
        monkeypatch.setattr(wizard, "_source_id_for", lambda _url: "abcd1234")
        monkeypatch.setattr(wizard, "_peek_join_count", lambda _p, _sid: 0)
        monkeypatch.setattr(wizard, "_peek_entity_count", lambda _p, _sid: 4)

        def _raise(**_kw: object) -> None:
            raise wizard._EmptySchemaAtWizard()

        monkeypatch.setattr(wizard, "_run_join_suggestion", _raise)

        outcome = wizard._stage_joins(WizardContext(config=cfg))

        assert outcome.status == "skipped"
        assert "FK constraints" in outcome.message


# ----- _peek_join_count ----------------------------------------------------


class TestPeekJoinCount:
    """Mirror of `TestPeekMetricCount`."""

    def test_returns_zero_for_fresh_store(self, tmp_path: Path) -> None:
        from schemabrain.core.store import SQLiteStore

        store_path = tmp_path / "peek_join.db"
        with SQLiteStore(store_path):
            pass

        assert wizard._peek_join_count(store_path, "missing_source") == 0

    def test_returns_failed_outcome_on_schema_mismatch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from schemabrain.core.store import SchemaVersionMismatchError

        store_path = tmp_path / "mismatch_join.db"
        store_path.touch()

        def _raise_mismatch(*_a: object, **_kw: object) -> None:
            raise SchemaVersionMismatchError("v10 != v12")

        monkeypatch.setattr("schemabrain.core.store.SQLiteStore.__init__", _raise_mismatch)

        outcome = wizard._peek_join_count(store_path, "src")

        assert isinstance(outcome, StageOutcome)
        assert outcome.status == "failed"
        assert outcome.stage == 5
        assert outcome.name == "joins"

    def test_returns_failed_outcome_on_os_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store_path = tmp_path / "unreadable_join.db"
        store_path.touch()

        def _raise_os(*_a: object, **_kw: object) -> None:
            raise OSError("read-only filesystem")

        monkeypatch.setattr("schemabrain.core.store.SQLiteStore.__init__", _raise_os)

        outcome = wizard._peek_join_count(store_path, "src")

        assert isinstance(outcome, StageOutcome)
        assert outcome.status == "failed"
        assert "store unreadable" in outcome.message
        assert outcome.stage == 5


# ----- _run_join_suggestion ------------------------------------------------


class TestRunJoinSuggestionSmoke:
    """Mirror of `TestRunMetricSuggestionSmoke`. Substitutes the
    SQLite store + `suggest_canonical_joins` so the function's
    wiring is verified without standing up Postgres.
    """

    def test_pipeline_invoked_and_writes_applied(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _pg_config(base_config)

        class _FakeCanonical:
            def __init__(self, name: str) -> None:
                self.name = name

        class _FakeCandidate:
            def __init__(self, name: str) -> None:
                self.name = name

            def to_canonical_join(self) -> object:
                return _FakeCanonical(self.name)

        applied_writes: list[str] = []

        class _FakeStore:
            def __init__(self, *_a: object, **_kw: object) -> None:
                pass

            def __enter__(self) -> _FakeStore:
                return self

            def __exit__(self, *_a: object) -> None:
                return None

            def write_canonical_join(self, join: object, *, source_connection_id: str) -> None:
                applied_writes.append(join.name)  # type: ignore[attr-defined]

        def _fake_suggest(*, store: object, source_connection_id: str) -> list[object]:
            return [_FakeCandidate("customer_order"), _FakeCandidate("order_product")]

        monkeypatch.setattr("schemabrain.core.store.SQLiteStore", _FakeStore)
        monkeypatch.setattr("schemabrain.joins.suggest.suggest_canonical_joins", _fake_suggest)

        result = wizard._run_join_suggestion(cfg=cfg, source_id="abcd1234")

        assert result.applied_count == 2
        assert result.candidates_proposed == 2
        assert applied_writes == ["customer_order", "order_product"]
        assert result.skip_reason is None

    def test_partial_apply_breaks_on_integrity_error(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Verify the `except IntegrityError: break` path: write 0
        # succeeds, write 1 raises, applied=1.
        from sqlite3 import IntegrityError

        cfg = _pg_config(base_config)

        class _FakeCanonical:
            def __init__(self, name: str) -> None:
                self.name = name

        class _FakeCandidate:
            def __init__(self, name: str) -> None:
                self.name = name

            def to_canonical_join(self) -> object:
                return _FakeCanonical(self.name)

        applied: list[str] = []

        class _FakeStore:
            def __init__(self, *_a: object, **_kw: object) -> None:
                pass

            def __enter__(self) -> _FakeStore:
                return self

            def __exit__(self, *_a: object) -> None:
                return None

            def write_canonical_join(self, join: object, *, source_connection_id: str) -> None:
                if join.name == "ghost_join":  # type: ignore[attr-defined]
                    raise IntegrityError("FOREIGN KEY constraint failed")
                applied.append(join.name)  # type: ignore[attr-defined]

        def _fake_suggest(*, store: object, source_connection_id: str) -> list[object]:
            return [_FakeCandidate("customer_order"), _FakeCandidate("ghost_join")]

        monkeypatch.setattr("schemabrain.core.store.SQLiteStore", _FakeStore)
        monkeypatch.setattr("schemabrain.joins.suggest.suggest_canonical_joins", _fake_suggest)

        result = wizard._run_join_suggestion(cfg=cfg, source_id="abcd1234")

        assert result.applied_count == 1
        assert applied == ["customer_order"]
        assert result.skip_reason is not None
        assert "IntegrityError" in result.skip_reason
        assert "ghost_join" in result.skip_reason

    def test_empty_candidate_list_returns_zero_applied(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _pg_config(base_config)

        class _FakeStore:
            def __init__(self, *_a: object, **_kw: object) -> None:
                pass

            def __enter__(self) -> _FakeStore:
                return self

            def __exit__(self, *_a: object) -> None:
                return None

        def _fake_suggest(*, store: object, source_connection_id: str) -> list[object]:
            return []

        monkeypatch.setattr("schemabrain.core.store.SQLiteStore", _FakeStore)
        monkeypatch.setattr("schemabrain.joins.suggest.suggest_canonical_joins", _fake_suggest)

        result = wizard._run_join_suggestion(cfg=cfg, source_id="abcd1234")

        assert result.applied_count == 0
        assert result.candidates_proposed == 0
        assert result.skip_reason is None


# ----- _run_entities_from_dbt (PR C) --------------------------------------


class TestRunEntitiesFromDbtSmoke:
    """Mirror of `TestRunEntitySuggestionSmoke` for the dbt path.
    Substitutes `parse_dbt_manifest`, `PostgresDataSource`,
    `SQLiteStore`, `plan_dbt_import`, and `apply_dbt_import_plan` so
    the wiring is verified without Postgres or a real manifest file.
    """

    def _patch_dbt_deps(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        plan: object,
        result: object,
        parse_raises: Exception | None = None,
        connect_raises: Exception | None = None,
    ) -> None:
        """Stub every dbt-import dependency the helper imports."""

        def _fake_parse(_path: object) -> object:
            if parse_raises is not None:
                raise parse_raises
            return object()  # opaque manifest token

        monkeypatch.setattr("schemabrain.imports.dbt.parse_dbt_manifest", _fake_parse)

        class _FakeStore:
            def __init__(self, *_a: object, **_kw: object) -> None:
                pass

            def __enter__(self) -> _FakeStore:
                return self

            def __exit__(self, *_a: object) -> None:
                return None

        class _FakeSource:
            def __init__(self, *_a: object, **_kw: object) -> None:
                if connect_raises is not None:
                    raise connect_raises

            def __enter__(self) -> _FakeSource:
                return self

            def __exit__(self, *_a: object) -> None:
                return None

        monkeypatch.setattr("schemabrain.core.store.SQLiteStore", _FakeStore)
        monkeypatch.setattr("schemabrain.connectors.postgres.PostgresDataSource", _FakeSource)
        monkeypatch.setattr(
            "schemabrain.imports.dbt.plan_dbt_import",
            lambda *_a, **_kw: plan,
        )
        monkeypatch.setattr(
            "schemabrain.imports.dbt.apply_dbt_import_plan",
            lambda *_a, **_kw: result,
        )

    def test_returns_applied_count_from_three_writable_buckets(
        self,
        base_config: WizardConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        cfg = _pg_config(base_config)

        # Use SimpleNamespace stubs — `DbtImportPlan` + `DbtImportResult`
        # are frozen dataclasses with complex inner types; here we only
        # need shape-compatible attributes.
        from types import SimpleNamespace

        plan = SimpleNamespace(
            to_add=(object(), object()),
            to_update=(object(),),
            to_take_ownership=(object(),),
        )
        result = SimpleNamespace(write_failures=())

        self._patch_dbt_deps(monkeypatch, plan=plan, result=result)

        out = wizard._run_entities_from_dbt(
            cfg=cfg,
            manifest_path=tmp_path / "manifest.json",
            source_id="abcd1234",
        )

        # 2 to_add + 1 to_update + 1 to_take_ownership = 4 planned writes
        # No failures → applied == planned.
        assert out.applied_count == 4
        assert out.candidates_proposed == 4
        assert out.cost_usd == 0.0
        assert out.llm_model == wizard._DBT_IMPORT_MODEL_LABEL
        assert out.source == "dbt"
        assert out.skip_reason is None

    def test_partial_write_failures_reported(
        self,
        base_config: WizardConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from types import SimpleNamespace

        cfg = _pg_config(base_config)
        plan = SimpleNamespace(
            to_add=(object(), object(), object()),
            to_update=(),
            to_take_ownership=(),
        )
        result = SimpleNamespace(
            write_failures=(
                SimpleNamespace(entity_name="ghost_model", message="bound table missing"),
            ),
        )
        self._patch_dbt_deps(monkeypatch, plan=plan, result=result)

        out = wizard._run_entities_from_dbt(
            cfg=cfg,
            manifest_path=tmp_path / "manifest.json",
            source_id="abcd1234",
        )

        # 3 planned, 1 failed → 2 applied.
        assert out.applied_count == 2
        assert out.candidates_proposed == 3
        assert out.skip_reason is not None
        assert "ghost_model" in out.skip_reason
        assert "bound table missing" in out.skip_reason

    def test_manifest_parse_error_translates_to_helper_exception(
        self,
        base_config: WizardConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from schemabrain.imports.dbt import DbtManifestParseError

        cfg = _pg_config(base_config)
        from types import SimpleNamespace

        self._patch_dbt_deps(
            monkeypatch,
            plan=SimpleNamespace(to_add=(), to_update=(), to_take_ownership=()),
            result=SimpleNamespace(write_failures=()),
            parse_raises=DbtManifestParseError("missing target/manifest.json"),
        )

        with pytest.raises(wizard._DbtImportFailedAtWizard) as exc_info:
            wizard._run_entities_from_dbt(
                cfg=cfg,
                manifest_path=tmp_path / "manifest.json",
                source_id="abcd1234",
            )

        assert "missing target/manifest.json" in exc_info.value.message
        assert "dbt compile" in exc_info.value.next_step

    def test_postgres_operational_error_translates_to_helper_exception(
        self,
        base_config: WizardConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from sqlalchemy.exc import OperationalError

        cfg = _pg_config(base_config)
        from types import SimpleNamespace

        self._patch_dbt_deps(
            monkeypatch,
            plan=SimpleNamespace(to_add=(), to_update=(), to_take_ownership=()),
            result=SimpleNamespace(write_failures=()),
            connect_raises=OperationalError("statement", {}, "connection refused"),
        )

        with pytest.raises(wizard._DbtImportFailedAtWizard) as exc_info:
            wizard._run_entities_from_dbt(
                cfg=cfg,
                manifest_path=tmp_path / "manifest.json",
                source_id="abcd1234",
            )

        assert "Postgres connection failed" in exc_info.value.message
        assert "source URL" in exc_info.value.next_step


# ----- _run_metrics_from_dbt (PR C) ---------------------------------------


class TestRunMetricsFromDbtSmoke:
    """Mirror of `TestRunMetricSuggestionSmoke` for the dbt path.
    Substitutes `parse_dbt_metrics` + `SQLiteStore`. No Postgres
    connection needed for the metric importer — `parse_dbt_metrics`
    reads only the manifest JSON.
    """

    def _patch_store(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        entity_names: set[str],
        write_metric: object,
    ) -> None:
        # Closure captures `write_metric` + `entity_names` so the
        # nested class can reach them without re-binding to the
        # class body's local namespace.
        captured_write = write_metric
        captured_names = entity_names

        class _FakeStore:
            def __init__(self, *_a: object, **_kw: object) -> None:
                pass

            def __enter__(self) -> _FakeStore:
                return self

            def __exit__(self, *_a: object) -> None:
                return None

            def list_entities(self, *, source_connection_id: str) -> list[object]:
                # Return a list of objects whose `.name` attribute
                # matches the supplied names.
                from types import SimpleNamespace

                return [SimpleNamespace(name=n) for n in captured_names]

            def write_metric(self, metric: object, *, source_connection_id: str) -> None:
                # type: ignore[misc] — captured_write is a callable
                # from the enclosing closure.
                captured_write(metric, source_connection_id=source_connection_id)  # type: ignore[operator]

        monkeypatch.setattr("schemabrain.core.store.SQLiteStore", _FakeStore)

    def test_writes_each_metric_returned_by_parser(
        self,
        base_config: WizardConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from types import SimpleNamespace

        cfg = _pg_config(base_config)
        written: list[str] = []

        def _write_metric(metric: object, *, source_connection_id: str) -> None:
            written.append(metric.name)  # type: ignore[attr-defined]

        self._patch_store(
            monkeypatch,
            entity_names={"customer", "order"},
            write_metric=_write_metric,
        )

        canned_metrics = (
            SimpleNamespace(name="revenue"),
            SimpleNamespace(name="orders_placed"),
        )

        def _fake_parse_metrics(
            _path: object, *, imported_entity_names: set[str]
        ) -> tuple[tuple[object, ...], tuple[object, ...]]:
            assert imported_entity_names == {"customer", "order"}
            return canned_metrics, ()

        monkeypatch.setattr(
            "schemabrain.imports.dbt_metrics.parse_dbt_metrics", _fake_parse_metrics
        )

        out = wizard._run_metrics_from_dbt(
            cfg=cfg,
            manifest_path=tmp_path / "manifest.json",
            source_id="abcd1234",
        )

        assert out.applied_count == 2
        assert out.candidates_proposed == 2
        assert out.cost_usd == 0.0
        assert out.llm_model == wizard._DBT_IMPORT_MODEL_LABEL
        assert out.source == "dbt"
        assert written == ["revenue", "orders_placed"]

    def test_dbt_owned_error_breaks_loop_with_skip_reason(
        self,
        base_config: WizardConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from types import SimpleNamespace

        from schemabrain.core.store import DbtOwnedMetricError

        cfg = _pg_config(base_config)
        written: list[str] = []

        def _write_metric(metric: object, *, source_connection_id: str) -> None:
            name = metric.name  # type: ignore[attr-defined]
            if name == "aov":
                raise DbtOwnedMetricError("aov is owned by another dbt project")
            written.append(name)

        self._patch_store(monkeypatch, entity_names={"order"}, write_metric=_write_metric)
        canned = (SimpleNamespace(name="revenue"), SimpleNamespace(name="aov"))
        monkeypatch.setattr(
            "schemabrain.imports.dbt_metrics.parse_dbt_metrics",
            lambda _p, *, imported_entity_names: (canned, ()),
        )

        out = wizard._run_metrics_from_dbt(
            cfg=cfg,
            manifest_path=tmp_path / "manifest.json",
            source_id="abcd1234",
        )

        assert out.applied_count == 1
        assert written == ["revenue"]
        assert out.skip_reason is not None
        assert "DbtOwnedMetricError" in out.skip_reason
        assert "aov" in out.skip_reason

    def test_metric_parse_error_translates_to_helper_exception(
        self,
        base_config: WizardConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from schemabrain.imports.dbt_metrics import DbtMetricImportError

        cfg = _pg_config(base_config)
        self._patch_store(monkeypatch, entity_names={"order"}, write_metric=lambda *a, **kw: None)

        def _raise(_p: object, *, imported_entity_names: set[str]) -> None:
            raise DbtMetricImportError("manifest schema v9 < v11")

        monkeypatch.setattr("schemabrain.imports.dbt_metrics.parse_dbt_metrics", _raise)

        with pytest.raises(wizard._DbtImportFailedAtWizard) as exc_info:
            wizard._run_metrics_from_dbt(
                cfg=cfg,
                manifest_path=tmp_path / "manifest.json",
                source_id="abcd1234",
            )

        assert "metric import failed" in exc_info.value.message
        assert "dbt compile" in exc_info.value.next_step

    def test_empty_entity_set_raises_empty_schema_at_wizard(
        self,
        base_config: WizardConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # Defensive race: caller's `_peek_entity_count > 0` guard
        # passed, but entities were dropped between the peek and the
        # open. Same shape as the LLM path.
        cfg = _pg_config(base_config)
        self._patch_store(monkeypatch, entity_names=set(), write_metric=lambda *a, **kw: None)

        with pytest.raises(wizard._EmptySchemaAtWizard):
            wizard._run_metrics_from_dbt(
                cfg=cfg,
                manifest_path=tmp_path / "manifest.json",
                source_id="abcd1234",
            )


# ----- WizardConfig: no_joins default -------------------------------------


class TestWizardConfigJoinsDefaults:
    def _build(self, **overrides: object) -> WizardConfig:
        fields: dict[str, object] = {
            "source_url": "sqlite:///:memory:",
            "store_path": Path("/tmp/test-joins.db"),  # nosec B108 — never opened
            "host": "manual",
            "env_var_name": "SCHEMABRAIN_DATABASE_URL",
            "skip_index": False,
            "no_entities": False,
            "enrich": False,
            "entities_max_cost_usd": None,
            "assume_yes": False,
        }
        fields.update(overrides)
        return WizardConfig(**fields)  # type: ignore[arg-type]

    def test_no_joins_defaults_to_false(self) -> None:
        cfg = self._build()
        assert cfg.no_joins is False

    def test_no_joins_accepts_true(self) -> None:
        cfg = self._build(no_joins=True)
        assert cfg.no_joins is True


class TestWizardConfigDbtDefaults:
    """PR C: WizardConfig.from_dbt defaults to None + accepts Path."""

    def _build(self, **overrides: object) -> WizardConfig:
        fields: dict[str, object] = {
            "source_url": "sqlite:///:memory:",
            "store_path": Path("/tmp/test-dbt.db"),  # nosec B108 — never opened
            "host": "manual",
            "env_var_name": "SCHEMABRAIN_DATABASE_URL",
            "skip_index": False,
            "no_entities": False,
            "enrich": False,
            "entities_max_cost_usd": None,
            "assume_yes": False,
        }
        fields.update(overrides)
        return WizardConfig(**fields)  # type: ignore[arg-type]

    def test_from_dbt_defaults_to_none(self) -> None:
        cfg = self._build()
        assert cfg.from_dbt is None

    def test_from_dbt_accepts_path(self) -> None:
        path = Path("/some/dbt/target/manifest.json")  # nosec B108 — never opened
        cfg = self._build(from_dbt=path)
        assert cfg.from_dbt == path


class TestWizardConfigSkipLlmConfirmDefault:
    """Pre-LLM confirmation pause PR: `skip_llm_confirm` defaults to
    False so existing programmatic callers get the new pause unless
    they opt out. Non-TTY environments auto-suppress at runtime; the
    CLI's `--yes` flag opts out at construction time.
    """

    def _build(self, **overrides: object) -> WizardConfig:
        fields: dict[str, object] = {
            "source_url": "sqlite:///:memory:",
            "store_path": Path("/tmp/test-skip.db"),  # nosec B108 — never opened
            "host": "manual",
            "env_var_name": "SCHEMABRAIN_DATABASE_URL",
            "skip_index": False,
            "no_entities": False,
            "enrich": False,
            "entities_max_cost_usd": None,
            "assume_yes": False,
        }
        fields.update(overrides)
        return WizardConfig(**fields)  # type: ignore[arg-type]

    def test_skip_llm_confirm_defaults_to_false(self) -> None:
        cfg = self._build()
        assert cfg.skip_llm_confirm is False

    def test_skip_llm_confirm_accepts_true(self) -> None:
        cfg = self._build(skip_llm_confirm=True)
        assert cfg.skip_llm_confirm is True


class TestPromptLlmConfirmation:
    """Pre-LLM confirmation pause PR: direct tests for the helper.

    Production flow gates this with `cfg.skip_llm_confirm` before
    calling, so these tests cover the helper's pure behavior:
    auto-bypass when stdin isn't a TTY, Enter → proceed, Ctrl-C / EOF
    → cancelled.
    """

    def test_returns_true_when_stdin_not_tty(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Default pytest stdin is non-TTY. Helper returns True without
        # printing anything.
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        result = wizard._prompt_llm_confirmation(stage_label="entities", cost_cap_usd=1.0)
        assert result is True
        captured = capsys.readouterr()
        assert captured.err == ""

    def test_proceeds_when_user_presses_enter(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        # input() returns the empty string on bare Enter.
        monkeypatch.setattr("builtins.input", lambda: "")
        result = wizard._prompt_llm_confirmation(stage_label="entities", cost_cap_usd=1.0)
        assert result is True
        captured = capsys.readouterr()
        # Prompt mentions both the stage label and the cap.
        assert "entities" in captured.err
        assert "$1.00" in captured.err
        assert "Press Enter" in captured.err
        assert "Ctrl-C" in captured.err

    def test_cancels_on_keyboard_interrupt(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

        def _raise_ki() -> str:
            raise KeyboardInterrupt

        monkeypatch.setattr("builtins.input", _raise_ki)
        result = wizard._prompt_llm_confirmation(stage_label="metrics", cost_cap_usd=0.5)
        assert result is False
        captured = capsys.readouterr()
        # The helper prints a trailing newline after ^C so the next
        # rendered line lands on a clean row.
        assert captured.err.endswith("\n")

    def test_cancels_on_eof(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Closing stdin mid-prompt (pipe closure) raises EOFError;
        # treat same as user cancellation.
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

        def _raise_eof() -> str:
            raise EOFError

        monkeypatch.setattr("builtins.input", _raise_eof)
        result = wizard._prompt_llm_confirmation(stage_label="metrics", cost_cap_usd=0.5)
        assert result is False

    def test_cost_cap_formatted_with_two_decimals(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda: "")
        wizard._prompt_llm_confirmation(stage_label="entities", cost_cap_usd=0.50)
        captured = capsys.readouterr()
        assert "$0.50" in captured.err

    def test_stage_label_baked_into_prompt(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda: "")
        wizard._prompt_llm_confirmation(stage_label="custom-stage", cost_cap_usd=2.0)
        captured = capsys.readouterr()
        assert "custom-stage" in captured.err


class TestWizardContextDbtManifestPath:
    """PR C: WizardContext.dbt_manifest_path defaults to None + is mutable."""

    def test_dbt_manifest_path_defaults_to_none(self) -> None:
        cfg = WizardConfig(
            source_url="sqlite:///:memory:",
            store_path=Path("/tmp/x.db"),  # nosec B108 — never opened
            host="manual",
            env_var_name="X",
            skip_index=False,
            no_entities=False,
            enrich=False,
            entities_max_cost_usd=None,
            assume_yes=False,
        )
        ctx = WizardContext(config=cfg)
        assert ctx.dbt_manifest_path is None

    def test_dbt_manifest_path_is_mutable(self) -> None:
        # `WizardContext` is intentionally non-frozen so stage 1 can
        # populate dbt_manifest_path as a back-channel.
        cfg = WizardConfig(
            source_url="sqlite:///:memory:",
            store_path=Path("/tmp/x.db"),  # nosec B108 — never opened
            host="manual",
            env_var_name="X",
            skip_index=False,
            no_entities=False,
            enrich=False,
            entities_max_cost_usd=None,
            assume_yes=False,
        )
        ctx = WizardContext(config=cfg)
        ctx.dbt_manifest_path = Path("/some/path/manifest.json")
        assert ctx.dbt_manifest_path == Path("/some/path/manifest.json")


# ----- _auto_detect_dbt_manifest ------------------------------------------


class TestAutoDetectDbtManifest:
    """PR C: filesystem probe for a compiled dbt manifest.

    Search order (per the helper's contract):
      1. $DBT_PROJECT_DIR/target/manifest.json if env set and file exists
      2. Walk cwd up to _DBT_DETECT_PARENT_LIMIT parents looking for
         a dbt_project.yml sentinel; if found, return
         <dir>/target/manifest.json if it exists
      3. None
    """

    def test_returns_none_when_no_dbt_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DBT_PROJECT_DIR", raising=False)
        # Empty tmp_path → no dbt_project.yml anywhere on the walk.
        assert wizard._auto_detect_dbt_manifest(cwd=tmp_path) is None

    def test_returns_manifest_when_present_in_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DBT_PROJECT_DIR", raising=False)
        (tmp_path / "dbt_project.yml").write_text("name: test_project")
        target = tmp_path / "target"
        target.mkdir()
        manifest = target / "manifest.json"
        manifest.write_text("{}")

        result = wizard._auto_detect_dbt_manifest(cwd=tmp_path)

        assert result == manifest

    def test_returns_none_when_dbt_project_yml_but_no_compiled_manifest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # dbt_project.yml is present but `dbt compile` hasn't run yet.
        # The helper must NOT return the missing path (it'd crash
        # parse_dbt_manifest downstream); instead, fall through to None
        # so the wizard runs LLM-suggest.
        monkeypatch.delenv("DBT_PROJECT_DIR", raising=False)
        (tmp_path / "dbt_project.yml").write_text("name: test_project")
        # Note: no target/manifest.json

        assert wizard._auto_detect_dbt_manifest(cwd=tmp_path) is None

    def test_walks_up_to_parent_limit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # dbt_project.yml at tmp_path/, cwd at tmp_path/a/b/c (3 levels
        # below). The walk should find it.
        monkeypatch.delenv("DBT_PROJECT_DIR", raising=False)
        (tmp_path / "dbt_project.yml").write_text("name: test_project")
        target = tmp_path / "target"
        target.mkdir()
        manifest = target / "manifest.json"
        manifest.write_text("{}")
        nested = tmp_path / "a" / "b" / "c"
        nested.mkdir(parents=True)

        assert wizard._auto_detect_dbt_manifest(cwd=nested) == manifest

    def test_does_not_walk_beyond_parent_limit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # dbt_project.yml at tmp_path/, cwd 4 levels below.
        # _DBT_DETECT_PARENT_LIMIT is 3; the walk shouldn't reach it.
        monkeypatch.delenv("DBT_PROJECT_DIR", raising=False)
        (tmp_path / "dbt_project.yml").write_text("name: test_project")
        target = tmp_path / "target"
        target.mkdir()
        (target / "manifest.json").write_text("{}")
        too_deep = tmp_path / "a" / "b" / "c" / "d"
        too_deep.mkdir(parents=True)

        assert wizard._auto_detect_dbt_manifest(cwd=too_deep) is None

    def test_env_var_overrides_cwd_walk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # $DBT_PROJECT_DIR points at a different project; cwd has its
        # own dbt_project.yml. The env-var-derived path wins.
        env_project = tmp_path / "env_project"
        env_project.mkdir()
        env_target = env_project / "target"
        env_target.mkdir()
        env_manifest = env_target / "manifest.json"
        env_manifest.write_text("{}")

        cwd_project = tmp_path / "cwd_project"
        cwd_project.mkdir()
        (cwd_project / "dbt_project.yml").write_text("name: cwd_project")
        (cwd_project / "target").mkdir()
        (cwd_project / "target" / "manifest.json").write_text("{}")

        monkeypatch.setenv("DBT_PROJECT_DIR", str(env_project))

        assert wizard._auto_detect_dbt_manifest(cwd=cwd_project) == env_manifest

    def test_env_var_with_missing_manifest_falls_through_to_cwd_walk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # $DBT_PROJECT_DIR set but no compiled manifest there. Fall
        # through to cwd walk rather than refuse — env may be set as a
        # global default while the user works elsewhere.
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        monkeypatch.setenv("DBT_PROJECT_DIR", str(empty_dir))

        cwd_project = tmp_path / "cwd_project"
        cwd_project.mkdir()
        (cwd_project / "dbt_project.yml").write_text("name: cwd_project")
        (cwd_project / "target").mkdir()
        cwd_manifest = cwd_project / "target" / "manifest.json"
        cwd_manifest.write_text("{}")

        assert wizard._auto_detect_dbt_manifest(cwd=cwd_project) == cwd_manifest


# ----- _stage_wire_host ----------------------------------------------------


def _snippet_for(config: WizardConfig) -> SchemabrainSnippet:
    return SchemabrainSnippet(
        command="uvx",
        args=(
            "schemabrain==0.2.0a1",
            "serve",
            "--url-env",
            config.env_var_name,
            "--store-path",
            str(config.store_path.resolve()),
        ),
        env={config.env_var_name: config.source_url},
    )


class TestStageWireHost:
    @pytest.mark.parametrize(
        ("state", "expected_substr"),
        [
            ("written", "wrote schemabrain entry"),
            ("unchanged", "no changes"),
            ("shell_out_succeeded", "registered schemabrain with Claude Code"),
            ("shell_out_failed", "Claude Code registration failed"),
            ("printed_only", "manual mode"),
        ],
    )
    def test_state_to_message_mapping(
        self,
        base_config: WizardConfig,
        monkeypatch: pytest.MonkeyPatch,
        state: str,
        expected_substr: str,
    ) -> None:
        canned = InitResult(
            host="claude-desktop",
            snippet=_snippet_for(base_config),
            state=state,  # type: ignore[arg-type]
            config_path=Path("/tmp/cfg.json"),  # nosec B108 — fake path for test
            backup_made=False,
        )
        captured: dict[str, object] = {}

        def fake_init(**kwargs: object) -> InitResult:
            captured.update(kwargs)
            return canned

        monkeypatch.setattr(wizard, "init", fake_init)

        ctx = WizardContext(config=base_config)
        outcome = wizard._stage_wire_host(ctx)

        assert outcome.status == "done"
        assert expected_substr in outcome.message
        assert ctx.host_install_result is canned
        # The wizard always passes skip_index=True because stage 2
        # has already managed the index/skip choice.
        assert captured["skip_index"] is True

    def test_failed_when_init_refuses(
        self, base_config: WizardConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        err = GuidedError(
            kind="init_host_unavailable",
            message="Claude Desktop config directory not found",
            why="not installed at expected path",
            fix="install Claude Desktop or use --host manual",
            next_step=None,
        )

        def _raise(**_kwargs: object) -> InitResult:
            raise InitRefusal(err)

        monkeypatch.setattr(wizard, "init", _raise)

        ctx = WizardContext(config=base_config)
        outcome = wizard._stage_wire_host(ctx)

        assert outcome.status == "failed"
        assert outcome.message == "Claude Desktop config directory not found"
        assert ctx.host_install_result is None


# ----- _stage_next_step ----------------------------------------------------


class TestStageNextStep:
    def test_always_done(self, base_config: WizardConfig) -> None:
        ctx = WizardContext(config=base_config)
        outcome = wizard._stage_next_step(ctx)

        assert outcome.stage == 7
        assert outcome.status == "done"
        # Brief status line — the renderer's closing block carries the
        # actionable next-step copy.
        assert outcome.message == "Ready"


# ----- DEFAULT_STAGES contract --------------------------------------------


class TestDefaultStages:
    def test_seven_stages_in_order(self) -> None:
        assert [s.stage for s in DEFAULT_STAGES] == [1, 2, 3, 4, 5, 6, 7]

    def test_stage_names_match_contract(self) -> None:
        assert [s.name for s in DEFAULT_STAGES] == [
            "source_check",
            "index",
            "entities",
            "metrics",
            "joins",
            "wire_host",
            "next_step",
        ]

    def test_stages_3_4_5_are_continue_on_fail(self) -> None:
        # Entity + metric + canonical-join suggestion are all
        # best-effort: a failure in any of them records a `failed`
        # outcome but lets the wizard wire the host and print the
        # next step.
        abort_flags = [s.abort_on_fail for s in DEFAULT_STAGES]
        assert abort_flags == [True, True, False, False, False, True, True]


# ----- module exports ------------------------------------------------------


class TestModuleExports:
    def test_all_lists_public_names(self) -> None:
        # Defensive: a future contributor adding a name to `__all__`
        # without exporting it gets caught here.
        for name in wizard.__all__:
            assert hasattr(wizard, name), f"__all__ names {name!r} but wizard lacks it"


# ----- helper: stage handler invariants -----------------------------------


class TestStageHandlerSignature:
    def test_handlers_accept_wizard_context(self) -> None:
        # Light smoke that the canonical handlers all accept a
        # WizardContext and return a StageOutcome.
        ctx = WizardContext(
            config=WizardConfig(
                source_url="sqlite:///:memory:",
                store_path=Path("/tmp/never-touched.db"),  # nosec B108 — never opened
                host="manual",
                env_var_name="SCHEMABRAIN_DATABASE_URL",
                skip_index=True,
                no_entities=True,
                enrich=False,
                entities_max_cost_usd=None,
                assume_yes=True,
                no_metrics=True,
                metrics_max_cost_usd=None,
                no_joins=True,
            )
        )

        # Only the cheap stages that don't touch the network here —
        # source_check + wire_host are exercised elsewhere with
        # monkeypatched dependencies.
        idx_outcome = wizard._stage_index(ctx)
        ent_outcome = wizard._stage_entities(ctx)
        met_outcome = wizard._stage_metrics(ctx)
        join_outcome = wizard._stage_joins(ctx)
        next_outcome = wizard._stage_next_step(ctx)

        for outcome in (idx_outcome, ent_outcome, met_outcome, join_outcome, next_outcome):
            assert isinstance(outcome, StageOutcome)


# ----- ensure unused fixture parameter doesn't bite ------------------------


def test_run_wizard_passes_context_to_handlers(base_config: WizardConfig) -> None:
    """The orchestrator MUST pass the same `WizardContext` to every
    stage handler so prior outcomes + the host-install slot are
    visible across the pipeline.
    """
    seen_contexts: list[WizardContext] = []

    def handler(ctx: WizardContext) -> StageOutcome:
        seen_contexts.append(ctx)
        return StageOutcome(stage=1, name="probe", status="done", message="ok")

    stages: Sequence[WizardStage] = [
        WizardStage(stage=1, name="probe1", handler=handler, abort_on_fail=True),
        WizardStage(stage=2, name="probe2", handler=handler, abort_on_fail=True),
    ]
    run_wizard(base_config, stages=stages)

    assert len(seen_contexts) == 2
    assert seen_contexts[0] is seen_contexts[1]
