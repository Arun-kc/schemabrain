"""LLM client abstraction.

Defines the Protocol every concrete client implements (Anthropic today,
hypothetically Bedrock or Vertex tomorrow) plus shared types and the
Haiku 4.5 cost calculator. The pipeline programs against `LLMClient`
so tests can substitute `FakeLLMClient` without an API key.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# Anthropic Haiku 4.5 pricing (USD per 1M tokens). Update when Anthropic
# changes pricing — that's a deliberate cache invalidation if any
# downstream code stamps cost into a hash, but currently it's only used
# for runtime cost reporting + the --max-cost guard.
_HAIKU_45_INPUT_PER_MTOK = 0.80
_HAIKU_45_CACHED_INPUT_PER_MTOK = 0.08  # cache READS (90% off)
_HAIKU_45_CACHE_WRITE_PER_MTOK = 1.00  # 5-min TTL ephemeral writes (1.25x base)
_HAIKU_45_OUTPUT_PER_MTOK = 4.00


@dataclass(frozen=True)
class LLMUsage:
    """Token usage for one LLM call.

    `input_tokens` is the TOTAL number of input tokens for this call
    (inclusive of both cached reads and cache writes — see below).

    `cached_input_tokens` is the SUBSET of `input_tokens` served from
    Anthropic's prompt cache (90%-off rate).

    `cache_creation_tokens` is the SUBSET of `input_tokens` that was
    written into the cache on this call (1.25x base rate; only on the
    first call of a session).

    The remainder (`input_tokens - cached_input_tokens -
    cache_creation_tokens`) is processed at the standard input rate.
    """

    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    cache_creation_tokens: int = 0


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    usage: LLMUsage


def haiku_45_cost_usd(usage: LLMUsage) -> float:
    """USD cost for one Haiku 4.5 call given its usage breakdown.

    `input_tokens` is treated as the TOTAL input; `cached_input_tokens`
    and `cache_creation_tokens` are SUBSETS of it billed at their own
    rates. The remainder is billed at the standard input rate.

    Defensively clamps the subsets so a provider quirk that double-counts
    cache cannot produce a negative regular-rate slice.
    """
    cached = min(usage.cached_input_tokens, usage.input_tokens)
    creation = min(usage.cache_creation_tokens, max(0, usage.input_tokens - cached))
    regular = max(0, usage.input_tokens - cached - creation)
    return (
        regular * _HAIKU_45_INPUT_PER_MTOK / 1_000_000
        + cached * _HAIKU_45_CACHED_INPUT_PER_MTOK / 1_000_000
        + creation * _HAIKU_45_CACHE_WRITE_PER_MTOK / 1_000_000
        + usage.output_tokens * _HAIKU_45_OUTPUT_PER_MTOK / 1_000_000
    )


@runtime_checkable
class LLMClient(Protocol):
    """Single-turn completion with optional system + cache."""

    def complete(self, *, system: str, user: str) -> LLMResponse:
        """Return the model's response to the system + user prompts."""
        ...


@dataclass
class FakeLLMClient:
    """Test double for `LLMClient`. Records every call."""

    text_provider: Callable[[str, str], str]
    model: str = "fake-model"
    calls: list[tuple[str, str]] = field(default_factory=list)

    def complete(self, *, system: str, user: str) -> LLMResponse:
        self.calls.append((system, user))
        text = self.text_provider(system, user)
        # Approximate token counts (≈4 chars per token) so pipeline cost
        # tests get realistic non-zero numbers without an actual tokenizer.
        return LLMResponse(
            text=text,
            model=self.model,
            usage=LLMUsage(
                input_tokens=max(1, len(system) // 4 + len(user) // 4),
                cached_input_tokens=0,
                output_tokens=max(1, len(text) // 4),
            ),
        )
