"""Data-dictionary generation: store -> model -> Markdown/HTML.

The `schemabrain docs` CLI surface. Mirrors the `inspect` package's
engine/render split:

  - `model`         — frozen `DictionaryModel` dataclass tree (pure)
  - `aggregator`    — `build_dictionary`: the only store reader
  - `render_common` — shared `render_on_clause` + PII label maps (pure)
  - `render_markdown` / `render_html` — the two output formats (pure)

The public re-exports below are the import surface for `cli._cmd_docs`
and the golden / dogfood tests.
"""

from __future__ import annotations

from schemabrain.datadict.aggregator import build_dictionary
from schemabrain.datadict.model import DictionaryModel
from schemabrain.datadict.render_html import render_html
from schemabrain.datadict.render_markdown import render_markdown

__all__ = [
    "DictionaryModel",
    "build_dictionary",
    "render_html",
    "render_markdown",
]
