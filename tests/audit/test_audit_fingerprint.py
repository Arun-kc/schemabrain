"""Tests for the fingerprint primitive (`schemabrain.audit.fingerprint`).

Pins the field count of `FingerprintInput` (a deliberate structural
invariant — adding a field requires reviewed action; the failing test
forces the addition through review), the determinism of
`compute_fingerprint`, the per-input pinned digests (regression alarm
against accidental formula drift), and the collision-distinguishability
of the inputs via a Hypothesis property test.

Per ADR 0001 ("Privacy-by-construction"), the fingerprint contains no
row content, no values, and no identifying schema info. Tests in this
file pin SHAPE invariants; the privacy guarantee is documented in the
ADR.
"""

from __future__ import annotations

from dataclasses import fields

import pytest
from hypothesis import given
from hypothesis import strategies as st

from schemabrain.audit.fingerprint import (
    FINGERPRINT_VERSION,
    FingerprintInput,
    compute_fingerprint,
)
from schemabrain.pii.categories import PII_CATEGORIES


class TestFieldCount:
    """Privacy boundary is structural. Adding a field requires a
    reviewed PR that knowingly trades off privacy for some other goal.
    Reviewers see this test fail and have to justify the addition.
    """

    def test_fingerprint_input_has_exactly_five_fields(self) -> None:
        assert len(fields(FingerprintInput)) == 5

    def test_fingerprint_input_field_names_are_pinned(self) -> None:
        """Renames are as load-bearing as additions — a rename would
        change the canonical JSON keys via dataclass-side reflection
        and silently shift every downstream digest."""
        names = {f.name for f in fields(FingerprintInput)}
        assert names == {
            "ast_shape_hash",
            "pii_tags_touched",
            "refusal_reason",
            "cost_class",
            "rule_id",
        }


class TestVersion:
    def test_version_is_fp_v1(self) -> None:
        """v1 ships with fp-v1. Bumping the version is a deliberate
        coordinated change — the next minor bump introduces fp-v2 and
        both rows coexist in audit history."""
        assert FINGERPRINT_VERSION == "fp-v1"


class TestImmutability:
    def test_dataclass_is_frozen(self) -> None:
        inp = FingerprintInput(
            ast_shape_hash=None,
            pii_tags_touched=frozenset(),
            refusal_reason=None,
            cost_class="small",
            rule_id=None,
        )
        with pytest.raises(AttributeError):
            inp.cost_class = "large"  # type: ignore[misc]


class TestDigestShape:
    def test_output_is_thirty_two_bytes(self) -> None:
        inp = FingerprintInput(
            ast_shape_hash=None,
            pii_tags_touched=frozenset(),
            refusal_reason=None,
            cost_class="small",
            rule_id=None,
        )
        out = compute_fingerprint(inp)
        assert isinstance(out, bytes)
        assert len(out) == 32

    def test_same_input_same_output(self) -> None:
        inp = FingerprintInput(
            ast_shape_hash=bytes.fromhex("ab" * 32),
            pii_tags_touched=frozenset({"contact"}),
            refusal_reason="pii_blocked",
            cost_class="small",
            rule_id="builtin:pii_contact",
        )
        assert compute_fingerprint(inp) == compute_fingerprint(inp)


class TestPinnedDigests:
    """Regression alarm against accidental formula drift. Any change to
    the canonical-form keys, sort order, or hash algorithm flips ALL
    pinned hex strings simultaneously — a deliberate formula bump
    re-pins them in the same PR.
    """

    def test_v1_default_row(self) -> None:
        """Every v1 row produces this digest. Differentiation lands
        when v2 brings real PII / AST / refusal-reason values."""
        inp = FingerprintInput(
            ast_shape_hash=None,
            pii_tags_touched=frozenset(),
            refusal_reason=None,
            cost_class="small",
            rule_id=None,
        )
        assert compute_fingerprint(inp).hex() == (
            "501af286a571b5e12e8e5f8d6e0b9e376b9d4e526aa3683dc522e95c4a6d0a06"
        )

    def test_pure_cost_cap_refusal(self) -> None:
        inp = FingerprintInput(
            ast_shape_hash=None,
            pii_tags_touched=frozenset(),
            refusal_reason="cost_cap_exceeded",
            cost_class="large",
            rule_id="builtin:cost_cap",
        )
        assert compute_fingerprint(inp).hex() == (
            "9f274eb645bac31ebb8c5cbc58abc76afee12b104b7574b521894234c840dda4"
        )

    def test_pii_contact_refusal_with_ast(self) -> None:
        inp = FingerprintInput(
            ast_shape_hash=bytes.fromhex("a" * 64),
            pii_tags_touched=frozenset({"contact"}),
            refusal_reason="pii_blocked",
            cost_class="small",
            rule_id="builtin:pii_contact",
        )
        assert compute_fingerprint(inp).hex() == (
            "c64e6983577d7204179a8fb5d9a71b49d251aaa6409e77664114a36c42a4bd5c"
        )

    def test_multi_pii_intersection(self) -> None:
        inp = FingerprintInput(
            ast_shape_hash=bytes.fromhex("b" * 64),
            pii_tags_touched=frozenset({"contact", "financial", "location"}),
            refusal_reason="pii_blocked",
            cost_class="medium",
            rule_id="fleet:fp-v1:0xc0ffee",
        )
        assert compute_fingerprint(inp).hex() == (
            "9ae5131ce97e04153d14802ffaf7419d48f6fb40a9c5fad809cf43cad888770a"
        )


class TestCategoryOrderingIsCanonical:
    def test_frozenset_iteration_order_does_not_affect_digest(self) -> None:
        """`pii_tags_touched` is a frozenset — iteration order is
        unspecified across runs. The fingerprint sorts the categories
        before serialising so a Python interpreter's hash seed cannot
        produce different digests for the same logical input."""
        inp1 = FingerprintInput(
            ast_shape_hash=None,
            pii_tags_touched=frozenset({"contact", "financial", "location"}),
            refusal_reason="pii_blocked",
            cost_class="small",
            rule_id=None,
        )
        inp2 = FingerprintInput(
            ast_shape_hash=None,
            pii_tags_touched=frozenset(["location", "contact", "financial"]),
            refusal_reason="pii_blocked",
            cost_class="small",
            rule_id=None,
        )
        # Inputs are equal as dataclasses (frozensets compare by value)
        # but the explicit test below pins the canonical-sort behaviour.
        assert compute_fingerprint(inp1) == compute_fingerprint(inp2)


# Hypothesis strategies for the property test below.
_cost_classes = st.sampled_from(["small", "medium", "large", "refused"])
_refusal_reasons = st.sampled_from(
    [
        None,
        "pii_blocked",
        "allowlist_violation",
        "fragment_unsafe",
        "cost_cap_exceeded",
        "ambiguous_resolution",
        "schema_drift",
    ]
)
_pii_sets = st.frozensets(st.sampled_from(sorted(PII_CATEGORIES)), max_size=5)
_ast_shapes = st.one_of(
    st.none(),
    st.binary(min_size=32, max_size=32),
)
_rule_ids = st.one_of(
    st.none(),
    st.text(min_size=1, max_size=40),
)


@st.composite
def _fingerprint_inputs(draw: st.DrawFn) -> FingerprintInput:
    return FingerprintInput(
        ast_shape_hash=draw(_ast_shapes),
        pii_tags_touched=draw(_pii_sets),
        refusal_reason=draw(_refusal_reasons),
        cost_class=draw(_cost_classes),
        rule_id=draw(_rule_ids),
    )


class TestCollisionResistance:
    """For inputs we actually generate via well-formed strategies, no
    fingerprint collisions arise in practice. The property is a
    smoke-test of the input-space partitioning — sha256 collisions are
    cryptographically unreachable, so any failure here points at a
    bug in the canonical serialisation (e.g. dropping a field from
    the JSON payload) rather than a hash collision."""

    @given(input1=_fingerprint_inputs(), input2=_fingerprint_inputs())
    def test_distinct_inputs_produce_distinct_digests(
        self, input1: FingerprintInput, input2: FingerprintInput
    ) -> None:
        if input1 == input2:
            assert compute_fingerprint(input1) == compute_fingerprint(input2)
        else:
            assert compute_fingerprint(input1) != compute_fingerprint(input2)
