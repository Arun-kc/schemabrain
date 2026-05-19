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
which set sensible defaults for each tier. Both factories honor per-tier
env-var overrides (`SCHEMABRAIN_HAIKU_MAX_OUTPUT_TOKENS`,
`SCHEMABRAIN_SONNET_MAX_OUTPUT_TOKENS`) for operators who need a
different token budget without a code change.

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

from schemabrain._env import resolve_positive_int_env
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

# Anthropic SDK defaults `max_retries=2`. That's not enough to weather a
# sustained Tier-1 rate-limit window (50 RPM) during an index run that
# fans out hundreds of column-description calls. The SDK already
# implements retry-after-aware exponential backoff; we just give it more
# room. 8 retries with the SDK's default backoff caps the worst-case
# tail latency for a single call at ~60s, well under any operator's
# patience for an indexing pass that already takes minutes.
_SDK_MAX_RETRIES = 8

# Haiku is asked for one short sentence (≤30 words) per column. 200 is
# comfortably above that; hitting it indicates the model went off-prompt
# or the column-description schema convention is unusually verbose
# (long localized names, multi-language hints). Operators on verbose
# schemas can raise it via SCHEMABRAIN_HAIKU_MAX_OUTPUT_TOKENS without
# a code change.
_HAIKU_DEFAULT_MAX_OUTPUT_TOKENS = 200
_HAIKU_MAX_OUTPUT_TOKENS_ENV = "SCHEMABRAIN_HAIKU_MAX_OUTPUT_TOKENS"

# Sonnet handles two distinct workloads through this client today:
# (a) cryptic-column descriptions (one paragraph each), and
# (b) entity / metric suggestion (multi-candidate YAML, typically 10+
#     entries with rationale and PII hints).
# The previous default (300) was sized for (a) only and silently
# truncated (b) on schemas with more than ~5 entities — discovered via
# the 2026-05-19 ecommerce-fixture smoke. 4096 comfortably fits 10+
# candidates with room to spare while still bounding a runaway response.
# Sonnet 4.6 supports much higher (8192+); callers who need it can set
# SCHEMABRAIN_SONNET_MAX_OUTPUT_TOKENS or construct AnthropicClient
# directly with `max_output_tokens=`.
_SONNET_DEFAULT_MAX_OUTPUT_TOKENS = 4096
_SONNET_MAX_OUTPUT_TOKENS_ENV = "SCHEMABRAIN_SONNET_MAX_OUTPUT_TOKENS"

# Per-tier max output tokens resolution delegates to the shared
# `schemabrain._env` parser (factored out 2026-05-19 from the original
# PR #67 implementation that lived here). The shared module's
# `on_invalid="raise"` mode preserves PR #67's "fail fast on a
# typo'd cap rather than send a nonsensical max_tokens to the API and
# chase an opaque 400" contract. Test coverage of the strict-regex
# footguns now lives in `tests/test_env_resolution.py`; behavior
# pins on this module's factories stay in
# `tests/test_enrichment_anthropic.py::TestMaxOutputTokensConfiguration`.


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

            client = Anthropic(api_key=api_key, max_retries=_SDK_MAX_RETRIES)
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
    """Construct an `AnthropicClient` configured for Claude Haiku 4.5.

    `max_output_tokens` resolves from `SCHEMABRAIN_HAIKU_MAX_OUTPUT_TOKENS`
    when set, falling back to the package default (see
    `_HAIKU_DEFAULT_MAX_OUTPUT_TOKENS`).
    """
    return AnthropicClient(
        model=_HAIKU_45_MODEL,
        api_key=api_key,
        client=client,
        max_output_tokens=resolve_positive_int_env(
            _HAIKU_MAX_OUTPUT_TOKENS_ENV,
            _HAIKU_DEFAULT_MAX_OUTPUT_TOKENS,
        ),
    )


def anthropic_sonnet_46_client(
    *,
    api_key: str | None = None,
    client: Any | None = None,
) -> AnthropicClient:
    """Construct an `AnthropicClient` configured for Claude Sonnet 4.6.

    Used by the enrichment pipeline for columns that the routing
    heuristic flags as cryptic (e.g. `acct_dim_v3`, `pmt_fct_h`), and
    by the wizard's stage-3/4 entity- and metric-suggestion code
    paths (which depend on Sonnet's multi-candidate YAML responses).
    `max_output_tokens` resolves from `SCHEMABRAIN_SONNET_MAX_OUTPUT_TOKENS`
    when set, falling back to the package default (see
    `_SONNET_DEFAULT_MAX_OUTPUT_TOKENS`).
    """
    return AnthropicClient(
        model=_SONNET_46_MODEL,
        api_key=api_key,
        client=client,
        max_output_tokens=resolve_positive_int_env(
            _SONNET_MAX_OUTPUT_TOKENS_ENV,
            _SONNET_DEFAULT_MAX_OUTPUT_TOKENS,
        ),
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
