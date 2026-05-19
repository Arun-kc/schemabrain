"""Tests for the new design-system dry-run cost-estimate Panel body
(`_compose_dry_run_panel_body` in cli.py).

The wider integration shape is verified in tests/test_cli.py via the
real `index --dry-run` command; this file pins the body builder's
row-suppression logic in isolation.
"""

from __future__ import annotations

import io

from rich.console import Console

from schemabrain.cli import _compose_dry_run_panel_body
from schemabrain.indexer import IndexResult


def _render_body(
    result: IndexResult, *, store_path: str = "/tmp/store.db", elapsed_s: float = 1.5
) -> str:
    body = _compose_dry_run_panel_body(
        result=result,
        store_path=store_path,
        elapsed_s=elapsed_s,
    )
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=120)
    console.print(body)
    return buf.getvalue()


def _result(**overrides: int) -> IndexResult:
    defaults = dict(
        tables_seen=7,
        tables_changed=3,
        tables_unchanged=4,
        tables_removed=0,
        columns_added=30,
        columns_changed=0,
        columns_removed=0,
        descriptions_generated=0,
        llm_cost_usd=0.0,
        embeddings_generated=0,
    )
    defaults.update(overrides)
    return IndexResult(**defaults)


class TestDryRunPanelBody:
    def test_renders_tables_row(self) -> None:
        out = _render_body(_result())
        assert "tables" in out
        assert "7 seen" in out
        assert "3 changed" in out

    def test_renders_columns_row(self) -> None:
        out = _render_body(_result(columns_added=12, columns_changed=4, columns_removed=2))
        assert "columns" in out
        assert "+12" in out and "~4" in out and "-2" in out

    def test_renders_store_row(self) -> None:
        out = _render_body(_result(), store_path="/tmp/sb_smoke.db")
        assert "/tmp/sb_smoke.db" in out

    def test_renders_elapsed_row(self) -> None:
        out = _render_body(_result(), elapsed_s=2.7)
        assert "2.7s" in out

    def test_omits_est_cost_when_no_descriptions(self) -> None:
        # Zero LLM descriptions → "est. cost" row is suppressed
        # (no point showing a $0.0000 cost row for a cost-free run).
        out = _render_body(_result(descriptions_generated=0))
        assert "est. cost" not in out

    def test_renders_est_cost_when_descriptions_generated(self) -> None:
        out = _render_body(_result(descriptions_generated=42, llm_cost_usd=0.0123))
        assert "est. cost" in out
        assert "0.0123" in out
        assert "42 description" in out

    def test_singular_description_grammar(self) -> None:
        out = _render_body(_result(descriptions_generated=1, llm_cost_usd=0.0001))
        # ``1 description`` (no trailing `s`) per English grammar.
        assert "1 description" in out
        # The literal string ``1 descriptions`` must not appear.
        assert "1 descriptions" not in out

    def test_omits_embeddings_when_zero(self) -> None:
        out = _render_body(_result(embeddings_generated=0))
        assert "embeddings" not in out

    def test_renders_embeddings_when_positive(self) -> None:
        out = _render_body(_result(embeddings_generated=18))
        assert "embeddings" in out
        assert "18 estimated" in out
