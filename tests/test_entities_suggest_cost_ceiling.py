"""Tests for `CostCeilingGuard` — pre-flight + cumulative cost enforcement.

The cost ceiling is a hard fail, not
a silent truncation. The guard pre-flight-estimates each call's input
cost and refuses to start if the cumulative plus estimate would exceed
the ceiling. After the call, real cost (from the LLMClient adapter) is
added to the running total, and subsequent calls see the updated number.

The guard implements the `LLMClient` Protocol itself — it's a transparent
Decorator. The suggest pipeline takes `LLMClient`, so wrapping the
production adapter in `CostCeilingGuard` requires zero pipeline changes.
"""

from __future__ import annotations

import pytest

from schemabrain.enrichment.llm import FakeLLMClient, LLMClient
from schemabrain.entities.suggest import (
    CostCeilingExceededError,
    CostCeilingGuard,
    EntitySuggestionPipeline,
)


def _short_stub(_system: str, _user: str) -> str:
    return "candidates: []"


# ----- construction ---------------------------------------------------------


class TestCostCeilingGuardConstruction:
    def test_accepts_positive_ceiling(self) -> None:
        inner = FakeLLMClient(text_provider=_short_stub)
        guard = CostCeilingGuard(inner=inner, max_cost_usd=1.0)
        assert guard.max_cost_usd == pytest.approx(1.0)
        assert guard.cumulative_cost_usd == pytest.approx(0.0)

    @pytest.mark.parametrize("ceiling", [0.0, -0.01, -1.0])
    def test_rejects_non_positive_ceiling(self, ceiling: float) -> None:
        inner = FakeLLMClient(text_provider=_short_stub)
        with pytest.raises(ValueError, match="max_cost_usd"):
            CostCeilingGuard(inner=inner, max_cost_usd=ceiling)

    def test_guard_is_an_llm_client(self) -> None:
        # `runtime_checkable` Protocol check — the guard must satisfy
        # the LLMClient surface so the pipeline can accept it without
        # any branching on adapter type.
        inner = FakeLLMClient(text_provider=_short_stub)
        guard = CostCeilingGuard(inner=inner, max_cost_usd=1.0)
        assert isinstance(guard, LLMClient)


# ----- pass-through behavior under ceiling -----------------------------------


class TestCostCeilingGuardPassThrough:
    def test_complete_delegates_to_inner(self) -> None:
        inner = FakeLLMClient(text_provider=_short_stub)
        guard = CostCeilingGuard(inner=inner, max_cost_usd=10.0)

        response = guard.complete(system="sys", user="user")

        # Inner saw the call with the exact same prompts.
        assert inner.calls == [("sys", "user")]
        assert response.text == "candidates: []"

    def test_cost_usd_delegates_to_inner(self) -> None:
        # Cost dispatch lives ON the adapter (provider-owned pricing).
        # The guard MUST NOT reprice — it just adds + checks.
        inner = FakeLLMClient(text_provider=_short_stub)
        guard = CostCeilingGuard(inner=inner, max_cost_usd=10.0)

        response = guard.complete(system="sys", user="user")
        assert guard.cost_usd(response.usage) == inner.cost_usd(response.usage)

    def test_cumulative_cost_accrues_across_calls(self) -> None:
        inner = FakeLLMClient(text_provider=_short_stub)
        guard = CostCeilingGuard(inner=inner, max_cost_usd=10.0)

        guard.complete(system="sys1", user="user1")
        first_total = guard.cumulative_cost_usd
        assert first_total > 0

        guard.complete(system="sys2", user="user2")
        second_total = guard.cumulative_cost_usd
        # Strictly greater — every call adds non-zero cost (FakeLLMClient
        # uses chars/4 token approximation).
        assert second_total > first_total


# ----- hard-fail on exceed ---------------------------------------------------


class TestCostCeilingGuardEnforcement:
    def test_pre_flight_rejects_oversized_first_call(self) -> None:
        # Construct a prompt large enough that the input-only estimate
        # already exceeds an aggressively-low ceiling. Pre-flight fires
        # BEFORE inner.complete is invoked.
        inner = FakeLLMClient(text_provider=_short_stub)
        # Haiku 4.5 input is $0.80 per MTok. 10,000-char prompt is ~2500
        # tokens ≈ $0.002. A ceiling of $0.0001 is well below that.
        guard = CostCeilingGuard(inner=inner, max_cost_usd=0.0001)

        huge_user = "x" * 10_000
        with pytest.raises(CostCeilingExceededError):
            guard.complete(system="sys", user=huge_user)

        # And the inner client was never called — the user does not
        # pay for the refused call.
        assert inner.calls == []
        # Cumulative cost stays at zero — refused calls don't accrue.
        assert guard.cumulative_cost_usd == 0.0

    def test_rejects_when_cumulative_plus_estimate_would_exceed(self) -> None:
        # First call under ceiling -> succeeds.
        # Second call's estimate + cumulative > ceiling -> rejected.
        inner = FakeLLMClient(text_provider=_short_stub)
        # Headroom: first call eats some, second call's huge prompt
        # tips us over. Concrete numbers picked so the first call
        # passes and the second fails.
        guard = CostCeilingGuard(inner=inner, max_cost_usd=0.002)

        guard.complete(system="s", user="u")  # cheap — passes
        cumulative_after_first = guard.cumulative_cost_usd
        assert cumulative_after_first > 0
        assert cumulative_after_first < 0.002

        huge_user = "x" * 50_000
        with pytest.raises(CostCeilingExceededError):
            guard.complete(system="sys", user=huge_user)

        # Cumulative cost unchanged after refusal.
        assert guard.cumulative_cost_usd == cumulative_after_first
        # And only the first (successful) call hit the inner client.
        assert len(inner.calls) == 1

    def test_exceeded_error_carries_diagnostic_fields(self) -> None:
        inner = FakeLLMClient(text_provider=_short_stub)
        guard = CostCeilingGuard(inner=inner, max_cost_usd=0.0001)

        with pytest.raises(CostCeilingExceededError) as exc_info:
            guard.complete(system="sys", user="x" * 10_000)

        err = exc_info.value
        # All three fields the CLI needs to render the guided error
        # message ("you spent $X.XX of $Y.YY; next call would add $Z.ZZ").
        assert err.cumulative_cost_usd == pytest.approx(0.0)
        assert err.max_cost_usd == pytest.approx(0.0001)
        assert err.next_call_estimate_usd > 0

    def test_subsequent_calls_continue_to_refuse(self) -> None:
        # Once the ceiling is breached, every subsequent call refuses
        # — the guard does not "unlock" if you wait or rebuild.
        inner = FakeLLMClient(text_provider=_short_stub)
        guard = CostCeilingGuard(inner=inner, max_cost_usd=0.0001)

        with pytest.raises(CostCeilingExceededError):
            guard.complete(system="sys", user="x" * 10_000)

        with pytest.raises(CostCeilingExceededError):
            guard.complete(system="sys", user="x" * 10_000)

        assert inner.calls == []


# ----- integration with the suggest pipeline ---------------------------------


class TestCostCeilingGuardWithPipeline:
    def test_pipeline_accepts_guard_transparently(self) -> None:
        # The whole point of the Decorator shape: pipeline sees a
        # plain LLMClient, doesn't know or care it's wrapped.
        inner = FakeLLMClient(text_provider=lambda _s, _u: "candidates: []")
        guard = CostCeilingGuard(inner=inner, max_cost_usd=10.0)
        pipeline = EntitySuggestionPipeline(llm=guard)

        from schemabrain.core.models import Column, Table

        tables = [
            Table(
                name="users",
                schema_name="public",
                columns=(
                    Column(
                        name="id",
                        table_name="users",
                        schema_name="public",
                        data_type="bigint",
                        nullable=False,
                        ordinal_position=1,
                        is_primary_key=True,
                    ),
                ),
            )
        ]
        result = pipeline.propose_from_tables(tables)
        assert result.candidates == ()
        # Cost made it through the guard into the result.
        assert result.total_cost_usd > 0
        # And the guard tracked it.
        assert guard.cumulative_cost_usd == pytest.approx(result.total_cost_usd)

    def test_pipeline_run_aborts_when_ceiling_breached(self) -> None:
        from schemabrain.core.models import Column, Table

        inner = FakeLLMClient(text_provider=lambda _s, _u: "candidates: []")
        guard = CostCeilingGuard(inner=inner, max_cost_usd=0.0001)
        pipeline = EntitySuggestionPipeline(llm=guard)

        tables = [
            Table(
                name="users",
                schema_name="public",
                columns=tuple(
                    Column(
                        name=f"col_{i}",
                        table_name="users",
                        schema_name="public",
                        data_type="text",
                        nullable=True,
                        ordinal_position=i,
                    )
                    for i in range(1, 30)
                ),
            )
        ] * 50  # Lots of tables to bulk up the prompt

        with pytest.raises(CostCeilingExceededError):
            pipeline.propose_from_tables(tables)

        # Inner never saw the call; cumulative stays zero.
        assert inner.calls == []
        assert guard.cumulative_cost_usd == 0.0
