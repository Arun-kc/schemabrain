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


def visible_allowed_columns(
    allowed_columns: tuple[str, ...],
    *,
    entity_name: str,
    store: Store,
    source_connection_id: str,
    effective_block: frozenset[PIICategory],
) -> tuple[str, ...]:
    """Drop columns whose PII categories intersect ``effective_block``.

    The ``Allowed columns: [...]`` hint on an unknown-column error
    (``unknown_group_by_column`` / ``unknown_filter_column`` /
    ``unknown_measure_column``) tells the agent which columns it MAY
    reference on retry. A blocked column is NOT a referenceable target —
    a group_by / filter / measure on it refuses with ``pii_blocked`` — and
    naming it here re-discloses exactly the name ``describe_*`` hides
    behind a ``<redacted_*>`` placeholder. So blocked names are dropped
    entirely: the hint lists only columns the agent can actually use.

    Resolves ``entity_name`` to its bound table to look up PII tags. The
    entity is always resolvable on these error paths (the metric compiled
    against it); if it somehow isn't, fails closed — drops every name
    rather than risk leaking one.
    """
    if not allowed_columns:
        return allowed_columns
    entity = store.get_entity(entity_name, source_connection_id=source_connection_id)
    if entity is None:  # pragma: no cover — entity is resolved upstream on this path
        return ()
    tags = store.get_column_pii_tags(
        source_connection_id=source_connection_id,
        qualified_table=entity.qualified_table,
        columns=list(allowed_columns),
    )
    return tuple(
        col
        for col in allowed_columns
        if not (tags.get(col, ("public", frozenset()))[1] & effective_block)
    )
