"""Structural tests for the packaging surface that user installs touch.

These guard invariants that are easy to drop silently — a missing
marker file or a renamed data file shows up only when a real user
installs from PyPI and hits a confusing failure. The tests fire on
the working tree, so a CI run catches drift before the wheel is
built.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PACKAGE_ROOT = _REPO_ROOT / "schemabrain"


class TestPyTypedMarker:
    """PEP 561 type-checker discoverability.

    Without a `py.typed` marker file inside the package, type checkers
    (mypy, pyright, pyrefly) running in *user* projects treat
    SchemaBrain as untyped — they silently ignore the type hints in
    the installed source, even though every function signature carries
    them. The marker is an empty file; its presence is the contract.

    Hatchling's default behavior (`[tool.hatch.build.targets.wheel]
    packages = ["schemabrain"]`) includes every file under the
    package directory, so dropping `schemabrain/py.typed` in source
    is enough for the wheel to ship it. The matching wheel-side
    assertion lives in the release workflow's wheel-inspection step.
    """

    def test_py_typed_marker_exists_in_source(self) -> None:
        marker = _PACKAGE_ROOT / "py.typed"
        assert marker.is_file(), (
            "PEP 561 marker `schemabrain/py.typed` is missing — type "
            "checkers in downstream projects will ignore SchemaBrain's "
            "type hints. Create an empty file at that path."
        )

    def test_py_typed_marker_is_empty(self) -> None:
        """PEP 561 leaves room for an optional `partial` literal but
        the typical contract is an empty marker file. SchemaBrain is
        fully typed (no `Any` escape hatches in the public surface),
        so an empty marker is correct.
        """
        marker = _PACKAGE_ROOT / "py.typed"
        if marker.is_file():
            assert marker.read_text() == "", (
                "py.typed should be empty for a fully-typed package; "
                "SchemaBrain has no Any-typed public surface that "
                "would justify the 'partial' literal."
            )
