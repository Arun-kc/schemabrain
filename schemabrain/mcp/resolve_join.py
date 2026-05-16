"""MCP tool implementation: resolve_join.

The the canonical-join lookup tool. Given two entity names (in any
order), return the confirmed `CanonicalJoin` between them — including
a paste-ready `JOIN ... ON ...` SQL skeleton.

Behaviour:

  - 0 canonical joins between the pair → `NoCanonicalJoinError`
  - 1 canonical join, no `name` arg → return that join
  - 1 canonical join, `name` matches → return that join
  - 1 canonical join, `name` doesn't match → `JoinNameMismatchError`
  - 2+ canonical joins, no `name` arg → `AmbiguousJoinError`
  - 2+ canonical joins, `name` matches one → return that join
  - 2+ canonical joins, `name` doesn't match → `UnknownJoinNameError`

Direction-insensitive: `resolve_join("customer", "order")` and
`resolve_join("order", "customer")` consult the same row set. The
returned `CanonicalJoinInfo` preserves the STORED direction (which
the user picked at apply time) so the SQL skeleton stays consistent
regardless of call order.

Per the design, cycle detection is NOT done here — `resolve_join` is
a 1-hop lookup. The cycle-detection report is surfaced via the CLI
`joins suggest --report` flag.
"""

from __future__ import annotations

import re

from schemabrain.core.entity import Entity
from schemabrain.core.join import CanonicalJoin
from schemabrain.core.store_protocol import Store
from schemabrain.mcp._helpers import _validate_ident, _with_token_estimate
from schemabrain.mcp.shapes import (
    AmbiguousJoinError,
    CanonicalJoinInfo,
    EntityNotFoundError,
    JoinColumnPairInfo,
    JoinNameMismatchError,
    NoCanonicalJoinError,
    UnknownJoinNameError,
)

# Postgres unquoted identifier shape — matches the entity / column
# alphabet. Reused for sanitising entity names into SQL aliases.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def resolve_join_impl(
    *,
    store: Store,
    source_connection_id: str,
    entity_a: str,
    entity_b: str,
    name: str | None = None,
) -> CanonicalJoinInfo:
    """Return the canonical join between `entity_a` and `entity_b`.

    Raises:
      `ValueError`: either entity name is malformed
      `EntityNotFoundError`: either entity is absent in the store
      `NoCanonicalJoinError`: zero canonical joins between the pair
      `AmbiguousJoinError`: 2+ joins, no name disambiguator
      `JoinNameMismatchError`: 1 join, name mismatch
      `UnknownJoinNameError`: 2+ joins, name doesn't match any

    The MCP server wrapper maps each error to the appropriate
    Charter-v1.1 error kind on the response envelope.
    """
    _validate_ident(entity_a, role="entity_a")
    _validate_ident(entity_b, role="entity_b")
    if name is not None:
        _validate_ident(name, role="name")

    # Both entities must exist for the join to be meaningful.
    entity_a_obj = store.get_entity(entity_a, source_connection_id=source_connection_id)
    if entity_a_obj is None:
        raise EntityNotFoundError(
            f"No entity named {entity_a!r} for this source. "
            f"Call `list_entities` to see what's defined."
        )
    entity_b_obj = store.get_entity(entity_b, source_connection_id=source_connection_id)
    if entity_b_obj is None:
        raise EntityNotFoundError(
            f"No entity named {entity_b!r} for this source. "
            f"Call `list_entities` to see what's defined."
        )

    matches = store.resolve_canonical_joins(
        entity_a, entity_b, source_connection_id=source_connection_id
    )

    if not matches:
        raise NoCanonicalJoinError(
            f"No canonical join exists between {entity_a!r} and {entity_b!r}. "
            f"Run `schemabrain joins suggest --source <URL>` to surface "
            f"candidates, then `schemabrain joins apply` the chosen one."
        )

    selected = _select_join(matches, name=name, entity_a=entity_a, entity_b=entity_b)

    return _render_join_info(
        selected,
        source_entity=_lookup_entity(store, selected.source_entity, source_connection_id),
        target_entity=_lookup_entity(store, selected.target_entity, source_connection_id),
    )


def _select_join(
    matches: list[CanonicalJoin],
    *,
    name: str | None,
    entity_a: str,
    entity_b: str,
) -> CanonicalJoin:
    """Pick the right canonical join based on count + name disambiguator.

    Implements the full resolve_join truth table — the single most
    important interaction surface of the canonical-join work.

    Precondition: `matches` must be non-empty. The caller
    (`resolve_join_impl`) guards this explicitly before invoking
    `_select_join`; the assertion below documents the contract for any
    future caller and converts a precondition violation into a
    diagnostic message instead of a bare `IndexError` on
    `matches[0]`.
    """
    if not matches:  # pragma: no cover — caller guards with NoCanonicalJoinError
        raise AssertionError(
            "_select_join requires non-empty `matches`; the caller "
            "must raise NoCanonicalJoinError when the store returns []"
        )
    if len(matches) == 1:
        only = matches[0]
        if name is None or name == only.name:
            return only
        raise JoinNameMismatchError(
            f"The only canonical join between {entity_a!r} and "
            f"{entity_b!r} is named {only.name!r}, not {name!r}. "
            f"Pass `name={only.name!r}` or omit the name argument.",
            canonical_name=only.name,
        )

    # 2+ matches — ambiguity refusal path.
    candidate_names = tuple(j.name for j in matches)
    if name is None:
        joined = ", ".join(repr(n) for n in candidate_names)
        raise AmbiguousJoinError(
            f"Multiple canonical joins exist between {entity_a!r} and "
            f"{entity_b!r}: {joined}. Pass `name=<one_of_these>` to "
            f"disambiguate.",
            candidate_names=candidate_names,
        )
    for match in matches:
        if match.name == name:
            return match
    joined = ", ".join(repr(n) for n in candidate_names)
    raise UnknownJoinNameError(
        f"No canonical join named {name!r} between {entity_a!r} and "
        f"{entity_b!r}. Available: {joined}.",
        candidate_names=candidate_names,
    )


def _lookup_entity(store: Store, name: str, source_connection_id: str) -> Entity:
    """Defensive lookup — the store's FK invariant guarantees both
    entities exist when the canonical join row does, but we surface
    a real exception rather than an AttributeError if the store is
    corrupt.
    """
    entity = store.get_entity(name, source_connection_id=source_connection_id)
    if (
        entity is None
    ):  # pragma: no cover — canonical_joins FK to entities makes this structurally unreachable
        raise RuntimeError(
            f"FK invariant violated: canonical join references missing entity {name!r}"
        )
    return entity


def _render_join_info(
    join: CanonicalJoin,
    *,
    source_entity: Entity,
    target_entity: Entity,
) -> CanonicalJoinInfo:
    """Build the `CanonicalJoinInfo` envelope including a paste-ready
    SQL JOIN skeleton.

    The skeleton uses the entity name as the table alias when the
    entity name is identifier-shaped (which the dataclass guarantees).
    Composite-key joins render with `AND`-joined predicates.
    """
    source_alias = _safe_alias(source_entity.name)
    target_alias = _safe_alias(target_entity.name)
    predicates = " AND ".join(
        f"{source_alias}.{pair.source_column} = {target_alias}.{pair.target_column}"
        for pair in join.on
    )
    sql_skeleton = f"JOIN {target_entity.qualified_table} AS {target_alias} ON {predicates}"

    partial = CanonicalJoinInfo(
        name=join.name,
        description=join.description,
        source_entity=join.source_entity,
        target_entity=join.target_entity,
        on=[
            JoinColumnPairInfo(
                source_column=pair.source_column,
                target_column=pair.target_column,
            )
            for pair in join.on
        ],
        sql_skeleton=sql_skeleton,
        token_estimate=0,  # placeholder; `_with_token_estimate` rebuilds
    )
    return _with_token_estimate(partial)


def _safe_alias(name: str) -> str:
    """Return a SQL-safe alias derived from `name`.

    The entity dataclass already enforces identifier shape, so this
    is the identity function in practice — but we run it through
    `_IDENT_RE` to defend against future contract drift (e.g. someone
    relaxing the Entity validator).
    """
    if _IDENT_RE.fullmatch(name):
        return name
    # Pathological fallback — never reached under current contracts,
    # but cheap defence-in-depth.
    return "t"  # pragma: no cover
