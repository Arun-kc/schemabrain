"""SF-005 regression pin — CI default-suite counterpart to the opt-in
corpus repro at ``tests/firewall/test_sf_005_build_server_default_allow.py``.

The ``firewall_bypass`` corpus is excluded from the project's default
pytest selection (``pyproject.toml`` ``addopts`` carries
``not firewall_bypass``) and no CI job opts back in — so a corpus-only
pin would never run in CI and would give zero regression protection.
This module asserts the same invariant in the default suite.

Invariant: the library entrypoints ``build_server`` / ``run_stdio`` must
default ``pii_block`` to the catastrophic-leak set, matching the CLI's
``schemabrain serve`` default — so a library consumer who wraps either
entrypoint and omits the argument inherits the safe policy, not an empty
one.

This is a defense-in-depth / least-surprise pin, NOT a leak fix: every
MCP read path already unions ``CATASTROPHIC_LEAK_CATEGORIES`` into its
effective block regardless of ``pii_block`` (``get_metric``,
``describe_*``, ``find_relevant_*``, ``suggest_joins``, ``resolve_join``,
and the dashboard sidecar), so the catastrophic floor holds even with an
empty default. The default *value* should still match the CLI so the API
layer isn't default-allow.
"""

from __future__ import annotations

import inspect

from schemabrain.mcp.server import build_server, run_stdio
from schemabrain.pii import CATASTROPHIC_LEAK_CATEGORIES


def test_build_server_default_pii_block_is_catastrophic_floor() -> None:
    default = inspect.signature(build_server).parameters["pii_block"].default
    # Superset-or-equal, not strict equality, so a future widening of the
    # default (e.g. adding a category to the safe set) does not false-fail
    # this pin — it only fails if the default drops BELOW the floor.
    assert default >= CATASTROPHIC_LEAK_CATEGORIES, (
        f"build_server default pii_block={default!r}; expected at least the "
        f"catastrophic-leak floor {sorted(CATASTROPHIC_LEAK_CATEGORIES)} "
        f"(matching the CLI serve default)."
    )


def test_run_stdio_default_pii_block_is_catastrophic_floor() -> None:
    default = inspect.signature(run_stdio).parameters["pii_block"].default
    assert default >= CATASTROPHIC_LEAK_CATEGORIES, (
        f"run_stdio default pii_block={default!r}; expected at least the "
        f"catastrophic-leak floor {sorted(CATASTROPHIC_LEAK_CATEGORIES)} "
        f"(matching the CLI serve default)."
    )
