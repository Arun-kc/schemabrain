"""Tests for `schemabrain init --pii-block` resolution.

L-1 fix: under `--yes` (CI / scripted) and non-TTY stderr, the prior
behavior silently fell through to the wizard's `("contact",)`
default — so an operator following the README zero-config path got
`contact`-only blocking while `staff.password` (credential),
`customer.credit_card` (payment_card), and `staff.ssn`
(government_id) remained agent-readable.

Post-fix three-state contract:
  - `--pii-block <csv>` explicit  → parse, validate, use it
  - `--pii-block ''` explicit empty → empty tuple (no flag in snippet)
  - flag absent + interactive TTY  → interactive prompt (unchanged)
  - flag absent + --yes / non-TTY  → catastrophic-leak default
                                     {credential, payment_card,
                                     government_id} + stderr confirm

These tests stub `run_default_wizard` and `prompt_for_pii_block` so
they exercise the resolution branches in `_cmd_init` without paying
the cost of a real wizard run.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from schemabrain.cli import main
from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore


@pytest.fixture
def seeded_store(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "store.db"
    store = SQLiteStore(path=path)
    try:
        store.write_table(
            Table(
                name="orders",
                schema_name="public",
                columns=(
                    Column(
                        name="id",
                        table_name="orders",
                        schema_name="public",
                        data_type="bigint",
                        nullable=False,
                        ordinal_position=1,
                        is_primary_key=True,
                    ),
                ),
            ),
            source_connection_id="src_a",
        )
        store.write_entity(
            Entity(
                name="order",
                description="",
                binding=SingleTableBinding(qualified_table="public.orders"),
                identity="id",
            ),
            source_connection_id="src_a",
        )
        yield path
    finally:
        store.close()


@pytest.fixture
def stub_uvx(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(
        "shutil.which",
        lambda n: "/usr/local/bin/uvx" if n == "uvx" else None,
    )
    monkeypatch.setattr(
        "schemabrain.setup.init_flow._is_pypi_install",
        lambda: True,
    )
    yield


class TestInitPiiBlockResolution:
    """Resolution branches end-to-end through `--print-only`. The
    snippet's `args` list carries the `--pii-block <csv>` token when
    enforcement is active, and omits the flag when disabled.
    """

    def _snippet_args(self, capsys: pytest.CaptureFixture[str]) -> list[str]:
        parsed = json.loads(capsys.readouterr().out)
        return parsed["mcpServers"]["schemabrain"]["args"]

    def test_yes_no_pii_block_defaults_to_catastrophic_with_one_liner(
        self,
        seeded_store: Path,
        stub_uvx: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Stage 0 prompt is skipped under --yes, so init reaches the
        # PII-block resolution branch with no flag and not-TTY (the
        # pytest harness already provides this) — catastrophic
        # default lands in the snippet.
        exit_code = main(
            [
                "init",
                "--source",
                "sqlite:///:memory:",
                "--store-path",
                str(seeded_store),
                "--print-only",
                "--yes",
            ]
        )
        assert exit_code == 0
        snippet_args = self._snippet_args(capsys)
        # --pii-block flag present with the catastrophic CSV
        idx = snippet_args.index("--pii-block")
        csv = snippet_args[idx + 1]
        assert set(csv.split(",")) == {"credential", "payment_card", "government_id"}

    def test_yes_no_pii_block_emits_one_liner_to_stderr(
        self,
        seeded_store: Path,
        stub_uvx: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main(
            [
                "init",
                "--source",
                "sqlite:///:memory:",
                "--store-path",
                str(seeded_store),
                "--print-only",
                "--yes",
            ]
        )
        stderr = capsys.readouterr().err
        # One-line stderr surfaces what got enforced + how to override
        assert "--pii-block not passed" in stderr
        assert "credential" in stderr
        assert "use --pii-block '' to disable" in stderr

    def test_yes_explicit_pii_block_respected_and_no_default_message(
        self,
        seeded_store: Path,
        stub_uvx: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main(
            [
                "init",
                "--source",
                "sqlite:///:memory:",
                "--store-path",
                str(seeded_store),
                "--print-only",
                "--yes",
                "--pii-block",
                "contact,health",
            ]
        )
        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        snippet_args = parsed["mcpServers"]["schemabrain"]["args"]
        idx = snippet_args.index("--pii-block")
        csv = snippet_args[idx + 1]
        assert set(csv.split(",")) == {"contact", "health"}
        # Explicit value short-circuits the default-confirmation
        assert "--pii-block not passed" not in captured.err

    def test_yes_explicit_empty_pii_block_omits_flag_from_snippet(
        self,
        seeded_store: Path,
        stub_uvx: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # `--pii-block ''` is the explicit escape hatch — operator
        # opts out of enforcement. The snippet must NOT include a
        # `--pii-block` token at all (empty tuple → no flag rendered),
        # otherwise the server would receive `--pii-block` followed
        # by the next token in argv as its value.
        main(
            [
                "init",
                "--source",
                "sqlite:///:memory:",
                "--store-path",
                str(seeded_store),
                "--print-only",
                "--yes",
                "--pii-block",
                "",
            ]
        )
        captured = capsys.readouterr()
        snippet_args = json.loads(captured.out)["mcpServers"]["schemabrain"]["args"]
        assert "--pii-block" not in snippet_args
        # No surprise default-confirmation message
        assert "--pii-block not passed" not in captured.err

    def test_unknown_pii_category_exits_two_with_listing(
        self,
        seeded_store: Path,
        stub_uvx: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        exit_code = main(
            [
                "init",
                "--source",
                "sqlite:///:memory:",
                "--store-path",
                str(seeded_store),
                "--print-only",
                "--yes",
                "--pii-block",
                "contact,nonsense",
            ]
        )
        assert exit_code == 2
        stderr = capsys.readouterr().err
        assert "nonsense" in stderr
        assert "Valid categories" in stderr


class TestInitPiiBlockInteractivePromptUnchanged:
    """The interactive-TTY branch is unchanged by L-1: when stderr
    is a TTY AND --yes is absent AND --pii-block is absent, the
    prompt fires as before. This guards against a regression where
    the new catastrophic-default branch silently swallows the
    interactive path.
    """

    def test_interactive_tty_without_flag_invokes_prompt(
        self,
        seeded_store: Path,
        stub_uvx: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        prompt_calls: list[int] = []

        def _fake_prompt(*, console: object) -> tuple[str, ...]:
            prompt_calls.append(1)
            return ("contact",)

        monkeypatch.setattr("schemabrain.cli._stderr_is_interactive_tty", lambda: True)
        monkeypatch.setattr(
            "schemabrain.setup.setup_stage.prompt_for_pii_block",
            _fake_prompt,
        )
        # Stage 0 prompt must not fire — only the PII prompt should.
        # Provide `--source` so init skips stage 0 entirely.
        exit_code = main(
            [
                "init",
                "--source",
                "sqlite:///:memory:",
                "--store-path",
                str(seeded_store),
                "--print-only",
            ]
        )
        assert exit_code == 0
        assert prompt_calls == [1]

    def test_explicit_flag_short_circuits_interactive_prompt(
        self,
        seeded_store: Path,
        stub_uvx: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # When the operator passes `--pii-block` explicitly on the
        # command line, even an interactive TTY does NOT fire the
        # prompt — the explicit choice wins. Without this short-
        # circuit, a scripted operator would have to thread `--yes`
        # in addition to `--pii-block`.
        prompt_calls: list[int] = []
        monkeypatch.setattr("schemabrain.cli._stderr_is_interactive_tty", lambda: True)
        monkeypatch.setattr(
            "schemabrain.setup.setup_stage.prompt_for_pii_block",
            lambda *, console: prompt_calls.append(1) or (),
        )
        main(
            [
                "init",
                "--source",
                "sqlite:///:memory:",
                "--store-path",
                str(seeded_store),
                "--print-only",
                "--pii-block",
                "credential",
            ]
        )
        assert prompt_calls == []
