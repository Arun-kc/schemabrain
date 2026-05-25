"""Focused tests for `schemabrain.inspect.render` covering branches the
CLI tests don't naturally exercise.

The CLI tests verify the happy path (rendered output appears, exit
codes correct). These tests cover the edge-case render branches:

  - Empty columns ("bound table not indexed" hint)
  - PII tags rendering with category list
  - Cardinality omitted (renders `?` placeholder)
  - Anchored metric with time_dimension + grains (vs non-temporal)
"""

from __future__ import annotations

import io
from collections.abc import Callable
from typing import Any

from rich.console import Console

from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.join import CanonicalJoin, JoinColumnPair
from schemabrain.core.metric import Metric, MetricMeasure
from schemabrain.inspect.engine import (
    AnchoredMetric,
    EntityColumnDetail,
    EntityDetail,
    JoinDetail,
    MetricDetail,
    RelatedEntity,
    StoreSummary,
)
from schemabrain.inspect.render import (
    render_entity_detail,
    render_join_detail,
    render_metric_detail,
    render_summary,
)


def _capture(callable_: Callable[..., None], *args: Any, **kwargs: Any) -> str:
    """Render to an in-memory Console + return the captured string.

    Mirrors `capsys` but works at the rich-Console level directly so
    we don't have to route through stderr.
    """
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=120)
    callable_(*args, console=console, **kwargs)
    return buf.getvalue()


def _entity() -> Entity:
    return Entity(
        name="customer",
        description="A registered user.",
        binding=SingleTableBinding(qualified_table="public.users"),
        identity="id",
    )


class TestRenderSummary:
    def test_summary_with_only_entities_skips_metric_join_sections(self) -> None:
        summary = StoreSummary(
            table_count=1,
            column_count=2,
            entity_count=1,
            metric_count=0,
            join_count=0,
            entity_names=("customer",),
            metric_names=(),
            join_names=(),
        )
        out = _capture(render_summary, summary)
        # The summary tree adds a branch per non-empty category. With
        # only entities present, the "Entities" branch is the sole
        # subtree of "Definitions"; the empty Metrics/Joins branches
        # are omitted entirely (not rendered as empty headings).
        assert "Entities" in out
        assert "customer" in out
        assert "Metrics" not in out
        assert "Joins" not in out

    def test_summary_with_only_metrics_skips_entity_join_sections(self) -> None:
        summary = StoreSummary(
            table_count=0,
            column_count=0,
            entity_count=0,
            metric_count=1,
            join_count=0,
            entity_names=(),
            metric_names=("retention_rate",),
            join_names=(),
        )
        out = _capture(render_summary, summary)
        assert "Metrics" in out
        assert "retention_rate" in out
        assert "Entities" not in out
        assert "Joins" not in out

    def test_summary_with_column_count_none_renders_em_dash(self) -> None:
        # Cross-source sentinel — render shows "—" instead of "0
        # columns" so the operator can distinguish "unknown" from
        # "no columns indexed".
        summary = StoreSummary(
            table_count=7,
            column_count=None,
            entity_count=2,
            metric_count=0,
            join_count=0,
            entity_names=("customer", "order"),
            metric_names=(),
            join_names=(),
        )
        out = _capture(render_summary, summary)
        assert "—" in out
        assert "use --source to count" in out
        # Plain "0 columns" must NOT appear in the cross-source case.
        assert "0 columns" not in out

    def test_summary_with_multiple_source_ids_renders_warning_banner(self) -> None:
        # Operator-reported issue: when the store carries data from
        # more than one source-id (typically: orphan rows from an
        # older schemabrain version), the renderer surfaces a banner
        # so the operator can diagnose the duplication. Without the
        # banner, the deduped tree LOOKS identical to a single-source
        # store of the same shape — silent data integrity hazard.
        summary = StoreSummary(
            table_count=7,
            column_count=None,
            entity_count=6,
            metric_count=10,
            join_count=5,
            entity_names=("address", "category", "order", "order_item", "product", "user"),
            metric_names=("total_revenue",),
            join_names=("customer_orders",),
            source_connection_ids=("src_new_abc", "src_old_xyz"),
        )
        out = _capture(render_summary, summary)
        # Yellow warning glyph + count + remediation hint must surface.
        assert "⚠" in out
        assert "2 source connections" in out
        assert "--source" in out
        # The banner explicitly mentions the orphan-data recovery path
        # so an operator hitting this for the first time isn't left
        # guessing.
        assert "rm" in out or "delete" in out.lower()

    def test_summary_with_single_source_id_skips_banner(self) -> None:
        # A scoped build (filter is a real source-id) sets a single
        # source-id in the tuple → the banner must NOT render.
        summary = StoreSummary(
            table_count=2,
            column_count=8,
            entity_count=2,
            metric_count=1,
            join_count=1,
            entity_names=("customer", "order"),
            metric_names=("total_revenue",),
            join_names=("customer_orders",),
            source_connection_ids=("src_only_one",),
        )
        out = _capture(render_summary, summary)
        # The banner's distinctive substring must be absent.
        assert "source connections" not in out

    def test_summary_with_empty_source_id_tuple_skips_banner(self) -> None:
        # Empty store: source-id tuple is `()`. Banner must stay
        # off — there's nothing to warn about. Defends the default
        # field value (`()`) against accidental >1-length futures.
        summary = StoreSummary(
            table_count=0,
            column_count=None,
            entity_count=0,
            metric_count=0,
            join_count=0,
            entity_names=(),
            metric_names=(),
            join_names=(),
            source_connection_ids=(),
        )
        out = _capture(render_summary, summary)
        assert "source connections" not in out


class TestRenderEntityDetailEdgeCases:
    def test_empty_columns_renders_not_indexed_hint(self) -> None:
        detail = EntityDetail(
            entity=_entity(),
            columns=(),
            related_entities=(),
            anchored_metrics=(),
        )
        out = _capture(render_entity_detail, detail)
        assert "bound table not indexed" in out

    def test_column_with_pii_categories_renders_categories(self) -> None:
        detail = EntityDetail(
            entity=_entity(),
            columns=(
                EntityColumnDetail(
                    name="email",
                    data_type="text",
                    nullable=False,
                    is_primary_key=False,
                    is_identity=False,
                    pii_sensitivity="pii",
                    pii_categories=("contact", "online_identifier"),
                ),
            ),
            related_entities=(),
            anchored_metrics=(),
        )
        out = _capture(render_entity_detail, detail)
        # Both categories surface in the parenthetical.
        assert "contact" in out
        assert "online_identifier" in out

    def test_nullable_column_renders_nullable_marker(self) -> None:
        detail = EntityDetail(
            entity=_entity(),
            columns=(
                EntityColumnDetail(
                    name="middle_name",
                    data_type="text",
                    nullable=True,
                    is_primary_key=False,
                    is_identity=False,
                    pii_sensitivity="public",
                    pii_categories=(),
                ),
            ),
            related_entities=(),
            anchored_metrics=(),
        )
        out = _capture(render_entity_detail, detail)
        assert "nullable" in out

    def test_related_entity_without_cardinality_renders_question_mark(self) -> None:
        detail = EntityDetail(
            entity=_entity(),
            columns=(
                EntityColumnDetail(
                    name="id",
                    data_type="bigint",
                    nullable=False,
                    is_primary_key=True,
                    is_identity=True,
                    pii_sensitivity="public",
                    pii_categories=(),
                ),
            ),
            related_entities=(
                RelatedEntity(
                    name="order",
                    direction="outgoing",
                    join_name="customer_orders",
                    on=(("id", "user_id"),),
                    cardinality=None,
                ),
            ),
            anchored_metrics=(),
        )
        out = _capture(render_entity_detail, detail)
        # Cardinality `None` renders as `?` placeholder.
        assert "?" in out
        assert "via" in out
        assert "customer_orders" in out

    def test_entity_drill_renders_trust_line(self) -> None:
        """O4-P1: `inspect <entity>` must render the 2D trust line at
        the bottom of the drill view, symmetric with `inspect <metric>`
        and `inspect <join>`. Without it operators got no surface
        signal for whether the entity was LLM-suggested vs FK-derived
        vs hand-authored even when the data was present.
        """
        entity = Entity(
            name="customer",
            description="A buyer.",
            binding=SingleTableBinding(qualified_table="public.users"),
            identity="id",
            origin="suggested",
            inference_method="llm_suggested",
            validation_state="applied",
        )
        detail = EntityDetail(
            entity=entity,
            columns=(
                EntityColumnDetail(
                    name="id",
                    data_type="bigint",
                    nullable=False,
                    is_primary_key=True,
                    is_identity=True,
                    pii_sensitivity="public",
                    pii_categories=(),
                ),
            ),
            related_entities=(),
            anchored_metrics=(),
        )
        out = _capture(render_entity_detail, detail)
        # Single Trust line carrying inference_method, validation_state,
        # and the derived bucket — same shape as metric + join drills.
        assert "Trust:" in out
        assert "llm_suggested" in out
        assert "applied" in out
        # `derive_confidence("llm_suggested", "applied")` → MEDIUM per
        # charter v1.2 — the bucket the MCP envelope would report.
        assert "(MEDIUM)" in out

    def test_entity_without_description_skips_description_line(self) -> None:
        # Empty `description` field — the renderer skips the
        # "Description:" line entirely rather than rendering an
        # empty value.
        entity = Entity(
            name="customer",
            description="",
            binding=SingleTableBinding(qualified_table="public.users"),
            identity="id",
        )
        detail = EntityDetail(
            entity=entity,
            columns=(
                EntityColumnDetail(
                    name="id",
                    data_type="bigint",
                    nullable=False,
                    is_primary_key=True,
                    is_identity=True,
                    pii_sensitivity="public",
                    pii_categories=(),
                ),
            ),
            related_entities=(),
            anchored_metrics=(),
        )
        out = _capture(render_entity_detail, detail)
        assert "Description:" not in out
        # Design brand line still names the bound table.
        assert "public.users" in out

    def test_bridge_edge_never_truncates_long_join_name(self) -> None:
        """Bridge join names (`<a>_<b>_via_<junction>`) are typically
        long and used to ellipsis-truncate on narrow terminals. The
        `overflow="fold"` setting on the `On` column must let the name
        wrap across rows instead of dropping characters.
        """
        buf = io.StringIO()
        # Narrow width forces Rich to size columns aggressively, which
        # is the exact scenario where ellipsis truncation kicked in.
        console = Console(file=buf, force_terminal=False, width=70)
        long_bridge = "category_film_via_film_category_with_extras"
        detail = EntityDetail(
            entity=_entity(),
            columns=(
                EntityColumnDetail(
                    name="id",
                    data_type="bigint",
                    nullable=False,
                    is_primary_key=True,
                    is_identity=True,
                    pii_sensitivity="public",
                    pii_categories=(),
                ),
            ),
            related_entities=(
                RelatedEntity(
                    name="category",
                    direction="outgoing",
                    join_name=long_bridge,
                    on=(("id", "film_id"),),
                    cardinality="many_to_many",
                    via_junction="film_category",
                ),
            ),
            anchored_metrics=(),
        )
        render_entity_detail(detail, console=console)
        out = buf.getvalue()
        # The full bridge name must survive; rendering can fold across
        # lines but must never replace characters with an ellipsis.
        normalized = out.replace("\n", "").replace(" ", "")
        assert long_bridge in normalized
        assert "…" not in out

    def test_anchored_metric_without_time_dimension_renders_non_temporal(
        self,
    ) -> None:
        detail = EntityDetail(
            entity=_entity(),
            columns=(
                EntityColumnDetail(
                    name="id",
                    data_type="bigint",
                    nullable=False,
                    is_primary_key=True,
                    is_identity=True,
                    pii_sensitivity="public",
                    pii_categories=(),
                ),
            ),
            related_entities=(),
            anchored_metrics=(
                AnchoredMetric(
                    name="customer_count",
                    description="",
                    agg="count",
                    column="id",
                    expression=None,
                    time_dimension=None,
                    time_grains=(),
                ),
            ),
        )
        out = _capture(render_entity_detail, detail)
        assert "customer_count" in out
        assert "non-temporal" in out

    def test_composite_expression_metric_renders_expression_body(self) -> None:
        # When `column` is None and `expression` is populated, the
        # renderer should show the expression inside the agg call
        # rather than `sum(None)`.
        detail = EntityDetail(
            entity=Entity(
                name="order_item",
                description="",
                binding=SingleTableBinding(qualified_table="public.order_items"),
                identity="id",
            ),
            columns=(
                EntityColumnDetail(
                    name="id",
                    data_type="bigint",
                    nullable=False,
                    is_primary_key=True,
                    is_identity=True,
                    pii_sensitivity="public",
                    pii_categories=(),
                ),
            ),
            related_entities=(),
            anchored_metrics=(
                AnchoredMetric(
                    name="line_revenue",
                    description="",
                    agg="sum",
                    column=None,
                    expression="unit_price_cents * quantity",
                    time_dimension=None,
                    time_grains=(),
                ),
            ),
        )
        out = _capture(render_entity_detail, detail)
        assert "line_revenue" in out
        assert "sum(unit_price_cents * quantity)" in out
        # The string `None` must never appear inside the agg cell.
        assert "sum(None)" not in out


# ---------------------------------------------------------------------------
# Design surface — brand line + summary panels (PR #7+)
# ---------------------------------------------------------------------------


class TestDesignBrandLineSummary:
    """Pins the ``◆ store · <path>`` brand line + 3 compact panels."""

    def _empty_but_populated_summary(self) -> StoreSummary:
        return StoreSummary(
            table_count=7,
            column_count=30,
            entity_count=2,
            metric_count=1,
            join_count=1,
            entity_names=("customer", "order"),
            metric_names=("revenue",),
            join_names=("customer_orders",),
        )

    def test_brand_glyph_appears_in_summary(self) -> None:
        out = _capture(render_summary, self._empty_but_populated_summary())
        assert "◆" in out

    def test_brand_line_includes_store_path(self) -> None:
        out = _capture(
            render_summary,
            self._empty_but_populated_summary(),
            store_path="/tmp/test/store.db",
        )
        assert "/tmp/test/store.db" in out

    def test_brand_line_collapses_when_store_path_missing(self) -> None:
        out = _capture(render_summary, self._empty_but_populated_summary())
        # Brand line still emits the ◆ store header; just no path
        # suffix when store_path is None.
        assert "◆ store" in out

    def test_old_header_no_longer_renders(self) -> None:
        # Regression guard: the pre-PR-#7 ``SchemaBrain inspect``
        # plain-text header must not return on a future revert.
        out = _capture(render_summary, self._empty_but_populated_summary())
        assert "SchemaBrain inspect" not in out

    def test_summary_panel_entities_count_renders(self) -> None:
        out = _capture(render_summary, self._empty_but_populated_summary())
        # Each panel header shows ``label · N``. The 2-entity summary
        # renders ``entities · 2`` in the panel chrome.
        assert "entities" in out
        assert "customer" in out
        assert "order" in out

    def test_summary_renders_all_three_panels_even_when_empty(self) -> None:
        # Entities-only summary: ALL three panels still render —
        # empty metrics/joins panels show ``(none yet)`` body so
        # the 3-column grid stays balanced (matches the design's
        # mock at ``cli/operator.jsx:66-74``). The "teach the
        # operator what they don't have yet" affordance — folded
        # in PR #7's round-2 review per UX feedback.
        summary = StoreSummary(
            table_count=1,
            column_count=2,
            entity_count=1,
            metric_count=0,
            join_count=0,
            entity_names=("customer",),
            metric_names=(),
            join_names=(),
        )
        out = _capture(render_summary, summary)
        # All three panel border-tops render.
        assert out.count("╭") == 3
        assert out.count("╰") == 3
        # Empty categories show the ``(none yet)`` body.
        assert "(none yet)" in out

    def test_summary_panel_truncates_long_lists(self) -> None:
        many_entities = tuple(f"e{i}" for i in range(20))
        summary = StoreSummary(
            table_count=1,
            column_count=2,
            entity_count=20,
            metric_count=0,
            join_count=0,
            entity_names=many_entities,
            metric_names=(),
            join_names=(),
        )
        out = _capture(render_summary, summary)
        # First 12 names visible; the rest collapse to "(N more)".
        assert "e0" in out
        assert "e11" in out
        # The "more" trailer surfaces the omitted count.
        assert "8 more" in out


class TestDesignBrandLineDrill:
    """Pins the ``◆ <qualified_table>`` drill brand line."""

    def test_brand_glyph_appears(self) -> None:
        detail = EntityDetail(
            entity=_entity(),
            columns=(),
            related_entities=(),
            anchored_metrics=(),
        )
        out = _capture(render_entity_detail, detail)
        assert "◆" in out

    def test_brand_line_names_qualified_table(self) -> None:
        detail = EntityDetail(
            entity=_entity(),
            columns=(),
            related_entities=(),
            anchored_metrics=(),
        )
        out = _capture(render_entity_detail, detail)
        assert "public.users" in out

    def test_brand_line_carries_entity_tag(self) -> None:
        detail = EntityDetail(
            entity=_entity(),
            columns=(),
            related_entities=(),
            anchored_metrics=(),
        )
        out = _capture(render_entity_detail, detail)
        # ``entity:<name>`` tag is the design's per-row signature.
        assert "entity:customer" in out

    def test_brand_line_carries_binding_identity(self) -> None:
        detail = EntityDetail(
            entity=_entity(),
            columns=(),
            related_entities=(),
            anchored_metrics=(),
        )
        out = _capture(render_entity_detail, detail)
        assert "binding id" in out

    def test_old_entity_header_no_longer_renders(self) -> None:
        # Regression guard: the pre-PR-#7 ``Entity: <name>`` +
        # dashed-rule header must not return.
        detail = EntityDetail(
            entity=_entity(),
            columns=(),
            related_entities=(),
            anchored_metrics=(),
        )
        out = _capture(render_entity_detail, detail)
        assert "Entity: customer" not in out


class TestEmptyStoreHint:
    def test_empty_store_renders_hint_after_brand_line(self) -> None:
        # A fresh store with zero definitions of any kind shows
        # the "Run entities suggest/apply" hint at the bottom of
        # the brand-line + counts header.
        summary = StoreSummary(
            table_count=0,
            column_count=0,
            entity_count=0,
            metric_count=0,
            join_count=0,
            entity_names=(),
            metric_names=(),
            join_names=(),
        )
        out = _capture(render_summary, summary)
        assert "◆ store" in out
        assert "No semantic-layer definitions in the store yet" in out
        assert "entities suggest" in out


class TestDiscoveryBlock:
    """Day-one UX: every `inspect` exit surface must show the
    `doctor` / `check` / `tail` discovery links, regardless of
    whether the store is populated. The empty-state branch is
    where these links matter MOST — a new operator who just
    ran `init` without an API key has zero definitions to
    drill into and needs to know what commands exist next."""

    def test_empty_store_renders_discovery_block(self) -> None:
        # Regression coverage: empty-state branch was early-returning
        # before the discovery block; new operators saw the "no
        # definitions yet" hint and nothing else.
        summary = StoreSummary(
            table_count=7,
            column_count=30,
            entity_count=0,
            metric_count=0,
            join_count=0,
            entity_names=(),
            metric_names=(),
            join_names=(),
        )
        out = _capture(render_summary, summary)
        assert "schemabrain doctor" in out
        assert "schemabrain check" in out
        assert "schemabrain tail --follow" in out

    def test_populated_store_renders_discovery_block(self) -> None:
        # Discovery block must still appear in the populated-store
        # branch — moved into a helper but the contract is
        # unchanged from the original render_summary.
        summary = StoreSummary(
            table_count=7,
            column_count=30,
            entity_count=1,
            metric_count=0,
            join_count=0,
            entity_names=("customer",),
            metric_names=(),
            join_names=(),
        )
        out = _capture(render_summary, summary)
        assert "schemabrain doctor" in out
        assert "schemabrain check" in out
        assert "schemabrain tail --follow" in out


class TestNonManualEntityOrigin:
    def test_origin_suggested_renders_in_brand_line(self) -> None:
        # When an entity was suggested by the LLM (origin="llm") or
        # imported (origin="dbt"), the brand line surfaces that as
        # a final ``· origin <kind>`` segment so the operator knows
        # the provenance. Manual-origin entities omit the segment.
        suggested = Entity(
            name="customer",
            description="Suggested entity.",
            binding=SingleTableBinding(qualified_table="public.users"),
            identity="id",
            origin="suggested",
        )
        detail = EntityDetail(
            entity=suggested,
            columns=(),
            related_entities=(),
            anchored_metrics=(),
        )
        out = _capture(render_entity_detail, detail)
        assert "origin suggested" in out


def _anchor_entity() -> Entity:
    return Entity(
        name="order",
        description="A purchase placed by one customer.",
        binding=SingleTableBinding(qualified_table="public.orders"),
        identity="id",
    )


class TestRenderMetricDetail:
    """`render_metric_detail` branches not exercised by the CLI happy-path
    fixture (which uses origin=manual, no description, no time_dimension).
    """

    def test_orphaned_metric_renders_orphan_marker(self) -> None:
        # A metric whose anchor entity was deleted out from under it:
        # the renderer must surface the orphan marker rather than
        # silently omitting it.
        metric = Metric(
            name="total_revenue",
            description="",
            entity="order",
            measure=MetricMeasure(agg="sum", column="total_cents"),
            time_dimension=None,
            time_grains=(),
        )
        detail = MetricDetail(metric=metric, anchor=None)
        out = _capture(render_metric_detail, detail)
        assert "(orphaned)" in out

    def test_metric_origin_suggested_renders_origin_segment(self) -> None:
        # Metric authored by the LLM-suggest pipeline carries
        # origin="suggested"; the brand line surfaces the provenance.
        metric = Metric(
            name="total_revenue",
            description="",
            entity="order",
            measure=MetricMeasure(agg="sum", column="total_cents"),
            time_dimension=None,
            time_grains=(),
            origin="suggested",
        )
        detail = MetricDetail(metric=metric, anchor=_anchor_entity())
        out = _capture(render_metric_detail, detail)
        assert "origin suggested" in out

    def test_metric_with_time_dimension_renders_grains(self) -> None:
        # Temporal metric renders the time_dimension + ordered grain
        # list; non-temporal metrics render "non-temporal" instead.
        metric = Metric(
            name="total_revenue",
            description="Sum of order subtotals.",
            entity="order",
            measure=MetricMeasure(agg="sum", column="total_cents"),
            time_dimension="order.placed_at",
            time_grains=("day", "week", "month"),
        )
        detail = MetricDetail(metric=metric, anchor=_anchor_entity())
        out = _capture(render_metric_detail, detail)
        assert "Description:" in out
        assert "order.placed_at" in out
        assert "day, week, month" in out


class TestRenderJoinDetail:
    """`render_join_detail` branches not exercised by the CLI happy-path
    fixture (which uses origin=manual, empty description, and provides
    both `source` + `target` entities).
    """

    def test_join_origin_suggested_renders_origin_segment(self) -> None:
        # Joins surfaced by `joins suggest` land with origin="suggested";
        # the brand line surfaces the provenance so the operator knows
        # to hand-verify before depending on the chain.
        join = CanonicalJoin(
            name="customer_orders",
            description="Joins customers to their orders.",
            source_entity="customer",
            target_entity="order",
            on=(JoinColumnPair(source_column="id", target_column="user_id"),),
            cardinality="one_to_many",
            origin="suggested",
        )
        customer = Entity(
            name="customer",
            description="",
            binding=SingleTableBinding(qualified_table="public.users"),
            identity="id",
        )
        order = Entity(
            name="order",
            description="",
            binding=SingleTableBinding(qualified_table="public.orders"),
            identity="id",
        )
        detail = JoinDetail(join=join, source=customer, target=order)
        out = _capture(render_join_detail, detail)
        # Origin segment + description line both render.
        assert "origin suggested" in out
        assert "Description:" in out
        # Tables: line renders only when BOTH source and target are
        # resolvable — pinning the branch.
        assert "Tables:" in out
        assert "public.users" in out
        assert "public.orders" in out

    def test_join_without_cardinality_skips_cardinality_segment(self) -> None:
        # Joins authored before the cardinality column shipped carry
        # cardinality=None; the brand line must skip the segment
        # rather than render `cardinality None`.
        join = CanonicalJoin(
            name="customer_orders",
            description="",
            source_entity="customer",
            target_entity="order",
            on=(JoinColumnPair(source_column="id", target_column="user_id"),),
            cardinality=None,
        )
        detail = JoinDetail(join=join, source=None, target=None)
        out = _capture(render_join_detail, detail)
        # Brand line carries the join name + entity arrow but no
        # `cardinality ...` segment.
        assert "customer_orders" in out
        assert "customer → order" in out
        assert "cardinality" not in out
        # When EITHER end is unresolved, the Tables: line is omitted
        # so the operator sees only what the store can actually
        # reconstruct.
        assert "Tables:" not in out
