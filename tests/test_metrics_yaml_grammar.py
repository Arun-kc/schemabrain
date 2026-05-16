"""Tests for `metrics/yaml_grammar.py`.

Pins the metric YAML grammar:

  - `version: 1` required (integer; string `"1"` + float `1.0` rejected).
  - Top-level required keys: `version`, `name`, `entity`, `measure`.
  - Top-level optional keys: `description`, `time_dimension`,
    `time_grains`, `origin`.
  - Strict-keys rejection of typos.
  - `measure` is a mapping with required `agg` + `column`; strict keys.
  - `time_dimension` + `time_grains` must be both present or both absent.
  - `time_grains` is a non-empty list of TimeGrain literals (`day`,
    `week`, `month`, `quarter`, `year`) when present.
  - `MetricYamlError` is the uniform CLI-facing error type.
  - dbt_import is RESERVED at YAML parse-time — refused with a guided
    message pointing at `schemabrain import dbt --include-metrics`
    (same posture as the join grammar's dbt_import reservation).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from schemabrain.core.metric import Metric
from schemabrain.metrics.yaml_grammar import (
    MetricYamlError,
    parse_metric_yaml,
    parse_metric_yaml_file,
)

# ----- happy paths -----------------------------------------------------------


def _minimal_yaml() -> str:
    # Smallest valid metric: name + entity + measure. No time dimension.
    return """
version: 1
name: open_ticket_count
entity: support_ticket
measure:
  agg: count
  column: id
""".strip()


def _temporal_yaml() -> str:
    return """
version: 1
name: total_revenue
description: Sum of completed order totals.
entity: order
measure:
  agg: sum
  column: total_amount
time_dimension: order.created_at
time_grains: [day, week, month]
""".strip()


class TestHappyPath:
    def test_minimal_yaml_round_trips(self) -> None:
        metric = parse_metric_yaml(_minimal_yaml())
        assert isinstance(metric, Metric)
        assert metric.name == "open_ticket_count"
        assert metric.entity == "support_ticket"
        assert metric.measure.agg == "count"
        assert metric.measure.column == "id"
        assert metric.description == ""
        assert metric.time_dimension is None
        assert metric.time_grains == ()
        assert metric.origin == "manual"

    def test_temporal_yaml_round_trips(self) -> None:
        metric = parse_metric_yaml(_temporal_yaml())
        assert metric.name == "total_revenue"
        assert metric.description == "Sum of completed order totals."
        assert metric.time_dimension == "order.created_at"
        assert metric.time_grains == ("day", "week", "month")

    def test_explicit_origin_carries_through(self) -> None:
        text = """
version: 1
name: total_revenue
entity: order
measure:
  agg: sum
  column: total_amount
origin: suggested
""".strip()
        metric = parse_metric_yaml(text)
        assert metric.origin == "suggested"

    @pytest.mark.parametrize("origin", ["manual", "suggested"])
    def test_manual_and_suggested_origins_accepted(self, origin: str) -> None:
        # `dbt_import` is RESERVED at YAML parse-time (see
        # `TestDbtImportReservation`); only values with hand-authoring
        # use cases parse cleanly.
        text = f"""
version: 1
name: total_revenue
entity: order
measure:
  agg: sum
  column: total_amount
origin: {origin}
""".strip()
        metric = parse_metric_yaml(text)
        assert metric.origin == origin

    def test_time_grains_canonical_ordering_round_trips(self) -> None:
        # YAML lets users write `[month, day, week]` — but the dataclass
        # invariant requires canonical sort. Parser surfaces the error
        # with a guided message naming the field rather than letting
        # the dataclass ValueError leak verbatim.
        text = """
version: 1
name: total_revenue
entity: order
measure:
  agg: sum
  column: total_amount
time_dimension: order.created_at
time_grains: [month, day, week]
""".strip()
        with pytest.raises(MetricYamlError, match="time_grains"):
            parse_metric_yaml(text)


class TestDbtImportReservation:
    def test_dbt_import_origin_refused_at_parse_time(self) -> None:
        # The dbt-metrics importer is the sole producer of
        # `origin: dbt_import` on metrics. Hand-authored YAML with
        # `origin: dbt_import` is a user error — refuse at parse
        # with a message pointing them at the importer.
        text = """
version: 1
name: total_revenue
entity: order
measure:
  agg: sum
  column: total_amount
origin: dbt_import
""".strip()
        with pytest.raises(MetricYamlError, match="dbt"):
            parse_metric_yaml(text)


class TestStrictKeys:
    def test_rejects_unknown_top_level_key(self) -> None:
        text = """
version: 1
name: total_revenue
entity: order
measure:
  agg: sum
  column: total_amount
typo_field: oops
""".strip()
        with pytest.raises(MetricYamlError, match="unknown"):
            parse_metric_yaml(text)

    def test_rejects_unknown_measure_key(self) -> None:
        text = """
version: 1
name: total_revenue
entity: order
measure:
  agg: sum
  column: total_amount
  filter: status = 'paid'
""".strip()
        with pytest.raises(MetricYamlError, match="measure"):
            parse_metric_yaml(text)


class TestRequiredFields:
    @pytest.mark.parametrize(
        "missing_field",
        ["version", "name", "entity", "measure"],
    )
    def test_missing_top_level_field_rejected(self, missing_field: str) -> None:
        lines = [
            "version: 1",
            "name: total_revenue",
            "entity: order",
            "measure:",
            "  agg: sum",
            "  column: total_amount",
        ]
        if missing_field == "version":
            text = "\n".join(lines[1:])
        elif missing_field == "name":
            text = "\n".join([lines[0], *lines[2:]])
        elif missing_field == "entity":
            text = "\n".join([*lines[:2], *lines[3:]])
        else:  # measure
            text = "\n".join(lines[:3])
        with pytest.raises(MetricYamlError, match=missing_field):
            parse_metric_yaml(text)

    @pytest.mark.parametrize(
        "missing_measure_key",
        ["agg", "column"],
    )
    def test_missing_measure_subfield_rejected(self, missing_measure_key: str) -> None:
        if missing_measure_key == "agg":
            text = """
version: 1
name: total_revenue
entity: order
measure:
  column: total_amount
""".strip()
        else:
            text = """
version: 1
name: total_revenue
entity: order
measure:
  agg: sum
""".strip()
        with pytest.raises(MetricYamlError, match=missing_measure_key):
            parse_metric_yaml(text)

    def test_empty_yaml_rejected(self) -> None:
        with pytest.raises(MetricYamlError, match="empty"):
            parse_metric_yaml("")

    def test_non_mapping_top_level_rejected(self) -> None:
        with pytest.raises(MetricYamlError, match="mapping"):
            parse_metric_yaml("- not a mapping\n")

    def test_measure_must_be_a_mapping(self) -> None:
        text = """
version: 1
name: total_revenue
entity: order
measure: sum(total_amount)
""".strip()
        with pytest.raises(MetricYamlError, match="measure"):
            parse_metric_yaml(text)


class TestVersion:
    def test_string_version_rejected(self) -> None:
        text = """
version: "1"
name: total_revenue
entity: order
measure:
  agg: sum
  column: total_amount
""".strip()
        with pytest.raises(MetricYamlError, match="version"):
            parse_metric_yaml(text)

    def test_float_version_rejected(self) -> None:
        text = """
version: 1.0
name: total_revenue
entity: order
measure:
  agg: sum
  column: total_amount
""".strip()
        with pytest.raises(MetricYamlError, match="version"):
            parse_metric_yaml(text)

    def test_bool_version_rejected(self) -> None:
        # PyYAML parses `version: true` as Python `True` which is an int
        # subclass. Catching the bool case explicitly prevents that
        # silent acceptance.
        text = """
version: true
name: total_revenue
entity: order
measure:
  agg: sum
  column: total_amount
""".strip()
        with pytest.raises(MetricYamlError, match="version"):
            parse_metric_yaml(text)

    def test_wrong_version_number_rejected(self) -> None:
        text = """
version: 2
name: total_revenue
entity: order
measure:
  agg: sum
  column: total_amount
""".strip()
        with pytest.raises(MetricYamlError, match="version"):
            parse_metric_yaml(text)


class TestTimeDimensionPairing:
    def test_time_dimension_without_grains_rejected(self) -> None:
        text = """
version: 1
name: total_revenue
entity: order
measure:
  agg: sum
  column: total_amount
time_dimension: order.created_at
""".strip()
        with pytest.raises(MetricYamlError, match="time_grains"):
            parse_metric_yaml(text)

    def test_grains_without_time_dimension_rejected(self) -> None:
        text = """
version: 1
name: total_revenue
entity: order
measure:
  agg: sum
  column: total_amount
time_grains: [day]
""".strip()
        with pytest.raises(MetricYamlError, match="time_dimension"):
            parse_metric_yaml(text)

    def test_empty_time_grains_list_rejected(self) -> None:
        text = """
version: 1
name: total_revenue
entity: order
measure:
  agg: sum
  column: total_amount
time_dimension: order.created_at
time_grains: []
""".strip()
        with pytest.raises(MetricYamlError, match="time_grains"):
            parse_metric_yaml(text)

    def test_non_list_time_grains_rejected(self) -> None:
        text = """
version: 1
name: total_revenue
entity: order
measure:
  agg: sum
  column: total_amount
time_dimension: order.created_at
time_grains: day
""".strip()
        with pytest.raises(MetricYamlError, match="time_grains"):
            parse_metric_yaml(text)


class TestTypeChecking:
    def test_non_string_name_rejected(self) -> None:
        text = """
version: 1
name: 42
entity: order
measure:
  agg: sum
  column: total_amount
""".strip()
        with pytest.raises(MetricYamlError, match="name"):
            parse_metric_yaml(text)

    def test_non_string_entity_rejected(self) -> None:
        text = """
version: 1
name: total_revenue
entity: 42
measure:
  agg: sum
  column: total_amount
""".strip()
        with pytest.raises(MetricYamlError, match="entity"):
            parse_metric_yaml(text)

    def test_non_string_origin_rejected(self) -> None:
        text = """
version: 1
name: total_revenue
entity: order
measure:
  agg: sum
  column: total_amount
origin: 42
""".strip()
        with pytest.raises(MetricYamlError, match="origin"):
            parse_metric_yaml(text)

    def test_non_string_description_rejected(self) -> None:
        text = """
version: 1
name: total_revenue
description: 42
entity: order
measure:
  agg: sum
  column: total_amount
""".strip()
        with pytest.raises(MetricYamlError, match="description"):
            parse_metric_yaml(text)

    def test_non_string_time_dimension_rejected(self) -> None:
        text = """
version: 1
name: total_revenue
entity: order
measure:
  agg: sum
  column: total_amount
time_dimension: 42
time_grains: [day]
""".strip()
        with pytest.raises(MetricYamlError, match="time_dimension"):
            parse_metric_yaml(text)

    def test_non_string_time_grain_value_rejected(self) -> None:
        text = """
version: 1
name: total_revenue
entity: order
measure:
  agg: sum
  column: total_amount
time_dimension: order.created_at
time_grains: [1]
""".strip()
        with pytest.raises(MetricYamlError, match="time_grains"):
            parse_metric_yaml(text)

    def test_non_string_measure_agg_rejected(self) -> None:
        text = """
version: 1
name: total_revenue
entity: order
measure:
  agg: 42
  column: total_amount
""".strip()
        with pytest.raises(MetricYamlError, match="agg"):
            parse_metric_yaml(text)

    def test_non_string_measure_column_rejected(self) -> None:
        text = """
version: 1
name: total_revenue
entity: order
measure:
  agg: sum
  column: 42
""".strip()
        with pytest.raises(MetricYamlError, match="column"):
            parse_metric_yaml(text)


class TestValidationLeakage:
    def test_dataclass_validation_errors_wrap_cleanly(self) -> None:
        # A YAML with a bad-shape `name` should raise a single
        # MetricYamlError naming the field, not let the underlying
        # `Metric.__post_init__` ValueError leak verbatim with no
        # context.
        text = """
version: 1
name: 1leading_digit
entity: order
measure:
  agg: sum
  column: total_amount
""".strip()
        with pytest.raises(MetricYamlError, match="validation failed"):
            parse_metric_yaml(text)


class TestParseFile:
    def test_parses_from_path(self, tmp_path: Path) -> None:
        target = tmp_path / "total_revenue.yaml"
        target.write_text(_temporal_yaml(), encoding="utf-8")
        metric = parse_metric_yaml_file(target)
        assert metric.name == "total_revenue"

    def test_missing_file_raises_file_not_found(self, tmp_path: Path) -> None:
        # Missing files are passed through verbatim so the CLI can
        # distinguish "wrong path" from "malformed YAML."
        missing = tmp_path / "absent.yaml"
        with pytest.raises(FileNotFoundError):
            parse_metric_yaml_file(missing)

    def test_directory_passed_as_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(IsADirectoryError):
            parse_metric_yaml_file(tmp_path)

    def test_non_utf8_file_wrapped(self, tmp_path: Path) -> None:
        target = tmp_path / "bad.yaml"
        target.write_bytes(b"\xff\xfeversion: 1\n")
        with pytest.raises(MetricYamlError, match="UTF-8"):
            parse_metric_yaml_file(target)


class TestYamlParseErrors:
    def test_malformed_yaml_raises(self) -> None:
        # YAML with an unclosed list/mapping; PyYAML raises YAMLError,
        # which we wrap as MetricYamlError so callers don't need to
        # import yaml just to except.
        text = "name: total_revenue\n  measure: ["
        with pytest.raises(MetricYamlError, match="parse"):
            parse_metric_yaml(text)


class TestParseErrorMessages:
    def test_empty_required_string_field_rejected(self) -> None:
        # `_require_str` rejects empty strings with a "must be
        # non-empty" message — distinct from missing-field (which is
        # surfaced as "missing required").
        text = """
version: 1
name: ""
entity: order
measure:
  agg: sum
  column: total_amount
""".strip()
        with pytest.raises(MetricYamlError, match="non-empty"):
            parse_metric_yaml(text)

    def test_invalid_agg_string_rejected_with_allowed_list(self) -> None:
        # An agg that's a string but not in `_VALID_AGGS` (e.g.
        # "median") raises with a message listing the allowed set so
        # the user knows what to try instead.
        text = """
version: 1
name: total_revenue
entity: order
measure:
  agg: median
  column: total_amount
""".strip()
        with pytest.raises(MetricYamlError, match="median"):
            parse_metric_yaml(text)

    def test_measure_dataclass_validation_wrapped(self) -> None:
        # `MetricMeasure.__post_init__` rejects non-identifier
        # `column` values. The parser catches the raw ValueError and
        # re-raises with a `measure:` prefix so the user sees which
        # field's validation fired.
        text = """
version: 1
name: total_revenue
entity: order
measure:
  agg: sum
  column: "has space"
""".strip()
        with pytest.raises(MetricYamlError, match="measure"):
            parse_metric_yaml(text)

    def test_invalid_time_grain_string_rejected_with_allowed_list(self) -> None:
        # A time_grains item that's a string but not in `_VALID_GRAINS`
        # (e.g. "hour") raises with the allowed set listed.
        text = """
version: 1
name: total_revenue
entity: order
measure:
  agg: sum
  column: total_amount
time_dimension: order.created_at
time_grains: [hour]
""".strip()
        with pytest.raises(MetricYamlError, match="hour"):
            parse_metric_yaml(text)

    def test_invalid_origin_string_rejected_with_allowed_list(self) -> None:
        # An origin that's a string but neither `dbt_import` (which has
        # its own dedicated reservation message) nor in `_VALID_ORIGINS`
        # falls through to the generic "must be one of" branch.
        text = """
version: 1
name: total_revenue
entity: order
measure:
  agg: sum
  column: total_amount
origin: imported
""".strip()
        with pytest.raises(MetricYamlError, match="origin"):
            parse_metric_yaml(text)


class TestParseFilePermissionWrap:
    def test_permission_denied_wrapped(self, tmp_path: Path) -> None:
        # PermissionError is wrapped as MetricYamlError so the CLI
        # surface stays uniform (same posture as
        # `entities/yaml_grammar.py`). We simulate by chmod 000 — the
        # branch is hard to hit on a normal dev machine otherwise.
        import os
        import stat

        target = tmp_path / "no_read.yaml"
        target.write_text("version: 1\n", encoding="utf-8")
        target.chmod(0)
        try:
            # Skip the assertion entirely if the test runner has
            # permissions that bypass mode bits (root in some CI
            # environments). We test the wrap path; if the OS can't
            # produce the PermissionError we can't exercise the wrap.
            try:
                with open(target, "rb"):
                    pass
            except PermissionError:
                with pytest.raises(MetricYamlError, match="permission denied"):
                    parse_metric_yaml_file(target)
            else:
                pytest.skip(
                    "filesystem permits read of 0-mode file; cannot exercise PermissionError wrap"
                )
        finally:
            # Restore so the tmp_path cleanup can remove it.
            target.chmod(stat.S_IRUSR | stat.S_IWUSR)
            del os  # silence unused-import warning when test path skips
