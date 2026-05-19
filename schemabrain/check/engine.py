"""Drift detection engine for `schemabrain check`.

The engine reads the entities, metrics, and canonical joins persisted
in the local SQLite store and confirms each one against the live
source database. Each mismatch is recorded as a `Drift`.

Cascade rule (load-bearing for readable output):

  - If an entity is drifted (its bound table is missing, or its
    identity column is missing), every metric anchored on that entity
    and every canonical join touching it is **suppressed** from the
    drift list. The entity drift is the canonical issue; reporting
    "measure_column_missing" under a missing table would just be noise.

Live-source caching (load-bearing for performance):

  - `(schema, table)` lookups are cached per run. A 50-metric run
    against 10 tables hits the live source 10 times, not 100.
    `TableNotFoundError` is cached as `None` so subsequent references
    short-circuit without retrying the introspection call.

The engine is intentionally `DataSource`-typed (the existing connector
Protocol) rather than `PostgresDataSource`-specific so future backends
(SQLite, BigQuery, Snowflake) can participate without an engine change.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal, get_args

from schemabrain.connectors.base import DataSource
from schemabrain.connectors.errors import TableNotFoundError
from schemabrain.core.models import Table
from schemabrain.core.store_protocol import Store

_log = logging.getLogger(__name__)

# `DefKind` names the kind of semantic-layer definition that drifted.
# `DriftKind` names the specific shape of the drift. Both are closed
# Literals so adding a value here REQUIRES adding renderer + JSON
# handling cases — type checkers refuse the partial change.
DefKind = Literal["entity", "metric", "canonical_join"]
DriftKind = Literal[
    "table_missing",
    "identity_column_missing",
    "measure_column_missing",
    "time_dimension_column_missing",
    "join_column_missing",
]

_VALID_DEF_KINDS: frozenset[str] = frozenset(get_args(DefKind))
_VALID_DRIFT_KINDS: frozenset[str] = frozenset(get_args(DriftKind))


@dataclass(frozen=True)
class Drift:
    """One drift between a persisted definition and the live source.

    `def_kind` + `def_name` identify the broken definition.
    `drift_kind` names the failure mode (closed Literal so renderers
    can switch exhaustively). `detail` is a one-line human description
    of *what specifically is missing* (e.g. `public.customers.id`).
    `fix_hint` is a one-line operator next-step.
    """

    def_kind: DefKind
    def_name: str
    drift_kind: DriftKind
    detail: str
    fix_hint: str

    def __post_init__(self) -> None:
        if self.def_kind not in _VALID_DEF_KINDS:
            raise ValueError(
                f"def_kind must be one of {sorted(_VALID_DEF_KINDS)} (got {self.def_kind!r})"
            )
        if self.drift_kind not in _VALID_DRIFT_KINDS:
            raise ValueError(
                f"drift_kind must be one of {sorted(_VALID_DRIFT_KINDS)} (got {self.drift_kind!r})"
            )
        if not self.def_name:
            raise ValueError("def_name must be non-empty")


@dataclass(frozen=True)
class CheckReport:
    """Aggregated outcome of a drift check.

    `drifts` is ordered by definition kind (entities first, then
    metrics, then canonical joins) and within each kind by definition
    name. The ordering is stable so CI diffs over the JSON output
    don't churn on re-runs.

    `exit_code` is `0` when no drifts were found, `1` otherwise.
    """

    drifts: tuple[Drift, ...]
    entities_healthy: int
    metrics_healthy: int
    joins_healthy: int
    total_entities: int
    total_metrics: int
    total_joins: int

    def __post_init__(self) -> None:
        # Direct-construction guard — `check_drift` cannot violate
        # these invariants, but a future caller (test fixture, hosted
        # control-plane consumer) building a CheckReport by hand can.
        # Fail loud here rather than render an incoherent summary.
        for label, healthy, total in (
            ("entities", self.entities_healthy, self.total_entities),
            ("metrics", self.metrics_healthy, self.total_metrics),
            ("joins", self.joins_healthy, self.total_joins),
        ):
            if healthy < 0 or total < 0 or healthy > total:
                raise ValueError(
                    f"{label}_healthy must satisfy 0 <= healthy <= total "
                    f"(got healthy={healthy}, total={total})"
                )

    @property
    def exit_code(self) -> int:
        return 1 if self.drifts else 0

    def to_json_dict(self) -> dict[str, Any]:
        """Render the report as a JSON-serialisable dict.

        Stable insertion-order keys so the output diffs cleanly across
        runs. The shape is the same one consumed by `schemabrain
        check --json`.
        """
        return {
            "drifts": [
                {
                    "def_kind": d.def_kind,
                    "def_name": d.def_name,
                    "drift_kind": d.drift_kind,
                    "detail": d.detail,
                    "fix_hint": d.fix_hint,
                }
                for d in self.drifts
            ],
            "summary": {
                "entities_total": self.total_entities,
                "entities_healthy": self.entities_healthy,
                "metrics_total": self.total_metrics,
                "metrics_healthy": self.metrics_healthy,
                "joins_total": self.total_joins,
                "joins_healthy": self.joins_healthy,
            },
            "exit_code": self.exit_code,
        }


def check_drift(
    *,
    store: Store,
    source: DataSource,
    source_connection_id: str,
) -> CheckReport:
    """Compare persisted definitions against live source schema.

    The store is read-only here — no writes, no mutation. The live
    source is hit at most once per `(schema, table)` referenced by any
    definition; missing tables are cached as `None` so a downstream
    reference to the same table short-circuits without re-querying.
    """
    entities = store.list_entities(source_connection_id=source_connection_id)
    metrics = store.list_metrics(source_connection_id=source_connection_id)
    joins = store.list_canonical_joins(source_connection_id=source_connection_id)

    entity_by_name = {e.name: e for e in entities}

    # `(schema, table) -> Table | None` — None means the introspection
    # call surfaced a TableNotFoundError. Caching the negative result
    # is the load-bearing optimisation: a deleted table referenced by
    # an entity + 4 metrics + 2 joins resolves to one introspection
    # call, not seven.
    table_cache: dict[tuple[str, str], Table | None] = {}

    def _live_table(schema: str, name: str) -> Table | None:
        key = (schema, name)
        if key not in table_cache:
            try:
                table_cache[key] = source.get_table(name, schema)
            except TableNotFoundError:
                table_cache[key] = None
            except Exception:
                # Any non-`TableNotFoundError` is a real introspection
                # failure (connection drop mid-reflection, permission
                # error on `information_schema`, etc.). Log the
                # `(schema, table)` key so the operator knows which
                # entity / metric triggered the failure, then re-raise
                # — the CLI's outer `except OperationalError` returns
                # exit 2 with a guided error envelope. Without this
                # log the operator sees a generic "could not connect"
                # with no clue which definition was being checked.
                _log.exception(
                    "schema introspection failed for %s.%s during drift check",
                    schema,
                    name,
                )
                raise
        return table_cache[key]

    drifts: list[Drift] = []

    # Set of entity names that have *any* drift. Metrics anchored on
    # drifted entities + joins touching drifted entities are suppressed
    # to keep the output focused on root-cause issues.
    drifted_entity_names: set[str] = set()

    # ----- entities -----------------------------------------------------
    entities_healthy = 0
    for entity in entities:
        schema, table = entity.qualified_table.split(".", 1)
        live = _live_table(schema, table)
        if live is None:
            drifts.append(
                Drift(
                    def_kind="entity",
                    def_name=entity.name,
                    drift_kind="table_missing",
                    detail=entity.qualified_table,
                    fix_hint=(
                        f"restore the table, drop entity {entity.name!r}, "
                        f"or re-bind the entity to an existing table via "
                        f"`schemabrain entities apply`"
                    ),
                )
            )
            drifted_entity_names.add(entity.name)
            continue
        if live.get_column(entity.identity) is None:
            drifts.append(
                Drift(
                    def_kind="entity",
                    def_name=entity.name,
                    drift_kind="identity_column_missing",
                    detail=f"{entity.qualified_table}.{entity.identity}",
                    fix_hint=(
                        f"update entity `{entity.name}`'s `identity:` field "
                        f"and re-run `schemabrain entities apply`"
                    ),
                )
            )
            drifted_entity_names.add(entity.name)
            continue
        entities_healthy += 1

    # ----- metrics ------------------------------------------------------
    metrics_healthy = 0
    for metric in metrics:
        # FK at the store level guarantees the anchor entity exists in
        # the store; defensive guard handles direct-SQL corruption.
        anchor = entity_by_name.get(metric.entity)
        if anchor is None:  # pragma: no cover — defended by store FK
            continue
        if metric.entity in drifted_entity_names:
            # Cascade suppression: anchor entity is broken — the
            # metric's column-level drifts are downstream noise.
            continue
        schema, table = anchor.qualified_table.split(".", 1)
        live = _live_table(schema, table)
        # `live is None` is unreachable here — the anchor was healthy
        # above, which implies _live_table returned a Table. Fail
        # loud rather than silently skip if a future refactor breaks
        # the invariant.
        if live is None:  # pragma: no cover — invariant guard
            raise AssertionError(
                f"engine invariant violated: healthy anchor entity "
                f"{metric.entity!r} resolved to a missing live table"
            )

        if live.get_column(metric.measure.column) is None:
            drifts.append(
                Drift(
                    def_kind="metric",
                    def_name=metric.name,
                    drift_kind="measure_column_missing",
                    detail=f"{anchor.qualified_table}.{metric.measure.column}",
                    fix_hint=(
                        f"update metric `{metric.name}`'s `measure.column` "
                        f"and re-run `schemabrain metrics apply`"
                    ),
                )
            )
            continue
        if metric.time_dimension is not None:
            # v1 metric validator guarantees time_dimension is
            # `<entity>.<column>` form, and at v1 the prefix entity
            # MUST equal the anchor entity (cross-entity time
            # dimensions defer to v2). Assert the invariant
            # explicitly here so a future grammar change that allows
            # cross-entity time dimensions fails loud during `check`
            # rather than silently confirming the column against the
            # wrong table.
            td_entity, td_col = metric.time_dimension.split(".", 1)
            if td_entity != metric.entity:  # pragma: no cover — invariant guard
                raise AssertionError(
                    f"metric {metric.name!r} time_dimension entity "
                    f"prefix {td_entity!r} does not match anchor "
                    f"entity {metric.entity!r}; cross-entity time "
                    f"dimensions are not supported at v1"
                )
            if live.get_column(td_col) is None:
                drifts.append(
                    Drift(
                        def_kind="metric",
                        def_name=metric.name,
                        drift_kind="time_dimension_column_missing",
                        detail=f"{anchor.qualified_table}.{td_col}",
                        fix_hint=(
                            f"update metric `{metric.name}`'s "
                            f"`time_dimension` and re-run "
                            f"`schemabrain metrics apply`"
                        ),
                    )
                )
                continue
        metrics_healthy += 1

    # ----- canonical joins ---------------------------------------------
    joins_healthy = 0
    for join in joins:
        # FK at store level guarantees source/target entities exist.
        src_ent = entity_by_name.get(join.source_entity)
        tgt_ent = entity_by_name.get(join.target_entity)
        if src_ent is None or tgt_ent is None:  # pragma: no cover — store FK
            continue
        # Cascade suppression — if either endpoint entity is drifted,
        # the join's column-level drifts are downstream noise.
        if join.source_entity in drifted_entity_names or join.target_entity in drifted_entity_names:
            continue
        src_schema, src_table = src_ent.qualified_table.split(".", 1)
        tgt_schema, tgt_table = tgt_ent.qualified_table.split(".", 1)
        src_live = _live_table(src_schema, src_table)
        tgt_live = _live_table(tgt_schema, tgt_table)
        # Both must be live tables — cascade suppression above ensured
        # we only reach here when both endpoint entities were healthy,
        # which means both tables resolved cleanly. Fail loud rather
        # than silently skip if a future refactor breaks the
        # invariant.
        if src_live is None or tgt_live is None:  # pragma: no cover — invariant guard
            raise AssertionError(
                f"engine invariant violated: healthy join {join.name!r} "
                f"resolved to a missing live table on one endpoint"
            )

        # First-wins per-pair drift reporting: a join with composite
        # `on:` like `[(a,b), (c,d)]` and both pairs drifted produces
        # ONE Drift, not two. Operator-experience choice — the first
        # broken column is enough to make the operator act, and
        # surfacing every pair would amplify noise in junction-table
        # cases. Trade-off documented; revisit if junction-table
        # drift becomes a common operator complaint.
        join_drifted = False
        for pair in join.on:
            if src_live.get_column(pair.source_column) is None:
                drifts.append(
                    Drift(
                        def_kind="canonical_join",
                        def_name=join.name,
                        drift_kind="join_column_missing",
                        detail=f"{src_ent.qualified_table}.{pair.source_column}",
                        fix_hint=(
                            f"update join `{join.name}`'s `on:` columns "
                            f"or restore the source column"
                        ),
                    )
                )
                join_drifted = True
                break
            if tgt_live.get_column(pair.target_column) is None:
                drifts.append(
                    Drift(
                        def_kind="canonical_join",
                        def_name=join.name,
                        drift_kind="join_column_missing",
                        detail=f"{tgt_ent.qualified_table}.{pair.target_column}",
                        fix_hint=(
                            f"update join `{join.name}`'s `on:` columns "
                            f"or restore the target column"
                        ),
                    )
                )
                join_drifted = True
                break
        if not join_drifted:
            joins_healthy += 1

    return CheckReport(
        drifts=tuple(drifts),
        entities_healthy=entities_healthy,
        metrics_healthy=metrics_healthy,
        joins_healthy=joins_healthy,
        total_entities=len(entities),
        total_metrics=len(metrics),
        total_joins=len(joins),
    )
