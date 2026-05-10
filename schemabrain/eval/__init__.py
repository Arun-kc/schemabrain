"""Schema Brain eval harness.

Scores retrieval against a hand-curated `GoldenSet` of natural-language
questions paired with the qualified table names a correct retriever
should surface. **Domain-agnostic**: the harness works against any
indexed schema, with any user-authored golden set. The bundled
`golden_sets/ecommerce.json` (paired with `fixtures/ecommerce.sql`) is
ONE example domain — picked because its tables are universally legible,
not because Schema Brain is e-commerce-specific.

Public API:
- `GoldenQuestion`, `GoldenSet`, `load_golden`, `DEFAULT_GOLDEN_PATH`
- `Retriever` (Protocol), `KeywordRetriever`
- `QuestionResult`, `EvalReport`, `run_eval`, `format_report`

The retriever is a Protocol so the placeholder `KeywordRetriever`
shipped in Week 3 can be swapped for an embedding-based retriever in
Week 4 without touching the runner or the golden set.
"""

from schemabrain.eval.golden import (
    DEFAULT_GOLDEN_PATH,
    GoldenQuestion,
    GoldenSet,
    load_golden,
)
from schemabrain.eval.retriever import KeywordRetriever, Retriever
from schemabrain.eval.runner import EvalReport, QuestionResult, format_report, run_eval

__all__ = [
    "DEFAULT_GOLDEN_PATH",
    "EvalReport",
    "GoldenQuestion",
    "GoldenSet",
    "KeywordRetriever",
    "QuestionResult",
    "Retriever",
    "format_report",
    "load_golden",
    "run_eval",
]
