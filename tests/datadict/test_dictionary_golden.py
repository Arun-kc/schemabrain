"""Byte-for-byte golden for the rendered SaaS data dictionary.

The committed `golden/saas_dictionary.md` is the source of truth. This
test renders the deterministic offline fixture through the real pipeline
(build store -> build_dictionary -> render_markdown) and asserts the
bytes are identical. Any change to the fixture, the aggregator, or the
renderer that alters output must update the golden in the same commit.

To regenerate after an intentional change, run the exact pipeline this
test runs (build_saas_dictionary_store -> SQLiteStore -> build_dictionary
-> render_markdown, as in `test_rendered_markdown_matches_golden` below)
and overwrite `tests/datadict/golden/saas_dictionary.md` with the output.
There is no env-var regen hook by repo convention — the committed file is
the contract.
"""

from __future__ import annotations

from pathlib import Path

from schemabrain.core.store import SQLiteStore
from schemabrain.datadict.aggregator import build_dictionary
from schemabrain.datadict.demo_store import SOURCE_ID, build_saas_dictionary_store
from schemabrain.datadict.render_markdown import render_markdown

_GOLDEN = Path(__file__).parent / "golden" / "saas_dictionary.md"


def test_rendered_markdown_matches_golden(tmp_path: Path) -> None:
    build_saas_dictionary_store(tmp_path / "dict.db")
    with SQLiteStore(tmp_path / "dict.db") as store:
        model = build_dictionary(store=store, source_connection_id=SOURCE_ID)
    rendered = render_markdown(model)
    golden = _GOLDEN.read_text(encoding="utf-8")
    assert rendered == golden, (
        "rendered dictionary diverged from the golden; if intentional, "
        "regenerate tests/datadict/golden/saas_dictionary.md"
    )


def test_golden_ends_with_single_trailing_newline() -> None:
    golden = _GOLDEN.read_text(encoding="utf-8")
    assert golden.endswith("\n")
    assert not golden.endswith("\n\n")


def test_golden_has_exactly_one_h1() -> None:
    golden = _GOLDEN.read_text(encoding="utf-8")
    assert [line for line in golden.splitlines() if line.startswith("# ")] == ["# Data dictionary"]
