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
        # the default stage list reaches stage 5 with this stubbing in place.
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
        # `_failed_from_refusal` substitutes the default diagnostic
        # hint when the GuidedError carries `next_step=None`, because
        # `failed` outcomes are required to expose a recovery path.
        assert outcome.next_step is not None
        assert "schemabrain doctor" in outcome.next_step


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
        # Brief status line — the renderer's closing block carries the
        # actionable next-step copy.
        assert outcome.message == "Ready"


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
