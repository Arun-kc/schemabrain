"""PII taxonomy primitives.

The two-layer model (sensitivity + categorical tags) is defined here and
referenced by `docs/adr/0001-audit-row-and-pii-taxonomy.md`. v0.5 ships
the Literal types and the enumerations; propagation helpers and the
Pydantic cross-layer validator land with the v1 entity work.
"""

from __future__ import annotations

from schemabrain.pii.categories import (
    PII_CATEGORIES,
    SENSITIVITIES,
    PIICategory,
    Sensitivity,
)

__all__ = [
    "PII_CATEGORIES",
    "SENSITIVITIES",
    "PIICategory",
    "Sensitivity",
]
