"""Serialise a `DictionaryModel` to a plain JSON-able dict — the wire shape.

The single source of truth for the data-dictionary wire contract, consumed by:

  - ``GET /api/dict`` (the dashboard's Data Dictionary surface and its
    client-side "Export Markdown"), and
  - the committed cross-language fixture
    ``tests/datadict/golden/saas_dictionary.model.json`` (pinned to this
    serialiser by ``test_dictionary_golden`` so the TypeScript port in
    ``web/lib/dict/serialize.ts`` can never silently drift).

Pure: a deterministic dataclass -> dict walk, no I/O, no SQLite import.
Field names mirror the TypeScript ``DictionaryModel`` in
``web/lib/types/dict.ts`` 1:1, so the same model drives the Python
renderer, the TS renderer, and the browse surface from one shape.
"""

from __future__ import annotations

from typing import Any

from schemabrain.datadict.model import (
    DictColumn,
    DictEntity,
    DictionaryModel,
    DictJoin,
    DictMetric,
    DictSource,
)


def _column_json(column: DictColumn) -> dict[str, Any]:
    return {
        "name": column.name,
        "data_type": column.data_type,
        "nullable": column.nullable,
        "is_primary_key": column.is_primary_key,
        "is_identity": column.is_identity,
        "description": column.description,
        "pii_sensitivity": column.pii_sensitivity,
        "pii_categories": list(column.pii_categories),
    }


def _join_json(join: DictJoin) -> dict[str, Any]:
    return {
        "name": join.name,
        "description": join.description,
        "source_entity": join.source_entity,
        "target_entity": join.target_entity,
        "on_clause": join.on_clause,
        "cardinality": join.cardinality,
        "provenance": join.provenance,
    }


def _metric_json(metric: DictMetric) -> dict[str, Any]:
    return {
        "name": metric.name,
        "description": metric.description,
        "agg": metric.agg,
        "measure": metric.measure,
        "measure_is_expression": metric.measure_is_expression,
        "time_dimension": metric.time_dimension,
        "time_grains": list(metric.time_grains),
    }


def _entity_json(entity: DictEntity) -> dict[str, Any]:
    return {
        "name": entity.name,
        "description": entity.description,
        "qualified_table": entity.qualified_table,
        "identity": entity.identity,
        "group": entity.group,
        "columns": [_column_json(column) for column in entity.columns],
        "joins": [_join_json(join) for join in entity.joins],
        "metrics": [_metric_json(metric) for metric in entity.metrics],
    }


def _source_json(source: DictSource) -> dict[str, Any]:
    return {
        "source_connection_id": source.source_connection_id,
        "entities": [_entity_json(entity) for entity in source.entities],
    }


def dictionary_model_to_json(model: DictionaryModel) -> dict[str, Any]:
    """Render the full dictionary model as a JSON-able dict (the wire shape)."""
    return {
        "schema_version": model.schema_version,
        "sources": [_source_json(source) for source in model.sources],
    }
