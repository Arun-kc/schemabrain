"""Tests for `schemabrain.metrics.audit` + the `metrics audit` CLI.

Closes the PR-6h.3 fold: PR-6h.2 prevented the LLM from
RE-SUGGESTING anti-pattern metrics, but stores that received an
anti-pattern metric BEFORE PR-6h.2 shipped still have the bad metric
persisted. This module + CLI command scan + (optionally) delete them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from schemabrain.cli import main
from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.metric import Metric, MetricMeasure
from schemabrain.core.models import Column, Table
from schemabrain.core.store import DbtOwnedMetricError, SQLiteStore
from schemabrain.metrics.audit import (
    AuditFinding,
    find_anti_pattern_metrics,
    remove_anti_pattern_metrics,
)

SOURCE = "src_a"


def _seed_anchor(store: SQLiteStore) -> None:
    """Minimal entity scaffolding so metrics have somewhere to anchor."""
    store.write_table(
        Table(
            name="order_items",
            schema_name="public",
            columns=(
                Column(
                    name="id",
                    table_name="order_items",
                    schema_name="public",
                    data_type="bigint",
                    nullable=False,
                    ordinal_position=1,
                    is_primary_key=True,
                ),
                Column(
                    name="unit_price_cents",
                    table_name="order_items",
                    schema_name="public",
                    data_type="integer",
                    nullable=False,
                    ordinal_position=2,
                    is_primary_key=False,
                ),
                Column(
                    name="quantity",
                    table_name="order_items",
                    schema_name="public",
                    data_type="integer",
                    nullable=False,
                    ordinal_position=3,
                    is_primary_key=False,
                ),
            ),
        ),
        source_connection_id=SOURCE,
    )
    store.write_entity(
        Entity(
            name="order_item",
            description="",
            binding=SingleTableBinding(qualified_table="public.order_items"),
            identity="id",
        ),
        source_connection_id=SOURCE,
    )


def _write_metric(
    store: SQLiteStore,
    name: str,
    *,
    description: str,
    column: str = "unit_price_cents",
    origin: str = "suggested",
) -> Metric:
    metric = Metric(
        name=name,
        description=description,
        entity="order_item",
        measure=MetricMeasure(agg="sum", column=column),
        time_dimension=None,
        time_grains=(),
        origin=origin,  # type: ignore[arg-type]
    )
    store.write_metric(metric, source_connection_id=SOURCE)
    return metric


# ---------------------------------------------------------------------------
# find_anti_pattern_metrics
# ---------------------------------------------------------------------------


class TestFindAntiPatternMetrics:
    """The detector matches descriptions against the same phrase list
    PR-6h.2's `_parse_candidate` uses, so a future drift in either
    side surfaces in both places."""

    def test_finds_metric_with_not_directly_available_phrase(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_anchor(store)
            _write_metric(
                store,
                "total_order_item_revenue",
                description=(
                    "Sum of unit_price_cents across order_items. "
                    "True revenue (price x quantity) is not directly "
                    "available, so this is a price-mix indicator only."
                ),
            )
            _write_metric(
                store,
                "total_items_sold",
                description="Sum of quantity across order_items.",
                column="quantity",
            )

            findings = find_anti_pattern_metrics(store, source_connection_id=SOURCE)

        assert len(findings) == 1
        assert findings[0].metric.name == "total_order_item_revenue"
        assert findings[0].matched_phrase == "not directly available"
        assert findings[0].source_connection_id == SOURCE

    def test_finds_metric_with_would_require_multiplication_phrase(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_anchor(store)
            _write_metric(
                store,
                "approx_revenue",
                description="Computing accurate revenue would require multiplication of price by quantity.",
            )
            findings = find_anti_pattern_metrics(store, source_connection_id=SOURCE)

        assert len(findings) == 1
        assert findings[0].matched_phrase == "would require multiplication"

    def test_clean_store_returns_empty(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_anchor(store)
            _write_metric(
                store,
                "total_items_sold",
                description="Sum of quantity across order_items.",
                column="quantity",
            )
            findings = find_anti_pattern_metrics(store, source_connection_id=SOURCE)
        assert findings == []

    def test_phrase_match_is_case_insensitive(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_anchor(store)
            _write_metric(
                store,
                "weird_metric",
                description="This is NOT DIRECTLY AVAILABLE at full precision.",
            )
            findings = find_anti_pattern_metrics(store, source_connection_id=SOURCE)
        assert len(findings) == 1

    def test_finding_is_dbt_owned_property(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_anchor(store)
            _write_metric(
                store,
                "bad_dbt",
                description="Value not directly available at quantity granularity.",
                origin="dbt_import",
            )
            findings = find_anti_pattern_metrics(store, source_connection_id=SOURCE)
        assert len(findings) == 1
        assert findings[0].is_dbt_owned is True


# ---------------------------------------------------------------------------
# remove_anti_pattern_metrics
# ---------------------------------------------------------------------------


class TestRemoveAntiPatternMetrics:
    def test_removes_suggested_metrics_in_place(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_anchor(store)
            _write_metric(
                store,
                "bad_revenue",
                description="Not directly available — price-mix only.",
            )
            findings = find_anti_pattern_metrics(store, source_connection_id=SOURCE)
            removed, skipped = remove_anti_pattern_metrics(store, findings)
            after = store.list_metrics(source_connection_id=SOURCE)

        assert removed == 1
        assert skipped == []
        assert after == []

    def test_skips_dbt_owned_metrics(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_anchor(store)
            _write_metric(
                store,
                "dbt_bad",
                description="Not directly available at line-item granularity.",
                origin="dbt_import",
            )
            findings = find_anti_pattern_metrics(store, source_connection_id=SOURCE)
            removed, skipped = remove_anti_pattern_metrics(store, findings)
            after = store.list_metrics(source_connection_id=SOURCE)

        assert removed == 0
        assert len(skipped) == 1
        assert skipped[0].is_dbt_owned is True
        # Metric still present — only the upstream dbt repo can remove it.
        assert len(after) == 1
        assert after[0].name == "dbt_bad"

    def test_mixed_dbt_and_suggested_partial_removal(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_anchor(store)
            _write_metric(
                store,
                "suggested_bad",
                description="not directly available",
            )
            _write_metric(
                store,
                "dbt_bad",
                description="not directly available",
                origin="dbt_import",
            )
            findings = find_anti_pattern_metrics(store, source_connection_id=SOURCE)
            removed, skipped = remove_anti_pattern_metrics(store, findings)
            after_names = {m.name for m in store.list_metrics(source_connection_id=SOURCE)}

        assert removed == 1
        assert len(skipped) == 1
        assert after_names == {"dbt_bad"}


class TestDeleteMetricStoreAPI:
    """Pins the new `Store.delete_metric` contract."""

    def test_returns_false_when_metric_absent(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_anchor(store)
            assert store.delete_metric("nope", source_connection_id=SOURCE) is False

    def test_returns_true_when_metric_deleted(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_anchor(store)
            _write_metric(store, "kept", description="ok", column="quantity")
            assert store.delete_metric("kept", source_connection_id=SOURCE) is True
            assert store.get_metric("kept", source_connection_id=SOURCE) is None

    def test_dbt_owned_deletion_raises(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_anchor(store)
            _write_metric(
                store,
                "dbt_thing",
                description="ok",
                origin="dbt_import",
                column="quantity",
            )
            with pytest.raises(DbtOwnedMetricError):
                store.delete_metric("dbt_thing", source_connection_id=SOURCE)
            # Still present because the delete refused.
            assert store.get_metric("dbt_thing", source_connection_id=SOURCE) is not None


# ---------------------------------------------------------------------------
# CLI: `schemabrain metrics audit [--fix]`
# ---------------------------------------------------------------------------


class TestCliMetricsAudit:
    def test_audit_clean_exits_zero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store_path = tmp_path / "store.db"
        with SQLiteStore(store_path) as store:
            _seed_anchor(store)
            _write_metric(store, "good", description="all clean", column="quantity")
        exit_code = main(["metrics", "audit", "--store-path", str(store_path)])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "no anti-pattern metrics found" in out

    def test_audit_finds_flagged_metric_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store_path = tmp_path / "store.db"
        with SQLiteStore(store_path) as store:
            _seed_anchor(store)
            _write_metric(
                store,
                "bad_revenue",
                description="Value not directly available at the granularity claimed.",
            )
        exit_code = main(["metrics", "audit", "--store-path", str(store_path)])
        out = capsys.readouterr().out
        assert exit_code == 1
        assert "bad_revenue" in out
        assert "not directly available" in out
        assert "--fix" in out  # hint to the next step

    def test_audit_fix_removes_flagged_metrics(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store_path = tmp_path / "store.db"
        with SQLiteStore(store_path) as store:
            _seed_anchor(store)
            _write_metric(
                store,
                "bad_revenue",
                description="Not directly available at the right granularity.",
            )
            _write_metric(
                store,
                "good_count",
                description="Just a clean quantity sum.",
                column="quantity",
            )
        exit_code = main(["metrics", "audit", "--store-path", str(store_path), "--fix"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "removed 1 metric" in out
        with SQLiteStore(store_path) as store:
            remaining = {m.name for m in store.list_metrics(source_connection_id=SOURCE)}
        assert remaining == {"good_count"}

    def test_audit_with_source_filter_narrows_to_one_source(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The `--source URL` flag scopes the audit to one indexed
        source. Exercises the URL-resolution branch in the CLI handler.
        """
        store_path = tmp_path / "store.db"
        with SQLiteStore(store_path) as store:
            _seed_anchor(store)
            _write_metric(
                store,
                "bad_one",
                description="Not directly available at the right granularity.",
            )
        # Avoid touching the dev repo's .env (which may carry a real
        # DATABASE_URL); also pin to tmp_path so other tests' state
        # can't leak in.
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        # Use --url-env to avoid embedding a URL in argv.
        monkeypatch.setenv("DUMMY_DB_URL", "postgresql://localhost/test")
        exit_code = main(
            [
                "metrics",
                "audit",
                "--store-path",
                str(store_path),
                "--url-env",
                "DUMMY_DB_URL",
            ]
        )
        out = capsys.readouterr().out
        # Source-id-derived filtering — the URL hashes to a different
        # source than where the metric was written (SOURCE='src_a'),
        # so the audit comes up clean against that other source.
        assert exit_code == 0
        assert "no anti-pattern metrics found" in out

    def test_audit_fix_reports_dbt_owned_skipped(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store_path = tmp_path / "store.db"
        with SQLiteStore(store_path) as store:
            _seed_anchor(store)
            _write_metric(
                store,
                "dbt_bad",
                description="Value not directly available.",
                origin="dbt_import",
            )
        exit_code = main(["metrics", "audit", "--store-path", str(store_path), "--fix"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "DBT-OWNED" in out or "dbt-owned" in out
        # Metric still in place.
        with SQLiteStore(store_path) as store:
            assert store.get_metric("dbt_bad", source_connection_id=SOURCE) is not None


class TestCrossSourceAudit:
    """Cross-source audit path: source_connection_id=None scans across
    every indexed source. Covers the per-finding source_id discovery
    loop that the single-source path skips.
    """

    def test_finds_flagged_metric_across_sources(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_anchor(store)
            _write_metric(
                store,
                "bad_one",
                description="Not directly available for line items.",
            )
            findings = find_anti_pattern_metrics(store, source_connection_id=None)
        assert len(findings) == 1
        assert findings[0].source_connection_id == SOURCE

    def test_clean_metrics_skipped_in_cross_source_loop(self, tmp_path: Path) -> None:
        # The cross-source path must skip metrics whose descriptions
        # don't match — this exercises the `if phrase is None: continue`
        # branch separately from the single-source path.
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_anchor(store)
            _write_metric(store, "clean", description="Sum of quantity.", column="quantity")
            _write_metric(
                store,
                "bad",
                description="Not directly available at line-item granularity.",
            )
            findings = find_anti_pattern_metrics(store, source_connection_id=None)
        assert {f.metric.name for f in findings} == {"bad"}


class TestRemoveCountsDeletedOnly:
    """Pins the `if deleted: removed += 1` branch in
    `remove_anti_pattern_metrics` — when a metric vanishes between
    the audit pass and the fix call (e.g., a concurrent ops cleanup
    deleted the row), `delete_metric` returns False and the counter
    must not advance.
    """

    def test_concurrent_deletion_not_double_counted(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_anchor(store)
            _write_metric(
                store,
                "to_be_deleted",
                description="not directly available",
            )
            findings = find_anti_pattern_metrics(store, source_connection_id=SOURCE)
            # Simulate a concurrent delete between audit + fix —
            # `delete_metric` returns False on the second call.
            store.delete_metric("to_be_deleted", source_connection_id=SOURCE)
            removed, skipped = remove_anti_pattern_metrics(store, findings)
        assert removed == 0
        assert skipped == []


def test_audit_finding_pickles() -> None:
    """Lock the frozen dataclass shape for future audit-fingerprint
    work, same posture as the other compiler error pickle tests."""
    import pickle

    metric = Metric(
        name="x",
        description="not directly available",
        entity="order_item",
        measure=MetricMeasure(agg="sum", column="unit_price_cents"),
        time_dimension=None,
        time_grains=(),
        origin="suggested",
    )
    finding = AuditFinding(
        metric=metric,
        matched_phrase="not directly available",
        source_connection_id="src",
    )
    revived = pickle.loads(pickle.dumps(finding))
    assert revived == finding
