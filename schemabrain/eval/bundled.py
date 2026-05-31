"""Resolver for bundled fixture and golden-set files shipped with the wheel.

Backs the `schemabrain fixture-path <name>` CLI subcommand, which exists
so the README quickstart can stay copy-paste-friendly:

    psql ... < $(schemabrain fixture-path ecommerce.sql)

instead of the unreadable inline path-resolution expression the README
used pre-0.1.0a2.

The two bundled directories — `fixtures/` (SQL seeds) and `golden_sets/`
(eval JSON) — are both shipped inside the wheel via the explicit
`[tool.hatch.build.targets.wheel].artifacts` pin in `pyproject.toml`.

Path-traversal defense: `resolve_bundled_path` rejects any name
containing path separators or `..`, and runs a `.resolve()` /
`.is_relative_to()` containment check after every match so a symlink
inside the bundled dirs cannot escape to host-system files.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parent
# Sibling package roots for cross-module fixtures. The resolver
# searches across `eval/`, `imports/`, and `joins/` so the user-facing
# `fixture-path` CLI doesn't leak the internal directory layout.
_IMPORTS_FIXTURES_DIR: Path = _PACKAGE_ROOT.parent / "imports" / "fixtures"
# ----- demo pack registry --------------------------------------------------
# A "pack" is one vertical's demo semantic layer: the three YAML dir
# roots (entities / canonical joins / metrics) the wizard auto-applies
# when the user picks the bundled demo. The vertical name lives at three
# different physical depths — entities under `eval/fixtures/entities/`,
# joins + metrics under the package root — so the per-field joins in
# `_make_pack` absorb that asymmetry rather than a single shared prefix.
#
# ADD model: ecommerce is the only pack today and the default. Phase 1
# adds a second pack as a one-line drop-in —
#     _PACKS["saas"] = _make_pack("saas")
# and flips `DEFAULT_PACK = "saas"` (which also moves the joins/metrics
# search roots in `_BUNDLED_DIRS` below). Shipping the matching
# `*/fixtures/saas/*.yaml` dirs in the wheel is the only other step.

# Flipping this default ALSO moves resolve_bundled_path's joins/metrics
# search roots: `_BUNDLED_DIRS` (below) embeds the default pack's dirs,
# so a Phase-1 flip to "saas" changes what the `fixture-path` CLI
# resolves, not just the wizard demo. Intended, but flip deliberately.
DEFAULT_PACK = "saas"


@dataclass(frozen=True)
class Pack:
    """The demo-YAML directory roots for one vertical.

    Returned by `_get_pack`; the three public `bundled_*_fixture_dir`
    accessors read its fields. Frozen because a pack's layout is fixed
    at import time.
    """

    name: str
    entities_dir: Path
    joins_dir: Path
    metrics_dir: Path


def _make_pack(name: str) -> Pack:
    """Build the `Pack` for vertical `name` from the fixed dir layout."""
    return Pack(
        name=name,
        entities_dir=_PACKAGE_ROOT / "fixtures" / "entities" / name,
        joins_dir=_PACKAGE_ROOT.parent / "joins" / "fixtures" / name,
        metrics_dir=_PACKAGE_ROOT.parent / "metrics" / "fixtures" / name,
    )


_PACKS: dict[str, Pack] = {
    "ecommerce": _make_pack("ecommerce"),
    "saas": _make_pack("saas"),
}


def _get_pack(pack: str | None = None) -> Pack:
    """Return the registered `Pack` for `pack`, or the default.

    Only `None` resolves to `DEFAULT_PACK`, so every zero-arg accessor
    call keeps returning the default pack's dirs. Any other value —
    including an empty string — is treated as an explicit pack name and
    raises `ValueError` (the same bad-input convention as
    `resolve_bundled_path`) listing the available packs when it is not
    registered, rather than silently falling back to the default.
    """
    key = DEFAULT_PACK if pack is None else pack
    try:
        return _PACKS[key]
    except KeyError:
        raise ValueError(f"unknown demo pack {key!r}; available: {sorted(_PACKS)}") from None


# Module-level aliases of the DEFAULT pack's YAML dirs. Kept as named
# constants because `_BUNDLED_DIRS` (the resolve_bundled_path search
# surface) embeds the joins/metrics dirs, and the existing test suite
# references these names. A vertical rename of joins/metrics therefore
# changes behavior on two surfaces at once (the getter and the resolver).
_ENTITIES_FIXTURES_DIR: Path = _get_pack().entities_dir
_JOINS_FIXTURES_DIR: Path = _get_pack().joins_dir
_METRICS_FIXTURES_DIR: Path = _get_pack().metrics_dir

# Module-private — internal layout of the wheel is not a public API
# contract. Callers outside the package go through `resolve_bundled_path`
# or `list_bundled_files`; if a future Phase 2 consumer needs direct
# dir access, expose it via a function rather than re-exporting the
# constant.
_BUNDLED_FIXTURES_DIR: Path = _PACKAGE_ROOT / "fixtures"
_BUNDLED_GOLDEN_DIR: Path = _PACKAGE_ROOT / "golden_sets"

# Search order matters for collision-handling: if a future contributor
# accidentally ships the same basename in two directories, the first
# match wins. fixtures/ first because SQL seeds are the more common
# README quickstart target. The joins/metrics members are the DEFAULT
# pack's dirs.
_BUNDLED_DIRS: tuple[Path, ...] = (
    _BUNDLED_FIXTURES_DIR,
    _BUNDLED_GOLDEN_DIR,
    _IMPORTS_FIXTURES_DIR,
    _JOINS_FIXTURES_DIR,
    _METRICS_FIXTURES_DIR,
)


def bundled_joins_fixture_dir(pack: str | None = None) -> Path:
    """Return the absolute path to a bundled canonical-join example pack.

    The pack is a directory of YAML files (one canonical join per file)
    consumable by `schemabrain joins apply <dir>`. Distinct from
    `resolve_bundled_path` because joins are applied AS A SET, not as
    individual files — a `directory` is the natural unit of
    composition.

    `pack` selects the vertical; `None` resolves to `DEFAULT_PACK`
    (ecommerce), so existing zero-arg callers are unaffected. Raises
    `ValueError` for an unregistered pack.
    """
    return _get_pack(pack).joins_dir


def bundled_metrics_fixture_dir(pack: str | None = None) -> Path:
    """Return the absolute path to a bundled metric example pack.

    The pack is a directory of YAML files (one metric per file)
    consumable by `schemabrain metrics apply <dir>`. Same shape as
    `bundled_joins_fixture_dir`, including the `pack` selector.
    """
    return _get_pack(pack).metrics_dir


def bundled_entities_fixture_dir(pack: str | None = None) -> Path:
    """Return the absolute path to a bundled entity example pack.

    The pack is a directory of YAML files (one entity per file)
    consumable by `schemabrain entities apply <dir>`. Used by the
    wizard's demo path to pre-curate the bundled demo store without an
    LLM round-trip. `pack` selects the vertical; `None` resolves to
    `DEFAULT_PACK` (ecommerce).
    """
    return _get_pack(pack).entities_dir


def resolve_bundled_path(name: str) -> Path:
    """Return the absolute path to a bundled fixture named `name`.

    Searches `_BUNDLED_FIXTURES_DIR` then `_BUNDLED_GOLDEN_DIR` for an
    exact basename match. Returns the resolved absolute path.

    A symlink-containment check runs *after* `.resolve()`: if a candidate
    resolves outside its parent bundled directory, it is rejected. This
    blocks the attack where a maliciously crafted wheel ships a symlink
    inside the bundled dirs pointing at host-system files
    (e.g. `fixtures/escape.sql -> /etc/passwd`).

    Raises:
        ValueError: if `name` is empty, contains a path separator, or is
            `.` / `..`. Only bare basenames are valid.
        FileNotFoundError: if no bundled file matches (or if every match
            failed the containment check). The error message lists every
            available file so the caller can correct the mistake without
            grepping the source tree.
    """
    if not name:
        raise ValueError("fixture name is empty")
    if "/" in name or "\\" in name:
        raise ValueError(f"fixture name must not contain path separators: {name!r}")
    if name in (".", ".."):
        # The separator check above rejects every actual traversal
        # pattern (`../foo`, `foo/..`, `subdir/foo`). The remaining cases
        # are bare `.` and `..` — they would fall through to a confusing
        # FileNotFoundError on a directory ("no bundled fixture named
        # '..'"); reject them explicitly with a clearer message.
        raise ValueError(f"fixture name {name!r} is not a valid file basename")

    for base in _BUNDLED_DIRS:
        candidate = base / name
        if not candidate.is_file():
            continue
        resolved = candidate.resolve()
        if not resolved.is_relative_to(base.resolve()):
            # `is_file()` follows symlinks, so a symlink pointing outside
            # the package would otherwise pass. Skip silently and let the
            # search continue — falling through to FileNotFoundError is
            # the right "no valid bundled file matches" signal.
            continue
        return resolved

    available = list_bundled_files()
    raise FileNotFoundError(f"no bundled fixture named {name!r}; available: {available}")


def list_bundled_files() -> list[str]:
    """Return a sorted list of every basename in the bundled directories.

    Used to build the error message in `resolve_bundled_path` and could
    be consumed directly by a future `schemabrain fixture-path --list`
    if user demand surfaces.

    Raises FileNotFoundError if either bundled directory is missing —
    that means the wheel is broken, and a loud failure beats silently
    returning a partial list.
    """
    names: list[str] = []
    for base in _BUNDLED_DIRS:
        names.extend(p.name for p in base.iterdir() if p.is_file())
    return sorted(names)


__all__ = [
    "bundled_entities_fixture_dir",
    "bundled_joins_fixture_dir",
    "bundled_metrics_fixture_dir",
    "list_bundled_files",
    "resolve_bundled_path",
]
