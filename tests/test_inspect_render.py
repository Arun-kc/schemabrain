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
from schemabrain.inspect.engine import (
    AnchoredMetric,
    EntityColumnDetail,
    EntityDetail,
    RelatedEntity,
    StoreSummary,
)
from schemabrain.inspect.render import render_entity_detail, render_summary


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
                    time_dimension=None,
                    time_grains=(),
                ),
            ),
        )
        out = _capture(render_entity_detail, detail)
        assert "customer_count" in out
        assert "non-temporal" in out


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
        # Regression guard: the pre-PR-#7 ``Schema Brain inspect``
        # plain-text header must not return on a future revert.
        out = _capture(render_summary, self._empty_but_populated_summary())
        assert "Schema Brain inspect" not in out

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
