"""SF-005 — ``build_server(pii_block=frozenset())`` WAS default-allow
at the library API (now fixed — see below).

Background (the finding, since resolved):

    ``schemabrain/mcp/server.py`` — ``build_server(pii_block=
    frozenset())`` WAS the default. A library consumer that didn't pass
    ``pii_block`` inherited an empty operator policy (the catastrophic
    floor still applied via the per-tool union, so not a full bypass).

Expected SECURE behaviour:

    The CLI's ``schemabrain serve`` correctly defaults ``--pii-block``
    to the catastrophic-leak set. But ``build_server`` itself — the
    Python API — defaults to ``frozenset()``, which means "block
    nothing in the operator-policy slot". The catastrophic-leak floor
    still applies via the per-tool implementations
    (``CATASTROPHIC_LEAK_CATEGORIES`` is unioned in there), so this is
    NOT a full PII bypass — but it IS a default-allow at the API
    layer, where a library consumer who writes a thin wrapper around
    ``build_server`` and forgets to pass ``pii_block`` inherits the
    weakest possible policy. Defaults should match the CLI: safe.

The assertion below asserts the SECURE behaviour: the default must be
the catastrophic-leak set (or a superset). The fix landed in the v0.5.0
pre-publish hardening — ``build_server`` / ``run_stdio`` now default
``pii_block`` to ``CATASTROPHIC_LEAK_CATEGORIES``, matching
``schemabrain serve`` — so this corpus repro now passes. Because the
``firewall_bypass`` corpus is opt-in (excluded from the default pytest
selection and not run by any CI job), the load-bearing CI regression pin
lives in ``tests/test_mcp_secure_defaults.py``; this copy is the
historical audit record.
"""

from __future__ import annotations

import inspect

import pytest

from schemabrain.mcp.server import build_server
from schemabrain.pii import CATASTROPHIC_LEAK_CATEGORIES

pytestmark = [pytest.mark.firewall_bypass]


def test_build_server_default_pii_block_is_safe_set() -> None:
    """The ``pii_block`` parameter default MUST be the catastrophic-leak set
    (or a superset) rather than ``frozenset()``.

    Inspecting the signature is the surgically-narrow assertion: we
    don't have to spin up a real source DB just to read the default.
    """
    sig = inspect.signature(build_server)
    default = sig.parameters["pii_block"].default

    assert default == CATASTROPHIC_LEAK_CATEGORIES or default >= CATASTROPHIC_LEAK_CATEGORIES, (
        f"BYPASS: build_server(pii_block=...) defaults to {default!r}. "
        f"A library consumer that omits the argument gets "
        f"zero operator-policy enforcement. The catastrophic-leak floor "
        f"still applies inside each tool, but the API default-allow at "
        f"this layer is the silent failure surface — the CLI's "
        f"`--pii-block` flag defaults to the safe set, the API should "
        f"match. Expected default {sorted(CATASTROPHIC_LEAK_CATEGORIES)}."
    )


def test_run_stdio_default_pii_block_is_safe_set() -> None:
    """``run_stdio`` mirrors the same default; pin the parallel surface."""
    from schemabrain.mcp.server import run_stdio

    sig = inspect.signature(run_stdio)
    default = sig.parameters["pii_block"].default

    assert default == CATASTROPHIC_LEAK_CATEGORIES or default >= CATASTROPHIC_LEAK_CATEGORIES, (
        f"BYPASS: run_stdio(pii_block=...) defaults to {default!r}. "
        f"Same shape as build_server — a thin wrapper around "
        f"`schemabrain.mcp.run_stdio` that omits `pii_block` inherits "
        f"an empty operator-policy. Expected default "
        f"{sorted(CATASTROPHIC_LEAK_CATEGORIES)}."
    )
