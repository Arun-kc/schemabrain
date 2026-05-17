"""Tests for `schemabrain.setup.wizard`.

Covers:

  - `StageOutcome` validation invariants
  - `WizardResult.aborted_at` property
  - `run_wizard` state machine (happy path + abort-on-fail per stage +
    continue-on-fail at stage 3)
  - Production stage handlers (`_stage_source_check`, `_stage_index`,
    `_stage_entities`, `_stage_wire_host`, `_stage_next_step`) via
    monkeypatched dependencies — no real database is required.
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
        first_fail = StageOutcome(stage=2, name="b", status="failed", message="boom")
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
        # the default stage list reaches stage 5 with this stubbing in place.
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

        result = run_default_wizard(base_config)
        assert result.aborted is False
        assert len(result.outcomes) == 5
        assert [o.name for o in result.outcomes] == [
            "source_check",
            "index",
            "entities",
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
                )

            return WizardStage(stage=stage_num, name=name, handler=handler, abort_on_fail=True)

        stages = [
            _tracked(1, "stage1", "done"),
            _tracked(2, "stage2", "failed"),
            _tracked(3, "stage3", "done"),
        ]
        run_wizard(base_config, stages=stages)
        assert tracker.seen == ["stage1", "stage2"]


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
        assert outcome.next_step is None


# ----- _stage_index (stub at this commit) ----------------------------------


class TestStageIndexStub:
    def test_returns_skipped_with_recovery_hint(self, base_config: WizardConfig) -> None:
        ctx = WizardContext(config=base_config)
        outcome = wizard._stage_index(ctx)

        assert outcome.stage == 2
        assert outcome.status == "skipped"
        assert "schemabrain index" in (outcome.next_step or "")


# ----- _stage_entities (stub at this commit) -------------------------------


class TestStageEntitiesStub:
    def test_returns_skipped_with_recovery_hint(self, base_config: WizardConfig) -> None:
        ctx = WizardContext(config=base_config)
        outcome = wizard._stage_entities(ctx)

        assert outcome.stage == 3
        assert outcome.status == "skipped"
        assert "entities suggest" in (outcome.next_step or "")


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

        assert outcome.stage == 5
        assert outcome.status == "done"
        assert "restart" in outcome.message


# ----- DEFAULT_STAGES contract --------------------------------------------


class TestDefaultStages:
    def test_five_stages_in_order(self) -> None:
        assert [s.stage for s in DEFAULT_STAGES] == [1, 2, 3, 4, 5]

    def test_stage_names_match_contract(self) -> None:
        assert [s.name for s in DEFAULT_STAGES] == [
            "source_check",
            "index",
            "entities",
            "wire_host",
            "next_step",
        ]

    def test_only_stage_3_is_continue_on_fail(self) -> None:
        abort_flags = [s.abort_on_fail for s in DEFAULT_STAGES]
        assert abort_flags == [True, True, False, True, True]


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
            )
        )

        # Only the cheap stages that don't touch the network here —
        # source_check + wire_host are exercised elsewhere with
        # monkeypatched dependencies.
        idx_outcome = wizard._stage_index(ctx)
        ent_outcome = wizard._stage_entities(ctx)
        next_outcome = wizard._stage_next_step(ctx)

        for outcome in (idx_outcome, ent_outcome, next_outcome):
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
