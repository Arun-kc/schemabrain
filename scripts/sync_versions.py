#!/usr/bin/env python3
"""Propagate the canonical package version into README + docs.

Single source of truth is ``[project].version`` in ``pyproject.toml``.
Every *current-version* reference in the published surface (README +
docs) is rewritten to match it. Historical / point-in-time references
are deliberately NOT touched: the CHANGELOG (one pinned entry per
release), ADRs (immutable records), and ``docs/internal/`` (gitignored
working notes).

Usage::

    python scripts/sync_versions.py           # rewrite files in place
    python scripts/sync_versions.py --check    # exit 1 if anything is stale

The release flow bumps ``pyproject.toml`` then runs this script; the
``--check`` mode runs in CI (``tests/test_docs_version_sync.py``) so a
forgotten sync fails the build instead of shipping a stale version.

Each rule's regex matches *any* version, so the rewrite self-heals from
whatever the file currently holds to the canonical version.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Rule:
    """A single version-reference rewrite rule.

    ``glob`` is resolved relative to the repo root (a literal path when
    it contains no ``*``). ``pattern`` matches the full version-bearing
    phrase for *any* version; ``template`` reproduces that phrase with
    ``{v}`` (full version) / ``{mm}`` (major.minor) substituted.
    """

    glob: str
    pattern: str
    template: str
    description: str


# Order is irrelevant — rules are independent and idempotent.
RULES: tuple[Rule, ...] = (
    Rule(
        "README.md",
        r"\*\*Status: \d+\.\d+\.\d+ \(alpha\)\.\*\*",
        "**Status: {v} (alpha).**",
        "README status line",
    ),
    Rule(
        "README.md",
        r"SchemaBrain v\d+\.\d+ ships stdio only",
        "SchemaBrain v{mm} ships stdio only",
        "README transport-support line",
    ),
    Rule(
        "docs/landscape.md",
        r"Active — \d+\.\d+\.\d+",
        "Active — {v}",
        "landscape comparison-table status",
    ),
    Rule(
        "docs/setup/*.md",
        r"schemabrain==\d+\.\d+\.\d+",
        "schemabrain=={v}",
        "MCP host-config install pin",
    ),
    Rule(
        "docs/setup/docker.md",
        r"`:\d+\.\d+\.\d+`",
        "`:{v}`",
        "Docker image-tag example",
    ),
)


def canonical_version(root: Path = _REPO_ROOT) -> str:
    """Return ``[project].version`` from pyproject.toml — the SSOT."""
    raw = (root / "pyproject.toml").read_text()
    return tomllib.loads(raw)["project"]["version"]


def _major_minor(version: str) -> str:
    parts = version.split(".")
    return f"{parts[0]}.{parts[1]}"


def _rule_targets(rule: Rule, root: Path) -> list[Path]:
    if "*" in rule.glob:
        return sorted(root.glob(rule.glob))
    target = root / rule.glob
    return [target] if target.is_file() else []


def _rewrite(text: str, rule: Rule, version: str) -> tuple[str, int]:
    replacement = rule.template.format(v=version, mm=_major_minor(version))
    new_text, n = re.subn(rule.pattern, lambda _m: replacement, text)
    return new_text, n


@dataclass(frozen=True)
class Drift:
    path: str
    description: str


def sync(*, write: bool, root: Path = _REPO_ROOT) -> list[Drift]:
    """Apply every rule. Return the list of files that were (or, in
    check mode, would be) changed — i.e. the stale references.
    """
    version = canonical_version(root)
    drifts: list[Drift] = []
    # Accumulate per-file edits so multiple rules on one file write once.
    edits: dict[Path, str] = {}
    for rule in RULES:
        for path in _rule_targets(rule, root):
            original = edits.get(path, path.read_text())
            updated, n = _rewrite(original, rule, version)
            if n and updated != original:
                edits[path] = updated
                drifts.append(Drift(str(path.relative_to(root)), rule.description))
    if write:
        for path, updated in edits.items():
            path.write_text(updated)
    return drifts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync version references from pyproject.toml")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report stale references and exit 1 without writing (CI mode).",
    )
    args = parser.parse_args(argv)

    version = canonical_version()
    drifts = sync(write=not args.check)

    if args.check:
        if drifts:
            print(f"Stale version references (canonical is {version}):", file=sys.stderr)
            for d in drifts:
                print(f"  {d.path} — {d.description}", file=sys.stderr)
            print("\nRun `python scripts/sync_versions.py` to fix.", file=sys.stderr)
            return 1
        print(f"All version references match pyproject ({version}).")
        return 0

    if drifts:
        print(f"Synced {len(drifts)} reference(s) to {version}:")
        for d in drifts:
            print(f"  {d.path} — {d.description}")
    else:
        print(f"Already in sync at {version}; nothing to do.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
