"""Content-addressable fingerprints for cache-aware re-indexing.

A fingerprint is a SHA-256 hex digest derived from a deterministic JSON
canonicalization of the inputs that should invalidate cached LLM
enrichment for an object.

Two flavours per column:

- **Structural fingerprint** — hashes the immutable database identity:
  name, type, nullability, default, ordinal position, PK membership, and
  the sorted list of FK targets. Changes here mean the schema itself
  changed.

- **Semantic fingerprint** — structural + the column's sorted sample
  values. Changes here mean the *meaning* of the column may have shifted
  even when the schema didn't (new sample distribution, new domain).

Plus a table-level structural fingerprint that aggregates per-column
structural hashes for a quick "did anything in this table change?" check.

Determinism is enforced by:

- `json.dumps(..., sort_keys=True, separators=(",", ":"))` — no
  whitespace drift, dict-key order pinned.
- All collection inputs (FK targets, sample values) are sorted before
  being included.
- UTF-8 encoding so non-ASCII content hashes consistently.

The output of every function in this module is a 64-character lowercase
hex string. Callers MAY persist these strings as cache keys; if a
serialization change ever produces a different hash for the same input,
that is a breaking change and demands a `_SCHEMA_VERSION` bump in the
store.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from schemabrain.core.models import Column, Table
from schemabrain.profiler.stats import ColumnStats


def fk_targets_for_column(table: Table, column_name: str) -> tuple[str, ...]:
    """Return the sorted, deduped list of FK targets reachable from `column_name`.

    Each target is rendered as `"<schema>.<table>.<column>"` so cross-schema
    references are unambiguous. Composite FKs are decomposed positionally:
    each (source, target) pair contributes one target string.
    """
    targets: set[str] = set()
    for fk in table.foreign_keys:
        for src, tgt in zip(fk.source_columns, fk.target_columns, strict=True):
            if src == column_name:
                targets.add(f"{fk.target_schema}.{fk.target_table}.{tgt}")
    return tuple(sorted(targets))


def column_structural_fingerprint(col: Column, fk_targets: tuple[str, ...]) -> str:
    """SHA-256 hex of the column's immutable structural identity.

    `fk_targets` is taken as a parameter rather than derived from a
    `Table` so this function can be unit-tested without constructing a
    full table and so callers can substitute query-log-inferred join
    partners in future versions.
    """
    payload = {
        "name": col.name,
        "data_type": col.data_type,
        "nullable": col.nullable,
        "default": col.default,
        "position": col.ordinal_position,
        "is_pk": col.is_primary_key,
        "fk_targets": sorted(fk_targets),
    }
    return _hash_json(payload)


def column_semantic_fingerprint(
    col: Column,
    fk_targets: tuple[str, ...],
    stats: ColumnStats | None,
) -> str:
    """SHA-256 hex of structural identity + profiler-derived semantic signal.

    Includes the column's structural fingerprint plus its sorted sample
    values. Raw counts (`total_rows`, `null_count`, `distinct_count`) are
    deliberately NOT in the hash — a column whose row count grew but
    whose representative samples didn't shouldn't churn through LLM
    enrichment again.

    `stats=None` produces a distinct fingerprint from any provided stats
    object so an "unprofiled" cache entry can never collide with a
    "profiled but empty" one.
    """
    structural = column_structural_fingerprint(col, fk_targets)
    if stats is None:
        samples_payload = None
        shapes_payload = None
    else:
        samples_payload = sorted(stats.sample_values)
        # Include shape_patterns too: a shape shift (e.g. column flips
        # from `999-99-9999` to `aaa@aaaa.aaa`) is a strong semantic
        # signal even when the literal sample set didn't overlap before.
        shapes_payload = sorted(stats.shape_patterns)
    payload = {
        "structural": structural,
        "samples": samples_payload,
        "shapes": shapes_payload,
    }
    return _hash_json(payload)


def table_structural_fingerprint(table: Table) -> str:
    """SHA-256 hex aggregating the structural fingerprint of every column.

    Useful as a fast "did anything change in this table?" gate before
    diffing column-by-column. Includes `schema_name` and `name` so a
    rename in either dimension invalidates.
    """
    # Sort columns by ordinal_position before hashing. The `Table` model
    # enforces unique positions but does NOT enforce that the tuple is
    # already sorted — a connector could yield columns in a different
    # tuple order between runs and produce different hashes for the same
    # logical schema.
    columns_in_order = sorted(table.columns, key=lambda c: c.ordinal_position)
    column_fps = [
        column_structural_fingerprint(col, fk_targets_for_column(table, col.name))
        for col in columns_in_order
    ]
    payload = {
        "schema": table.schema_name,
        "name": table.name,
        "columns": column_fps,
    }
    return _hash_json(payload)


def _hash_json(payload: Any) -> str:
    serialized = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        # ensure_ascii=False so non-ASCII column names hash as their actual
        # UTF-8 codepoints rather than `\uXXXX` escape sequences. Both are
        # deterministic, but flipping this flag silently changes every hash.
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()
