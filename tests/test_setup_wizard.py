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

    def test_failed_for_non_postgres_source(self, base_config: WizardConfig) -> None:
        # base_config uses sqlite:///:memory: — wizard's stage-2 only
        # supports Postgres sources at v1.
        outcome = wizard._stage_index(WizardContext(config=base_config))

        assert outcome.status == "failed"
        assert "Postgres" in outcome.message
        assert outcome.next_step is not None
        assert "--skip-index" in outcome.next_step

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
