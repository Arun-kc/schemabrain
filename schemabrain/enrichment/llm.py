"""LLM client abstraction.

Defines the Protocol every concrete client implements (Anthropic today,
hypothetically Bedrock or Vertex tomorrow) plus shared types and the
Haiku 4.5 cost calculator. The pipeline programs against `LLMClient`
so tests can substitute `FakeLLMClient` without an API key.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# Anthropic pricing tables (USD per 1M tokens). Update when Anthropic
# changes pricing — that's a deliberate cache invalidation if any
# downstream code stamps cost into a hash, but currently pricing is
# only used for runtime cost reporting + the --max-cost guard.
#
# Cache rates apply Anthropic's published structure:
#   - cache READ: 0.10x base input rate
#   - cache WRITE (ephemeral 5-min TTL): 1.25x base input rate

_HAIKU_45_INPUT_PER_MTOK = 0.80
_HAIKU_45_CACHED_INPUT_PER_MTOK = 0.08
_HAIKU_45_CACHE_WRITE_PER_MTOK = 1.00
_HAIKU_45_OUTPUT_PER_MTOK = 4.00

_SONNET_46_INPUT_PER_MTOK = 3.00
_SONNET_46_CACHED_INPUT_PER_MTOK = 0.30
_SONNET_46_CACHE_WRITE_PER_MTOK = 3.75
_SONNET_46_OUTPUT_PER_MTOK = 15.00


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


def _cost_usd(
    usage: LLMUsage,
    *,
    input_per_mtok: float,
    cached_per_mtok: float,
    cache_write_per_mtok: float,
    output_per_mtok: float,
) -> float:
    """Shared cost arithmetic. `input_tokens` is treated as the TOTAL
    input; `cached_input_tokens` and `cache_creation_tokens` are SUBSETS
    of it billed at their own rates. The remainder is billed at the
    standard input rate.

    Defensively clamps the subsets so a provider quirk that double-counts
    cache cannot produce a negative regular-rate slice.
    """
    cached = min(usage.cached_input_tokens, usage.input_tokens)
    creation = min(usage.cache_creation_tokens, max(0, usage.input_tokens - cached))
    regular = max(0, usage.input_tokens - cached - creation)
    return (
        regular * input_per_mtok / 1_000_000
        + cached * cached_per_mtok / 1_000_000
        + creation * cache_write_per_mtok / 1_000_000
        + usage.output_tokens * output_per_mtok / 1_000_000
    )


def haiku_45_cost_usd(usage: LLMUsage) -> float:
    """USD cost for one Haiku 4.5 call given its usage breakdown."""
    return _cost_usd(
        usage,
        input_per_mtok=_HAIKU_45_INPUT_PER_MTOK,
        cached_per_mtok=_HAIKU_45_CACHED_INPUT_PER_MTOK,
        cache_write_per_mtok=_HAIKU_45_CACHE_WRITE_PER_MTOK,
        output_per_mtok=_HAIKU_45_OUTPUT_PER_MTOK,
    )


def sonnet_46_cost_usd(usage: LLMUsage) -> float:
    """USD cost for one Sonnet 4.6 call given its usage breakdown."""
    return _cost_usd(
        usage,
        input_per_mtok=_SONNET_46_INPUT_PER_MTOK,
        cached_per_mtok=_SONNET_46_CACHED_INPUT_PER_MTOK,
        cache_write_per_mtok=_SONNET_46_CACHE_WRITE_PER_MTOK,
        output_per_mtok=_SONNET_46_OUTPUT_PER_MTOK,
    )


# Anchored patterns so the dispatch can't false-positive on a future
# model whose version string is a numeric extension of ours (e.g.
# `"claude-haiku-4-50"` would silently match a substring like
# `"haiku-4-5"`). The `(?:-.+)?$` allows the alias on its own OR
# followed by a dash-prefixed non-empty suffix (typically a date stamp
# like `-20251001`), but never a digit-extension like `-50` and never a
# trailing-only dash like `claude-haiku-4-5-`.
_HAIKU_4_5_MODEL_RE = re.compile(r"^claude-haiku-4-5(?:-.+)?$")
_SONNET_4_6_MODEL_RE = re.compile(r"^claude-sonnet-4-6(?:-.+)?$")


def cost_usd_for(model: str, usage: LLMUsage) -> float:
    """Dispatch to the right per-model cost function based on `model`.

    The Anthropic API stamps the resolved model name (e.g.
    `"claude-haiku-4-5-20251001"`) on the response, so we match on the
    family + version PREFIX with an anchored regex — not a substring
    `in` check — so unrelated models that happen to embed the same
    version digits cannot silently resolve to the wrong pricing table.

    Raises:
        ValueError: if `model` does not match any known pricing entry.
            Add the pricing constants and a regex above before routing
            traffic to a new model.
    """
    if _HAIKU_4_5_MODEL_RE.match(model):
        return haiku_45_cost_usd(usage)
    if _SONNET_4_6_MODEL_RE.match(model):
        return sonnet_46_cost_usd(usage)
    raise ValueError(
        f"unknown Anthropic model for cost calculation: {model!r}. "
        "Add a pricing entry in llm.py before routing to it."
    )


@runtime_checkable
class LLMClient(Protocol):
    """Single-turn completion with optional system + cache."""

    def complete(self, *, system: str, user: str) -> LLMResponse:
        """Return the model's response to the system + user prompts."""
        ...


@dataclass
class FakeLLMClient:
    """Test double for `LLMClient`. Records every call.

    The `model` default uses the Haiku 4.5 alias so the pipeline's
    `cost_usd_for(response.model, ...)` dispatch resolves to a real
    pricing function in tests instead of raising on an unknown model.
    Tests that need a different tier override `model=` explicitly.
    """

    text_provider: Callable[[str, str], str]
    model: str = "claude-haiku-4-5"
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
