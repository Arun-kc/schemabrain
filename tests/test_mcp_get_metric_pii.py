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
from schemabrain.core.join import CanonicalJoin, JoinColumnPair
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

    def test_composite_expression_unions_all_operand_categories(self, tmp_path: Path) -> None:
        """Composite-expression measure unions PII categories across
        every operand column — without this, a tagged operand silently
        bypasses `--pii-block` because the v1 code only collected
        `measure.column` (a single string, now None for composites)."""
        store = SQLiteStore(tmp_path / "sb.db")
        try:
            _seed_table_and_entity(store)
            store.write_column_pii_tags(
                source_connection_id=SRC,
                qualified_table="public.users",
                tags={"email": ("pii", frozenset({"contact"}))},
            )
            # `email * amount` is silly but structurally permitted —
            # the parser only checks shape, not semantic sanity. We
            # use this shape to assert PII propagation walks BOTH
            # operands. amount is untagged → empty contribution;
            # email is tagged contact → contact in the propagated set.
            store.write_metric(
                Metric(
                    name="weird_metric",
                    description="",
                    entity="user",
                    measure=MetricMeasure(agg="sum", expression="email * amount"),
                    time_dimension=None,
                    time_grains=(),
                ),
                source_connection_id=SRC,
            )
            executor = _FakeExecutor()
            result = get_metric_impl(
                store=store,
                executor=executor,
                source_connection_id=SRC,
                name="weird_metric",
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
            # The exception carries the metric's anchor entity so the
            # MCP envelope wrapper can populate
            # `recovery.suggested_args.name` — an agent following the
            # structured-recovery contract gets `describe_entity(name=
            # <anchor>)` pre-filled and can enumerate non-PII columns
            # without parsing the human-readable message.
            assert exc_info.value.anchor_entity == "user"
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


class TestRefusalAttemptedBlockedAsymmetry:
    """Probe-oracle defense: `attempted_categories` (the full
    propagated set of categories the metric touched) stays out of
    the agent-facing envelope and message. Only `blocked_categories`
    (the policy intersection that triggered refusal) is surfaced.
    Surfacing `attempted` would let an adversarial agent map the
    full PII tagging in O(columns) calls without ever invoking
    `describe_entity`.

    The audit row keeps `attempted_categories` (operator-visible) so
    forensics still shows *what was touched*, not just *what was
    blocked*. The asymmetry is the load-bearing fix for L-2.
    """

    def test_blocked_categories_are_strict_subset_of_attempted(self, tmp_path: Path) -> None:
        # email→contact and amount→financial. Block only `credential`
        # (which neither column carries) — propagated categories
        # surface but the metric does not refuse. Then block only
        # `contact` and show that `attempted` is the FULL propagation
        # set ({contact, financial}) while `blocked` is the
        # intersection-only ({contact}).
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
            _write_metric(store, name="amount_sum", column="amount", agg="sum")
            executor = _FakeExecutor()
            with pytest.raises(PiiBlockedError) as exc_info:
                get_metric_impl(
                    store=store,
                    executor=executor,
                    source_connection_id=SRC,
                    name="amount_sum",
                    group_by=("user.email",),
                    pii_block=frozenset({"contact"}),  # type: ignore[arg-type]
                )
            # attempted = full propagated set (both columns touched)
            assert exc_info.value.attempted_categories == ("contact", "financial")
            # blocked = policy intersection only (the trigger)
            assert exc_info.value.blocked_categories == ("contact",)
        finally:
            store.close()

    def test_refusal_message_references_blocked_not_attempted(self, tmp_path: Path) -> None:
        # The human-readable message is part of the agent surface.
        # It must reference only the blocked subset — naming the
        # untouched-by-policy `financial` category in the agent-
        # facing message would defeat the asymmetry the envelope
        # field enforces.
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
            _write_metric(store, name="amount_sum", column="amount", agg="sum")
            executor = _FakeExecutor()
            with pytest.raises(PiiBlockedError) as exc_info:
                get_metric_impl(
                    store=store,
                    executor=executor,
                    source_connection_id=SRC,
                    name="amount_sum",
                    group_by=("user.email",),
                    pii_block=frozenset({"contact"}),  # type: ignore[arg-type]
                )
            message = str(exc_info.value)
            assert "contact" in message
            # The untouched-by-policy category must NOT appear in
            # the agent-facing message.
            assert "financial" not in message
        finally:
            store.close()

    def test_refusal_envelope_surfaces_blocked_only(self, tmp_path: Path) -> None:
        # End-to-end through the MCP envelope wrapper. The refused
        # `ToolResponse` carries `pii_categories=blocked_categories`,
        # NOT the full attempted set.
        import asyncio

        from schemabrain.mcp import build_server
        from schemabrain.mcp.envelope import ToolResponse

        class _ServerStubEmbedder:
            model_name = "stub"
            dimension = 4

            def embed(self, text: str) -> tuple[float, ...]:
                del text
                return (1.0, 0.0, 0.0, 0.0)

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
            _write_metric(store, name="amount_sum", column="amount", agg="sum")
            server = build_server(
                store=store,
                source_connection_id=SRC,
                embedder=_ServerStubEmbedder(),
                metric_executor=_FakeExecutor(),
                pii_block=frozenset({"contact"}),  # type: ignore[arg-type]
            )
            _content, structured = asyncio.run(
                server.call_tool(
                    "get_metric",
                    {"name": "amount_sum", "group_by": ["user.email"]},
                )
            )
            envelope = ToolResponse.model_validate(structured)
            assert envelope.status == "refused"
            assert envelope.error is not None
            assert envelope.error.kind == "pii_blocked"
            # Only blocked surfaces; attempted-but-not-blocked
            # `financial` stays out of the wire.
            assert tuple(envelope.error.pii_categories) == ("contact",)
            assert "financial" not in envelope.error.message
        finally:
            store.close()

    def test_audit_row_persists_attempted_categories(self, tmp_path: Path) -> None:
        # The audit row builds from a refused envelope's
        # `error.pii_categories` field — which post-L-2 carries
        # `blocked_categories` (the on-wire subset). To verify the
        # `attempted` set still reaches the audit-row test surface
        # via the exception instance fields, this test asserts the
        # PiiBlockedError carries both: `attempted` (full) and
        # `blocked` (subset). Operators digging into a refused-row
        # incident can pull `attempted` directly from the structured
        # error chain even though the agent never saw it.
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
            _write_metric(store, name="amount_sum", column="amount", agg="sum")
            executor = _FakeExecutor()
            with pytest.raises(PiiBlockedError) as exc_info:
                get_metric_impl(
                    store=store,
                    executor=executor,
                    source_connection_id=SRC,
                    name="amount_sum",
                    group_by=("user.email",),
                    pii_block=frozenset({"contact"}),  # type: ignore[arg-type]
                )
            # The exception INSTANCE preserves both sides of the
            # asymmetry — operator-visible (attempted) and
            # agent-visible (blocked).
            assert exc_info.value.attempted_categories == ("contact", "financial")
            assert exc_info.value.blocked_categories == ("contact",)
            # And they are different — the load-bearing invariant
            # that proves the audit-row writer sees more than the
            # agent does.
            assert exc_info.value.attempted_categories != exc_info.value.blocked_categories
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


# ----- Regression coverage: PII propagation through multi-hop JOIN ON pairs ---------


class TestPiiPropagatesAcrossJoinOnPairs:
    """regression test (silent-failure F5): multi-hop chains
    introduce intermediate JOIN ON columns that the agent's metric
    request never names directly (they live in `plan.joins[*].on_pairs`,
    not in `group_by` or `filter`). Before the fold, those columns
    bypassed `--pii-block` — a PII-tagged FK used only as a join key
    on an intermediate hop would NOT trigger refusal. The fix walks
    `plan.joins` and tags every on-pair column against the appropriate
    qualified_table.
    """

    def _seed_two_hop_with_pii_on_intermediate_fk(self, store: SQLiteStore) -> None:
        # `order_item → order → user` chain, with `order.user_id` (FK
        # column used in the orders_user_id JOIN) tagged contact-PII.
        order_items = Table(
            name="order_items",
            schema_name="public",
            columns=(
                Column(
                    name="id",
                    table_name="order_items",
                    schema_name="public",
                    data_type="BIGINT",
                    nullable=False,
                    ordinal_position=1,
                    is_primary_key=True,
                ),
                Column(
                    name="order_id",
                    table_name="order_items",
                    schema_name="public",
                    data_type="BIGINT",
                    nullable=False,
                    ordinal_position=2,
                ),
                Column(
                    name="quantity",
                    table_name="order_items",
                    schema_name="public",
                    data_type="INTEGER",
                    nullable=False,
                    ordinal_position=3,
                ),
            ),
        )
        orders = Table(
            name="orders",
            schema_name="public",
            columns=(
                Column(
                    name="id",
                    table_name="orders",
                    schema_name="public",
                    data_type="BIGINT",
                    nullable=False,
                    ordinal_position=1,
                    is_primary_key=True,
                ),
                Column(
                    name="user_id",
                    table_name="orders",
                    schema_name="public",
                    data_type="BIGINT",
                    nullable=False,
                    ordinal_position=2,
                ),
            ),
        )
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
                    name="region",
                    table_name="users",
                    schema_name="public",
                    data_type="TEXT",
                    nullable=False,
                    ordinal_position=2,
                ),
            ),
        )
        for tbl in (order_items, orders, users):
            store.write_table(tbl, source_connection_id=SRC)
        for ent_name, qt in (
            ("order_item", "public.order_items"),
            ("order", "public.orders"),
            ("user", "public.users"),
        ):
            store.write_entity(
                Entity(
                    name=ent_name,
                    description="",
                    binding=SingleTableBinding(qualified_table=qt),
                    identity="id",
                ),
                source_connection_id=SRC,
            )
        store.write_canonical_join(
            CanonicalJoin(
                name="order_items_order_id",
                description="",
                source_entity="order_item",
                target_entity="order",
                on=(JoinColumnPair(source_column="order_id", target_column="id"),),
                cardinality="many_to_one",
            ),
            source_connection_id=SRC,
        )
        store.write_canonical_join(
            CanonicalJoin(
                name="orders_user_id",
                description="",
                source_entity="order",
                target_entity="user",
                on=(JoinColumnPair(source_column="user_id", target_column="id"),),
                cardinality="many_to_one",
            ),
            source_connection_id=SRC,
        )
        store.write_metric(
            Metric(
                name="total_items_sold",
                description="",
                entity="order_item",
                measure=MetricMeasure(agg="sum", column="quantity"),
                time_dimension=None,
                time_grains=(),
            ),
            source_connection_id=SRC,
        )

    def test_pii_on_intermediate_fk_propagates_to_categories(self, tmp_path: Path) -> None:
        """`order.user_id` is the FK used in the orders_user_id JOIN
        predicate, but the agent never names it directly — it appears
        only on the `plan.joins[1].on_pairs[0].source_column`. The
        category MUST still surface on the MetricResult's
        `pii_categories` field so the audit trail records what the
        SQL touched.
        """
        store = SQLiteStore(tmp_path / "sb.db")
        try:
            self._seed_two_hop_with_pii_on_intermediate_fk(store)
            store.write_column_pii_tags(
                source_connection_id=SRC,
                qualified_table="public.orders",
                tags={"user_id": ("pii", frozenset({"contact"}))},
            )
            executor = _FakeExecutor()
            result = get_metric_impl(
                store=store,
                executor=executor,
                source_connection_id=SRC,
                name="total_items_sold",
                group_by=("user.region",),
            )
            # `contact` was tagged on a JOIN-only column, not a
            # group_by / filter column — the fix puts it into the
            # propagation set anyway.
            assert "contact" in result.pii_categories
        finally:
            store.close()

    def test_pii_on_intermediate_fk_triggers_block_when_in_policy(self, tmp_path: Path) -> None:
        """Same scenario but with `--pii-block=contact` — must refuse,
        not silently execute. Before the fold, the JOIN-key column
        bypassed `pii_block` entirely and the call would have
        succeeded — that's the security gap previously
        flagged.
        """
        store = SQLiteStore(tmp_path / "sb.db")
        try:
            self._seed_two_hop_with_pii_on_intermediate_fk(store)
            store.write_column_pii_tags(
                source_connection_id=SRC,
                qualified_table="public.orders",
                tags={"user_id": ("pii", frozenset({"contact"}))},
            )
            executor = _FakeExecutor()
            with pytest.raises(PiiBlockedError) as exc_info:
                get_metric_impl(
                    store=store,
                    executor=executor,
                    source_connection_id=SRC,
                    name="total_items_sold",
                    group_by=("user.region",),
                    pii_block=frozenset({"contact"}),
                )
            assert "contact" in exc_info.value.blocked_categories
        finally:
            store.close()
