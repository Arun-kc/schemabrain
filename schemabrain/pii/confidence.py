"""Deterministic, index-time PII-classification confidence.

ADVISORY ONLY — see `docs/adr/0009-trust-surface-confidence-data-
contract.md`. The band produced here is operator-triage metadata for the
PII matrix. It NEVER gates enforcement: a catastrophic-floor column is
blocked regardless of its band, and a non-floor `medium` band does not
weaken any guarantee. Enforcement is category membership, decided in the
compiler / `get_metric` path, not here.

Two index-time signals feed the score, both already collected during
profiling:

  - the NAME signal — the categories the name-regex classifier matched
    (the OUTPUT of `classify_column`); a match is real evidence.
  - the VALUE-SHAPE signal — `ColumnStats.shape_patterns`, structural
    signatures (`shape_of`: digit→``9``, lower→``a``, upper→``A``,
    separators kept) of the most common sampled values, computed from
    RAW in-memory values at index time in the profiler — NOT from
    redacted samples.

No LLM and no calibrated probability: a ``0.87`` from a generation model
is theater. `pii_confidence_score` (REAL) is the deterministic source of
truth; `pii_confidence` (the band) is the display bucket derived from it.
`floor_locked` is a band-only value (its score is ``None``) marking a
catastrophic-floor column whose tag is locked.

v1 emits `floor_locked`, `high`, and `medium`. It deliberately does NOT
emit `low`: producing a low-confidence PII claim from a name match we
cannot positively refute would under-state real sensitivity, which is
the dangerous direction. `low` stays reserved for a future operator
assertion or a richer heuristic with a genuine refutation signal.
"""

from __future__ import annotations

from collections.abc import Callable

from schemabrain.pii.categories import (
    CATASTROPHIC_LEAK_CATEGORIES,
    PIICategory,
    PiiConfidenceBand,
)

# Score primitives. A matched PII rule is the floor of evidence (a real
# name signal → `medium`); value-shape agreement lifts it into `high`.
_NAME_MATCH_BASE = 0.6
_SHAPE_CORROBORATION = 0.3
_HIGH_BAND_THRESHOLD = 0.8

# Characters a telephone-number shape may contain: digits plus the
# punctuation `shape_of` preserves verbatim (the country `+`, grouping
# parens/space, and `-`/`.` separators).
_PHONE_SHAPE_CHARS = frozenset("9 +-().")
# Characters a coordinate / IPv4 / UUID shape may contain.
_COORDINATE_SHAPE_CHARS = frozenset("9.-")
_IPV4_SHAPE_CHARS = frozenset("9.")
_UUID_SHAPE_CHARS = frozenset("9aA-")
_UUID_SHAPE_LEN = 36
_UUID_DASH_COUNT = 4


def _has_email_shape(shape: str) -> bool:
    """`aaa@aaa.aaa` — an ``@`` followed by a dotted domain."""
    at = shape.find("@")
    return at != -1 and "." in shape[at + 1 :]


def _has_phone_shape(shape: str) -> bool:
    """At least seven digits, every character a digit or phone separator."""
    if not shape:
        return False
    return shape.count("9") >= 7 and all(c in _PHONE_SHAPE_CHARS for c in shape)


def _has_coordinate_shape(shape: str) -> bool:
    """Signed/unsigned decimal — `[-]9.9` (exactly one dot, has digits)."""
    return (
        bool(shape)
        and set(shape) <= _COORDINATE_SHAPE_CHARS
        and shape.count(".") == 1
        and "9" in shape
    )


def _has_ip_or_uuid_shape(shape: str) -> bool:
    """IPv4 dotted-quad (`9.9.9.9`) or a UUID-length dash-grouped hex run."""
    is_ipv4 = set(shape) <= _IPV4_SHAPE_CHARS and shape.count(".") == 3 and "9" in shape
    is_uuid = (
        len(shape) == _UUID_SHAPE_LEN
        and shape.count("-") == _UUID_DASH_COUNT
        and set(shape) <= _UUID_SHAPE_CHARS
    )
    return is_ipv4 or is_uuid


# Only the categories with a genuinely characteristic value shape own a
# family. A category absent from this map can never corroborate (it
# stays `medium`) — free-text categories (`health`, `genetic`,
# `biometric`, `behavioral`, `demographic_protected`, `financial`) have
# no shape we can honestly check.
_CATEGORY_SHAPE_FAMILIES: dict[PIICategory, tuple[Callable[[str], bool], ...]] = {
    "contact": (_has_email_shape, _has_phone_shape),
    "location": (_has_coordinate_shape,),
    "online_identifier": (_has_ip_or_uuid_shape,),
}


def _shapes_corroborate(
    categories: frozenset[PIICategory],
    shape_patterns: tuple[str, ...],
) -> bool:
    """True when a value shape positively matches a matched category's family."""
    if not shape_patterns:
        return False
    predicates = tuple(pred for cat in categories for pred in _CATEGORY_SHAPE_FAMILIES.get(cat, ()))
    if not predicates:
        return False
    return any(pred(shape) for shape in shape_patterns for pred in predicates)


def classify_pii_confidence(
    categories: frozenset[PIICategory],
    shape_patterns: tuple[str, ...] = (),
) -> tuple[PiiConfidenceBand | None, float | None]:
    """Return ``(band, score)`` for one classified column.

    - No categories (a `public` column): ``(None, None)`` — the column
      is not on the PII matrix, so there is nothing to display.
    - Any catastrophic-floor category: ``("floor_locked", None)``. The
      tag is always-on enforced, so confidence is moot and the score is
      ``None``.
    - Otherwise: ``_NAME_MATCH_BASE``, plus ``_SHAPE_CORROBORATION`` when
      a value shape positively corroborates one of the matched
      categories' characteristic shape families. ``score >= 0.8 → high``,
      else ``medium``.
    """
    if not categories:
        return (None, None)
    if categories & CATASTROPHIC_LEAK_CATEGORIES:
        return ("floor_locked", None)
    score = _NAME_MATCH_BASE
    if _shapes_corroborate(categories, shape_patterns):
        score = min(1.0, score + _SHAPE_CORROBORATION)
    band: PiiConfidenceBand = "high" if score >= _HIGH_BAND_THRESHOLD else "medium"
    return (band, score)
