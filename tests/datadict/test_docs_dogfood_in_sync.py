"""The committed Mintlify dictionary page stays in sync with the renderer.

`docs/data-dictionary.md` is a dogfooded artifact: SchemaBrain's own
`docs` output for the bundled SaaS demo, committed so the docs site shows
a real dictionary and proves the CLI/export equivalence. This test is the
source of truth for the page's format (frontmatter + the rendered body
with its leading H1 dropped — the frontmatter `title` is the page
heading, matching `docs/architecture.mdx`). Regenerate the committed file
from `build_dogfood_page` whenever the renderer or fixture changes.
"""

from __future__ import annotations

from pathlib import Path

from schemabrain.core.store import SQLiteStore
from schemabrain.datadict.aggregator import build_dictionary
from schemabrain.datadict.demo_store import SOURCE_ID, build_saas_dictionary_store
from schemabrain.datadict.model import DictionaryModel
from schemabrain.datadict.render_markdown import render_markdown

_DOGFOOD = Path(__file__).resolve().parents[2] / "docs" / "data-dictionary.md"

_FRONTMATTER = (
    "---\n"
    'title: "Data dictionary"\n'
    'description: "A generated reference for the bundled SaaS demo — every '
    "entity, column, type, PII classification, semantic join, and metric, "
    'produced by the schemabrain docs command."\n'
    "---\n"
)

_BODY_H1_PREFIX = "# Data dictionary\n\n"


def build_dogfood_page(model: DictionaryModel) -> str:
    """Frontmatter + the rendered body with its leading H1 line removed.

    The CLI standalone artifact keeps its `# Data dictionary` H1; the
    Mintlify page drops it because the frontmatter `title` already renders
    as the page heading (single-H1 hierarchy, like architecture.mdx).
    """
    body = render_markdown(model)
    assert body.startswith(_BODY_H1_PREFIX)
    return _FRONTMATTER + "\n" + body[len(_BODY_H1_PREFIX) :]


def _rendered_page(tmp_path: Path) -> str:
    build_saas_dictionary_store(tmp_path / "dict.db")
    with SQLiteStore(tmp_path / "dict.db") as store:
        model = build_dictionary(store=store, source_connection_id=SOURCE_ID)
    return build_dogfood_page(model)


def test_committed_dictionary_matches_render(tmp_path: Path) -> None:
    assert _DOGFOOD.exists(), "docs/data-dictionary.md is missing; regenerate it"
    assert _DOGFOOD.read_text(encoding="utf-8") == _rendered_page(tmp_path), (
        "docs/data-dictionary.md is stale; regenerate from build_dogfood_page"
    )


def test_dogfood_has_frontmatter_and_no_duplicate_h1() -> None:
    text = _DOGFOOD.read_text(encoding="utf-8")
    assert text.startswith('---\ntitle: "Data dictionary"\n')
    # Frontmatter title is the heading — no body-level "# Data dictionary".
    assert "\n# Data dictionary" not in text
    assert "\n# " not in text  # no body H1 at all (sections start at ##)


def test_dogfood_is_mintlify_safe() -> None:
    text = _DOGFOOD.read_text(encoding="utf-8")
    # No unescaped dollar (MDX inline-math) and no stray angle brackets.
    body = text.split("---\n", 2)[-1]
    assert "$" not in body
    assert "<" not in body and ">" not in body
