"""One-command demo of the v0.4 dashboard sidecar.

Run::

    uv run python scripts/dashboard_demo.py

Builds a throwaway SQLite store under ``/tmp``, seeds it with a
handful of representative entities + audit rows (one success row,
one refusal, one degraded row), boots the FastAPI sidecar on
``http://127.0.0.1:7878``, and opens the dashboard in your browser.

Ctrl+C stops the sidecar. The tmp store path is printed at boot so
you can ``sqlite3`` into it if you want to poke at the raw rows.

Nothing in your real ``schemabrain.db`` is touched.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

from schemabrain.audit.ddl import ensure_audit_schema
from schemabrain.audit.writer import AuditWriter, build_audit_row
from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.dashboard.cli import run_dashboard
from schemabrain.dashboard.sidecar import is_ui_available
from schemabrain.mcp.envelope import Recovery, ToolError, ToolResponse
from schemabrain.pii.categories import ColumnPiiTag

SOURCE_CONNECTION_ID = "demo-source"


def main() -> int:
    if not is_ui_available():
        print(
            "ERROR: the [ui] extra is not installed.\n"
            "  Install with: uv sync --extra dev --extra otel --extra ui",
            file=sys.stderr,
        )
        return 2

    store_path = Path(tempfile.gettempdir()) / "schemabrain-dashboard-demo.db"
    if store_path.exists():
        store_path.unlink()
    _seed_store(store_path)
    _seed_audit_chain(store_path)

    print()
    print("=" * 60)
    print("SchemaBrain dashboard demo")
    print("=" * 60)
    print(f"  store: {store_path}")
    print("  dashboard: http://127.0.0.1:7878/")
    print("  try the routes:")
    print("    curl -s http://127.0.0.1:7878/api/health | jq")
    print("    curl -s http://127.0.0.1:7878/api/meta | jq")
    print("    curl -s http://127.0.0.1:7878/api/entities | jq")
    print("    curl -s http://127.0.0.1:7878/api/audit/rows | jq")
    print("    curl -s http://127.0.0.1:7878/api/audit/refusals | jq")
    print("    curl -s http://127.0.0.1:7878/api/audit/verify | jq")
    print()
    print("  ctrl+C to stop")
    print("=" * 60)
    print()

    return run_dashboard(
        store_path=store_path,
        port=7878,
        open_browser=True,
        source_connection_id=SOURCE_CONNECTION_ID,
    )


def _seed_store(store_path: Path) -> None:
    """Seed the SQLite store with 2 entities + their columns + PII tags."""
    with SQLiteStore(store_path) as store:
        users_table = Table(
            schema_name="public",
            name="users",
            columns=(
                Column(
                    schema_name="public",
                    table_name="users",
                    name="id",
                    data_type="integer",
                    nullable=False,
                    ordinal_position=1,
                ),
                Column(
                    schema_name="public",
                    table_name="users",
                    name="email",
                    data_type="text",
                    nullable=False,
                    ordinal_position=2,
                ),
                Column(
                    schema_name="public",
                    table_name="users",
                    name="password_hash",
                    data_type="text",
                    nullable=False,
                    ordinal_position=3,
                ),
                Column(
                    schema_name="public",
                    table_name="users",
                    name="created_at",
                    data_type="timestamptz",
                    nullable=False,
                    ordinal_position=4,
                ),
            ),
        )
        store.write_table(users_table, source_connection_id=SOURCE_CONNECTION_ID)
        store.write_entity(
            Entity(
                name="user",
                description="A registered account.",
                binding=SingleTableBinding(qualified_table="public.users"),
                identity="id",
            ),
            source_connection_id=SOURCE_CONNECTION_ID,
        )
        store.write_column_pii_tags(
            source_connection_id=SOURCE_CONNECTION_ID,
            qualified_table="public.users",
            tags={
                "email": ColumnPiiTag(("pii", frozenset({"contact"}))),
                "password_hash": ColumnPiiTag(("pii", frozenset({"credential"}))),
            },
        )

        orders_table = Table(
            schema_name="public",
            name="orders",
            columns=(
                Column(
                    schema_name="public",
                    table_name="orders",
                    name="id",
                    data_type="integer",
                    nullable=False,
                    ordinal_position=1,
                ),
                Column(
                    schema_name="public",
                    table_name="orders",
                    name="user_id",
                    data_type="integer",
                    nullable=False,
                    ordinal_position=2,
                ),
                Column(
                    schema_name="public",
                    table_name="orders",
                    name="card_pan",
                    data_type="text",
                    nullable=True,
                    ordinal_position=3,
                ),
                Column(
                    schema_name="public",
                    table_name="orders",
                    name="amount_cents",
                    data_type="integer",
                    nullable=False,
                    ordinal_position=4,
                ),
            ),
        )
        store.write_table(orders_table, source_connection_id=SOURCE_CONNECTION_ID)
        store.write_entity(
            Entity(
                name="order",
                description="A purchase by a user.",
                binding=SingleTableBinding(qualified_table="public.orders"),
                identity="id",
            ),
            source_connection_id=SOURCE_CONNECTION_ID,
        )
        store.write_column_pii_tags(
            source_connection_id=SOURCE_CONNECTION_ID,
            qualified_table="public.orders",
            tags={
                "card_pan": ColumnPiiTag(("pii", frozenset({"payment_card"}))),
            },
        )

        # Seed canonical joins and metrics for the semantic dashboard preview
        from schemabrain.core.join import CanonicalJoin, JoinColumnPair
        from schemabrain.core.metric import Metric, MetricMeasure

        store.write_canonical_join(
            CanonicalJoin(
                name="user_orders",
                description="Links user accounts to their placed orders.",
                source_entity="user",
                target_entity="order",
                on=(JoinColumnPair(source_column="id", target_column="user_id"),),
                origin="manual",
                cardinality="one_to_many",
            ),
            source_connection_id=SOURCE_CONNECTION_ID,
        )

        store.write_metric(
            Metric(
                name="user_count",
                description="Total count of registered unique users.",
                entity="user",
                measure=MetricMeasure(agg="count", column="id"),
                time_dimension="user.created_at",
                time_grains=("day", "month", "year"),
                origin="manual",
            ),
            source_connection_id=SOURCE_CONNECTION_ID,
        )

        store.write_metric(
            Metric(
                name="total_revenue",
                description="Sum of order purchase values (stored in cents).",
                entity="order",
                measure=MetricMeasure(agg="sum", column="amount_cents"),
                time_dimension=None,
                time_grains=(),
                origin="manual",
            ),
            source_connection_id=SOURCE_CONNECTION_ID,
        )


def _seed_audit_chain(store_path: Path) -> None:
    """Append 3 representative audit rows so the chain has shape."""
    conn = sqlite3.connect(str(store_path))
    ensure_audit_schema(conn)
    conn.commit()
    conn.close()

    writer = AuditWriter(store_path)
    try:
        # 1) A successful tool call.
        writer.write(
            build_audit_row(
                tool_name="list_entities",
                source_connection_id=SOURCE_CONNECTION_ID,
                response=ToolResponse(status="success", data={"items": ["user", "order"]}),
            )
        )
        # 2) A refusal — would touch payment_card.
        writer.write(
            build_audit_row(
                tool_name="get_metric",
                source_connection_id=SOURCE_CONNECTION_ID,
                response=ToolResponse(
                    status="refused",
                    error=ToolError(
                        kind="pii_blocked",
                        message="Refused: would touch payment_card",
                        recovery=Recovery(),
                        pii_categories=("payment_card",),
                    ),
                ),
            )
        )
        # 3) A refusal — would touch credential.
        writer.write(
            build_audit_row(
                tool_name="describe_entity",
                source_connection_id=SOURCE_CONNECTION_ID,
                response=ToolResponse(
                    status="refused",
                    error=ToolError(
                        kind="pii_blocked",
                        message="Refused: would touch credential",
                        recovery=Recovery(),
                        pii_categories=("credential",),
                    ),
                ),
            )
        )
    finally:
        writer.close()


if __name__ == "__main__":
    raise SystemExit(main())
