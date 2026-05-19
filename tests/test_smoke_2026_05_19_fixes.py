"""Regression tests for the polish bundle from the 2026-05-19 manual smoke.

Each test corresponds to one finding from `docs/internal/manual_smoke_2026_05_19.md`:

- B2: `inspect --source <VARNAME>` AND `check --source <VARNAME>` must emit a
  guided `url_invalid` error block, NOT crash with an unhandled `ValueError`
  traceback.
- S1: `find_relevant_tables` empty result must carry `follow_up_hints` so the
  agent has a fallback chain instead of a silent dead-end.
- S2: `index` must accept `--source URL` for surface parity with check / inspect
  / init / serve. Passing BOTH `--source` AND the positional form errors out.
- S3+S4: MCP tools must REJECT unknown kwargs (extra="forbid" semantics) AND
  emit one rejection audit row + one bus event so the rejection is visible in
  `audit list` and events.jsonl.
- S5: bundled ecommerce fixture must seed transactional data (orders /
  order_items / product_categories) so the marquee `total_revenue` metric
  returns a non-null number.
- N1: doctor's `host_config_store_path` warning must explain WHY the mismatch
  matters (MCP host reads the snippet's store, not the cwd's).
- N2: `url_source_missing` error must hint at `--url-env DATABASE_URL` when
  the user's environment already has a URL-shaped DATABASE_URL.

Grouped here for traceability against the smoke report; once the PR merges,
the tests are still useful as regression checks even though their grouping
is no longer load-bearing.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Generator
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError as FastMCPToolError

from schemabrain.audit.writer import AuditWriter
from schemabrain.cli import main
from schemabrain.core.embedding import ColumnEmbedding
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.mcp import build_server
from schemabrain.observability import Event, EventBus

# ---------------------------------------------------------------------------
# B2 — unhandled ValueError on `--source <VARNAME>`
# ---------------------------------------------------------------------------


class TestB2_BareVarnameAsSourceReturnsGuidedError:
    """`--source` expects a URL. A new user can easily pass a bare env-var
    name (because `--url-env <VARNAME>` exists alongside). Pre-fix that
    crashed `_canonical_url` with an unhandled ValueError; the guided
    error path now intercepts and returns exit code 2 + a `url_invalid`
    block. Both `check` and `inspect` are affected.
    """

    def test_check_with_bare_varname_as_source_does_not_crash(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        store_path = tmp_path / "store.db"
        store_path.touch()
        exit_code = main(
            [
                "check",
                "--source",
                "DATABASE_URL",  # bare varname — used to crash
                "--store-path",
                str(store_path),
            ]
        )
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "Traceback" not in err, f"unhandled traceback leaked: {err}"
        # The guided error renderer prints either the canonical
        # "Invalid connection URL" message or a why/fix block.
        assert "URL" in err or "scheme" in err.lower()

    def test_inspect_with_bare_varname_as_source_does_not_crash(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        store_path = tmp_path / "store.db"
        store_path.touch()
        exit_code = main(
            [
                "inspect",
                "--source",
                "DATABASE_URL",
                "--store-path",
                str(store_path),
            ]
        )
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "Traceback" not in err, f"unhandled traceback leaked: {err}"


# ---------------------------------------------------------------------------
# S1 — find_relevant_tables empty result must carry follow_up_hints
# ---------------------------------------------------------------------------


def _column(name: str, *, ordinal_position: int = 1) -> Column:
    return Column(
        name=name,
        table_name="users",
        schema_name="public",
        data_type="TEXT",
        nullable=False,
        ordinal_position=ordinal_position,
    )


class _StubEmbedder:
    model_name = "test-emb"
    dimension = 4

    def embed(self, text: str) -> tuple[float, ...]:
        del text
        return (1.0, 0.0, 0.0, 0.0)


class _ZeroEmbedder:
    model_name = "test-emb"
    dimension = 4

    def embed(self, text: str) -> tuple[float, ...]:
        del text
        return (0.0, 0.0, 0.0, 1.0)  # orthogonal to the stored vector


@pytest.fixture
def server_with_one_table_zero_embedder(
    tmp_path: Path,
) -> Generator[FastMCP, None, None]:
    store = SQLiteStore(tmp_path / "store.db")
    sid = "src1"
    store.write_table(
        Table(
            name="users",
            schema_name="public",
            columns=(_column("email", ordinal_position=1),),
        ),
        source_connection_id=sid,
    )
    store.write_table_embeddings(
        "public",
        "users",
        source_connection_id=sid,
        embeddings={
            "email": ColumnEmbedding(
                vector=(1.0, 0.0, 0.0, 0.0),
                model="test-emb",
                dimension=4,
            )
        },
    )
    app = build_server(store=store, source_connection_id=sid, embedder=_ZeroEmbedder())
    yield app
    store.close()


class TestS1_FindRelevantTablesEmptyHasFollowUpHint:
    """Pre-fix the empty case carried `follow_up_hints=None`. New users
    running the cost-free wizard (no `--enrich`) saw their agent get
    nothing back from `find_relevant_tables` with no actionable next
    step — a silent dead-end. Now the hint always points to
    `describe_table` as a fallback that doesn't depend on embeddings.
    """

    def test_empty_result_carries_describe_table_hint(
        self, server_with_one_table_zero_embedder: FastMCP
    ) -> None:
        _content, structured = asyncio.run(
            server_with_one_table_zero_embedder.call_tool(
                "find_relevant_tables", {"query": "anything", "limit": 5}
            )
        )
        assert structured["status"] == "empty"
        assert structured["data"] == []
        hints = structured["follow_up_hints"]
        assert hints is not None, "empty result must surface follow-up tools"
        assert "describe_table" in hints


# ---------------------------------------------------------------------------
# S2 — `index --source URL` accepts the flag for surface parity
# ---------------------------------------------------------------------------


class TestS2_IndexAcceptsSourceFlag:
    """Pre-fix, `index` was the only command without `--source URL` (it
    only accepted the positional form + `--url-env`). New users learning
    `--source` from check/inspect/init/serve got `unrecognized arguments`
    when they tried it on `index`. Now both the positional and flag forms
    work; passing BOTH errors out.
    """

    def test_index_recognizes_source_flag(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # `--source` followed by a malformed URL should reach the URL
        # validation step (not the argparse-unrecognized-argument path).
        # Exit code 2 + a guided url_invalid error is the expected shape.
        exit_code = main(
            [
                "index",
                "--source",
                "not-a-url",
                "--store-path",
                str(tmp_path / "store.db"),
                "--no-enrich",
                "--no-embed",
            ]
        )
        assert exit_code == 2
        err = capsys.readouterr().err
        # The key proof: argparse did NOT reject --source as unrecognized.
        assert "unrecognized arguments: --source" not in err

    def test_index_rejects_both_source_and_positional(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        exit_code = main(
            [
                "index",
                "postgresql+psycopg://positional/db",
                "--source",
                "postgresql+psycopg://flag/db",
                "--store-path",
                str(tmp_path / "store.db"),
            ]
        )
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "--source" in err
        assert "positional" in err.lower() or "both" in err.lower()


# ---------------------------------------------------------------------------
# S3 + S4 — MCP tools must reject unknown kwargs + emit rejection event
# ---------------------------------------------------------------------------


class _CapturingBus(EventBus):
    """Test double that captures every emitted event so assertions can
    inspect what the bus would have persisted to events.jsonl."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)


@pytest.fixture
def server_with_audit(
    tmp_path: Path,
) -> Generator[tuple[FastMCP, AuditWriter, _CapturingBus, Path], None, None]:
    """Build a server with a real audit writer + capturing bus so S4's
    rejection-event path can be verified end-to-end."""
    store = SQLiteStore(tmp_path / "store.db")
    sid = "src1"
    store.write_table(
        Table(
            name="users",
            schema_name="public",
            columns=(_column("email", ordinal_position=1),),
        ),
        source_connection_id=sid,
    )
    audit_db = tmp_path / "audit.db"
    audit = AuditWriter(audit_db)
    bus = _CapturingBus()
    app = build_server(
        store=store,
        source_connection_id=sid,
        embedder=_StubEmbedder(),
        audit_writer=audit,
        event_bus=bus,
    )
    yield app, audit, bus, audit_db
    store.close()


class TestS3S4_StrictArgsRejectionPath:
    """Pre-fix, FastMCP's auto-generated Pydantic models defaulted to
    `extra="ignore"`. An agent passing `get_metric name=... grain=month`
    (real arg is `time_grain`) silently got an un-grouped query — a
    structurally-valid wrong answer. The rejection also bypassed the
    audit table + events JSONL, so a sysadmin tailing either would not
    see broken-client traffic.

    The fix: `_StrictArgsFastMCP.call_tool` checks declared parameter
    names against the incoming arguments dict. Extras raise
    `FastMCPToolError` (so the client sees `isError: true`) AND fire
    a rejection hook that writes one audit row + one bus event.
    """

    def test_extra_kwarg_raises_tool_error(
        self,
        server_with_audit: tuple[FastMCP, AuditWriter, _CapturingBus, Path],
    ) -> None:
        app, _audit, _bus, _audit_db = server_with_audit
        with pytest.raises(FastMCPToolError) as excinfo:
            asyncio.run(
                app.call_tool(
                    "describe_table",
                    # `qualified_name` is real; `unexpected_kwarg` is not.
                    {
                        "qualified_name": "public.users",
                        "unexpected_kwarg": "boom",
                    },
                )
            )
        msg = str(excinfo.value)
        assert "unexpected_kwarg" in msg
        assert "describe_table" in msg

    def test_extra_kwarg_emits_one_bus_event(
        self,
        server_with_audit: tuple[FastMCP, AuditWriter, _CapturingBus, Path],
    ) -> None:
        app, _audit, bus, _audit_db = server_with_audit
        with pytest.raises(FastMCPToolError):
            asyncio.run(
                app.call_tool(
                    "describe_table",
                    {
                        "qualified_name": "public.users",
                        "garbage": "x",
                    },
                )
            )
        # Exactly one event for the rejection; status=error and
        # error_kind=invalid_argument so a tail viewer can spot it.
        rejection_events = [
            e for e in bus.events if e.tool_name == "describe_table" and e.status == "error"
        ]
        assert len(rejection_events) == 1
        assert rejection_events[0].error_kind == "invalid_argument"

    def test_extra_kwarg_writes_one_audit_row(
        self,
        server_with_audit: tuple[FastMCP, AuditWriter, _CapturingBus, Path],
        tmp_path: Path,
    ) -> None:
        app, _audit, _bus, audit_db = server_with_audit
        with pytest.raises(FastMCPToolError):
            asyncio.run(
                app.call_tool(
                    "describe_table",
                    {
                        "qualified_name": "public.users",
                        "garbage": "x",
                    },
                )
            )
        # The audit writer persisted one row for the rejected call;
        # status=error and tool=describe_table mark it for ops.
        conn = sqlite3.connect(audit_db)
        rows = list(
            conn.execute("SELECT tool_name, status FROM mcp_audit WHERE tool_name='describe_table'")
        )
        conn.close()
        assert len(rows) == 1
        assert rows[0] == ("describe_table", "error")

    def test_known_args_pass_through_normally(
        self,
        server_with_audit: tuple[FastMCP, AuditWriter, _CapturingBus, Path],
    ) -> None:
        """The strict-args layer must not break successful calls."""
        app, _audit, _bus, _audit_db = server_with_audit
        _content, structured = asyncio.run(
            app.call_tool(
                "describe_table",
                {"qualified_name": "public.users"},
            )
        )
        assert structured["status"] == "success"
        assert structured["data"]["name"] == "users"


# ---------------------------------------------------------------------------
# S5 — bundled ecommerce fixture has transactional rows
# ---------------------------------------------------------------------------


class TestS5_EcommerceFixtureHasTransactionalData:
    """Pre-fix, the bundled `schemabrain/eval/fixtures/ecommerce.sql`
    seeded users / addresses / products / categories but had ZERO rows
    in orders / order_items / product_categories. A new user running
    `examples/ecommerce/` Step 6 (the validated-SQL payoff demo) saw
    `total_revenue: null` and assumed the product was broken. The fix
    adds 3 orders + 4 line items + 2 product_categories so the marquee
    metrics return non-null numbers.
    """

    def test_fixture_has_at_least_three_orders(self) -> None:
        sql_path = (
            Path(__file__).parent.parent / "schemabrain" / "eval" / "fixtures" / "ecommerce.sql"
        )
        text = sql_path.read_text()
        # Search for at least one INSERT into orders that names columns.
        assert "INSERT INTO public.orders" in text, (
            "ecommerce.sql must seed orders rows for the payoff demo"
        )
        assert "INSERT INTO public.order_items" in text, (
            "ecommerce.sql must seed order_items rows for the payoff demo"
        )
        assert "INSERT INTO public.product_categories" in text, (
            "ecommerce.sql must seed product_categories junction rows"
        )

    def test_fixture_orders_total_sums_to_documented_value(self) -> None:
        """The fixture's revenue total appears in user-facing docs
        (`examples/ecommerce/` Step 6, smoke reports). A drift between
        the doc and the SQL would silently mislead readers. Pin the
        documented total here so any change forces a doc update too."""
        sql_path = (
            Path(__file__).parent.parent / "schemabrain" / "eval" / "fixtures" / "ecommerce.sql"
        )
        text = sql_path.read_text()
        # The seeded `orders.total_cents` values must sum to 86993 cents
        # ($869.93). Three orders: 32997 + 8999 + 44997 = 86993.
        for cents in ("32997", "8999", "44997"):
            assert cents in text, (
                f"expected total_cents={cents} in fixture; if you changed "
                "the seed data, update the documented total in "
                "docs/internal/manual_smoke_2026_05_19.md and "
                "examples/ecommerce/README.md"
            )


# ---------------------------------------------------------------------------
# N1 — doctor host_config_store_path warning explains WHY the mismatch matters
# ---------------------------------------------------------------------------


class TestN1_DoctorStorePathWarningExplainsMismatch:
    """Pre-fix, doctor's host_config_store_path warning told the user
    the paths differed but didn't explain why that matters — Claude
    Desktop reads from the SNIPPET's store, not the cwd's. New users
    saw the warning, didn't understand the impact, and ignored it.
    """

    def test_warning_message_mentions_host_reads_snippet_store(self, tmp_path: Path) -> None:
        from schemabrain.setup.doctor_flow import check_host_config_store_path_matches

        # Hand-craft a host config whose snippet store-path differs from
        # the workspace store we're asking doctor to verify.
        config_path = tmp_path / "host_config.json"
        config_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "schemabrain": {
                            "command": "uvx",
                            "args": [
                                "schemabrain",
                                "serve",
                                "--store-path",
                                "/some/other/place/store.db",
                            ],
                        }
                    }
                }
            )
        )
        result = check_host_config_store_path_matches(
            config_path,
            expected=tmp_path / "workspace_store.db",
        )
        assert result.outcome == "warn"
        # The new copy must call out the host-vs-workspace store
        # distinction explicitly — that's the whole point of the fix.
        msg_text = result.message + " " + (result.suggested_next or "")
        assert "host" in msg_text.lower(), (
            f"expected warning to explain that the MCP host reads the "
            f"snippet store, not the workspace one. Got: {msg_text!r}"
        )


# ---------------------------------------------------------------------------
# N2 — `check` error hints at --url-env DATABASE_URL when $DATABASE_URL set
# ---------------------------------------------------------------------------


class TestN2_UrlSourceMissingHintsAtDatabaseUrlEnv:
    """Pre-fix, `schemabrain check` (no flags) emitted a generic 'no
    connection URL provided' error. If the user already had
    DATABASE_URL exported (the common case), the error didn't connect
    those dots — the user had to guess the right flag form. Now the
    error checks the env and surfaces the exact recipe.
    """

    def test_database_url_set_hints_url_env_flag(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x/y")
        store_path = tmp_path / "store.db"
        store_path.touch()
        exit_code = main(["check", "--store-path", str(store_path)])
        assert exit_code == 2
        err = capsys.readouterr().err
        # The new hint must name `DATABASE_URL` explicitly so the user
        # sees the exact recipe they want.
        assert "DATABASE_URL" in err
        assert "--url-env" in err

    def test_database_url_unset_falls_back_to_generic_hint(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        store_path = tmp_path / "store.db"
        store_path.touch()
        exit_code = main(["check", "--store-path", str(store_path)])
        assert exit_code == 2
        err = capsys.readouterr().err
        # The generic hint still tells the user about --url-env; it just
        # doesn't claim DATABASE_URL is set.
        assert "--url-env" in err

    def test_non_url_database_url_falls_back_to_generic_hint(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # If DATABASE_URL is set but doesn't look like a URL, the hint
        # must NOT pretend it's usable. Otherwise we mislead users with
        # gibberish in the env.
        monkeypatch.setenv("DATABASE_URL", "not_a_url_at_all")
        store_path = tmp_path / "store.db"
        store_path.touch()
        exit_code = main(["check", "--store-path", str(store_path)])
        assert exit_code == 2
        err = capsys.readouterr().err
        # Generic hint applies; no claim that DATABASE_URL is a valid recipe.
        assert "--url-env" in err
        assert "looks like a URL" not in err
