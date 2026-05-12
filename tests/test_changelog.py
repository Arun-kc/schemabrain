"""Drift catcher for CHANGELOG.md.

The single load-bearing invariant: the current version in
`pyproject.toml` must appear somewhere in `CHANGELOG.md`. If a release
bump lands in `pyproject.toml` without a matching changelog entry, this
fires before the release goes out — forcing the developer to either
roll back the bump or add a `## [X.Y.Z]` heading.

Keep this file tiny on purpose. CHANGELOG content is human-written
prose; deeper structural checks add maintenance cost without catching
the failure mode that actually matters (silent version drift).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CHANGELOG = _REPO_ROOT / "CHANGELOG.md"


def _pyproject_version() -> str:
    """Return the `[project].version` literal from pyproject.toml."""
    raw = (_REPO_ROOT / "pyproject.toml").read_text()
    return tomllib.loads(raw)["project"]["version"]


class TestChangelog:
    def test_file_exists_at_repo_root(self) -> None:
        assert _CHANGELOG.is_file(), (
            "CHANGELOG.md must exist at the repo root — packaging tools, "
            "release tooling, and human readers all look there first."
        )

    def test_current_pyproject_version_has_a_bracketed_heading(self) -> None:
        # We look for `[X.Y.Z]` (the Keep-a-Changelog heading shape),
        # not the bare version string. A naive `version in text` check
        # would silently pass on the alpha-to-stable promotion: when
        # pyproject moves from "0.1.0a1" to "0.1.0", the bare substring
        # "0.1.0" is still present in the stale `[0.1.0a1]` heading, so
        # the guard never fires. Bracket-wrapping makes the check exact.
        version = _pyproject_version()
        text = _CHANGELOG.read_text()
        marker = f"[{version}]"
        assert marker in text, (
            f"pyproject version {version!r} has no `{marker}` entry in "
            f"CHANGELOG.md — roll the [Unreleased] section into a "
            f"`## {marker}` heading before tagging this release."
        )
