"""FZ-GM-007 — get_metric `limit=0` bypasses the ToolResponse envelope.

Fuzz-discovered finding on the MCP tool surface.

## Summary

`get_metric` declares `limit: Annotated[int, Field(ge=1, le=10_000)] = 1000`.
When an agent (or adversarial caller) passes `limit=0`, FastMCP's
auto-generated Pydantic argument model raises *before* the tool body
runs. The exception surfaces to the MCP client as a raw
`FastMCPToolError` (`isError: true`) carrying the Pydantic
`greater_than_equal` validation message — **not** a Charter-compliant
`ToolResponse` envelope with `status="error"` + a typed `Recovery`.

This is an asymmetric break of Charter Principle 1 ("loud, not silent
— but typed"): sibling tools handle equivalent boundary-violation cases
by returning a structured envelope:

* `find_relevant_tables(query=..., limit=0)`  →  `status="empty"` (graceful)
* `find_relevant_tables(query=..., limit=-1)` →  `status="empty"` (graceful)
* `suggest_joins(..., max_hops=0)`            →  `status="error"`, `kind="malformed_name"`
* `get_metric(..., limit=0)`                  →  RAISES `FastMCPToolError`

An agent calling `get_metric` cannot follow a `Recovery.suggested_tool`
hint on this path; it must parse the raw Pydantic error text to recover.

## Severity

`high` (not critical) — no PII leak, no firewall bypass of authorization
gates. But the inconsistency breaks the envelope-uniformity contract
the Charter pins in v1.2, and an agent harness that switches on
`isError` will treat this as a transport-level failure rather than a
business-logic refusal.

## Repro

See test below — single-line in-process call against an indexed store.

## Recommended fix

Two options:

1. Drop the Pydantic `Field(ge=1, le=10_000)` constraint from the
   `limit` parameter and validate inside the tool body, returning a
   structured `malformed_name` ToolResponse via the existing
   `_malformed_name_response` helper.
2. Keep the constraint but install a FastMCP-level argument-validation
   catch that converts the Pydantic ValidationError into the same
   structured envelope (similar to the existing `_StrictArgsFastMCP`
   strict-args path).

Option 1 matches what `suggest_joins(max_hops <= 0)` already does.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from mcp.server.fastmcp.exceptions import ToolError as FastMCPToolError
from sqlalchemy import create_engine

from schemabrain.core.store import SQLiteStore
from schemabrain.enrichment.embeddings import FastEmbedEmbedder
from schemabrain.mcp import build_server
from schemabrain.mcp.metric_executor import EngineMetricExecutor

PG_URL = "postgresql+psycopg://postgres:local@127.0.0.1:5433/postgres"
SOURCE_ID = "postgresql+psycopg://127.0.0.1:5433/postgres"
STORE_PATH = Path("./.sb-fuzz-store.db")


def _build_app():
    store = SQLiteStore(STORE_PATH)
    executor = EngineMetricExecutor(create_engine(PG_URL))
    app = build_server(
        store=store,
        source_connection_id=SOURCE_ID,
        embedder=FastEmbedEmbedder(),
        metric_executor=executor,
        pii_block=frozenset({"credential", "payment_card", "government_id"}),
    )
    return app, store


@pytest.mark.integration
@pytest.mark.firewall_bypass
class TestFZGM007GetMetricLimitZeroBypassesEnvelope:
    """`get_metric` raises FastMCPToolError on `limit=0` / `limit>10000`
    instead of returning the typed ToolResponse envelope.
    """

    def test_limit_zero_raises_instead_of_envelope(self) -> None:
        app, store = _build_app()
        try:
            with pytest.raises(FastMCPToolError) as excinfo:
                asyncio.run(app.call_tool("get_metric", {"name": "any_metric", "limit": 0}))
            # The raised error carries Pydantic's raw message rather
            # than a Charter ErrorKind + Recovery. This is the bug.
            assert "greater_than_equal" in str(excinfo.value)
            assert "limit" in str(excinfo.value)
        finally:
            store.close()

    def test_limit_above_cap_raises_instead_of_envelope(self) -> None:
        app, store = _build_app()
        try:
            with pytest.raises(FastMCPToolError) as excinfo:
                asyncio.run(app.call_tool("get_metric", {"name": "any_metric", "limit": 100_000}))
            assert "less_than_equal" in str(excinfo.value)
            assert "limit" in str(excinfo.value)
        finally:
            store.close()

    def test_sibling_tools_handle_boundary_gracefully(self) -> None:
        """Asymmetry contract: the sibling boundary-violation cases
        return structured envelopes. Pins the regression so the fix
        for `get_metric` produces the same shape these tools already
        emit.
        """
        app, store = _build_app()
        try:
            # find_relevant_tables with limit=0 → empty envelope, no raise
            _content, frt_zero = asyncio.run(
                app.call_tool("find_relevant_tables", {"query": "x", "limit": 0})
            )
            assert frt_zero["status"] == "empty"
            assert frt_zero["error"] is None

            # find_relevant_tables with limit=-1 → empty envelope, no raise
            _content, frt_neg = asyncio.run(
                app.call_tool("find_relevant_tables", {"query": "x", "limit": -1})
            )
            assert frt_neg["status"] == "empty"
            assert frt_neg["error"] is None

            # suggest_joins with max_hops=0 → typed malformed_name envelope
            _content, sj_zero = asyncio.run(
                app.call_tool(
                    "suggest_joins",
                    {"tables": ["public.users", "public.orgs"], "max_hops": 0},
                )
            )
            assert sj_zero["status"] == "error"
            assert sj_zero["error"]["kind"] == "malformed_name"
            assert sj_zero["error"]["recovery"]["suggested_tool"] == "find_relevant_tables"
        finally:
            store.close()
