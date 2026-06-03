"""Renderer edge branches the single-source golden does not exercise.

The golden (`test_dictionary_golden`) covers the rich common case. These
hand-built models cover the remaining branches: the empty model, the
multi-source divider, an empty entity/join description ("—"), and a null
cardinality.
"""

from __future__ import annotations

from schemabrain.datadict.model import (
    DictColumn,
    DictEntity,
    DictionaryModel,
    DictJoin,
    DictSource,
)
from schemabrain.datadict.render_markdown import render_markdown


def _column() -> DictColumn:
    return DictColumn(
        name="id",
        data_type="bigint",
        nullable=False,
        is_primary_key=True,
        is_identity=True,
        description=None,
        pii_sensitivity="public",
        pii_categories=(),
    )


def _entity(name: str, *, description: str, joins: tuple[DictJoin, ...] = ()) -> DictEntity:
    return DictEntity(
        name=name,
        description=description,
        qualified_table=f"public.{name}s",
        identity="id",
        group="other",
        origin="manual",
        inference_method="manually_authored",
        validation_state="applied",
        columns=(_column(),),
        joins=joins,
        metrics=(),
    )


def test_empty_model_renders_header_only() -> None:
    out = render_markdown(DictionaryModel(schema_version="15", sources=()))
    assert out.startswith("# Data dictionary\n")
    assert "schema version 15" in out
    assert "## " not in out  # no entity or source sections
    assert out.endswith("\n") and not out.endswith("\n\n")


def test_empty_entity_description_renders_em_dash() -> None:
    model = DictionaryModel(
        schema_version="15",
        sources=(DictSource("s1", entities=(_entity("widget", description=""),)),),
    )
    out = render_markdown(model)
    # The blank description line collapses to a literal em dash.
    assert "\n## widget\n\n—\n" in out


def test_null_cardinality_renders_em_dash() -> None:
    join = DictJoin(
        name="widget_gadget",
        description="",
        source_entity="widget",
        target_entity="gadget",
        on_clause='"widget"."gadget_id" = "gadget"."id"',
        cardinality=None,
        provenance="Inferred",
    )
    model = DictionaryModel(
        schema_version="15",
        sources=(DictSource("s1", entities=(_entity("widget", description="x", joins=(join,)),)),),
    )
    out = render_markdown(model)
    # Cardinality cell and the empty join description both render "—".
    assert '| `widget_gadget` | `"widget"."gadget_id" = "gadget"."id"` | — | Inferred | — |' in out


def test_free_text_cell_escapes_pipe_and_dollar() -> None:
    # A hand-authored description with a pipe must escape to `\|` (or it
    # breaks the Markdown table) and a literal `$` to `\$` (MDX math).
    # The bundled demo has no such description, so the golden never
    # exercises this Mintlify-safety contract — pin it explicitly.
    col = DictColumn(
        name="status",
        data_type="text",
        nullable=False,
        is_primary_key=False,
        is_identity=False,
        description="active | inactive ($5 tier)",
        pii_sensitivity="public",
        pii_categories=(),
    )
    entity = DictEntity(
        name="widget",
        description="d",
        qualified_table="public.widgets",
        identity="id",
        group="other",
        origin="manual",
        inference_method="manually_authored",
        validation_state="applied",
        columns=(col,),
        joins=(),
        metrics=(),
    )
    out = render_markdown(
        DictionaryModel(schema_version="15", sources=(DictSource("s1", entities=(entity,)),))
    )
    assert "active \\| inactive (\\$5 tier)" in out
    assert "active | inactive" not in out  # a raw pipe never survives in a cell


def test_multi_source_nests_entities_under_source_dividers() -> None:
    model = DictionaryModel(
        schema_version="15",
        sources=(
            DictSource("alpha", entities=(_entity("widget", description="a"),)),
            DictSource("bravo", entities=(_entity("gadget", description="b"),)),
        ),
    )
    out = render_markdown(model)
    # Source dividers at ##, entities nest at ###, section tables at ####
    # — a correct heading tree (no flat collision).
    assert "## Source: `alpha`" in out
    assert "## Source: `bravo`" in out
    assert "\n### widget\n" in out
    assert "\n### gadget\n" in out
    assert "\n#### Columns\n" in out
    assert "\n## widget\n" not in out  # entity is NOT a sibling of its source
