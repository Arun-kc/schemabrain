"""CLI-side tests for `schemabrain index --no-pii-classify`."""

from __future__ import annotations

from pathlib import Path

import pytest

from schemabrain.cli import main as cli_main


class TestNoPiiClassifyWarning:
    def test_warning_emitted_when_flag_set(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Use an invalid URL so we exit early — the warning is printed
        # BEFORE the URL is resolved, so its presence is the only
        # thing this test asserts.
        monkeypatch.setenv("FAKE_URL", "")
        exit_code = cli_main(
            [
                "index",
                "--url-env",
                "FAKE_URL",
                "--store-path",
                str(tmp_path / "sb.db"),
                "--no-pii-classify",
                "--no-enrich",
            ]
        )
        # Exit 2 from the empty-URL guard — fine; we only care about
        # the warning being on stderr.
        assert exit_code == 2
        stderr = capsys.readouterr().err
        assert "PII classification disabled" in stderr
        assert "pii_categories=''" in stderr

    def test_no_warning_when_flag_unset(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("FAKE_URL", "")
        cli_main(
            [
                "index",
                "--url-env",
                "FAKE_URL",
                "--store-path",
                str(tmp_path / "sb.db"),
                "--no-enrich",
            ]
        )
        stderr = capsys.readouterr().err
        assert "PII classification disabled" not in stderr


class TestArgparseSurface:
    def test_no_pii_classify_in_index_help(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Help text generation walks every flag; this exercises the
        # argparse wiring without needing a working DB.
        with pytest.raises(SystemExit) as exc:
            cli_main(["index", "--help"])
        assert exc.value.code == 0
        stdout = capsys.readouterr().out
        assert "--no-pii-classify" in stdout
