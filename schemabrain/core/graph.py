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
from schemabrain.core.join import _VALID_CARDINALITIES, Cardinality

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
    """One projected canonical-join edge with honest provenance, its
    canonical-path rank, and (for declared FK edges only) cardinality.

    `cardinality` is the equi-join shape (`one_to_one` / `one_to_many` /
    `many_to_one` / `many_to_many`), copied from the canonical join. It is
    `None` for `log_mined` and `inferred` edges: the projection writer only
    carries a cardinality when the edge is FK-backed (`declared`), so the
    wire never shows an engine-verified shape for a join that was mined or
    hand/dbt-asserted. `None` also covers declared joins authored before the
    cardinality column existed and FKs whose uniqueness the connector could
    not confirm — never a guess.
    """

    join_name: str
    source_entity: str
    target_entity: str
    edge_origin: EdgeOrigin = "declared"
    canonical_path_rank: int = MIN_PATH_RANK
    cardinality: Cardinality | None = None

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
        if self.cardinality is not None and self.cardinality not in _VALID_CARDINALITIES:
            raise ValueError(
                f"cardinality must be None or one of "
                f"{sorted(_VALID_CARDINALITIES)} (got {self.cardinality!r})"
            )


@dataclass(frozen=True)
class RefusalHotspots:
    """Live refusal tally for the graph's refusal-hotspot overlay (PR-17b).

    Built by aggregating append-only `mcp_audit` rows with `status='refused'`
    by their non-canonical `anchor_entity` column (store schema v17), NOT
    persisted to the projection (refusals change between projection rebuilds,
    so the count must be live or it goes stale immediately).

    `by_entity` maps an entity name → its attributed refusal count; entities
    with zero refusals are simply absent. `unattributed` is the count of
    refused rows the engine could not pin to a single entity (NULL
    `anchor_entity`) — surfaced explicitly rather than dropped so a badge
    total always reconciles with the audit log (a refusal is never silently
    uncounted). Both are honest live reads, never a guess.
    """

    by_entity: dict[str, int]
    unattributed: int


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
