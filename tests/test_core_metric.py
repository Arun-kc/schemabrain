"""Tests for `Metric` + `MetricMeasure` + `AggFunction` / `TimeGrain` literals.

Locks the metric-model design decisions in code:

  - One metric = one measure (modern dbt-Semantic-Layer pattern).
  - Closed-grammar agg: `sum / count / count_distinct / avg / min / max`;
    open `agg: str` would invite SQL injection.
  - Closed-grammar time grain: `day / week / month / quarter / year`.
  - Entity-anchored: the `measure.column` lives on `entity`'s table.
  - `time_dimension` is `<entity>.<column>` form (one dot, identifier
    parts) — required iff `time_grains` non-empty (parallel emptiness).
  - `time_grains` canonical-sorted (`day < week < month < quarter < year`)
    so equality and storage round-trips are deterministic.
  - `Origin = Literal["manual", "suggested", "dbt_import"]` — symmetric
    with `Entity.origin` / `CanonicalJoin.origin`. All three accepted
    at the dataclass; suggest/apply pipelines refuse `dbt_import` until
    the dbt-metrics importer ships in the same PR.

All invariants enforced in `__post_init__` so YAML loads, store reads,
and programmatic construction converge on the same validity contract.
"""

from __future__ import annotations

import dataclasses

import pytest

from schemabrain.core.metric import (
    DbtOwnedMetricError,
    Metric,
    MetricMeasure,
)

# ----- MetricMeasure --------------------------------------------------------


class TestMetricMeasure:
    def test_accepts_valid_agg_and_identifier_column(self) -> None:
        measure = MetricMeasure(agg="sum", column="total_amount")
        assert measure.agg == "sum"
        assert measure.column == "total_amount"

    def test_is_frozen(self) -> None:
        measure = MetricMeasure(agg="sum", column="total_amount")
        with pytest.raises(dataclasses.FrozenInstanceError):
            measure.agg = "count"  # type: ignore[misc]

    @pytest.mark.parametrize(
        "agg",
        ["sum", "count", "count_distinct", "avg", "min", "max"],
    )
    def test_accepts_all_six_agg_literals(self, agg: str) -> None:
        measure = MetricMeasure(agg=agg, column="total_amount")  # type: ignore[arg-type]
        assert measure.agg == agg

    @pytest.mark.parametrize(
        "bad_agg",
        [
            "",
            "SUM",  # case-sensitive
            "median",  # deferred — PERCENTILE_CONT portability
            "stddev",
            "unknown",
            None,
        ],
    )
    def test_rejects_unknown_agg(self, bad_agg: object) -> None:
        with pytest.raises(ValueError, match="agg"):
            MetricMeasure(agg=bad_agg, column="total_amount")  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "bad_column",
        [
            "",
            "1leading_digit",
            "has space",
            "has-hyphen",
            "has.dot",  # column-shape — bare identifier expected here
        ],
    )
    def test_rejects_non_identifier_column(self, bad_column: str) -> None:
        with pytest.raises(ValueError, match="column"):
            MetricMeasure(agg="sum", column=bad_column)

    def test_accepts_dollar_sign_in_identifier(self) -> None:
        # Same Postgres column alphabet as `JoinColumnPair`.
        measure = MetricMeasure(agg="sum", column="row$amount")
        assert measure.column == "row$amount"

    def test_two_equal_measures_compare_equal(self) -> None:
        a = MetricMeasure(agg="sum", column="total_amount")
        b = MetricMeasure(agg="sum", column="total_amount")
        assert a == b


class TestMetricMeasureCompositeExpression:
    """v2 composite-expression measures via the `expression=` field."""

    def test_composite_measure_constructed(self) -> None:
        measure = MetricMeasure(agg="sum", expression="unit_price * quantity")
        assert measure.agg == "sum"
        assert measure.column is None
        assert measure.expression == "unit_price * quantity"

    def test_composite_measure_columns_property(self) -> None:
        measure = MetricMeasure(agg="sum", expression="unit_price * quantity")
        assert measure.measure_columns == frozenset({"unit_price", "quantity"})

    def test_bare_column_measure_columns_property(self) -> None:
        measure = MetricMeasure(agg="sum", column="total")
        assert measure.measure_columns == frozenset({"total"})

    def test_setting_both_column_and_expression_rejected(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            MetricMeasure(agg="sum", column="a", expression="a * b")

    def test_setting_neither_column_nor_expression_rejected(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            MetricMeasure(agg="sum")

    def test_malformed_expression_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError, match="malformed measure expression"):
            MetricMeasure(agg="sum", expression="abs(x)")

    def test_syntax_error_expression_rejected(self) -> None:
        with pytest.raises(ValueError, match="syntax error"):
            MetricMeasure(agg="sum", expression="a *")

    def test_two_equal_composite_measures_compare_equal(self) -> None:
        a = MetricMeasure(agg="sum", expression="unit_price * quantity")
        b = MetricMeasure(agg="sum", expression="unit_price * quantity")
        assert a == b

    def test_composite_measure_is_hashable(self) -> None:
        # Frozen dataclass — must be hashable so `MetricPlan` (which
        # carries `Metric` which carries `MetricMeasure`) stays
        # hashable for the audit-layer fingerprinting path.
        measure = MetricMeasure(agg="sum", expression="unit_price * quantity")
        assert hash(measure) == hash(MetricMeasure(agg="sum", expression="unit_price * quantity"))


# ----- Metric ----------------------------------------------------------------


def _measure(agg: str = "sum", column: str = "total_amount") -> MetricMeasure:
    return MetricMeasure(agg=agg, column=column)  # type: ignore[arg-type]


def _make_metric(**overrides: object) -> Metric:
    defaults: dict[str, object] = {
        "name": "total_revenue",
        "description": "",
        "entity": "order",
        "measure": _measure(),
        "time_dimension": "order.created_at",
        "time_grains": ("day", "week", "month"),
        "origin": "manual",
    }
    defaults.update(overrides)
    return Metric(**defaults)  # type: ignore[arg-type]


class TestMetricHappyPath:
    def test_constructs_with_all_fields(self) -> None:
        metric = _make_metric()
        assert metric.name == "total_revenue"
        assert metric.entity == "order"
        assert metric.measure.agg == "sum"
        assert metric.measure.column == "total_amount"
        assert metric.time_dimension == "order.created_at"
        assert metric.time_grains == ("day", "week", "month")
        assert metric.origin == "manual"

    def test_origin_defaults_to_manual(self) -> None:
        metric = Metric(
            name="total_revenue",
            description="",
            entity="order",
            measure=_measure(),
            time_dimension="order.created_at",
            time_grains=("day",),
        )
        assert metric.origin == "manual"

    def test_is_frozen(self) -> None:
        metric = _make_metric()
        with pytest.raises(dataclasses.FrozenInstanceError):
            metric.name = "other"  # type: ignore[misc]

    def test_two_equal_metrics_compare_equal(self) -> None:
        # Frozen + tuple-typed `time_grains` means equality is field-wise.
        a = _make_metric()
        b = _make_metric()
        assert a == b

    def test_non_temporal_metric_round_trips(self) -> None:
        # A metric without a time dimension (e.g. "current count of open
        # tickets") drops both `time_dimension` and `time_grains`.
        metric = _make_metric(time_dimension=None, time_grains=())
        assert metric.time_dimension is None
        assert metric.time_grains == ()


class TestMetricValidation:
    @pytest.mark.parametrize(
        "bad_name",
        [
            "",
            "1starts_with_digit",
            "has space",
            "has-hyphen",
            "has.dot",
        ],
    )
    def test_rejects_non_identifier_name(self, bad_name: str) -> None:
        with pytest.raises(ValueError, match="name"):
            _make_metric(name=bad_name)

    @pytest.mark.parametrize(
        "bad_entity",
        [
            "",
            "1starts_with_digit",
            "has space",
        ],
    )
    def test_rejects_non_identifier_entity(self, bad_entity: str) -> None:
        with pytest.raises(ValueError, match="entity"):
            _make_metric(entity=bad_entity)

    @pytest.mark.parametrize(
        "bad_origin",
        [
            "",
            "approved",
            "imported",
            "DBT_IMPORT",
            None,
        ],
    )
    def test_rejects_unknown_origin(self, bad_origin: object) -> None:
        with pytest.raises(ValueError, match="origin"):
            _make_metric(origin=bad_origin)

    def test_accepts_all_three_valid_origins(self) -> None:
        # `dbt_import` is valid at the dataclass; the dbt-metrics
        # importer ships in the same PR (commit 6) and is the sole
        # producer. Lock the symmetry with `Entity.origin` and
        # `CanonicalJoin.origin` here.
        for origin in ("manual", "suggested", "dbt_import"):
            _make_metric(origin=origin)

    def test_rejects_unknown_inference_method(self) -> None:
        # Charter v1.2 2D trust signal: the closed-set check in
        # `__post_init__` rejects any inference_method outside the
        # five-value Literal. Mirrors the equivalent on `Entity` and
        # `CanonicalJoin`.
        with pytest.raises(ValueError, match="inference_method"):
            _make_metric(inference_method="bogus")

    def test_rejects_unknown_validation_state(self) -> None:
        # Charter v1.2 2D trust signal: `validation_state` must be
        # one of {draft, applied, confirmed}.
        with pytest.raises(ValueError, match="validation_state"):
            _make_metric(validation_state="archived")


class TestMetricTimeDimension:
    def test_time_dimension_set_with_empty_grains_rejected(self) -> None:
        # Parallel-emptiness invariant: a time dimension without buckets
        # is meaningless (we don't know how to bucket by it). YAML loads
        # surface this as a guided message; the dataclass refuses
        # construction so direct callers can't bypass.
        with pytest.raises(ValueError, match="time_grains"):
            _make_metric(time_dimension="order.created_at", time_grains=())

    def test_time_grains_set_without_time_dimension_rejected(self) -> None:
        with pytest.raises(ValueError, match="time_dimension"):
            _make_metric(time_dimension=None, time_grains=("day",))

    @pytest.mark.parametrize(
        "bad_dim",
        [
            "",
            "no_dot",  # not entity.column shape
            "too.many.dots",
            "1leading.id",  # bad left side
            "ent.1bad",  # bad right side
            "ent.has space",
            ".empty_left",
            "empty_right.",
        ],
    )
    def test_time_dimension_must_be_qualified_column_form(self, bad_dim: str) -> None:
        with pytest.raises(ValueError, match="time_dimension"):
            _make_metric(time_dimension=bad_dim, time_grains=("day",))

    @pytest.mark.parametrize(
        "grain",
        ["day", "week", "month", "quarter", "year"],
    )
    def test_accepts_all_five_time_grain_literals(self, grain: str) -> None:
        metric = _make_metric(time_dimension="order.created_at", time_grains=(grain,))
        assert metric.time_grains == (grain,)

    @pytest.mark.parametrize(
        "bad_grain",
        [
            "",
            "DAY",  # case-sensitive
            "hour",  # too fine — deferred (sub-day buckets aren't a v1 use case)
            "minute",
            "decade",  # too coarse
            "fortnight",
        ],
    )
    def test_rejects_unknown_time_grain(self, bad_grain: str) -> None:
        with pytest.raises(ValueError, match="time_grains"):
            _make_metric(time_dimension="order.created_at", time_grains=(bad_grain,))

    def test_time_grains_must_be_canonically_sorted(self) -> None:
        # Canonical ordering (day < week < month < quarter < year) so
        # `Metric` equality is deterministic across YAML hand-ordering
        # and store round-trips. The store layer enforces sort on read;
        # the dataclass enforces sort on construction so they can't
        # diverge.
        with pytest.raises(ValueError, match="time_grains"):
            _make_metric(
                time_dimension="order.created_at",
                time_grains=("month", "day"),
            )

    def test_time_grains_must_be_unique(self) -> None:
        # Duplicates are nonsense and would produce duplicate columns
        # in the compiler's emitted SQL. Refuse at construction.
        with pytest.raises(ValueError, match="time_grains"):
            _make_metric(
                time_dimension="order.created_at",
                time_grains=("day", "day"),
            )

    def test_time_grains_is_a_tuple(self) -> None:
        # Tuple, not list — preserves hashability + frozen invariant.
        metric = _make_metric()
        assert isinstance(metric.time_grains, tuple)


class TestDbtOwnedMetricError:
    def test_is_a_value_error_subclass(self) -> None:
        # Mirrors `DbtOwnedEntityError` from PR #28 — callers that just
        # want to surface "this write was refused" can `except ValueError`.
        assert issubclass(DbtOwnedMetricError, ValueError)

    def test_can_be_raised_and_caught(self) -> None:
        with pytest.raises(DbtOwnedMetricError, match="total_revenue"):
            raise DbtOwnedMetricError("total_revenue is owned by dbt; manual edits refused")
