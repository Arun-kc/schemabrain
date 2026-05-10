"""Eval runner — score a `Retriever` against a `GoldenSet`.

Produces a `QuestionResult` per question and an `EvalReport` with
aggregate recall@1 / @3 / @10. The aggregate metric is the per-question
mean of (expected_in_top_k / |expected|), which is the standard "macro"
recall@k. We use macro because each curated question is equally
important to the eval signal regardless of how many tables it expects.

Recall@k is the headline metric for the preview launch (≥70% top-3
recall on `find_relevant_tables`); this module is the scoreboard.
"""

from __future__ import annotations

from dataclasses import dataclass

from schemabrain.eval.golden import GoldenSet
from schemabrain.eval.retriever import Retriever

_DEFAULT_LIMIT = 10
_HEADLINE_KS = (1, 3, 10)


@dataclass(frozen=True)
class QuestionResult:
    """One question's outcome — what was expected, what came back, in what order."""

    question_id: str
    question: str
    expected: tuple[str, ...]
    retrieved: tuple[str, ...]

    def recall_at(self, k: int) -> float:
        """Fraction of `expected` tables found in the top-k retrieved.

        `k <= 0` returns 0.0; `expected` is enforced non-empty by the
        loader so we don't guard divide-by-zero here.
        """
        if k <= 0:
            return 0.0
        top_k = set(self.retrieved[:k])
        hits = sum(1 for t in self.expected if t in top_k)
        return hits / len(self.expected)


@dataclass(frozen=True)
class EvalReport:
    """All per-question results plus aggregate-recall accessors.

    `limit` is the top-K the runner used. It's stored here so
    `format_report` can label per-question and aggregate lines with k
    values that actually correspond to data the runner saw — labeling a
    `recall@10` that was computed against a 3-element retrieved list
    silently understates the score and lies about what was measured.
    """

    results: tuple[QuestionResult, ...]
    limit: int

    def recall_at(self, k: int) -> float:
        """Macro mean of per-question `recall_at(k)`. Zero if no results."""
        if not self.results:
            return 0.0
        return sum(r.recall_at(k) for r in self.results) / len(self.results)


def run_eval(*, golden: GoldenSet, retriever: Retriever, limit: int = _DEFAULT_LIMIT) -> EvalReport:
    """Run every question through the retriever and bundle the report."""
    results = tuple(
        QuestionResult(
            question_id=q.id,
            question=q.question,
            expected=q.expected_tables,
            retrieved=tuple(retriever.retrieve(q.question, limit=limit)),
        )
        for q in golden.questions
    )
    return EvalReport(results=results, limit=limit)


def format_report(report: EvalReport) -> str:
    """Render the report as a human-readable text block.

    Layout:
        - One header line with headline aggregates capped to k <= limit
          (so we never claim a `recall@10` that the run couldn't
          actually evaluate against a smaller retrieved list).
        - Per-question lines: `[HIT|MISS] qid recall@K=X | retrieved -> ...`
          where K is the run's actual `limit`.
        - Empty-report fallback so callers don't have to guard.
    """
    if not report.results:
        return "no questions in report"

    # Only show headline k values the run could actually evaluate.
    visible_ks = tuple(k for k in _HEADLINE_KS if k <= report.limit)
    if not visible_ks:
        # `limit < 1` is meaningless but defensible — fall back to it
        # so the headline still has at least one column.
        visible_ks = (report.limit,)

    headline = "  ".join(f"recall@{k}={report.recall_at(k):.3f}" for k in visible_ks)
    lines = [f"Eval — {len(report.results)} questions  |  {headline}", ""]

    for r in report.results:
        score = r.recall_at(report.limit)
        marker = "HIT " if score > 0 else "MISS"
        retrieved_preview = ", ".join(r.retrieved[:3]) if r.retrieved else "(none)"
        lines.append(
            f"  [{marker}] {r.question_id}  recall@{report.limit}={score:.2f}  "
            f"expected={list(r.expected)}  top3=[{retrieved_preview}]"
        )

    return "\n".join(lines)


__all__ = ["EvalReport", "QuestionResult", "format_report", "run_eval"]
