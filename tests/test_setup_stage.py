"""Tests for `schemabrain.setup.setup_stage` — the pre-wizard demo
vs own-DB fork prompt that the day-one UX overhaul adds in front of
the existing 7-stage wizard.

The module is small (~80 LOC of code) but the contract is load-
bearing — it's the first thing a new user sees when running
`schemabrain init`. Tests pin:

* The fork prompt defaults to ``[2]`` (demo) — UX research showed
  this is the safer default for new users.
* The Docker preflight short-circuits to the own-DB path when
  Docker isn't on PATH (instead of dying).
* The demo path returns the pinned ``DEMO_DATABASE_URL`` after the
  user presses Enter.
* The own-DB path delegates to ``prompt_for_url`` so the prompt
  copy stays consistent with the 5 post-init commands.
* ``KeyboardInterrupt`` propagates verbatim (caller routes to
  exit-130).
"""

from __future__ import annotations

import io

import pytest

from schemabrain._ui import make_console
from schemabrain.setup.setup_stage import (
    DEMO_DATABASE_URL,
    DEMO_DOCKER_RUN_COMMAND,
    DEMO_FIXTURE_LOAD_COMMAND,
    detect_docker,
    prompt_for_init_setup,
    prompt_for_pii_block,
)


class TestDetectDocker:
    def test_returns_none_when_docker_on_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # When the user has Docker installed, the demo path is viable
        # and no fallback explanation is needed.
        monkeypatch.setattr("shutil.which", lambda cmd: "/usr/local/bin/docker")
        assert detect_docker() is None

    def test_returns_explanation_when_docker_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # When Docker isn't installed, the explanation must be
        # actionable — install link + fallback to option 1. A bare
        # "docker not found" wouldn't tell the user what to do next.
        monkeypatch.setattr("shutil.which", lambda cmd: None)
        explanation = detect_docker()
        assert explanation is not None
        assert "Docker" in explanation
        # Must include the install URL — the user shouldn't have to
        # google "where do I get docker".
        assert "docker.com" in explanation
        # Must mention the fallback so the user knows they're not
        # dead-ended.
        assert "option 1" in explanation


class TestPromptForPiiBlock:
    """The interactive PII-block prompt added by the init wizard.

    Prior behavior was to silently bake ("contact",) into the host
    snippet without asking. The prompt surfaces the choice so an
    operator can opt into the full v1 category set OR opt out
    entirely for dev/synthetic databases — without scripted flows
    (-y / piped stderr) ever blocking on stdin.
    """

    def test_default_recommended_returns_contact(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Pressing Enter at the prompt selects the recommended default —
        # `contact` only, matching the pre-prompt silent default so the
        # zero-effort path is unchanged for operators who don't read
        # prompt copy carefully.
        monkeypatch.setattr("rich.prompt.Prompt.ask", lambda *a, **kw: "1")
        result = prompt_for_pii_block(console=make_console(file=io.StringIO()))
        assert result == ("contact",)

    def test_all_categories_returns_full_v1_enum(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Option 2 fans out to every v1 PIICategory the classifier can
        # tag. Sorted output keeps the host-snippet shape deterministic
        # across machines (otherwise the comma-joined arg order would
        # flap based on frozenset iteration).
        from schemabrain.pii.categories import PII_CATEGORIES

        monkeypatch.setattr("rich.prompt.Prompt.ask", lambda *a, **kw: "2")
        result = prompt_for_pii_block(console=make_console(file=io.StringIO()))
        assert result == tuple(sorted(PII_CATEGORIES))
        # Must include the universally-sensitive category (`contact`)
        # AND at least one rarer category (`government_id`) so a
        # future enum addition that misses sorting/inclusion surfaces
        # here rather than silently shipping a partial set.
        assert "contact" in result
        assert "government_id" in result

    def test_none_choice_returns_empty_tuple(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Option 3 = dev/synthetic database escape hatch. Empty tuple
        # signals "no `--pii-block` flag in the host snippet"; the
        # server then runs with PII enforcement off.
        monkeypatch.setattr("rich.prompt.Prompt.ask", lambda *a, **kw: "3")
        result = prompt_for_pii_block(console=make_console(file=io.StringIO()))
        assert result == ()


class TestPromptForInitSetup:
    @pytest.fixture(autouse=True)
    def _disable_auto_docker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # D2 added auto-`docker run` to the demo path. These existing
        # tests pre-date D2 and pin the manual-recipe UX (the path
        # that fires when auto-docker fails). Short-circuit
        # `_try_auto_docker_demo` to False so they exercise the
        # fallback path verbatim — same intent, no real subprocess
        # work. The auto-docker path itself has its own dedicated
        # test class below (`TestAutoDockerDemoPath`).
        monkeypatch.setattr(
            "schemabrain.setup.setup_stage._try_auto_docker_demo",
            lambda *, console: False,
        )

    def test_demo_choice_returns_pinned_demo_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Default path — user accepts the demo fork [2] and presses
        # Enter at the wait-for-Postgres prompt. Returns the pinned
        # demo URL so the wizard's existing stage 1+2 can index
        # against it.
        monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/docker")
        responses = iter(["2", ""])
        monkeypatch.setattr("rich.prompt.Prompt.ask", lambda *a, **kw: next(responses))
        result = prompt_for_init_setup(console=make_console(file=io.StringIO()))
        assert result == DEMO_DATABASE_URL

    def test_demo_choice_falls_back_to_own_db_when_docker_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Critical UX: if the user picks demo but doesn't have Docker,
        # we don't dead-end them. We print the install explanation
        # AND fall through to the own-DB URL prompt so they can still
        # complete setup with their existing Postgres.
        monkeypatch.setattr("shutil.which", lambda cmd: None)
        responses = iter(["2", "postgresql://user:pw@host/db"])
        monkeypatch.setattr("rich.prompt.Prompt.ask", lambda *a, **kw: next(responses))
        result = prompt_for_init_setup(console=make_console(file=io.StringIO()))
        # Falls back to whatever the user typed at the own-DB prompt.
        assert result == "postgresql://user:pw@host/db"

    def test_own_db_choice_delegates_to_prompt_for_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # User picks [1] — own-DB path. Returns whatever
        # prompt_for_url returns, including the user's URL verbatim
        # (the silent +psycopg rewrite happens later in _resolve_url).
        responses = iter(["1", "postgresql://app:pass@db:5432/analytics"])
        monkeypatch.setattr("rich.prompt.Prompt.ask", lambda *a, **kw: next(responses))
        result = prompt_for_init_setup(console=make_console(file=io.StringIO()))
        assert result == "postgresql://app:pass@db:5432/analytics"

    def test_own_db_choice_empty_url_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # User picks [1] then presses Enter without typing a URL.
        # prompt_for_url returns None; we propagate None so the
        # caller falls back to _resolve_url_source's guided error.
        responses = iter(["1", ""])
        monkeypatch.setattr("rich.prompt.Prompt.ask", lambda *a, **kw: next(responses))
        assert prompt_for_init_setup(console=make_console(file=io.StringIO())) is None

    def test_demo_recipe_shows_exact_docker_run_command(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The user copies this command verbatim into another terminal.
        # If the wizard mangles or rewrites it, they hit a Docker
        # error they can't debug. Pin the exact string.
        monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/docker")
        buf = io.StringIO()
        responses = iter(["2", ""])
        monkeypatch.setattr("rich.prompt.Prompt.ask", lambda *a, **kw: next(responses))
        prompt_for_init_setup(console=make_console(file=buf, force_terminal=False, width=120))
        out = buf.getvalue()
        assert DEMO_DOCKER_RUN_COMMAND in out
        # The fixture-load recipe must also appear — without it,
        # the user has Postgres up but no tables and the wizard
        # later finds an empty schema.
        assert "psql" in out

    def test_fork_prompt_default_is_demo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # UX research showed new users without their own DB are the
        # larger cohort. Default [2] (demo) is the safer pick for
        # the "press Enter" path. An experienced user picks 1
        # deliberately by reading the option labels.
        captured: dict[str, object] = {}

        def fake_ask(*args: object, **kwargs: object) -> str:
            captured.update(kwargs)
            return ""  # Empty response triggers the default

        monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/docker")
        # Two prompts fire: fork choice, then wait-for-Postgres.
        # The first one's default IS the contract being tested.
        responses = iter(["2", ""])

        def routing_ask(*args: object, **kwargs: object) -> str:
            # Capture only the FIRST Prompt.ask invocation (the
            # fork prompt) — that's the one whose default matters
            # for this test.
            if "default" in kwargs and not captured:
                captured["default"] = kwargs["default"]
            return next(responses)

        monkeypatch.setattr("rich.prompt.Prompt.ask", routing_ask)
        prompt_for_init_setup(console=make_console(file=io.StringIO()))
        assert captured.get("default") == "2"

    def test_keyboard_interrupt_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Ctrl-C at the setup prompt is a clean abort. The caller
        # (_cmd_init) translates this to exit-130. The helper must
        # not catch and silently swallow.
        def raising_ask(*args: object, **kwargs: object) -> str:
            raise KeyboardInterrupt

        monkeypatch.setattr("rich.prompt.Prompt.ask", raising_ask)
        with pytest.raises(KeyboardInterrupt):
            prompt_for_init_setup(console=make_console(file=io.StringIO()))


class TestDemoCommandsConsistency:
    """The DEMO_DATABASE_URL, DEMO_DOCKER_RUN_COMMAND, and
    DEMO_FIXTURE_LOAD_COMMAND constants bake the same host/port/
    password into different forms. If they drift, the user copies
    a docker run command that doesn't match the URL the wizard
    returns and gets a confusing connection failure.
    """

    def test_demo_url_matches_docker_run_port_and_password(self) -> None:
        # Both must reference port 5433 and password `local`.
        assert "5433" in DEMO_DATABASE_URL
        assert ":local@" in DEMO_DATABASE_URL
        assert "5433:5432" in DEMO_DOCKER_RUN_COMMAND
        assert "POSTGRES_PASSWORD=local" in DEMO_DOCKER_RUN_COMMAND

    def test_fixture_load_command_targets_demo_port_and_password(self) -> None:
        # The psql command must hit the same port/password as the
        # docker run + URL. PGPASSWORD env var must match the
        # container's POSTGRES_PASSWORD.
        assert "5433" in DEMO_FIXTURE_LOAD_COMMAND
        assert "PGPASSWORD=local" in DEMO_FIXTURE_LOAD_COMMAND
        # Must reference the bundled fixture path.
        assert "ecommerce.sql" in DEMO_FIXTURE_LOAD_COMMAND


class TestAutoDockerDemoPath:
    """D2: auto-`docker run` orchestration for the stage 0 demo path.

    Three subprocess helpers (`_docker_run_demo_postgres`,
    `_wait_for_postgres_ready`, `_docker_load_fixture`) compose via
    `_try_auto_docker_demo` into the end-to-end flow. On any failure,
    the orchestrator returns False and `_handle_demo_path` falls
    through to the pre-D2 manual copy-paste recipe — auto-docker
    augments the safe path, never replaces it.
    """

    def test_orchestrator_returns_true_when_all_three_steps_succeed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # End-to-end happy path: container starts, readiness probe
        # connects, fixture loads. Orchestrator returns True so
        # `_handle_demo_path` skips the manual recipe.
        from schemabrain.setup import setup_stage

        monkeypatch.setattr(setup_stage, "_docker_run_demo_postgres", lambda *, console: True)
        monkeypatch.setattr(setup_stage, "_wait_for_postgres_ready", lambda **_kw: True)
        monkeypatch.setattr(setup_stage, "_docker_load_fixture", lambda *, console: True)

        assert setup_stage._try_auto_docker_demo(console=make_console(file=io.StringIO())) is True

    def test_orchestrator_short_circuits_on_docker_run_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # First step fails: `_wait_for_postgres_ready` and
        # `_docker_load_fixture` must NOT be called. The orchestrator
        # is fail-fast — a partial setup (container up but fixture
        # missing) is more confusing than clean fall-through to manual.
        from schemabrain.setup import setup_stage

        wait_calls: list[object] = []
        fixture_calls: list[object] = []
        monkeypatch.setattr(setup_stage, "_docker_run_demo_postgres", lambda *, console: False)
        monkeypatch.setattr(
            setup_stage,
            "_wait_for_postgres_ready",
            lambda **kw: wait_calls.append(kw) or True,
        )
        monkeypatch.setattr(
            setup_stage,
            "_docker_load_fixture",
            lambda *, console: fixture_calls.append(console) or True,
        )

        result = setup_stage._try_auto_docker_demo(console=make_console(file=io.StringIO()))
        assert result is False
        assert wait_calls == []
        assert fixture_calls == []

    def test_orchestrator_short_circuits_on_readiness_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Container starts but Postgres never becomes ready (timeout).
        # Fixture-load must NOT be attempted — loading into a
        # not-yet-ready DB would surface a confusing psql error.
        from schemabrain.setup import setup_stage

        fixture_calls: list[object] = []
        monkeypatch.setattr(setup_stage, "_docker_run_demo_postgres", lambda *, console: True)
        monkeypatch.setattr(setup_stage, "_wait_for_postgres_ready", lambda **_kw: False)
        monkeypatch.setattr(
            setup_stage,
            "_docker_load_fixture",
            lambda *, console: fixture_calls.append(console) or True,
        )

        assert setup_stage._try_auto_docker_demo(console=make_console(file=io.StringIO())) is False
        assert fixture_calls == []

    def test_orchestrator_returns_false_on_fixture_load_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # All steps run; last one fails. Returns False so caller
        # falls through to manual recipe — the operator may have
        # a partial state (container running, no fixture) but the
        # manual psql command can complete the setup.
        from schemabrain.setup import setup_stage

        monkeypatch.setattr(setup_stage, "_docker_run_demo_postgres", lambda *, console: True)
        monkeypatch.setattr(setup_stage, "_wait_for_postgres_ready", lambda **_kw: True)
        monkeypatch.setattr(setup_stage, "_docker_load_fixture", lambda *, console: False)

        assert setup_stage._try_auto_docker_demo(console=make_console(file=io.StringIO())) is False

    def test_demo_path_skips_manual_recipe_when_auto_docker_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # End-to-end: when auto-docker succeeds, `_handle_demo_path`
        # must NOT print the manual recipe block. The "Falling back
        # to manual setup" line is the user-visible signal the path
        # is degraded; its absence is the success contract.
        from schemabrain.setup import setup_stage

        monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/docker")
        monkeypatch.setattr(setup_stage, "_try_auto_docker_demo", lambda *, console: True)
        responses = iter(["2"])
        monkeypatch.setattr("rich.prompt.Prompt.ask", lambda *a, **kw: next(responses))

        buf = io.StringIO()
        result = prompt_for_init_setup(
            console=make_console(file=buf, force_terminal=False, width=120)
        )
        assert result == DEMO_DATABASE_URL
        out = buf.getvalue()
        # No fallback signal, no manual recipe.
        assert "Falling back to manual setup" not in out
        assert DEMO_DOCKER_RUN_COMMAND not in out
        # Success line still appears.
        assert "Demo Postgres ready" in out


class TestDockerRunDemoPostgres:
    """D2: `_docker_run_demo_postgres` — fresh-run + reuse paths."""

    def test_reuses_running_container_without_docker_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # If `sb-demo-pg` is already running, skip `docker run` —
        # idempotent re-invocation of the wizard shouldn't fail
        # on the name conflict that a fresh `docker run` would
        # hit.
        from schemabrain.setup import setup_stage

        argv_log: list[list[str]] = []

        def fake_safe_subprocess(argv, *, timeout_s):  # type: ignore[no-untyped-def]
            argv_log.append(argv)
            # `docker inspect ... .State.Running` → "true\n" + rc=0
            from subprocess import CompletedProcess

            return CompletedProcess(args=argv, returncode=0, stdout="true\n", stderr="")

        monkeypatch.setattr(setup_stage, "_safe_subprocess", fake_safe_subprocess)

        buf = io.StringIO()
        assert (
            setup_stage._docker_run_demo_postgres(
                console=make_console(file=buf, force_terminal=False, width=120)
            )
            is True
        )
        # Exactly one subprocess call — the inspect probe.
        assert len(argv_log) == 1
        assert argv_log[0][:2] == ["docker", "inspect"]
        # No `docker run` argv issued.
        assert all("run" not in argv for argv in argv_log)
        assert "already running" in buf.getvalue()

    def test_starts_stopped_container_via_docker_start(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Container exists but is stopped (.State.Running == "false").
        # `docker start <name>` is the idempotent restart.
        from subprocess import CompletedProcess

        from schemabrain.setup import setup_stage

        argv_log: list[list[str]] = []

        def fake_safe_subprocess(argv, *, timeout_s):  # type: ignore[no-untyped-def]
            argv_log.append(argv)
            if argv[:2] == ["docker", "inspect"]:
                return CompletedProcess(args=argv, returncode=0, stdout="false\n", stderr="")
            if argv[:2] == ["docker", "start"]:
                return CompletedProcess(args=argv, returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected argv: {argv}")

        monkeypatch.setattr(setup_stage, "_safe_subprocess", fake_safe_subprocess)

        buf = io.StringIO()
        assert (
            setup_stage._docker_run_demo_postgres(
                console=make_console(file=buf, force_terminal=False, width=120)
            )
            is True
        )
        assert any(argv[:2] == ["docker", "start"] for argv in argv_log)
        assert "Started existing container" in buf.getvalue()

    def test_runs_fresh_container_when_none_exists(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # `docker inspect` returns rc=1 (no such object). Fresh
        # `docker run` is issued with the pinned image + port + env.
        from subprocess import CompletedProcess

        from schemabrain.setup import setup_stage

        argv_log: list[list[str]] = []

        def fake_safe_subprocess(argv, *, timeout_s):  # type: ignore[no-untyped-def]
            argv_log.append(argv)
            if argv[:2] == ["docker", "inspect"]:
                return CompletedProcess(args=argv, returncode=1, stdout="", stderr="No such object")
            if argv[:2] == ["docker", "run"]:
                return CompletedProcess(args=argv, returncode=0, stdout="container_id\n", stderr="")
            raise AssertionError(f"unexpected argv: {argv}")

        monkeypatch.setattr(setup_stage, "_safe_subprocess", fake_safe_subprocess)

        buf = io.StringIO()
        assert (
            setup_stage._docker_run_demo_postgres(
                console=make_console(file=buf, force_terminal=False, width=120)
            )
            is True
        )
        # Run argv pins the demo's host/port/password/image.
        run_argv = next(argv for argv in argv_log if argv[:2] == ["docker", "run"])
        assert "127.0.0.1:5433:5432" in run_argv
        assert "POSTGRES_PASSWORD=local" in run_argv
        assert "postgres:16-alpine" in run_argv
        assert "sb-demo-pg" in run_argv

    def test_returns_false_when_docker_run_exits_non_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Daemon down / port conflict / perm denied → non-zero exit.
        # Helper surfaces the stderr first line so the operator
        # has a real diagnostic before the fallback recipe.
        from subprocess import CompletedProcess

        from schemabrain.setup import setup_stage

        def fake_safe_subprocess(argv, *, timeout_s):  # type: ignore[no-untyped-def]
            if argv[:2] == ["docker", "inspect"]:
                return CompletedProcess(args=argv, returncode=1, stdout="", stderr="No such object")
            return CompletedProcess(
                args=argv,
                returncode=125,
                stdout="",
                stderr="docker: Error response from daemon: port is already allocated\n",
            )

        monkeypatch.setattr(setup_stage, "_safe_subprocess", fake_safe_subprocess)

        buf = io.StringIO()
        assert (
            setup_stage._docker_run_demo_postgres(
                console=make_console(file=buf, force_terminal=False, width=120)
            )
            is False
        )
        out = buf.getvalue()
        assert "couldn't start Postgres container" in out
        # First stderr line surfaced verbatim.
        assert "port is already allocated" in out

    def test_returns_false_when_subprocess_helper_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `_safe_subprocess` returns None on `FileNotFoundError`
        # (docker missing) or `TimeoutExpired` (hung). Helper must
        # not crash on the None — translated to False + explanation.
        from schemabrain.setup import setup_stage

        monkeypatch.setattr(setup_stage, "_safe_subprocess", lambda argv, *, timeout_s: None)

        buf = io.StringIO()
        assert (
            setup_stage._docker_run_demo_postgres(
                console=make_console(file=buf, force_terminal=False, width=120)
            )
            is False
        )
        assert "subprocess didn't return" in buf.getvalue()


class TestWaitForPostgresReady:
    """D2: `_wait_for_postgres_ready` — connect-loop with driver rewrite."""

    def test_returns_true_on_first_successful_connect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Connection succeeds on first iteration → return True
        # immediately. No `time.sleep` should fire.
        from schemabrain.setup import setup_stage

        sleeps: list[float] = []
        monkeypatch.setattr(setup_stage.time, "sleep", lambda s: sleeps.append(s))

        class _FakeConn:
            def execute(self, *a):
                return None

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

        class _FakeEngine:
            def connect(self):
                return _FakeConn()

            def dispose(self):
                pass

        monkeypatch.setattr("sqlalchemy.create_engine", lambda *a, **kw: _FakeEngine())

        buf = io.StringIO()
        result = setup_stage._wait_for_postgres_ready(
            url="postgresql://u:p@h/d",
            console=make_console(file=buf, force_terminal=False, width=120),
            timeout_s=5,
        )
        assert result is True
        assert sleeps == []
        assert "Postgres ready" in buf.getvalue()

    def test_breaks_early_on_argument_error_without_full_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Regression coverage: a malformed URL
        # is deterministic — polling for 30s won't help and the
        # operator deserves the error fast. Pre-fold, the broad
        # `except Exception` caught ArgumentError, set last_error,
        # then slept and retried — burning the full 30s for a
        # deterministic failure. Now ArgumentError breaks the loop
        # immediately.
        from sqlalchemy.exc import ArgumentError

        from schemabrain.setup import setup_stage

        sleeps: list[float] = []
        monkeypatch.setattr(setup_stage.time, "sleep", lambda s: sleeps.append(s))

        def always_arg_error(*a, **kw):
            raise ArgumentError("Could not parse SQLAlchemy URL")

        monkeypatch.setattr("sqlalchemy.create_engine", always_arg_error)

        buf = io.StringIO()
        result = setup_stage._wait_for_postgres_ready(
            url="postgresql://malformed",
            console=make_console(file=buf, force_terminal=False, width=120),
            timeout_s=30,  # generous timeout — fix should bail FAST, not wait
            interval_s=0.01,
        )
        assert result is False
        # The break should fire before any sleep at all.
        assert sleeps == []
        # Operator sees the real error in the timeout message.
        assert "ArgumentError" in buf.getvalue()

    def test_returns_false_after_timeout_with_persistent_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Connection always fails. Loop should respect `timeout_s`
        # and return False. Use tiny interval + timeout so the
        # test runs in milliseconds.
        from sqlalchemy.exc import OperationalError

        from schemabrain.setup import setup_stage

        def always_fail(*a, **kw):
            raise OperationalError("simulated", None, Exception("connection refused"))

        monkeypatch.setattr("sqlalchemy.create_engine", always_fail)

        buf = io.StringIO()
        result = setup_stage._wait_for_postgres_ready(
            url="postgresql://u:p@h/d",
            console=make_console(file=buf, force_terminal=False, width=120),
            timeout_s=0.05,
            interval_s=0.01,
        )
        assert result is False
        out = buf.getvalue()
        assert "didn't become ready" in out
        # Surfaces the last error so the operator has a clue.
        assert "connection refused" in out

    def test_rewrites_bare_postgresql_url_to_psycopg_driver(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Bare `postgresql://` would default SQLAlchemy to psycopg2
        # (not installed). Helper must rewrite to `postgresql+psycopg://`
        # so the connect actually uses the v3 driver SchemaBrain ships.
        from schemabrain.setup import setup_stage

        captured_urls: list[str] = []

        class _FakeEngine:
            def connect(self):
                raise RuntimeError("don't care — only the URL matters")

            def dispose(self):
                pass

        def fake_create_engine(url, *a, **kw):
            captured_urls.append(url)
            return _FakeEngine()

        monkeypatch.setattr("sqlalchemy.create_engine", fake_create_engine)
        monkeypatch.setattr(setup_stage.time, "sleep", lambda s: None)

        buf = io.StringIO()
        setup_stage._wait_for_postgres_ready(
            url="postgresql://postgres:local@localhost:5433/postgres",
            console=make_console(file=buf, force_terminal=False, width=120),
            timeout_s=0.01,
            interval_s=0.001,
        )
        # At least one create_engine call; URL must carry the driver suffix.
        assert len(captured_urls) >= 1
        assert all(url.startswith("postgresql+psycopg://") for url in captured_urls)


class TestDockerLoadFixture:
    """D2: `_docker_load_fixture` — psql shell-out + fixture-path check."""

    def test_returns_false_when_bundled_fixture_unresolvable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `resolve_bundled_path` fails when the wheel is broken (or
        # someone monkeypatched the bundled-dirs out of existence).
        # The helper must print a helpful message — pointing at
        # `pip install --force-reinstall` — and refuse to fire a
        # docker subprocess that would error with a less helpful
        # signal. PR-6h: replaced the CWD-relative path check with
        # `resolve_bundled_path`, so the failure surface flipped
        # from "file not found at $CWD" to "no bundled fixture
        # named X".
        from schemabrain.setup import setup_stage

        monkeypatch.setattr(
            setup_stage,
            "resolve_bundled_path",
            lambda name: (_ for _ in ()).throw(
                FileNotFoundError(f"no bundled fixture named {name!r}; available: []")
            ),
        )
        # Guard: no real subprocess should fire.
        monkeypatch.setattr(
            setup_stage,
            "_safe_subprocess",
            lambda *a, **kw: (_ for _ in ()).throw(
                AssertionError("subprocess should not run when fixture is missing")
            ),
        )

        buf = io.StringIO()
        assert (
            setup_stage._docker_load_fixture(
                console=make_console(file=buf, force_terminal=False, width=120)
            )
            is False
        )
        output = buf.getvalue()
        assert "Couldn't resolve bundled ecommerce fixture" in output
        assert "force-reinstall" in output

    def test_returns_true_when_psql_subprocess_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        from subprocess import CompletedProcess

        from schemabrain.setup import setup_stage

        # Patch `resolve_bundled_path` directly — the prior
        # `pathlib.Path.exists` monkeypatch was a stale guard from
        # the pre-PR-6h `Path.cwd()`-based implementation that had
        # zero effect on the new code path (resolve_bundled_path
        # uses `.is_file()`, not `.exists()`). Test then passed only
        # because the real wheel happens to contain ecommerce.sql.
        fake_fixture = tmp_path / "ecommerce.sql"
        fake_fixture.write_text("-- stub\n")
        monkeypatch.setattr(setup_stage, "resolve_bundled_path", lambda name: fake_fixture)
        argv_log: list[list[str]] = []

        def fake_safe_subprocess(argv, *, timeout_s):  # type: ignore[no-untyped-def]
            argv_log.append(argv)
            return CompletedProcess(args=argv, returncode=0, stdout="LOAD 1.2k\n", stderr="")

        monkeypatch.setattr(setup_stage, "_safe_subprocess", fake_safe_subprocess)

        buf = io.StringIO()
        assert (
            setup_stage._docker_load_fixture(
                console=make_console(file=buf, force_terminal=False, width=120)
            )
            is True
        )
        assert "Fixture loaded" in buf.getvalue()
        # Argv must contain the psql command shape.
        argv = argv_log[0]
        assert "psql" in argv
        assert "-f" in argv
        assert "/f.sql" in argv
        # The /v mount references the absolute fixture path.
        assert any("ecommerce.sql" in a for a in argv)

    def test_returns_false_when_psql_subprocess_exits_non_zero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        # Connection refused / fixture syntax error → non-zero exit.
        # Surface the stderr first line.
        from subprocess import CompletedProcess

        from schemabrain.setup import setup_stage

        # Same fix as the sibling success test — patch the resolver,
        # not `Path.exists` which the new code path never calls.
        fake_fixture = tmp_path / "ecommerce.sql"
        fake_fixture.write_text("-- stub\n")
        monkeypatch.setattr(setup_stage, "resolve_bundled_path", lambda name: fake_fixture)
        monkeypatch.setattr(
            setup_stage,
            "_safe_subprocess",
            lambda argv, *, timeout_s: CompletedProcess(
                args=argv,
                returncode=2,
                stdout="",
                stderr="psql: error: connection to server at ... refused\n",
            ),
        )

        buf = io.StringIO()
        assert (
            setup_stage._docker_load_fixture(
                console=make_console(file=buf, force_terminal=False, width=120)
            )
            is False
        )
        out = buf.getvalue()
        assert "couldn't load the ecommerce fixture" in out
        assert "connection to server" in out


class TestSafeSubprocess:
    """D2: `_safe_subprocess` — try/except wrapper around `subprocess.run`."""

    def test_returns_completed_process_for_real_subprocess(self) -> None:
        # Exercise the actual `subprocess.run` path with a trivial,
        # CI-portable command (Python is guaranteed to be present
        # since pytest is running).
        from schemabrain.setup import setup_stage

        result = setup_stage._safe_subprocess(["python", "-c", "print('hello')"], timeout_s=5)
        assert result is not None
        assert result.returncode == 0
        assert "hello" in result.stdout

    def test_returns_none_on_permission_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # regression test HIGH convergent (silent-failure +
        # reality check B1): pre-fold, only FileNotFoundError +
        # TimeoutExpired were caught. A binary on PATH but not
        # executable (mode 0555 stripped, snap confinement, macOS
        # quarantine, non-docker-group user on Linux) raised
        # PermissionError and bypassed the fall-through-to-manual
        # design intent. Now catches the full `OSError` family.
        import subprocess

        from schemabrain.setup import setup_stage

        def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(subprocess, "run", boom)

        assert setup_stage._safe_subprocess(["docker", "ps"], timeout_s=5) is None

    def test_returns_none_on_generic_oserror(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Defensive: ENOMEM, EACCES, or any other OSError flavor
        # the OS may surface during spawn — all must funnel to None
        # so the caller's "fall through to manual recipe" branch
        # always wins.
        import subprocess

        from schemabrain.setup import setup_stage

        def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise OSError(12, "Cannot allocate memory")

        monkeypatch.setattr(subprocess, "run", boom)

        assert setup_stage._safe_subprocess(["docker", "ps"], timeout_s=5) is None


class TestDockerStartFailure:
    """D2: branch where `docker start` (re-start of stopped container) fails.

    Distinct from `docker run` failure — covers the case where a
    container exists but is stopped AND can't be started (corrupted
    state, image gone, etc.).
    """

    def test_failed_docker_start_surfaces_explanation_and_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from subprocess import CompletedProcess

        from schemabrain.setup import setup_stage

        def fake_safe_subprocess(argv, *, timeout_s):  # type: ignore[no-untyped-def]
            if argv[:2] == ["docker", "inspect"]:
                return CompletedProcess(args=argv, returncode=0, stdout="false\n", stderr="")
            if argv[:2] == ["docker", "start"]:
                return CompletedProcess(
                    args=argv,
                    returncode=1,
                    stdout="",
                    stderr="Error response from daemon: image not found\n",
                )
            raise AssertionError(f"unexpected argv: {argv}")

        monkeypatch.setattr(setup_stage, "_safe_subprocess", fake_safe_subprocess)

        buf = io.StringIO()
        assert (
            setup_stage._docker_run_demo_postgres(
                console=make_console(file=buf, force_terminal=False, width=120)
            )
            is False
        )
        out = buf.getvalue()
        assert "couldn't start the existing container" in out
        assert "image not found" in out


class TestPrintDockerFailureEmptyStderr:
    """D2: `_print_docker_failure` fallback when subprocess exits non-zero
    with no stderr — surfaces the exit code instead of leaving the
    operator with a blank diagnostic."""

    def test_no_stderr_surfaces_exit_code(self) -> None:
        from subprocess import CompletedProcess

        from schemabrain.setup import setup_stage

        result = CompletedProcess(args=["docker", "run"], returncode=125, stdout="", stderr="")
        buf = io.StringIO()
        setup_stage._print_docker_failure(
            make_console(file=buf, force_terminal=False, width=120),
            "couldn't start Postgres container",
            result,
        )
        out = buf.getvalue()
        assert "couldn't start Postgres container" in out
        assert "exited 125 with no stderr" in out
