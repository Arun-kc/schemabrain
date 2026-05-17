"""get_metric PII propagation + pii_blocked refusal tests.

Asserts that:
  - get_metric attaches propagated categories to MetricResult.pii_categories
  - `pii_block` policy raises PiiBlockedError pre-emission
  - PiiBlockedError carries attempted + blocked sets for the audit row
  - fingerprint differs across pii-tagged vs untagged plans (the
    load-bearing demonstration that propagation actually varies the
    audit-row fingerprint)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from schemabrain.audit.writer import build_audit_row
from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.metric import Metric, MetricMeasure
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.mcp.envelope import Recovery, ToolError, ToolResponse
from schemabrain.mcp.get_metric import get_metric_impl
from schemabrain.mcp.shapes import MetricResult
from schemabrain.semantic.compiler import PiiBlockedError

SRC = "pii_test_source"


def _seed_table_and_entity(store: SQLiteStore) -> None:
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
            Column(
                name="amount",
                table_name="users",
                schema_name="public",
                data_type="NUMERIC",
                nullable=False,
                ordinal_position=3,
            ),
        ),
    )
    store.write_table(users, source_connection_id=SRC)
    store.write_entity(
        Entity(
            name="user",
            description="",
            binding=SingleTableBinding(qualified_table="public.users"),
            identity="id",
        ),
        source_connection_id=SRC,
    )


class _FakeExecutor:
    """Stub MetricExecutor — returns fixed rows without touching a DB."""

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or [{"value": 42}]
        self.execute_calls: list[tuple[str, dict]] = []

    def execute(self, sql: str, params: dict) -> list[dict]:
        self.execute_calls.append((sql, params))
        return list(self.rows)


def _write_metric(
    store: SQLiteStore,
    *,
    name: str,
    column: str,
    agg: str = "sum",
) -> None:
    store.write_metric(
        Metric(
            name=name,
            description="",
            entity="user",
            measure=MetricMeasure(agg=agg, column=column),  # type: ignore[arg-type]
            time_dimension=None,
            time_grains=(),
        ),
        source_connection_id=SRC,
    )


class TestPropagationAttachedToMetricResult:
    def test_metric_on_pii_column_attaches_contact_category(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "sb.db")
        try:
            _seed_table_and_entity(store)
            store.write_column_pii_tags(
                source_connection_id=SRC,
                qualified_table="public.users",
                tags={"email": ("pii", frozenset({"contact"}))},
            )
            _write_metric(store, name="email_count", column="email", agg="count")
            executor = _FakeExecutor()
            result = get_metric_impl(
                store=store,
                executor=executor,
                source_connection_id=SRC,
                name="email_count",
            )
            assert result.pii_categories == ("contact",)
        finally:
            store.close()

    def test_metric_on_untagged_column_has_empty_categories(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "sb.db")
        try:
            _seed_table_and_entity(store)
            # Deliberately do NOT classify `amount` — verifies that
            # the absent-row code path produces ("public", frozenset())
            # rather than raising.
            _write_metric(store, name="amount_sum", column="amount")
            executor = _FakeExecutor()
            result = get_metric_impl(
                store=store,
                executor=executor,
                source_connection_id=SRC,
                name="amount_sum",
            )
            assert result.pii_categories == ()
        finally:
            store.close()

    def test_group_by_pii_column_unions_with_measure_category(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "sb.db")
        try:
            _seed_table_and_entity(store)
            store.write_column_pii_tags(
                source_connection_id=SRC,
                qualified_table="public.users",
                tags={
                    "email": ("pii", frozenset({"contact"})),
                    "amount": ("pii", frozenset({"financial"})),
                },
            )
            _write_metric(store, name="amount_sum", column="amount")
            executor = _FakeExecutor()
            result = get_metric_impl(
                store=store,
                executor=executor,
                source_connection_id=SRC,
                name="amount_sum",
                group_by=("user.email",),
            )
            assert result.pii_categories == ("contact", "financial")
        finally:
            store.close()


class TestPiiBlockedRefusal:
    def test_block_set_intersecting_propagated_raises(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "sb.db")
        try:
            _seed_table_and_entity(store)
            store.write_column_pii_tags(
                source_connection_id=SRC,
                qualified_table="public.users",
                tags={"email": ("pii", frozenset({"contact"}))},
            )
            _write_metric(store, name="email_count", column="email", agg="count")
            executor = _FakeExecutor()
            with pytest.raises(PiiBlockedError) as exc_info:
                get_metric_impl(
                    store=store,
                    executor=executor,
                    source_connection_id=SRC,
                    name="email_count",
                    pii_block=frozenset({"contact"}),  # type: ignore[arg-type]
                )
            assert exc_info.value.attempted_categories == ("contact",)
            assert exc_info.value.blocked_categories == ("contact",)
            # CRITICAL: emit_sql + executor.execute must NOT have run.
            # The refusal happens pre-emission so the SQL is never
            # compiled, logged, or executed — verified by the empty
            # `execute_calls` list on the fake executor.
            assert executor.execute_calls == []
        finally:
            store.close()

    def test_disjoint_block_set_does_not_raise(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "sb.db")
        try:
            _seed_table_and_entity(store)
            store.write_column_pii_tags(
                source_connection_id=SRC,
                qualified_table="public.users",
                tags={"email": ("pii", frozenset({"contact"}))},
            )
            _write_metric(store, name="email_count", column="email", agg="count")
            executor = _FakeExecutor()
            result = get_metric_impl(
                store=store,
                executor=executor,
                source_connection_id=SRC,
                name="email_count",
                pii_block=frozenset({"financial", "health"}),  # type: ignore[arg-type]
            )
            assert result.pii_categories == ("contact",)
        finally:
            store.close()

    def test_empty_block_set_disables_enforcement(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "sb.db")
        try:
            _seed_table_and_entity(store)
            store.write_column_pii_tags(
                source_connection_id=SRC,
                qualified_table="public.users",
                tags={"email": ("pii", frozenset({"contact"}))},
            )
            _write_metric(store, name="email_count", column="email", agg="count")
            executor = _FakeExecutor()
            # Default pii_block=frozenset() = enforcement off.
            result = get_metric_impl(
                store=store,
                executor=executor,
                source_connection_id=SRC,
                name="email_count",
            )
            assert result.pii_categories == ("contact",)
        finally:
            store.close()


class TestEmptyTagTableWarning:
    def test_warning_fires_when_no_tags_for_source(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Reset the dedup set so this test runs cleanly even if other
        # tests already consumed the once-per-source budget.
        from schemabrain.mcp import get_metric as _gm

        _gm._empty_tag_table_warned.clear()

        store = SQLiteStore(tmp_path / "sb.db")
        try:
            _seed_table_and_entity(store)
            # Deliberately do NOT classify any column.
            _write_metric(store, name="m1", column="email", agg="count")
            executor = _FakeExecutor()
            get_metric_impl(
                store=store,
                executor=executor,
                source_connection_id=SRC,
                name="m1",
            )
            stderr = capsys.readouterr().err
            assert "no PII tags found" in stderr
            assert SRC in stderr
        finally:
            store.close()


class TestAuditRowReadsPiiCategories:
    """build_audit_row's pii_categories extraction across both paths.

    Success path reads from response.data.pii_categories;
    refusal path reads from response.error.pii_categories.
    """

    def test_success_envelope_categories_pulled_from_data(self) -> None:
        result = MetricResult(
            rows=[],
            row_count=0,
            sql_skeleton="SELECT 1",
            sql_params={},
            fingerprint="fp-unset",
            token_estimate=0,
            required_joins=[],
            fan_out_join_names=[],
            pii_categories=("contact", "financial"),
        )
        envelope = ToolResponse[MetricResult](status="success", data=result)
        row = build_audit_row(
            tool_name="get_metric",
            source_connection_id=SRC,
            response=envelope,
        )
        # build_audit_row now carries the typed frozenset through the
        # draft; CSV encoding happens at AuditWriter.write time.
        assert row["pii_categories"] == frozenset({"contact", "financial"})
        assert row["refusal_reason"] is None
        assert row["cost_class"] == "small"

    def test_refusal_envelope_categories_pulled_from_error(self) -> None:
        envelope = ToolResponse(
            status="refused",
            error=ToolError(
                kind="pii_blocked",
                message="blocked",
                recovery=Recovery(suggested_tool="describe_entity"),
                pii_categories=("contact",),
            ),
        )
        row = build_audit_row(
            tool_name="get_metric",
            source_connection_id=SRC,
            response=envelope,
        )
        assert row["pii_categories"] == frozenset({"contact"})
        assert row["refusal_reason"] == "pii_blocked"
        assert row["cost_class"] == "refused"


class TestFingerprintDifferentiation:
    """Fingerprints actually differ across calls that touch different
    category sets. Prior to PII propagation wiring, every audit row
    hashed identically because every `FingerprintInput` field was a
    v1 constant; pii_tags_touched now varies per call.
    """

    def test_two_metrics_with_different_pii_have_distinct_audit_fingerprints(
        self, tmp_path: Path
    ) -> None:
        from schemabrain.audit.writer import AuditWriter

        store = SQLiteStore(tmp_path / "sb.db")
        try:
            _seed_table_and_entity(store)
            store.write_column_pii_tags(
                source_connection_id=SRC,
                qualified_table="public.users",
                tags={
                    "email": ("pii", frozenset({"contact"})),
                    "amount": ("pii", frozenset({"financial"})),
                },
            )
            _write_metric(store, name="email_count", column="email", agg="count")
            _write_metric(store, name="amount_sum", column="amount", agg="sum")
            executor = _FakeExecutor()

            r1 = get_metric_impl(
                store=store,
                executor=executor,
                source_connection_id=SRC,
                name="email_count",
            )
            r2 = get_metric_impl(
                store=store,
                executor=executor,
                source_connection_id=SRC,
                name="amount_sum",
            )

            writer = AuditWriter(tmp_path / "sb.db")
            try:
                env1 = ToolResponse[MetricResult](status="success", data=r1)
                env2 = ToolResponse[MetricResult](status="success", data=r2)
                row1 = writer.write(
                    build_audit_row(
                        tool_name="get_metric",
                        source_connection_id=SRC,
                        response=env1,
                    )
                )
                row2 = writer.write(
                    build_audit_row(
                        tool_name="get_metric",
                        source_connection_id=SRC,
                        response=env2,
                    )
                )
                # Pre-PR-#36 these would have been identical (both v1
                # constants for every FingerprintInput field). Post-
                # PR-#36 pii_tags_touched varies, so the fingerprints
                # must differ.
                assert row1.fingerprint != row2.fingerprint
            finally:
                writer.close()
        finally:
            store.close()
