"""Anthropic Claude Haiku 4.5 adapter.

Implements the `LLMClient` Protocol against the official `anthropic`
Python SDK. The shared system prompt (which never varies between
column-description calls in a single index run) is marked with ephemeral
`cache_control` so Anthropic caches it for 5 minutes — every call after
the first in a run pays the much cheaper cached input rate for the
system prefix.

The adapter accepts a pre-constructed SDK client via the `client=`
parameter, which lets tests inject a fake without instantiating the
real `Anthropic()` (no API key needed in the test environment).

**Do NOT call `AnthropicHaikuClient.complete` directly from application
code** — always go through `EnrichmentPipeline.enrich_column`, which
enforces the `--max-cost` cap. Calling `complete` directly bypasses the
cap and can produce uncapped LLM spend.
"""

from __future__ import annotations

from typing import Any

from schemabrain.enrichment.llm import LLMResponse, LLMUsage

__all__ = ["AnthropicHaikuClient"]

_DEFAULT_MODEL = "claude-haiku-4-5"
_DEFAULT_MAX_OUTPUT_TOKENS = 200


class AnthropicHaikuClient:
    """One-shot completion via Anthropic Claude Haiku 4.5.

    Caches the system prefix via Anthropic's ephemeral cache. The user
    message is per-column and not cache-marked.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        client: Any | None = None,
        model: str = _DEFAULT_MODEL,
        max_output_tokens: int = _DEFAULT_MAX_OUTPUT_TOKENS,
    ) -> None:
        if client is None:
            # Lazy import so the unit tests (which inject a fake client)
            # don't fail when ANTHROPIC_API_KEY is missing.
            from anthropic import Anthropic

            client = Anthropic(api_key=api_key)
        self._client = client
        self._model = model
        self._max_output_tokens = max_output_tokens

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
    # `max_tokens=200` is comfortably above 30 words; hitting it usually
    # means the model went off-script and produced a wall of text.
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
