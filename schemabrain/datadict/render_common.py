"""Shared, pure rendering helpers for the data dictionary.

This module is the SOLE owner of the data-dictionary display contract:

  - `render_on_clause` — the canonical SQL `ON` grammar (double-quoted,
    entity-name-as-alias). It agrees byte-for-byte with the executed-SQL
    emitter (`semantic.compiler.emit`) and the agent-facing join surface
    (`mcp.resolve_join`), the two paste-safe surfaces. The dashboard's
    unquoted JS rendering is the documented outlier and is out of scope
    here.
  - `CATEGORY_LABELS` / `SENSITIVITY_LABELS` — the canonical human labels
    for the closed PII taxonomy. No other label map exists in the
    codebase; this is the single source of truth.
  - `is_catastrophic_category` — the per-category catastrophic-leak test
    both renderers use to decide the "(catastrophic)" marker.

Pure: no store, no I/O, no SQLite import. Imported by both the Markdown
and HTML renderers so the two formats can never disagree on a label, a
catastrophic marker, or an ON clause.
"""

from __future__ import annotations

from schemabrain.pii import (
    CATASTROPHIC_LEAK_CATEGORIES,
    PIICategory,
    Sensitivity,
)

# Canonical human labels for the 12-member PII category taxonomy. Pinned
# to the enum by `test_render_common.test_category_labels_cover_every_pii_category`
# so a future taxonomy addition fails CI until a label is supplied.
CATEGORY_LABELS: dict[PIICategory, str] = {
    "contact": "Contact",
    "financial": "Financial",
    "payment_card": "Payment Card",
    "health": "Health",
    "genetic": "Genetic",
    "biometric": "Biometric",
    "behavioral": "Behavioral",
    "online_identifier": "Online Identifier",
    "credential": "Credential",
    "government_id": "Government ID",
    "location": "Location",
    "demographic_protected": "Protected Demographic",
}

# Canonical labels for the 4-level sensitivity ladder. "pii" renders as
# the uppercase acronym, not the title-cased "Pii".
SENSITIVITY_LABELS: dict[Sensitivity, str] = {
    "public": "Public",
    "internal": "Internal",
    "confidential": "Confidential",
    "pii": "PII",
}


def category_label(category: PIICategory) -> str:
    """Human label for a PII category; the raw token on an unknown value.

    Degrading to the raw token (rather than raising) keeps the renderer
    total — a taxonomy addition shows the bare enum string until the
    label map catches up, instead of crashing the CLI.
    """
    return CATEGORY_LABELS.get(category, category)


def sensitivity_label(sensitivity: Sensitivity) -> str:
    """Human label for a sensitivity level; the raw token on an unknown value."""
    return SENSITIVITY_LABELS.get(sensitivity, sensitivity)


def is_catastrophic_category(category: PIICategory) -> bool:
    """True when this category is in the catastrophic-leak set.

    The single owner of the catastrophic display decision — both
    renderers call this per category to decide the "(catastrophic)"
    marker, so the two formats can never diverge on which categories are
    flagged.
    """
    return category in CATASTROPHIC_LEAK_CATEGORIES


def _dq(identifier: str) -> str:
    """Wrap a bare SQL identifier in ASCII double quotes.

    Identifiers reaching here are already shape-validated upstream
    (`JoinColumnPair` enforces `_IDENT_RE`), so no embedded-quote escaping
    is required — the quotes exist to make reserved words like ``order``
    and ``user`` safe to paste into Postgres.
    """
    return f'"{identifier}"'


def render_on_clause(
    on_pairs: tuple[tuple[str, str], ...],
    *,
    source_alias: str,
    target_alias: str,
) -> str:
    """Render join column pairs as a quoted SQL ``ON`` predicate.

    Each pair is ``(source_column, target_column)``. Single pair ->
    ``"src"."a" = "tgt"."b"``. Composite (N pairs) -> the per-pair
    predicates joined by `` AND ``. Empty -> ``""`` (the aggregator never
    passes an empty tuple, but the helper stays total).

    Column strings are passed verbatim — the aggregator may substitute a
    ``<redacted_column>`` placeholder for a catastrophic-leak FK column
    BEFORE calling this, which is why the input is plain strings rather
    than the shape-validated ``JoinColumnPair`` (the placeholder is not
    identifier-shaped). The aliases are the bare entity names; the grammar
    is byte-identical to `mcp.resolve_join`'s rendering.
    """
    return " AND ".join(
        f"{_dq(source_alias)}.{_dq(source_column)} = {_dq(target_alias)}.{_dq(target_column)}"
        for source_column, target_column in on_pairs
    )
