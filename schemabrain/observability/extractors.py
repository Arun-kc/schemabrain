"""Per-tool `result_summary` extractors.

Each MCP tool returns a `ToolResponse[T]`. The extractor pulls a
small, JSON-safe dict out of the data payload — counts, fingerprints,
match counts — that the tail can show in one line. Tools without a
bespoke extractor fall back to `default_result_extractor`, which
returns `{}`.

Extractors must NEVER raise. If they can't shape a summary, they
return `{}` (the decorator catches anyway, but the extractors are
the first line of defence).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

ResultExtractor = Callable[[Any], dict[str, Any]]


def default_result_extractor(_data: Any) -> dict[str, Any]:
    return {}


def _find_relevant_tables_summary(data: Any) -> dict[str, Any]:
    """Pulls a `{"matches": N}` summary from a `list[TableHit]`.

    `data` is the bare list (the envelope's `data` field), not an object
    with a `.matches` attribute — see `_list_entities_summary` /
    `_find_relevant_entities_summary` for the same pattern. The previous
    `.matches` attribute probe was a copy-paste bug: it always returned
    `{}` because lists have no such attribute. The summary now actually
    fires for the tail render + audit row.
    """
    try:
        if data is None:
            return {}
        return {"matches": len(data)}
    except Exception:
        return {}


def _find_relevant_entities_summary(data: Any) -> dict[str, Any]:
    """Pulls a `{"matches": N}` summary from a `list[EntityHit]`.

    `data` is the bare list (the envelope's `data` field), not an
    object with a `.matches` attribute — see `_list_entities_summary`
    for the same pattern. Returns `{}` on `None` so the audit row and
    OTel span stay clean when the tool short-circuits before producing
    any result.
    """
    try:
        if data is None:
            return {}
        return {"matches": len(data)}
    except Exception:
        return {}


def _describe_table_summary(data: Any) -> dict[str, Any]:
    try:
        columns = getattr(data, "columns", None)
        token_estimate = getattr(data, "token_estimate", None)
        out: dict[str, Any] = {}
        if columns is not None:
            out["columns"] = len(columns)
        if token_estimate is not None:
            out["tokens"] = token_estimate
        return out
    except Exception:
        return {}


def _describe_column_summary(data: Any) -> dict[str, Any]:
    try:
        qname = getattr(data, "qualified_name", None)
        return {"column": qname} if qname else {}
    except Exception:
        return {}


def _suggest_joins_summary(data: Any) -> dict[str, Any]:
    try:
        paths = getattr(data, "paths", None)
        if paths is None:
            return {}
        return {"paths": len(paths)}
    except Exception:
        return {}


def _get_example_queries_summary(data: Any) -> dict[str, Any]:
    try:
        queries = getattr(data, "queries", None)
        if queries is None:
            return {}
        return {"queries": len(queries)}
    except Exception:
        return {}


def _list_entities_summary(data: Any) -> dict[str, Any]:
    try:
        if data is None:
            return {}
        # data is a list of EntitySummary
        return {"entities": len(data)}
    except Exception:
        return {}


def _describe_entity_summary(data: Any) -> dict[str, Any]:
    try:
        name = getattr(data, "name", None)
        columns = getattr(data, "columns", None)
        out: dict[str, Any] = {}
        if name:
            out["entity"] = name
        if columns is not None:
            out["columns"] = len(columns)
        return out
    except Exception:
        return {}


def _resolve_join_summary(data: Any) -> dict[str, Any]:
    try:
        name = getattr(data, "name", None)
        source = getattr(data, "source_entity", None)
        target = getattr(data, "target_entity", None)
        out: dict[str, Any] = {}
        if name:
            out["join"] = name
        if source and target:
            out["from"] = source
            out["to"] = target
        return out
    except Exception:
        return {}


def _get_metric_summary(data: Any) -> dict[str, Any]:
    try:
        rows = getattr(data, "rows", None)
        fingerprint = getattr(data, "fingerprint", None)
        out: dict[str, Any] = {}
        if rows is not None:
            out["rows"] = len(rows)
        if fingerprint:
            out["fingerprint"] = fingerprint
        return out
    except Exception:
        return {}


_REGISTRY: dict[str, ResultExtractor] = {
    "find_relevant_tables": _find_relevant_tables_summary,
    "find_relevant_entities": _find_relevant_entities_summary,
    "describe_table": _describe_table_summary,
    "describe_column": _describe_column_summary,
    "suggest_joins": _suggest_joins_summary,
    "get_example_queries": _get_example_queries_summary,
    "list_entities": _list_entities_summary,
    "describe_entity": _describe_entity_summary,
    "resolve_join": _resolve_join_summary,
    "get_metric": _get_metric_summary,
}


def get_result_extractor(tool_name: str) -> ResultExtractor:
    return _REGISTRY.get(tool_name, default_result_extractor)
