"""Schema Brain profiling layer.

Computes per-column statistics (NULL %, distinct count, sample values, shape
patterns) used as LLM-enrichment context. PII is redacted from sample values
*before* they are persisted, so customer secrets never reach the cache or any
downstream LLM call.
"""

from schemabrain.profiler.stats import (
    ColumnStats,
    redact_pii,
    shape_of,
    top_shapes,
)

__all__ = ["ColumnStats", "redact_pii", "shape_of", "top_shapes"]
