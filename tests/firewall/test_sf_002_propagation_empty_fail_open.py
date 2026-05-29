"""SF-002 — ``propagate([])`` fails OPEN by treating empty
input as confirmed non-PII.

Background:

    ``schemabrain/pii/propagation.py:41-63`` — ``propagate([])``
    returns ``("public", frozenset())``. If the store returns empty
    tags for a column that was indexed before tags were populated,
    the call-site treats it as confirmed non-PII.

Expected SECURE behaviour:

    Empty input from a security primitive should fail CLOSED — the
    propagator can't tell "this query touched zero columns" apart from
    "the store has not yet classified these columns", so the right
    answer is to refuse the query OR to surface a distinguishable
    sentinel (e.g. raise, or return a third sensitivity rank that
    callers must explicitly handle).

This test pins the current fail-open behaviour. The fix lives
upstream: either ``propagate`` raises on empty input, or call-sites
distinguish "empty because no columns" from "empty because untagged".
Until then, the test FAILS — the bypass is real.
"""

from __future__ import annotations

import pytest

from schemabrain.pii.propagation import propagate

pytestmark = [pytest.mark.firewall_bypass]


def test_propagate_empty_input_currently_returns_public() -> None:
    """Documents the silent fail-open today.

    The bypass is that ``propagate([])`` is indistinguishable from
    ``propagate([("public", frozenset())])`` — both return
    ``("public", frozenset())``. A call-site that passes empty input
    because the store lookup missed cannot tell the difference.
    """
    result = propagate([])

    # The assertion below is how the test should READ once the fix
    # lands — either ``propagate`` raises on empty input, OR it returns
    # a sentinel value distinct from ``("public", frozenset())`` (for
    # example, raise ``ValueError`` here so callers can't slip past).
    #
    # Today this assertion FAILS, locking the bypass in until fixed.
    assert result != ("public", frozenset()), (
        "BYPASS: propagate([]) returned ('public', frozenset()) — "
        "indistinguishable from confirmed non-PII. A store lookup miss "
        "for a column whose tags were never written silently degrades "
        "to 'public', skipping every downstream PII gate."
    )


def test_propagate_single_public_tuple_returns_public() -> None:
    """Control case — confirmed-public input legitimately returns public.

    This test PASSES today and SHOULD keep passing after the fix.
    It locks the working contract so the remediation doesn't
    over-correct empty-input handling into legitimate-input handling.
    """
    result = propagate([("public", frozenset())])
    assert result == ("public", frozenset())
