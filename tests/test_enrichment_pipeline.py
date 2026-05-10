"""Tests for the column-enrichment pipeline.

The pipeline coordinates: build prompt → call LLM → track cost →
return ColumnDescription. Cost is capped — exceeding the cap aborts
the run BEFORE the next call (so the user never gets billed past the
cap they asked for).
"""

from __future__ import annotations

import pytest

from schemabrain.core.models import Column, Table
from schemabrain.enrichment.llm import FakeLLMClient, LLMResponse, LLMUsage
from schemabrain.enrichment.pipeline import (
    ColumnDescription,
    CostCapExceeded,
    EnrichmentPipeline,
)
from schemabrain.profiler.stats import ColumnStats


def _users_table() -> Table:
    return Table(
        name="users",
        schema_name="public",
        columns=(
            Column(
                name="id",
                table_name="users",
                schema_name="public",
                data_type="BIGINT",
                nullable=False,
                ordinal_position=1,
                is_primary_key=True,
            ),
            Column(
                name="email",
                table_name="users",
                schema_name="public",
                data_type="TEXT",
                nullable=False,
                ordinal_position=2,
            ),
        ),
    )


def _stats_for_email() -> ColumnStats:
    return ColumnStats(
        column_name="email",
        total_rows=100,
        null_count=0,
        distinct_count=100,
        sample_values=("<EMAIL>",),
        shape_patterns=("aaaa@aaaa.aa",),
    )


class TestColumnDescription:
    def test_is_frozen(self) -> None:
        desc = ColumnDescription(
            text="user email address",
            model="claude-haiku-4-5",
            prompt_version="2026-05-10-1",
            input_tokens=100,
            cached_input_tokens=0,
            output_tokens=10,
            cost_usd=0.0001,
        )
        try:
            desc.text = "x"  # type: ignore[misc]
        except Exception:
            return
        raise AssertionError("ColumnDescription should be frozen")


class TestEnrichmentPipelineHappyPath:
    def test_returns_description_with_llm_text(self) -> None:
        client = FakeLLMClient(text_provider=lambda system, user: "User email address")
        pipeline = EnrichmentPipeline(client=client)
        table = _users_table()
        email_col = table.columns[1]

        desc = pipeline.enrich_column(
            table=table,
            column=email_col,
            stats=_stats_for_email(),
            fk_targets=(),
        )

        assert desc.text == "User email address"
        # FakeLLMClient defaults to a Haiku-family model name so the
        # pipeline's cost dispatch resolves cleanly in tests.
        assert desc.model == "claude-haiku-4-5"
        assert desc.prompt_version  # whatever the live PROMPT_VERSION is
        assert desc.input_tokens > 0
        assert desc.output_tokens > 0
        assert desc.cost_usd > 0.0

    def test_passes_system_prompt_and_rendered_user_prompt(self) -> None:
        # Verify the pipeline sends the SYSTEM_PROMPT (cache prefix) and
        # the rendered user prompt with column context.
        client = FakeLLMClient(text_provider=lambda system, user: "x")
        pipeline = EnrichmentPipeline(client=client)
        table = _users_table()

        pipeline.enrich_column(
            table=table,
            column=table.columns[1],
            stats=_stats_for_email(),
            fk_targets=("public.contacts.email",),
        )

        assert len(client.calls) == 1
        system, user = client.calls[0]
        # System: must include the PII rule (proxy for SYSTEM_PROMPT identity).
        assert "pii" in system.lower() or "PII" in system
        # User: must mention this specific column and table.
        assert "Column: email" in user
        assert "public.users" in user
        assert "public.contacts.email" in user

    def test_tracks_cumulative_spend(self) -> None:
        client = FakeLLMClient(text_provider=lambda system, user: "x")
        pipeline = EnrichmentPipeline(client=client)
        table = _users_table()
        for col in table.columns:
            pipeline.enrich_column(table=table, column=col, stats=None, fk_targets=())
        # Two calls, cumulative spend is sum of per-call costs.
        assert pipeline.spent_usd > 0.0
        assert len(client.calls) == 2


class TestCostCap:
    def test_default_cap_is_ten_dollars(self) -> None:
        pipeline = EnrichmentPipeline(client=FakeLLMClient(text_provider=lambda s, u: "x"))
        assert pipeline.max_cost_usd == 10.0

    def test_call_after_cap_exceeded_raises(self) -> None:
        # Build a client whose responses cost a known amount: a huge text
        # to inflate output tokens (~$5 per call).
        big_text = "x" * 5_000_000
        client = FakeLLMClient(text_provider=lambda s, u: big_text)
        pipeline = EnrichmentPipeline(client=client, max_cost_usd=1.0)
        table = _users_table()

        # First call — passes the cap check (spent=0 < 1.0), goes through.
        # After the call, spent is ~$5 (well over the cap).
        pipeline.enrich_column(table=table, column=table.columns[0], stats=None, fk_targets=())
        assert len(client.calls) == 1
        assert pipeline.spent_usd > pipeline.max_cost_usd

        # Second call — cap check fires, refuses without calling the LLM.
        with pytest.raises(CostCapExceeded) as exc_info:
            pipeline.enrich_column(table=table, column=table.columns[1], stats=None, fk_targets=())
        assert exc_info.value.spent > exc_info.value.cap
        assert len(client.calls) == 1, "no second call should have fired"

    def test_cap_check_happens_before_call(self) -> None:
        # If we're already at the cap, the next enrich must NOT call the
        # LLM at all — the cap is a "we've seen enough" guard.
        import contextlib

        client = FakeLLMClient(text_provider=lambda s, u: "x" * 5_000_000)
        pipeline = EnrichmentPipeline(client=client, max_cost_usd=0.5)
        table = _users_table()
        with contextlib.suppress(CostCapExceeded):
            pipeline.enrich_column(table=table, column=table.columns[0], stats=None, fk_targets=())
        calls_before = len(client.calls)
        with pytest.raises(CostCapExceeded):
            pipeline.enrich_column(table=table, column=table.columns[1], stats=None, fk_targets=())
        assert len(client.calls) == calls_before, "no LLM call should fire after cap"

    def test_zero_cap_blocks_first_call(self) -> None:
        client = FakeLLMClient(text_provider=lambda s, u: "x")
        pipeline = EnrichmentPipeline(client=client, max_cost_usd=0.0)
        table = _users_table()
        with pytest.raises(CostCapExceeded):
            pipeline.enrich_column(table=table, column=table.columns[0], stats=None, fk_targets=())
        assert client.calls == []

    def test_negative_cap_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_cost_usd"):
            EnrichmentPipeline(
                client=FakeLLMClient(text_provider=lambda s, u: "x"),
                max_cost_usd=-1.0,
            )


class TestPromptVersionBindsToDescription:
    def test_description_records_live_prompt_version(self) -> None:
        # Whatever PROMPT_VERSION the prompts module exports today must
        # appear on the produced ColumnDescription, so re-enrichment can
        # detect prompt-version drift later without re-hashing.
        from schemabrain.enrichment.prompts import PROMPT_VERSION

        client = FakeLLMClient(text_provider=lambda s, u: "x")
        pipeline = EnrichmentPipeline(client=client)
        table = _users_table()
        desc = pipeline.enrich_column(
            table=table, column=table.columns[0], stats=None, fk_targets=()
        )
        assert desc.prompt_version == PROMPT_VERSION


class TestCostReporting:
    def test_response_usage_fields_propagate(self) -> None:
        # When the LLM client reports cached input tokens (e.g. Anthropic
        # prompt cache hit), the description should record them so the
        # CLI can show cache effectiveness.
        class _CachingClient:
            def __init__(self) -> None:
                self.model = "claude-haiku-4-5"

            def complete(self, *, system: str, user: str) -> LLMResponse:
                return LLMResponse(
                    text="cached!",
                    model=self.model,
                    usage=LLMUsage(input_tokens=1000, cached_input_tokens=900, output_tokens=20),
                )

        pipeline = EnrichmentPipeline(client=_CachingClient())
        table = _users_table()
        desc = pipeline.enrich_column(
            table=table, column=table.columns[0], stats=None, fk_targets=()
        )
        assert desc.cached_input_tokens == 900
        assert desc.input_tokens == 1000
        assert desc.output_tokens == 20


def _cryptic_table() -> Table:
    """Table with a deliberately cryptic column name (per the plan's example)."""
    return Table(
        name="ledger",
        schema_name="public",
        columns=(
            Column(
                name="id",
                table_name="ledger",
                schema_name="public",
                data_type="BIGINT",
                nullable=False,
                ordinal_position=1,
                is_primary_key=True,
            ),
            Column(
                name="acct_dim_v3",
                table_name="ledger",
                schema_name="public",
                data_type="JSONB",
                nullable=True,
                ordinal_position=2,
            ),
        ),
    )


class TestTwoTierRouting:
    """Single-client mode is the default; cryptic-tier kicks in only
    when a second client is wired."""

    def test_single_client_mode_routes_everything_to_default(self) -> None:
        # No cryptic_client → both clear and cryptic columns hit the
        # default client. Backward-compatible with everything before.
        haiku = FakeLLMClient(text_provider=lambda s, u: "x", model="claude-haiku-4-5")
        pipeline = EnrichmentPipeline(client=haiku)
        assert pipeline.has_cryptic_tier is False

        table = _cryptic_table()
        for col in table.columns:
            pipeline.enrich_column(table=table, column=col, stats=None, fk_targets=())
        assert len(haiku.calls) == 2

    def test_clear_column_routes_to_default_client(self) -> None:
        haiku = FakeLLMClient(text_provider=lambda s, u: "clear", model="claude-haiku-4-5")
        sonnet = FakeLLMClient(text_provider=lambda s, u: "sonnet", model="claude-sonnet-4-6")
        pipeline = EnrichmentPipeline(client=haiku, cryptic_client=sonnet)
        assert pipeline.has_cryptic_tier is True

        table = _users_table()  # only `id` and `email` — both clear
        for col in table.columns:
            pipeline.enrich_column(table=table, column=col, stats=None, fk_targets=())
        assert len(haiku.calls) == 2
        assert len(sonnet.calls) == 0

    def test_cryptic_column_routes_to_cryptic_client(self) -> None:
        haiku = FakeLLMClient(text_provider=lambda s, u: "clear", model="claude-haiku-4-5")
        sonnet = FakeLLMClient(
            text_provider=lambda s, u: "sonnet-explained", model="claude-sonnet-4-6"
        )
        pipeline = EnrichmentPipeline(client=haiku, cryptic_client=sonnet)

        table = _cryptic_table()  # `id` clear, `acct_dim_v3` cryptic
        for col in table.columns:
            pipeline.enrich_column(table=table, column=col, stats=None, fk_targets=())
        assert len(haiku.calls) == 1, "clear column must use default client"
        assert len(sonnet.calls) == 1, "cryptic column must use cryptic client"

    def test_chosen_model_appears_on_description(self) -> None:
        # The model name on the produced ColumnDescription tells us
        # which tier actually handled each column — useful for both
        # debugging and for the final cost-tier breakdown.
        haiku = FakeLLMClient(text_provider=lambda s, u: "clear", model="claude-haiku-4-5")
        sonnet = FakeLLMClient(text_provider=lambda s, u: "sonnet", model="claude-sonnet-4-6")
        pipeline = EnrichmentPipeline(client=haiku, cryptic_client=sonnet)
        table = _cryptic_table()
        clear_desc = pipeline.enrich_column(
            table=table, column=table.columns[0], stats=None, fk_targets=()
        )
        cryptic_desc = pipeline.enrich_column(
            table=table, column=table.columns[1], stats=None, fk_targets=()
        )
        assert clear_desc.model == "claude-haiku-4-5"
        assert cryptic_desc.model == "claude-sonnet-4-6"

    def test_cumulative_spend_includes_both_tiers(self) -> None:
        # The cap is shared across BOTH clients — Sonnet calls add to
        # the same counter that Haiku calls add to.
        haiku = FakeLLMClient(text_provider=lambda s, u: "x", model="claude-haiku-4-5")
        sonnet = FakeLLMClient(text_provider=lambda s, u: "x", model="claude-sonnet-4-6")
        pipeline = EnrichmentPipeline(client=haiku, cryptic_client=sonnet)
        table = _cryptic_table()
        for col in table.columns:
            pipeline.enrich_column(table=table, column=col, stats=None, fk_targets=())
        # Both calls happened; both contributed to spend.
        assert pipeline.spent_usd > 0.0
        assert len(haiku.calls) == 1
        assert len(sonnet.calls) == 1

    def test_cap_fires_regardless_of_tier(self) -> None:
        # If spend hit the cap on a Haiku call, the next call (whether
        # it would route to Haiku or Sonnet) is refused.
        big = "x" * 5_000_000
        haiku = FakeLLMClient(text_provider=lambda s, u: big, model="claude-haiku-4-5")
        sonnet = FakeLLMClient(text_provider=lambda s, u: "small", model="claude-sonnet-4-6")
        pipeline = EnrichmentPipeline(client=haiku, cryptic_client=sonnet, max_cost_usd=0.5)
        table = _cryptic_table()
        # First call (clear column → haiku) blows the cap.
        pipeline.enrich_column(table=table, column=table.columns[0], stats=None, fk_targets=())
        assert pipeline.spent_usd > pipeline.max_cost_usd
        # Second call (cryptic → would route to sonnet) is refused
        # before either client is touched.
        with pytest.raises(CostCapExceeded):
            pipeline.enrich_column(table=table, column=table.columns[1], stats=None, fk_targets=())
        assert len(sonnet.calls) == 0, "sonnet must not be called after cap"


class TestUnknownModelRejected:
    """If the response model has no pricing entry, the pipeline raises
    immediately rather than silently free-riding past the cost cap."""

    def test_unknown_model_in_response_raises(self) -> None:
        client = FakeLLMClient(text_provider=lambda s, u: "x", model="claude-opus-4-5")
        pipeline = EnrichmentPipeline(client=client)
        table = _users_table()
        with pytest.raises(ValueError, match="unknown"):
            pipeline.enrich_column(table=table, column=table.columns[0], stats=None, fk_targets=())

    def test_unknown_model_does_not_record_spend(self) -> None:
        # Document the current behavior: when `cost_usd_for` raises
        # AFTER the LLM call succeeded, that single call's cost escapes
        # the cap counter. This is acceptable in practice because:
        #   (1) reaching this branch requires a programming error
        #       (a routed-to model has no pricing entry in llm.py),
        #   (2) the exception propagates and tanks the run, so no
        #       further calls happen,
        #   (3) the un-recorded cost is bounded by `max_output_tokens`.
        # If this behavior ever needs to change (e.g. add a worst-case
        # fallback charge), this test will catch the silent change.
        client = FakeLLMClient(text_provider=lambda s, u: "x", model="claude-opus-4-5")
        pipeline = EnrichmentPipeline(client=client)
        table = _users_table()
        with pytest.raises(ValueError):
            pipeline.enrich_column(table=table, column=table.columns[0], stats=None, fk_targets=())
        assert pipeline.spent_usd == 0.0
        # The LLM call DID happen — only the cost-recording failed.
        assert len(client.calls) == 1
