"""Anthropic SDK adapter (Claude Haiku 4.5 or Sonnet 4.6).

Implements the `LLMClient` Protocol against the official `anthropic`
Python SDK. The shared system prompt (which never varies between
column-description calls in a single index run) is marked with ephemeral
`cache_control` so Anthropic caches it for 5 minutes — every call after
the first in a run pays the much cheaper cached input rate for the
system prefix, when the prefix is large enough to qualify (≥4096 tokens
for Haiku 4.5, ≥2048 for Sonnet 4.6 per the Anthropic prompt-caching
docs, verified 2026-05-13). Below threshold, `cache_control` is
silently no-op'd — no error, both `cache_creation_input_tokens` and
`cache_read_input_tokens` come back as 0. Our SYSTEM_PROMPT is far
under either threshold today, so the marker is a no-op until the
prefix grows (e.g. sibling context, examples).

The class is model-agnostic: pass any Anthropic model name and the
matching `max_output_tokens`. For ergonomics, prefer the factory
functions `anthropic_haiku_45_client()` and `anthropic_sonnet_46_client()`,
which set sensible defaults for each tier.

The adapter accepts a pre-constructed SDK client via the `client=`
parameter, which lets tests inject a fake without instantiating the
real `Anthropic()` (no API key needed in the test environment).

**Do NOT call `AnthropicClient.complete` directly from application
code** — always go through `EnrichmentPipeline.enrich_column`, which
enforces the `--max-cost` cap. Calling `complete` directly bypasses the
cap and can produce uncapped LLM spend.
"""

from __future__ import annotations

from typing import Any

from schemabrain.enrichment.llm import (
    CostFn,
    LLMResponse,
    LLMUsage,
    anthropic_cost_fn_for_model,
)

__all__ = [
    "AnthropicClient",
    "anthropic_haiku_45_client",
    "anthropic_sonnet_46_client",
]

_HAIKU_45_MODEL = "claude-haiku-4-5"
_SONNET_46_MODEL = "claude-sonnet-4-6"

# Haiku is asked for one short sentence (≤30 words). 200 is comfortably
# above that; hitting it indicates the model went off-prompt.
_HAIKU_DEFAULT_MAX_OUTPUT_TOKENS = 200

# Sonnet handles cryptic columns that may need a slightly longer
# explanation to convey what an abbreviated name actually represents.
# Still bounded so a runaway response surfaces as a max_tokens error.
_SONNET_DEFAULT_MAX_OUTPUT_TOKENS = 300


class AnthropicClient:
    """One-shot completion via Anthropic Claude (Haiku or Sonnet).

    Caches the system prefix via Anthropic's ephemeral cache. The user
    message is per-column and not cache-marked.
    """

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        client: Any | None = None,
        max_output_tokens: int = _HAIKU_DEFAULT_MAX_OUTPUT_TOKENS,
    ) -> None:
        # Resolve pricing at construction. A misrouted / typo'd model
        # name raises here BEFORE any API call is made — so we never
        # successfully burn money on a call whose cost we can't compute.
        # See `anthropic_cost_fn_for_model` for the regex contract.
        self._cost_fn: CostFn = anthropic_cost_fn_for_model(model)
        if client is None:
            # Lazy import so the unit tests (which inject a fake client)
            # don't fail when ANTHROPIC_API_KEY is missing.
            from anthropic import Anthropic

            client = Anthropic(api_key=api_key)
        self._client = client
        self._model = model
        self._max_output_tokens = max_output_tokens

    @property
    def model(self) -> str:
        """The model name this client targets (e.g. `"claude-haiku-4-5"`)."""
        return self._model

    def cost_usd(self, usage: LLMUsage) -> float:
        """USD cost for this client's `usage`, using the family rate
        resolved at construction. No `response.model` parsing — the
        client knows its own pricing family from the model it was
        configured with.
        """
        return self._cost_fn(usage)

    def complete(self, *, system: str, user: str) -> LLMResponse:
        message = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_output_tokens,
            system=[
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user}],
        )
        return LLMResponse(
            text=_extract_text(message),
            model=getattr(message, "model", self._model),
            usage=_extract_usage(message),
        )


def anthropic_haiku_45_client(
    *,
    api_key: str | None = None,
    client: Any | None = None,
) -> AnthropicClient:
    """Construct an `AnthropicClient` configured for Claude Haiku 4.5."""
    return AnthropicClient(
        model=_HAIKU_45_MODEL,
        api_key=api_key,
        client=client,
        max_output_tokens=_HAIKU_DEFAULT_MAX_OUTPUT_TOKENS,
    )


def anthropic_sonnet_46_client(
    *,
    api_key: str | None = None,
    client: Any | None = None,
) -> AnthropicClient:
    """Construct an `AnthropicClient` configured for Claude Sonnet 4.6.

    Used by the enrichment pipeline for columns that the routing
    heuristic flags as cryptic (e.g. `acct_dim_v3`, `pmt_fct_h`) where
    Sonnet's deeper reasoning is worth the ~5x cost over Haiku.
    """
    return AnthropicClient(
        model=_SONNET_46_MODEL,
        api_key=api_key,
        client=client,
        max_output_tokens=_SONNET_DEFAULT_MAX_OUTPUT_TOKENS,
    )


def _extract_text(message: Any) -> str:
    content = message.content
    if not content:
        raise RuntimeError("Anthropic returned an empty content list")
    block = content[0]
    block_type = getattr(block, "type", None)
    if block_type != "text":
        raise RuntimeError(
            f"Anthropic returned a non-text first block (type={block_type!r}); "
            "the prompt must elicit a plain-text reply"
        )
    # Surface a truncated reply rather than silently storing a half-sentence.
    # `max_tokens` is comfortably above the expected reply length;
    # hitting it usually means the model went off-script.
    if getattr(message, "stop_reason", None) == "max_tokens":
        raise RuntimeError(
            "Anthropic hit max_tokens before finishing the description. "
            "The reply was truncated; either the model went off-prompt or "
            "max_output_tokens needs raising."
        )
    return block.text.strip()


def _extract_usage(message: Any) -> LLMUsage:
    """Convert Anthropic's separated token counts back into our inclusive form.

    Anthropic's SDK reports `usage.input_tokens` as the count NOT in
    cache (regular-rate) plus separate `cache_read_input_tokens` and
    `cache_creation_input_tokens` fields. Our `LLMUsage.input_tokens`
    is defined as the inclusive total, with cached/creation as subsets,
    so we sum the three Anthropic counts to get the total.
    """
    usage = message.usage
    cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    cache_create = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    regular = int(usage.input_tokens)
    return LLMUsage(
        input_tokens=regular + cache_read + cache_create,
        cached_input_tokens=cache_read,
        output_tokens=int(usage.output_tokens),
        cache_creation_tokens=cache_create,
    )
