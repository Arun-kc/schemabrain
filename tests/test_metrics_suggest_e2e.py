"""End-to-end: ecommerce schema -> metrics suggest pipeline -> store/file output.

Mirrors `test_entities_suggest_e2e.py` for the metric-suggester arc:
seed an ecommerce schema (tables + entities) into a temp store, run
`schemabrain metrics suggest` with the stub LLM provider, and verify
the three output modes (--apply, --out-dir, --dry-run) land the
expected metric definitions.

The bundled ecommerce metric fixtures live at
`schemabrain/metrics/fixtures/ecommerce/` — `total_revenue.yaml`,
`order_count.yaml`, and `customer_count.yaml`. The stub response is
crafted to mirror those YAMLs; `TestOutDirParityWithBundledFixtures`
pins suggest output against committed fixtures so a future pipeline
refactor that drifts from the bundled-fixture grammar fails loudly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from schemabrain.cli import _make_source_id, main
from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.models import Column, ForeignKey, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.metrics.yaml_grammar import parse_metric_yaml_file

_BUNDLED_DIR = (
    Path(__file__).resolve().parent.parent / "schemabrain" / "metrics" / "fixtures" / "ecommerce"
)
_TEST_URL = "postgresql+psycopg://user:pw@localhost:5432/db"

# Canned LLM response — three metric candidates whose canonical fields
# mirror the bundled metric YAMLs. Confidence + rationale are envelope-
# only and don't have to match anything in the bundled files.
_ECOMMERCE_STUB_RESPONSE = """\
candidates:
  - name: total_revenue
    description: Sum of order totals (cents). One row per requested grain.
    entity: order
    measure:
      agg: sum
      column: total_cents
    time_dimension: order.placed_at
    time_grains: [day, week, month]
    confidence: high
    rationale: total_cents on the order entity is the headline revenue measure.
  - name: order_count
    description: Number of orders placed per requested grain.
    entity: order
    measure:
      agg: count
      column: id
    time_dimension: order.placed_at
    time_grains: [day, week, month]
    confidence: high
    rationale: count(*) on order is the standard volume metric.
  - name: customer_count
    description: Distinct customers per grain.
    entity: order
    measure:
      agg: count_distinct
      column: user_id
    time_dimension: order.placed_at
    time_grains: [week, month]
    confidence: medium
    rationale: count_distinct on user_id captures repeat customers exactly once per bucket.
"""


# ----- shared fixtures -------------------------------------------------------


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


def _ecommerce_tables() -> list[Table]:
    """Mirror the ecommerce schema enough for metric suggestion.

    Two tables: `users` (id) and `orders` (id, user_id FK, total_cents,
    placed_at). Both back entities in the seeded store. Inline so the
    test doesn't need a Postgres container.
    """
    users = Table(
        name="users",
        schema_name="public",
        columns=(_column(name="id", table="users", is_primary_key=True, ordinal=1),),
    )
    orders = Table(
        name="orders",
        schema_name="public",
        columns=(
            _column(name="id", table="orders", is_primary_key=True, ordinal=1),
            _column(name="user_id", table="orders", ordinal=2),
            _column(
                name="total_cents",
                table="orders",
                data_type="integer",
                ordinal=3,
            ),
            _column(
                name="placed_at",
                table="orders",
                data_type="timestamp",
                ordinal=4,
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


def _ecommerce_entities() -> list[Entity]:
    return [
        Entity(
            name="customer",
            description="A registered shopper account",
            binding=SingleTableBinding(qualified_table="public.users"),
            identity="id",
            origin="manual",
        ),
        Entity(
            name="order",
            description="One placed order",
            binding=SingleTableBinding(qualified_table="public.orders"),
            identity="id",
            origin="manual",
        ),
    ]


def _seed_store_with_ecommerce(store_path: Path) -> None:
    source_id = _make_source_id(_TEST_URL)
    with SQLiteStore(store_path) as store:
        for table in _ecommerce_tables():
            store.write_table(table, source_connection_id=source_id)
        for entity in _ecommerce_entities():
            store.write_entity(entity, source_connection_id=source_id)


@pytest.fixture
def ecommerce_stub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCHEMABRAIN_STUB_RESPONSE", _ECOMMERCE_STUB_RESPONSE)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


# ----- e2e: apply mode -------------------------------------------------------


class TestApplyModeAgainstEcommerce:
    def test_apply_lands_three_metrics_with_suggested_origin(
        self,
        tmp_path: Path,
        ecommerce_stub_env: None,
    ) -> None:
        store_path = tmp_path / "store.db"
        _seed_store_with_ecommerce(store_path)

        exit_code = main(
            [
                "metrics",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
                "--apply",
                "--provider",
                "stub",
            ]
        )
        assert exit_code == 0

        source_id = _make_source_id(_TEST_URL)
        with SQLiteStore(store_path) as store:
            metrics = store.list_metrics(source_connection_id=source_id)

        by_name = {m.name: m for m in metrics}
        assert set(by_name) == {"total_revenue", "order_count", "customer_count"}
        for metric in by_name.values():
            assert metric.origin == "suggested"
            assert metric.entity == "order"

        # Measure shapes match the bundled YAMLs.
        assert by_name["total_revenue"].measure.agg == "sum"
        assert by_name["total_revenue"].measure.column == "total_cents"
        assert by_name["order_count"].measure.agg == "count"
        assert by_name["customer_count"].measure.agg == "count_distinct"
        assert by_name["customer_count"].measure.column == "user_id"


# ----- e2e: out-dir mode + bundled-fixture parity ---------------------------


class TestOutDirParityWithBundledFixtures:
    def test_out_dir_yamls_match_bundled_fixtures_on_canonical_fields(
        self,
        tmp_path: Path,
        ecommerce_stub_env: None,
    ) -> None:
        """Regenerate the 3 bundled metric YAMLs via --out-dir; compare
        against the committed bundled fixtures on canonical Metric fields
        (name, entity, measure, time_dimension, time_grains).

        `origin` is NOT compared: bundled fixtures default to "manual";
        suggest-generated YAMLs carry "suggested". The asymmetry is
        intentional — the same downstream metric can arrive via either
        path, and provenance distinguishes them.

        `description` is NOT compared either: bundled fixtures use
        multi-line scalars; suggest output uses single-line. The
        canonical semantics-bearing fields are what we pin.
        """
        store_path = tmp_path / "store.db"
        _seed_store_with_ecommerce(store_path)
        out_dir = tmp_path / "regenerated"

        exit_code = main(
            [
                "metrics",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
                "--out-dir",
                str(out_dir),
                "--provider",
                "stub",
            ]
        )
        assert exit_code == 0

        for fixture_name in ("total_revenue", "order_count", "customer_count"):
            bundled = parse_metric_yaml_file(_BUNDLED_DIR / f"{fixture_name}.yaml")
            regenerated = parse_metric_yaml_file(out_dir / f"{fixture_name}.yaml")
            assert regenerated.name == bundled.name
            assert regenerated.entity == bundled.entity
            assert regenerated.measure == bundled.measure
            assert regenerated.time_dimension == bundled.time_dimension
            assert regenerated.time_grains == bundled.time_grains
            # Origin asymmetry is intentional.
            assert regenerated.origin == "suggested"
            assert bundled.origin == "manual"


# ----- e2e: dry-run mode ----------------------------------------------------


class TestDryRunAgainstEcommerce:
    def test_dry_run_output_names_all_three_metrics(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        ecommerce_stub_env: None,
    ) -> None:
        store_path = tmp_path / "store.db"
        _seed_store_with_ecommerce(store_path)

        exit_code = main(
            [
                "metrics",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
                "--dry-run",
                "--provider",
                "stub",
            ]
        )
        assert exit_code == 0
        out = capsys.readouterr().out
        # All three canonical metric names visible.
        for name in ("total_revenue", "order_count", "customer_count"):
            assert name in out
        # All three measure shapes visible.
        assert "sum" in out
        assert "count" in out
        assert "count_distinct" in out
        # Cost summary line is present.
        assert "cost:" in out


# ----- e2e: refusal paths ---------------------------------------------------


class TestRefusalPaths:
    def test_refuses_when_no_entities_present(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        ecommerce_stub_env: None,
    ) -> None:
        """`metrics suggest` against an indexed but entity-less store
        should bail with a guided error before the LLM is invoked.
        """
        store_path = tmp_path / "store.db"
        source_id = _make_source_id(_TEST_URL)
        # Seed tables but NO entities.
        with SQLiteStore(store_path) as store:
            for table in _ecommerce_tables():
                store.write_table(table, source_connection_id=source_id)

        exit_code = main(
            [
                "metrics",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
                "--dry-run",
                "--provider",
                "stub",
            ]
        )
        assert exit_code == 1
        err = capsys.readouterr().err
        assert "no entities in the local store" in err
        assert "schemabrain entities" in err

    def test_apply_refuses_metric_anchored_on_unknown_entity(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A candidate that anchors on an entity not present in the store
        should fail at the SQLite FK constraint and surface as an
        actionable error message.
        """
        store_path = tmp_path / "store.db"
        _seed_store_with_ecommerce(store_path)

        # Stub response anchors on `ghost`, which isn't in the store.
        bad_stub = """\
candidates:
  - name: ghost_count
    entity: ghost
    measure: {agg: count, column: id}
    confidence: low
    rationale: Anchored on an entity that does not exist.
"""
        monkeypatch.setenv("SCHEMABRAIN_STUB_RESPONSE", bad_stub)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        exit_code = main(
            [
                "metrics",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
                "--apply",
                "--provider",
                "stub",
            ]
        )
        assert exit_code == 1
        err = capsys.readouterr().err
        assert "ghost" in err
        assert "entities apply" in err

    def test_dry_run_renders_empty_when_no_candidates(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When the LLM returns an empty list, the dry-run prints a
        terminal message rather than nothing — the user shouldn't be
        left wondering whether the command actually ran.
        """
        store_path = tmp_path / "store.db"
        _seed_store_with_ecommerce(store_path)

        monkeypatch.setenv("SCHEMABRAIN_STUB_RESPONSE", "candidates: []")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        exit_code = main(
            [
                "metrics",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
                "--dry-run",
                "--provider",
                "stub",
            ]
        )
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "no candidates suggested" in out

    def test_out_dir_refuses_to_overwrite_existing_files(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        ecommerce_stub_env: None,
    ) -> None:
        store_path = tmp_path / "store.db"
        _seed_store_with_ecommerce(store_path)

        out_dir = tmp_path / "regenerated"
        out_dir.mkdir()
        # Pre-create the file the suggester would write.
        (out_dir / "total_revenue.yaml").write_text("# hand-edited\n")

        exit_code = main(
            [
                "metrics",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
                "--out-dir",
                str(out_dir),
                "--provider",
                "stub",
            ]
        )
        assert exit_code == 1
        err = capsys.readouterr().err
        assert "already contains" in err
        # The hand-edited file survived intact.
        assert (out_dir / "total_revenue.yaml").read_text() == "# hand-edited\n"

    def test_missing_anthropic_api_key_with_default_provider(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        store_path = tmp_path / "store.db"
        _seed_store_with_ecommerce(store_path)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        # D4: chdir so `main()`'s .env load doesn't pick up the dev
        # repo's `.env` and silently re-populate ANTHROPIC_API_KEY.
        monkeypatch.chdir(tmp_path)

        exit_code = main(
            [
                "metrics",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
                "--apply",
                # default provider is anthropic
            ]
        )
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "ANTHROPIC_API_KEY" in err

    def test_malformed_max_cost_env_var_rejected(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        store_path = tmp_path / "store.db"
        _seed_store_with_ecommerce(store_path)
        monkeypatch.setenv("SCHEMABRAIN_MAX_LLM_COST_USD", "not-a-number")
        monkeypatch.setenv("SCHEMABRAIN_STUB_RESPONSE", "candidates: []")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        exit_code = main(
            [
                "metrics",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
                "--dry-run",
                "--provider",
                "stub",
            ]
        )
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "SCHEMABRAIN_MAX_LLM_COST_USD" in err

    def test_stub_provider_without_response_env_warns_and_returns_empty(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        store_path = tmp_path / "store.db"
        _seed_store_with_ecommerce(store_path)
        monkeypatch.delenv("SCHEMABRAIN_STUB_RESPONSE", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        exit_code = main(
            [
                "metrics",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
                "--dry-run",
                "--provider",
                "stub",
            ]
        )
        assert exit_code == 0
        captured = capsys.readouterr()
        # Warning lands on stderr; the empty-result message lands on stdout.
        assert "SCHEMABRAIN_STUB_RESPONSE" in captured.err
        assert "no candidates" in captured.out

    def test_cost_ceiling_exceeded_yields_guided_error(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A microscopic --max-cost-usd forces the pre-flight guard to refuse
        before the LLM is invoked; the user sees a guided error and the
        command exits 1.
        """
        store_path = tmp_path / "store.db"
        _seed_store_with_ecommerce(store_path)
        monkeypatch.setenv("SCHEMABRAIN_STUB_RESPONSE", _ECOMMERCE_STUB_RESPONSE)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        exit_code = main(
            [
                "metrics",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
                "--dry-run",
                "--provider",
                "stub",
                "--max-cost-usd",
                "0.0000001",
            ]
        )
        assert exit_code == 1
        err = capsys.readouterr().err
        assert "cost ceiling exceeded" in err

    def test_out_dir_unwritable_yields_guided_error(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        ecommerce_stub_env: None,
    ) -> None:
        """An unwritable out-dir target surfaces as a guided error with
        exit 2 — not a raw Python traceback. Covers the OSError wrap
        around `out_dir.mkdir(...)` and the per-file `write_text` loop.
        """
        store_path = tmp_path / "store.db"
        _seed_store_with_ecommerce(store_path)

        # A file at the target path makes `mkdir(parents=True,
        # exist_ok=True)` raise NotADirectoryError (an OSError subclass).
        # This is a deterministic, portable trigger that doesn't need
        # a permission-change side effect.
        out_dir = tmp_path / "blocked"
        out_dir.write_text("not a directory")

        exit_code = main(
            [
                "metrics",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
                "--out-dir",
                str(out_dir),
                "--provider",
                "stub",
            ]
        )
        assert exit_code == 2
        err = capsys.readouterr().err
        # GuidedError panel structure: message line + why/fix/next.
        assert "cannot write candidates" in err

    def test_malformed_llm_yaml_yields_guided_error(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When the LLM returns YAML the parser can't accept, the CLI
        surfaces a `MetricSuggestionParseError` -> guided error with
        exit 1. Covers the `except MetricSuggestionParseError` branch
        in `_cmd_metrics_suggest`.
        """
        store_path = tmp_path / "store.db"
        _seed_store_with_ecommerce(store_path)

        # `candidates: not-a-list` — top-level shape error caught at parse.
        monkeypatch.setenv("SCHEMABRAIN_STUB_RESPONSE", "candidates: not-a-list")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        exit_code = main(
            [
                "metrics",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
                "--dry-run",
                "--provider",
                "stub",
            ]
        )
        assert exit_code == 1
        err = capsys.readouterr().err
        assert "unparseable YAML" in err

    def test_sidecar_conflict_refuses_to_overwrite(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        ecommerce_stub_env: None,
    ) -> None:
        """When the metadata sidecar already exists in the target dir,
        the suggester refuses the entire write — same atomicity
        guarantee as for the per-metric YAMLs. Covers the
        `conflicts.append("_suggestion_metadata.json")` branch.
        """
        store_path = tmp_path / "store.db"
        _seed_store_with_ecommerce(store_path)

        out_dir = tmp_path / "regenerated"
        out_dir.mkdir()
        # Pre-create the sidecar file (not the per-metric YAMLs).
        sidecar = out_dir / "_suggestion_metadata.json"
        sidecar.write_text('{"hand-edited": true}')

        exit_code = main(
            [
                "metrics",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
                "--out-dir",
                str(out_dir),
                "--provider",
                "stub",
            ]
        )
        assert exit_code == 1
        err = capsys.readouterr().err
        assert "_suggestion_metadata.json" in err
        # The hand-edited sidecar survived intact.
        assert sidecar.read_text() == '{"hand-edited": true}'

    def test_non_temporal_metric_renders_without_time_fields(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When the LLM proposes a metric without a time dimension, the
        rendered YAML must omit BOTH `time_dimension` and `time_grains`
        (paired-emptiness invariant). Covers the `if metric.description`
        false branch and the `if metric.time_dimension is not None`
        false branch in `_format_metric_yaml_body`.
        """
        store_path = tmp_path / "store.db"
        _seed_store_with_ecommerce(store_path)

        # Stub: ONE candidate, no description, no time_dimension.
        # Exercises both omitted-optional branches in the formatter.
        non_temporal_stub = """\
candidates:
  - name: row_count
    entity: order
    measure: {agg: count, column: id}
    confidence: high
"""
        monkeypatch.setenv("SCHEMABRAIN_STUB_RESPONSE", non_temporal_stub)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        exit_code = main(
            [
                "metrics",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
                "--dry-run",
                "--provider",
                "stub",
            ]
        )
        assert exit_code == 0
        out = capsys.readouterr().out
        # The rendered body must NOT carry time_dimension or time_grains
        # (paired-emptiness invariant flows through to the YAML output).
        assert "time_dimension" not in out
        assert "time_grains" not in out
        # Description is omitted when blank — no `description:` line should appear.
        assert "description:" not in out
        # The metric itself rendered.
        assert "name: row_count" in out
        assert "count" in out

    def test_composite_expression_candidate_renders_expression_not_column(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An LLM-suggested composite-expression candidate must render
        as `measure: {agg: ..., expression: ...}` in the YAML body, NOT
        as `measure: {agg: ..., column: ...}`. Round-tripping via
        `parse_metric_yaml` validates the emitted YAML is grammar-valid
        in the other direction.
        """
        store_path = tmp_path / "store.db"
        _seed_store_with_ecommerce(store_path)

        composite_stub = """\
candidates:
  - name: line_revenue
    description: Revenue per line item.
    entity: order
    measure: {agg: sum, expression: unit_price_cents * quantity}
    confidence: high
"""
        monkeypatch.setenv("SCHEMABRAIN_STUB_RESPONSE", composite_stub)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        exit_code = main(
            [
                "metrics",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
                "--dry-run",
                "--provider",
                "stub",
            ]
        )
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "name: line_revenue" in out
        # The composite branch emits `expression:`, NOT `column:`.
        assert "expression: unit_price_cents * quantity" in out
        # A `column:` line for the measure must NOT appear (the bare-
        # column branch is the wrong one for this candidate).
        assert "column:" not in out


class TestLlmFailureShape:
    """F5: Anthropic SDK errors from `metrics suggest` render Shape C, not a traceback.

    Mirrors `TestLlmFailureShape` in test_cli_entities_suggest.py
    for the metrics-side LLM callsite (`_cmd_metrics_suggest`).
    """

    def _run_with_pipeline_raising(
        self,
        *,
        exc: BaseException,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> int:
        from schemabrain.metrics.suggest import MetricSuggestionPipeline

        store_path = tmp_path / "store.db"
        _seed_store_with_ecommerce(store_path)

        def _raise(self: object, *args: object, **kwargs: object) -> None:
            raise exc

        monkeypatch.setattr(MetricSuggestionPipeline, "propose_from_entities", _raise)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

        return main(
            [
                "metrics",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
                "--apply",
            ]
        )

    def test_overloaded_renders_shape_c_and_exits_two(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import anthropic

        exc = anthropic.APIStatusError.__new__(anthropic.APIStatusError)
        exc.status_code = 529
        exc.message = "Overloaded"

        exit_code = self._run_with_pipeline_raising(
            exc=exc, tmp_path=tmp_path, monkeypatch=monkeypatch
        )
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "◆ error" in err
        assert "Anthropic is overloaded" in err
        # Retry command names the metrics surface, not entities.
        assert "schemabrain metrics suggest" in err
        assert "Traceback" not in err

    def test_connection_error_renders_shape_c_and_exits_two(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import anthropic

        exc = anthropic.APIConnectionError.__new__(anthropic.APIConnectionError)
        exc.message = "connection refused"

        exit_code = self._run_with_pipeline_raising(
            exc=exc, tmp_path=tmp_path, monkeypatch=monkeypatch
        )
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "couldn't reach Anthropic" in err

    def test_non_sdk_exception_still_propagates(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        with pytest.raises(ValueError, match="local bug"):
            self._run_with_pipeline_raising(
                exc=ValueError("local bug"),
                tmp_path=tmp_path,
                monkeypatch=monkeypatch,
            )


class TestSuggestProgressIntegration:
    """F1: `metrics suggest` shows the wizard-parity cost preamble + spinner.

    Mirror of `TestSuggestProgressIntegration` in
    test_cli_entities_suggest.py. Metrics estimate is 2x entities
    ($0.02 vs $0.01) to match the wizard's preamble.
    """

    def test_stub_provider_skips_progress_helper(
        self,
        tmp_path: Path,
        ecommerce_stub_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from contextlib import nullcontext

        progress_calls: list[dict[str, object]] = []

        def _spy_progress(**kwargs):
            progress_calls.append(kwargs)
            return nullcontext()

        monkeypatch.setattr("schemabrain.cli._suggest_llm_progress", _spy_progress)

        store_path = tmp_path / "store.db"
        _seed_store_with_ecommerce(store_path)

        exit_code = main(
            [
                "metrics",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
                "--dry-run",
                "--provider",
                "stub",
            ]
        )
        assert exit_code == 0
        assert progress_calls == []

    def test_anthropic_provider_invokes_progress_helper(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from contextlib import nullcontext

        progress_calls: list[dict[str, object]] = []

        def _spy_progress(**kwargs):
            progress_calls.append(kwargs)
            return nullcontext()

        monkeypatch.setattr("schemabrain.cli._suggest_llm_progress", _spy_progress)

        from schemabrain.metrics.suggest import (
            MetricSuggestionPipeline,
            MetricSuggestionResult,
        )

        monkeypatch.setattr(
            MetricSuggestionPipeline,
            "propose_from_entities",
            lambda self, *args, **kwargs: MetricSuggestionResult(
                candidates=[], total_cost_usd=0.0, llm_model="claude-sonnet-4-6"
            ),
        )

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")

        store_path = tmp_path / "store.db"
        _seed_store_with_ecommerce(store_path)

        exit_code = main(
            [
                "metrics",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
                "--dry-run",
            ]
        )
        assert exit_code == 0
        assert len(progress_calls) == 1
        call = progress_calls[0]
        assert call["model"] == "claude-sonnet-4-6"
        # Metrics is 2x entities cost — pairs with the wizard.
        assert call["cost_estimate_usd"] == 0.02
        assert call["cap_usd"] == 1.0
        assert "define metrics" in str(call["action"])
