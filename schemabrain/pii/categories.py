"""Sensitivity and PII category Literal types.

Defined as the runtime ground truth for the 2-layer taxonomy in
`docs/adr/0001-audit-row-and-pii-taxonomy.md`. The Literal types let
static checkers reject typos at function boundaries; the parallel
tuple / frozenset enumerations are what runtime code (SQL CHECK
constraint generation, Pydantic validators, propagation helpers) uses
when it needs a value list rather than a type.

The two enumerations and their matching Literal arguments are pinned
in `tests/test_pii_categories.py` — any drift between static and
runtime views fails CI.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

# Layer 1 — sensitivity. Ordered `public < internal < confidential <
# pii` so MAX propagation across joins (ADR Decision 4) is a tuple-
# index comparison. The order is part of the public contract.
Sensitivity = Literal["public", "internal", "confidential", "pii"]
SENSITIVITIES: tuple[Sensitivity, ...] = (
    "public",
    "internal",
    "confidential",
    "pii",
)

# Layer 2 — categorical PII tags. Set-valued at use sites:
# `frozenset[PIICategory]`. The frozenset below is the closed v1.0
# enumeration; additions are minor charter bumps.
PIICategory = Literal[
    "contact",
    "financial",
    "payment_card",
    "health",
    "genetic",
    "biometric",
    "behavioral",
    "online_identifier",
    "credential",
    "government_id",
    "location",
    "demographic_protected",
]
# Canonical display order — matches the `PIICategory` Literal above 1:1.
# `PII_CATEGORIES` (the frozenset) is the set-arithmetic surface used by
# membership-testing and validation; `PII_CATEGORIES_ORDERED` is the
# iteration surface used by anything that renders columns or headers
# where order is observable (dashboard PII matrix, deterministic CLI
# output). `frozenset` iteration is hash-driven and varies across
# Python processes with the default PYTHONHASHSEED, which previously
# leaked into the dashboard as a header-order regression. Anything
# user-visible MUST iterate the ordered tuple.
PII_CATEGORIES_ORDERED: tuple[PIICategory, ...] = (
    "contact",
    "financial",
    "payment_card",
    "health",
    "genetic",
    "biometric",
    "behavioral",
    "online_identifier",
    "credential",
    "government_id",
    "location",
    "demographic_protected",
)
PII_CATEGORIES: frozenset[PIICategory] = frozenset(PII_CATEGORIES_ORDERED)

# Catastrophic-leak categories — the subset where no plausible
# aggregate-analytics use case exists and a single leak is reportable
# under GDPR / CCPA / PCI-DSS / HIPAA. Two layers consume this:
# (1) `schemabrain serve` defaults `--pii-block` to this set when the
# flag is absent, so a zero-config operator gets safe-by-default
# enforcement; (2) `describe_entity` always redacts the column
# description when any of these categories appears, even with an
# empty `--pii-block`, so the agent never reads "password hash" or
# "social security number" semantics regardless of operator policy.
# Pinned so the closed set is auditable and a future addition is a
# minor charter bump (see ADR 0001).
CATASTROPHIC_LEAK_CATEGORIES: frozenset[PIICategory] = frozenset(
    {
        "credential",
        "payment_card",
        "government_id",
    }
)

# Per-column tag pair carried across the classifier → store → compiler
# → audit chain. Aliasing the tuple shape in one place so every layer
# (Store Protocol, SQLiteStore, get_metric, audit writer) agrees on the
# Literal-narrowed types — and a future change to the pair shape lands
# in exactly one declaration site.
ColumnPiiTag = tuple[Sensitivity, frozenset[PIICategory]]

# Display band for per-column PII-classification confidence (ADR 0009).
# ADVISORY metadata for the PII matrix — it NEVER gates enforcement.
# `floor_locked` marks a catastrophic-floor column whose tag is locked
# regardless of any numeric score (the score is NULL for it); `high` /
# `medium` / `low` are derived from the index-time `pii_confidence_score`.
# Mirrors the `pii_confidence` SQL CHECK in `core/store.py`; the two stay
# in lock-step (pinned by `tests/test_pii_categories.py`).
PiiConfidenceBand = Literal["floor_locked", "high", "medium", "low"]
PII_CONFIDENCE_BANDS: tuple[PiiConfidenceBand, ...] = (
    "floor_locked",
    "high",
    "medium",
    "low",
)


def has_catastrophic_category(categories: Iterable[str]) -> bool:
    """True iff any category is a catastrophic-leak floor category.

    The single floor predicate shared by the entity PII-severity rollup,
    the PII matrix, and the v15 graph projection (ADR 0010) so the
    surfaces can never disagree on what counts as catastrophic. The floor
    is CATEGORY membership, not sensitivity level — a column is
    catastrophic regardless of its band.
    """
    return any(category in CATASTROPHIC_LEAK_CATEGORIES for category in categories)
