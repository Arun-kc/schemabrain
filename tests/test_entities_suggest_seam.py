"""Tests for the LLMClient integration point on the suggest pipeline.

The suggest pipeline reuses the existing `LLMClient` Protocol from
`schemabrain/enrichment/llm.py` rather than inventing a new provider
seam. This test file pins the contract that
the suggest pipeline programs against the Protocol and accepts any
conforming client — including the canonical `FakeLLMClient` test
double already shipped for the enrichment pipeline.

The pipeline's actual prompt-building and response-parsing logic
lands in step 2 (`test_entities_suggest_pipeline.py`). Step 1 only
verifies the seam: that `EntitySuggestionPipeline(llm=...)` accepts
an `LLMClient`, calls its `complete` method correctly, and threads
the response through.
"""

from __future__ import annotations

from schemabrain.enrichment.llm import FakeLLMClient, LLMClient
from schemabrain.entities.suggest import EntitySuggestionPipeline


def _stub_text(_system: str, _user: str) -> str:
    # Step 1 doesn't exercise parsing yet — the pipeline accepts any
    # text and the seam test only inspects whether `complete` was
    # called with the system + user we passed in. The pipeline's
    # parser lands in step 2.
    return "stub response"


class TestPipelineAcceptsLLMClient:
    def test_constructor_accepts_fake_client(self) -> None:
        # FakeLLMClient is the canonical Protocol-conforming test
        # double. If the pipeline accepts it, any other adapter
        # implementing `complete` + `cost_usd` works too.
        client = FakeLLMClient(text_provider=_stub_text)
        pipeline = EntitySuggestionPipeline(llm=client)
        assert pipeline._llm is client

    def test_constructor_accepts_anthropic_client_via_protocol(self) -> None:
        # Verify the typing surface: a function annotated `LLMClient`
        # accepts whatever shape it likes. We don't construct a real
        # AnthropicClient here (no API key in CI), but the type
        # annotation alone is the contract.
        def _factory(llm: LLMClient) -> EntitySuggestionPipeline:
            return EntitySuggestionPipeline(llm=llm)

        client = FakeLLMClient(text_provider=_stub_text)
        pipeline = _factory(client)
        assert pipeline._llm is client


class TestPipelineCallsLLMComplete:
    def test_pipeline_records_call_through_to_complete(self) -> None:
        # Step 1's minimal `_invoke_llm` helper exists only to pin the
        # seam — it sends a system + user prompt and returns the raw
        # response. Step 2 will wrap this with schema-to-prompt and
        # response-to-candidates conversion.
        client = FakeLLMClient(text_provider=_stub_text)
        pipeline = EntitySuggestionPipeline(llm=client)

        response = pipeline._invoke_llm(system="sys prompt", user="user prompt")

        assert len(client.calls) == 1
        assert client.calls[0] == ("sys prompt", "user prompt")
        assert response.text == "stub response"
        assert response.model == "claude-haiku-4-5"  # FakeLLMClient default
        # Usage is populated from FakeLLMClient's approximation
        assert response.usage.input_tokens > 0
        assert response.usage.output_tokens > 0

    def test_pipeline_cost_dispatch_uses_client(self) -> None:
        # Cost flows through `LLMClient.cost_usd(usage)` — the pipeline
        # MUST NOT compute cost itself. Provider-owned dispatch keeps
        # the seam clean: a future Bedrock adapter prices its own
        # calls without the pipeline knowing.
        client = FakeLLMClient(text_provider=_stub_text)
        pipeline = EntitySuggestionPipeline(llm=client)

        response = pipeline._invoke_llm(system="s", user="u")
        cost = pipeline._cost_for(response)

        # Must be non-negative + finite (the LLMClient.cost_usd
        # contract). Cheap to assert here; corruption would break
        # the step-3 cost ceiling.
        assert cost >= 0.0
        # And it MUST equal what the client itself reports — the
        # pipeline does not transform or round.
        assert cost == client.cost_usd(response.usage)
