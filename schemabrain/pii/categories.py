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
PII_CATEGORIES: frozenset[PIICategory] = frozenset(
    {
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
    }
)

# Per-column tag pair carried across the classifier → store → compiler
# → audit chain. Aliasing the tuple shape in one place so every layer
# (Store Protocol, SQLiteStore, get_metric, audit writer) agrees on the
# Literal-narrowed types — and a future change to the pair shape lands
# in exactly one declaration site.
ColumnPiiTag = tuple[Sensitivity, frozenset[PIICategory]]
