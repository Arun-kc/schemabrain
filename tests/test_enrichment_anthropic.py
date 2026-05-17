"""Tests for the Anthropic SDK adapter.

Uses mock SDK objects rather than calling the real API. The end-to-end
test (run separately) exercises a real call when ANTHROPIC_API_KEY is
present.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from schemabrain.enrichment.anthropic_client import (
    AnthropicClient,
    anthropic_haiku_45_client,
    anthropic_sonnet_46_client,
)
from schemabrain.enrichment.llm import LLMClient


@dataclass
class _FakeTextBlock:
    text: str
    type: str = "text"


@dataclass
class _FakeUsage:
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class _FakeMessage:
    content: list[Any]
    usage: _FakeUsage
    model: str = "claude-haiku-4-5"


class _FakeMessagesAPI:
    def __init__(self, response: _FakeMessage) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _FakeMessage:
        self.calls.append(kwargs)
        return self._response


class _FakeAnthropic:
    def __init__(self, response: _FakeMessage) -> None:
        self.messages = _FakeMessagesAPI(response)


def _make_client(response: _FakeMessage) -> tuple[AnthropicClient, _FakeAnthropic]:
    fake_sdk = _FakeAnthropic(response)
    return anthropic_haiku_45_client(client=fake_sdk), fake_sdk


class TestRealClientConstruction:
    def test_constructs_real_anthropic_when_no_client_injected(self) -> None:
        # Without `client=`, the constructor lazily imports anthropic
        # and instantiates it. The Anthropic() ctor is lazy — no network
        # call until messages.create — so we can build it with a fake
        # api_key without burning real credits.
        client = anthropic_haiku_45_client(api_key="sk-ant-fake-key-for-construction-test")
        assert client.model == "claude-haiku-4-5"

    def test_sdk_constructed_with_elevated_max_retries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Regression: Anthropic SDK defaults `max_retries=2`, which is
        # not enough to weather a sustained 50-RPM Tier-1 rate limit
        # during a multi-hundred-column index run. We pass an explicit
        # higher value so the SDK's built-in retry-after-aware backoff
        # has room to clear the window. Real workload that exposed this:
        # indexing BIRD Mini-Dev's 798 columns crashed at column 38 with
        # `anthropic.RateLimitError` and no retry.
        captured_kwargs: dict[str, object] = {}

        class _CapturingAnthropic:
            def __init__(self, **kwargs: object) -> None:
                captured_kwargs.update(kwargs)

        import anthropic

        monkeypatch.setattr(anthropic, "Anthropic", _CapturingAnthropic)
        anthropic_haiku_45_client(api_key="sk-ant-fake")
        assert captured_kwargs.get("max_retries") == 8, (
            "AnthropicClient must construct the SDK with max_retries=8 so the "
            "SDK's built-in 429 backoff handles rate-limited bursts. "
            f"Got: {captured_kwargs.get('max_retries')}"
        )


class TestProtocolConformance:
    def test_haiku_factory_satisfies_llm_client_protocol(self) -> None:
        fake = _FakeAnthropic(
            _FakeMessage(
                content=[_FakeTextBlock(text="x")],
                usage=_FakeUsage(input_tokens=1, output_tokens=1),
            )
        )
        client = anthropic_haiku_45_client(client=fake)
        assert isinstance(client, LLMClient)

    def test_sonnet_factory_satisfies_llm_client_protocol(self) -> None:
        fake = _FakeAnthropic(
            _FakeMessage(
                content=[_FakeTextBlock(text="x")],
                usage=_FakeUsage(input_tokens=1, output_tokens=1),
            )
        )
        client = anthropic_sonnet_46_client(client=fake)
        assert isinstance(client, LLMClient)


class TestComplete:
    def test_returns_text_from_content_block(self) -> None:
        client, _ = _make_client(
            _FakeMessage(
                content=[_FakeTextBlock(text="The user's email")],
                usage=_FakeUsage(input_tokens=100, output_tokens=10),
            )
        )
        resp = client.complete(system="sys", user="describe email")
        assert resp.text == "The user's email"

    def test_strips_whitespace_from_text(self) -> None:
        # The model occasionally emits a trailing newline. Trim it so the
        # stored description is tidy.
        client, _ = _make_client(
            _FakeMessage(
                content=[_FakeTextBlock(text="  some description  \n")],
                usage=_FakeUsage(input_tokens=10, output_tokens=5),
            )
        )
        resp = client.complete(system="sys", user="user")
        assert resp.text == "some description"

    def test_records_input_and_output_tokens(self) -> None:
        # Anthropic's input_tokens = regular-rate input only (no cache).
        # Our LLMUsage.input_tokens is the inclusive total (regular +
        # cache reads + cache writes).
        client, _ = _make_client(
            _FakeMessage(
                content=[_FakeTextBlock(text="x")],
                usage=_FakeUsage(input_tokens=1234, output_tokens=56),
            )
        )
        resp = client.complete(system="sys", user="user")
        # No cache → total == regular.
        assert resp.usage.input_tokens == 1234
        assert resp.usage.output_tokens == 56

    def test_records_cache_read_tokens_when_present(self) -> None:
        # On a cache hit, Anthropic returns cache_read_input_tokens > 0
        # AND a smaller `input_tokens` (the regular-rate slice). Our
        # LLMUsage.input_tokens is the SUM, so 100 regular + 900 cached
        # = 1000 total.
        client, _ = _make_client(
            _FakeMessage(
                content=[_FakeTextBlock(text="x")],
                usage=_FakeUsage(input_tokens=100, output_tokens=10, cache_read_input_tokens=900),
            )
        )
        resp = client.complete(system="sys", user="user")
        assert resp.usage.input_tokens == 1000  # regular + cached
        assert resp.usage.cached_input_tokens == 900

    def test_records_cache_creation_tokens_when_present(self) -> None:
        # First call of a session: Anthropic charges 1.25x for tokens
        # written into the cache. We track these as a separate subset.
        client, _ = _make_client(
            _FakeMessage(
                content=[_FakeTextBlock(text="x")],
                usage=_FakeUsage(
                    input_tokens=50,
                    output_tokens=10,
                    cache_creation_input_tokens=400,
                ),
            )
        )
        resp = client.complete(system="sys", user="user")
        assert resp.usage.cache_creation_tokens == 400
        # Total inclusive: regular + creation = 50 + 400 = 450.
        assert resp.usage.input_tokens == 450

    def test_zero_cache_read_when_field_missing(self) -> None:
        # Some SDK versions / call paths may not include the cache field.
        # The client must default it to zero, not crash.

        @dataclass
        class _UsageNoCache:
            input_tokens: int
            output_tokens: int

        client, _ = _make_client(
            _FakeMessage(
                content=[_FakeTextBlock(text="x")],
                usage=_UsageNoCache(input_tokens=10, output_tokens=2),  # type: ignore[arg-type]
            )
        )
        resp = client.complete(system="sys", user="user")
        assert resp.usage.cached_input_tokens == 0

    def test_reports_model_name(self) -> None:
        client, _ = _make_client(
            _FakeMessage(
                content=[_FakeTextBlock(text="x")],
                usage=_FakeUsage(input_tokens=1, output_tokens=1),
                model="claude-haiku-4-5-2026",
            )
        )
        resp = client.complete(system="sys", user="user")
        assert resp.model == "claude-haiku-4-5-2026"


class TestPromptCachingRequest:
    def test_marks_system_prompt_as_ephemeral_cache(self) -> None:
        # The SYSTEM_PROMPT is identical across every column-description
        # call. Marking it as ephemeral cache_control makes Anthropic
        # cache it (5min TTL) so the second through Nth calls in a run
        # only pay the cached rate for the system tokens.
        client, fake = _make_client(
            _FakeMessage(
                content=[_FakeTextBlock(text="x")],
                usage=_FakeUsage(input_tokens=1, output_tokens=1),
            )
        )
        client.complete(system="SHARED SYSTEM", user="per-column user")
        assert len(fake.messages.calls) == 1
        kwargs = fake.messages.calls[0]
        # System should be a list of content blocks with cache_control
        # on the system block (so the system prefix is cached).
        system = kwargs["system"]
        assert isinstance(system, list)
        assert system[0]["type"] == "text"
        assert system[0]["text"] == "SHARED SYSTEM"
        assert system[0]["cache_control"] == {"type": "ephemeral"}

    def test_user_message_is_not_cache_marked(self) -> None:
        # The user message changes per column (different name/type/etc),
        # so caching it would never hit. Only the system prefix is cached.
        client, fake = _make_client(
            _FakeMessage(
                content=[_FakeTextBlock(text="x")],
                usage=_FakeUsage(input_tokens=1, output_tokens=1),
            )
        )
        client.complete(system="sys", user="varying user")
        kwargs = fake.messages.calls[0]
        messages = kwargs["messages"]
        assert messages == [{"role": "user", "content": "varying user"}]


class TestModelSelection:
    def test_haiku_factory_uses_haiku_model(self) -> None:
        client, fake = _make_client(
            _FakeMessage(
                content=[_FakeTextBlock(text="x")],
                usage=_FakeUsage(input_tokens=1, output_tokens=1),
            )
        )
        client.complete(system="s", user="u")
        assert fake.messages.calls[0]["model"] == "claude-haiku-4-5"

    def test_sonnet_factory_uses_sonnet_model(self) -> None:
        fake = _FakeAnthropic(
            _FakeMessage(
                content=[_FakeTextBlock(text="x")],
                usage=_FakeUsage(input_tokens=1, output_tokens=1),
                model="claude-sonnet-4-6",
            )
        )
        client = anthropic_sonnet_46_client(client=fake)
        client.complete(system="s", user="u")
        assert fake.messages.calls[0]["model"] == "claude-sonnet-4-6"

    def test_sonnet_factory_uses_higher_max_output_tokens(self) -> None:
        # Sonnet handles cryptic columns and may need slightly longer
        # responses; the factory bumps max_tokens accordingly. If this
        # ever flips, Sonnet truncations would surface as mysterious
        # max_tokens errors despite Haiku passing the same prompt.
        haiku = anthropic_haiku_45_client(api_key="sk-ant-fake")
        sonnet = anthropic_sonnet_46_client(api_key="sk-ant-fake")
        # Access the protected attr — the public property only exposes
        # the model. The token cap isn't user-tunable today, so a pin.
        assert sonnet._max_output_tokens > haiku._max_output_tokens

    def test_class_requires_explicit_model(self) -> None:
        # No default model on the underlying class — callers must be
        # explicit, which is what the factories provide ergonomically.
        with pytest.raises(TypeError, match="model"):
            AnthropicClient(api_key="sk-ant-fake")  # type: ignore[call-arg]


class TestErrorHandling:
    def test_empty_content_raises(self) -> None:
        # If Anthropic somehow returns no content blocks, surface a
        # clean error rather than IndexError.
        client, _ = _make_client(
            _FakeMessage(
                content=[],
                usage=_FakeUsage(input_tokens=1, output_tokens=0),
            )
        )
        with pytest.raises(RuntimeError, match="empty"):
            client.complete(system="sys", user="user")

    def test_max_tokens_truncation_raises(self) -> None:
        # If Anthropic stops because it hit max_tokens, the reply is
        # mid-sentence — surface it as an error rather than store
        # garbage in the cache.
        @dataclass
        class _MessageWithStopReason:
            content: list[Any]
            usage: _FakeUsage
            model: str = "claude-haiku-4-5"
            stop_reason: str = "max_tokens"

        client, _ = _make_client(
            _MessageWithStopReason(  # type: ignore[arg-type]
                content=[_FakeTextBlock(text="this description got cut off mid-")],
                usage=_FakeUsage(input_tokens=1, output_tokens=200),
            )
        )
        with pytest.raises(RuntimeError, match="max_tokens"):
            client.complete(system="sys", user="user")

    def test_non_text_block_raises(self) -> None:
        # Anthropic could return a tool_use block. We only handle text.
        @dataclass
        class _ToolUseBlock:
            type: str = "tool_use"

        client, _ = _make_client(
            _FakeMessage(
                content=[_ToolUseBlock()],
                usage=_FakeUsage(input_tokens=1, output_tokens=1),
            )
        )
        with pytest.raises(RuntimeError, match="text"):
            client.complete(system="sys", user="user")
