"""Tests for ``schemabrain/setup/env_file.py`` (D4 .env load + persist)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from schemabrain.setup.env_file import (
    is_path_in_gitignore,
    load_env_file_into_environ,
    persist_key_to_env_file,
)

# ----- load_env_file_into_environ ------------------------------------------


class TestLoadEnvFileIntoEnviron:
    """D4: read .env, set keys NOT already in os.environ, shell exports win."""

    def test_returns_zero_when_file_missing(self, tmp_path: Path) -> None:
        # No `.env` at the path → silent no-op (the wizard's persist
        # flow is optional; a project without `.env` is the default).
        loaded = load_env_file_into_environ(tmp_path / ".env")
        assert loaded == 0

    def test_loads_simple_key_value_pairs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("FOO=bar\nBAZ=qux\n", encoding="utf-8")
        monkeypatch.delenv("FOO", raising=False)
        monkeypatch.delenv("BAZ", raising=False)

        loaded = load_env_file_into_environ(env_file)

        assert loaded == 2
        assert os.environ["FOO"] == "bar"
        assert os.environ["BAZ"] == "qux"

    def test_existing_env_var_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # The hard contract: a shell `export ANTHROPIC_API_KEY=fresh`
        # must NEVER be silently overridden by a stale `.env` entry.
        env_file = tmp_path / ".env"
        env_file.write_text("ANTHROPIC_API_KEY=stale_from_file\n", encoding="utf-8")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fresh_from_shell")

        loaded = load_env_file_into_environ(env_file)

        assert loaded == 0
        assert os.environ["ANTHROPIC_API_KEY"] == "fresh_from_shell"

    def test_quoted_values_unquoted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text(
            "DOUBLE=\"value with spaces\"\nSINGLE='single quoted'\n",
            encoding="utf-8",
        )
        monkeypatch.delenv("DOUBLE", raising=False)
        monkeypatch.delenv("SINGLE", raising=False)

        load_env_file_into_environ(env_file)

        assert os.environ["DOUBLE"] == "value with spaces"
        assert os.environ["SINGLE"] == "single quoted"

    def test_comments_and_blanks_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text(
            "# this is a comment\n\nFOO=bar\n  # indented comment\n",
            encoding="utf-8",
        )
        monkeypatch.delenv("FOO", raising=False)

        loaded = load_env_file_into_environ(env_file)

        assert loaded == 1
        assert os.environ["FOO"] == "bar"

    def test_malformed_lines_silently_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The loader is best-effort — a malformed `.env` should not
        # abort the wizard. Lines without `=` or with invalid
        # identifiers on the left silently skip.
        env_file = tmp_path / ".env"
        env_file.write_text(
            "this is not a key=value pair\n=missing_key\n9STARTS_WITH_DIGIT=x\nVALID=ok\n",
            encoding="utf-8",
        )
        monkeypatch.delenv("VALID", raising=False)

        loaded = load_env_file_into_environ(env_file)

        assert loaded == 1
        assert os.environ["VALID"] == "ok"


# ----- persist_key_to_env_file ---------------------------------------------


class TestPersistKeyToEnvFile:
    """D4: write/update ANTHROPIC_API_KEY in .env, preserve other entries."""

    def test_creates_env_file_when_missing(self, tmp_path: Path) -> None:
        env_path = tmp_path / ".env"
        persist_key_to_env_file(
            key_name="ANTHROPIC_API_KEY",
            key_value="sk-ant-test123",
            env_path=env_path,
        )
        assert env_path.exists()
        assert env_path.read_text(encoding="utf-8") == "ANTHROPIC_API_KEY=sk-ant-test123\n"

    def test_appends_when_key_absent(self, tmp_path: Path) -> None:
        env_path = tmp_path / ".env"
        env_path.write_text("OTHER=value\n", encoding="utf-8")
        persist_key_to_env_file(
            key_name="ANTHROPIC_API_KEY",
            key_value="sk-ant-test123",
            env_path=env_path,
        )
        body = env_path.read_text(encoding="utf-8")
        # Both lines present; OTHER preserved exactly.
        assert "OTHER=value" in body
        assert "ANTHROPIC_API_KEY=sk-ant-test123" in body

    def test_replaces_existing_key_in_place(self, tmp_path: Path) -> None:
        # Critical: re-pasting must REPLACE not duplicate. A duplicate
        # ANTHROPIC_API_KEY entry would land the wrong-of-two in
        # os.environ on next load (whichever wins the loader's
        # iteration), which is exactly the silent-failure mode D4
        # is designed to prevent.
        env_path = tmp_path / ".env"
        env_path.write_text(
            "OTHER=untouched\nANTHROPIC_API_KEY=sk-ant-old\nTAIL=last\n",
            encoding="utf-8",
        )
        persist_key_to_env_file(
            key_name="ANTHROPIC_API_KEY",
            key_value="sk-ant-new",
            env_path=env_path,
        )
        body = env_path.read_text(encoding="utf-8")
        assert body.count("ANTHROPIC_API_KEY=") == 1
        assert "ANTHROPIC_API_KEY=sk-ant-new" in body
        assert "ANTHROPIC_API_KEY=sk-ant-old" not in body
        # Other lines preserved in order.
        assert body.startswith("OTHER=untouched\n")
        assert body.endswith("TAIL=last\n")

    def test_preserves_comments_and_blanks(self, tmp_path: Path) -> None:
        env_path = tmp_path / ".env"
        env_path.write_text(
            "# top comment\n\nOTHER=value\n# another comment\n",
            encoding="utf-8",
        )
        persist_key_to_env_file(
            key_name="ANTHROPIC_API_KEY",
            key_value="sk-ant-test",
            env_path=env_path,
        )
        body = env_path.read_text(encoding="utf-8")
        assert "# top comment" in body
        assert "# another comment" in body

    def test_owner_only_permissions_on_create(self, tmp_path: Path) -> None:
        # The `.env` carries a secret — it must NOT land
        # group/world-readable on a fresh write. (On macOS/Linux;
        # the test is no-op on Windows where chmod bits aren't
        # honored the same way, but persist_key still works.)
        env_path = tmp_path / ".env"
        persist_key_to_env_file(
            key_name="ANTHROPIC_API_KEY",
            key_value="sk-ant-test",
            env_path=env_path,
        )
        if os.name == "posix":
            mode = env_path.stat().st_mode & 0o777
            # Owner-only mode (0o600) — no group/world bits.
            assert mode & 0o077 == 0

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        env_path = tmp_path / "nested" / "deeper" / ".env"
        persist_key_to_env_file(
            key_name="ANTHROPIC_API_KEY",
            key_value="sk-ant-test",
            env_path=env_path,
        )
        assert env_path.exists()

    def test_cleans_up_tmp_file_when_replace_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Contract: a failure between tmp-write and rename MUST NOT
        # leave a sibling `.tmp` file behind. Otherwise re-runs would
        # accumulate broken half-writes in the operator's repo.
        env_path = tmp_path / ".env"

        def boom(*_args: object, **_kwargs: object) -> None:
            raise PermissionError("simulated replace failure")

        monkeypatch.setattr("schemabrain.setup.env_file.os.replace", boom)
        with pytest.raises(PermissionError, match="simulated replace failure"):
            persist_key_to_env_file(
                key_name="ANTHROPIC_API_KEY",
                key_value="sk-ant-test",
                env_path=env_path,
            )
        # No `.tmp` sibling left in the directory.
        residue = list(tmp_path.glob("*.tmp"))
        assert residue == []


# ----- is_path_in_gitignore ------------------------------------------------


class TestIsPathInGitignore:
    """D4: best-effort literal-match check feeding the consent warning."""

    def test_false_when_gitignore_missing(self, tmp_path: Path) -> None:
        result = is_path_in_gitignore(
            target=tmp_path / ".env",
            gitignore=tmp_path / ".gitignore",
        )
        assert result is False

    def test_true_when_basename_listed(self, tmp_path: Path) -> None:
        gi = tmp_path / ".gitignore"
        gi.write_text(".env\n", encoding="utf-8")
        result = is_path_in_gitignore(target=tmp_path / ".env", gitignore=gi)
        assert result is True

    def test_true_with_leading_slash(self, tmp_path: Path) -> None:
        # Operators sometimes anchor patterns to the repo root.
        gi = tmp_path / ".gitignore"
        gi.write_text("/.env\n", encoding="utf-8")
        result = is_path_in_gitignore(target=tmp_path / ".env", gitignore=gi)
        assert result is True

    def test_false_when_glob_pattern(self, tmp_path: Path) -> None:
        # Glob patterns are deliberately NOT matched — we want to
        # warn aggressively when the operator's setup is non-standard,
        # at the cost of a spurious warning when their pattern is
        # glob-clever. (Docstring contract.)
        gi = tmp_path / ".gitignore"
        gi.write_text("*.env\n", encoding="utf-8")
        result = is_path_in_gitignore(target=tmp_path / ".env", gitignore=gi)
        assert result is False

    def test_comments_and_blanks_ignored(self, tmp_path: Path) -> None:
        gi = tmp_path / ".gitignore"
        gi.write_text(
            "# secrets\n\n.env\nother\n",
            encoding="utf-8",
        )
        result = is_path_in_gitignore(target=tmp_path / ".env", gitignore=gi)
        assert result is True

    def test_false_when_other_entry_present(self, tmp_path: Path) -> None:
        gi = tmp_path / ".gitignore"
        gi.write_text("node_modules\n*.pyc\n", encoding="utf-8")
        result = is_path_in_gitignore(target=tmp_path / ".env", gitignore=gi)
        assert result is False
