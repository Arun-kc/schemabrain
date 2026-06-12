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

import json
from pathlib import Path

from schemabrain.core.store import SQLiteStore
from schemabrain.datadict.aggregator import build_dictionary
from schemabrain.datadict.demo_store import SOURCE_ID, build_saas_dictionary_store
from schemabrain.datadict.model_json import dictionary_model_to_json
from schemabrain.datadict.render_markdown import render_markdown

_GOLDEN = Path(__file__).parent / "golden" / "saas_dictionary.md"
# The JSON wire shape `GET /api/dict` serves and the TypeScript parity test
# (`web/lib/dict/serialize.test.ts`) renders. Pinned to the live aggregator
# here so the cross-language port can never silently drift from the Python
# model. Regenerate it in the same commit as any model/aggregator change.
_GOLDEN_MODEL_JSON = Path(__file__).parent / "golden" / "saas_dictionary.model.json"


def _build_golden_model(tmp_path: Path):
    build_saas_dictionary_store(tmp_path / "dict.db")
    with SQLiteStore(tmp_path / "dict.db") as store:
        return build_dictionary(store=store, source_connection_id=SOURCE_ID)


def test_rendered_markdown_matches_golden(tmp_path: Path) -> None:
    rendered = render_markdown(_build_golden_model(tmp_path))
    golden = _GOLDEN.read_text(encoding="utf-8")
    assert rendered == golden, (
        "rendered dictionary diverged from the golden; if intentional, "
        "regenerate tests/datadict/golden/saas_dictionary.md"
    )


def test_model_json_matches_committed_fixture(tmp_path: Path) -> None:
    """The committed wire-shape fixture equals the live aggregator output.

    This is the cross-language bridge: the same JSON drives the dashboard's
    Data Dictionary surface, `GET /api/dict`, and the TypeScript serializer's
    byte-for-byte parity test against `saas_dictionary.md`. If this fails,
    regenerate `tests/datadict/golden/saas_dictionary.model.json` from this
    exact pipeline alongside the markdown golden.
    """
    payload = dictionary_model_to_json(_build_golden_model(tmp_path))
    committed = json.loads(_GOLDEN_MODEL_JSON.read_text(encoding="utf-8"))
    assert payload == committed, (
        "data-dictionary wire shape diverged from the committed JSON fixture; "
        "if intentional, regenerate tests/datadict/golden/saas_dictionary.model.json"
    )


def test_golden_ends_with_single_trailing_newline() -> None:
    golden = _GOLDEN.read_text(encoding="utf-8")
    assert golden.endswith("\n")
    assert not golden.endswith("\n\n")


def test_golden_has_exactly_one_h1() -> None:
    golden = _GOLDEN.read_text(encoding="utf-8")
    assert [line for line in golden.splitlines() if line.startswith("# ")] == ["# Data dictionary"]
