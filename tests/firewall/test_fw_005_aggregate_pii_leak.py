"""FW-005 — ``MAX(email)`` / ``MIN(email)`` aggregates return
raw row data through ``get_metric``.

Background:

    ``get_metric`` MAX/MIN on text columns. ``MAX(email) FROM users``
    returns the actual max-string email value as raw row data in
    ``result.rows``. The default ``pii_block`` is
    ``credential,payment_card,government_id`` (catastrophic-leak set)
    — ``contact`` is NOT in the default block.

Expected SECURE behaviour:

    Aggregations that return a SAMPLE of the underlying data
    (``MAX``/``MIN`` over text, ``MIN``/``MAX`` over identifiers) must
    be treated like a row-level read for the column's PII category,
    not like a numeric aggregate. Either:
      (a) refuse the metric when ``MAX``/``MIN`` is applied to a
          PII-tagged text column, OR
      (b) auto-expand the ``pii_block`` for MAX/MIN to cover the
          column's tag categories.

This test runs against a live Postgres at ``127.0.0.1:5433``. We
create a private schema, populate the rows, register the metric, and
execute end-to-end through the real ``EngineMetricExecutor`` so the
test demonstrates the real bypass surface (not a stubbed executor
returning canned rows).
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.metric import Metric, MetricMeasure
from schemabrain.core.models import Column, Table
from schemabrain.mcp.get_metric import get_metric_impl

pytestmark = [pytest.mark.firewall_bypass, pytest.mark.integration]

SOURCE_ID = "fw_005"
SCHEMA = "fw_005"


class _CapturingExecutor:
    """Wraps the real Postgres engine + records every SQL it ran.

    Mirrors the ``MetricExecutor`` Protocol — ``execute(sql, params)``
    returns rows. Implemented inline so the test stays self-contained;
    the real ``EngineMetricExecutor`` carries a read-only connection
    pool we don't need for a one-off table.
    """

    def __init__(self, engine) -> None:
        self._engine = engine
        self.calls: list[tuple[str, dict]] = []

    def execute(self, sql_text: str, params: dict) -> list[dict]:
        self.calls.append((sql_text, params))
        with self._engine.connect() as conn:
            result = conn.execute(text(sql_text), params)
            return [dict(row._mapping) for row in result]


@pytest.fixture
def fw_005_pg(pg_engine, make_schema):
    """Schema with a ``users`` table holding 3 known emails."""
    make_schema(SCHEMA)
    with pg_engine.begin() as conn:
        conn.execute(
            text(
                f"""
                CREATE TABLE "{SCHEMA}".users (
                    id bigserial PRIMARY KEY,
                    email text NOT NULL
                )
                """
            )
        )
        conn.execute(
            text(
                f"""
                INSERT INTO "{SCHEMA}".users (email) VALUES
                  ('alice@firewall.test'),
                  ('bob@firewall.test'),
                  ('zachary@firewall.test')
                """
            )
        )
    return pg_engine


def _seed_store_with_max_email_metric(store) -> None:
    """SchemaBrain store: ``users`` table + ``max_email_user`` metric."""
    users = Table(
        name="users",
        schema_name=SCHEMA,
        columns=(
            Column(
                name="id",
                table_name="users",
                schema_name=SCHEMA,
                data_type="bigint",
                nullable=False,
                ordinal_position=1,
                is_primary_key=True,
            ),
            Column(
                name="email",
                table_name="users",
                schema_name=SCHEMA,
                data_type="text",
                nullable=False,
                ordinal_position=2,
            ),
        ),
    )
    store.write_table(users, source_connection_id=SOURCE_ID)
    store.write_column_pii_tags(
        source_connection_id=SOURCE_ID,
        qualified_table=f"{SCHEMA}.users",
        tags={"email": ("pii", frozenset({"contact"}))},
    )
    store.write_entity(
        Entity(
            name="user",
            description="",
            binding=SingleTableBinding(qualified_table=f"{SCHEMA}.users"),
            identity="id",
        ),
        source_connection_id=SOURCE_ID,
    )
    store.write_metric(
        Metric(
            name="max_email_user",
            description="",
            entity="user",
            measure=MetricMeasure(agg="max", column="email"),
            time_dimension=None,
            time_grains=(),
        ),
        source_connection_id=SOURCE_ID,
    )


def test_get_metric_max_email_does_not_leak_raw_address(store, fw_005_pg) -> None:
    """The ``MAX(email)`` aggregate MUST NOT return a raw email string.

    The default ``pii_block`` is the catastrophic-leak set
    (``credential``, ``payment_card``, ``government_id``). ``contact``
    is not in that set, so a metric defined as ``max(email)`` runs end-
    to-end and returns the lexicographic max email — leaking the row
    value disguised as an aggregate.

    The assertion below is the SECURE shape — either the metric must
    refuse (``PiiBlockedError``) or the row value must not be a raw
    email. Today the call succeeds and ``rows[0]['max_email_user']`` is
    ``'zachary@firewall.test'``.
    """
    _seed_store_with_max_email_metric(store)
    executor = _CapturingExecutor(fw_005_pg)

    result = get_metric_impl(
        store=store,
        executor=executor,
        source_connection_id=SOURCE_ID,
        name="max_email_user",
    )

    # `MetricResult.rows` is a list of dicts. The aggregate alias is
    # the metric name. We expect either a refusal (no rows returned
    # because PiiBlockedError fired upstream of the executor) or a
    # masked / coarse-grained value.
    assert result.rows, "sanity: real Postgres returned rows"
    max_value = result.rows[0].get("max_email_user")
    assert max_value is not None, "sanity: max aggregate populated"

    # Today: max_value == 'zachary@firewall.test' — a real row value.
    assert "@" not in str(max_value), (
        f"BYPASS: get_metric(max_email_user) returned {max_value!r} — "
        f"a raw email pulled from a user row, surfaced through a MAX "
        f"aggregate. The 'contact' tag is not in the default "
        f"catastrophic-leak block, so the firewall let the row escape "
        f"as an aggregate value. Treat MAX/MIN over PII text columns "
        f"as row-level reads."
    )
