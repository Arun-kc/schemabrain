#!/usr/bin/env python3
"""Propagate the canonical positioning into non-importable surfaces.

The single source of truth is :mod:`schemabrain.positioning`. Python surfaces
(``cli.py`` banner, ``mcp/server.py`` instructions) import those constants
directly and therefore cannot drift. Surfaces that *can't* import Python —
``pyproject.toml``, and (in later passes) the README + docs — are rewritten to
match by this script.

Usage::

    python scripts/sync_positioning.py           # rewrite files in place
    python scripts/sync_positioning.py --check    # exit 1 if anything is stale

``--check`` runs in CI (``tests/test_positioning_sync.py``) so a forgotten sync
fails the build instead of shipping a stale tagline. Each rule's regex matches
*any* current value AND reproduces a value the same regex re-matches, so the
rewrite is idempotent and self-heals from whatever the file currently holds.

Scope: this is the positioning analogue of ``scripts/sync_versions.py``. As the
broader positioning doc-sweep lands, add README/docs rules below. Surfaces that
are point-in-time records are deliberately NEVER swept: the CHANGELOG, ADRs
(``docs/adr/``), ``docs/internal/`` (gitignored working notes), the rendered
hero/OG SVGs, and ``web/app/page.tsx`` (the landing owns its own copy, mirrored
from ``web/lib/positioning.ts`` under the TS↔Python parity test).
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from schemabrain import positioning

_REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Rule:
    """A single positioning rewrite rule.

    ``glob`` is resolved relative to the repo root (a literal path when it
    contains no ``*``). ``pattern`` matches the full phrase for *any* current
    value; ``template`` reproduces that phrase with ``{value}`` substituted by
    ``value`` — chosen so the regex re-matches the output (idempotence).
    """

    glob: str
    pattern: str
    template: str
    value: str
    description: str


# Order is irrelevant — rules are independent and idempotent.
RULES: tuple[Rule, ...] = (
    Rule(
        glob="pyproject.toml",
        # The `[project].description` line lives at column 0; match it alone.
        pattern=r'^description = "[^"]*"',
        template='description = "{value}"',
        value=positioning.SHORT_DESCRIPTION,
        description="pyproject project.description",
    ),
)


def _rule_targets(rule: Rule, root: Path) -> list[Path]:
    if "*" in rule.glob:
        return sorted(root.glob(rule.glob))
    target = root / rule.glob
    return [target] if target.is_file() else []


def _rewrite(text: str, rule: Rule) -> tuple[str, int]:
    replacement = rule.template.format(value=rule.value)
    new_text, n = re.subn(rule.pattern, lambda _m: replacement, text, flags=re.MULTILINE)
    return new_text, n


@dataclass(frozen=True)
class Drift:
    path: str
    description: str


def sync(*, write: bool, root: Path = _REPO_ROOT) -> list[Drift]:
    """Apply every rule. Return the files that were (or, in check mode, would
    be) changed — i.e. the stale references."""
    drifts: list[Drift] = []
    # Accumulate per-file edits so multiple rules on one file write once.
    edits: dict[Path, str] = {}
    for rule in RULES:
        for path in _rule_targets(rule, root):
            original = edits.get(path, path.read_text())
            updated, n = _rewrite(original, rule)
            if n and updated != original:
                edits[path] = updated
                drifts.append(Drift(str(path.relative_to(root)), rule.description))
    if write:
        for path, updated in edits.items():
            path.write_text(updated)
    return drifts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sync positioning references from schemabrain.positioning"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report stale references and exit 1 without writing (CI mode).",
    )
    args = parser.parse_args(argv)

    drifts = sync(write=not args.check)

    if args.check:
        if drifts:
            print("Stale positioning references:", file=sys.stderr)
            for d in drifts:
                print(f"  {d.path} — {d.description}", file=sys.stderr)
            print("\nRun `python scripts/sync_positioning.py` to fix.", file=sys.stderr)
            return 1
        print("All positioning references match schemabrain.positioning.")
        return 0

    if drifts:
        print(f"Synced {len(drifts)} reference(s):")
        for d in drifts:
            print(f"  {d.path} — {d.description}")
    else:
        print("Already in sync; nothing to do.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
