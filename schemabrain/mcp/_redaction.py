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

from schemabrain.core.entity import Entity
from schemabrain.core.join import CanonicalJoin
from schemabrain.core.store_protocol import Store
from schemabrain.pii import CATASTROPHIC_LEAK_CATEGORIES, PIICategory

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


def render_join_on_clause(
    join: CanonicalJoin,
    *,
    store: Store,
    source_connection_id: str,
    entity_by_name: dict[str, Entity],
    effective_block: frozenset[PIICategory] = CATASTROPHIC_LEAK_CATEGORIES,
) -> str:
    """Render a canonical join's ``ON`` clause with catastrophic FK names masked.

    The single shared composition of ``redact_blocked_fk_columns`` (applied to
    BOTH endpoints) and ``render_on_clause`` (the canonical quoted grammar).
    Used by the data dictionary (``datadict.aggregator``) and the dashboard
    entity drilldown (``dashboard.sidecar``) so the two surfaces can never
    render the same join's ON clause differently — same quoting, same
    redaction policy.

    Both endpoints are FK-guaranteed entities (``write_canonical_join``
    enforces it at the SQLite layer), so the ``entity_by_name`` lookups
    always resolve. ``effective_block`` defaults to the catastrophic-leak
    set; callers may pass a wider policy.
    """
    # Imported lazily: `datadict`'s package __init__ eagerly imports the
    # aggregator, which imports THIS module — a module-level import of the
    # (pure) renderer would close that cycle. Deferring to call time breaks
    # it; the cost is negligible (per-join, not a tight loop).
    from schemabrain.datadict.render_common import render_on_clause

    source_entity = entity_by_name[join.source_entity]
    target_entity = entity_by_name[join.target_entity]
    source_columns = redact_blocked_fk_columns(
        [pair.source_column for pair in join.on],
        qualified_table=source_entity.qualified_table,
        store=store,
        source_connection_id=source_connection_id,
        effective_block=effective_block,
    )
    target_columns = redact_blocked_fk_columns(
        [pair.target_column for pair in join.on],
        qualified_table=target_entity.qualified_table,
        store=store,
        source_connection_id=source_connection_id,
        effective_block=effective_block,
    )
    pairs = tuple(zip(source_columns, target_columns, strict=True))
    return render_on_clause(pairs, source_alias=join.source_entity, target_alias=join.target_entity)


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
