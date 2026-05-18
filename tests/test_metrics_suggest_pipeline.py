"""Tests for `MetricSuggestionPipeline` orchestration + dataclass shapes.

Covers the suggest-pipeline's responsibilities:
  - Input validation (empty entities, empty tables, top_k <= 0)
  - LLM seam (single call, cost dispatch through the client)
  - top_k truncation (post-parse cap)
  - Result envelope round-trip
  - `_serialize_entities_for_llm` output shape

Also covers `MetricCandidate` and `MetricSuggestionResult` dataclass
invariants — the runtime guards that fire if the static `Literal` is
bypassed by an untyped caller.
"""

from __future__ import annotations

import dataclasses

import pytest

from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.metric import Metric, MetricMeasure
from schemabrain.core.models import Column, ForeignKey, Table
from schemabrain.enrichment.llm import FakeLLMClient
from schemabrain.metrics.suggest import (
    MetricCandidate,
    MetricSuggestionParseError,
    MetricSuggestionPipeline,
    MetricSuggestionResult,
    serialize_entities_for_llm,
)

# ----- fixtures --------------------------------------------------------------


def _column(
    *,
    name: str,
    table: str,
    schema: str = "public",
    data_type: str = "bigint",
    nullable: bool = False,
    ordinal: int = 1,
    is_primary_key: bool = False,
) -> Column:
    return Column(
        name=name,
        table_name=table,
        schema_name=schema,
        data_type=data_type,
        nullable=nullable,
        ordinal_position=ordinal,
        is_primary_key=is_primary_key,
    )


def _make_orders_table(foreign_keys: tuple[ForeignKey, ...] = ()) -> Table:
    return Table(
        name="orders",
        schema_name="public",
        columns=(
            _column(name="id", table="orders", is_primary_key=True, ordinal=1),
            _column(name="user_id", table="orders", ordinal=2),
            _column(name="total_cents", table="orders", data_type="integer", ordinal=3),
            _column(
                name="placed_at",
                table="orders",
                data_type="timestamp",
                ordinal=4,
            ),
        ),
        foreign_keys=foreign_keys,
    )


def _make_customers_table() -> Table:
    return Table(
        name="customers",
        schema_name="public",
        columns=(
            _column(name="id", table="customers", is_primary_key=True, ordinal=1),
            _column(
                name="email",
                table="customers",
                data_type="text",
                nullable=True,
                ordinal=2,
            ),
        ),
    )


def _order_entity() -> Entity:
    return Entity(
        name="order",
        description="",
        binding=SingleTableBinding(qualified_table="public.orders"),
        identity="id",
        origin="manual",
    )


def _customer_entity() -> Entity:
    return Entity(
        name="customer",
        description="",
        binding=SingleTableBinding(qualified_table="public.customers"),
        identity="id",
        origin="manual",
    )


def _valid_yaml() -> str:
    return """
candidates:
  - name: total_revenue
    entity: order
    measure: {agg: sum, column: total_cents}
    time_dimension: order.placed_at
    time_grains: [day, week, month]
    confidence: high
    rationale: Revenue is the headline metric for an order entity.
"""


def _valid_metric() -> Metric:
    return Metric(
        name="rev",
        description="",
        entity="order",
        measure=MetricMeasure(agg="sum", column="total_cents"),
        time_dimension=None,
        time_grains=(),
        origin="suggested",
    )


# ----- MetricCandidate shape -------------------------------------------------


def test_metric_candidate_accepts_valid_input() -> None:
    candidate = MetricCandidate(
        metric=_valid_metric(),
        confidence="high",
        rationale="",
    )
    assert candidate.confidence == "high"
    assert candidate.rationale == ""


def test_metric_candidate_rejects_unknown_confidence() -> None:
    with pytest.raises(ValueError, match="confidence must be one of"):
        MetricCandidate(
            metric=_valid_metric(),
            confidence="medium-high",  # type: ignore[arg-type]
            rationale="",
        )


def test_metric_candidate_is_frozen() -> None:
    candidate = MetricCandidate(
        metric=_valid_metric(),
        confidence="high",
        rationale="",
    )
    # `dataclass(frozen=True)` raises `dataclasses.FrozenInstanceError`
    # (an `AttributeError` subclass) on attribute reassignment. Pinning
    # the specific exception type guards against a future Python version
    # widening the exception surface — we want this test to fail loudly
    # if the frozen guarantee weakens.
    with pytest.raises(dataclasses.FrozenInstanceError):
        candidate.confidence = "low"  # type: ignore[misc]


# ----- MetricSuggestionResult shape ------------------------------------------


def test_metric_suggestion_result_accepts_valid_input() -> None:
    result = MetricSuggestionResult(
        candidates=(),
        total_cost_usd=0.0,
        llm_model="claude-haiku-4-5",
    )
    assert result.candidates == ()
    assert result.total_cost_usd == 0.0


def test_metric_suggestion_result_rejects_negative_cost() -> None:
    with pytest.raises(ValueError, match="total_cost_usd must be non-negative"):
        MetricSuggestionResult(
            candidates=(),
            total_cost_usd=-0.01,
            llm_model="claude-haiku-4-5",
        )


def test_metric_suggestion_result_rejects_empty_model() -> None:
    with pytest.raises(ValueError, match="llm_model must be a non-empty string"):
        MetricSuggestionResult(
            candidates=(),
            total_cost_usd=0.0,
            llm_model="",
        )


# ----- pipeline.propose_from_entities ----------------------------------------


def test_propose_from_entities_round_trip() -> None:
    fake = FakeLLMClient(text_provider=lambda _s, _u: _valid_yaml())
    pipeline = MetricSuggestionPipeline(llm=fake)
    result = pipeline.propose_from_entities(
        entities=[_order_entity()],
        tables=[_make_orders_table()],
        top_k=10,
    )
    assert len(result.candidates) == 1
    assert result.candidates[0].metric.name == "total_revenue"
    assert result.total_cost_usd >= 0.0
    assert result.llm_model == "claude-haiku-4-5"


def test_propose_from_entities_passes_top_k_to_user_prompt() -> None:
    captured: list[tuple[str, str]] = []

    def text_provider(system: str, user: str) -> str:
        captured.append((system, user))
        return "candidates: []"

    pipeline = MetricSuggestionPipeline(llm=FakeLLMClient(text_provider=text_provider))
    pipeline.propose_from_entities(
        entities=[_order_entity()],
        tables=[_make_orders_table()],
        top_k=7,
    )
    assert len(captured) == 1
    _system, user = captured[0]
    assert "Return at most 7" in user


def test_propose_from_entities_truncates_to_top_k() -> None:
    text = """
candidates:
  - name: m1
    entity: order
    measure: {agg: sum, column: total_cents}
    confidence: high
  - name: m2
    entity: order
    measure: {agg: count, column: id}
    confidence: high
  - name: m3
    entity: order
    measure: {agg: avg, column: total_cents}
    confidence: medium
"""
    pipeline = MetricSuggestionPipeline(llm=FakeLLMClient(text_provider=lambda _s, _u: text))
    result = pipeline.propose_from_entities(
        entities=[_order_entity()],
        tables=[_make_orders_table()],
        top_k=2,
    )
    assert [c.metric.name for c in result.candidates] == ["m1", "m2"]


def test_propose_from_entities_refuses_empty_entities() -> None:
    pipeline = MetricSuggestionPipeline(
        llm=FakeLLMClient(text_provider=lambda _s, _u: "candidates: []")
    )
    with pytest.raises(ValueError, match="at least one entity"):
        pipeline.propose_from_entities(
            entities=[],
            tables=[_make_orders_table()],
            top_k=10,
        )


def test_propose_from_entities_refuses_empty_tables() -> None:
    pipeline = MetricSuggestionPipeline(
        llm=FakeLLMClient(text_provider=lambda _s, _u: "candidates: []")
    )
    with pytest.raises(ValueError, match="at least one table"):
        pipeline.propose_from_entities(
            entities=[_order_entity()],
            tables=[],
            top_k=10,
        )


def test_propose_from_entities_refuses_nonpositive_top_k() -> None:
    pipeline = MetricSuggestionPipeline(
        llm=FakeLLMClient(text_provider=lambda _s, _u: "candidates: []")
    )
    with pytest.raises(ValueError, match="top_k must be a positive integer"):
        pipeline.propose_from_entities(
            entities=[_order_entity()],
            tables=[_make_orders_table()],
            top_k=0,
        )


def test_propose_from_entities_surfaces_parser_errors() -> None:
    pipeline = MetricSuggestionPipeline(
        llm=FakeLLMClient(text_provider=lambda _s, _u: "candidates: not-a-list")
    )
    with pytest.raises(MetricSuggestionParseError):
        pipeline.propose_from_entities(
            entities=[_order_entity()],
            tables=[_make_orders_table()],
            top_k=10,
        )


def test_propose_from_entities_makes_one_llm_call_per_run() -> None:
    fake = FakeLLMClient(text_provider=lambda _s, _u: "candidates: []")
    pipeline = MetricSuggestionPipeline(llm=fake)
    pipeline.propose_from_entities(
        entities=[_order_entity()],
        tables=[_make_orders_table()],
        top_k=10,
    )
    assert len(fake.calls) == 1


def test_propose_from_entities_cost_routes_through_client() -> None:
    text = _valid_yaml()
    fake = FakeLLMClient(text_provider=lambda _s, _u: text)
    pipeline = MetricSuggestionPipeline(llm=fake)
    result = pipeline.propose_from_entities(
        entities=[_order_entity()],
        tables=[_make_orders_table()],
        top_k=10,
    )
    # Haiku pricing > 0 for a non-empty response.
    assert result.total_cost_usd > 0.0


# ----- serialize_entities_for_llm -------------------------------------------


def test_serialize_entities_includes_qualified_table_and_columns() -> None:
    text, surviving = serialize_entities_for_llm(
        entities=[_order_entity()],
        tables=[_make_orders_table()],
    )
    assert "entity: order" in text
    assert "bound to: public.orders" in text
    assert "identity: id" in text
    assert "id bigint NOT NULL PK" in text
    assert "total_cents integer NOT NULL" in text
    assert "placed_at timestamp NOT NULL" in text
    assert surviving == ["order"]


def test_serialize_entities_renders_foreign_keys() -> None:
    table = _make_orders_table(
        foreign_keys=(
            ForeignKey(
                name="orders_user_fk",
                source_columns=("user_id",),
                target_schema="public",
                target_table="users",
                target_columns=("id",),
            ),
        )
    )
    text, _surviving = serialize_entities_for_llm(
        entities=[_order_entity()],
        tables=[table],
    )
    assert "foreign_keys:" in text
    assert "user_id -> public.users(id)" in text


def test_serialize_entities_renders_none_marker_when_no_fks() -> None:
    text, _surviving = serialize_entities_for_llm(
        entities=[_order_entity()],
        tables=[_make_orders_table()],
    )
    assert "foreign_keys:\n  (none)" in text


def test_serialize_entities_skips_entities_with_no_matching_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The entity binds to public.orders, but the supplied table list
    # only carries public.customers. The serializer must (1) drop the
    # entity from the rendered text, (2) drop it from the surviving-
    # names list, and (3) log a WARNING so an operator debugging
    # "why is my entity missing?" sees the cause.
    #
    # Spy on the module-level logger directly rather than caplog —
    # caplog depends on the root logger's propagation chain, which
    # other tests in the suite reconfigure (via `configure_logging`).
    # Direct spy is order-independent and test-isolated.
    import schemabrain.metrics.suggest as suggest_module

    warning_calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        suggest_module._logger,
        "warning",
        lambda fmt, *args, **kwargs: warning_calls.append((fmt, args)),
    )

    text, surviving = serialize_entities_for_llm(
        entities=[_order_entity()],
        tables=[_make_customers_table()],
    )
    assert "entity: order" not in text
    assert surviving == []
    # One warning, formatted with "dropping entity %r" and the dropped name.
    assert len(warning_calls) == 1
    fmt, args = warning_calls[0]
    assert "dropping entity" in fmt
    assert "order" in args


def test_serialize_entities_blank_line_between_entities() -> None:
    text, surviving = serialize_entities_for_llm(
        entities=[_order_entity(), _customer_entity()],
        tables=[_make_orders_table(), _make_customers_table()],
    )
    assert text.count("\n\n") == 1
    assert "entity: order" in text
    assert "entity: customer" in text
    # Surviving-names list preserves input order.
    assert surviving == ["order", "customer"]


def test_serialize_entities_partial_mismatch_returns_only_surviving() -> None:
    # Two entities, only one backing table: the missing-table entity is
    # dropped from BOTH the rendered text and the surviving-names list,
    # so the user-prompt builder won't tell the LLM to anchor on it.
    text, surviving = serialize_entities_for_llm(
        entities=[_order_entity(), _customer_entity()],
        tables=[_make_customers_table()],
    )
    assert "entity: order" not in text
    assert "entity: customer" in text
    assert surviving == ["customer"]


def test_serialize_entities_renders_nullable_columns_without_not_null() -> None:
    text, _surviving = serialize_entities_for_llm(
        entities=[_customer_entity()],
        tables=[_make_customers_table()],
    )
    # The `email` line is nullable → must not have "NOT NULL".
    email_line = next(line for line in text.splitlines() if line.strip().startswith("email "))
    assert "NOT NULL" not in email_line
    # The `id` line is non-null PK → must have both markers.
    id_line = next(line for line in text.splitlines() if line.strip().startswith("id "))
    assert "NOT NULL" in id_line
    assert "PK" in id_line


def test_build_user_prompt_names_only_surviving_entities() -> None:
    # Even though two entities are passed in, only one has a backing
    # table — the user prompt must tell the LLM to anchor on the
    # surviving entity only, not the dropped one.
    from schemabrain.metrics.suggest import _build_user_prompt

    user_prompt = _build_user_prompt(
        entities=[_order_entity(), _customer_entity()],
        tables=[_make_customers_table()],
        top_k=5,
    )
    assert "customer" in user_prompt
    # The dropped entity must NOT appear in the comma-separated entity
    # list after "anchor … on one of these entities:". Substring matching
    # on the whole anchor line would false-positive on "ordered" — so
    # extract the names portion and assert against the comma-split tokens.
    anchor_line = next(line for line in user_prompt.splitlines() if "Anchor each candidate" in line)
    names_portion = anchor_line.split("entities:", 1)[1].rstrip(". ")
    anchor_names = {n.strip() for n in names_portion.split(",")}
    assert anchor_names == {"customer"}
