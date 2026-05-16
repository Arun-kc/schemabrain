"""Entity — the foundation primitive of the semantic layer.

An Entity is a named, validator-backed binding from a domain concept
("customer", "order") to one physical table. It's the substrate
metrics and canonical joins compose on top of in follow-up PRs.

Design decisions baked into this module:
  - single_table binding only at v1. Multi-table bindings are a v2
    concern; the dataclass exposes only `SingleTableBinding` and any
    attempt to construct another shape is a static type error.
  - `identity` column is required. An entity without one can't
    anchor metrics or dedupe in joins, so we reject at construction.
  - `origin` is a closed Literal — manual / suggested / dbt_import.
    Defaults to "manual" because every YAML hand-edited file omits
    it; the LLM-suggest path writes "suggested", the dbt import
    path writes "dbt_import". The store-side write path refuses
    cross-origin overwrites where the existing row is `dbt_import`
    — see `DbtOwnedEntityError` below.

`DbtOwnedEntityError` lives in this module (not in `core/store.py`)
because it is part of the `Store` Protocol's behavioural contract — a
future hosted-backend implementor must be able to raise it without
importing from the concrete SQLite implementation.

Validation runs in `__post_init__` so the same invariants apply
regardless of whether construction comes from YAML loading, store
reads, or programmatic calls. The store reads rely on this because
the SQLite CHECK constraints cover only `origin`; identifier shape +
binding form are enforced here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, get_args

# Closed set of provenance values. Stays in lock-step with the SQL
# CHECK constraint on the `entities` table in
# `core/store.py::_DDL_STATEMENTS` — adding a value here without
# bumping the schema version (and updating the CHECK) will silently
# break round-trips.
Origin = Literal["manual", "suggested", "dbt_import"]
_VALID_ORIGINS: frozenset[str] = frozenset(get_args(Origin))

# Postgres unquoted identifier shape: letter/underscore lead,
# letters/digits/underscores/`$` after. Matches the column-name regex
# in `mcp/_helpers.py` so an entity whose `identity` column references
# a real Postgres column like `row$id` constructs cleanly. Quoted
# identifiers (embedded spaces, dots) are intentionally rejected.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")

# `schema.table` form — exactly one dot, both parts identifier-shaped.
# Rejects column-shaped `schema.table.column` (two dots) and any
# bare-table-name input. Tracks the same identifier alphabet as
# `_IDENT_RE` (including `$`).
_QUALIFIED_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*\.[A-Za-z_][A-Za-z0-9_$]*$")


class DbtOwnedEntityError(ValueError):
    """Raised when a manual or LLM-suggested write targets a dbt-owned entity.

    An entity with `origin = "dbt_import"` is owned upstream by the
    user's dbt project. The wk-12 dbt-import write-path is the only
    code path allowed to overwrite it; manual edits and LLM-suggested
    edits are refused at the store boundary so a re-import never has
    to litigate "whose version of the entity wins."

    Subclass of `ValueError` so callers that just want to surface
    "this write was refused" can `except ValueError`.

    Lives in this module (`core/entity.py`) — not in `core/store.py` —
    because the `Store` Protocol's `write_entity` contract names it
    explicitly. A future store backend (in-memory mock, hosted v3)
    raises this exception without depending on the concrete SQLite
    implementation.
    """


@dataclass(frozen=True)
class SingleTableBinding:
    """Binds an entity to one physical table by qualified name.

    `qualified_table` is the postgres-style `schema.table` form (e.g.
    `public.customers`). Validation is regex-strict to keep the
    invariant cheap to assert across all callsites.
    """

    qualified_table: str

    def __post_init__(self) -> None:
        if not _QUALIFIED_TABLE_RE.fullmatch(self.qualified_table):
            raise ValueError(
                f"qualified_table must be 'schema.table' form (got {self.qualified_table!r})"
            )


@dataclass(frozen=True)
class Entity:
    """A named binding from a domain concept to one physical table.

    `name` and `identity` must both be identifier-shaped. `description`
    may be empty — LLM-suggest populates it in the next PR; hand-edited
    YAML can omit it without parse error. `binding` is currently
    locked to `SingleTableBinding`; future multi-table shapes (v2)
    will arrive as additional variant types and a
    `binding: SingleTableBinding | MultiTableBinding` type.
    """

    name: str
    description: str
    binding: SingleTableBinding
    identity: str
    origin: Origin = "manual"

    def __post_init__(self) -> None:
        if not _IDENT_RE.fullmatch(self.name):
            raise ValueError(f"name must be an identifier (got {self.name!r})")
        if not _IDENT_RE.fullmatch(self.identity):
            raise ValueError(f"identity must be an identifier (got {self.identity!r})")
        # Origin is type-checked statically via Literal, but the
        # runtime guard catches malformed YAML and untyped callers
        # before the store-side CHECK has a chance to.
        if self.origin not in _VALID_ORIGINS:
            raise ValueError(
                f"origin must be one of {sorted(_VALID_ORIGINS)} (got {self.origin!r})"
            )

    @property
    def qualified_table(self) -> str:
        """The `schema.table` form of the bound physical table.

        Convenience accessor used by callers that don't care about the
        binding shape — and the seam that insulates them from the v2
        multi-table refactor. When `binding` becomes
        `SingleTableBinding | MultiTableBinding`, this property
        rewrites; callsites do not.
        """
        return self.binding.qualified_table
