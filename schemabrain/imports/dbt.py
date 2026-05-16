"""Parse dbt `manifest.json` into typed Schema Brain shapes.

This module is the substrate of the dbt import write-path. It turns the
deep, version-shifting JSON dbt emits at compile time into a small,
frozen-dataclass tree that the rest of the import pipeline consumes
without touching the original JSON shape.

Scope boundaries — by design:

  - **Parse, don't map.** This module produces `DbtModelNode` /
    `DbtSourceNode` / `DbtColumn` shapes that mirror dbt's own
    concepts. Mapping a `DbtModelNode` to a Schema Brain `Entity`
    (identity resolution, source provenance, name validation) is a
    separate concern that consumes this module's output.
  - **No live-schema contact.** The parser never opens a database
    connection. The verification step that checks dbt's claims against
    the running source DB is a separate concern.
  - **No `dbt-core` runtime dep.** We read `manifest.json` as plain
    JSON. Adding `dbt-core` as a dep would lock Schema Brain to a
    specific dbt version + pull in a heavy + opinionated runtime tree
    of Jinja, networkx, etc. The cost is that we own the manifest
    schema interpretation — bounded by the version-range guard below.

Supported manifest schema versions: **v10 and above**. v10 introduced
the column `constraints` syntax (e.g. `[{"type": "primary_key"}]`) that
the identity resolver consumes. Older manifests are rejected with a
guided error pointing the user at `dbt compile` after a dbt-core
upgrade.

Test aggregation: dbt models declare `unique` / `not_null` tests as
separate `test.*` nodes under `manifest.nodes`, attached to their
target via `attached_node` + `column_name`. The parser folds those
aggregates back into per-column `DbtColumnTests` so the identity
resolver can read constraints + tests off a single `DbtColumn`
instance.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal, get_args

from schemabrain.connectors.base import DataSource
from schemabrain.connectors.errors import TableNotFoundError
from schemabrain.core.entity import DbtOwnedEntityError, Entity, Origin, SingleTableBinding
from schemabrain.core.store_protocol import Store

_logger = logging.getLogger(__name__)

# Minimum dbt manifest schema version supported. v10 was the first to
# carry the `constraints` syntax we rely on for identity-resolution
# tier 1 ("explicit primary_key constraint"). dbt-core 1.7+ produces
# v10; 1.8+ produces v11/v12.
_MIN_MANIFEST_VERSION = 10

# dbt's `metadata.dbt_schema_version` is a URL embedding the schema
# major version: `https://schemas.getdbt.com/dbt/manifest/v<N>.json`.
# We extract `<N>` to compare against `_MIN_MANIFEST_VERSION`. Any URL
# shape that doesn't match is rejected as malformed — better than
# silently accepting an unknown schema.
_SCHEMA_URL_RE = re.compile(r"manifest/v(\d+)\.json$")

# The `attached_node` field of a `test.*` node names the model under
# test; `column_name` names the column. We aggregate per (model_id,
# column_name) tuple so identity resolution can consult a single
# `DbtColumnTests` record per column.
_UNIQUE_TEST_NAME = "unique"
_NOT_NULL_TEST_NAME = "not_null"

# Constraint-type names used in identity resolution. Matches dbt's
# `columns.<col>.constraints[].type` literals.
_PRIMARY_KEY_CONSTRAINT = "primary_key"
_UNIQUE_CONSTRAINT = "unique"
_NOT_NULL_CONSTRAINT = "not_null"

# Resource-type → `DbtSkipCounts` field mapping. Lives at module level
# so a future contributor wiring a new countable resource only edits
# this map + the `DbtSkipCounts` dataclass. Resource types not in this
# map and not equal to "model" / "test" fall through to the `other`
# bucket so an upgrade-time count is surfaceable rather than silently
# truncating the import.
_COUNTED_NODE_TYPES: dict[str, str] = {
    "metric": "metrics",
    "snapshot": "snapshots",
    "seed": "seeds",
    "analysis": "analyses",
    "operation": "operations",
    "exposure": "exposures",
}

# Closed Literal of identity-resolution methods. The audit-log
# `entity.imported.dbt` event records which tier matched so a future
# audit dashboard can bucket imports by method. Keeping this as a
# closed Literal means a typo in a new tier label would be caught at
# the `__post_init__` validator below rather than silently breaking
# the dashboard.
IdentityResolutionMethod = Literal[
    "primary_key_constraint",
    "unique_not_null_constraints",
    "unique_not_null_tests",
]
_VALID_IDENTITY_METHODS: frozenset[str] = frozenset(get_args(IdentityResolutionMethod))


class DbtManifestParseError(ValueError):
    """Raised when a manifest.json cannot be loaded or interpreted.

    Subclass of `ValueError` so callers that want a single broad catch
    can use `except ValueError`. Specific cases include missing file,
    invalid JSON, missing top-level keys, unsupported schema version,
    and malformed schema URL.
    """


class DbtIdentityResolutionError(ValueError):
    """Raised when a dbt model's identity column cannot be resolved.

    Two cases:
      - No column matches any of the 3 priority tiers (no primary_key
        constraint, no unique+not_null constraint pair, no unique+
        not_null test pair). The user needs to add an identity
        declaration to their dbt YAML.
      - Multiple columns match WITHIN the same tier. This signals a
        composite-PK declaration, which Schema Brain v1 doesn't
        support (single-table single-column identity only;
        multi-column identity is part of the v2 multi-table-binding
        work).

    Subclass of `ValueError` so a single broad catch in the CLI's
    per-model skip loop can decide whether to surface as
    `DbtIdentityResolutionError` (skip + log) or a deeper error.
    """


class DbtEntityMappingError(ValueError):
    """Raised when a dbt model cannot be mapped to a valid Entity.

    Wraps a deeper `ValueError` from the `Entity` / `SingleTableBinding`
    constructors when the dbt model's name or compiled identifier
    doesn't satisfy Schema Brain's identifier-shape constraints.

    The error message names the dbt `unique_id` of the offending
    model so the user can locate it in `dbt ls` output and fix the
    upstream YAML — usually by renaming the model to identifier
    shape (no hyphens, no embedded dots, no leading digit).

    Distinct from `DbtIdentityResolutionError`: identity issues are
    fixable in dbt YAML (add a primary_key constraint); mapping
    issues are fixable in dbt's model naming (rename the model).
    """


class DbtSchemaDriftError(ValueError):
    """Raised when a dbt model's manifest claims don't match the live source.

    Three flavours of drift, all surfaced through this single exception:
      - The compiled physical table doesn't exist in the live source
        (typically a `dbt compile` ran but `dbt run` did not).
      - One or more dbt-declared columns are missing in the live table
        (YAML is ahead of the materialization).
      - The resolved identity column is nullable in the live schema
        (cannot reliably anchor metrics).

    Error messages list every drift point at once so the user can fix
    in one round-trip rather than discovering drift incrementally.

    The driver layer catches this per-model and surfaces in the run
    summary — a single drifted model doesn't kill the import.
    """


@dataclass(frozen=True)
class DbtConstraint:
    """A column-level constraint as dbt's manifest declares it.

    dbt's `columns.<col>.constraints` is a list of `{"type": ..., "name":
    ...}` entries — `type` is values like `primary_key` / `not_null` /
    `unique` / `foreign_key` / `check`. `name` is optional (only mostly
    used for foreign-key + check constraints with explicit names).
    """

    type: str
    name: str | None = None


@dataclass(frozen=True)
class DbtColumnTests:
    """Aggregate of `unique` and `not_null` tests attached to one column.

    dbt represents tests as separate `test.*` nodes that point back at
    a (model, column) target. The parser aggregates the relevant ones
    here so identity resolution can read one record per column.
    """

    is_unique: bool = False
    is_not_null: bool = False


@dataclass(frozen=True)
class DbtColumn:
    """A column declaration on a dbt model, as the manifest carries it.

    `data_type` is `None` when dbt didn't see a `data_type:` entry in
    the column's YAML — the live-schema verify step resolves the real
    type from the database. `description` defaults to empty string;
    constraints + tests default to empty / all-False respectively.
    """

    name: str
    data_type: str | None
    description: str
    constraints: tuple[DbtConstraint, ...]
    tests: DbtColumnTests


@dataclass(frozen=True)
class DbtModelNode:
    """A `resource_type=model` node distilled to Schema Brain's shape.

    `name` is the dbt model name (logical); `identifier` is the
    physical table name (dbt's `alias` if set, else `name`). Schema
    Brain binds entities to the physical table, so `identifier` is
    what flows into `SingleTableBinding.qualified_table` downstream.

    `depends_on_sources` is the filtered-to-source.* subset of dbt's
    `depends_on.nodes`. Model→model lineage is out of scope for v1.
    """

    unique_id: str
    name: str
    database: str
    schema_name: str
    identifier: str
    description: str
    columns: tuple[DbtColumn, ...]
    depends_on_sources: tuple[str, ...]


@dataclass(frozen=True)
class DbtSourceNode:
    """A `sources.*` entry — an upstream table dbt sources from.

    Sources at v1 ride only as `upstream_source` provenance on the
    importing model; they don't materialise as standalone entities.
    The parser captures the full record anyway so future PRs can lift
    sources into entities without re-parsing.
    """

    unique_id: str
    source_name: str
    name: str
    database: str
    schema_name: str
    identifier: str


@dataclass(frozen=True)
class DbtSkipCounts:
    """Counts of non-model resource types the parser skipped.

    Surfaces in the CLI's end-of-run stderr breadcrumb so a user who
    expected their `metrics:` block to be imported sees the count of
    skipped items rather than silent absence. dbt metric import lands
    alongside the Schema Brain metric model in a later release.

    `other` counts resource types the parser doesn't recognise (e.g.
    future dbt additions like `semantic_model`). It surfaces in the
    breadcrumb so a user upgrading dbt-core to a version with new
    node types sees the count rather than silently shorter imports.
    """

    metrics: int = 0
    snapshots: int = 0
    seeds: int = 0
    analyses: int = 0
    operations: int = 0
    exposures: int = 0
    other: int = 0

    def __post_init__(self) -> None:
        # Counts come from monotonic increment-from-zero in `_parse_nodes`,
        # so negatives can only arise from programmatic mis-construction
        # (e.g. in tests). Reject loudly rather than letting a "skipped -3
        # metrics" line reach a user.
        for field_name in (
            "metrics",
            "snapshots",
            "seeds",
            "analyses",
            "operations",
            "exposures",
            "other",
        ):
            value = getattr(self, field_name)
            if value < 0:
                raise ValueError(f"{field_name} count must be >= 0 (got {value!r})")


@dataclass(frozen=True)
class DbtManifest:
    """The parsed-and-filtered view of a dbt `manifest.json`.

    `models` is a tuple (not list) so the dataclass stays hashable and
    the frozen invariant holds. `sources_by_id` is a Mapping wrapped
    in `MappingProxyType` to prevent post-construction mutation.
    """

    manifest_version: int
    dbt_project_name: str
    models: tuple[DbtModelNode, ...]
    sources_by_id: Mapping[str, DbtSourceNode]
    skipped: DbtSkipCounts

    def __post_init__(self) -> None:
        # Mapping proxy — frozen=True blocks `manifest.sources_by_id =
        # ...` but not `manifest.sources_by_id[x] = ...` if the field
        # were a plain dict. The wrap closes that bypass. Re-wrapping
        # an already-wrapped Mapping is a no-op; the early return
        # only fires on programmatic re-construction with an already-
        # wrapped mapping (the parser entrypoint always passes a plain
        # dict).
        if isinstance(self.sources_by_id, MappingProxyType):  # pragma: no cover
            return
        object.__setattr__(self, "sources_by_id", MappingProxyType(dict(self.sources_by_id)))


# ----- public entrypoint ----------------------------------------------------


def parse_dbt_manifest(path: Path) -> DbtManifest:
    """Load and parse a dbt `manifest.json` file into a `DbtManifest`.

    Raises `DbtManifestParseError` on any I/O, JSON, or
    schema-interpretation failure with a guided message that names
    the file path so a user running this against the wrong file
    sees which file the parser rejected.
    """
    raw = _read_manifest_json(path)
    _require_top_level_keys(raw, path)

    metadata = raw["metadata"]
    manifest_version = _parse_schema_version(metadata, path)
    project_name = _require_project_name(metadata, path)

    sources_by_id = _parse_sources(raw["sources"])
    column_test_aggregates = _aggregate_column_tests(raw["nodes"])
    models, skipped = _parse_nodes(raw["nodes"], column_test_aggregates)

    return DbtManifest(
        manifest_version=manifest_version,
        dbt_project_name=project_name,
        models=models,
        sources_by_id=sources_by_id,
        skipped=skipped,
    )


# ----- internals ------------------------------------------------------------


def _read_manifest_json(path: Path) -> dict:
    """Read + JSON-decode the manifest file, surfacing rich errors."""
    if not path.exists():
        raise DbtManifestParseError(
            f"dbt manifest not found at {path}; "
            "run `dbt compile` in your dbt project to produce target/manifest.json"
        )
    if not path.is_file():
        raise DbtManifestParseError(f"{path} is not a regular file (expected target/manifest.json)")
    try:
        text = path.read_text()
    except OSError as exc:  # pragma: no cover — guarded by exists()/is_file()
        raise DbtManifestParseError(f"could not read {path}: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise DbtManifestParseError(
            f"invalid JSON in {path}: {exc.msg} (line {exc.lineno} column {exc.colno})"
        ) from exc


def _require_top_level_keys(raw: dict, path: Path) -> None:
    """The three top-level keys we depend on must all be present."""
    for key in ("metadata", "nodes", "sources"):
        if key not in raw:
            raise DbtManifestParseError(
                f"manifest at {path} missing required top-level key {key!r}"
            )


def _parse_schema_version(metadata: dict, path: Path) -> int:
    """Extract + validate the manifest schema major version."""
    schema_url = metadata.get("dbt_schema_version", "")
    match = _SCHEMA_URL_RE.search(schema_url)
    if match is None:
        raise DbtManifestParseError(
            f"manifest at {path} has unrecognized dbt_schema_version "
            f"{schema_url!r}; expected URL of form "
            "'https://schemas.getdbt.com/dbt/manifest/v<N>.json'"
        )
    version = int(match.group(1))
    if version < _MIN_MANIFEST_VERSION:
        raise DbtManifestParseError(
            f"manifest at {path} uses schema v{version}; "
            f"Schema Brain requires v{_MIN_MANIFEST_VERSION} or newer "
            "(upgrade dbt-core to >=1.7 and re-run `dbt compile`)"
        )
    return version


def _require_project_name(metadata: dict, path: Path) -> str:
    """`metadata.project_name` is non-optional in every supported version."""
    project_name = metadata.get("project_name")
    if not project_name or not isinstance(project_name, str):
        raise DbtManifestParseError(f"manifest at {path} missing metadata.project_name")
    return project_name


def _parse_sources(sources_raw: dict) -> dict[str, DbtSourceNode]:
    """Convert `manifest.sources` into a unique_id → DbtSourceNode dict."""
    out: dict[str, DbtSourceNode] = {}
    for unique_id, entry in sources_raw.items():
        out[unique_id] = DbtSourceNode(
            unique_id=unique_id,
            source_name=entry.get("source_name", ""),
            name=entry.get("name", ""),
            database=entry.get("database", ""),
            schema_name=entry.get("schema", ""),
            identifier=entry.get("identifier", entry.get("name", "")),
        )
    return out


def _aggregate_column_tests(
    nodes_raw: dict,
) -> dict[tuple[str, str], DbtColumnTests]:
    """Walk `test.*` nodes; aggregate unique/not_null per (model, column).

    The returned mapping is keyed by `(model_unique_id, column_name)`.
    Tests targeting a model not in the manifest are silently dropped —
    they typically appear during partial-build states and crashing on
    them would be hostile.
    """
    aggregates: dict[tuple[str, str], dict[str, bool]] = {}
    for entry in nodes_raw.values():
        if entry.get("resource_type") != "test":
            continue
        test_name = entry.get("test_metadata", {}).get("name") or entry.get("name", "")
        target_model = entry.get("attached_node")
        column_name = entry.get("column_name")
        # Split the guards so a debug consumer can tell apart "test
        # has no attached model" (legitimate orphan) from "test has
        # no column_name" (model-level test like
        # `dbt_utils.equal_rowcount`, or a serialisation gap).
        if not target_model:
            continue
        if not column_name:
            _logger.debug(
                "dbt test node %r has no column_name; skipped (model-level "
                "test or serialisation gap).",
                entry.get("unique_id", "<unknown>"),
            )
            continue
        if test_name not in (_UNIQUE_TEST_NAME, _NOT_NULL_TEST_NAME):
            continue
        key = (target_model, column_name)
        bucket = aggregates.setdefault(key, {"unique": False, "not_null": False})
        if test_name == _UNIQUE_TEST_NAME:
            bucket["unique"] = True
        else:
            bucket["not_null"] = True
    return {
        key: DbtColumnTests(is_unique=bucket["unique"], is_not_null=bucket["not_null"])
        for key, bucket in aggregates.items()
    }


def _parse_nodes(
    nodes_raw: dict,
    column_tests: dict[tuple[str, str], DbtColumnTests],
) -> tuple[tuple[DbtModelNode, ...], DbtSkipCounts]:
    """Filter `nodes` to models; count non-model resource types."""
    models: list[DbtModelNode] = []
    counts: dict[str, int] = {field: 0 for field in _COUNTED_NODE_TYPES.values()}
    counts["other"] = 0
    for unique_id, entry in nodes_raw.items():
        resource_type = entry.get("resource_type")
        if resource_type == "model":
            try:
                models.append(_build_model_node(unique_id, entry, column_tests))
            except KeyError as exc:
                # `_build_model_node` does bare subscript access on the
                # required model fields (`name`). A malformed manifest
                # (truncated, third-party plugin output) raises KeyError
                # — wrap with the dbt unique_id so the user can locate
                # the offending node.
                raise DbtManifestParseError(
                    f"manifest node {unique_id!r} is missing required field {exc}; "
                    "the manifest may be truncated or from an unsupported dbt plugin."
                ) from exc
        elif resource_type in _COUNTED_NODE_TYPES:
            counts[_COUNTED_NODE_TYPES[resource_type]] += 1
        elif resource_type == "test":
            # `test` nodes are handled in `_aggregate_column_tests`,
            # not counted as skipped — they represent metadata about
            # other nodes, not skipped work.
            continue
        else:
            # Unknown / future resource types (e.g. `semantic_model`
            # introduced in dbt-core 1.6+, or third-party plugin
            # outputs). Counting them surfaces upgrade-time silent
            # truncation through the run summary.
            counts["other"] += 1
    skipped = DbtSkipCounts(**counts)
    # Sort for deterministic ordering — agents reading the import
    # output benefit from stable iteration order across runs of the
    # same manifest.
    models.sort(key=lambda m: m.unique_id)
    return tuple(models), skipped


def _build_model_node(
    unique_id: str,
    entry: dict,
    column_tests: dict[tuple[str, str], DbtColumnTests],
) -> DbtModelNode:
    """Convert one `nodes.<model.*>` entry into a `DbtModelNode`."""
    columns = tuple(
        _build_column(unique_id, col_name, col_raw, column_tests)
        for col_name, col_raw in entry.get("columns", {}).items()
    )
    depends_on_sources = tuple(
        node_id
        for node_id in entry.get("depends_on", {}).get("nodes", [])
        if node_id.startswith("source.")
    )
    return DbtModelNode(
        unique_id=unique_id,
        name=entry["name"],
        database=entry.get("database", ""),
        schema_name=entry.get("schema", ""),
        identifier=entry.get("alias") or entry["name"],
        description=entry.get("description", ""),
        columns=columns,
        depends_on_sources=depends_on_sources,
    )


def _build_column(
    model_unique_id: str,
    col_name: str,
    col_raw: dict,
    column_tests: dict[tuple[str, str], DbtColumnTests],
) -> DbtColumn:
    """Convert one column entry into a `DbtColumn`."""
    constraints = tuple(
        DbtConstraint(type=c.get("type", ""), name=c.get("name"))
        for c in col_raw.get("constraints", [])
    )
    tests = column_tests.get((model_unique_id, col_name), DbtColumnTests())
    return DbtColumn(
        name=col_raw.get("name", col_name),
        data_type=col_raw.get("data_type"),
        description=col_raw.get("description", ""),
        constraints=constraints,
        tests=tests,
    )


# ----- identity resolution --------------------------------------------------


@dataclass(frozen=True)
class DbtIdentityResolution:
    """The outcome of resolving a dbt model's identity column.

    `column_name` is the chosen identity column on the model.
    `method` names which priority tier matched — surfaces in the
    audit-log `entity.imported.dbt` event so an operator can answer
    "which of my dbt models rely on the older test-pair pattern vs
    the constraint syntax?".
    """

    column_name: str
    method: IdentityResolutionMethod

    def __post_init__(self) -> None:
        # Closed-Literal guard. The static type checker enforces this
        # at call sites, but `# type: ignore` callsites + future
        # programmatic construction in tests benefit from a runtime
        # check that fails loudly on an unknown method label.
        if self.method not in _VALID_IDENTITY_METHODS:
            raise ValueError(
                f"method must be one of {sorted(_VALID_IDENTITY_METHODS)} (got {self.method!r})"
            )


def resolve_dbt_identity(model: DbtModelNode) -> DbtIdentityResolution:
    """Resolve a dbt model's identity column via the 3-tier priority.

    Priority order:
      1. Single column with an explicit `primary_key` constraint.
      2. Single column with both `unique` AND `not_null` constraints.
      3. Single column with both `unique` AND `not_null` tests
         (older dbt projects pre-constraints-syntax).

    A higher tier always wins over a lower one — if a model has both
    a `primary_key` column AND a separate column with `unique` +
    `not_null` tests, the `primary_key` column resolves as the
    identity.

    Raises `DbtIdentityResolutionError` when:
      - Multiple columns match within the same tier (composite-PK
        signal — defer to v2 multi-table bindings)
      - No column matches any tier

    The function is pure — no side effects, deterministic across runs
    given the same input. The tier walk reads column order from
    `model.columns` and is stable for that reason.
    """
    tier_1 = _columns_with_constraint(model, _PRIMARY_KEY_CONSTRAINT)
    if len(tier_1) == 1:
        return DbtIdentityResolution(column_name=tier_1[0].name, method="primary_key_constraint")
    if len(tier_1) > 1:
        raise DbtIdentityResolutionError(
            _format_ambiguity_error(model, tier_1, tier_label="primary_key constraint")
        )

    tier_2 = _columns_with_constraint_pair(model, _UNIQUE_CONSTRAINT, _NOT_NULL_CONSTRAINT)
    if len(tier_2) == 1:
        return DbtIdentityResolution(
            column_name=tier_2[0].name, method="unique_not_null_constraints"
        )
    if len(tier_2) > 1:
        raise DbtIdentityResolutionError(
            _format_ambiguity_error(model, tier_2, tier_label="unique+not_null constraints")
        )

    tier_3 = _columns_with_unique_not_null_tests(model)
    if len(tier_3) == 1:
        return DbtIdentityResolution(column_name=tier_3[0].name, method="unique_not_null_tests")
    if len(tier_3) > 1:
        raise DbtIdentityResolutionError(
            _format_ambiguity_error(model, tier_3, tier_label="unique+not_null tests")
        )

    raise DbtIdentityResolutionError(_format_no_match_error(model))


def _columns_with_constraint(model: DbtModelNode, constraint_type: str) -> list[DbtColumn]:
    """Columns carrying any constraint of the given type."""
    return [col for col in model.columns if any(c.type == constraint_type for c in col.constraints)]


def _columns_with_constraint_pair(model: DbtModelNode, type_a: str, type_b: str) -> list[DbtColumn]:
    """Columns carrying BOTH of the named constraint types."""
    return [
        col
        for col in model.columns
        if any(c.type == type_a for c in col.constraints)
        and any(c.type == type_b for c in col.constraints)
    ]


def _columns_with_unique_not_null_tests(model: DbtModelNode) -> list[DbtColumn]:
    """Columns whose aggregated tests record BOTH `unique` AND `not_null`."""
    return [col for col in model.columns if col.tests.is_unique and col.tests.is_not_null]


def _format_ambiguity_error(
    model: DbtModelNode, candidates: list[DbtColumn], *, tier_label: str
) -> str:
    """Error message for the multiple-match-within-one-tier case."""
    names = ", ".join(repr(c.name) for c in candidates)
    return (
        f"dbt model {model.name!r} has multiple identity candidates at the "
        f"{tier_label} tier ({names}); Schema Brain v1 supports single-column "
        "identity only. Drop the duplicate declaration in your dbt YAML, or "
        "wait for v2 multi-table bindings."
    )


def _format_no_match_error(model: DbtModelNode) -> str:
    """Error message for the no-tier-matched case."""
    column_names = ", ".join(repr(c.name) for c in model.columns) or "(none)"
    return (
        f"dbt model {model.name!r} has no resolvable identity column "
        f"(candidates: {column_names}). Add either a primary_key constraint, "
        "a unique+not_null constraint pair, or unique+not_null tests to one "
        "column in your dbt YAML."
    )


# ----- model → Entity mapping -----------------------------------------------


@dataclass(frozen=True)
class DbtImportedEntity:
    """Envelope around an imported `Entity` carrying audit metadata.

    Mirrors the `EntityCandidate` shape used by the LLM-suggest
    pipeline: the persisted-clean `Entity` rides at the centre, and
    the import-time metadata (`dbt_unique_id`,
    `identity_resolution_method`, `upstream_sources`) flows into the
    audit-log row when the entity is written.

    Once `--apply` runs, only the inner `Entity` persists to the
    `entities` table. The envelope fields land in the audit-log
    `entity.imported.dbt` event, never in the entity row itself —
    keeping the canonical entity record honest.

    `upstream_sources` is a tuple of qualified table names (e.g.
    `("raw.users",)`) — the schema-qualified physical tables this
    dbt model `depends_on`. Schema Brain doesn't materialise source
    nodes as entities at v1, but the lineage signal lives on the
    envelope for the audit log + future downstream tooling.
    """

    entity: Entity
    dbt_unique_id: str
    identity_resolution_method: IdentityResolutionMethod
    upstream_sources: tuple[str, ...]

    def __post_init__(self) -> None:
        # The upstream `DbtIdentityResolution` constructor already
        # validates the method label, but a future caller (or test
        # double) might build a `DbtImportedEntity` directly. Mirror
        # the same closed-Literal guard so the audit log can never
        # carry an unknown method value.
        if self.identity_resolution_method not in _VALID_IDENTITY_METHODS:
            raise ValueError(
                f"identity_resolution_method must be one of "
                f"{sorted(_VALID_IDENTITY_METHODS)} "
                f"(got {self.identity_resolution_method!r})"
            )


def dbt_model_to_entity(model: DbtModelNode, manifest: DbtManifest) -> DbtImportedEntity:
    """Map a parsed dbt model + resolved identity → a Schema Brain `Entity`.

    Composes the identity resolver with `Entity` construction.
    Raises:
      - `DbtIdentityResolutionError` when identity resolution fails
        (caller decides whether to skip + log or escalate).
      - `DbtEntityMappingError` when the dbt model's name or the
        compiled `schema.identifier` don't satisfy Schema Brain's
        identifier-shape constraints. Wraps the underlying
        `Entity` / `SingleTableBinding` `ValueError` with dbt
        context (the model's `unique_id`).

    Pure / deterministic: no side effects, identical inputs produce
    identical envelopes.
    """
    resolution = resolve_dbt_identity(model)
    qualified_table = f"{model.schema_name}.{model.identifier}"
    try:
        entity = Entity(
            name=model.name,
            description=model.description,
            binding=SingleTableBinding(qualified_table=qualified_table),
            identity=resolution.column_name,
            origin="dbt_import",
        )
    except ValueError as exc:
        raise DbtEntityMappingError(
            f"dbt model {model.unique_id!r} does not map to a valid Schema Brain entity: {exc}"
        ) from exc
    upstream_sources = _resolve_upstream_sources(model, manifest)
    return DbtImportedEntity(
        entity=entity,
        dbt_unique_id=model.unique_id,
        identity_resolution_method=resolution.method,
        upstream_sources=upstream_sources,
    )


def _resolve_upstream_sources(model: DbtModelNode, manifest: DbtManifest) -> tuple[str, ...]:
    """Resolve `depends_on_sources` IDs to qualified-table strings.

    A `depends_on` entry that has no matching `sources_by_id` record
    is dropped (importing the model minus the lineage entry is better
    than failing the whole entity), but emits a WARNING log line so a
    malformed-manifest case isn't silently invisible.
    """
    resolved: list[str] = []
    for src_id in model.depends_on_sources:
        src = manifest.sources_by_id.get(src_id)
        if src is None:
            _logger.warning(
                "dbt model %r references source %r which has no entry in "
                "manifest.sources; upstream lineage entry dropped.",
                model.unique_id,
                src_id,
            )
            continue
        resolved.append(f"{src.schema_name}.{src.identifier}")
    return tuple(resolved)


# ----- live-schema verification ---------------------------------------------


def verify_against_live_schema(
    imported: DbtImportedEntity,
    model: DbtModelNode,
    source: DataSource,
) -> None:
    """Confirm a dbt-imported entity matches the live source schema.

    Performs exactly one `DataSource.get_table` call. Raises
    `DbtSchemaDriftError` listing ALL drift points at once (missing
    table, missing columns, nullable identity) so the user can fix
    in one round-trip. On success returns `None` and leaves the
    `imported` envelope untouched — verify is read-only.

    Drift rules:
      - The compiled `schema.identifier` must resolve to a live
        table via `source.get_table`.
      - Every dbt-declared column name must appear in the live
        table's column list. Extra live columns NOT in dbt are
        ignored — dbt owns its column list; live can carry
        unmodeled columns.
      - The identity column (resolved by the identity-resolution
        logic and carried on the `Entity`) must have `nullable=False`
        in live. A nullable identity can't anchor metrics later.
    """
    schema = model.schema_name
    identifier = model.identifier
    qualified = f"{schema}.{identifier}"
    try:
        live = source.get_table(identifier, schema)
    except TableNotFoundError as exc:
        raise DbtSchemaDriftError(
            f"dbt model {model.unique_id!r} compiles to physical table "
            f"{qualified!r} which does not exist in the live source database; "
            "run `dbt run` to materialize the model before importing."
        ) from exc

    drift_messages: list[str] = []
    live_columns_by_name = {c.name: c for c in live.columns}
    dbt_column_names = [c.name for c in model.columns]
    missing_columns = [col for col in dbt_column_names if col not in live_columns_by_name]
    if missing_columns:
        formatted = ", ".join(repr(c) for c in missing_columns)
        drift_messages.append(
            f"column(s) {formatted} declared in the dbt manifest are missing "
            f"in the live table {qualified!r}; run `dbt run` to refresh the "
            "materialization."
        )

    identity = imported.entity.identity
    identity_live = live_columns_by_name.get(identity)
    if identity_live is None:
        # The identity column may have been resolved from a tier-1
        # `primary_key` constraint that lives in the manifest's
        # `constraints` block but never appeared in the `columns`
        # block (legal in dbt). The missing-columns check above
        # won't catch that case because the identity isn't in
        # `dbt_column_names`. Explicit check here closes the gap so
        # an entity can't be imported with an identity column that
        # doesn't exist in live.
        drift_messages.append(
            f"identity column {identity!r} on {qualified!r} does not exist in "
            "the live schema; a metric anchored on this entity would fail at "
            "query time. Add the column upstream or pick a different identity."
        )
    elif identity_live.nullable:
        drift_messages.append(
            f"identity column {identity!r} on {qualified!r} is nullable in "
            "the live schema; a nullable identity can't reliably anchor "
            "metrics. Add a NOT NULL constraint upstream or pick a different "
            "identity column."
        )

    if drift_messages:
        joined = " ".join(drift_messages)
        raise DbtSchemaDriftError(
            f"dbt model {model.unique_id!r} drifted from the live source: {joined}"
        )


# ----- diff + write driver --------------------------------------------------

# Closed Literal naming why a model couldn't be imported. Each label
# corresponds to one of the three import-pipeline exceptions; the run
# summary buckets skips by these values + the audit log records them
# for forensics.
SkipReason = Literal["identity_resolution", "mapping", "schema_drift"]
_VALID_SKIP_REASONS: frozenset[str] = frozenset(get_args(SkipReason))


@dataclass(frozen=True)
class DbtImportSkip:
    """One model that the driver couldn't import, with the cause.

    Carries the dbt `unique_id` (so the user can cross-reference
    against `dbt ls`), the closed-Literal `reason` (audit-log
    bucket), and the underlying exception's message for the
    stderr breadcrumb.
    """

    dbt_unique_id: str
    reason: SkipReason
    message: str

    def __post_init__(self) -> None:
        if self.reason not in _VALID_SKIP_REASONS:
            raise ValueError(
                f"reason must be one of {sorted(_VALID_SKIP_REASONS)} (got {self.reason!r})"
            )


@dataclass(frozen=True)
class DbtWriteFailure:
    """An entity that classified into a write bucket but then failed to land.

    The two flavours of write failure expected at v1 are FK
    violations (bound table not in store) and dbt-guard refusals
    (which shouldn't fire — the planner already checked the
    existing origin — but the catch is defensive against
    concurrent writers).
    """

    entity_name: str
    message: str


@dataclass(frozen=True)
class DbtImportPlan:
    """Pre-write classification of a manifest against the store.

    Four mutually-exclusive write buckets:
      - `to_add`: model has no matching entity in the store.
      - `to_update`: existing entity has `origin=dbt_import` (the
        idempotent re-import path; dbt is already the owner).
      - `to_take_ownership`: existing entity has `origin=manual`
        or `suggested`; the import will flip the row to
        `dbt_import`. Pairs each envelope with the previous origin
        for the stderr breadcrumb.
      - `skipped`: model couldn't be mapped + verified.

    `orphans` lists names of `origin=dbt_import` rows that exist
    in the store but no longer correspond to any manifest model.
    The driver does NOT auto-delete these — the user can decide.

    `skip_counts` carries the parser's non-model resource counts
    (metrics, snapshots, etc.) so the CLI can print one summary
    breadcrumb.
    """

    dbt_project_name: str
    to_add: tuple[DbtImportedEntity, ...]
    to_update: tuple[DbtImportedEntity, ...]
    to_take_ownership: tuple[tuple[DbtImportedEntity, Origin], ...]
    orphans: tuple[str, ...]
    skipped: tuple[DbtImportSkip, ...]
    skip_counts: DbtSkipCounts


@dataclass(frozen=True)
class DbtImportResult:
    """Post-write summary — `plan` plus any per-entity write failures."""

    plan: DbtImportPlan
    write_failures: tuple[DbtWriteFailure, ...]


def plan_dbt_import(
    manifest: DbtManifest,
    source: DataSource,
    store: Store,
    *,
    source_connection_id: str,
) -> DbtImportPlan:
    """Classify each manifest model against the store + live source.

    Pure read — no entity writes. Per model:
      1. Map to `DbtImportedEntity`. Identity-resolution or
         entity-mapping failures land in `skipped`.
      2. Verify against the live `source` schema. Schema-drift
         failures also land in `skipped`.
      3. Look up the existing entity (if any) in the store; classify
         into `to_add` / `to_update` / `to_take_ownership` based on
         the previous origin.

    Orphans are existing `dbt_import` entities in the store whose
    names don't appear in `manifest.models` at all. A model that
    failed identity resolution this run is NOT classified as an
    orphan — orphan-ness is defined against the manifest's full
    declared model set, not against the subset that successfully
    mapped this run.

    Results are sorted deterministically by entity name within each
    bucket so the stderr breadcrumb + audit log are stable across
    runs of the same manifest.

    Performance: one `list_entities` call up front + a dict lookup
    per model. No per-model `get_entity` round-trip — both the
    classification and orphan detection share the same single scan.
    """
    # One sweep — the dict feeds both classification and orphan
    # detection, eliminating N `get_entity` round-trips for an
    # N-model manifest.
    existing_by_name: dict[str, Entity] = {
        e.name: e for e in store.list_entities(source_connection_id=source_connection_id)
    }

    to_add: list[DbtImportedEntity] = []
    to_update: list[DbtImportedEntity] = []
    to_take_ownership: list[tuple[DbtImportedEntity, Origin]] = []
    skipped: list[DbtImportSkip] = []

    for model in manifest.models:
        try:
            imported = dbt_model_to_entity(model, manifest)
        except DbtIdentityResolutionError as exc:
            skipped.append(
                DbtImportSkip(
                    dbt_unique_id=model.unique_id,
                    reason="identity_resolution",
                    message=str(exc),
                )
            )
            continue
        except DbtEntityMappingError as exc:
            skipped.append(
                DbtImportSkip(
                    dbt_unique_id=model.unique_id,
                    reason="mapping",
                    message=str(exc),
                )
            )
            continue
        try:
            verify_against_live_schema(imported, model, source)
        except DbtSchemaDriftError as exc:
            skipped.append(
                DbtImportSkip(
                    dbt_unique_id=model.unique_id,
                    reason="schema_drift",
                    message=str(exc),
                )
            )
            continue

        existing = existing_by_name.get(imported.entity.name)
        if existing is None:
            to_add.append(imported)
        elif existing.origin == "dbt_import":
            to_update.append(imported)
        else:
            to_take_ownership.append((imported, existing.origin))

    # Orphan detection: dbt_import rows in store whose names don't
    # appear in the manifest at all. Built from `manifest.models`
    # directly (not from successfully-mapped models) so a model
    # that failed identity resolution this run doesn't generate a
    # spurious orphan warning for its own previously-imported row.
    manifest_model_names = {m.name for m in manifest.models}
    orphans = [
        name
        for name, entity in existing_by_name.items()
        if entity.origin == "dbt_import" and name not in manifest_model_names
    ]

    return DbtImportPlan(
        dbt_project_name=manifest.dbt_project_name,
        to_add=tuple(sorted(to_add, key=lambda e: e.entity.name)),
        to_update=tuple(sorted(to_update, key=lambda e: e.entity.name)),
        to_take_ownership=tuple(sorted(to_take_ownership, key=lambda p: p[0].entity.name)),
        orphans=tuple(sorted(orphans)),
        skipped=tuple(sorted(skipped, key=lambda s: s.dbt_unique_id)),
        skip_counts=manifest.skipped,
    )


def apply_dbt_import_plan(
    plan: DbtImportPlan, store: Store, *, source_connection_id: str
) -> DbtImportResult:
    """Execute the plan — write each entity, emit one audit log row per write.

    Walks `to_add` + `to_update` + `to_take_ownership` and calls
    `store.write_entity()` per envelope. On `sqlite3.IntegrityError`
    (FK violation — bound table not indexed) or
    `DbtOwnedEntityError` (defensive — planner already filtered),
    the per-entity failure is captured as a `DbtWriteFailure` and
    the apply continues.

    Orphans are left in place. Skipped models are not retried
    (already in the plan's skipped bucket).

    Emits an INFO-level log per successful write with the structured
    audit fields. These become the audit-log row when the persistent
    audit table lands.
    """
    # Each `_write_one` call returns either `None` (success) or a
    # `DbtWriteFailure` (FK violation / dbt-guard refusal). Collect
    # via list comprehension + None filter — keeps the per-bucket
    # control flow uniform and lets the test surface cover the
    # filter once rather than three times.
    raw_failures: list[DbtWriteFailure | None] = []
    for envelope in plan.to_add:
        raw_failures.append(
            _write_one(
                envelope,
                store,
                source_connection_id=source_connection_id,
                event="entity.imported.dbt.added",
                previous_origin=None,
                dbt_project_name=plan.dbt_project_name,
            )
        )
    for envelope in plan.to_update:
        raw_failures.append(
            _write_one(
                envelope,
                store,
                source_connection_id=source_connection_id,
                event="entity.imported.dbt.updated",
                previous_origin="dbt_import",
                dbt_project_name=plan.dbt_project_name,
            )
        )
    for envelope, prior in plan.to_take_ownership:
        raw_failures.append(
            _write_one(
                envelope,
                store,
                source_connection_id=source_connection_id,
                event="entity.imported.dbt.ownership_transferred",
                previous_origin=prior,
                dbt_project_name=plan.dbt_project_name,
            )
        )

    write_failures = tuple(f for f in raw_failures if f is not None)
    return DbtImportResult(plan=plan, write_failures=write_failures)


def _write_one(
    envelope: DbtImportedEntity,
    store: Store,
    *,
    source_connection_id: str,
    event: str,
    previous_origin: Origin | None,
    dbt_project_name: str,
) -> DbtWriteFailure | None:
    """Write one entity, returning a `DbtWriteFailure` on FK / guard violations."""
    try:
        store.write_entity(envelope.entity, source_connection_id=source_connection_id)
    except sqlite3.IntegrityError:
        # The only IntegrityError this call path can raise is the
        # bound-table FK ("entity binds to schema.table which isn't
        # in the `tables` index"). Surface as a write failure named
        # in the result; apply continues with the next entity.
        return DbtWriteFailure(
            entity_name=envelope.entity.name,
            message=(
                f"entity {envelope.entity.name!r} binds to table "
                f"{envelope.entity.binding.qualified_table!r} which is not in the "
                f"tables index for source {source_connection_id!r}. Run "
                "`schemabrain index <source-url>` with the SAME source URL before "
                "importing."
            ),
        )
    except DbtOwnedEntityError as exc:
        # Defensive: the planner already checks `existing.origin` and
        # routes non-dbt origins through `to_take_ownership` (which
        # the store contract allows). A `DbtOwnedEntityError` here
        # means a concurrent writer flipped the row between plan and
        # apply. Surface as a write failure rather than crashing the
        # whole apply.
        return DbtWriteFailure(entity_name=envelope.entity.name, message=str(exc))
    _logger.info(
        event,
        extra={
            "entity_name": envelope.entity.name,
            "dbt_unique_id": envelope.dbt_unique_id,
            "dbt_project_name": dbt_project_name,
            "previous_origin": previous_origin,
            "identity_resolution_method": envelope.identity_resolution_method,
            "upstream_sources": envelope.upstream_sources,
        },
    )
    return None
