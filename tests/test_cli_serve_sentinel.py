"""Tests for `schemabrain serve`'s policy-mtime sentinel write.

The sentinel file (`./schemabrain/.serve_policy_mtime`) records what
state of `pii_policy.yaml` serve resolved against at startup. The
dashboard sidecar reads it to detect drift between the running
firewall and the operator's edits to YAML; without the sentinel,
the dashboard can't tell whether `policy show` is reading the same
state the firewall is enforcing.

These tests pin three contract slices:

  1. Default-mode serve writes the sentinel with the resolved mtime.
  2. Explicit `--pii-block <csv>` deletes any stale sentinel (CLI
     overrides YAML; drift signal would be misleading).
  3. The sentinel write is best-effort — an unwritable directory
     does not abort serve startup.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from schemabrain.cli import main as cli_main
from schemabrain.core.store import SQLiteStore

SENTINEL_REL_PATH = "schemabrain/.serve_policy_mtime"


def _seed_store(path: Path) -> None:
    SQLiteStore(path).close()


def _patch_run_stdio_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub out `run_stdio` so the CLI returns immediately."""

    def _noop(**_: object) -> None:
        return None

    monkeypatch.setattr("schemabrain.cli.run_stdio", _noop)


def _write_yaml_block_contact(yaml_path: Path) -> None:
    yaml_path.parent.mkdir(parents=True, exist_ok=True)
    yaml_path.write_text(
        "version: 1\nblock:\n  - contact\n",
        encoding="utf-8",
    )


class TestServeSentinelWrite:
    def test_default_serve_writes_sentinel_with_yaml_mtime(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        store_path = tmp_path / "store.db"
        _seed_store(store_path)
        yaml_path = tmp_path / "schemabrain" / "pii_policy.yaml"
        _write_yaml_block_contact(yaml_path)
        _patch_run_stdio_noop(monkeypatch)
        monkeypatch.setenv("FAKE_URL", "postgresql+psycopg://fake/sentinel")

        exit_code = cli_main(
            [
                "serve",
                "--url-env",
                "FAKE_URL",
                "--store-path",
                str(store_path),
                "--no-events",
                "--no-audit",
            ]
        )
        assert exit_code == 0

        sentinel = tmp_path / SENTINEL_REL_PATH
        assert sentinel.exists(), "sentinel file must be written by default-mode serve"
        payload = json.loads(sentinel.read_text(encoding="utf-8"))
        # Schema contract.
        assert set(payload.keys()) == {
            "policy_path",
            "recorded_at_mtime",
            "recorded_at_iso",
            "yaml_existed_at_boot",
        }
        # The sentinel records the ABSOLUTE resolved path so the sidecar
        # can verify the same file is being watched.
        assert payload["policy_path"] == str(yaml_path.resolve())
        # YAML existed at boot.
        assert payload["yaml_existed_at_boot"] is True
        # Recorded mtime matches the YAML file's actual mtime.
        assert payload["recorded_at_mtime"] == pytest.approx(yaml_path.stat().st_mtime, abs=0.001)
        # iso8601 format with offset.
        assert "T" in payload["recorded_at_iso"]
        assert payload["recorded_at_iso"].endswith("+00:00")

    def test_default_serve_writes_sentinel_when_yaml_absent(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When no YAML exists at boot, the sentinel records that fact
        so a later YAML creation fires a drift signal."""
        monkeypatch.chdir(tmp_path)
        store_path = tmp_path / "store.db"
        _seed_store(store_path)
        # NO yaml created.
        _patch_run_stdio_noop(monkeypatch)
        monkeypatch.setenv("FAKE_URL", "postgresql+psycopg://fake/sentinel")

        exit_code = cli_main(
            [
                "serve",
                "--url-env",
                "FAKE_URL",
                "--store-path",
                str(store_path),
                "--no-events",
                "--no-audit",
            ]
        )
        assert exit_code == 0

        sentinel = tmp_path / SENTINEL_REL_PATH
        assert sentinel.exists()
        payload = json.loads(sentinel.read_text(encoding="utf-8"))
        assert payload["yaml_existed_at_boot"] is False
        assert payload["recorded_at_mtime"] is None
        # Path still recorded so sidecar can match.
        assert payload["policy_path"].endswith("pii_policy.yaml")

    def test_explicit_pii_block_deletes_stale_sentinel(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When the operator passes `--pii-block <csv>`, the CLI flag
        overrides YAML — leaving a stale sentinel around would fire a
        misleading drift banner. Pre-seed a sentinel, then run with
        an explicit block, and assert the sentinel is gone."""
        monkeypatch.chdir(tmp_path)
        store_path = tmp_path / "store.db"
        _seed_store(store_path)
        sentinel = tmp_path / SENTINEL_REL_PATH
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text('{"stale": true}', encoding="utf-8")
        _patch_run_stdio_noop(monkeypatch)
        monkeypatch.setenv("FAKE_URL", "postgresql+psycopg://fake/sentinel")

        exit_code = cli_main(
            [
                "serve",
                "--url-env",
                "FAKE_URL",
                "--store-path",
                str(store_path),
                "--no-events",
                "--no-audit",
                "--pii-block",
                "contact",
            ]
        )
        assert exit_code == 0
        assert not sentinel.exists(), (
            "stale sentinel must be removed when --pii-block overrides YAML"
        )

    def test_explicit_empty_pii_block_deletes_stale_sentinel(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The empty escape hatch `--pii-block ''` also overrides YAML,
        so the sentinel must be removed."""
        monkeypatch.chdir(tmp_path)
        store_path = tmp_path / "store.db"
        _seed_store(store_path)
        sentinel = tmp_path / SENTINEL_REL_PATH
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text('{"stale": true}', encoding="utf-8")
        _patch_run_stdio_noop(monkeypatch)
        monkeypatch.setenv("FAKE_URL", "postgresql+psycopg://fake/sentinel")

        exit_code = cli_main(
            [
                "serve",
                "--url-env",
                "FAKE_URL",
                "--store-path",
                str(store_path),
                "--no-events",
                "--no-audit",
                "--pii-block",
                "",
            ]
        )
        assert exit_code == 0
        assert not sentinel.exists()

    def test_sentinel_write_failure_does_not_abort_serve(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """If the sentinel directory can't be created (e.g. permission
        denied), serve must log a warning and continue. Drift detection
        is observability, not safety — never an abort."""
        monkeypatch.chdir(tmp_path)
        store_path = tmp_path / "store.db"
        _seed_store(store_path)
        _patch_run_stdio_noop(monkeypatch)
        monkeypatch.setenv("FAKE_URL", "postgresql+psycopg://fake/sentinel")

        # Patch Path.write_text to raise — exercises the inner try/except.
        original_write_text = Path.write_text

        def _patched_write_text(self: Path, *a: object, **kw: object) -> int:
            if str(self).endswith(".serve_policy_mtime"):
                raise OSError("disk full")
            return original_write_text(self, *a, **kw)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "write_text", _patched_write_text)

        exit_code = cli_main(
            [
                "serve",
                "--url-env",
                "FAKE_URL",
                "--store-path",
                str(store_path),
                "--no-events",
                "--no-audit",
            ]
        )
        assert exit_code == 0
        captured = capsys.readouterr()
        assert (
            "cannot write sentinel" in captured.err or "Drift detection disabled" in captured.err
        ), f"serve must warn on sentinel write failure; got stderr: {captured.err!r}"

    def test_record_serve_policy_mtime_handles_unstattable_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Direct test of the helper: when the YAML path can't be stat'd
        for a reason other than FileNotFoundError, log and return."""
        from schemabrain.cli import _record_serve_policy_mtime

        monkeypatch.chdir(tmp_path)
        original_stat = Path.stat

        def _patched_stat(self: Path, *a: object, **kw: object) -> object:
            if str(self).endswith("pii_policy.yaml"):
                raise PermissionError("denied")
            return original_stat(self, *a, **kw)  # type: ignore[misc]

        monkeypatch.setattr(Path, "stat", _patched_stat)
        # Create a YAML file so the path "exists" — the stat call is the
        # one that fails.
        yaml_path = tmp_path / "schemabrain" / "pii_policy.yaml"
        yaml_path.parent.mkdir(parents=True, exist_ok=True)
        yaml_path.write_text("version: 1\nblock: []\n", encoding="utf-8")

        _record_serve_policy_mtime(str(yaml_path))
        captured = capsys.readouterr()
        assert "cannot stat" in captured.err
        sentinel = tmp_path / SENTINEL_REL_PATH
        assert not sentinel.exists(), "sentinel should NOT be written when stat fails"

    def test_delete_stale_serve_policy_sentinel_handles_missing_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The delete helper must be idempotent — no-op when sentinel
        doesn't exist."""
        from schemabrain.cli import _delete_stale_serve_policy_sentinel

        monkeypatch.chdir(tmp_path)
        # No sentinel created.
        _delete_stale_serve_policy_sentinel()  # must not raise
