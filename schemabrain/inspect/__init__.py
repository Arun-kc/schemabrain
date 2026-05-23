"""`schemabrain inspect` — store-only schema browser.

Renders the persisted semantic-layer surface (entities, metrics,
canonical joins) plus the indexed physical schema (tables, columns,
PII tags) without ever calling the live source database. The
debugging counterpart to the agent-facing MCP tools — same view the
agent sees, no agent needed.

Two layers:

  - `engine` — pure data builders that turn `Store` reads into frozen
    `StoreSummary` and `EntityDetail` dataclasses. No I/O beyond the
    supplied Store. No `rich`.
  - `render` — rich-rendered output that consumes the engine's
    dataclasses. Future surfaces (a hosted web UI, a JSON-only
    plain-output mode) plug in at this seam without touching engine.
"""

from __future__ import annotations

from schemabrain.inspect.engine import (
    AnchoredMetric,
    EntityColumnDetail,
    EntityDetail,
    JoinDetail,
    MetricDetail,
    RelatedEntity,
    StoreSummary,
    build_entity_detail,
    build_join_detail,
    build_metric_detail,
    build_summary,
)
from schemabrain.inspect.render import (
    render_entity_detail,
    render_join_detail,
    render_metric_detail,
    render_summary,
)

__all__ = [
    "AnchoredMetric",
    "EntityColumnDetail",
    "EntityDetail",
    "JoinDetail",
    "MetricDetail",
    "RelatedEntity",
    "StoreSummary",
    "build_entity_detail",
    "build_join_detail",
    "build_metric_detail",
    "build_summary",
    "render_entity_detail",
    "render_join_detail",
    "render_metric_detail",
    "render_summary",
]
