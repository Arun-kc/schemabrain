"""PII taxonomy + classifier + propagation primitives.

The two-layer model (sensitivity + categorical tags) is defined in
`docs/adr/0001-audit-row-and-pii-taxonomy.md`. This package ships:

  - `categories`  — the Literal types + tuple/frozenset enumerations
  - `classifier`  — heuristic regex-on-column-name → `(Sensitivity,
                    frozenset[PIICategory])` at index time
  - `confidence`  — deterministic index-time confidence band for a
                    classified column, from name + value-shape signal
                    (advisory matrix metadata; see ADR 0009)
  - `propagation` — MAX-sensitivity + UNION-categories over a set of
                    tagged columns at query time

Consumers (index pipeline, `get_metric`, audit writer) import from
this package directly.
"""

from __future__ import annotations

from schemabrain.pii.categories import (
    CATASTROPHIC_LEAK_CATEGORIES,
    PII_CATEGORIES,
    PII_CATEGORIES_ORDERED,
    PII_CONFIDENCE_BANDS,
    SENSITIVITIES,
    ColumnPiiTag,
    PIICategory,
    PiiConfidenceBand,
    Sensitivity,
)
from schemabrain.pii.classifier import RULE_COUNT, classify_column
from schemabrain.pii.confidence import classify_pii_confidence
from schemabrain.pii.propagation import propagate

__all__ = [
    "CATASTROPHIC_LEAK_CATEGORIES",
    "PII_CATEGORIES",
    "PII_CATEGORIES_ORDERED",
    "PII_CONFIDENCE_BANDS",
    "RULE_COUNT",
    "SENSITIVITIES",
    "ColumnPiiTag",
    "PIICategory",
    "PiiConfidenceBand",
    "Sensitivity",
    "classify_column",
    "classify_pii_confidence",
    "propagate",
]
