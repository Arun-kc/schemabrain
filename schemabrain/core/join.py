"""CanonicalJoin — the persisted answer to "how do entity A and entity B connect?".

A `CanonicalJoin` is a named, validator-backed binding between two
entities, expressing the equi-join predicate that links them. It is the
fourth piece of the semantic-layer substrate (after entities, the
LLM-suggest pipeline, and the dbt-manifest importer).

Design decisions baked into this module:

  - **Name-keyed, not pair-keyed.** Multiple canonical joins between the
    same `(source_entity, target_entity)` pair are explicitly supported
    (the billing/shipping address case is the canonical example). The
    name is the public identifier; the entity pair is searchable but
    not unique.

  - **Equi-join only.** `on` is a tuple of `(source_column,
    target_column)` pairs; both must be simple identifiers on their
    respective entities. Range and non-equi predicates are deferred to
    v2's expression layer.

  - **Origin is the same closed Literal as `Entity.origin`.** All three
    values (`manual` / `suggested` / `dbt_import`) are valid from day
    one. `dbt_import` is RESERVED — no producer ships at the current
    release, so the suggest + apply layers refuse it explicitly.
    Reserving keeps the schema symmetric and avoids a SQLite CHECK
    migration when the dbt-relationships importer lands in a future
    release.

  - **Composite-key joins are first-class.** `on` is a tuple, not a
    single pair. Composite-PK FK joins (most common in junction tables)
    work without special casing.

Validation runs in `__post_init__` so the same invariants apply
regardless of construction path (YAML loading, store reads, mining
output). The store reads rely on this because the SQLite CHECK covers
only `origin`; identifier shape + on-list non-emptiness + self-join
refusal are enforced here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, get_args

# Closed set of provenance values. Stays in lock-step with the SQL
# CHECK constraint on the `canonical_joins` table in
# `core/store.py::_DDL_STATEMENTS` — adding a value here without
# bumping the schema version (and updating the CHECK) will silently
# break round-trips.
JoinOrigin = Literal["manual", "suggested", "dbt_import"]
_VALID_ORIGINS: frozenset[str] = frozenset(get_args(JoinOrigin))

# Closed set of cardinality values. Optional on the dataclass — joins
# authored before the cardinality column landed don't carry one, and
# the metric compiler treats `None` as `many_to_many` (worst-case →
# emits fan-out warning). The set matches the dimensional-modelling
# convention used by dbt's semantic layer.
Cardinality = Literal["one_to_one", "one_to_many", "many_to_one", "many_to_many"]
_VALID_CARDINALITIES: frozenset[str] = frozenset(get_args(Cardinality))

# Postgres unquoted identifier shape — same alphabet as
# `Entity.name` and `Entity.identity` so a canonical join's
# columns satisfy the same identifier invariants the bound
# entity's columns do.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


@dataclass(frozen=True)
class JoinColumnPair:
    """One equi-join predicate pair: `source_entity.<src> = target_entity.<tgt>`.

    A canonical join with composite keys (a junction-table FK, a
    natural-key join) carries N of these in `CanonicalJoin.on`. Each
    column is a simple identifier on its respective entity's bound
    table.
    """

    source_column: str
    target_column: str

    def __post_init__(self) -> None:
        if not _IDENT_RE.fullmatch(self.source_column):
            raise ValueError(f"source_column must be an identifier (got {self.source_column!r})")
        if not _IDENT_RE.fullmatch(self.target_column):
            raise ValueError(f"target_column must be an identifier (got {self.target_column!r})")


@dataclass(frozen=True)
class CanonicalJoin:
    """A named, confirmed equi-join between two entities.

    `name` is the primary identifier; `(source_connection_id, name)` is
    the storage PK. `source_entity` and `target_entity` reference
    `entities (source_connection_id, name)` — the store enforces both
    must exist before this row is written.

    `on` is non-empty: a join with no predicate is a cross join, and
    cross joins are not canonical. Self-joins (`source_entity ==
    target_entity`) are rejected — they're uncommon enough that
    deferring native support to a future release is fine; the
    workaround is to model each side as a separate entity (e.g.,
    `manager` and `direct_report` for an employee reports-to graph).
    """

    name: str
    description: str
    source_entity: str
    target_entity: str
    on: tuple[JoinColumnPair, ...]
    origin: JoinOrigin = "manual"
    cardinality: Cardinality | None = None

    def __post_init__(self) -> None:
        if not _IDENT_RE.fullmatch(self.name):
            raise ValueError(f"name must be an identifier (got {self.name!r})")
        if not _IDENT_RE.fullmatch(self.source_entity):
            raise ValueError(f"source_entity must be an identifier (got {self.source_entity!r})")
        if not _IDENT_RE.fullmatch(self.target_entity):
            raise ValueError(f"target_entity must be an identifier (got {self.target_entity!r})")
        if self.source_entity == self.target_entity:
            raise ValueError(
                f"self-joins are not supported (got "
                f"source_entity == target_entity == {self.source_entity!r}). "
                f"Workaround: model each side as a separate entity "
                f"(e.g. `manager` and `direct_report` for an employee "
                f"reports-to graph), then define the canonical join on "
                f"the FK column from one side."
            )
        if not self.on:
            raise ValueError(
                f"canonical join {self.name!r} requires at least one "
                f"`on` column pair (cross joins are not canonical)"
            )
        # Origin is type-checked statically via Literal, but the
        # runtime guard catches malformed YAML and untyped callers
        # before the store-side CHECK has a chance to.
        if self.origin not in _VALID_ORIGINS:
            raise ValueError(
                f"origin must be one of {sorted(_VALID_ORIGINS)} (got {self.origin!r})"
            )
        # `None` is valid (the back-compat shape for joins authored
        # before this column existed); any string value must be in
        # the closed Literal set.
        if self.cardinality is not None and self.cardinality not in _VALID_CARDINALITIES:
            raise ValueError(
                f"cardinality must be None or one of "
                f"{sorted(_VALID_CARDINALITIES)} (got {self.cardinality!r})"
            )
