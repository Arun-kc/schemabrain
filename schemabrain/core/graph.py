"""Domain models for the v15 knowledge-graph projection (PR-16, ADR 0010).

`graph_nodes` / `graph_edges` are a denormalised, persisted read-model of
`entities` + `canonical_joins`. `GraphNode` / `GraphEdge` are the in-memory
shape the projection builder produces and the store round-trips — kept
deliberately small (one node per entity, one edge per canonical join) so the
dashboard reads a flat graph without re-walking the FK graph per request.

Provenance lives on the EDGE: `edge_origin` is the honest evidence band
(declared FK / log-mined from query logs / inferred) projected from the
canonical join's `inference_method`. A node carries no origin — it is
one-per-entity regardless of how it was derived.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

from schemabrain.core.entity import Group, InferenceMethod

# The honest edge-evidence vocabulary. `declared` = backed by a DB foreign
# key; `log_mined` = recovered from query-log mining; `inferred` = an
# LLM-suggested / manually-authored join with no FK backing. Never any
# phrasing implying the engine inspects agent-authored SQL — the engine is
# credential-less and store-only.
EdgeOrigin = Literal["declared", "log_mined", "inferred"]

_VALID_GROUPS: frozenset[str] = frozenset(get_args(Group))
_VALID_EDGE_ORIGINS: frozenset[str] = frozenset(get_args(EdgeOrigin))

# `canonical_path_rank`: 0 = off the highlighted path (default), 1 = on the
# primary canonical path, 2 = on an alternate path (reserved for PR-17). The
# store CHECK pins the same closed set.
MIN_PATH_RANK = 0
MAX_PATH_RANK = 2


@dataclass(frozen=True)
class GraphNode:
    """One projected entity node: its cosmetic group, a catastrophic-PII
    snapshot, and the cached row-count estimate (`None`, never a fabricated
    0, when the backend can't cheaply estimate)."""

    entity_name: str
    group: Group = "other"
    is_catastrophic: bool = False
    row_count: int | None = None

    def __post_init__(self) -> None:
        if self.group not in _VALID_GROUPS:
            raise ValueError(f"group must be one of {sorted(_VALID_GROUPS)} (got {self.group!r})")


@dataclass(frozen=True)
class GraphEdge:
    """One projected canonical-join edge with honest provenance and its
    canonical-path rank."""

    join_name: str
    source_entity: str
    target_entity: str
    edge_origin: EdgeOrigin = "declared"
    canonical_path_rank: int = MIN_PATH_RANK

    def __post_init__(self) -> None:
        if self.edge_origin not in _VALID_EDGE_ORIGINS:
            raise ValueError(
                f"edge_origin must be one of {sorted(_VALID_EDGE_ORIGINS)} "
                f"(got {self.edge_origin!r})"
            )
        if not MIN_PATH_RANK <= self.canonical_path_rank <= MAX_PATH_RANK:
            raise ValueError(
                f"canonical_path_rank must be in "
                f"[{MIN_PATH_RANK}, {MAX_PATH_RANK}] (got {self.canonical_path_rank!r})"
            )


def edge_origin_from_inference_method(inference_method: InferenceMethod) -> EdgeOrigin:
    """Project a canonical join's `inference_method` onto the edge-evidence band.

    `fk_constraint` → `declared`; `observed_in_query_log` → `log_mined`;
    everything else (`manually_authored`, `llm_suggested`, `dbt_import`) →
    `inferred`. Mirrors the existing `is_log_mined` test on the entities
    surface so the graph and the rest of the dashboard agree on provenance.
    """
    if inference_method == "fk_constraint":
        return "declared"
    if inference_method == "observed_in_query_log":
        return "log_mined"
    return "inferred"
