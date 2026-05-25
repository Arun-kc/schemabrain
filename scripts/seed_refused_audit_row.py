"""Seed one chain-valid refused row into ``mcp_audit`` for demo rendering.

Demo recordings (``docs/assets/demo-pagila.tape``) need at least one
row with ``status=refused`` and ``pii_categories=credential`` visible
under ``schemabrain audit tail`` so Act 7 has something to display
without depending on a live Claude Desktop walkthrough actually
triggering a credential-PII refusal on every retake.

This helper appends one row that goes through the canonical
``AuditWriter.write`` path: same canonical serialiser, same chain
hash, same fingerprint version, same SQL CHECK constraints, same
DDL. The row is indistinguishable from one a real ``get_metric``
refusal would have produced, so ``schemabrain audit verify`` over
the chain returns exit 0 and the rendered ``audit tail`` row carries
the realistic shape.

The helper is intentionally narrow:

* Only writes to the ``mcp_audit`` table; never reads or touches any
  semantic-layer table.
* Refuses to run unless the target store already has an audit
  schema (i.e. ``schemabrain init`` has been run against it). This
  prevents the helper from being mistaken for an init substitute.
* Never used at runtime by the MCP server. It exists exclusively to
  populate demo fixtures.

Usage::

    python scripts/seed_refused_audit_row.py \\
        --store-path ./pagila.db \\
        --source-id pagila

Exit codes::

    0  one row appended; row id + chain-hash prefix printed to stdout
    2  operational error (store missing, audit schema missing, etc.)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make ``schemabrain`` importable when this script is invoked directly
# from the repo root (``python scripts/seed_refused_audit_row.py``).
# When invoked via pytest the package is already on sys.path from the
# editable install — this path mutation is a no-op in that case.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from schemabrain.audit.fingerprint import FINGERPRINT_VERSION  # noqa: E402
from schemabrain.audit.writer import AuditRow, AuditWriter  # noqa: E402
from schemabrain.pii.categories import PIICategory  # noqa: E402

# The tool name + refusal shape mirror what a real credential-PII
# refusal would produce against Pagila: ``get_metric`` refused with
# ``pii_blocked`` because the agent attempted to group_by a
# credential-tagged column (``staff.password``). Keeping these
# constants module-level so the seeded row stays aligned with the
# narration in the Pagila demo tape if the row's shape ever needs
# to track a refusal-envelope change.
_SEED_TOOL_NAME = "get_metric"
_SEED_REFUSAL_REASON = "pii_blocked"
_SEED_PII_CATEGORY: PIICategory = "credential"


def seed_one_refused_row(
    *,
    store_path: Path,
    source_connection_id: str,
    tool_name: str = _SEED_TOOL_NAME,
    pii_category: PIICategory = _SEED_PII_CATEGORY,
) -> AuditRow:
    """Append one chain-valid refused row and return the materialised AuditRow.

    Raises ``FileNotFoundError`` if the store does not exist yet —
    operator must run ``schemabrain init`` first so the audit schema
    DDL is applied. The DDL is idempotent and ``AuditWriter`` would
    create the table anyway, but failing here surfaces the wrong-path
    mistake (e.g. typo in ``--store-path``) instead of silently
    seeding a brand-new ``audit.db`` that no other tool will read.
    """
    rows = seed_refused_rows(
        store_path=store_path,
        source_connection_id=source_connection_id,
        tool_name=tool_name,
        pii_category=pii_category,
        count=1,
    )
    return rows[0]


def seed_refused_rows(
    *,
    store_path: Path,
    source_connection_id: str,
    tool_name: str = _SEED_TOOL_NAME,
    pii_category: PIICategory = _SEED_PII_CATEGORY,
    count: int = 1,
) -> list[AuditRow]:
    """Append ``count`` chain-valid refused rows and return them.

    Used by the Pagila demo tape to bulk-seed the audit table after
    Act 2's ``init`` clears it. Single ``AuditWriter`` open so the
    chain stays continuous and each row's ``chain_hash`` references
    the previous one (not ``GENESIS_CHAIN_HASH`` per call).
    """
    if count < 1:
        raise ValueError(f"count must be >= 1, got {count}")
    if not store_path.exists():
        raise FileNotFoundError(
            f"store not found at {store_path!s}; run `schemabrain init "
            f"--store-path {store_path!s}` first so the audit schema "
            f"exists before seeding the demo row."
        )

    draft: dict[str, object] = {
        "source_connection_id": source_connection_id,
        "caller_id": None,
        "tool_name": tool_name,
        "status": "refused",
        "refusal_reason": _SEED_REFUSAL_REASON,
        "cost_class": "refused",
        "pii_categories": frozenset({pii_category}),
        "ast_shape_hash": None,
        "rule_id": None,
        "fingerprint_version": FINGERPRINT_VERSION,
    }

    writer = AuditWriter(store_path)
    try:
        return [writer.write(draft) for _ in range(count)]
    finally:
        writer.close()


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="seed_refused_audit_row",
        description=(
            "Append one chain-valid refused row to mcp_audit for demo "
            "rendering. Used exclusively by docs/assets/demo-pagila.tape."
        ),
    )
    parser.add_argument(
        "--store-path",
        required=True,
        type=Path,
        help="path to the SchemaBrain SQLite store (e.g. ./pagila.db)",
    )
    parser.add_argument(
        "--source-id",
        required=True,
        help="source_connection_id stamped on the seeded row (e.g. pagila)",
    )
    parser.add_argument(
        "--tool-name",
        default=_SEED_TOOL_NAME,
        help=f"tool name on the seeded row (default: {_SEED_TOOL_NAME})",
    )
    parser.add_argument(
        "--pii-category",
        default=_SEED_PII_CATEGORY,
        help=(
            f"PII category on the seeded row (default: {_SEED_PII_CATEGORY}). "
            f"Must match a value declared in schemabrain.pii.categories.PIICategory."
        ),
    )
    parser.add_argument(
        "--count",
        type=int,
        default=1,
        help=(
            "number of refused rows to seed (default: 1). All rows share the "
            "same tool name + PII category but are chained through a single "
            "writer so chain_hash references the previous row."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    try:
        rows = seed_refused_rows(
            store_path=args.store_path,
            source_connection_id=args.source_id,
            tool_name=args.tool_name,
            pii_category=args.pii_category,
            count=args.count,
        )
    except FileNotFoundError as exc:
        print(f"seed_refused_audit_row: {exc}", file=sys.stderr)
        return 2
    except (ValueError, RuntimeError) as exc:
        # ValueError catches canonical-row schema drift / unknown PII
        # category / corrupted prev chain hash / count<1. RuntimeError
        # catches the "writer is closed" guard. All operator-fixable.
        print(f"seed_refused_audit_row: {exc}", file=sys.stderr)
        return 2

    first, last = rows[0], rows[-1]
    print(
        f"seeded {len(rows)} row(s) "
        f"ids={first.id}..{last.id} status={first.status} "
        f"refusal_reason={first.refusal_reason} "
        f"pii_categories={first.pii_categories or '(none)'} "
        f"chain_hash={last.chain_hash[:4].hex()}…"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
