"""MCP tool implementation: list_metrics.

Surfaces the metric surface as a flat list of summaries. Returns
one `MetricSummary` per declared metric, ordered alphabetically by
name (the store provides the ordering so the wire shape is
deterministic across repeat calls).

Mirrors `list_entities_impl` — same shape, same ordering contract,
same lean-summary philosophy: the agent gets enough to pick a
metric to feed to `get_metric` (name + aggregation + time grains)
without paying the full Metric serialisation cost.

Closes the day-one discovery gap surfaced by the 2026-05-21 manual
smoke: ten metrics lived in the store, the agent had no path to
enumerate them. The previous `get_metric` description routed agents
to a CLI subcommand for discovery — agents have no terminal, so
every ad-hoc question dead-ended.
"""

from __future__ import annotations

from schemabrain.core.store_protocol import Store
from schemabrain.mcp.shapes import MetricSummary


def list_metrics_impl(
    *,
    store: Store,
    source_connection_id: str,
) -> list[MetricSummary]:
    """Return all declared metrics for `source_connection_id`.

    Ordering is alphabetical by `name`, inherited from the store —
    callers (MCP envelope wrapper, CLI list view) depend on this so
    they don't have to re-sort.

    `time_dimension` and `time_grains` are passed through verbatim:
    `time_dimension is None` and `time_grains is ()` is the
    non-temporal case; both set together is the temporal case. The
    parallel-emptiness invariant is enforced at the `Metric`
    construction site, not re-checked here.
    """
    metrics = store.list_metrics(source_connection_id=source_connection_id)
    return [
        MetricSummary(
            name=m.name,
            description=m.description,
            entity=m.entity,
            aggregation=m.measure.agg,
            measure_column=m.measure.column,
            measure_expression=m.measure.expression,
            time_dimension=m.time_dimension,
            time_grains=m.time_grains,
            origin=m.origin,
        )
        for m in metrics
    ]
