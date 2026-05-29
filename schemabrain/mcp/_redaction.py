"""Shared redaction helpers for MCP tool implementations.

Centralises the catastrophic-leak column-name scrubbing that ``describe_table``
applies to its column list, so the FK metadata surfaces in ``describe_table``
and ``describe_column`` mirror the same policy. Without this, an agent that
sees ``<redacted_credential_column_1>`` on the column list can still read the
real name out of the FK graph.

The placeholder string used here intentionally drops the per-table ordinal
numbering — FK lists enumerate specific columns of the target table, not the
target's whole column population, so a `<redacted_column>` placeholder is
sufficient to mask the name without requiring a cross-table simulation of
``describe_table``'s numbering loop.
"""

from __future__ import annotations

from schemabrain.core.store_protocol import Store
from schemabrain.pii import PIICategory

_FK_REDACTED_PLACEHOLDER = "<redacted_column>"


def redact_blocked_fk_columns(
    columns: list[str],
    *,
    qualified_table: str,
    store: Store,
    source_connection_id: str,
    effective_block: frozenset[PIICategory],
) -> list[str]:
    """Return ``columns`` with catastrophic-leak names masked to a placeholder.

    Looks up PII tags for the named columns on ``qualified_table`` and
    replaces any whose categories intersect ``effective_block`` with
    ``<redacted_column>``. Non-blocked names pass through unchanged.

    Empty ``columns`` short-circuits without hitting the store.
    """
    if not columns:
        return columns
    tags = store.get_column_pii_tags(
        source_connection_id=source_connection_id,
        qualified_table=qualified_table,
        columns=columns,
    )
    result: list[str] = []
    for col in columns:
        _, categories = tags.get(col, ("public", frozenset()))
        if categories & effective_block:
            result.append(_FK_REDACTED_PLACEHOLDER)
        else:
            result.append(col)
    return result
