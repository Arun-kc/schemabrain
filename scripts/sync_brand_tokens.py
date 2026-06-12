#!/usr/bin/env python3
"""Mirror the dashboard's brand layer into the standalone marketing ``site/``.

The marketing landing (``site/``) is a separate Next static-export app deployed
to Vercel — out of the pip wheel (see
``docs/internal/landing_site_separation_plan_2026_06_09.md``). It reuses the
dashboard's design tokens, self-hosted fonts, the canonical ``BrainMark``, and
the mirrored positioning strings. Those live in ``web/`` as the single source;
this script copies them verbatim into ``site/`` so the two apps can never drift.

This is the brand analogue of ``scripts/sync_positioning.py`` /
``scripts/sync_versions.py``.

Usage::

    python scripts/sync_brand_tokens.py            # copy web -> site in place
    python scripts/sync_brand_tokens.py --check     # exit 1 if any copy is stale

``--check`` runs in CI so a brand edit in ``web/`` that forgets to re-sync
``site/`` fails the build instead of shipping a divergent landing.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_WEB = _REPO_ROOT / "web"
_SITE = _REPO_ROOT / "site"


@dataclass(frozen=True)
class Asset:
    """One brand file mirrored ``web`` → ``site`` (paths relative to each app root)."""

    src: str
    dst: str
    description: str


# Single source = web/. Every entry is copied byte-for-byte into site/.
ASSETS: tuple[Asset, ...] = (
    Asset("app/sb-theme.css", "app/sb-theme.css", "design tokens (dual-theme)"),
    Asset("app/kit.css", "app/kit.css", "component-kit base classes"),
    Asset("app/fonts.ts", "app/fonts.ts", "self-hosted font declarations"),
    Asset(
        "app/fonts/space-grotesk-latin-wght.woff2",
        "app/fonts/space-grotesk-latin-wght.woff2",
        "font: Space Grotesk",
    ),
    Asset(
        "app/fonts/jetbrains-mono-latin-wght.woff2",
        "app/fonts/jetbrains-mono-latin-wght.woff2",
        "font: JetBrains Mono",
    ),
    Asset(
        "app/fonts/ibm-plex-sans-latin-400.woff2",
        "app/fonts/ibm-plex-sans-latin-400.woff2",
        "font: IBM Plex Sans 400",
    ),
    Asset(
        "app/fonts/ibm-plex-sans-latin-600.woff2",
        "app/fonts/ibm-plex-sans-latin-600.woff2",
        "font: IBM Plex Sans 600",
    ),
    Asset("components/kit/BrainMark.tsx", "components/BrainMark.tsx", "canonical brain mark"),
    Asset("lib/positioning.ts", "lib/positioning.ts", "mirrored positioning strings"),
)


@dataclass(frozen=True)
class Drift:
    path: str
    description: str


def _is_stale(src: Path, dst: Path) -> bool:
    if not dst.exists():
        return True
    return src.read_bytes() != dst.read_bytes()


def sync(*, write: bool, web: Path = _WEB, site: Path = _SITE) -> list[Drift]:
    """Copy every asset web → site. Return the assets that were (or, in check
    mode, would be) updated — i.e. the stale copies."""
    drifts: list[Drift] = []
    for asset in ASSETS:
        src = web / asset.src
        dst = site / asset.dst
        if not src.exists():
            raise FileNotFoundError(f"brand source missing: {src.relative_to(_REPO_ROOT)}")
        if _is_stale(src, dst):
            drifts.append(Drift(str(dst.relative_to(_REPO_ROOT)), asset.description))
            if write:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
    return drifts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Mirror web/ brand tokens, fonts, mark, and positioning into site/"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report stale copies and exit 1 without writing (CI mode).",
    )
    args = parser.parse_args(argv)

    drifts = sync(write=not args.check)

    if args.check:
        if drifts:
            print("Stale brand copies in site/:", file=sys.stderr)
            for d in drifts:
                print(f"  {d.path} — {d.description}", file=sys.stderr)
            print("\nRun `python scripts/sync_brand_tokens.py` to fix.", file=sys.stderr)
            return 1
        print("site/ brand layer matches web/.")
        return 0

    if drifts:
        print(f"Synced {len(drifts)} brand asset(s) web -> site:")
        for d in drifts:
            print(f"  {d.path} — {d.description}")
    else:
        print("Already in sync; nothing to do.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
