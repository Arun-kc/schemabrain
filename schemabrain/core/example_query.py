"""ExampleQuery — one observed SQL pattern for an indexed table.

Storage primitive behind tool #5 `get_example_queries`. v0.5 ships the
read side; the write side (`pg_stat_statements` mining + `sqlglot`
parsing) lands in the next PR. The `sensitivity` and `pii_categories`
fields carry the Phase B ADR's 2-layer PII taxonomy from day one —
they default to public / empty until mining computes real propagated
values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from schemabrain.pii.categories import PIICategory, Sensitivity

# The closed enumeration of where an example came from. Day one ships
# the two we know about: mined SQL from `pg_stat_statements` and a
# hand-curated seed (operators or schema donors). Additions are minor
# charter bumps and require updating the SQL CHECK constraint on
# `example_queries.source` to match.
ExampleQuerySource = Literal["pg_stat_statements", "curated"]


@dataclass(frozen=True)
class ExampleQuery:
    """One observed example SQL for a single table.

    `schema_name` + `table_name` identify the table the example is
    "about" — i.e. the table the agent asked for examples of.
    `sql_text` is the literal SQL as observed (mining-side normalisation
    happens before write; the read layer is pass-through).

    `observation_count`, `first_seen_at`, `last_seen_at` describe how
    often the same SQL pattern was seen and over what window. Both
    timestamps are Unix epoch seconds (UTC), matching the project's
    other store-side time columns.

    `sensitivity` + `pii_categories` carry the Phase B ADR's 2-layer
    PII taxonomy. They default to the safe empty shape; mining lands
    real values once column-resolution + propagation are wired up.

    The cross-layer invariant (`sensitivity == "pii"` requires at
    least one category; non-pii sensitivity must carry an empty
    category set) is enforced in `__post_init__` — locked from day
    one so the mining writer can't ship rows that violate it.
    """

    schema_name: str
    table_name: str
    sql_text: str
    observation_count: int
    first_seen_at: int
    last_seen_at: int
    source: ExampleQuerySource
    sensitivity: Sensitivity = "public"
    pii_categories: frozenset[PIICategory] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if self.sensitivity == "pii" and not self.pii_categories:
            raise ValueError("sensitivity='pii' requires at least one pii_category")
        if self.sensitivity != "pii" and self.pii_categories:
            raise ValueError(
                f"sensitivity={self.sensitivity!r} cannot carry pii_categories "
                f"(got {sorted(self.pii_categories)!r})"
            )
