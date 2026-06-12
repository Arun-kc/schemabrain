"""Frozen data-dictionary model — the pure shape rendered to Markdown/HTML.

`build_dictionary` (the aggregator) reads the store and assembles this
tree; the renderers walk it. Every field is sourced from a real store
dataclass (`Entity`, `Column`, `CanonicalJoin`, `Metric`) so the closed
Literal types (`Cardinality`, `AggFunction`, `Sensitivity`,
`PIICategory`) carry through instead of widening to `str`.

Deliberately omitted (no store reader / unhydrated reserved columns at
SCHEMA_VERSION 15, so they would render as all-"—" noise):

  - row counts (`tables.estimated_row_count` is NULL on SQLite)
  - per-column PII confidence band/score
  - `bind_confidence` / `rationale` / `semantic_type` / `meaning`

Add them here once a writer populates them.
"""

from __future__ import annotations

from dataclasses import dataclass

from schemabrain.core.join import Cardinality
from schemabrain.core.metric import AggFunction
from schemabrain.pii import PIICategory, Sensitivity


@dataclass(frozen=True)
class DictColumn:
    """One physical column with its type, identity role, and PII tags."""

    name: str
    data_type: str
    nullable: bool
    is_primary_key: bool
    is_identity: bool  # name == owning entity's identity column
    description: str | None  # None when un-enriched -> renders "—"
    pii_sensitivity: Sensitivity
    pii_categories: tuple[PIICategory, ...]  # ordered by PII_CATEGORIES_ORDERED


@dataclass(frozen=True)
class DictJoin:
    """A canonical join anchored to an entity, with its rendered ON clause."""

    name: str
    description: str  # "" -> renders "—"
    source_entity: str
    target_entity: str
    on_clause: str  # render_on_clause output, catastrophic FK names redacted
    cardinality: Cardinality | None  # None -> renders "—"
    provenance: str  # human label derived from inference_method


@dataclass(frozen=True)
class DictMetric:
    """A named, entity-anchored business measure."""

    name: str
    description: str
    agg: AggFunction
    measure: str  # column name OR expression text
    measure_is_expression: bool
    time_dimension: str | None  # "<entity>.<column>" or None -> "—"
    time_grains: tuple[str, ...]  # canonically sorted TimeGrain values


@dataclass(frozen=True)
class DictEntity:
    """One curated entity: its binding, columns, joins, and metrics."""

    name: str
    description: str
    qualified_table: str  # "schema.table"
    identity: str
    group: str  # v15 coarse grouping (identity|billing|activity|other)
    origin: str
    inference_method: str
    validation_state: str
    columns: tuple[DictColumn, ...]  # ordinal order
    joins: tuple[DictJoin, ...]  # name order; anchored on either endpoint
    metrics: tuple[DictMetric, ...]  # name order; anchored entity == this


@dataclass(frozen=True)
class DictSource:
    """All entities for one indexed source connection."""

    source_connection_id: str
    entities: tuple[DictEntity, ...]  # list_entities order (by name)


@dataclass(frozen=True)
class DictionaryModel:
    """The complete dictionary: every source's curated semantic layer."""

    schema_version: str  # store.SCHEMA_VERSION, echoed in the page header
    sources: tuple[DictSource, ...]  # sorted by source_connection_id
