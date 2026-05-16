"""Tests for `EntitySuggestionPipeline.propose_from_tables` — full round trip.

Wires schema serialization + prompt + LLM call + parse + SuggestionResult.
Uses `FakeLLMClient` to inject canned LLM responses keyed on the prompt
shape, so tests stay deterministic without network access.

The pipeline's job is orchestration only: it does not interpret the
LLM's output beyond passing it to `parse_suggestions`. Parser unit
tests cover edge cases of the YAML grammar; these tests cover the
orchestration (top-k truncation, cost dispatch, response.model passthrough).
"""

from __future__ import annotations

import pytest

from schemabrain.core.models import Column, ForeignKey, Table
from schemabrain.enrichment.llm import FakeLLMClient
from schemabrain.entities.suggest import (
    EntitySuggestionPipeline,
    SuggestionParseError,
)


def _pk_col(name: str, table: str) -> Column:
    return Column(
        name=name,
        table_name=table,
        schema_name="public",
        data_type="bigint",
        nullable=False,
        ordinal_position=1,
        is_primary_key=True,
    )


def _users_orders_tables() -> list[Table]:
    """Standard two-table fixture: users + orders with FK."""
    users = Table(
        name="users",
        schema_name="public",
        columns=(
            _pk_col("id", "users"),
            Column(
                name="email",
                table_name="users",
                schema_name="public",
                data_type="text",
                nullable=False,
                ordinal_position=2,
            ),
        ),
    )
    orders = Table(
        name="orders",
        schema_name="public",
        columns=(
            _pk_col("id", "orders"),
            Column(
                name="user_id",
                table_name="orders",
                schema_name="public",
                data_type="bigint",
                nullable=False,
                ordinal_position=2,
            ),
        ),
        foreign_keys=(
            ForeignKey(
                name="orders_user_id_fkey",
                source_columns=("user_id",),
                target_schema="public",
                target_table="users",
                target_columns=("id",),
            ),
        ),
    )
    return [users, orders]


_TWO_CANDIDATES_YAML = """
candidates:
  - name: customer
    description: A registered customer
    binding:
      single_table: public.users
    identity: id
    confidence: high
    rationale: users has id PK and NOT NULL email
    pii_hints:
      email: pii
  - name: order
    description: One placed order
    binding:
      single_table: public.orders
    identity: id
    confidence: medium
    rationale: orders has id PK, FK into users
    pii_hints: {}
"""


# ----- happy path ------------------------------------------------------------


class TestProposeFromTablesHappyPath:
    def test_returns_suggestion_result_with_parsed_candidates(self) -> None:
        client = FakeLLMClient(text_provider=lambda _s, _u: _TWO_CANDIDATES_YAML)
        pipeline = EntitySuggestionPipeline(llm=client)

        result = pipeline.propose_from_tables(_users_orders_tables())

        assert len(result.candidates) == 2
        assert [c.entity.name for c in result.candidates] == ["customer", "order"]
        # The LLM model field on the response is the source of truth — pipeline
        # threads it through into the result (so audit-log records what model
        # actually produced the candidates, not whatever the user configured).
        assert result.llm_model == "claude-haiku-4-5"

    def test_cost_is_non_negative_and_finite(self) -> None:
        # `total_cost_usd` is paid through `client.cost_usd(usage)` —
        # the pipeline never reprices. The exact number depends on
        # FakeLLMClient's 4-chars-per-token approximation, which we
        # don't pin here (that'd duplicate the fake's internals).
        # Provenance + invariant are what matter.
        client = FakeLLMClient(text_provider=lambda _s, _u: _TWO_CANDIDATES_YAML)
        pipeline = EntitySuggestionPipeline(llm=client)

        result = pipeline.propose_from_tables(_users_orders_tables())

        assert result.total_cost_usd > 0
        assert result.total_cost_usd == pytest.approx(result.total_cost_usd)  # finite

    def test_llm_called_once_with_schema_in_user_prompt(self) -> None:
        client = FakeLLMClient(text_provider=lambda _s, _u: _TWO_CANDIDATES_YAML)
        pipeline = EntitySuggestionPipeline(llm=client)

        pipeline.propose_from_tables(_users_orders_tables())

        # One LLM call per propose run — no retries, no chunking at v1.
        assert len(client.calls) == 1
        system, user = client.calls[0]
        # System prompt names the YAML output shape so the model
        # produces what we can parse.
        assert "candidates" in system
        # User prompt contains the serialized schema — table names
        # appear in user, not system.
        assert "public.users" in user
        assert "public.orders" in user

    def test_top_k_truncates_excess_candidates(self) -> None:
        # The LLM may return more candidates than asked; pipeline
        # truncates defensively to `top_k`.
        text = """
candidates:
  - name: a
    binding:
      single_table: public.t1
    identity: id
    confidence: high
    rationale: r
    pii_hints: {}
  - name: b
    binding:
      single_table: public.t2
    identity: id
    confidence: high
    rationale: r
    pii_hints: {}
  - name: c
    binding:
      single_table: public.t3
    identity: id
    confidence: high
    rationale: r
    pii_hints: {}
"""
        client = FakeLLMClient(text_provider=lambda _s, _u: text)
        pipeline = EntitySuggestionPipeline(llm=client)

        result = pipeline.propose_from_tables(_users_orders_tables(), top_k=2)
        assert len(result.candidates) == 2
        assert [c.entity.name for c in result.candidates] == ["a", "b"]

    def test_top_k_in_user_prompt(self) -> None:
        # The top_k value is communicated to the LLM via the user
        # prompt — not just enforced post-hoc — so the model can
        # rank-and-stop instead of generating more than needed.
        client = FakeLLMClient(text_provider=lambda _s, _u: "candidates: []")
        pipeline = EntitySuggestionPipeline(llm=client)

        pipeline.propose_from_tables(_users_orders_tables(), top_k=5)

        _system, user = client.calls[0]
        # User prompt names the cap so the LLM can respect it. The
        # exact phrasing isn't pinned (room to tune); the number is.
        assert "5" in user

    def test_empty_candidates_returned_when_llm_returns_none(self) -> None:
        client = FakeLLMClient(text_provider=lambda _s, _u: "candidates: []")
        pipeline = EntitySuggestionPipeline(llm=client)

        result = pipeline.propose_from_tables(_users_orders_tables())

        assert result.candidates == ()
        # Cost is still paid even for an empty result (LLM was called).
        assert result.total_cost_usd > 0


# ----- error / edge cases ----------------------------------------------------


class TestProposeFromTablesEdgeCases:
    def test_empty_tables_raises_value_error(self) -> None:
        # Pipeline contract: at least one table required. The CLI is
        # responsible for catching the "no indexed schema yet" case
        # before invoking the pipeline. We surface a fast error rather
        # than burn an LLM call on an empty schema.
        client = FakeLLMClient(text_provider=lambda _s, _u: "candidates: []")
        pipeline = EntitySuggestionPipeline(llm=client)

        with pytest.raises(ValueError, match="at least one table"):
            pipeline.propose_from_tables([])
        # And no LLM call was made — the check fires before _invoke_llm.
        assert client.calls == []

    def test_zero_top_k_raises_value_error(self) -> None:
        client = FakeLLMClient(text_provider=lambda _s, _u: "candidates: []")
        pipeline = EntitySuggestionPipeline(llm=client)

        with pytest.raises(ValueError, match="top_k"):
            pipeline.propose_from_tables(_users_orders_tables(), top_k=0)

    def test_negative_top_k_raises_value_error(self) -> None:
        client = FakeLLMClient(text_provider=lambda _s, _u: "candidates: []")
        pipeline = EntitySuggestionPipeline(llm=client)

        with pytest.raises(ValueError, match="top_k"):
            pipeline.propose_from_tables(_users_orders_tables(), top_k=-1)

    def test_llm_returns_malformed_yaml_propagates_parse_error(self) -> None:
        client = FakeLLMClient(text_provider=lambda _s, _u: ":\n: : :")
        pipeline = EntitySuggestionPipeline(llm=client)

        with pytest.raises(SuggestionParseError):
            pipeline.propose_from_tables(_users_orders_tables())

    def test_default_top_k_is_ten(self) -> None:
        # Sanity pin: the default top_k matches what the CLI expects
        # — change this number in both places or document the drift.
        text = "candidates: []"
        client = FakeLLMClient(text_provider=lambda _s, _u: text)
        pipeline = EntitySuggestionPipeline(llm=client)

        pipeline.propose_from_tables(_users_orders_tables())

        _system, user = client.calls[0]
        assert "10" in user
