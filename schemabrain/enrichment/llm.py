"""LLM client abstraction.

Defines the Protocol every concrete client implements (Anthropic today,
hypothetically Bedrock or Vertex tomorrow) plus shared types and the
Anthropic per-family pricing helpers. The pipeline programs against
`LLMClient` so tests can substitute `FakeLLMClient` without an API key.

**Provider-agnostic cost dispatch (Phase A seams commit).** Pricing
lives ON the `LLMClient` Protocol via `cost_usd(usage)` — each adapter
owns its own pricing. There is intentionally NO module-level
`cost_usd_for(model, usage)` central dispatch. A future provider drops
in by implementing `cost_usd` on its adapter, without touching this
module or the pipeline. See `tests/test_seams.py` for the invariants.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

CostFn = Callable[["LLMUsage"], float]

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


# Anchored patterns so the resolver can't false-positive on a future
# model whose version string is a numeric extension of ours (e.g.
# `"claude-haiku-4-50"` would silently match a substring like
# `"haiku-4-5"`). The `(?:-.+)?$` allows the alias on its own OR
# followed by a dash-prefixed non-empty suffix (typically a date stamp
# like `-20251001`), but never a digit-extension like `-50` and never a
# trailing-only dash like `claude-haiku-4-5-`.
_HAIKU_4_5_MODEL_RE = re.compile(r"^claude-haiku-4-5(?:-.+)?$")
_SONNET_4_6_MODEL_RE = re.compile(r"^claude-sonnet-4-6(?:-.+)?$")


def anthropic_cost_fn_for_model(model: str) -> CostFn:
    """Resolve the Anthropic cost function for `model`, at CONSTRUCTION time.

    Each Anthropic adapter (`AnthropicClient`, `FakeLLMClient`) calls
    this once in its constructor and stashes the result. Two reasons:

    1. **Fail-fast at ctor.** A misrouted model surfaces immediately,
       not on the first successful API call (which would have burned
       money before the cost path tripped).
    2. **Pricing dispatch is intrinsic to the adapter, not central.**
       `LLMClient.cost_usd(usage)` then becomes a single function-call
       indirection on the stashed reference — no per-call regex match,
       no central dispatch table to update for a new provider.

    Whitespace tolerance: leading/trailing whitespace is stripped before
    matching. Anthropic API responses have been observed returning model
    names with formatting artifacts; without this tolerance a benign
    whitespace artifact in `response.model` would silently raise
    `ValueError` when a future caller passes that string back through —
    breaking production while every test with literal model names passes.
    Per type-design audit 2026-05-15.

    Raises:
        ValueError: if `model` does not match any Anthropic family this
            module has pricing for (after whitespace strip). A new
            family lands by adding a regex + cost function above and a
            branch here.
    """
    model = model.strip()
    if _HAIKU_4_5_MODEL_RE.match(model):
        return haiku_45_cost_usd
    if _SONNET_4_6_MODEL_RE.match(model):
        return sonnet_46_cost_usd
    raise ValueError(
        f"unknown Anthropic model for pricing: {model!r}. "
        "Add a pricing entry in llm.py (new regex + cost function + "
        "branch in anthropic_cost_fn_for_model) before routing to it."
    )


@runtime_checkable
class LLMClient(Protocol):
    """Single-turn completion + per-client cost dispatch.

    Each adapter owns its pricing (`cost_usd(usage)`). There is no
    module-level Anthropic-only central dispatch; adding a new
    provider means writing an adapter that implements both methods.
    """

    def complete(self, *, system: str, user: str) -> LLMResponse:
        """Return the model's response to the system + user prompts."""
        ...

    def cost_usd(self, usage: LLMUsage) -> float:
        """Compute USD cost for this client's last call given its usage.

        The pipeline does not know or care which provider is on the
        other end — it just calls `cost_usd` on the same client object
        it called `complete` on. Tests can substitute clients with
        arbitrary pricing without touching the pipeline.

        Returns:
            A non-negative, finite USD cost. Implementations MUST NOT
            return negative values, `math.inf`, or `nan` — the pipeline
            adds this directly to its cumulative spend counter and a
            non-finite value would corrupt the cost-cap invariant
            silently. If the underlying provider returns malformed
            usage data, raise `RuntimeError` (or a more specific
            exception) instead — that propagates cleanly through
            `EnrichmentPipeline.enrich_column`. Per type-design audit
            2026-05-15.
        """
        ...


class FakeLLMClient:
    """Test double for `LLMClient`. Records every call.

    The `model` default uses the Haiku 4.5 alias so `cost_usd` resolves
    to Haiku pricing in tests. Tests that need a different tier
    override `model=` explicitly. Unknown models raise at construction
    — same fail-fast shape as `AnthropicClient`, so a test passing on
    the fake won't fail on a real client.

    **`model` is read-only after construction.** Mutating it post-ctor
    would leave `_cost_fn` stale and silently mis-price subsequent
    calls. To switch tiers, construct a new `FakeLLMClient` with the
    desired model. This matches `AnthropicClient`'s shape. Per
    type-design audit 2026-05-15.

    **Scope: Anthropic-priced models only.** The model name is resolved
    via `anthropic_cost_fn_for_model`, so this fake validates against
    the Anthropic regex set. Tests that need to exercise the seam
    against a hypothetical non-Anthropic provider should write a
    purpose-built inline `LLMClient` (see `FlatRateClient` in
    `tests/test_seams.py` for the pattern). The seam exists precisely
    so future providers don't have to extend `FakeLLMClient`.
    """

    def __init__(
        self,
        *,
        text_provider: Callable[[str, str], str],
        model: str = "claude-haiku-4-5",
        calls: list[tuple[str, str]] | None = None,
    ) -> None:
        # Resolve pricing once at construction — same shape as
        # AnthropicClient. A test that constructs the fake with an
        # unknown model tripping ValueError here is by design: the
        # production adapter would have done the same.
        self._cost_fn: CostFn = anthropic_cost_fn_for_model(model)
        self._text_provider = text_provider
        self._model = model
        # `calls` is a public, mutable recorder of each `complete` call.
        # Tests inspect it to assert call counts and arguments. A fresh
        # list per instance (NOT a class-level mutable default) avoids
        # cross-test bleed.
        self.calls: list[tuple[str, str]] = calls if calls is not None else []

    @property
    def model(self) -> str:
        """The model name this client targets. Read-only by design — see
        the class docstring for the rationale.
        """
        return self._model

    def complete(self, *, system: str, user: str) -> LLMResponse:
        self.calls.append((system, user))
        text = self._text_provider(system, user)
        # Approximate token counts (≈4 chars per token) so pipeline cost
        # tests get realistic non-zero numbers without an actual tokenizer.
        return LLMResponse(
            text=text,
            model=self._model,
            usage=LLMUsage(
                input_tokens=max(1, len(system) // 4 + len(user) // 4),
                cached_input_tokens=0,
                output_tokens=max(1, len(text) // 4),
            ),
        )

    def cost_usd(self, usage: LLMUsage) -> float:
        return self._cost_fn(usage)
