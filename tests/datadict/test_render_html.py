"""HTML renderer: well-formedness, escaping, and edge branches.

HTML is the convenience export (not byte-golden — assertions are
structural). Escaping is the load-bearing property: any value that
contains markup (a redacted ON clause, or hostile text in a description)
must render as escaped text, never as live HTML.
"""

from __future__ import annotations

from pathlib import Path

from schemabrain.core.store import SQLiteStore
from schemabrain.datadict.aggregator import build_dictionary
from schemabrain.datadict.demo_store import SOURCE_ID, build_saas_dictionary_store
from schemabrain.datadict.model import (
    DictColumn,
    DictEntity,
    DictionaryModel,
    DictJoin,
    DictSource,
)
from schemabrain.datadict.render_html import render_html


def _saas_html(tmp_path: Path) -> str:
    build_saas_dictionary_store(tmp_path / "dict.db")
    with SQLiteStore(tmp_path / "dict.db") as store:
        model = build_dictionary(store=store, source_connection_id=SOURCE_ID)
    return render_html(model)


def test_html_document_is_wellformed(tmp_path: Path) -> None:
    out = _saas_html(tmp_path)
    assert out.startswith("<!DOCTYPE html>\n")
    assert "<title>schemabrain · data dictionary</title>" in out
    assert "<table>" in out
    assert "password_hash" in out
    # Catastrophic columns get the highlight span.
    assert '<span class="catastrophic">Credential (catastrophic)</span>' in out
    assert out.rstrip("\n").endswith("</html>")


def test_html_carries_brand_identity(tmp_path: Path) -> None:
    from schemabrain.positioning import TAGLINE

    out = _saas_html(tmp_path)
    # Masthead: brain mark + schema/brain wordmark with the brand accent.
    assert '<header class="masthead">' in out
    assert '<span class="wordmark">schema<span class="accent">brain</span></span>' in out
    assert "<svg" in out and 'viewBox="0 0 64 64"' in out  # the inlined brand mark
    # Canonical tagline + a branded footer.
    assert TAGLINE in out
    assert '<footer class="footer">' in out
    # Brand green is defined as a token and applied (wordmark accent, h1 rule).
    assert "--brand: #3ecf8e" in out


def test_html_escapes_markup_in_values() -> None:
    # A redacted ON clause carries angle brackets; a hostile description
    # carries a tag. Both must render escaped, never as live markup.
    column = DictColumn(
        name="id",
        data_type="bigint",
        nullable=False,
        is_primary_key=True,
        is_identity=True,
        description="<script>alert(1)</script>",
        pii_sensitivity="public",
        pii_categories=(),
    )
    join = DictJoin(
        name="ab",
        description="",
        source_entity="a",
        target_entity="b",
        on_clause='"a"."<redacted_column>" = "b"."id"',
        cardinality="many_to_one",
        provenance="Operator-authored",
    )
    entity = DictEntity(
        name="a",
        description="x",
        qualified_table="public.a",
        identity="id",
        group="other",
        origin="manual",
        inference_method="manually_authored",
        validation_state="applied",
        columns=(column,),
        joins=(join,),
        metrics=(),
    )
    out = render_html(
        DictionaryModel(schema_version="15", sources=(DictSource("s1", entities=(entity,)),))
    )
    assert "&lt;redacted_column&gt;" in out
    assert "&lt;script&gt;" in out
    assert "<script>" not in out  # the literal tag never appears un-escaped


def test_html_empty_model_is_wellformed() -> None:
    out = render_html(DictionaryModel(schema_version="15", sources=()))
    assert out.startswith("<!DOCTYPE html>\n")
    assert "schema version 15" in out
    assert "<section" not in out
    assert out.rstrip("\n").endswith("</html>")


def test_html_multi_source_and_edge_branches() -> None:
    bare_column = DictColumn(
        name="id",
        data_type="bigint",
        nullable=False,
        is_primary_key=True,
        is_identity=True,
        description=None,
        pii_sensitivity="public",
        pii_categories=(),
    )
    null_card_join = DictJoin(
        name="ab",
        description="",
        source_entity="alpha_e",
        target_entity="beta_e",
        on_clause='"alpha_e"."beta_id" = "beta_e"."id"',
        cardinality=None,
        provenance="Inferred",
    )
    alpha = DictEntity(
        name="alpha_e",
        description="",  # empty description -> em dash branch
        qualified_table="public.alpha",
        identity="id",
        group="other",
        origin="manual",
        inference_method="llm_suggested",
        validation_state="applied",
        columns=(bare_column,),
        joins=(null_card_join,),
        metrics=(),
    )
    beta = DictEntity(
        name="beta_e",
        description="b",
        qualified_table="public.beta",
        identity="id",
        group="other",
        origin="manual",
        inference_method="manually_authored",
        validation_state="applied",
        columns=(bare_column,),
        joins=(),
        metrics=(),
    )
    model = DictionaryModel(
        schema_version="15",
        sources=(
            DictSource("alpha", entities=(alpha,)),
            DictSource("bravo", entities=(beta,)),
        ),
    )
    out = render_html(model)
    # Source dividers at h2; entities nest at h3; section tables at h4 —
    # a correct heading tree (styling is class-driven, not tag-driven).
    assert '<h2 class="source-heading">Source: <code>alpha</code></h2>' in out
    assert '<h2 class="source-heading">Source: <code>bravo</code></h2>' in out
    assert '<h3 class="entity-name">alpha_e</h3>' in out
    assert '<h4 class="section-heading">Columns</h4>' in out
    # entity is nested (h3), never a sibling of its source (h2)
    assert '<h2 class="entity-name">alpha_e' not in out
    # Null cardinality renders an em dash cell.
    assert "<td>—</td>" in out
