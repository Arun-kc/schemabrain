"""Tests for the schemabrain CLI."""

import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import schemabrain
from schemabrain.cli import _build_index_reporter, _canonical_url, _make_source_id, main
from schemabrain.core.store import SQLiteStore
from schemabrain.indexer import NullReporter

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _pyproject_version() -> str:
    """Read `[project].version` directly from pyproject.toml.

    Used as the drift anchor — `pyproject.toml` is the single source of
    truth, and both `schemabrain.__version__` and `schemabrain --version`
    must read through to it.
    """
    raw = (_REPO_ROOT / "pyproject.toml").read_text()
    parsed = tomllib.loads(raw)
    return parsed["project"]["version"]


class TestVersionFlag:
    """`schemabrain --version` and `schemabrain.__version__` must both
    read from the installed package metadata (which is built from
    `pyproject.toml`), so the version literal lives in exactly one place.
    """

    def test_package_version_matches_pyproject(self) -> None:
        # If this fires, someone bumped pyproject.toml without
        # reinstalling the package (so importlib.metadata is stale), OR
        # they accidentally hardcoded a different __version__ back into
        # __init__.py. Both are drift bugs worth catching in CI.
        assert schemabrain.__version__ == _pyproject_version()

    def test_version_flag_exits_zero(self) -> None:
        # argparse's `action="version"` prints to stdout and raises
        # SystemExit(0). Pin the exit code so we don't accidentally swap
        # to a custom handler that returns non-zero.
        with pytest.raises(SystemExit) as exc:
            main(["--version"])
        assert exc.value.code == 0

    def test_short_V_flag_also_works(self, capsys: pytest.CaptureFixture[str]) -> None:
        # `-V` is the conventional short form (python -V, pip -V). We
        # alias it intentionally so users from those ecosystems get the
        # expected behavior; this test pins it so a future contributor
        # doesn't quietly drop the alias.
        with pytest.raises(SystemExit) as exc:
            main(["-V"])
        assert exc.value.code == 0
        assert schemabrain.__version__ in capsys.readouterr().out

    def test_version_flag_prints_pyproject_version_to_stdout(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Anchor on the pyproject literal (not schemabrain.__version__)
        # so this is a true end-to-end check: pyproject.toml → installed
        # metadata → __version__ → CLI output. If any link breaks, the
        # CLI doesn't print the literal that's in pyproject and this test
        # fires regardless of where the chain snapped.
        with pytest.raises(SystemExit):
            main(["--version"])
        captured = capsys.readouterr()
        assert _pyproject_version() in captured.out
        # `action="version"` writes to stdout, not stderr.
        assert captured.err == ""

    def test_version_flag_prints_prog_name(self, capsys: pytest.CaptureFixture[str]) -> None:
        # argparse's default format is `%(prog)s %(version)s` — the
        # prog name must appear so users running it can verify they
        # invoked the right binary. (The version literal `0.1.0a1` does
        # not contain "schemabrain", so this genuinely checks the
        # %(prog)s substitution rather than passing tautologically.)
        with pytest.raises(SystemExit):
            main(["--version"])
        assert "schemabrain" in capsys.readouterr().out


class TestCanonicalUrl:
    def test_strips_credentials(self):
        with_creds = "postgresql://alice:s3cret@db.example.com:5432/mydb"
        without_creds = "postgresql://db.example.com:5432/mydb"
        assert _canonical_url(with_creds) == _canonical_url(without_creds)

    def test_normalizes_default_port(self):
        with_port = "postgresql://db.example.com:5432/mydb"
        no_port = "postgresql://db.example.com/mydb"
        assert _canonical_url(with_port) == _canonical_url(no_port)

    def test_normalizes_trailing_slash(self):
        with_slash = "postgresql://db.example.com:5432/mydb/"
        without_slash = "postgresql://db.example.com:5432/mydb"
        assert _canonical_url(with_slash) == _canonical_url(without_slash)

    def test_does_not_contain_credentials(self):
        url = "postgresql://alice:s3cret@db.example.com:5432/mydb"
        canonical = _canonical_url(url)
        assert "alice" not in canonical
        assert "s3cret" not in canonical

    def test_rejects_url_with_no_scheme(self):
        with pytest.raises(ValueError, match="no scheme"):
            _canonical_url("not-a-url")

    def test_rejects_unsupported_scheme(self):
        with pytest.raises(ValueError, match="Unsupported scheme"):
            _canonical_url("mysql://db.example.com:3306/mydb")

    def test_accepts_postgres_alias_scheme(self):
        # postgres:// is a common alias accepted by drivers
        assert _canonical_url("postgres://host/db").startswith("postgres://")

    def test_accepts_psycopg_driver_scheme(self):
        url = "postgresql+psycopg://host/db"
        assert _canonical_url(url).startswith("postgresql+psycopg://")


class TestMakeSourceId:
    def test_same_db_via_different_credentials_produces_same_id(self):
        a = "postgresql://alice:secret1@db.example.com:5432/mydb"
        b = "postgresql://bob:secret2@db.example.com:5432/mydb"
        assert _make_source_id(a) == _make_source_id(b)

    def test_default_port_and_explicit_5432_produce_same_id(self):
        a = "postgresql://db.example.com:5432/mydb"
        b = "postgresql://db.example.com/mydb"
        assert _make_source_id(a) == _make_source_id(b)

    def test_trailing_slash_produces_same_id(self):
        a = "postgresql://db.example.com/mydb"
        b = "postgresql://db.example.com/mydb/"
        assert _make_source_id(a) == _make_source_id(b)

    def test_different_databases_get_different_ids(self):
        a = "postgresql://db.example.com:5432/db_a"
        b = "postgresql://db.example.com:5432/db_b"
        assert _make_source_id(a) != _make_source_id(b)

    def test_different_hosts_get_different_ids(self):
        a = "postgresql://host1:5432/mydb"
        b = "postgresql://host2:5432/mydb"
        assert _make_source_id(a) != _make_source_id(b)

    def test_id_is_short_hex(self):
        url = "postgresql://db.example.com:5432/mydb"
        result = _make_source_id(url)
        assert len(result) == 16
        assert all(c in "0123456789abcdef" for c in result)

    def test_rejects_malformed_url(self):
        with pytest.raises(ValueError):
            _make_source_id("not-a-url")


class TestArgParsing:
    def test_no_args_exits_nonzero(self):
        with pytest.raises(SystemExit) as exc:
            main([])
        assert exc.value.code != 0

    def test_unknown_subcommand_exits_nonzero(self):
        with pytest.raises(SystemExit) as exc:
            main(["nonexistent"])
        assert exc.value.code != 0

    def test_index_requires_url(self, capsys: pytest.CaptureFixture[str]) -> None:
        # Q1: positional URL became optional so users can prefer --url-env.
        # When neither is passed we render a guided error and exit 2 rather
        # than letting argparse blow up with a generic "missing argument".
        exit_code = main(["index"])
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "error:" in err
        assert "--url-env" in err


class TestIndexCommandValidation:
    def test_malformed_url_returns_nonzero_exit_code(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        store_path = tmp_path / "schemabrain.db"
        exit_code = main(["index", "not-a-url", "--store-path", str(store_path)])
        assert exit_code == 2
        err = capsys.readouterr().err
        # Guided-error format: "error: ..." headline + "why:" / "fix:".
        assert "error:" in err
        assert "fix:" in err

    def test_unsupported_scheme_returns_nonzero_exit_code(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        store_path = tmp_path / "schemabrain.db"
        exit_code = main(["index", "mysql://db/test", "--store-path", str(store_path)])
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "Unsupported scheme" in err
        assert "fix:" in err

    def test_bare_postgresql_scheme_rejected_with_guided_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        # Papercut: bare `postgresql://` resolves to psycopg2 in
        # SQLAlchemy but we ship only psycopg v3, producing a confusing
        # `ModuleNotFoundError: psycopg2` traceback at create_engine
        # time. We catch this at the URL boundary with a guided error
        # pointing at the correct scheme.
        store_path = tmp_path / "schemabrain.db"
        exit_code = main(["index", "postgresql://user:pw@host/db", "--store-path", str(store_path)])
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "psycopg v3" in err
        # The fix line must include the EXACT corrected URL — the
        # user shouldn't have to figure out the rewrite themselves.
        assert "postgresql+psycopg://user:pw@host/db" in err

    def test_psycopg2_explicit_scheme_rejected_with_guided_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        store_path = tmp_path / "schemabrain.db"
        exit_code = main(
            ["index", "postgresql+psycopg2://host/db", "--store-path", str(store_path)]
        )
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "psycopg+psycopg2" in err or "psycopg v3" in err

    def test_asyncpg_scheme_rejected_with_guided_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        store_path = tmp_path / "schemabrain.db"
        exit_code = main(["index", "postgresql+asyncpg://host/db", "--store-path", str(store_path)])
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "asyncpg" in err

    def test_warns_when_no_tables_indexed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ):
        """When list_tables returns empty, print a warning to stderr."""

        class _EmptySource:
            def __init__(self, url: str) -> None:
                pass

            def __enter__(self) -> "_EmptySource":
                return self

            def __exit__(self, *_args: object) -> None:
                pass

            def list_tables(self, schema: str | None = None) -> list[tuple[str, str]]:
                return []

            def get_table(self, name: str, schema: str):
                # Never called when list_tables returns empty, but the
                # Protocol still requires this method.
                raise NotImplementedError

            def close(self) -> None:
                pass

        from schemabrain.core.models import Table as _Table
        from schemabrain.profiler.stats import ColumnStats as _ColumnStats

        class _StubProfiler:
            def __init__(self, url: str) -> None:
                pass

            def __enter__(self) -> "_StubProfiler":
                return self

            def __exit__(self, *_args: object) -> None:
                pass

            def profile_table(self, table: _Table) -> dict[str, _ColumnStats]:
                return {}

            def close(self) -> None:
                pass

        # Sanity: the stubs must satisfy the Protocols the indexer
        # programs against, otherwise this test could pass while the
        # real CLI flow breaks on a Protocol divergence.
        from schemabrain.connectors.base import DataSource
        from schemabrain.profiler.base import Profiler

        assert isinstance(_EmptySource("postgresql+psycopg://fake/db"), DataSource)
        assert isinstance(_StubProfiler("postgresql+psycopg://fake/db"), Profiler)

        monkeypatch.setattr("schemabrain.cli.PostgresDataSource", _EmptySource)
        monkeypatch.setattr("schemabrain.cli.PostgresProfiler", _StubProfiler)
        store_path = tmp_path / "schemabrain.db"
        # --no-enrich so we don't need ANTHROPIC_API_KEY for this test.
        exit_code = main(
            [
                "index",
                "postgresql+psycopg://fake/db",
                "--store-path",
                str(store_path),
                "--no-enrich",
            ]
        )
        assert exit_code == 0
        assert "no tables indexed" in capsys.readouterr().err


class TestEnrichmentCliFlags:
    def test_missing_api_key_without_no_enrich_returns_nonzero(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ):
        # Without ANTHROPIC_API_KEY and without --no-enrich, the CLI must
        # refuse to run rather than silently fall back to a no-LLM mode.
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        store_path = tmp_path / "schemabrain.db"
        exit_code = main(["index", "postgresql+psycopg://fake/db", "--store-path", str(store_path)])
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "ANTHROPIC_API_KEY" in err
        # Guided-error format with a remediation pointer to --no-enrich
        # so the user can unblock themselves without first fetching a key.
        assert "--no-enrich" in err
        assert "fix:" in err

    def test_no_enrich_skips_api_key_requirement(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        class _EmptySource:
            def __init__(self, url: str) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def list_tables(self, schema: str | None = None) -> list[tuple[str, str]]:
                return []

            def get_table(self, name: str, schema: str):
                raise NotImplementedError

            def close(self) -> None:
                pass

        class _StubProfiler:
            def __init__(self, url: str) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def profile_table(self, table):
                return {}

            def close(self) -> None:
                pass

        monkeypatch.setattr("schemabrain.cli.PostgresDataSource", _EmptySource)
        monkeypatch.setattr("schemabrain.cli.PostgresProfiler", _StubProfiler)
        store_path = tmp_path / "schemabrain.db"
        exit_code = main(
            [
                "index",
                "postgresql+psycopg://fake/db",
                "--store-path",
                str(store_path),
                "--no-enrich",
            ]
        )
        assert exit_code == 0

    def test_max_cost_default_is_one_dollar(self) -> None:
        """Default cap intentionally low so a fresh user's first `index`
        can't surprise-spend more than $1 before they understand what's
        happening. Override with `--max-cost N` or `--no-cost-cap`.
        """
        from schemabrain.cli import _DEFAULT_MAX_COST_USD

        assert _DEFAULT_MAX_COST_USD == 1.0

    def test_no_cost_cap_overrides_max_cost(self) -> None:
        """When both are passed, `--no-cost-cap` wins. argparse parses
        both; the CLI resolves to the no-cap sentinel.
        """
        import argparse

        from schemabrain.cli import _build_parser, _resolve_max_cost

        parser: argparse.ArgumentParser = _build_parser()
        args = parser.parse_args(
            [
                "index",
                "postgresql+psycopg://x/y",
                "--max-cost",
                "5",
                "--no-cost-cap",
            ]
        )
        assert _resolve_max_cost(args) >= 1e9  # effectively unlimited

    def test_max_cost_flag_still_works(self) -> None:
        from schemabrain.cli import _build_parser, _resolve_max_cost

        parser = _build_parser()
        args = parser.parse_args(
            [
                "index",
                "postgresql+psycopg://x/y",
                "--max-cost",
                "2.5",
            ]
        )
        assert _resolve_max_cost(args) == 2.5

    def test_pipeline_wired_with_store_and_source_id(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The CLI must construct `EnrichmentPipeline` with `store=`
        and `source_connection_id=` so cumulative spend persists across
        runs. Without this wiring, the cost cap is per-process only —
        a fresh `index` run resets to $0 even if previous runs already
        spent the full cap.
        """
        captured: dict[str, object] = {}

        import schemabrain.cli as cli_mod
        from schemabrain.enrichment.pipeline import EnrichmentPipeline

        original_init = EnrichmentPipeline.__init__

        # Mirror the production signature so a future positional
        # parameter on `EnrichmentPipeline.__init__` produces a test
        # failure rather than silently passing.
        def _capturing_init(
            self: object,
            *,
            store: object = None,
            source_connection_id: object = None,
            **rest: object,
        ) -> None:
            captured["store"] = store
            captured["source_connection_id"] = source_connection_id
            original_init(
                self,
                store=store,
                source_connection_id=source_connection_id,
                **rest,
            )

        monkeypatch.setattr(EnrichmentPipeline, "__init__", _capturing_init)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

        # Stub the LLM clients so no real Anthropic calls happen.
        monkeypatch.setattr(cli_mod, "anthropic_haiku_45_client", lambda **_: object())
        monkeypatch.setattr(cli_mod, "anthropic_sonnet_46_client", lambda **_: object())
        # Stub fastembed so no ONNX download.
        monkeypatch.setattr(cli_mod, "fastembed_default", lambda: None)
        # Stub the indexer so we don't need a real Postgres.
        monkeypatch.setattr(
            cli_mod,
            "PostgresDataSource",
            lambda _url: __import__("contextlib").nullcontext(enter_result=object()),
        )
        monkeypatch.setattr(
            cli_mod,
            "PostgresProfiler",
            lambda _url: __import__("contextlib").nullcontext(enter_result=object()),
        )
        from schemabrain.indexer import IndexResult

        monkeypatch.setattr(
            cli_mod,
            "index",
            lambda **_: IndexResult(
                tables_seen=0,
                tables_changed=0,
                tables_unchanged=0,
                tables_removed=0,
                columns_added=0,
                columns_changed=0,
                columns_removed=0,
            ),
        )

        store_path = tmp_path / "schemabrain.db"
        cli_mod.main(
            [
                "index",
                "postgresql+psycopg://fake/db",
                "--store-path",
                str(store_path),
            ]
        )

        assert captured["store"] is not None, (
            "CLI must pass store= into EnrichmentPipeline so the cumulative "
            "cost ledger persists across runs"
        )
        # Equality check, not just is-not-None: an off-by-construction
        # empty string would pass the latter but break the ledger keying.
        expected_source_id = cli_mod._make_source_id("postgresql+psycopg://fake/db")
        assert captured["source_connection_id"] == expected_source_id, (
            f"CLI must pass the canonical source_id; "
            f"got {captured['source_connection_id']!r}, expected {expected_source_id!r}"
        )

    def test_no_cost_cap_alone_returns_sentinel(self) -> None:
        """Coverage gap closed: `--no-cost-cap` passed WITHOUT an
        explicit `--max-cost`. The implementation short-circuits before
        reading `args.max_cost`, but a future refactor that removes that
        short-circuit would only fail on this combination.
        """
        from schemabrain.cli import (
            _NO_COST_CAP_SENTINEL,
            _build_parser,
            _resolve_max_cost,
        )

        args = _build_parser().parse_args(
            [
                "index",
                "postgresql+psycopg://x/y",
                "--no-cost-cap",
            ]
        )
        assert _resolve_max_cost(args) == _NO_COST_CAP_SENTINEL

    def test_default_resolved_cap_is_one(self) -> None:
        from schemabrain.cli import _build_parser, _resolve_max_cost

        parser = _build_parser()
        args = parser.parse_args(
            [
                "index",
                "postgresql+psycopg://x/y",
            ]
        )
        assert _resolve_max_cost(args) == 1.0

    def test_enable_sonnet_default_is_off(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ):
        # Default index run must NOT construct a Sonnet client — Sonnet
        # is opt-in to keep automatic runs cheap. Verify by stubbing the
        # Sonnet factory and asserting it was never called.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")

        sonnet_calls: list[dict] = []

        def _track_sonnet(**kwargs):
            sonnet_calls.append(kwargs)
            raise AssertionError("Sonnet factory must not be called without --enable-sonnet")

        monkeypatch.setattr("schemabrain.cli.anthropic_sonnet_46_client", _track_sonnet)

        class _EmptySource:
            def __init__(self, url: str) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def list_tables(self, schema: str | None = None) -> list[tuple[str, str]]:
                return []

            def get_table(self, name: str, schema: str):
                raise NotImplementedError

            def close(self) -> None:
                pass

        class _StubProfiler:
            def __init__(self, url: str) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def profile_table(self, table):
                return {}

            def close(self) -> None:
                pass

        monkeypatch.setattr("schemabrain.cli.PostgresDataSource", _EmptySource)
        monkeypatch.setattr("schemabrain.cli.PostgresProfiler", _StubProfiler)
        store_path = tmp_path / "schemabrain.db"
        exit_code = main(["index", "postgresql+psycopg://fake/db", "--store-path", str(store_path)])
        assert exit_code == 0
        assert sonnet_calls == []

    def test_enable_sonnet_constructs_cryptic_tier(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ):
        # With --enable-sonnet, the CLI constructs the Sonnet client and
        # passes it to the pipeline as `cryptic_client`. Verify by spying
        # on both factory calls.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")

        haiku_calls: list[dict] = []
        sonnet_calls: list[dict] = []

        class _DummyClient:
            model = "claude-haiku-4-5"

            def complete(self, *, system, user):
                raise NotImplementedError("no tables → no calls")

        class _DummySonnetClient:
            model = "claude-sonnet-4-6"

            def complete(self, *, system, user):
                raise NotImplementedError("no tables → no calls")

        def _track_haiku(**kwargs):
            haiku_calls.append(kwargs)
            return _DummyClient()

        def _track_sonnet(**kwargs):
            sonnet_calls.append(kwargs)
            return _DummySonnetClient()

        monkeypatch.setattr("schemabrain.cli.anthropic_haiku_45_client", _track_haiku)
        monkeypatch.setattr("schemabrain.cli.anthropic_sonnet_46_client", _track_sonnet)

        class _EmptySource:
            def __init__(self, url: str) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def list_tables(self, schema: str | None = None) -> list[tuple[str, str]]:
                return []

            def get_table(self, name: str, schema: str):
                raise NotImplementedError

            def close(self) -> None:
                pass

        class _StubProfiler:
            def __init__(self, url: str) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def profile_table(self, table):
                return {}

            def close(self) -> None:
                pass

        monkeypatch.setattr("schemabrain.cli.PostgresDataSource", _EmptySource)
        monkeypatch.setattr("schemabrain.cli.PostgresProfiler", _StubProfiler)
        store_path = tmp_path / "schemabrain.db"
        exit_code = main(
            [
                "index",
                "postgresql+psycopg://fake/db",
                "--store-path",
                str(store_path),
                "--enable-sonnet",
            ]
        )
        assert exit_code == 0
        assert len(haiku_calls) == 1
        assert len(sonnet_calls) == 1
        # Same API key plumbed to both.
        assert haiku_calls[0]["api_key"] == sonnet_calls[0]["api_key"] == "sk-ant-fake"

    def test_default_constructs_embedder(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ):
        # Default index run (no flags) must construct the fastembed
        # embedder. Stub the factory to capture the call without
        # downloading the 70MB ONNX model.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")

        embedder_calls: list[None] = []

        class _DummyEmbedder:
            model_name = "fake-emb"
            dimension = 4

            def embed(self, text: str) -> tuple[float, ...]:
                return (0.0, 0.0, 0.0, 0.0)

        def _track_embedder():
            embedder_calls.append(None)
            return _DummyEmbedder()

        monkeypatch.setattr("schemabrain.cli.fastembed_default", _track_embedder)

        class _EmptySource:
            def __init__(self, url: str) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def list_tables(self, schema: str | None = None) -> list[tuple[str, str]]:
                return []

            def get_table(self, name: str, schema: str):
                raise NotImplementedError

            def close(self) -> None:
                pass

        class _StubProfiler:
            def __init__(self, url: str) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def profile_table(self, table):
                return {}

            def close(self) -> None:
                pass

        monkeypatch.setattr("schemabrain.cli.PostgresDataSource", _EmptySource)
        monkeypatch.setattr("schemabrain.cli.PostgresProfiler", _StubProfiler)
        store_path = tmp_path / "schemabrain.db"
        exit_code = main(["index", "postgresql+psycopg://fake/db", "--store-path", str(store_path)])
        assert exit_code == 0
        assert len(embedder_calls) == 1

    def test_no_embed_skips_embedder(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ):
        # --no-embed must NOT call the fastembed factory.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")

        def _fail_if_called():
            raise AssertionError("fastembed_default must not be called with --no-embed")

        monkeypatch.setattr("schemabrain.cli.fastembed_default", _fail_if_called)

        class _EmptySource:
            def __init__(self, url: str) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def list_tables(self, schema: str | None = None) -> list[tuple[str, str]]:
                return []

            def get_table(self, name: str, schema: str):
                raise NotImplementedError

            def close(self) -> None:
                pass

        class _StubProfiler:
            def __init__(self, url: str) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def profile_table(self, table):
                return {}

            def close(self) -> None:
                pass

        monkeypatch.setattr("schemabrain.cli.PostgresDataSource", _EmptySource)
        monkeypatch.setattr("schemabrain.cli.PostgresProfiler", _StubProfiler)
        store_path = tmp_path / "schemabrain.db"
        exit_code = main(
            ["index", "postgresql+psycopg://fake/db", "--store-path", str(store_path), "--no-embed"]
        )
        assert exit_code == 0

    def test_no_enrich_implies_no_embedder(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ):
        # With --no-enrich there are no descriptions, so the embedder is
        # pointless. Verify the factory is not called.
        def _fail_if_called():
            raise AssertionError("fastembed_default must not be called when --no-enrich is set")

        monkeypatch.setattr("schemabrain.cli.fastembed_default", _fail_if_called)

        class _EmptySource:
            def __init__(self, url: str) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def list_tables(self, schema: str | None = None) -> list[tuple[str, str]]:
                return []

            def get_table(self, name: str, schema: str):
                raise NotImplementedError

            def close(self) -> None:
                pass

        class _StubProfiler:
            def __init__(self, url: str) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def profile_table(self, table):
                return {}

            def close(self) -> None:
                pass

        monkeypatch.setattr("schemabrain.cli.PostgresDataSource", _EmptySource)
        monkeypatch.setattr("schemabrain.cli.PostgresProfiler", _StubProfiler)
        store_path = tmp_path / "schemabrain.db"
        exit_code = main(
            [
                "index",
                "postgresql+psycopg://fake/db",
                "--store-path",
                str(store_path),
                "--no-enrich",
            ]
        )
        assert exit_code == 0

    def test_with_api_key_constructs_pipeline_and_runs(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ):
        # With ANTHROPIC_API_KEY set, the CLI builds the pipeline. We
        # don't want to make a real call, so stub out the source/profiler
        # to return empty (no tables → no LLM call) AND rely on the
        # Anthropic factory's lazy SDK construction (no network call
        # until messages.create) to avoid burning real credits.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")

        class _EmptySource:
            def __init__(self, url: str) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def list_tables(self, schema: str | None = None) -> list[tuple[str, str]]:
                return []

            def get_table(self, name: str, schema: str):
                raise NotImplementedError

            def close(self) -> None:
                pass

        class _StubProfiler:
            def __init__(self, url: str) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def profile_table(self, table):
                return {}

            def close(self) -> None:
                pass

        monkeypatch.setattr("schemabrain.cli.PostgresDataSource", _EmptySource)
        monkeypatch.setattr("schemabrain.cli.PostgresProfiler", _StubProfiler)

        store_path = tmp_path / "schemabrain.db"
        # No --no-enrich → exercises the pipeline construction path.
        exit_code = main(["index", "postgresql+psycopg://fake/db", "--store-path", str(store_path)])
        assert exit_code == 0

    def test_cost_cap_exceeded_returns_exit_3(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ):
        # Stub the source to return one table, the profiler to return
        # empty stats, and the Anthropic Haiku factory with a fake that
        # produces enormous output → trips a tiny --max-cost cap.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")

        from schemabrain.core.models import Column, Table
        from schemabrain.enrichment.llm import LLMResponse, LLMUsage

        users = Table(
            name="users",
            schema_name="public",
            columns=(
                Column(
                    name="id",
                    table_name="users",
                    schema_name="public",
                    data_type="BIGINT",
                    nullable=False,
                    ordinal_position=1,
                    is_primary_key=True,
                ),
                Column(
                    name="email",
                    table_name="users",
                    schema_name="public",
                    data_type="TEXT",
                    nullable=False,
                    ordinal_position=2,
                ),
            ),
        )

        class _OneTableSource:
            def __init__(self, url: str) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def list_tables(self, schema: str | None = None) -> list[tuple[str, str]]:
                return [("public", "users")]

            def get_table(self, name: str, schema: str):
                return users

            def close(self) -> None:
                pass

        class _StubProfiler:
            def __init__(self, url: str) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def profile_table(self, table):
                return {}

            def close(self) -> None:
                pass

        class _ExpensiveClient:
            def __init__(self, *, api_key: str | None = None) -> None:
                self.model = "claude-haiku-4-5"

            def complete(self, *, system: str, user: str) -> LLMResponse:
                # Each call costs ~$1, way over the 0.01 cap.
                return LLMResponse(
                    text="x",
                    model=self.model,
                    usage=LLMUsage(
                        input_tokens=1_000_000, cached_input_tokens=0, output_tokens=100
                    ),
                )

            def cost_usd(self, usage: LLMUsage) -> float:
                # `LLMClient` Protocol: per-client cost dispatch (seams
                # commit). Haiku rate matches `self.model`.
                from schemabrain.enrichment.llm import haiku_45_cost_usd

                return haiku_45_cost_usd(usage)

        monkeypatch.setattr("schemabrain.cli.PostgresDataSource", _OneTableSource)
        monkeypatch.setattr("schemabrain.cli.PostgresProfiler", _StubProfiler)
        # Stub the Haiku factory: the CLI calls
        # `anthropic_haiku_45_client(api_key=...)` and the return value
        # is what gets handed to EnrichmentPipeline as `client=`. We
        # return an `_ExpensiveClient` directly so the pipeline talks
        # to it without an SDK round-trip.
        monkeypatch.setattr(
            "schemabrain.cli.anthropic_haiku_45_client",
            lambda *, api_key=None: _ExpensiveClient(api_key=api_key),
        )
        # `concurrency=1` makes the per-task cap check strict so the
        # second call after a cap-breaching first is refused
        # deterministically. Under default concurrency=8, all in-flight
        # tasks complete before any cap check sees the breach — fine
        # for production but not for this regression test.
        monkeypatch.setattr("schemabrain.cli._PIPELINE_DEFAULT_CONCURRENCY", 1)
        monkeypatch.setattr("schemabrain.cli._PIPELINE_CRYPTIC_CONCURRENCY", 1)

        store_path = tmp_path / "schemabrain.db"
        exit_code = main(
            [
                "index",
                "postgresql+psycopg://fake/db",
                "--store-path",
                str(store_path),
                "--max-cost",
                "0.01",
            ]
        )
        assert exit_code == 3
        assert "cap" in capsys.readouterr().err.lower()

    def test_cost_cap_closes_reporter_before_printing_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ):
        # Ordering regression test: when CostCapExceeded fires, the live
        # progress widget MUST be torn down BEFORE the "error: ..." line
        # prints to stderr. Otherwise rich's live render thread can
        # repaint a stale bar over the error message (observed during
        # manual testing as: "error:..." line followed by a final bar
        # frame). The fix lives in cli._cmd_index's nested try/finally
        # — reporter.close() runs inside the inner finally, BEFORE the
        # outer except CostCapExceeded handler prints anything.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")

        from schemabrain.core.models import Column, Table
        from schemabrain.enrichment.llm import LLMResponse, LLMUsage

        # Two columns so the second `enrich_column` call trips the cap
        # check that runs BEFORE each call. With one column, the cap
        # check sees zero spend at call-time and the only call goes
        # through cleanly — no CostCapExceeded raised.
        users = Table(
            name="users",
            schema_name="public",
            columns=(
                Column(
                    name="id",
                    table_name="users",
                    schema_name="public",
                    data_type="BIGINT",
                    nullable=False,
                    ordinal_position=1,
                    is_primary_key=True,
                ),
                Column(
                    name="email",
                    table_name="users",
                    schema_name="public",
                    data_type="TEXT",
                    nullable=False,
                    ordinal_position=2,
                ),
            ),
        )

        class _OneTableSource:
            def __init__(self, url: str) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def list_tables(self, schema: str | None = None) -> list[tuple[str, str]]:
                return [("public", "users")]

            def get_table(self, name: str, schema: str):
                return users

            def close(self) -> None:
                pass

        class _StubProfiler:
            def __init__(self, url: str) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def profile_table(self, table):
                return {}

            def close(self) -> None:
                pass

        class _ExpensiveClient:
            def __init__(self, *, api_key: str | None = None) -> None:
                self.model = "claude-haiku-4-5"

            def complete(self, *, system: str, user: str) -> LLMResponse:
                return LLMResponse(
                    text="x",
                    model=self.model,
                    usage=LLMUsage(
                        input_tokens=1_000_000, cached_input_tokens=0, output_tokens=100
                    ),
                )

            def cost_usd(self, usage: LLMUsage) -> float:
                from schemabrain.enrichment.llm import haiku_45_cost_usd

                return haiku_45_cost_usd(usage)

        monkeypatch.setattr("schemabrain.cli.PostgresDataSource", _OneTableSource)
        monkeypatch.setattr("schemabrain.cli.PostgresProfiler", _StubProfiler)
        monkeypatch.setattr(
            "schemabrain.cli.anthropic_haiku_45_client",
            lambda *, api_key=None: _ExpensiveClient(api_key=api_key),
        )
        # `concurrency=1` for deterministic cap-trip ordering — see
        # the cap-test rationale on `test_cost_cap_exceeded_returns_exit_3`.
        monkeypatch.setattr("schemabrain.cli._PIPELINE_DEFAULT_CONCURRENCY", 1)
        monkeypatch.setattr("schemabrain.cli._PIPELINE_CRYPTIC_CONCURRENCY", 1)

        # Record whether close() fired before "error:" hit stderr. We
        # use sys.stderr directly rather than capsys.readouterr() inside
        # close() because readouterr resets the capture buffer, which
        # would hide the subsequent error line from the outer assertion.
        import sys as _sys

        events: list[str] = []

        class _OrderingReporter(NullReporter):
            def close(self) -> None:
                # Inspect the underlying capture stream that capsys is
                # buffering into. If "error:" is in there at this
                # moment, close() ran AFTER the print — bug.
                buf = getattr(_sys.stderr, "getvalue", lambda: "")()
                events.append("close: error_in_stderr=" + str("error:" in buf))

        # Force the CLI to pick OUR reporter regardless of TTY state.
        monkeypatch.setattr(
            "schemabrain.cli._build_index_reporter",
            lambda *, quiet: _OrderingReporter(),
        )

        store_path = tmp_path / "schemabrain.db"
        exit_code = main(
            [
                "index",
                "postgresql+psycopg://fake/db",
                "--store-path",
                str(store_path),
                "--max-cost",
                "0.01",
            ]
        )
        assert exit_code == 3
        # When close() ran, stderr should NOT have contained "error:"
        # yet — that's the whole point of the ordering fix.
        assert events == ["close: error_in_stderr=False"], (
            f"reporter.close() ran AFTER the error message was printed; events={events}"
        )
        # And the error line did eventually appear in captured stderr.
        assert "cap" in capsys.readouterr().err.lower()

    def test_anthropic_auth_error_renders_guided(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ):
        # Stub everything so we reach the enrichment loop, then have
        # the fake LLM client raise `anthropic.AuthenticationError`.
        # Without the guided-error layer this would surface as a raw
        # 401 traceback spanning httpx → anthropic SDK → indexer; we
        # catch the typed exception and render a guided block pointing
        # at the console.anthropic.com key page + the --no-enrich
        # escape hatch.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")

        import anthropic
        import httpx

        from schemabrain.core.models import Column, Table

        users = Table(
            name="users",
            schema_name="public",
            columns=(
                Column(
                    name="id",
                    table_name="users",
                    schema_name="public",
                    data_type="BIGINT",
                    nullable=False,
                    ordinal_position=1,
                    is_primary_key=True,
                ),
            ),
        )

        class _OneTableSource:
            def __init__(self, url: str) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def list_tables(self, schema: str | None = None) -> list[tuple[str, str]]:
                return [("public", "users")]

            def get_table(self, name: str, schema: str):
                return users

            def close(self) -> None:
                pass

        class _StubProfiler:
            def __init__(self, url: str) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def profile_table(self, table):
                return {}

            def close(self) -> None:
                pass

        class _AuthFailingClient:
            def __init__(self, *, api_key: str | None = None) -> None:
                self.model = "claude-haiku-4-5"

            def complete(self, *, system: str, user: str):
                # Construct a real anthropic.AuthenticationError so the
                # isinstance check in cli.py matches.
                request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
                response = httpx.Response(401, request=request)
                raise anthropic.AuthenticationError(
                    "invalid x-api-key", response=response, body={"error": "unauthorized"}
                )

        monkeypatch.setattr("schemabrain.cli.PostgresDataSource", _OneTableSource)
        monkeypatch.setattr("schemabrain.cli.PostgresProfiler", _StubProfiler)
        monkeypatch.setattr(
            "schemabrain.cli.anthropic_haiku_45_client",
            lambda *, api_key=None: _AuthFailingClient(api_key=api_key),
        )

        store_path = tmp_path / "schemabrain.db"
        exit_code = main(
            [
                "index",
                "postgresql+psycopg://fake/db",
                "--store-path",
                str(store_path),
            ]
        )
        assert exit_code == 2
        err = capsys.readouterr().err
        # Translator output.
        assert "Anthropic API rejected the key" in err
        assert "console.anthropic.com" in err
        assert "--no-enrich" in err
        # Raw traceback must not escape.
        assert "Traceback" not in err

    def test_postgres_operational_error_renders_guided(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ):
        # Connect to a port nothing is listening on. SQLAlchemy raises
        # `OperationalError` wrapping psycopg's connection-refused
        # error, which `postgres_operational_error` translates into a
        # `postgres_connection_refused` GuidedError. Without the
        # guided-error layer this would surface as a raw traceback.
        store_path = tmp_path / "schemabrain.db"
        # Port 1 is reserved and never accepts connections; tcpip
        # connect() will return ECONNREFUSED before any auth roundtrip,
        # so the test is deterministic regardless of CI environment.
        exit_code = main(
            [
                "index",
                "postgresql+psycopg://nobody:nopw@127.0.0.1:1/nowhere",
                "--store-path",
                str(store_path),
                "--no-enrich",
            ]
        )
        assert exit_code == 2
        err = capsys.readouterr().err
        # Connection-refused branch text from the translator.
        assert "could not reach the Postgres server" in err
        assert "fix:" in err
        # No raw traceback escaped.
        assert "Traceback" not in err


@pytest.mark.integration
class TestIndexEndToEnd:
    def test_index_populates_store_with_seeded_postgres(
        self, seeded_pg_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        store_path = tmp_path / "schemabrain.db"
        exit_code = main(["index", seeded_pg_url, "--store-path", str(store_path), "--no-enrich"])
        assert exit_code == 0

        with SQLiteStore(store_path) as store:
            indexed = sorted(store.list_tables())

        assert ("public", "users") in indexed
        assert ("public", "orgs") in indexed
        assert ("public", "org_members") in indexed
        assert ("audit", "events") in indexed

    def test_index_writes_correct_table_metadata(
        self, seeded_pg_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        store_path = tmp_path / "schemabrain.db"
        main(["index", seeded_pg_url, "--store-path", str(store_path), "--no-enrich"])

        source_id = _make_source_id(seeded_pg_url)
        with SQLiteStore(store_path) as store:
            users = store.get_table("public", "users", source_connection_id=source_id)

        assert users is not None
        assert users.primary_key_columns() == ("id",)
        col_names = {c.name for c in users.columns}
        assert col_names == {"id", "email", "created_at"}

    def test_index_is_idempotent_on_table_data(
        self, seeded_pg_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        """Re-indexing the same database must produce byte-identical tables.

        Catches the bug class where re-index appends duplicate column rows
        instead of replacing them.
        """
        store_path = tmp_path / "schemabrain.db"
        source_id = _make_source_id(seeded_pg_url)

        main(["index", seeded_pg_url, "--store-path", str(store_path), "--no-enrich"])
        with SQLiteStore(store_path) as store:
            first_tables = sorted(store.list_tables())
            first_users = store.get_table("public", "users", source_connection_id=source_id)
            first_members = store.get_table("public", "org_members", source_connection_id=source_id)

        main(["index", seeded_pg_url, "--store-path", str(store_path), "--no-enrich"])
        with SQLiteStore(store_path) as store:
            second_tables = sorted(store.list_tables())
            second_users = store.get_table("public", "users", source_connection_id=source_id)
            second_members = store.get_table(
                "public", "org_members", source_connection_id=source_id
            )

        assert first_tables == second_tables
        assert first_users == second_users
        assert first_members == second_members
        # Spot-check that we didn't accumulate duplicate column rows
        assert first_users is not None
        assert len(first_users.columns) == 3

    def test_summary_does_not_leak_credentials(
        self, seeded_pg_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        # The seeded_pg_url contains test/test credentials
        store_path = tmp_path / "schemabrain.db"
        main(["index", seeded_pg_url, "--store-path", str(store_path), "--no-enrich"])
        captured = capsys.readouterr()
        # The default testcontainers user/password is "test"; ensure neither
        # the password nor "test:test" credential pair appears in the summary.
        assert "test:test" not in captured.err

    def test_index_prints_summary(
        self, seeded_pg_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        store_path = tmp_path / "schemabrain.db"
        main(["index", seeded_pg_url, "--store-path", str(store_path), "--no-enrich"])
        captured = capsys.readouterr()
        assert "Indexed" in captured.err
        assert "table" in captured.err


class TestIndexReporterWiring:
    """Reporter selection by `_build_index_reporter` for `index`."""

    def test_quiet_returns_null_reporter_regardless_of_tty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Even on a real terminal, --quiet must collapse to NullReporter.
        monkeypatch.setattr("sys.stderr.isatty", lambda: True)
        reporter = _build_index_reporter(quiet=True)
        assert isinstance(reporter, NullReporter)

    def test_non_tty_falls_back_to_null_reporter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Piping stderr to a file: live progress would flood the log
        # with cursor escapes, so default to no-op.
        monkeypatch.setattr("sys.stderr.isatty", lambda: False)
        reporter = _build_index_reporter(quiet=False)
        assert isinstance(reporter, NullReporter)

    def test_tty_without_quiet_returns_rich_reporter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The happy path — a real terminal with no --quiet → live UI.
        from schemabrain.cli_ui import RichReporter

        monkeypatch.setattr("sys.stderr.isatty", lambda: True)
        reporter = _build_index_reporter(quiet=False)
        assert isinstance(reporter, RichReporter)

    def test_quiet_flag_runs_index_without_crashing(
        self, seeded_pg_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # End-to-end smoke: --quiet works against a real Postgres seed,
        # the final summary is still emitted, and no progress chrome
        # appears in the captured output.
        store_path = tmp_path / "schemabrain.db"
        exit_code = main(
            [
                "index",
                seeded_pg_url,
                "--store-path",
                str(store_path),
                "--no-enrich",
                "--quiet",
            ]
        )
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "Indexed" in captured.err

    def test_quiet_flag_emits_no_ansi_chrome_even_on_tty(
        self,
        seeded_pg_url: str,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Negative test: with stderr.isatty()==True, --quiet must still
        # collapse to NullReporter — no progress chrome. A regression
        # that swapped the branch order in `_build_index_reporter` would
        # render rich's Live to the captured stream, leaving ANSI CSI
        # bytes (\x1b[) in the output. This is what the existing
        # "Indexed in stderr" assertion misses.
        monkeypatch.setattr("sys.stderr.isatty", lambda: True)
        store_path = tmp_path / "schemabrain.db"
        main(
            [
                "index",
                seeded_pg_url,
                "--store-path",
                str(store_path),
                "--no-enrich",
                "--quiet",
            ]
        )
        captured = capsys.readouterr()
        # The ESC-[ pair is the CSI introducer for every rich color /
        # cursor-control sequence. Its absence is proof rich's Live
        # never ran.
        assert "\x1b[" not in captured.err
        assert "\x1b[" not in captured.out


@pytest.mark.integration
class TestIndexDryRun:
    """End-to-end behavior of `schemabrain index --dry-run`.

    Anchored on the real seeded Postgres fixture (the only place we
    actually verify the introspection + diff loop drives the cost
    estimator). Reaches `index` flow through `main(...)`.
    """

    def test_dry_run_without_api_key_succeeds(
        self,
        monkeypatch: pytest.MonkeyPatch,
        seeded_pg_url: str,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Core promise of --dry-run: cost-estimate without needing a
        # real API key. A user who's only trying to scope before
        # buying a key MUST be able to run this.
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        store_path = tmp_path / "schemabrain.db"
        exit_code = main(
            [
                "index",
                seeded_pg_url,
                "--store-path",
                str(store_path),
                "--dry-run",
            ]
        )
        assert exit_code == 0
        captured = capsys.readouterr()
        # Dry-run summary format must mark the run as preview-only.
        assert "Would index" in captured.err
        assert "Estimated LLM" in captured.err
        assert "No changes made to the store." in captured.err

    def test_dry_run_creates_no_store_file(
        self,
        monkeypatch: pytest.MonkeyPatch,
        seeded_pg_url: str,
        tmp_path: Path,
    ) -> None:
        # The cache file should NOT exist after --dry-run against a
        # path that didn't exist before. (SQLiteStore opens with
        # parent-dir semantics; a successful open creates the file.
        # But dry_run_index doesn't write anything except what
        # `store.list_tables` triggers — which on an empty store is
        # a SELECT-only path.)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        store_path = tmp_path / "absent.db"
        assert not store_path.exists()
        main(
            [
                "index",
                seeded_pg_url,
                "--store-path",
                str(store_path),
                "--dry-run",
            ]
        )
        # The store file IS created (SQLiteStore opens read-write to
        # honor `list_tables` against possibly-existing schema), but
        # it MUST contain zero rows in the tables index — proving no
        # tables were written.
        import sqlite3

        if store_path.exists():
            conn = sqlite3.connect(store_path)
            try:
                # Tables persisted by `index()` live in the `tables` row.
                rows = conn.execute("SELECT COUNT(*) FROM tables").fetchone()
            finally:
                conn.close()
            assert rows[0] == 0

    def test_dry_run_then_real_index_produces_same_table_counts(
        self,
        seeded_pg_url: str,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # The diff dry-run reports MUST match what a real `index`
        # would do. Run --dry-run, capture the table-changed count
        # from its summary, then run a real --no-enrich index and
        # confirm the same count appears. Without this we can't
        # trust the dry-run as a planning artifact.
        store_path = tmp_path / "schemabrain.db"
        # Dry-run first (cache is empty → "would index N changed").
        main(
            [
                "index",
                seeded_pg_url,
                "--store-path",
                str(store_path),
                "--dry-run",
                "--no-enrich",
            ]
        )
        dry_err = capsys.readouterr().err

        import re

        m = re.search(r"Would index (\d+) table\(s\): (\d+) changed", dry_err)
        assert m is not None, f"dry-run summary did not match expected pattern: {dry_err!r}"
        dry_seen, dry_changed = int(m.group(1)), int(m.group(2))

        # Real run on the same cache-state (now non-existent file).
        main(
            [
                "index",
                seeded_pg_url,
                "--store-path",
                str(store_path),
                "--no-enrich",
            ]
        )
        real_err = capsys.readouterr().err

        m_real = re.search(r"Indexed (\d+) table\(s\): (\d+) changed", real_err)
        assert m_real is not None
        real_seen, real_changed = int(m_real.group(1)), int(m_real.group(2))

        assert dry_seen == real_seen
        assert dry_changed == real_changed

    def test_dry_run_no_enrich_zeroes_estimate(
        self,
        seeded_pg_url: str,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        store_path = tmp_path / "schemabrain.db"
        exit_code = main(
            [
                "index",
                seeded_pg_url,
                "--store-path",
                str(store_path),
                "--dry-run",
                "--no-enrich",
            ]
        )
        assert exit_code == 0
        err = capsys.readouterr().err
        # `--no-enrich` means no LLM would fire, so the Estimated LLM
        # line is absent (summary() suppresses the LLM clause when
        # both descriptions_generated == 0 and llm_cost_usd == 0).
        assert "Estimated LLM" not in err
        assert "Would index" in err

    def test_dry_run_since_renders_freshness_audit(
        self,
        seeded_pg_url: str,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # End-to-end: real index against seeded Postgres, then a
        # --dry-run --since on a generous window. The store rows have
        # `indexed_at = now`, so a future cutoff classifies everything
        # as stale; the audit line names the column count.
        store_path = tmp_path / "schemabrain.db"
        # Real index first so the cache has stamped rows.
        main(
            [
                "index",
                seeded_pg_url,
                "--store-path",
                str(store_path),
                "--no-enrich",
            ]
        )
        capsys.readouterr()  # discard

        # Dry-run with --since at a far-future ts: every cached row
        # has `indexed_at` before "now + 1 year", so the freshness
        # audit reports the full cached set.
        future = (datetime.now(UTC) + timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")
        exit_code = main(
            [
                "index",
                seeded_pg_url,
                "--store-path",
                str(store_path),
                "--dry-run",
                "--since",
                future,
            ]
        )
        assert exit_code == 0
        err = capsys.readouterr().err
        assert "Stale since" in err
        assert "estimated refresh $" in err

    def test_dry_run_since_zero_stale_still_prints_line(
        self,
        seeded_pg_url: str,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Inverse: cutoff before any cached row. Nothing is stale, but
        # the audit line MUST still render so operators know the flag
        # took effect.
        store_path = tmp_path / "schemabrain.db"
        main(
            [
                "index",
                seeded_pg_url,
                "--store-path",
                str(store_path),
                "--no-enrich",
            ]
        )
        capsys.readouterr()

        past = (datetime.now(UTC) - timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")
        main(
            [
                "index",
                seeded_pg_url,
                "--store-path",
                str(store_path),
                "--dry-run",
                "--since",
                past,
            ]
        )
        err = capsys.readouterr().err
        assert "Stale since" in err
        assert "0 columns" in err

    def test_dry_run_since_malformed_returns_two(
        self,
        seeded_pg_url: str,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        store_path = tmp_path / "schemabrain.db"
        exit_code = main(
            [
                "index",
                seeded_pg_url,
                "--store-path",
                str(store_path),
                "--dry-run",
                "--since",
                "not-a-duration",
            ]
        )
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "--since" in err

    def test_dry_run_help_text_lists_flag(self) -> None:
        from schemabrain.cli import _build_parser

        parser = _build_parser()
        # Smoke: `schemabrain index --dry-run` parses as a flag.
        ns = parser.parse_args(["index", "postgresql+psycopg://x", "--dry-run"])
        assert ns.dry_run is True
        ns2 = parser.parse_args(["index", "postgresql+psycopg://x"])
        assert ns2.dry_run is False

    def test_dry_run_postgres_unreachable_renders_guided(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Dry-run still needs to reach the source for `list_tables` +
        # `get_table` introspection, so connection errors should
        # surface through the same guided-error translator the real
        # `index` path uses. Mirrors `test_postgres_operational_error_renders_guided`
        # for the dry-run code path.
        store_path = tmp_path / "schemabrain.db"
        exit_code = main(
            [
                "index",
                "postgresql+psycopg://nobody:nopw@127.0.0.1:1/nowhere",
                "--store-path",
                str(store_path),
                "--dry-run",
            ]
        )
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "could not reach the Postgres server" in err
        # No raw traceback leaked.
        assert "Traceback" not in err

    def test_dry_run_store_path_unwritable_renders_guided(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # OSError on store init must translate through the guided-error
        # renderer for dry-run too (parallel to the same branch in the
        # real index path). Stubs both PostgresDataSource and SQLiteStore
        # so the test is deterministic regardless of platform — a real
        # SQLite OSError is hard to trigger portably.
        class _NoopSource:
            def __init__(self, url: str) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def list_tables(self, schema: str | None = None) -> list[tuple[str, str]]:
                return []

            def get_table(self, name: str, schema: str):
                raise NotImplementedError

            def close(self) -> None:
                pass

        class _OSErrorStore:
            def __init__(self, path: str) -> None:
                self.path = path

            def __enter__(self):
                raise OSError(f"Permission denied: {self.path}")

            def __exit__(self, *_args):
                return False

        monkeypatch.setattr("schemabrain.cli.PostgresDataSource", _NoopSource)
        monkeypatch.setattr("schemabrain.cli.SQLiteStore", _OSErrorStore)

        store_path = tmp_path / "blocked.db"
        exit_code = main(
            [
                "index",
                "postgresql+psycopg://fake/db",
                "--store-path",
                str(store_path),
                "--dry-run",
            ]
        )
        assert exit_code == 2
        err = capsys.readouterr().err
        # Guided format from `store_path_unwritable` translator.
        assert "could not open the local store" in err
        assert "Permission denied" in err
        assert "Traceback" not in err

    def test_dry_run_warns_on_empty_source(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Empty schemas are a real footgun (user typed the wrong dbname,
        # or RLS hides everything). Dry-run should still succeed and
        # emit a warning so the user notices the empty result before
        # building plans around a zero-cost estimate.
        class _EmptySource:
            def __init__(self, url: str) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def list_tables(self, schema: str | None = None) -> list[tuple[str, str]]:
                return []

            def get_table(self, name: str, schema: str):
                raise NotImplementedError

            def close(self) -> None:
                pass

        monkeypatch.setattr("schemabrain.cli.PostgresDataSource", _EmptySource)
        store_path = tmp_path / "schemabrain.db"
        exit_code = main(
            [
                "index",
                "postgresql+psycopg://fake/empty",
                "--store-path",
                str(store_path),
                "--dry-run",
                "--no-enrich",
            ]
        )
        assert exit_code == 0
        err = capsys.readouterr().err
        assert "Would index 0 table(s)" in err
        assert "no user-visible tables" in err
        assert "dry-run produced an empty diff" in err


class TestEvalSubcommandValidation:
    """The `eval` subcommand's argparse + early validation paths."""

    def test_eval_requires_source(self, capsys: pytest.CaptureFixture[str]) -> None:
        # Q1: --source dropped its argparse-level `required=True` so it
        # can be supplied either way (--source or --url-env). Missing
        # both still fails — but via our guided-error path, not argparse.
        exit_code = main(["eval"])
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "error:" in err
        assert "--url-env" in err

    def test_eval_malformed_source_returns_exit_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        store_path = tmp_path / "schemabrain.db"
        exit_code = main(
            [
                "eval",
                "--source",
                "not-a-url",
                "--store-path",
                str(store_path),
            ]
        )
        assert exit_code == 2
        assert "error" in capsys.readouterr().err.lower()

    def test_eval_missing_golden_file_returns_exit_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        store_path = tmp_path / "schemabrain.db"
        exit_code = main(
            [
                "eval",
                "--source",
                "postgresql+psycopg://fake/db",
                "--store-path",
                str(store_path),
                "--golden",
                str(tmp_path / "missing.json"),
            ]
        )
        assert exit_code == 2
        assert "not found" in capsys.readouterr().err.lower()

    def test_eval_invalid_golden_file_returns_exit_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        bad = tmp_path / "bad.json"
        bad.write_text("{not valid json")
        store_path = tmp_path / "schemabrain.db"
        exit_code = main(
            [
                "eval",
                "--source",
                "postgresql+psycopg://fake/db",
                "--store-path",
                str(store_path),
                "--golden",
                str(bad),
            ]
        )
        assert exit_code == 2
        assert "invalid" in capsys.readouterr().err.lower()

    def test_eval_default_limit_is_ten(self):
        from schemabrain.cli import _DEFAULT_EVAL_LIMIT

        assert _DEFAULT_EVAL_LIMIT == 10


class TestEvalSubcommandIntegration:
    """End-to-end through the eval CLI against a real (empty + populated)
    SQLite store, no Postgres required."""

    def _seed_store(self, store_path: Path, source_id: str) -> None:
        from schemabrain.core.description import ColumnDescription
        from schemabrain.core.models import Column, Table
        from schemabrain.core.store import SQLiteStore

        users = Table(
            name="users",
            schema_name="public",
            columns=(
                Column(
                    name="id",
                    table_name="users",
                    schema_name="public",
                    data_type="BIGINT",
                    nullable=False,
                    ordinal_position=1,
                    is_primary_key=True,
                ),
                Column(
                    name="email",
                    table_name="users",
                    schema_name="public",
                    data_type="TEXT",
                    nullable=False,
                    ordinal_position=2,
                ),
            ),
        )
        with SQLiteStore(store_path) as store:
            store.write_table(users, source_connection_id=source_id)
            store.write_table_descriptions(
                "public",
                "users",
                source_connection_id=source_id,
                descriptions={
                    "id": ColumnDescription(
                        text="primary user identifier",
                        model="m",
                        prompt_version="v",
                        input_tokens=1,
                        cached_input_tokens=0,
                        output_tokens=1,
                        cost_usd=0.0,
                    ),
                    "email": ColumnDescription(
                        text="user email address for contact",
                        model="m",
                        prompt_version="v",
                        input_tokens=1,
                        cached_input_tokens=0,
                        output_tokens=1,
                        cost_usd=0.0,
                    ),
                },
            )

    def test_runs_against_seeded_store_and_prints_recall(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        from schemabrain.cli import _make_source_id

        source_url = "postgresql+psycopg://fake-host/eval-test"
        store_path = tmp_path / "store.db"
        self._seed_store(store_path, _make_source_id(source_url))

        # Tiny one-question golden set — `users` table only.
        golden_path = tmp_path / "g.json"
        golden_path.write_text(
            '{"version":"1","schema_description":"t",'
            '"questions":[{"id":"q1","question":"Where do user emails live?",'
            '"expected_tables":["public.users"]}]}'
        )

        exit_code = main(
            [
                "eval",
                "--source",
                source_url,
                "--store-path",
                str(store_path),
                "--golden",
                str(golden_path),
                # Existing seed has descriptions but no embeddings; the
                # default --retriever embedding would return nothing here.
                "--retriever",
                "keyword",
            ]
        )
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "recall@1" in out
        assert "recall@3" in out
        assert "recall@10" in out
        # `users` is the only table in the store and matches "user emails"
        # via both column name and description text → top-1 hit.
        assert "1.000" in out

    def test_runs_against_empty_store(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
        # Eval against a store with no tables for the given source must
        # still print a clean report (all questions miss).
        store_path = tmp_path / "empty.db"
        from schemabrain.core.store import SQLiteStore

        SQLiteStore(store_path).close()  # initializes schema, leaves it empty

        golden_path = tmp_path / "g.json"
        golden_path.write_text(
            '{"version":"1","schema_description":"t",'
            '"questions":[{"id":"q1","question":"x?",'
            '"expected_tables":["public.users"]}]}'
        )
        exit_code = main(
            [
                "eval",
                "--source",
                "postgresql+psycopg://fake-host/empty",
                "--store-path",
                str(store_path),
                "--golden",
                str(golden_path),
                "--retriever",
                "keyword",
            ]
        )
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "recall@1=0.000" in out
        assert "MISS" in out

    def test_uses_bundled_default_golden_when_omitted(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        # Omitting --golden falls back to the bundled e-commerce golden set.
        # Against an empty store, every question misses but the run still
        # succeeds and emits a 10-question report.
        store_path = tmp_path / "empty.db"
        from schemabrain.core.store import SQLiteStore

        SQLiteStore(store_path).close()

        exit_code = main(
            [
                "eval",
                "--source",
                "postgresql+psycopg://fake-host/default-golden-test",
                "--store-path",
                str(store_path),
                "--retriever",
                "keyword",
            ]
        )
        assert exit_code == 0
        out = capsys.readouterr().out
        # Bundled set has 10 questions per the file in this slice.
        assert "10 questions" in out

    def test_embedding_retriever_against_seeded_embeddings(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ):
        # Default --retriever embedding path: seed a store with both
        # descriptions and pre-computed embeddings, monkeypatch
        # fastembed_default to a deterministic test embedder, run eval.
        from schemabrain.cli import _make_source_id
        from schemabrain.core.embedding import ColumnEmbedding
        from schemabrain.core.models import Column, Table
        from schemabrain.core.store import SQLiteStore

        source_url = "postgresql+psycopg://fake-host/embed-eval"
        store_path = tmp_path / "store.db"
        sid = _make_source_id(source_url)

        # 4-dim deterministic vectors. users.email aligns with axis 0.
        users = Table(
            name="users",
            schema_name="public",
            columns=(
                Column(
                    name="email",
                    table_name="users",
                    schema_name="public",
                    data_type="TEXT",
                    nullable=False,
                    ordinal_position=1,
                ),
            ),
        )
        with SQLiteStore(store_path) as s:
            s.write_table(users, source_connection_id=sid)
            s.write_table_embeddings(
                "public",
                "users",
                source_connection_id=sid,
                embeddings={
                    "email": ColumnEmbedding(
                        vector=(1.0, 0.0, 0.0, 0.0), model="test-emb", dimension=4
                    )
                },
            )

        class _AxisZeroEmbedder:
            model_name = "test-emb"
            dimension = 4

            def embed(self, text: str) -> tuple[float, ...]:
                return (1.0, 0.0, 0.0, 0.0)

        monkeypatch.setattr("schemabrain.cli.fastembed_default", _AxisZeroEmbedder)

        golden_path = tmp_path / "g.json"
        golden_path.write_text(
            '{"version":"1","schema_description":"t",'
            '"questions":[{"id":"q1","question":"about user emails",'
            '"expected_tables":["public.users"]}]}'
        )

        exit_code = main(
            [
                "eval",
                "--source",
                source_url,
                "--store-path",
                str(store_path),
                "--golden",
                str(golden_path),
                # No --retriever flag → default to embedding.
            ]
        )
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "1.000" in out  # cosine = 1.0 → recall@1 = 1.000

    def test_embedding_retriever_default_choice(self) -> None:
        # Pin the default for the retriever flag — switching this away
        # from `embedding` should be a deliberate test change, not
        # invisible drift.
        import argparse

        from schemabrain.cli import _build_parser

        parser = _build_parser()
        args = parser.parse_args(
            [
                "eval",
                "--source",
                "postgresql+psycopg://fake/db",
            ]
        )
        assert isinstance(args, argparse.Namespace)
        assert args.retriever == "embedding"


class TestServeSubcommand:
    """The `serve` subcommand. We don't actually run stdio in tests
    (would block); we patch the run_stdio entrypoint and verify the CLI
    plumbing wires source/store correctly into it.
    """

    def test_serve_requires_source(self, capsys: pytest.CaptureFixture[str]) -> None:
        # Q1: --source dropped its argparse-level `required=True` so it
        # can be supplied either way (--source or --url-env). Missing
        # both still fails — but via our guided-error path, not argparse.
        exit_code = main(["serve"])
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "error:" in err
        assert "--url-env" in err

    def test_serve_malformed_source_returns_exit_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store_path = tmp_path / "s.db"
        SQLiteStore(store_path).close()
        exit_code = main(
            [
                "serve",
                "--source",
                "not-a-url",
                "--store-path",
                str(store_path),
            ]
        )
        assert exit_code == 2
        assert "error" in capsys.readouterr().err.lower()

    def test_serve_calls_run_stdio_with_correct_arguments(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Pin the wiring: --source is hashed into the source_connection_id,
        # --store-path is opened, fastembed_default() is constructed, and
        # all three flow into run_stdio. Don't actually serve — capture
        # the call.
        from schemabrain.cli import _make_source_id

        store_path = tmp_path / "store.db"
        SQLiteStore(store_path).close()

        captured: dict[str, object] = {}

        def _capture_run_stdio(
            *,
            store,
            source_connection_id,
            embedder,
            metric_executor=None,
            event_bus=None,
            server_session_id=None,
            audit_writer=None,
            pii_block=frozenset(),
            tracer=None,
        ) -> None:
            captured["store_is_sqlite_store"] = isinstance(store, SQLiteStore)
            captured["source_connection_id"] = source_connection_id
            captured["embedder_is_callable"] = hasattr(embedder, "embed")
            captured["metric_executor_provided"] = metric_executor is not None
            captured["event_bus_provided"] = event_bus is not None

        monkeypatch.setattr("schemabrain.cli.run_stdio", _capture_run_stdio)

        source_url = "postgresql+psycopg://fake/serve-test"
        exit_code = main(
            [
                "serve",
                "--source",
                source_url,
                "--store-path",
                str(store_path),
            ]
        )
        assert exit_code == 0
        assert captured["store_is_sqlite_store"] is True
        assert captured["source_connection_id"] == _make_source_id(source_url)
        assert captured["embedder_is_callable"] is True


class TestFixturePathSubcommand:
    """`schemabrain fixture-path <name>` prints the absolute path to a
    bundled fixture (e.g. `ecommerce.sql`) or golden set (e.g.
    `ecommerce.json`). Designed to be drop-in copy-paste-able inside a
    shell `$(...)` substitution, so stdout must be paste-clean and
    stderr must stay empty on success.
    """

    def test_requires_name(self) -> None:
        with pytest.raises(SystemExit) as exc:
            main(["fixture-path"])
        assert exc.value.code != 0

    def test_prints_absolute_path_for_sql_fixture(self, capsys: pytest.CaptureFixture[str]) -> None:
        exit_code = main(["fixture-path", "ecommerce.sql"])
        assert exit_code == 0
        captured = capsys.readouterr()
        out = captured.out.strip()
        path = Path(out)
        assert path.is_absolute()
        assert path.is_file()
        assert path.name == "ecommerce.sql"
        assert path.parent.name == "fixtures"
        # Paste-clean: no leading/trailing noise, no stderr.
        assert captured.err == ""

    def test_prints_absolute_path_for_golden_set(self, capsys: pytest.CaptureFixture[str]) -> None:
        exit_code = main(["fixture-path", "ecommerce.json"])
        assert exit_code == 0
        out = capsys.readouterr().out.strip()
        path = Path(out)
        assert path.is_absolute()
        assert path.is_file()
        assert path.name == "ecommerce.json"
        assert path.parent.name == "golden_sets"

    def test_unknown_name_returns_exit_2(self, capsys: pytest.CaptureFixture[str]) -> None:
        exit_code = main(["fixture-path", "nonexistent.txt"])
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "error" in err.lower()
        # The error message must list what's available, so the user can
        # correct without grep-ing the source tree.
        assert "ecommerce.sql" in err

    def test_path_traversal_returns_exit_2(self, capsys: pytest.CaptureFixture[str]) -> None:
        exit_code = main(["fixture-path", "../etc/passwd"])
        assert exit_code == 2
        assert "error" in capsys.readouterr().err.lower()

    def test_empty_name_returns_exit_2(self, capsys: pytest.CaptureFixture[str]) -> None:
        exit_code = main(["fixture-path", ""])
        assert exit_code == 2


# ---------------------------------------------------------------------------
# `--url-env` support across index/serve/eval. Credentials passed via
# argv leak to shell history, `ps`, and journald, so we require an env
# variable for any URL containing a non-empty password.
# ---------------------------------------------------------------------------


class TestResolveUrlSource:
    """Unit tests for the `_resolve_url_source` helper that backs --url-env."""

    def test_returns_env_value_when_url_env_is_set(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from schemabrain.cli import _resolve_url_source

        monkeypatch.setenv("MY_DB_URL", "postgresql+psycopg://u:p@h/db")
        assert (
            _resolve_url_source(positional=None, url_env="MY_DB_URL")
            == "postgresql+psycopg://u:p@h/db"
        )
        # Resolving from an env var must not warn — env is the safe path.
        assert capsys.readouterr().err == ""

    def test_returns_none_and_renders_error_when_env_var_unset(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from schemabrain.cli import _resolve_url_source

        monkeypatch.delenv("ABSENT_DB_URL", raising=False)
        assert _resolve_url_source(positional=None, url_env="ABSENT_DB_URL") is None
        err = capsys.readouterr().err
        assert "ABSENT_DB_URL" in err
        # Guided-error block — same shape as the rest of the CLI.
        assert "error:" in err
        assert "fix:" in err

    def test_returns_none_and_renders_error_when_env_var_empty(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from schemabrain.cli import _resolve_url_source

        monkeypatch.setenv("EMPTY_DB_URL", "")
        assert _resolve_url_source(positional=None, url_env="EMPTY_DB_URL") is None
        err = capsys.readouterr().err
        assert "EMPTY_DB_URL" in err
        assert "empty" in err.lower()

    def test_returns_none_when_both_positional_and_url_env_given(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from schemabrain.cli import _resolve_url_source

        monkeypatch.setenv("MY_DB_URL", "postgresql+psycopg://u:p@h/db")
        assert (
            _resolve_url_source(
                positional="postgresql+psycopg://other/db",
                url_env="MY_DB_URL",
            )
            is None
        )
        err = capsys.readouterr().err
        assert "error:" in err
        # The error must spell out which two inputs are conflicting.
        assert "--url-env" in err

    def test_returns_none_when_neither_positional_nor_url_env_given(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from schemabrain.cli import _resolve_url_source

        assert _resolve_url_source(positional=None, url_env=None) is None
        err = capsys.readouterr().err
        assert "error:" in err

    def test_positional_without_password_emits_no_warning(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from schemabrain.cli import _resolve_url_source

        url = "postgresql+psycopg://host/db"
        assert _resolve_url_source(positional=url, url_env=None) == url
        # No credentials → no leak risk → no warning.
        assert capsys.readouterr().err == ""

    def test_positional_with_password_emits_deprecation_warning(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from schemabrain.cli import _resolve_url_source

        url = "postgresql+psycopg://alice:s3cret@host:5432/db"
        # Returns the URL successfully (so the user isn't blocked) but
        # surfaces a warning so they know to switch.
        assert _resolve_url_source(positional=url, url_env=None) == url
        err = capsys.readouterr().err
        assert "--url-env" in err
        # The warning itself must not echo the password back at the user.
        # (Echoing it would be the same leak risk we're warning about.)
        assert "s3cret" not in err

    def test_malformed_positional_url_falls_through_without_warning(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `urlparse` raises ValueError on some malformed inputs (e.g.
        # unclosed IPv6 bracket). We must not crash trying to detect
        # a password — defer to the downstream `_resolve_url` to render
        # the real diagnostic. No warning here, because we couldn't even
        # determine whether credentials were present.
        from schemabrain.cli import _resolve_url_source

        malformed = "postgresql://[::1"  # urlparse: Invalid IPv6 URL
        assert _resolve_url_source(positional=malformed, url_env=None) == malformed
        assert capsys.readouterr().err == ""

    def test_positional_with_empty_password_emits_no_warning(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `urlparse('postgresql+psycopg://appuser:@host/db').password`
        # returns "" (empty string), not None. That form is legitimately
        # used with .pgpass or peer authentication — no password is
        # actually being passed in argv, so warning the user would be a
        # false positive. The helper must distinguish "no password" from
        # "empty password" via truthiness, not identity.
        from schemabrain.cli import _resolve_url_source

        url = "postgresql+psycopg://appuser:@host/db"
        assert _resolve_url_source(positional=url, url_env=None) == url
        assert capsys.readouterr().err == ""

    def test_env_var_with_credentials_is_silent(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from schemabrain.cli import _resolve_url_source

        # Even a credentialed URL via env is the SAFE path — no warning.
        monkeypatch.setenv("SAFE_DB_URL", "postgresql+psycopg://alice:s3cret@host/db")
        result = _resolve_url_source(positional=None, url_env="SAFE_DB_URL")
        assert result == "postgresql+psycopg://alice:s3cret@host/db"
        assert capsys.readouterr().err == ""


class TestIndexUrlEnv:
    """`schemabrain index --url-env VARNAME` wiring."""

    def test_url_env_resolves_url_from_environment(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Stub Postgres + profiler so we exercise arg wiring without a
        # real database. --no-enrich skips the ANTHROPIC_API_KEY check.
        class _EmptySource:
            def __init__(self, url: str) -> None:
                # Verify the URL flowed through from the env var, not
                # some hardcoded default or argparse fallback.
                assert url == "postgresql+psycopg://envhost/db"

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def list_tables(self, schema: str | None = None):
                return []

            def get_table(self, name: str, schema: str):
                raise NotImplementedError

            def close(self) -> None:
                pass

        class _StubProfiler:
            def __init__(self, url: str) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def profile_table(self, table):
                return {}

            def close(self) -> None:
                pass

        monkeypatch.setattr("schemabrain.cli.PostgresDataSource", _EmptySource)
        monkeypatch.setattr("schemabrain.cli.PostgresProfiler", _StubProfiler)
        monkeypatch.setenv("DBURL_X", "postgresql+psycopg://envhost/db")
        store_path = tmp_path / "schemabrain.db"
        exit_code = main(
            [
                "index",
                "--url-env",
                "DBURL_X",
                "--store-path",
                str(store_path),
                "--no-enrich",
            ]
        )
        assert exit_code == 0

    def test_url_env_pointing_at_unset_var_returns_exit_2(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.delenv("UNSET_DB_URL", raising=False)
        store_path = tmp_path / "schemabrain.db"
        exit_code = main(["index", "--url-env", "UNSET_DB_URL", "--store-path", str(store_path)])
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "UNSET_DB_URL" in err
        assert "fix:" in err

    def test_index_with_neither_positional_nor_url_env_returns_exit_2(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        store_path = tmp_path / "schemabrain.db"
        # argparse no longer makes positional required; the helper
        # enforces the "one-of" rule and emits a guided error.
        exit_code = main(["index", "--store-path", str(store_path)])
        assert exit_code == 2
        assert "error:" in capsys.readouterr().err

    def test_positional_with_credentials_still_works_but_warns(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Backwards-compat path: existing scripts passing the URL
        # positionally must keep working, but the user gets nudged
        # toward --url-env.
        class _EmptySource:
            def __init__(self, url: str) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def list_tables(self, schema: str | None = None):
                return []

            def get_table(self, name: str, schema: str):
                raise NotImplementedError

            def close(self) -> None:
                pass

        class _StubProfiler:
            def __init__(self, url: str) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def profile_table(self, table):
                return {}

            def close(self) -> None:
                pass

        monkeypatch.setattr("schemabrain.cli.PostgresDataSource", _EmptySource)
        monkeypatch.setattr("schemabrain.cli.PostgresProfiler", _StubProfiler)
        store_path = tmp_path / "schemabrain.db"
        exit_code = main(
            [
                "index",
                "postgresql+psycopg://alice:s3cret@host/db",
                "--store-path",
                str(store_path),
                "--no-enrich",
            ]
        )
        assert exit_code == 0
        err = capsys.readouterr().err
        assert "--url-env" in err
        # Warning must not echo the password verbatim.
        assert "s3cret" not in err


class TestServeUrlEnv:
    """`schemabrain serve --url-env VARNAME` wiring.

    `serve` opens an MCP stdio loop — we don't drive that here. The
    target is the arg-parsing layer: --url-env must be accepted, the
    legacy --source must still work, and missing-both must fail
    cleanly without ever opening the SQLite store.
    """

    def test_url_env_unset_returns_exit_2(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.delenv("SERVE_URL", raising=False)
        store_path = tmp_path / "schemabrain.db"
        exit_code = main(["serve", "--url-env", "SERVE_URL", "--store-path", str(store_path)])
        assert exit_code == 2
        assert "SERVE_URL" in capsys.readouterr().err

    def test_serve_with_neither_source_nor_url_env_returns_exit_2(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        store_path = tmp_path / "schemabrain.db"
        exit_code = main(["serve", "--store-path", str(store_path)])
        assert exit_code == 2
        assert "error:" in capsys.readouterr().err


class TestEvalUrlEnv:
    """`schemabrain eval --url-env VARNAME` wiring."""

    def test_url_env_unset_returns_exit_2(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.delenv("EVAL_URL", raising=False)
        store_path = tmp_path / "schemabrain.db"
        exit_code = main(["eval", "--url-env", "EVAL_URL", "--store-path", str(store_path)])
        assert exit_code == 2
        assert "EVAL_URL" in capsys.readouterr().err

    def test_eval_with_neither_source_nor_url_env_returns_exit_2(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        store_path = tmp_path / "schemabrain.db"
        exit_code = main(["eval", "--store-path", str(store_path)])
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "error:" in err
