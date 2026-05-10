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
- `Retriever` (Protocol), `KeywordRetriever`, `EmbeddingRetriever`
- `QuestionResult`, `EvalReport`, `run_eval`, `format_report`

The Protocol exists so future strategies (rerankers, hybrid keyword +
embedding, sqlite-vec ANN) can drop in without touching the runner or
golden files.
"""

from schemabrain.eval.golden import (
    DEFAULT_GOLDEN_PATH,
    GoldenQuestion,
    GoldenSet,
    load_golden,
)
from schemabrain.eval.retriever import EmbeddingRetriever, KeywordRetriever, Retriever
from schemabrain.eval.runner import EvalReport, QuestionResult, format_report, run_eval

__all__ = [
    "DEFAULT_GOLDEN_PATH",
    "EmbeddingRetriever",
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
