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


class TestPromptForInitSetup:
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
