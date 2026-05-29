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
from schemabrain.semantic.compiler import PiiBlockedError

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


def test_get_metric_max_email_refused_before_executor_runs(store, fw_005_pg) -> None:
    """The ``MAX(email)`` aggregate MUST refuse before SQL emission.

    The default ``pii_block`` is the catastrophic-leak set
    (``credential``, ``payment_card``, ``government_id``). ``contact``
    is not in that set, so without a dedicated gate a metric defined
    as ``max(email)`` would have run end-to-end and returned the
    lexicographic max email — leaking the row value disguised as an
    aggregate.

    The secure posture, locked by ``_resolve_pii_categories`` in
    ``mcp/get_metric.py``: MIN/MAX over a column with non-empty PII
    categories raises ``PiiBlockedError`` independent of the
    operator's ``--pii-block`` policy. The operator's policy is about
    which aggregate categories may pass; MIN/MAX of a tagged column
    isn't aggregation — the returned value IS a row value,
    indistinguishable from a SELECT.
    """
    _seed_store_with_max_email_metric(store)
    executor = _CapturingExecutor(fw_005_pg)

    with pytest.raises(PiiBlockedError) as exc_info:
        get_metric_impl(
            store=store,
            executor=executor,
            source_connection_id=SOURCE_ID,
            name="max_email_user",
        )

    # The refusal names the column that triggered it so the operator
    # can act. The category surfaced is the column's tag set, NOT a
    # subset of the operator's policy — the policy is independent of
    # this gate.
    assert exc_info.value.blocked_categories == ("contact",), (
        "BYPASS: MIN/MAX-as-sampling gate must refuse with the "
        "column's category set, regardless of the operator's "
        "--pii-block policy. The category disclosure here is the row "
        "leak, not a policy violation."
    )
    assert "max" in str(exc_info.value)
    assert "email" in str(exc_info.value)

    # CRITICAL: the executor must NEVER have been invoked. Refusal
    # happens pre-SQL-emission, so even the parameterised SQL is
    # never compiled, never logged, never sent to Postgres.
    assert executor.calls == [], (
        "BYPASS: refusal must fire before SQL emission. Any executor "
        "call here means the firewall let the query through and we're "
        "relying on the post-execution gate to redact the row, which "
        "is a defense-in-depth issue: a logged SQL is itself a leak "
        "surface."
    )
