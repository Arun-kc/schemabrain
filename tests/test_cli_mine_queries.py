"""CLI tests for `schemabrain mine-queries`.

These tests cover the argument parsing + handler wiring; they patch
`schemabrain.cli.mine_queries` so no real Postgres or sqlglot work
happens — the pipeline's own behaviour is exercised in
`tests/test_mining_pipeline.py`. End-to-end coverage against a real
`pg_stat_statements`-enabled Postgres lives in
`tests/test_mining_integration.py` (integration-marked).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from schemabrain.cli import main
from schemabrain.mining.pipeline import MineReport


class TestArgParsing:
    def test_missing_source_and_url_env_exits_two(self, capsys: pytest.CaptureFixture[str]) -> None:
        exit_code = main(["mine-queries"])
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "--url-env" in err

    def test_malformed_url_exits_two(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store_path = tmp_path / "schemabrain.db"
        exit_code = main(
            [
                "mine-queries",
                "--source",
                "not-a-url",
                "--store-path",
                str(store_path),
            ]
        )
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "error:" in err


class TestSuccessfulMining:
    def test_emits_summary_with_counts(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        store_path = tmp_path / "schemabrain.db"
        url = "postgresql+psycopg://user:pw@localhost/db"

        fake_mine = MagicMock(
            return_value=MineReport(
                statements_read=20,
                statements_used=12,
                rows_written=18,
                skipped_unavailable=False,
            )
        )
        monkeypatch.setattr("schemabrain.cli.mine_queries", fake_mine)

        exit_code = main(
            [
                "mine-queries",
                "--source",
                url,
                "--store-path",
                str(store_path),
            ]
        )
        assert exit_code == 0
        err = capsys.readouterr().err
        # The summary must surface all three counts so operators can
        # confirm mining did something meaningful.
        assert "20" in err  # statements_read
        assert "12" in err  # statements_used
        assert "18" in err  # rows_written
        # Confirm the handler invoked the pipeline at all.
        fake_mine.assert_called_once()


class TestUnavailableSource:
    def test_unavailable_emits_actionable_message(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        store_path = tmp_path / "schemabrain.db"
        url = "postgresql+psycopg://user:pw@localhost/db"

        fake_mine = MagicMock(
            return_value=MineReport(
                statements_read=0,
                statements_used=0,
                rows_written=0,
                skipped_unavailable=True,
            )
        )
        monkeypatch.setattr("schemabrain.cli.mine_queries", fake_mine)

        exit_code = main(
            [
                "mine-queries",
                "--source",
                url,
                "--store-path",
                str(store_path),
            ]
        )
        # Soft-skip: not a hard failure. The operator sees the
        # message and acts on it.
        assert exit_code == 0
        err = capsys.readouterr().err
        assert "pg_stat_statements" in err
        # Must include enough guidance for the operator to fix the
        # source-side configuration (CREATE EXTENSION + shared_preload_-
        # libraries).
        assert "extension" in err.lower()


class TestSourceIdResolution:
    def test_passes_canonical_source_id_to_pipeline(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The pipeline must receive the same source_id that index
        # uses, so mined rows land under the right source partition.
        from schemabrain.cli import _make_source_id

        store_path = tmp_path / "schemabrain.db"
        url = "postgresql+psycopg://user:pw@localhost/db"
        expected_source_id = _make_source_id(url)

        fake_mine = MagicMock(
            return_value=MineReport(
                statements_read=0,
                statements_used=0,
                rows_written=0,
                skipped_unavailable=False,
            )
        )
        monkeypatch.setattr("schemabrain.cli.mine_queries", fake_mine)

        main(
            [
                "mine-queries",
                "--source",
                url,
                "--store-path",
                str(store_path),
            ]
        )
        # Pipeline call gets the right source_id.
        kwargs = fake_mine.call_args.kwargs
        assert kwargs["source_connection_id"] == expected_source_id
