"""PII taxonomy + classifier + propagation primitives.

The two-layer model (sensitivity + categorical tags) is defined in
`docs/adr/0001-audit-row-and-pii-taxonomy.md`. This package ships:

  - `categories`  — the Literal types + tuple/frozenset enumerations
  - `classifier`  — heuristic regex-on-column-name → `(Sensitivity,
                    frozenset[PIICategory])` at index time
  - `propagation` — MAX-sensitivity + UNION-categories over a set of
                    tagged columns at query time

Consumers (index pipeline, `get_metric`, audit writer) import from
this package directly.
"""

from __future__ import annotations

from schemabrain.pii.categories import (
    PII_CATEGORIES,
    SENSITIVITIES,
    ColumnPiiTag,
    PIICategory,
    Sensitivity,
)
from schemabrain.pii.classifier import RULE_COUNT, classify_column
from schemabrain.pii.propagation import propagate

__all__ = [
    "PII_CATEGORIES",
    "RULE_COUNT",
    "SENSITIVITIES",
    "ColumnPiiTag",
    "PIICategory",
    "Sensitivity",
    "classify_column",
    "propagate",
]
