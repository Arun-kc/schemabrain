"""Tests for the eval runner — recall@k math and report generation.

The runner takes a `GoldenSet` and a `Retriever`, runs every question
through the retriever, and produces an `EvalReport` with per-question
results plus aggregate recall@1 / @3 / @10. The report is the artifact
we publish in the README and track week-over-week.
"""

from __future__ import annotations

from dataclasses import dataclass

from schemabrain.eval.golden import GoldenQuestion, GoldenSet
from schemabrain.eval.runner import (
    EvalReport,
    QuestionResult,
    format_report,
    run_eval,
)


@dataclass
class _StaticRetriever:
    """Test double — returns a fixed list per query, regardless of query text."""

    answers: dict[str, list[str]]

    def retrieve(self, query: str, *, limit: int) -> list[str]:
        return self.answers.get(query, [])[:limit]


def _gs(questions: list[GoldenQuestion]) -> GoldenSet:
    return GoldenSet(
        version="1",
        schema_description="test",
        questions=tuple(questions),
    )


def _q(qid: str, text: str, expected: tuple[str, ...]) -> GoldenQuestion:
    return GoldenQuestion(id=qid, question=text, expected_tables=expected)


class TestQuestionResultRecall:
    def test_full_hit_when_only_expected_is_top1(self) -> None:
        r = QuestionResult(
            question_id="q1",
            question="x?",
            expected=("public.users",),
            retrieved=("public.users", "public.orders"),
        )
        assert r.recall_at(1) == 1.0
        assert r.recall_at(3) == 1.0

    def test_zero_when_expected_not_in_top_k(self) -> None:
        r = QuestionResult(
            question_id="q1",
            question="x?",
            expected=("public.users",),
            retrieved=("public.orders", "public.products"),
        )
        assert r.recall_at(10) == 0.0

    def test_partial_recall_for_multi_expected(self) -> None:
        r = QuestionResult(
            question_id="q1",
            question="x?",
            expected=("public.orders", "public.order_items"),
            retrieved=("public.orders", "public.users"),
        )
        # 1 of 2 expected found in top-3.
        assert r.recall_at(3) == 0.5

    def test_promotion_at_higher_k(self) -> None:
        # The expected table is at rank 4 — out at k=3, in at k=10.
        r = QuestionResult(
            question_id="q1",
            question="x?",
            expected=("public.target",),
            retrieved=("a.x", "a.y", "a.z", "public.target", "a.w"),
        )
        assert r.recall_at(3) == 0.0
        assert r.recall_at(10) == 1.0

    def test_zero_k_yields_zero(self) -> None:
        r = QuestionResult(
            question_id="q1",
            question="x?",
            expected=("public.users",),
            retrieved=("public.users",),
        )
        assert r.recall_at(0) == 0.0


class TestRunEval:
    def test_runs_each_question_through_retriever(self) -> None:
        gs = _gs(
            [
                _q("q1", "users?", ("public.users",)),
                _q("q2", "orders?", ("public.orders",)),
            ]
        )
        retriever = _StaticRetriever(
            answers={
                "users?": ["public.users", "public.orders"],
                "orders?": ["public.orders"],
            }
        )
        report = run_eval(golden=gs, retriever=retriever, limit=10)
        assert len(report.results) == 2
        assert report.results[0].question_id == "q1"
        assert report.results[0].retrieved == ("public.users", "public.orders")
        assert report.results[1].question_id == "q2"

    def test_propagates_limit_to_retriever(self) -> None:
        gs = _gs([_q("q1", "x?", ("public.a",))])

        captured = {}

        class _Spy:
            def retrieve(self, query: str, *, limit: int) -> list[str]:
                captured["limit"] = limit
                return ["public.a"]

        run_eval(golden=gs, retriever=_Spy(), limit=42)
        assert captured["limit"] == 42

    def test_default_limit_is_10(self) -> None:
        gs = _gs([_q("q1", "x?", ("public.a",))])
        captured = {}

        class _Spy:
            def retrieve(self, query: str, *, limit: int) -> list[str]:
                captured["limit"] = limit
                return []

        run_eval(golden=gs, retriever=_Spy())
        assert captured["limit"] == 10

    def test_handles_empty_retrieval(self) -> None:
        gs = _gs([_q("q1", "noise?", ("public.users",))])
        retriever = _StaticRetriever(answers={})
        report = run_eval(golden=gs, retriever=retriever, limit=10)
        assert report.results[0].retrieved == ()
        assert report.results[0].recall_at(10) == 0.0


class TestEvalReportAggregates:
    def test_empty_results_recall_is_zero(self) -> None:
        # Direct construction with zero results — `recall_at` must not
        # divide by zero. (`format_report` short-circuits earlier, but
        # callers can still ask the metric directly.)
        report = EvalReport(results=(), limit=10)
        assert report.recall_at(10) == 0.0

    def test_averages_recall_across_questions(self) -> None:
        # q1 hits perfectly, q2 misses. Mean recall@10 = 0.5.
        gs = _gs(
            [
                _q("q1", "x?", ("public.users",)),
                _q("q2", "y?", ("public.orders",)),
            ]
        )
        retriever = _StaticRetriever(
            answers={
                "x?": ["public.users"],
                "y?": ["public.products"],  # miss
            }
        )
        report = run_eval(golden=gs, retriever=retriever, limit=10)
        assert report.recall_at(10) == 0.5

    def test_recall_at_1_separates_top_choice_quality(self) -> None:
        # q1: top-1 hits. q2: hits at rank 2 — counts at k=3 but not k=1.
        gs = _gs(
            [
                _q("q1", "x?", ("public.a",)),
                _q("q2", "y?", ("public.b",)),
            ]
        )
        retriever = _StaticRetriever(
            answers={
                "x?": ["public.a", "public.z"],
                "y?": ["public.z", "public.b"],
            }
        )
        report = run_eval(golden=gs, retriever=retriever, limit=10)
        assert report.recall_at(1) == 0.5
        assert report.recall_at(3) == 1.0


class TestFormatReport:
    def test_includes_aggregate_metrics(self) -> None:
        gs = _gs([_q("q1", "x?", ("public.users",))])
        retriever = _StaticRetriever(answers={"x?": ["public.users"]})
        report = run_eval(golden=gs, retriever=retriever, limit=10)
        text = format_report(report)
        assert "recall@1" in text
        assert "recall@3" in text
        assert "recall@10" in text
        assert "1.000" in text or "100" in text  # 100% recall

    def test_includes_per_question_lines(self) -> None:
        gs = _gs([_q("q1", "x?", ("public.users",))])
        retriever = _StaticRetriever(answers={"x?": ["public.users"]})
        report = run_eval(golden=gs, retriever=retriever, limit=10)
        text = format_report(report)
        assert "q1" in text

    def test_marks_misses_visually(self) -> None:
        gs = _gs([_q("q1", "x?", ("public.users",))])
        retriever = _StaticRetriever(answers={"x?": ["public.orders"]})
        report = run_eval(golden=gs, retriever=retriever, limit=10)
        text = format_report(report)
        # Some indicator that q1 missed — we don't pin the exact glyph,
        # just that the per-question line for a miss is differentiated.
        assert "MISS" in text or "FAIL" in text or "✗" in text or "miss" in text.lower()

    def test_zero_questions_report_does_not_crash(self) -> None:
        # Defensive: build an EvalReport directly with zero results.
        # `format_report` must not divide by zero.
        report = EvalReport(results=(), limit=10)
        text = format_report(report)
        assert "no questions" in text.lower() or "0 questions" in text.lower()

    def test_headline_caps_at_run_limit(self) -> None:
        # Regression: `format_report` used to label `recall@10` even when
        # the run used `limit=3`, silently understating the score because
        # `retrieved` was only 3 elements. Now the headline only shows
        # k values <= the run's actual limit.
        gs = _gs([_q("q1", "x?", ("public.users",))])
        retriever = _StaticRetriever(answers={"x?": ["public.users"]})
        report = run_eval(golden=gs, retriever=retriever, limit=3)
        text = format_report(report)
        assert "recall@1" in text
        assert "recall@3" in text
        assert "recall@10" not in text  # would be a lie

    def test_per_question_line_uses_run_limit(self) -> None:
        gs = _gs([_q("q1", "x?", ("public.users",))])
        retriever = _StaticRetriever(answers={"x?": ["public.users"]})
        report = run_eval(golden=gs, retriever=retriever, limit=5)
        text = format_report(report)
        # Per-question score is labeled with the actual k used.
        assert "recall@5=" in text

    def test_report_preserves_limit(self) -> None:
        # `limit` is part of the report's identity — Week 4 retrievers
        # (and any cross-run comparison) need to know what k the score
        # was measured against.
        gs = _gs([_q("q1", "x?", ("public.users",))])
        retriever = _StaticRetriever(answers={"x?": ["public.users"]})
        report = run_eval(golden=gs, retriever=retriever, limit=7)
        assert report.limit == 7

    def test_zero_limit_falls_back_in_headline(self) -> None:
        # `limit=0` is meaningless but format_report must not crash on
        # it. The visible_ks fallback puts the (zero) limit in so the
        # headline still has one column.
        report = EvalReport(
            results=(
                QuestionResult(
                    question_id="q1",
                    question="x?",
                    expected=("public.users",),
                    retrieved=(),
                ),
            ),
            limit=0,
        )
        text = format_report(report)
        assert "recall@0" in text
