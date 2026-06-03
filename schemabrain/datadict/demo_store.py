"""Deterministic, offline SaaS demo store — the golden + dogfood fixture.

Builds a fully-populated SQLite store (tables, columns, PII tags,
entities, joins, metrics) with a FIXED `source_connection_id`, WITHOUT a
live Postgres, an Anthropic call, or any RNG. Shared by:

  - `tests/datadict/test_dictionary_golden.py` (byte-for-byte golden), and
  - the committed Mintlify dogfood page `docs/data-dictionary.md`.

`saas.sql` is Postgres-only DDL (it is loaded via `psql` into the demo
container, never into SchemaBrain's SQLite store), so there is no offline
loader to reuse — the 12 tables / 84 columns are transcribed from it
below. `_assert_tables_match_saas_sql` pins the transcription against the
real DDL so a future schema edit can't drift the fixture silently.

The semantic layer (entities / joins / metrics) IS loadable offline: it
comes from the same bundled YAML packs the `schemabrain init --demo`
wizard applies, parsed deterministically.

`DICTIONARY_SOURCE_URL` mirrors the wizard's `DEMO_DATABASE_URL`, so
`SOURCE_ID` equals the id a real `schemabrain init --demo` would assign —
pinned by `test_demo_store.test_source_id_is_pinned`.
"""

from __future__ import annotations

from pathlib import Path

from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.entities.yaml_grammar import parse_entity_yaml_file
from schemabrain.eval.bundled import (
    bundled_entities_fixture_dir,
    bundled_joins_fixture_dir,
    bundled_metrics_fixture_dir,
)
from schemabrain.joins.yaml_grammar import parse_canonical_join_yaml_file
from schemabrain.metrics.yaml_grammar import parse_metric_yaml_file
from schemabrain.pii import classify_column

# Mirrors `setup.setup_stage.DEMO_DATABASE_URL` (bare `postgresql://`) so
# the dogfood doc reflects the source id a real `init --demo` produces.
DICTIONARY_SOURCE_URL = "postgresql://postgres:local@localhost:5433/postgres"
# == make_source_id(DICTIONARY_SOURCE_URL); pinned in test_demo_store.
SOURCE_ID = "6e04b1c8f8789f48"

_SCHEMA = "public"
_PACK = "saas"

# Type strings use lowercase canonical Postgres names (what `psql \d`
# shows). A live SQLAlchemy index may stringify them slightly differently
# (e.g. uppercased), so the dogfood is representative, not a byte-mirror
# of a live index — but it is internally consistent (the in-sync test
# regenerates from this same fixture).
#
# Each row: (column_name, data_type, nullable, is_primary_key).
# Transcribed 1:1 from `schemabrain/eval/fixtures/saas.sql` (12 tables,
# 84 columns). FK-safe declaration order (parents before children).
_TABLES: dict[str, tuple[tuple[str, str, bool, bool], ...]] = {
    "workspaces": (
        ("id", "bigint", False, True),
        ("name", "text", False, False),
        ("slug", "text", False, False),
        ("region", "text", False, False),
        ("plan_tier", "text", False, False),
        ("seat_count", "integer", False, False),
        ("created_at", "timestamptz", False, False),
    ),
    "plans": (
        ("id", "bigint", False, True),
        ("plan_code", "text", False, False),
        ("title", "text", False, False),
        ("monthly_price_cents", "integer", False, False),
        ("included_seats", "integer", False, False),
        ("sla_tier", "text", False, False),
        ("is_active", "boolean", False, False),
    ),
    "users": (
        ("id", "bigint", False, True),
        ("workspace_id", "bigint", False, False),
        ("email", "text", False, False),
        ("full_name", "text", False, False),
        ("password_hash", "text", False, False),
        ("role", "text", False, False),
        ("bio", "text", True, False),
        ("created_at", "timestamptz", False, False),
    ),
    "subscriptions": (
        ("id", "bigint", False, True),
        ("workspace_id", "bigint", False, False),
        ("plan_id", "bigint", False, False),
        ("status", "text", False, False),
        ("mrr_cents", "integer", False, False),
        ("started_at", "timestamptz", False, False),
        ("canceled_at", "timestamptz", True, False),
        ("trial_ends_at", "timestamptz", True, False),
    ),
    "subscription_items": (
        ("id", "bigint", False, True),
        ("subscription_id", "bigint", False, False),
        ("plan_id", "bigint", False, False),
        ("seats", "integer", False, False),
        ("unit_price_cents", "integer", False, False),
    ),
    "payment_methods": (
        ("id", "bigint", False, True),
        ("workspace_id", "bigint", False, False),
        ("card_number_last4", "text", False, False),
        ("card_brand", "text", False, False),
        ("card_exp_month", "integer", False, False),
        ("card_exp_year", "integer", False, False),
        ("is_default", "boolean", False, False),
        ("created_at", "timestamptz", False, False),
    ),
    "invoices": (
        ("id", "bigint", False, True),
        ("workspace_id", "bigint", False, False),
        ("subscription_id", "bigint", True, False),
        ("invoice_number", "text", False, False),
        ("status", "text", False, False),
        ("invoice_total_cents", "integer", False, False),
        ("issued_at", "timestamptz", False, False),
        ("paid_at", "timestamptz", True, False),
    ),
    "api_keys": (
        ("id", "bigint", False, True),
        ("workspace_id", "bigint", False, False),
        ("key_prefix", "text", False, False),
        ("api_key_hash", "text", False, False),
        ("scope", "text", False, False),
        ("created_at", "timestamptz", False, False),
        ("revoked_at", "timestamptz", True, False),
    ),
    "sessions": (
        ("id", "bigint", False, True),
        ("user_id", "bigint", False, False),
        ("session_token", "text", False, False),
        ("last_login_ip", "text", True, False),
        ("created_at", "timestamptz", False, False),
        ("expires_at", "timestamptz", False, False),
    ),
    "usage_events": (
        ("id", "bigint", False, True),
        ("workspace_id", "bigint", False, False),
        ("event_type", "text", False, False),
        ("quantity", "integer", False, False),
        ("occurred_at", "timestamptz", False, False),
        ("recorded_at", "timestamptz", False, False),
    ),
    "support_tickets": (
        ("id", "bigint", False, True),
        ("workspace_id", "bigint", False, False),
        ("user_id", "bigint", True, False),
        ("subject", "text", False, False),
        ("body", "text", True, False),
        ("status", "text", False, False),
        ("created_at", "timestamptz", False, False),
    ),
    "billing_profiles": (
        ("id", "bigint", False, True),
        ("workspace_id", "bigint", False, False),
        ("legal_entity", "text", False, False),
        ("tax_id", "text", False, False),
        ("national_id", "text", True, False),
        ("country_code", "text", False, False),
        ("created_at", "timestamptz", False, False),
    ),
}


def _build_table(table_name: str, specs: tuple[tuple[str, str, bool, bool], ...]) -> Table:
    columns = tuple(
        Column(
            name=name,
            table_name=table_name,
            schema_name=_SCHEMA,
            data_type=data_type,
            nullable=nullable,
            ordinal_position=index + 1,
            is_primary_key=is_pk,
        )
        for index, (name, data_type, nullable, is_pk) in enumerate(specs)
    )
    return Table(name=table_name, schema_name=_SCHEMA, columns=columns, foreign_keys=())


def build_saas_dictionary_store(store_path: str | Path) -> str:
    """Populate a fresh store at `store_path` with the SaaS demo and return its source id.

    Writes, in FK-safe order: physical tables + columns -> per-column PII
    tags (heuristic `classify_column`, exactly as the indexer would run it)
    -> entities -> canonical joins -> metrics (all from the bundled YAML
    packs). Deterministic: no RNG, no network. Re-running over the same
    path rebuilds an identical store (every write is an idempotent upsert).
    """
    with SQLiteStore(store_path) as store:
        for table_name, specs in _TABLES.items():
            table = _build_table(table_name, specs)
            store.write_table(table, source_connection_id=SOURCE_ID)
            tags = {
                col.name: classify_column(
                    col.name,
                    column_type=col.data_type,
                    is_primary_key=col.is_primary_key,
                    table_name=table_name,
                )
                for col in table.columns
            }
            store.write_column_pii_tags(
                source_connection_id=SOURCE_ID,
                qualified_table=f"{_SCHEMA}.{table_name}",
                tags=tags,
            )

        for path in sorted(bundled_entities_fixture_dir(pack=_PACK).glob("*.yaml")):
            store.write_entity(parse_entity_yaml_file(path), source_connection_id=SOURCE_ID)
        for path in sorted(bundled_joins_fixture_dir(pack=_PACK).glob("*.yaml")):
            store.write_canonical_join(
                parse_canonical_join_yaml_file(path), source_connection_id=SOURCE_ID
            )
        for path in sorted(bundled_metrics_fixture_dir(pack=_PACK).glob("*.yaml")):
            store.write_metric(parse_metric_yaml_file(path), source_connection_id=SOURCE_ID)

    return SOURCE_ID
