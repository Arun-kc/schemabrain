"""Render a `DictionaryModel` to GitHub-flavoured, Mintlify-safe Markdown.

Pure: walks the model, emits one H1 page + a section per entity (header,
columns table, joins table, metrics table). Output is deterministic and
ends in exactly one trailing newline so `cli._write_yaml_body` writes it
verbatim and the byte-exact golden stays stable.

Mintlify-safety rules enforced here:
  - no raw ``|`` inside a table cell (multi-value cells use `` / ``
    separators; free-text cells escape ``|`` -> ``\\|``);
  - literal ``$`` is backslash-escaped (dodges MDX inline-math);
  - identifiers/types/ON-clauses render inside single backtick spans
    (they contain no pipe);
  - one H1 per page.
"""

from __future__ import annotations

from schemabrain.datadict.model import (
    DictColumn,
    DictEntity,
    DictionaryModel,
    DictJoin,
    DictMetric,
)
from schemabrain.datadict.render_common import (
    category_label,
    is_catastrophic_category,
    sensitivity_label,
)

_EM_DASH = "—"

_COLUMN_HEADER = (
    "| Column | Type | Null | PK | Identity | Sensitivity | PII categories | Description |"
)
_COLUMN_DIVIDER = "| --- | --- | --- | --- | --- | --- | --- | --- |"
_JOIN_HEADER = "| Join | On | Cardinality | Provenance | Description |"
_JOIN_DIVIDER = "| --- | --- | --- | --- | --- |"
_METRIC_HEADER = "| Metric | Aggregation | Measure | Time dimension | Grains | Description |"
_METRIC_DIVIDER = "| --- | --- | --- | --- | --- | --- |"


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def _escape_prose(text: str) -> str:
    """Escape a literal ``$`` in prose so MDX doesn't read it as math."""
    return text.replace("$", "\\$")


def _cell(text: str) -> str:
    """Escape free text for a Markdown table cell (no raw pipe / newline / $)."""
    return text.replace("\n", " ").replace("|", "\\|").replace("$", "\\$")


def _desc_cell(text: str | None) -> str:
    return _cell(text) if text else _EM_DASH


def _code(value: str) -> str:
    """A backtick code span for a single non-pipe identifier/type/clause."""
    return f"`{value}`"


def _pii_cell(categories: tuple[str, ...]) -> str:
    if not categories:
        return _EM_DASH
    pieces: list[str] = []
    for category in categories:
        piece = _code(category_label(category))  # type: ignore[arg-type]
        if is_catastrophic_category(category):  # type: ignore[arg-type]
            piece += " (catastrophic)"
        pieces.append(piece)
    return " / ".join(pieces)


def _columns_table(columns: tuple[DictColumn, ...], *, heading: str) -> list[str]:
    lines = ["", f"{heading} Columns", "", _COLUMN_HEADER, _COLUMN_DIVIDER]
    for col in columns:
        lines.append(
            f"| {_code(col.name)} | {_code(col.data_type)} | {_yes_no(col.nullable)} | "
            f"{_yes_no(col.is_primary_key)} | {_yes_no(col.is_identity)} | "
            f"{sensitivity_label(col.pii_sensitivity)} | {_pii_cell(col.pii_categories)} | "
            f"{_desc_cell(col.description)} |"
        )
    return lines


def _joins_table(joins: tuple[DictJoin, ...], *, heading: str) -> list[str]:
    lines = ["", f"{heading} Joins", "", _JOIN_HEADER, _JOIN_DIVIDER]
    for join in joins:
        cardinality = join.cardinality if join.cardinality else _EM_DASH
        lines.append(
            f"| {_code(join.name)} | {_code(join.on_clause)} | {cardinality} | "
            f"{join.provenance} | {_desc_cell(join.description)} |"
        )
    return lines


def _metrics_table(metrics: tuple[DictMetric, ...], *, heading: str) -> list[str]:
    lines = ["", f"{heading} Metrics", "", _METRIC_HEADER, _METRIC_DIVIDER]
    for metric in metrics:
        time_dimension = _code(metric.time_dimension) if metric.time_dimension else _EM_DASH
        grains = ", ".join(metric.time_grains) if metric.time_grains else _EM_DASH
        lines.append(
            f"| {_code(metric.name)} | {metric.agg} | {_code(metric.measure)} | "
            f"{time_dimension} | {grains} | {_desc_cell(metric.description)} |"
        )
    return lines


def _entity_block(entity: DictEntity, *, level: int) -> list[str]:
    """Render one entity. `level` is the entity heading depth (## or ###).

    Sub-section tables (Columns / Joins / Metrics) nest one level deeper,
    so a multi-source document keeps a correct heading tree
    (`## Source` -> `### entity` -> `#### Columns`).
    """
    entity_prefix = "#" * level
    sub_prefix = "#" * (level + 1)
    lines = [
        "",
        f"{entity_prefix} {entity.name}",
        "",
        _escape_prose(entity.description) if entity.description else _EM_DASH,
        "",
        f"- **Table:** {_code(entity.qualified_table)}",
        f"- **Identity:** {_code(entity.identity)}",
        f"- **Group:** {entity.group}",
    ]
    lines += _columns_table(entity.columns, heading=sub_prefix)
    if entity.joins:
        lines += _joins_table(entity.joins, heading=sub_prefix)
    if entity.metrics:
        lines += _metrics_table(entity.metrics, heading=sub_prefix)
    return lines


def render_markdown(model: DictionaryModel) -> str:
    """Render the full dictionary as a single Markdown document."""
    lines: list[str] = [
        "# Data dictionary",
        "",
        f"Generated from the local SchemaBrain store (schema version "
        f"{model.schema_version}). Every indexed table, column, type, PII "
        f"classification, semantic join, and metric.",
    ]
    multi_source = len(model.sources) > 1
    # With one source, entities head the page at ##. With several, each
    # source gets a ## divider and its entities nest at ###.
    entity_level = 3 if multi_source else 2
    for source in model.sources:
        if multi_source:
            lines += ["", f"## Source: {_code(source.source_connection_id)}"]
        for entity in source.entities:
            lines += _entity_block(entity, level=entity_level)
    return "\n".join(lines) + "\n"
