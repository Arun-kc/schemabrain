"""Tests for the LLMClient Protocol, usage/response types, and cost helpers."""

from __future__ import annotations

import math

from schemabrain.enrichment.llm import (
    FakeLLMClient,
    LLMClient,
    LLMResponse,
    LLMUsage,
    haiku_45_cost_usd,
    sonnet_46_cost_usd,
)


class TestLLMUsage:
    def test_is_frozen(self) -> None:
        usage = LLMUsage(input_tokens=100, cached_input_tokens=0, output_tokens=20)
        try:
            usage.input_tokens = 99  # type: ignore[misc]
        except Exception:
            return
        raise AssertionError("LLMUsage should be frozen")

    def test_zero_tokens_allowed(self) -> None:
        usage = LLMUsage(input_tokens=0, cached_input_tokens=0, output_tokens=0)
        assert usage.input_tokens == 0


class TestHaikuCost:
    # Pricing (per 1M tokens):
    #   Input non-cached: $0.80
    #   Input cached:     $0.08
    #   Output:           $4.00

    def test_zero_usage_costs_zero(self) -> None:
        usage = LLMUsage(input_tokens=0, cached_input_tokens=0, output_tokens=0)
        assert haiku_45_cost_usd(usage) == 0.0

    def test_non_cached_input_only(self) -> None:
        # 1M non-cached input tokens = $0.80
        usage = LLMUsage(input_tokens=1_000_000, cached_input_tokens=0, output_tokens=0)
        assert math.isclose(haiku_45_cost_usd(usage), 0.80, rel_tol=1e-9)

    def test_output_tokens(self) -> None:
        usage = LLMUsage(input_tokens=0, cached_input_tokens=0, output_tokens=1_000_000)
        assert math.isclose(haiku_45_cost_usd(usage), 4.00, rel_tol=1e-9)

    def test_fully_cached_input_charged_at_cached_rate(self) -> None:
        # All input came from cache → cheap rate, no normal-rate charge.
        usage = LLMUsage(input_tokens=1_000_000, cached_input_tokens=1_000_000, output_tokens=0)
        assert math.isclose(haiku_45_cost_usd(usage), 0.08, rel_tol=1e-9)

    def test_partial_cache_split_correctly(self) -> None:
        # 1M total input, 800K from cache → 200K @ $0.80 + 800K @ $0.08
        usage = LLMUsage(input_tokens=1_000_000, cached_input_tokens=800_000, output_tokens=0)
        expected = 200_000 * 0.80 / 1_000_000 + 800_000 * 0.08 / 1_000_000
        assert math.isclose(haiku_45_cost_usd(usage), expected, rel_tol=1e-9)

    def test_cache_creation_tokens_charged_at_125x_rate(self) -> None:
        # Cache writes cost 1.25x the standard input rate ($1.00/MTok).
        # 1M tokens written → $1.00.
        usage = LLMUsage(
            input_tokens=1_000_000,
            cached_input_tokens=0,
            output_tokens=0,
            cache_creation_tokens=1_000_000,
        )
        assert math.isclose(haiku_45_cost_usd(usage), 1.00, rel_tol=1e-9)

    def test_first_call_with_cache_write_costs_more_than_pure_input(self) -> None:
        # A first-of-session call writes the system prefix to cache,
        # paying 1.25x for those tokens. Subsequent calls in the same
        # session pay the cheap cached rate.
        # Realistic numbers: 500-token system prompt, 100-token user.
        first_call = LLMUsage(
            input_tokens=600,
            cached_input_tokens=0,
            output_tokens=50,
            cache_creation_tokens=500,
        )
        second_call = LLMUsage(
            input_tokens=600,
            cached_input_tokens=500,
            output_tokens=50,
            cache_creation_tokens=0,
        )
        assert haiku_45_cost_usd(first_call) > haiku_45_cost_usd(second_call)

    def test_realistic_call_cost_under_one_cent(self) -> None:
        # Typical column-description call: ~500 input + ~50 output, no cache hit.
        usage = LLMUsage(input_tokens=500, cached_input_tokens=0, output_tokens=50)
        # 500 * 0.80/1M + 50 * 4.00/1M = 0.0004 + 0.0002 = 0.0006 USD
        cost = haiku_45_cost_usd(usage)
        assert 0.0 < cost < 0.01

    def test_cached_input_tokens_must_not_exceed_input_tokens(self) -> None:
        # If a future LLMUsage somehow has cached > input (a bug or
        # provider quirk), the cost calculation must not go negative.
        # We choose to clamp: cached counted up to input_tokens.
        usage = LLMUsage(input_tokens=100, cached_input_tokens=500, output_tokens=0)
        cost = haiku_45_cost_usd(usage)
        assert cost >= 0.0


class TestSonnetCost:
    # Sonnet 4.6 pricing (per 1M tokens):
    #   Input non-cached: $3.00
    #   Input cached:     $0.30
    #   Cache write:      $3.75
    #   Output:           $15.00

    def test_zero_usage_costs_zero(self) -> None:
        usage = LLMUsage(input_tokens=0, cached_input_tokens=0, output_tokens=0)
        assert sonnet_46_cost_usd(usage) == 0.0

    def test_non_cached_input_only(self) -> None:
        usage = LLMUsage(input_tokens=1_000_000, cached_input_tokens=0, output_tokens=0)
        assert math.isclose(sonnet_46_cost_usd(usage), 3.00, rel_tol=1e-9)

    def test_output_tokens(self) -> None:
        usage = LLMUsage(input_tokens=0, cached_input_tokens=0, output_tokens=1_000_000)
        assert math.isclose(sonnet_46_cost_usd(usage), 15.00, rel_tol=1e-9)

    def test_fully_cached_input_charged_at_cached_rate(self) -> None:
        usage = LLMUsage(input_tokens=1_000_000, cached_input_tokens=1_000_000, output_tokens=0)
        assert math.isclose(sonnet_46_cost_usd(usage), 0.30, rel_tol=1e-9)

    def test_cache_creation_tokens_charged_at_125x_rate(self) -> None:
        usage = LLMUsage(
            input_tokens=1_000_000,
            cached_input_tokens=0,
            output_tokens=0,
            cache_creation_tokens=1_000_000,
        )
        # 1M creation tokens at $3.75 per MTok = $3.75
        assert math.isclose(sonnet_46_cost_usd(usage), 3.75, rel_tol=1e-9)

    def test_sonnet_is_meaningfully_more_expensive_than_haiku(self) -> None:
        # Sanity: any non-trivial Sonnet call costs more than the same
        # Haiku call. If this ever flips, the routing logic gets the
        # premium relationship backwards and we'd silently pay 5x.
        usage = LLMUsage(input_tokens=500, cached_input_tokens=0, output_tokens=50)
        assert sonnet_46_cost_usd(usage) > haiku_45_cost_usd(usage)


# The previous `TestCostDispatch` class (8 tests against the now-removed
# module-level `cost_usd_for(model, usage)` central dispatch) is gone.
# The same invariants — Haiku/Sonnet routing, date-suffix tolerance,
# false-positive guards against `claude-haiku-4-50` / `claude-haiku-4-5xyz` —
# are now pinned in `tests/test_seams.py::TestAnthropicClientCostUsd` and
# `tests/test_seams.py::TestAnthropicClientConstructionValidation`. The
# new shape validates at the adapter's CONSTRUCTOR rather than at the
# response-time central function, so a misrouted model fails before any
# API call burns money. See `schemabrain/enrichment/llm.py` module
# docstring for the seam rationale.


class TestLLMResponse:
    def test_is_frozen(self) -> None:
        resp = LLMResponse(
            text="hello",
            model="claude-haiku-4-5",
            usage=LLMUsage(input_tokens=1, cached_input_tokens=0, output_tokens=1),
        )
        try:
            resp.text = "goodbye"  # type: ignore[misc]
        except Exception:
            return
        raise AssertionError("LLMResponse should be frozen")


class TestFakeLLMClient:
    def test_satisfies_protocol(self) -> None:
        assert isinstance(FakeLLMClient(text_provider=lambda system, user: "x"), LLMClient)

    def test_returns_canned_text(self) -> None:
        client = FakeLLMClient(text_provider=lambda system, user: "the answer")
        resp = client.complete(system="sys", user="hi")
        assert resp.text == "the answer"

    def test_records_each_call(self) -> None:
        client = FakeLLMClient(text_provider=lambda system, user: "ok")
        client.complete(system="s1", user="u1")
        client.complete(system="s1", user="u2")
        assert len(client.calls) == 2
        assert client.calls[0] == ("s1", "u1")
        assert client.calls[1] == ("s1", "u2")

    def test_text_provider_receives_system_and_user(self) -> None:
        seen: list[tuple[str, str]] = []

        def provider(system: str, user: str) -> str:
            seen.append((system, user))
            return f"echo: {user[:5]}"

        client = FakeLLMClient(text_provider=provider)
        resp = client.complete(system="SYS", user="hello world")
        assert seen == [("SYS", "hello world")]
        assert resp.text == "echo: hello"

    def test_default_usage_is_realistic(self) -> None:
        # Token counts proportional to text length (rough heuristic) so a
        # FakeLLMClient call still produces non-zero cost in pipeline tests.
        client = FakeLLMClient(text_provider=lambda system, user: "x" * 50)
        resp = client.complete(system="some system prompt", user="some user prompt")
        assert resp.usage.input_tokens > 0
        assert resp.usage.output_tokens > 0
