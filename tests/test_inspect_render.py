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
        # Other fields still render.
        assert "Binding:" in out and "public.users" in out

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
