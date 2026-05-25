"""Tests for `metrics/suggest.parse_suggestions`.

The pipeline asks the LLM to emit a strict YAML document with a
top-level `candidates` list. Each item is a metric body (matching
`metrics/yaml_grammar` shape with `version` lifted out + envelope
fields added). This parser enforces the shape strictly — a single
malformed candidate fails the whole batch, mirroring the entity-
suggestion parser's no-partial-acceptance contract.

Round-trip implication: the persisted Metric inside each candidate
gets `origin="suggested"` here, so by the time CLI `--apply` calls
into the store, the origin is already correct.
"""

from __future__ import annotations

import pytest

from schemabrain.core.metric import Metric, MetricMeasure
from schemabrain.metrics.suggest import (
    MetricCandidate,
    MetricSuggestionParseError,
    parse_suggestions,
)

# ----- happy-path round-trip --------------------------------------------------


def test_parse_suggestions_round_trip_with_time_dimension() -> None:
    text = """
candidates:
  - name: total_revenue
    description: Total revenue summed across all orders.
    entity: order
    measure:
      agg: sum
      column: total_cents
    time_dimension: order.placed_at
    time_grains: [day, week, month]
    confidence: high
    rationale: Standard revenue metric anchored on the order entity.
"""
    candidates = parse_suggestions(text)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert isinstance(candidate, MetricCandidate)
    assert candidate.confidence == "high"
    assert candidate.rationale.startswith("Standard revenue")
    metric = candidate.metric
    assert isinstance(metric, Metric)
    assert metric.name == "total_revenue"
    assert metric.entity == "order"
    assert metric.measure == MetricMeasure(agg="sum", column="total_cents")
    assert metric.time_dimension == "order.placed_at"
    assert metric.time_grains == ("day", "week", "month")
    assert metric.origin == "suggested"
    # `inference_method="llm_suggested"` is the 2D trust-signal
    # classification that distinguishes LLM-guessed metrics (→ MEDIUM
    # via `derive_confidence`) from hand-authored YAMLs (→ HIGH).
    # Without this, every wizard-curated metric collapses to flat HIGH
    # on the wire and the 2D signal silently degrades to a single bucket.
    assert metric.inference_method == "llm_suggested"
    assert metric.description == "Total revenue summed across all orders."


def test_parse_suggestions_composite_expression() -> None:
    # LLM-suggested composite-expression metric (the v2 measure shape).
    # Parser must accept `expression` as an alternative to `column`,
    # mirroring the YAML grammar's XOR contract.
    text = """
candidates:
  - name: line_revenue
    description: SUM of line-item revenue (unit_price_cents x quantity).
    entity: order_item
    measure:
      agg: sum
      expression: unit_price_cents * quantity
    confidence: high
    rationale: Per-line revenue captures the actual order economics.
"""
    candidates = parse_suggestions(text)
    assert len(candidates) == 1
    metric = candidates[0].metric
    assert metric.name == "line_revenue"
    assert metric.entity == "order_item"
    assert metric.measure.agg == "sum"
    assert metric.measure.column is None
    assert metric.measure.expression == "unit_price_cents * quantity"


def test_parse_suggestions_rejects_both_column_and_expression() -> None:
    text = """
candidates:
  - name: bad_metric
    entity: order
    measure:
      agg: sum
      column: total_cents
      expression: total_cents * 1
    confidence: low
"""
    with pytest.raises(MetricSuggestionParseError, match="exactly one"):
        parse_suggestions(text)


def test_parse_suggestions_rejects_neither_column_nor_expression() -> None:
    text = """
candidates:
  - name: bad_metric
    entity: order
    measure:
      agg: sum
    confidence: low
"""
    with pytest.raises(MetricSuggestionParseError, match=r"column.*expression|expression.*column"):
        parse_suggestions(text)


def test_parse_suggestions_rejects_malformed_expression() -> None:
    text = """
candidates:
  - name: bad_metric
    entity: order
    measure:
      agg: sum
      expression: abs(total_cents)
    confidence: low
"""
    with pytest.raises(MetricSuggestionParseError, match="malformed measure expression"):
        parse_suggestions(text)


def test_parse_suggestions_non_temporal_metric() -> None:
    text = """
candidates:
  - name: customer_count
    entity: customer
    measure:
      agg: count
      column: id
    confidence: medium
"""
    candidates = parse_suggestions(text)
    assert len(candidates) == 1
    metric = candidates[0].metric
    assert metric.time_dimension is None
    assert metric.time_grains == ()
    assert metric.description == ""  # default for omitted field
    assert candidates[0].rationale == ""  # default for omitted field


def test_parse_suggestions_multiple_candidates_preserve_order() -> None:
    text = """
candidates:
  - name: total_revenue
    entity: order
    measure: {agg: sum, column: total_cents}
    confidence: high
  - name: order_count
    entity: order
    measure: {agg: count, column: id}
    confidence: high
  - name: average_order_value
    entity: order
    measure: {agg: avg, column: total_cents}
    confidence: medium
"""
    candidates = parse_suggestions(text)
    assert [c.metric.name for c in candidates] == [
        "total_revenue",
        "order_count",
        "average_order_value",
    ]


def test_parse_suggestions_empty_candidates_list() -> None:
    text = "candidates: []"
    candidates = parse_suggestions(text)
    assert candidates == []


# ----- top-level structural failures -----------------------------------------


def test_parse_suggestions_rejects_non_yaml() -> None:
    # Tab inside a `key:` scalar is a structural YAML error.
    with pytest.raises(MetricSuggestionParseError) as exc_info:
        parse_suggestions("candidates:\n\t- bad")
    assert "failed to parse YAML" in str(exc_info.value)


def test_parse_suggestions_rejects_empty_document() -> None:
    with pytest.raises(MetricSuggestionParseError, match="empty"):
        parse_suggestions("")


def test_parse_suggestions_rejects_top_level_list() -> None:
    with pytest.raises(MetricSuggestionParseError, match="mapping at the top level"):
        parse_suggestions("- name: total_revenue")


def test_parse_suggestions_rejects_missing_candidates_key() -> None:
    with pytest.raises(MetricSuggestionParseError, match="missing required top-level key"):
        parse_suggestions("metrics: []")


def test_parse_suggestions_rejects_unknown_top_level_keys() -> None:
    with pytest.raises(MetricSuggestionParseError, match="unknown top-level keys"):
        parse_suggestions("candidates: []\nextra: 1")


def test_parse_suggestions_rejects_non_list_candidates() -> None:
    with pytest.raises(MetricSuggestionParseError, match="candidates must be a list"):
        parse_suggestions("candidates: not-a-list")


# ----- per-candidate structural failures -------------------------------------


def test_parse_suggestions_rejects_non_mapping_candidate() -> None:
    with pytest.raises(MetricSuggestionParseError, match="candidate #0 must be a mapping"):
        parse_suggestions("candidates: ['scalar']")


def test_parse_suggestions_rejects_missing_required_keys() -> None:
    text = """
candidates:
  - name: x
    entity: order
"""
    with pytest.raises(MetricSuggestionParseError, match="missing required field"):
        parse_suggestions(text)


def test_parse_suggestions_rejects_unknown_candidate_keys() -> None:
    text = """
candidates:
  - name: total_revenue
    entity: order
    measure: {agg: sum, column: total_cents}
    confidence: high
    confidance: medium
"""
    with pytest.raises(MetricSuggestionParseError, match="unknown field"):
        parse_suggestions(text)


def test_parse_suggestions_error_includes_candidate_index() -> None:
    text = """
candidates:
  - name: revenue_a
    entity: order
    measure: {agg: sum, column: amt}
    confidence: high
  - name: revenue_b
    entity: order
    measure: {agg: sum, column: amt}
"""
    with pytest.raises(MetricSuggestionParseError, match=r"candidate #1"):
        parse_suggestions(text)


# ----- field-type failures ---------------------------------------------------


def test_parse_suggestions_rejects_non_string_name() -> None:
    text = """
candidates:
  - name: 7
    entity: order
    measure: {agg: sum, column: x}
    confidence: high
"""
    with pytest.raises(MetricSuggestionParseError, match=r"candidate #0.name must be a string"):
        parse_suggestions(text)


def test_parse_suggestions_rejects_empty_name() -> None:
    text = """
candidates:
  - name: ""
    entity: order
    measure: {agg: sum, column: x}
    confidence: high
"""
    with pytest.raises(MetricSuggestionParseError, match=r"candidate #0.name must be non-empty"):
        parse_suggestions(text)


def test_parse_suggestions_rejects_non_string_description() -> None:
    text = """
candidates:
  - name: rev
    description: 5
    entity: order
    measure: {agg: sum, column: x}
    confidence: high
"""
    with pytest.raises(MetricSuggestionParseError, match="description must be a string"):
        parse_suggestions(text)


def test_parse_suggestions_rejects_non_string_rationale() -> None:
    text = """
candidates:
  - name: rev
    entity: order
    measure: {agg: sum, column: x}
    confidence: high
    rationale: 7
"""
    with pytest.raises(MetricSuggestionParseError, match="rationale must be a string"):
        parse_suggestions(text)


def test_parse_suggestions_rejects_non_string_confidence() -> None:
    text = """
candidates:
  - name: rev
    entity: order
    measure: {agg: sum, column: x}
    confidence: 1
"""
    with pytest.raises(MetricSuggestionParseError, match="confidence must be a string"):
        parse_suggestions(text)


def test_parse_suggestions_rejects_unknown_confidence_value() -> None:
    text = """
candidates:
  - name: rev
    entity: order
    measure: {agg: sum, column: x}
    confidence: very-high
"""
    with pytest.raises(MetricSuggestionParseError, match=r"confidence must be one of"):
        parse_suggestions(text)


# ----- measure sub-mapping ---------------------------------------------------


def test_parse_suggestions_rejects_non_mapping_measure() -> None:
    text = """
candidates:
  - name: rev
    entity: order
    measure: sum(x)
    confidence: high
"""
    with pytest.raises(MetricSuggestionParseError, match="measure must be a mapping"):
        parse_suggestions(text)


def test_parse_suggestions_rejects_missing_agg() -> None:
    # `agg` is the only required key now; `column` and `expression`
    # are XOR'd by `_parse_measure` after the required-key check.
    text = """
candidates:
  - name: rev
    entity: order
    measure: {column: total_cents}
    confidence: high
"""
    with pytest.raises(MetricSuggestionParseError, match="missing required field"):
        parse_suggestions(text)


def test_parse_suggestions_rejects_unknown_measure_keys() -> None:
    text = """
candidates:
  - name: rev
    entity: order
    measure: {agg: sum, column: x, filter: 'y > 0'}
    confidence: high
"""
    with pytest.raises(MetricSuggestionParseError, match=r"measure has unknown field"):
        parse_suggestions(text)


def test_parse_suggestions_rejects_non_string_agg() -> None:
    text = """
candidates:
  - name: rev
    entity: order
    measure: {agg: 1, column: x}
    confidence: high
"""
    with pytest.raises(MetricSuggestionParseError, match=r"measure\.agg must be a string"):
        parse_suggestions(text)


def test_parse_suggestions_rejects_unknown_agg_value() -> None:
    text = """
candidates:
  - name: rev
    entity: order
    measure: {agg: median, column: x}
    confidence: high
"""
    with pytest.raises(MetricSuggestionParseError, match=r"measure\.agg must be one of"):
        parse_suggestions(text)


def test_parse_suggestions_rejects_non_string_column() -> None:
    text = """
candidates:
  - name: rev
    entity: order
    measure: {agg: sum, column: 7}
    confidence: high
"""
    with pytest.raises(MetricSuggestionParseError, match=r"measure\.column must be a string"):
        parse_suggestions(text)


def test_parse_suggestions_rejects_invalid_column_identifier() -> None:
    text = """
candidates:
  - name: rev
    entity: order
    measure: {agg: sum, column: "1bad"}
    confidence: high
"""
    # MetricMeasure.__post_init__ wrapped as MetricSuggestionParseError.
    with pytest.raises(MetricSuggestionParseError, match="column must be an identifier"):
        parse_suggestions(text)


# ----- time-dimension + time-grains ------------------------------------------


def test_parse_suggestions_rejects_dimension_without_grains() -> None:
    text = """
candidates:
  - name: rev
    entity: order
    measure: {agg: sum, column: x}
    time_dimension: order.placed_at
    confidence: high
"""
    with pytest.raises(MetricSuggestionParseError, match="time_grains is missing"):
        parse_suggestions(text)


def test_parse_suggestions_rejects_grains_without_dimension() -> None:
    text = """
candidates:
  - name: rev
    entity: order
    measure: {agg: sum, column: x}
    time_grains: [day]
    confidence: high
"""
    with pytest.raises(MetricSuggestionParseError, match="time_dimension is missing"):
        parse_suggestions(text)


def test_parse_suggestions_rejects_non_string_time_dimension() -> None:
    text = """
candidates:
  - name: rev
    entity: order
    measure: {agg: sum, column: x}
    time_dimension: 5
    time_grains: [day]
    confidence: high
"""
    with pytest.raises(MetricSuggestionParseError, match="time_dimension must be a string"):
        parse_suggestions(text)


def test_parse_suggestions_rejects_non_list_grains() -> None:
    text = """
candidates:
  - name: rev
    entity: order
    measure: {agg: sum, column: x}
    time_dimension: order.placed_at
    time_grains: "day"
    confidence: high
"""
    with pytest.raises(MetricSuggestionParseError, match="time_grains must be a list"):
        parse_suggestions(text)


def test_parse_suggestions_rejects_empty_grain_list() -> None:
    text = """
candidates:
  - name: rev
    entity: order
    measure: {agg: sum, column: x}
    time_dimension: order.placed_at
    time_grains: []
    confidence: high
"""
    with pytest.raises(MetricSuggestionParseError, match="non-empty list"):
        parse_suggestions(text)


def test_parse_suggestions_rejects_non_string_grain_item() -> None:
    text = """
candidates:
  - name: rev
    entity: order
    measure: {agg: sum, column: x}
    time_dimension: order.placed_at
    time_grains: [1]
    confidence: high
"""
    with pytest.raises(MetricSuggestionParseError, match="time_grains items must be strings"):
        parse_suggestions(text)


def test_parse_suggestions_rejects_unknown_grain_value() -> None:
    text = """
candidates:
  - name: rev
    entity: order
    measure: {agg: sum, column: x}
    time_dimension: order.placed_at
    time_grains: [day, century]
    confidence: high
"""
    with pytest.raises(MetricSuggestionParseError, match="time_grains items must be one of"):
        parse_suggestions(text)


def test_parse_suggestions_rejects_grains_out_of_canonical_order() -> None:
    text = """
candidates:
  - name: rev
    entity: order
    measure: {agg: sum, column: x}
    time_dimension: order.placed_at
    time_grains: [month, day]
    confidence: high
"""
    # Metric.__post_init__ enforces canonical ordering; wrapped at parser.
    with pytest.raises(MetricSuggestionParseError, match="canonical order"):
        parse_suggestions(text)


# ----- dataclass-level validation surfacing through parser -------------------


def test_parse_suggestions_rejects_invalid_metric_name() -> None:
    text = """
candidates:
  - name: "1bad"
    entity: order
    measure: {agg: sum, column: x}
    confidence: high
"""
    with pytest.raises(MetricSuggestionParseError, match="name must be an identifier"):
        parse_suggestions(text)


def test_parse_suggestions_rejects_invalid_entity_name() -> None:
    text = """
candidates:
  - name: rev
    entity: "1bad"
    measure: {agg: sum, column: x}
    confidence: high
"""
    with pytest.raises(MetricSuggestionParseError, match="entity must be an identifier"):
        parse_suggestions(text)


def test_parse_suggestions_rejects_malformed_time_dimension() -> None:
    # `placed_at` (no entity prefix) fails the `entity.column` regex.
    text = """
candidates:
  - name: rev
    entity: order
    measure: {agg: sum, column: x}
    time_dimension: placed_at
    time_grains: [day]
    confidence: high
"""
    with pytest.raises(MetricSuggestionParseError, match=r"entity\.column"):
        parse_suggestions(text)


# ----- description anti-pattern validation (an earlier gap belt-and-suspenders) ------


def test_parse_suggestions_rejects_admits_aggregation_cannot_deliver() -> None:
    """an earlier gap regression — 2026-05-21 smoke surfaced a metric where the
    LLM admitted in its own description that the aggregation couldn't
    deliver what the name promised: `total_order_item_revenue` with
    measure `sum(unit_price_cents)` and description "unit_price_cents
    * quantity is not directly available, but unit_price_cents summed
    gives a price-mix signal". The system prompt now forbids this, but
    parse-time validation is the belt to that suspenders.
    """
    text = """
candidates:
  - name: total_order_item_revenue
    description: >-
      Sum of line-item revenue (unit_price_cents * quantity is not
      directly available, but unit_price_cents summed gives a
      price-mix signal) across all order items.
    entity: order_item
    measure: {agg: sum, column: unit_price_cents}
    confidence: medium
"""
    with pytest.raises(MetricSuggestionParseError, match="anti-pattern"):
        parse_suggestions(text)


def test_parse_suggestions_rejects_would_require_multiplication() -> None:
    text = """
candidates:
  - name: total_revenue
    description: True line revenue would require multiplication of unit_price * quantity.
    entity: order_item
    measure: {agg: sum, column: unit_price_cents}
    confidence: low
"""
    with pytest.raises(MetricSuggestionParseError, match="anti-pattern"):
        parse_suggestions(text)


def test_parse_suggestions_accepts_honest_description() -> None:
    """A metric description that's honest about what it measures (no
    anti-pattern phrases) MUST still parse cleanly. Regression guard
    against the validator over-rejecting innocent descriptions.
    """
    text = """
candidates:
  - name: total_items_sold
    description: Sum of quantities sold across all order line items.
    entity: order_item
    measure: {agg: sum, column: quantity}
    confidence: high
"""
    candidates = parse_suggestions(text)
    assert len(candidates) == 1
    assert candidates[0].metric.name == "total_items_sold"
