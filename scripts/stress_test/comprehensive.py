"""Comprehensive stress test of Schema Brain's MCP surface.

50+ scenarios across the categories an agent actually uses:
  - Schema discovery (find / list / describe)
  - Metric catalog
  - Single-row aggregates
  - Top-N rankings (PR-6h.2's marquee feature)
  - Time-grained queries
  - Multi-hop joins (PR-6h.1 / PR-6h.1.1)
  - Disambiguation via= (PR-6h.1)
  - Filter scenarios (PR-6h.3 column validation)
  - PII propagation
  - Edge cases + error envelopes
  - Volume / fan-out / degradation precedence

Each scenario reports PASS / FAIL / UNEXPECTED with a salient excerpt
of the response. The harness is deterministic — runs against the
PR-6h.3 fixture with random.Random(42), so retries produce identical
output.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import sqlalchemy

from schemabrain.core.store import SQLiteStore
from schemabrain.enrichment.embeddings import fastembed_default
from schemabrain.mcp import build_server
from schemabrain.mcp.metric_executor import EngineMetricExecutor

STORE_PATH = Path("/tmp/stress-store.db")
SOURCE_ID = "87c80af69c3b0547"
DB_URL = "postgresql+psycopg://postgres:local@localhost:5433/postgres"


@dataclass
class Result:
    category: str
    name: str
    status: str  # PASS / FAIL / UNEXPECTED
    reason: str = ""
    payload: Any = None
    error: str | None = None


@dataclass
class Scenario:
    category: str
    name: str
    tool: str
    args: dict[str, Any]
    check: Callable[[dict[str, Any]], tuple[bool, str]]


# ---------------------------------------------------------------------------
# Helper predicates
# ---------------------------------------------------------------------------


def status_in(r: dict[str, Any], *kinds: str) -> bool:
    return r.get("status") in kinds


def _rows(r: dict[str, Any]) -> list:
    return ((r.get("data") or {}).get("rows") or []) if isinstance(r.get("data"), dict) else []


def _row_count(r: dict[str, Any]) -> int:
    return len(_rows(r))


def _list_data(r: dict[str, Any]) -> list:
    d = r.get("data")
    return d if isinstance(d, list) else []


def _err_kind(r: dict[str, Any]) -> str | None:
    err = r.get("error") or {}
    return err.get("kind")


def _check_error(r: dict[str, Any], expected: str) -> tuple[bool, str]:
    kind = _err_kind(r)
    if kind != expected:
        return False, f"error.kind={kind!r} expected {expected!r}"
    return True, f"error.kind={kind!r}"


def _check_status(r: dict[str, Any], expected: str) -> tuple[bool, str]:
    if r.get("status") != expected:
        return False, f"status={r.get('status')!r} expected {expected!r}"
    return True, f"status={expected!r}"


def _check_degradation(r: dict[str, Any], expected: str) -> tuple[bool, str]:
    if r.get("degradation_reason") != expected:
        return False, f"degradation_reason={r.get('degradation_reason')!r} expected {expected!r}"
    return True, f"degradation_reason={expected!r}"


def _check_has_in_rows(r: dict[str, Any], key: str, value: Any) -> tuple[bool, str]:
    for row in _rows(r):
        if str(row.get(key)) == str(value):
            return True, f"row matching {key}={value} found"
    return False, f"no row with {key}={value} (rows={_rows(r)[:3]})"


def _check_top_row(r: dict[str, Any], expected_key: str, expected_value: Any) -> tuple[bool, str]:
    rows = _rows(r)
    if not rows:
        return False, "no rows"
    if str(rows[0].get(expected_key)) == str(expected_value):
        return True, f"top row {expected_key}={expected_value}"
    return False, f"top row was {rows[0]}; expected {expected_key}={expected_value}"


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


def discovery_scenarios() -> list[Scenario]:
    return [
        Scenario(
            "DISCOVERY", "list_entities returns all 6",
            "list_entities", {},
            lambda r: (
                status_in(r, "success") and len(_list_data(r)) == 6,
                f"{len(_list_data(r))} entities",
            ),
        ),
        Scenario(
            "DISCOVERY", "list_metrics returns 10",
            "list_metrics", {},
            lambda r: (
                status_in(r, "success") and len(_list_data(r)) == 10,
                f"{len(_list_data(r))} metrics",
            ),
        ),
        Scenario(
            "DISCOVERY", "list_joins returns 5 incl. billing/shipping",
            "list_joins", {},
            lambda r: (
                len(_list_data(r)) == 5
                and any("billing" in (j.get("name") or "") for j in _list_data(r))
                and any("shipping" in (j.get("name") or "") for j in _list_data(r)),
                f"{[j.get('name') for j in _list_data(r)]}",
            ),
        ),
        Scenario(
            "DISCOVERY", "describe_entity user",
            "describe_entity", {"name": "user"},
            lambda r: (
                status_in(r, "success")
                and len((r.get("data") or {}).get("columns") or []) >= 3,
                f"{len((r.get('data') or {}).get('columns') or [])} columns",
            ),
        ),
        Scenario(
            "DISCOVERY", "describe_entity unknown → error envelope",
            "describe_entity", {"name": "subscription"},
            lambda r: _check_status(r, "error"),
        ),
        Scenario(
            "DISCOVERY", "describe_column public.users.email",
            "describe_column", {"qualified_name": "public.users.email"},
            lambda r: (
                status_in(r, "success") and (r.get("data") or {}).get("name") == "email",
                f"name={(r.get('data') or {}).get('name')}",
            ),
        ),
        Scenario(
            "DISCOVERY", "describe_column unknown → error envelope",
            "describe_column", {"qualified_name": "public.users.bogus"},
            lambda r: _check_status(r, "error"),
        ),
        Scenario(
            "DISCOVERY", "describe_table users",
            "describe_table", {"qualified_name": "public.users"},
            lambda r: (
                status_in(r, "success") and (r.get("data") or {}).get("name") == "users",
                f"table name={(r.get('data') or {}).get('name')}",
            ),
        ),
    ]


def aggregate_scenarios() -> list[Scenario]:
    return [
        Scenario(
            "AGGREGATE", "total_revenue > $1000 (in cents)",
            "get_metric", {"name": "total_revenue"},
            lambda r: (
                status_in(r, "success") and int((_rows(r)[0] or {}).get("total_revenue", 0)) > 100_000,
                f"value={(_rows(r)[0] or {}).get('total_revenue')}",
            ),
        ),
        Scenario(
            "AGGREGATE", "order_count == 177",
            "get_metric", {"name": "order_count"},
            lambda r: (
                (_rows(r)[0] or {}).get("order_count") == 177,
                f"value={(_rows(r)[0] or {}).get('order_count')}",
            ),
        ),
        Scenario(
            "AGGREGATE", "distinct_ordering_users == 50",
            "get_metric", {"name": "distinct_ordering_users"},
            lambda r: (
                (_rows(r)[0] or {}).get("distinct_ordering_users") == 50,
                f"value={(_rows(r)[0] or {}).get('distinct_ordering_users')}",
            ),
        ),
        Scenario(
            "AGGREGATE", "average_order_value returns positive number",
            "get_metric", {"name": "average_order_value"},
            lambda r: (
                status_in(r, "success") and float((_rows(r)[0] or {}).get("average_order_value", 0)) > 0,
                f"value={(_rows(r)[0] or {}).get('average_order_value')}",
            ),
        ),
        Scenario(
            "AGGREGATE", "product_count == 18",
            "get_metric", {"name": "product_count"},
            lambda r: (
                (_rows(r)[0] or {}).get("product_count") == 18,
                f"value={(_rows(r)[0] or {}).get('product_count')}",
            ),
        ),
        Scenario(
            "AGGREGATE", "category_count == 6",
            "get_metric", {"name": "category_count"},
            lambda r: (
                (_rows(r)[0] or {}).get("category_count") == 6,
                f"value={(_rows(r)[0] or {}).get('category_count')}",
            ),
        ),
        Scenario(
            "AGGREGATE", "registered_user_count == 80",
            "get_metric", {"name": "registered_user_count"},
            lambda r: (
                (_rows(r)[0] or {}).get("registered_user_count") == 80,
                f"value={(_rows(r)[0] or {}).get('registered_user_count')}",
            ),
        ),
    ]


def ranking_scenarios() -> list[Scenario]:
    return [
        Scenario(
            "RANKING", "top 5 customers by items — Alice (75) on top",
            "get_metric", {
                "name": "total_quantity_ordered",
                "group_by": ["user.email"],
                "order_by": [{"column": "total_quantity_ordered", "direction": "desc"}],
                "limit": 5,
            },
            lambda r: _check_top_row(r, "group_col_0", "alice@example.com"),
        ),
        Scenario(
            "RANKING", "top 10 by items returns 10 rows",
            "get_metric", {
                "name": "total_quantity_ordered",
                "group_by": ["user.email"],
                "order_by": [{"column": "total_quantity_ordered", "direction": "desc"}],
                "limit": 10,
            },
            lambda r: (_row_count(r) == 10, f"{_row_count(r)} rows"),
        ),
        Scenario(
            "RANKING", "bottom 3 customers by items (asc) — minimum buyers",
            "get_metric", {
                "name": "total_quantity_ordered",
                "group_by": ["user.email"],
                "order_by": [{"column": "total_quantity_ordered", "direction": "asc"}],
                "limit": 3,
            },
            lambda r: (
                status_in(r, "success", "degraded") and _row_count(r) == 3,
                f"rows={_row_count(r)}, top={_rows(r)[0] if _rows(r) else None}",
            ),
        ),
        Scenario(
            "RANKING", "top 5 by order count — Alice/Cara/etc.",
            "get_metric", {
                "name": "order_count",
                "group_by": ["user.email"],
                "order_by": [{"column": "order_count", "direction": "desc"}],
                "limit": 5,
            },
            lambda r: (
                _row_count(r) == 5 and any("alice" in str(row).lower() for row in _rows(r)),
                f"top={_rows(r)[0]}",
            ),
        ),
        Scenario(
            "RANKING", "top 5 with explicit tie-break on email",
            "get_metric", {
                "name": "total_quantity_ordered",
                "group_by": ["user.email"],
                "order_by": [
                    {"column": "total_quantity_ordered", "direction": "desc"},
                    {"column": "user.email", "direction": "asc"},
                ],
                "limit": 5,
            },
            lambda r: (_row_count(r) == 5, f"{_row_count(r)} rows"),
        ),
        Scenario(
            "RANKING", "order_by group_col only — sorted alphabetically",
            "get_metric", {
                "name": "total_quantity_ordered",
                "group_by": ["user.email"],
                "order_by": [{"column": "user.email", "direction": "asc"}],
                "limit": 3,
            },
            lambda r: (
                _row_count(r) == 3 and _rows(r)[0].get("group_col_0", "") <= _rows(r)[1].get("group_col_0", "zz"),
                f"first row email={_rows(r)[0].get('group_col_0') if _rows(r) else None}",
            ),
        ),
        Scenario(
            "RANKING", "no limit + order_by works",
            "get_metric", {
                "name": "total_quantity_ordered",
                "group_by": ["user.email"],
                "order_by": [{"column": "total_quantity_ordered", "direction": "desc"}],
            },
            lambda r: (
                status_in(r, "success", "degraded") and _row_count(r) == 50,
                f"got {_row_count(r)} rows (expected 50 distinct-with-orders)",
            ),
        ),
    ]


def join_scenarios() -> list[Scenario]:
    return [
        Scenario(
            "JOIN", "multi-hop order_item → order → user",
            "get_metric", {
                "name": "total_quantity_ordered",
                "group_by": ["user.email"],
                "order_by": [{"column": "total_quantity_ordered", "direction": "desc"}],
                "limit": 1,
            },
            lambda r: _check_top_row(r, "group_col_0", "alice@example.com"),
        ),
        Scenario(
            "JOIN", "single-hop order → user",
            "get_metric", {
                "name": "order_count",
                "group_by": ["user.email"],
                "order_by": [{"column": "order_count", "direction": "desc"}],
                "limit": 3,
            },
            lambda r: _check_top_row(r, "group_col_0", "alice@example.com"),
        ),
        Scenario(
            "JOIN", "billing disambiguation via=orders_billing_address_id",
            "get_metric", {
                "name": "order_count",
                "group_by": ["address.city"],
                "via": ["orders_billing_address_id"],
                "order_by": [{"column": "order_count", "direction": "desc"}],
                "limit": 5,
            },
            lambda r: (status_in(r, "success", "degraded") and _row_count(r) > 0, f"rows={_row_count(r)}"),
        ),
        Scenario(
            "JOIN", "shipping disambiguation via=orders_shipping_address_id",
            "get_metric", {
                "name": "order_count",
                "group_by": ["address.country"],
                "via": ["orders_shipping_address_id"],
                "order_by": [{"column": "order_count", "direction": "desc"}],
                "limit": 5,
            },
            lambda r: (status_in(r, "success", "degraded") and _row_count(r) > 0, f"rows={_row_count(r)}"),
        ),
        Scenario(
            "JOIN", "ambiguous join without via — error",
            "get_metric", {
                "name": "order_count",
                "group_by": ["address.country"],
            },
            lambda r: _check_error(r, "ambiguous_join"),
        ),
        Scenario(
            "JOIN", "via=unknown_join_name → unknown_via_join",
            "get_metric", {
                "name": "order_count",
                "group_by": ["address.country"],
                "via": ["orders_invalid_join"],
            },
            lambda r: _check_error(r, "unknown_via_join"),
        ),
        Scenario(
            "JOIN", "unreachable entity (no canonical join)",
            "get_metric", {
                "name": "order_count",
                "group_by": ["category.name"],
            },
            lambda r: _check_error(r, "unreachable_entity"),
        ),
    ]


def column_validation_scenarios() -> list[Scenario]:
    """PR-6h.3's compile-time column-existence check."""
    return [
        Scenario(
            "COLUMN_VAL", "group_by bogus column → unknown_group_by_column",
            "get_metric", {
                "name": "order_count",
                "group_by": ["user.bogus_column"],
            },
            lambda r: _check_error(r, "unknown_group_by_column"),
        ),
        Scenario(
            "COLUMN_VAL", "filter bogus column → unknown_filter_column",
            "get_metric", {
                "name": "order_count",
                "filters": [{"column": "user.bogus_column", "op": "eq", "value": "x"}],
            },
            lambda r: _check_error(r, "unknown_filter_column"),
        ),
        Scenario(
            "COLUMN_VAL", "group_by typo'd column on anchor entity",
            "get_metric", {
                "name": "order_count",
                "group_by": ["order.placd_at"],  # typo: placd vs placed
            },
            lambda r: _check_error(r, "unknown_group_by_column"),
        ),
        Scenario(
            "COLUMN_VAL", "group_by typo on joined entity",
            "get_metric", {
                "name": "order_count",
                "group_by": ["user.emial"],  # typo: emial vs email
            },
            lambda r: _check_error(r, "unknown_group_by_column"),
        ),
        Scenario(
            "COLUMN_VAL", "filter on known column resolves fine",
            "get_metric", {
                "name": "order_count",
                "filters": [{"column": "order.status", "op": "eq", "value": "fulfilled"}],
            },
            lambda r: (status_in(r, "success", "degraded"), f"status={r.get('status')}"),
        ),
        Scenario(
            "COLUMN_VAL", "unknown_group_by_column carries allowed_columns",
            "get_metric", {
                "name": "order_count",
                "group_by": ["user.bogus_xyz"],
            },
            lambda r: (
                _err_kind(r) == "unknown_group_by_column"
                and "email" in (((r.get("error") or {}).get("recovery") or {}).get("suggested_args") or {}).get("allowed_columns", []),
                f"recovery={((r.get('error') or {}).get('recovery'))}",
            ),
        ),
    ]


def filter_scenarios() -> list[Scenario]:
    return [
        Scenario(
            "FILTER", "filter eq on anchor.status",
            "get_metric", {
                "name": "order_count",
                "filters": [{"column": "order.status", "op": "eq", "value": "fulfilled"}],
            },
            lambda r: (
                status_in(r, "success", "degraded")
                and int((_rows(r)[0] or {}).get("order_count", 0)) > 0
                and int((_rows(r)[0] or {}).get("order_count", 999)) < 177,
                f"order_count fulfilled={(_rows(r)[0] or {}).get('order_count')}",
            ),
        ),
        Scenario(
            "FILTER", "filter on joined entity (user.email)",
            "get_metric", {
                "name": "order_count",
                "filters": [{"column": "user.email", "op": "eq", "value": "alice@example.com"}],
            },
            lambda r: (
                (_rows(r)[0] or {}).get("order_count") == 15,
                f"alice's order_count={(_rows(r)[0] or {}).get('order_count')} (expected 15)",
            ),
        ),
        Scenario(
            "FILTER", "filter IN op",
            "get_metric", {
                "name": "order_count",
                "filters": [{"column": "order.status", "op": "in", "value": ["fulfilled", "shipped"]}],
            },
            lambda r: (
                status_in(r, "success", "degraded")
                and int((_rows(r)[0] or {}).get("order_count", 0)) > 0,
                f"order_count={(_rows(r)[0] or {}).get('order_count')}",
            ),
        ),
        Scenario(
            "FILTER", "filter not_null on placed_at",
            "get_metric", {
                "name": "order_count",
                "filters": [{"column": "order.placed_at", "op": "not_null"}],
            },
            lambda r: (
                (_rows(r)[0] or {}).get("order_count") == 177,
                f"order_count (placed_at not null)={(_rows(r)[0] or {}).get('order_count')}",
            ),
        ),
        Scenario(
            "FILTER", "filter unary op with value → malformed_name",
            "get_metric", {
                "name": "order_count",
                "filters": [{"column": "order.placed_at", "op": "is_null", "value": "now"}],
            },
            lambda r: _check_error(r, "malformed_name"),
        ),
        Scenario(
            "FILTER", "filter list op with scalar → malformed_name",
            "get_metric", {
                "name": "order_count",
                "filters": [{"column": "order.status", "op": "in", "value": "fulfilled"}],
            },
            lambda r: _check_error(r, "malformed_name"),
        ),
    ]


def degradation_scenarios() -> list[Scenario]:
    return [
        Scenario(
            "DEGRADE", "limit + group_by without order_by → missing_order_by_with_limit OR fan_out_join",
            "get_metric", {
                "name": "order_count",
                "group_by": ["user.email"],
                "limit": 5,
            },
            lambda r: (
                status_in(r, "degraded") and r.get("degradation_reason") in (
                    "missing_order_by_with_limit", "fan_out_join",
                ),
                f"status={r.get('status')} reason={r.get('degradation_reason')}",
            ),
        ),
        Scenario(
            "DEGRADE", "fan-out from order → order_item join",
            "get_metric", {
                "name": "total_quantity_ordered",
                "group_by": ["user.email"],
                "order_by": [{"column": "total_quantity_ordered", "direction": "desc"}],
                "limit": 3,
            },
            lambda r: (
                # multi-hop chain via order, order_item ↔ order is many-to-one so
                # no fan-out; should be plain success or specific degradation.
                status_in(r, "success", "degraded") and _row_count(r) > 0,
                f"status={r.get('status')} reason={r.get('degradation_reason')}",
            ),
        ),
        Scenario(
            "DEGRADE", "order_by present → no missing_order_by degradation",
            "get_metric", {
                "name": "order_count",
                "group_by": ["user.email"],
                "order_by": [{"column": "order_count", "direction": "desc"}],
                "limit": 5,
            },
            lambda r: (
                r.get("degradation_reason") != "missing_order_by_with_limit",
                f"degradation_reason={r.get('degradation_reason')}",
            ),
        ),
        Scenario(
            "DEGRADE", "no group_by + limit → no missing_order_by degradation",
            "get_metric", {"name": "order_count", "limit": 5},
            lambda r: (
                r.get("degradation_reason") != "missing_order_by_with_limit",
                f"degradation_reason={r.get('degradation_reason')}",
            ),
        ),
    ]


def error_scenarios() -> list[Scenario]:
    return [
        Scenario(
            "ERROR", "unknown_metric",
            "get_metric", {"name": "monthly_recurring_revenue"},
            lambda r: _check_error(r, "unknown_metric"),
        ),
        Scenario(
            "ERROR", "unknown entity in group_by",
            "get_metric", {"name": "order_count", "group_by": ["subscription.tier"]},
            lambda r: _check_error(r, "unknown_name"),
        ),
        Scenario(
            "ERROR", "malformed column ref (no dot)",
            "get_metric", {"name": "order_count", "group_by": ["userid"]},
            lambda r: _check_error(r, "malformed_name"),
        ),
        Scenario(
            "ERROR", "order_by referencing unselected column",
            "get_metric", {
                "name": "order_count",
                "order_by": [{"column": "user.email", "direction": "desc"}],
            },
            lambda r: _check_error(r, "unknown_order_by_column"),
        ),
        Scenario(
            "ERROR", "invalid time_grain string",
            "get_metric", {"name": "total_revenue", "time_grain": "biweekly"},
            lambda r: _check_error(r, "invalid_time_grain"),
        ),
        Scenario(
            "ERROR", "time_grain on metric without time_dimension",
            "get_metric", {"name": "category_count", "time_grain": "day"},
            lambda r: _check_error(r, "invalid_time_grain"),
        ),
    ]


def time_grain_scenarios() -> list[Scenario]:
    return [
        Scenario(
            "TIME", "total_revenue by month",
            "get_metric", {
                "name": "total_revenue",
                "time_grain": "month",
                "order_by": [{"column": "total_revenue", "direction": "desc"}],
                "limit": 12,
            },
            lambda r: (
                status_in(r, "success", "degraded") and _row_count(r) >= 6,
                f"rows={_row_count(r)}",
            ),
        ),
        Scenario(
            "TIME", "order_count by week",
            "get_metric", {
                "name": "order_count",
                "time_grain": "week",
                "limit": 60,
            },
            lambda r: (
                status_in(r, "success", "degraded") and _row_count(r) > 10,
                f"rows={_row_count(r)}",
            ),
        ),
        Scenario(
            "TIME", "total_revenue by day",
            "get_metric", {
                "name": "total_revenue",
                "time_grain": "day",
                "limit": 30,
            },
            lambda r: (
                status_in(r, "success", "degraded") and _row_count(r) > 0,
                f"rows={_row_count(r)}",
            ),
        ),
    ]


def pii_scenarios() -> list[Scenario]:
    return [
        Scenario(
            "PII", "group_by user.email → response carries pii_categories",
            "get_metric", {
                "name": "order_count",
                "group_by": ["user.email"],
                "order_by": [{"column": "order_count", "direction": "desc"}],
                "limit": 3,
            },
            lambda r: (
                "contact" in (r.get("data") or {}).get("pii_categories", []),
                f"pii_categories={(r.get('data') or {}).get('pii_categories', [])}",
            ),
        ),
        Scenario(
            "PII", "group_by user.full_name → response carries contact pii",
            "get_metric", {
                "name": "order_count",
                "group_by": ["user.full_name"],
                "order_by": [{"column": "order_count", "direction": "desc"}],
                "limit": 3,
            },
            lambda r: (
                "contact" in (r.get("data") or {}).get("pii_categories", []),
                f"pii_categories={(r.get('data') or {}).get('pii_categories', [])}",
            ),
        ),
        Scenario(
            "PII", "no PII fields → empty pii_categories",
            "get_metric", {"name": "category_count"},
            lambda r: (
                (r.get("data") or {}).get("pii_categories", []) == [],
                f"pii_categories={(r.get('data') or {}).get('pii_categories')}",
            ),
        ),
    ]


def volume_scenarios() -> list[Scenario]:
    return [
        Scenario(
            "VOLUME", "large limit returns all 50 distinct buyers",
            "get_metric", {
                "name": "order_count",
                "group_by": ["user.email"],
                "order_by": [{"column": "order_count", "direction": "desc"}],
                "limit": 100,
            },
            lambda r: (
                _row_count(r) == 50,
                f"rows={_row_count(r)} (expected 50)",
            ),
        ),
        Scenario(
            "VOLUME", "Alice at position 0 deterministically",
            "get_metric", {
                "name": "order_count",
                "group_by": ["user.email"],
                "order_by": [{"column": "order_count", "direction": "desc"}],
                "limit": 5,
            },
            lambda r: (
                _rows(r)[0].get("group_col_0") == "alice@example.com",
                f"top={_rows(r)[0]}",
            ),
        ),
        Scenario(
            "VOLUME", "duplicate order_by entries deduped",
            "get_metric", {
                "name": "order_count",
                "group_by": ["user.email"],
                "order_by": [
                    {"column": "order_count", "direction": "desc"},
                    {"column": "order_count", "direction": "asc"},
                ],
                "limit": 3,
            },
            lambda r: (
                _row_count(r) == 3 and "alice" in str(_rows(r)[0]).lower(),
                f"top={_rows(r)[0]}",
            ),
        ),
    ]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def run_one(server, scenario: Scenario) -> Result:
    try:
        _content, structured = await server.call_tool(scenario.tool, scenario.args)
    except Exception as exc:
        return Result(scenario.category, scenario.name, "UNEXPECTED", str(exc), error=type(exc).__name__)
    ok, reason = scenario.check(structured)
    return Result(
        scenario.category,
        scenario.name,
        "PASS" if ok else "FAIL",
        reason,
        payload={
            "status": structured.get("status"),
            "error_kind": _err_kind(structured),
            "degradation_reason": structured.get("degradation_reason"),
        },
    )


def all_scenarios() -> list[Scenario]:
    return (
        discovery_scenarios()
        + aggregate_scenarios()
        + ranking_scenarios()
        + join_scenarios()
        + column_validation_scenarios()
        + filter_scenarios()
        + degradation_scenarios()
        + error_scenarios()
        + time_grain_scenarios()
        + pii_scenarios()
        + volume_scenarios()
    )


async def main() -> int:
    store = SQLiteStore(STORE_PATH)
    try:
        engine = sqlalchemy.create_engine(
            DB_URL,
            connect_args={"options": "-c default_transaction_read_only=on"},
        )
        server = build_server(
            store=store,
            source_connection_id=SOURCE_ID,
            embedder=fastembed_default(),
            metric_executor=EngineMetricExecutor(engine),
        )
        scenarios = all_scenarios()
        results: list[Result] = []

        current_cat = ""
        for s in scenarios:
            if s.category != current_cat:
                current_cat = s.category
                print(f"\n=== {s.category} ===")
            r = await run_one(server, s)
            results.append(r)
            marker = {"PASS": "OK", "FAIL": "XX", "UNEXPECTED": "??"}[r.status]
            print(f"[{marker}] {r.name}")
            print(f"        {r.reason}")
            if r.status != "PASS":
                print(f"        payload={json.dumps(r.payload, default=str)}")
                if r.error:
                    print(f"        error={r.error}")

        # Summary by category
        print()
        print("=" * 70)
        from collections import Counter

        by_cat: dict[str, Counter] = {}
        for r in results:
            by_cat.setdefault(r.category, Counter())[r.status] += 1
        for cat, counts in sorted(by_cat.items()):
            total = sum(counts.values())
            pass_count = counts.get("PASS", 0)
            print(f"  {cat:12s}  {pass_count:>2}/{total:<2} PASS  "
                  f"({counts.get('FAIL', 0)} FAIL, {counts.get('UNEXPECTED', 0)} UNEXPECTED)")
        total = len(results)
        passed = sum(1 for r in results if r.status == "PASS")
        failed = sum(1 for r in results if r.status == "FAIL")
        bugs = sum(1 for r in results if r.status == "UNEXPECTED")
        print("=" * 70)
        print(f"TOTAL: {passed}/{total} PASS  ({failed} FAIL, {bugs} UNEXPECTED)")
        return 0 if failed == 0 and bugs == 0 else 1
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
