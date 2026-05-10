"""Tests for the schemabrain CLI."""

from pathlib import Path

import pytest

from schemabrain.cli import _canonical_url, _make_source_id, main
from schemabrain.core.store import SQLiteStore


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

    def test_index_requires_url(self):
        with pytest.raises(SystemExit) as exc:
            main(["index"])
        assert exc.value.code != 0


class TestIndexCommandValidation:
    def test_malformed_url_returns_nonzero_exit_code(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        store_path = tmp_path / "schemabrain.db"
        exit_code = main(["index", "not-a-url", "--store-path", str(store_path)])
        assert exit_code == 2
        assert "error" in capsys.readouterr().err.lower()

    def test_unsupported_scheme_returns_nonzero_exit_code(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        store_path = tmp_path / "schemabrain.db"
        exit_code = main(["index", "mysql://db/test", "--store-path", str(store_path)])
        assert exit_code == 2
        assert "Unsupported scheme" in capsys.readouterr().err

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

        assert isinstance(_EmptySource("postgresql://fake/db"), DataSource)
        assert isinstance(_StubProfiler("postgresql://fake/db"), Profiler)

        monkeypatch.setattr("schemabrain.cli.PostgresDataSource", _EmptySource)
        monkeypatch.setattr("schemabrain.cli.PostgresProfiler", _StubProfiler)
        store_path = tmp_path / "schemabrain.db"
        # --no-enrich so we don't need ANTHROPIC_API_KEY for this test.
        exit_code = main(
            [
                "index",
                "postgresql://fake/db",
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
        exit_code = main(["index", "postgresql://fake/db", "--store-path", str(store_path)])
        assert exit_code == 2
        assert "ANTHROPIC_API_KEY" in capsys.readouterr().err

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
                "postgresql://fake/db",
                "--store-path",
                str(store_path),
                "--no-enrich",
            ]
        )
        assert exit_code == 0

    def test_max_cost_default_is_ten(self) -> None:
        # Default --max-cost should match the EnrichmentPipeline default.
        from schemabrain.cli import _DEFAULT_MAX_COST_USD

        assert _DEFAULT_MAX_COST_USD == 10.0

    def test_with_api_key_constructs_pipeline_and_runs(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ):
        # With ANTHROPIC_API_KEY set, the CLI builds the pipeline. We
        # don't want to make a real call, so stub out the source/profiler
        # to return empty (no tables → no LLM call) AND stub the
        # AnthropicHaikuClient to avoid the real SDK construction.
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
        exit_code = main(["index", "postgresql://fake/db", "--store-path", str(store_path)])
        assert exit_code == 0

    def test_cost_cap_exceeded_returns_exit_3(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ):
        # Stub the source to return one table, the profiler to return
        # empty stats, and the AnthropicHaikuClient with a fake that
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

        monkeypatch.setattr("schemabrain.cli.PostgresDataSource", _OneTableSource)
        monkeypatch.setattr("schemabrain.cli.PostgresProfiler", _StubProfiler)
        monkeypatch.setattr("schemabrain.cli.AnthropicHaikuClient", _ExpensiveClient)

        store_path = tmp_path / "schemabrain.db"
        exit_code = main(
            [
                "index",
                "postgresql://fake/db",
                "--store-path",
                str(store_path),
                "--max-cost",
                "0.01",
            ]
        )
        assert exit_code == 3
        assert "cap" in capsys.readouterr().err.lower()


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


class TestEvalSubcommandValidation:
    """The `eval` subcommand's argparse + early validation paths."""

    def test_eval_requires_source(self):
        with pytest.raises(SystemExit) as exc:
            main(["eval"])
        assert exc.value.code != 0

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
                "postgresql://fake/db",
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
                "postgresql://fake/db",
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

        source_url = "postgresql://fake-host/eval-test"
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
                "postgresql://fake-host/empty",
                "--store-path",
                str(store_path),
                "--golden",
                str(golden_path),
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
                "postgresql://fake-host/default-golden-test",
                "--store-path",
                str(store_path),
            ]
        )
        assert exit_code == 0
        out = capsys.readouterr().out
        # Bundled set has 10 questions per the file in this slice.
        assert "10 questions" in out
