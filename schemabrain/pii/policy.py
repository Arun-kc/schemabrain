"""Policy — the operator-facing PII enforcement model.

Two parts:

  - **Block set.** The categories the server refuses to surface through
    aggregate queries (`get_metric`'s `--pii-block` policy). Defaults
    to `CATASTROPHIC_LEAK_CATEGORIES` when an operator hasn't set one;
    the empty frozenset is the explicit opt-out (no enforcement).
  - **Column overrides.** Operator-asserted per-column tag overrides
    that supersede the heuristic classifier. Carried in
    `column_pii_tags` rows with `origin='operator'`; the existing
    primary key `(source_connection_id, qualified_table, column_name)`
    means an override REPLACES the heuristic row rather than layering
    over it. This is the surface that resolves false positives like
    `card_number_last4` (heuristic flags `payment_card` from the
    column name; PCI-DSS Q&A says last4 alone isn't sensitive — the
    operator asserts `sensitivity: internal, categories: []`).

The two parts compose: the block set decides which categories to
refuse at query time, and per-column overrides change which categories
a column carries before the block intersection happens.

Wire round-trip lives in `pii.policy_yaml` (parser + emitter).
Store round-trip lands through the existing `write_column_pii_tags` /
`get_column_pii_tags` pair plus a thin policy-row read; the schema
already supports `origin='operator'` (see `core/store.py:654-665`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from schemabrain.pii.categories import (
    PIICategory,
    Sensitivity,
)

# `schema.table.column` form — three identifier-shaped parts joined by
# dots. Matches the Postgres-flavored alphabet used elsewhere in the
# project (`core/entity.py`, `core/metric.py`). Three dots permit
# `schema.table.column` exactly; rejects two-part (`table.column`) and
# four-part (`db.schema.table.column`) shapes.
_QUALIFIED_COLUMN_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_$]*"
    r"\.[A-Za-z_][A-Za-z0-9_$]*"
    r"\.[A-Za-z_][A-Za-z0-9_$]*$"
)


@dataclass(frozen=True)
class ColumnOverride:
    """One operator-asserted PII tag for one column.

    `qualified_column` is `schema.table.column` (three dots, all
    identifier-shaped). `sensitivity` and `categories` are the new
    tag values the operator wants the column to carry — they
    replace whatever the heuristic classifier wrote.

    `categories` is a frozenset for set-arithmetic at the propagation
    layer; YAML round-trip serialises it as a sorted list for
    deterministic output.

    Validation in `__post_init__` matches the other dataclasses in
    the project (Entity, Metric, CanonicalJoin) so a malformed
    override fails fast at construction rather than at apply time.
    """

    qualified_column: str
    sensitivity: Sensitivity
    categories: frozenset[PIICategory] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        from schemabrain.pii.categories import (
            PII_CATEGORIES,
            SENSITIVITIES,
        )

        if not _QUALIFIED_COLUMN_RE.fullmatch(self.qualified_column):
            raise ValueError(
                f"qualified_column must be `schema.table.column` form "
                f"with identifier-shaped parts (got {self.qualified_column!r})"
            )
        if self.sensitivity not in SENSITIVITIES:
            raise ValueError(
                f"sensitivity must be one of {list(SENSITIVITIES)} (got {self.sensitivity!r})"
            )
        unknown = self.categories - PII_CATEGORIES
        if unknown:
            raise ValueError(
                f"categories contains unknown values: "
                f"{sorted(unknown)} (allowed: {sorted(PII_CATEGORIES)})"
            )

    @property
    def qualified_table(self) -> str:
        """`schema.table` slice of the qualified column.

        Returned in the shape `write_column_pii_tags` expects so the
        apply layer can group overrides by table for bulk write
        without re-splitting.
        """
        schema, table, _column = self.qualified_column.split(".", 2)
        return f"{schema}.{table}"

    @property
    def column_name(self) -> str:
        """The column part of the qualified column."""
        _schema, _table, column = self.qualified_column.split(".", 2)
        return column


@dataclass(frozen=True)
class Policy:
    """The operator-editable PII enforcement policy.

    `block` is the category set the server refuses at query time
    (the runtime `--pii-block` equivalent). `column_overrides` is the
    sequence of operator-asserted tag overrides applied AFTER the
    heuristic classifier runs. Ordering of `column_overrides` is not
    semantically meaningful — the apply layer sorts by qualified
    column for deterministic output, and the propagation layer
    looks each one up by primary key.

    `description` is an optional free-text field operators can use
    to annotate the policy file (e.g. "PCI-DSS Q&A 1.1 — last4 is not
    sensitive when isolated"). Carried through the YAML round-trip
    so version control diffs are self-documenting.
    """

    block: frozenset[PIICategory]
    column_overrides: tuple[ColumnOverride, ...] = ()
    description: str = ""

    def __post_init__(self) -> None:
        from schemabrain.pii.categories import PII_CATEGORIES

        unknown = self.block - PII_CATEGORIES
        if unknown:
            raise ValueError(
                f"block contains unknown PII categories: "
                f"{sorted(unknown)} (allowed: {sorted(PII_CATEGORIES)})"
            )

        # Reject duplicate qualified columns in `column_overrides` —
        # two overrides for the same column would race at apply time
        # and the operator probably meant only one. Surface it as a
        # parse-time error rather than letting the second write
        # silently win.
        seen: set[str] = set()
        for override in self.column_overrides:
            if override.qualified_column in seen:
                raise ValueError(
                    f"duplicate column override: {override.qualified_column!r} "
                    f"appears more than once"
                )
            seen.add(override.qualified_column)
