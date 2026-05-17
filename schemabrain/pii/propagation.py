"""PII tag propagation across compiled queries.

When a metric or query touches multiple columns, the effective
sensitivity + category set of the result is computed from the touched
columns' tags. ADR 0001 §4 specifies the two rules:

  - Sensitivity propagates by MAX under the ordering
    `public < internal < confidential < pii`.
  - Categories propagate by UNION.

This module exposes a single pure function `propagate(...)`. The
compiler reads tags from the store, hands them in, and uses the
result for both `pii_blocked` enforcement and audit row population.

The function is deterministic on its input and independent of input
ordering — properties exercised by `tests/pii/test_propagation.py`.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

from schemabrain.pii.categories import (
    SENSITIVITIES,
    PIICategory,
    Sensitivity,
)

# Sensitivity rank lookup. Built once at import time from the
# canonical tuple in `pii.categories` so the ordering stays in
# lock-step with the public contract — adding a sensitivity value
# without touching this file would correctly raise KeyError at
# propagation time rather than silently producing wrong rankings.
_SENSITIVITY_RANK: Final[dict[Sensitivity, int]] = {
    value: rank for rank, value in enumerate(SENSITIVITIES)
}


def propagate(
    per_column: Iterable[tuple[Sensitivity, frozenset[PIICategory]]],
) -> tuple[Sensitivity, frozenset[PIICategory]]:
    """Combine per-column `(sensitivity, categories)` tuples.

    Returns the effective `(sensitivity, categories)` for the union of
    columns:
      - sensitivity = MAX under the canonical ordering.
      - categories  = UNION of all input frozensets.

    Empty input → `("public", frozenset())`. This is the v1 baseline
    every untagged column carries, so an empty input legitimately means
    "nothing PII-sensitive touched."
    """
    max_rank = 0  # "public" rank
    union: set[PIICategory] = set()
    for sensitivity, categories in per_column:
        rank = _SENSITIVITY_RANK[sensitivity]
        if rank > max_rank:
            max_rank = rank
        union.update(categories)
    # Reverse-lookup the rank back to the Sensitivity value. The tuple
    # is short (4 elements) so a direct index is the right shape.
    return (SENSITIVITIES[max_rank], frozenset(union))
