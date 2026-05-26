"""Tests for `schemabrain.setup.doctor_verify`.

The mock-agent smoke must:

  1. Refuse fast (single failing stage) when the store is missing or
     empty — without crashing partway through with a stack trace.
  2. Pass on a seeded store with at least one entity, even when
     embeddings are missing and no source URL is supplied (the
     find_relevant_entities + get_metric stages skip cleanly).
  3. Surface a clear renderer block with per-stage glyphs + timing
     + a single summary line tied to the exit code.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.setup.doctor_verify import (
    VerifyResult,
    VerifyStage,
    render_verify,
    verify_mock_agent,
)


@pytest.fixture()
def seeded_store(tmp_path: Path) -> Iterator[Path]:
    """An in-memory-shaped store with one source, one table, one entity.

    Mirrors the minimum shape verify needs to reach its required
    stages (list_entities + describe_entity). No metrics, no
    embeddings — so the optional stages skip rather than pass.
    """
    path = tmp_path / "store.db"
    store = SQLiteStore(path=path)
    try:
        store.write_table(
            Table(
                name="orders",
                schema_name="public",
                columns=(
                    Column(
                        name="id",
                        table_name="orders",
                        schema_name="public",
                        data_type="bigint",
                        nullable=False,
                        ordinal_position=1,
                        is_primary_key=True,
                    ),
                ),
            ),
            source_connection_id="src",
        )
        store.write_entity(
            Entity(
                name="order",
                description="",
                binding=SingleTableBinding(qualified_table="public.orders"),
                identity="id",
            ),
            source_connection_id="src",
        )
        store.close()
        yield path
    finally:
        # Already closed; idempotent.
        pass


class TestVerifyMockAgentGuards:
    def test_returns_single_failing_stage_when_store_missing(self, tmp_path: Path) -> None:
        ghost = tmp_path / "does-not-exist.db"
        result = verify_mock_agent(store_path=ghost, source_url=None)
        assert len(result.stages) == 1
        assert result.stages[0].status == "fail"
        assert result.stages[0].name == "store_present"
        assert "not found" in result.stages[0].message
        assert result.exit_code == 2

    def test_returns_single_failing_stage_when_no_source_connection(self, tmp_path: Path) -> None:
        """Store file exists with schema but no `tables` rows — verify
        cannot resolve a source_connection_id and refuses fast with a
        clear `source_resolved` failure rather than letting
        list_entities run against an unknown source."""
        path = tmp_path / "empty.db"
        SQLiteStore(path=path).close()
        result = verify_mock_agent(store_path=path, source_url=None)
        assert any(s.name == "source_resolved" and s.status == "fail" for s in result.stages)
        assert result.exit_code == 2

    def test_list_entities_fails_fast_when_no_entities_after_index(self, tmp_path: Path) -> None:
        """Store has indexed `tables` (so source_id resolves) but no
        curated `entities` — list_entities returns []. Verify must
        surface this as a `list_entities` fail with the actionable
        hint to run entity suggest, not as a `source_resolved` fail.
        """
        path = tmp_path / "indexed.db"
        store = SQLiteStore(path=path)
        try:
            store.write_table(
                Table(
                    name="orders",
                    schema_name="public",
                    columns=(
                        Column(
                            name="id",
                            table_name="orders",
                            schema_name="public",
                            data_type="bigint",
                            nullable=False,
                            ordinal_position=1,
                            is_primary_key=True,
                        ),
                    ),
                ),
                source_connection_id="src",
            )
        finally:
            store.close()
        result = verify_mock_agent(store_path=path, source_url=None)
        by_name = {s.name: s for s in result.stages}
        assert by_name["list_entities"].status == "fail"
        assert "entities suggest" in by_name["list_entities"].message
        # No subsequent stages ran — verify fail-fast worked.
        assert "describe_entity" not in by_name
        assert result.exit_code == 2

    def test_describe_entity_failure_short_circuits_remaining_stages(
        self, seeded_store: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When describe_entity raises, verify records a fail stage
        and short-circuits — no find_relevant / get_metric stages run.

        Exercises the early-return branch in verify_mock_agent at the
        describe_stage != pass guard.
        """
        from schemabrain.mcp import describe_entity

        def boom(**_kwargs: object) -> object:
            raise RuntimeError("describe_entity boom")

        monkeypatch.setattr(describe_entity, "describe_entity_impl", boom)
        result = verify_mock_agent(store_path=seeded_store, source_url=None)
        by_name = {s.name: s for s in result.stages}
        assert by_name["describe_entity"].status == "fail"
        assert "describe_entity boom" in by_name["describe_entity"].message
        # find_relevant and get_metric must NOT have run.
        assert "find_relevant_entities" not in by_name
        assert "get_metric" not in by_name
        assert result.exit_code == 2

    def test_get_metric_skipped_when_source_url_set_but_no_count_metric(
        self, seeded_store: Path
    ) -> None:
        """The store has entities but no `*_count` metric — get_metric
        skips with the `metrics suggest --apply` recovery hint.

        Exercises the "source_url passed but nothing to execute" path
        that the source_url=None test can't reach."""
        result = verify_mock_agent(store_path=seeded_store, source_url="sqlite:///:memory:")
        by_name = {s.name: s for s in result.stages}
        assert by_name["get_metric"].status == "skipped"
        assert "no `*_count` metric" in by_name["get_metric"].message


class TestVerifyMockAgentHappyPath:
    def test_seeded_store_passes_required_stages(self, seeded_store: Path) -> None:
        result = verify_mock_agent(store_path=seeded_store, source_url=None)
        # Required stages (list + describe) PASS; optional (find +
        # get_metric) SKIP since no embeddings and no source URL.
        by_name = {s.name: s for s in result.stages}
        assert by_name["list_entities"].status == "pass"
        assert "1 entity visible" in by_name["list_entities"].message
        assert by_name["describe_entity"].status == "pass"
        assert "resolved `order`" in by_name["describe_entity"].message
        assert by_name["get_metric"].status == "skipped"
        assert "no --source" in by_name["get_metric"].message
        # Exit code is 0 because skipped != fail.
        assert result.exit_code == 0

    def test_find_relevant_skips_cleanly_without_embeddings(self, seeded_store: Path) -> None:
        """The store has no embeddings — find_relevant_entities returns
        no hits, which the verify treats as `skipped` (degraded but
        not failing), not `fail`. The substrate works; the operator
        just hasn't enabled semantic search."""
        result = verify_mock_agent(store_path=seeded_store, source_url=None)
        by_name = {s.name: s for s in result.stages}
        assert by_name["find_relevant_entities"].status in ("skipped", "pass")
        # If skipped, it's the no-hits skip (not the import-error skip)
        # since fastembed IS importable in the test env on supported
        # platforms. Either skip shape is acceptable.

    def test_find_relevant_skips_when_embedder_construction_fails(
        self, seeded_store: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When `fastembed_default()` raises (no cached ONNX model,
        no network, etc.), the verify must surface `skipped` not
        `fail`. The substrate is otherwise green; semantic retrieval
        is an optional capability whose absence shouldn't flag the
        whole verify as broken.
        """
        from schemabrain.enrichment import embeddings

        def boom() -> object:
            raise FileNotFoundError("NoSuchFile: model_optimized.onnx missing on this runner")

        monkeypatch.setattr(embeddings, "fastembed_default", boom)
        result = verify_mock_agent(store_path=seeded_store, source_url=None)
        by_name = {s.name: s for s in result.stages}
        stage = by_name["find_relevant_entities"]
        assert stage.status == "skipped"
        assert "embedder unavailable" in stage.message
        assert "substrate is still green" in stage.message
        # The whole verify exits 0 — `find_relevant_entities` is
        # best-effort, not required.
        assert result.exit_code == 0

    def test_find_relevant_skips_when_search_raises_during_embed(
        self, seeded_store: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The actual CI failure: `fastembed_default()` SUCCEEDS
        (returns an embedder object) but the ONNX model loads
        LAZILY inside `find_relevant_entities_impl`'s first
        `.embed()` call, which raises `NoSuchFile` from onnxruntime.

        The previous shape caught only embedder-construction failures.
        This test pins the broader contract: any failure along the
        embedder + search path is `skipped`, not `fail`.
        """
        from schemabrain.mcp import find_relevant_entities

        def boom(**_kwargs: object) -> object:
            raise FileNotFoundError(
                "NoSuchFile: [ONNXRuntimeError] : 3 : NO_SUCHFILE : "
                "model_optimized.onnx failed. File doesn't exist"
            )

        monkeypatch.setattr(find_relevant_entities, "find_relevant_entities_impl", boom)
        result = verify_mock_agent(store_path=seeded_store, source_url=None)
        by_name = {s.name: s for s in result.stages}
        stage = by_name["find_relevant_entities"]
        assert stage.status == "skipped"
        assert "embedder unavailable" in stage.message
        # Whole verify still exits 0 — `find_relevant_entities` is
        # best-effort, not required.
        assert result.exit_code == 0

    def test_total_duration_sums_stage_durations(self, seeded_store: Path) -> None:
        result = verify_mock_agent(store_path=seeded_store, source_url=None)
        # Total wall time should be >= the sum of stage durations
        # (verify does a tiny amount of work between stages — store
        # open, source_id resolve — so total >= sum, not strict equal).
        stage_sum = sum(s.duration_s for s in result.stages)
        assert result.total_duration_s >= stage_sum * 0.9  # allow 10% slack for timer jitter


class TestRenderVerify:
    def test_renders_per_stage_lines_and_summary(self, capsys: pytest.CaptureFixture[str]) -> None:
        from rich.console import Console

        result = VerifyResult(
            stages=(
                VerifyStage(
                    name="list_entities", status="pass", message="3 entities", duration_s=0.01
                ),
                VerifyStage(
                    name="describe_entity",
                    status="pass",
                    message="resolved `customer`",
                    duration_s=0.02,
                ),
                VerifyStage(
                    name="find_relevant_entities",
                    status="skipped",
                    message="no hits",
                    duration_s=0.03,
                ),
                VerifyStage(
                    name="get_metric",
                    status="skipped",
                    message="no executor",
                    duration_s=0.01,
                ),
            ),
            exit_code=0,
            total_duration_s=0.07,
        )
        console = Console(stderr=True, force_terminal=False)
        render_verify(result, console=console)
        captured = capsys.readouterr()
        # Header carries the total wall time.
        assert "Mock-agent smoke" in captured.err
        # Each stage's name shows up.
        assert "list_entities" in captured.err
        assert "describe_entity" in captured.err
        assert "find_relevant_entities" in captured.err
        assert "get_metric" in captured.err
        # Summary line ties to exit code.
        assert "substrate green" in captured.err

    def test_renders_fail_summary_on_non_zero_exit(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from rich.console import Console

        result = VerifyResult(
            stages=(
                VerifyStage(
                    name="list_entities",
                    status="fail",
                    message="store has no entities",
                    duration_s=0.01,
                ),
            ),
            exit_code=2,
            total_duration_s=0.01,
        )
        console = Console(stderr=True, force_terminal=False)
        render_verify(result, console=console)
        captured = capsys.readouterr()
        assert "required stage failed" in captured.err


class TestCliDoctorVerify:
    """`schemabrain doctor --verify` integration: argparse → _cmd_doctor
    branch → verify_mock_agent → render_verify → exit code."""

    def test_verify_against_seeded_store_exits_zero(
        self, seeded_store: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from schemabrain.cli import main

        rc = main(["doctor", "--verify", "--store-path", str(seeded_store)])
        captured = capsys.readouterr()
        assert rc == 0
        assert "Mock-agent smoke" in captured.err
        assert "list_entities" in captured.err

    def test_verify_against_missing_store_exits_two(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from schemabrain.cli import main

        ghost = tmp_path / "does-not-exist.db"
        rc = main(["doctor", "--verify", "--store-path", str(ghost)])
        captured = capsys.readouterr()
        assert rc == 2
        assert "store not found" in captured.err
