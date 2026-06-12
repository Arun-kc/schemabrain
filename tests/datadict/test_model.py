"""The dictionary model is an immutable value tree."""

from __future__ import annotations

import dataclasses

import pytest

from schemabrain.datadict.model import (
    DictColumn,
    DictEntity,
    DictionaryModel,
    DictJoin,
    DictMetric,
    DictSource,
)


def _sample_model() -> DictionaryModel:
    col = DictColumn(
        name="email",
        data_type="text",
        nullable=False,
        is_primary_key=False,
        is_identity=False,
        description=None,
        pii_sensitivity="pii",
        pii_categories=("contact",),
    )
    join = DictJoin(
        name="workspace_users",
        description="",
        source_entity="workspace",
        target_entity="user",
        on_clause='"workspace"."id" = "user"."workspace_id"',
        cardinality="one_to_many",
        provenance="Operator-authored",
    )
    metric = DictMetric(
        name="user_count",
        description="Count of users.",
        agg="count",
        measure="id",
        measure_is_expression=False,
        time_dimension=None,
        time_grains=(),
    )
    entity = DictEntity(
        name="user",
        description="A workspace member.",
        qualified_table="public.users",
        identity="id",
        group="other",
        origin="manual",
        inference_method="manually_authored",
        validation_state="applied",
        columns=(col,),
        joins=(join,),
        metrics=(metric,),
    )
    return DictionaryModel(
        schema_version="15",
        sources=(DictSource(source_connection_id="abc123", entities=(entity,)),),
    )


def test_model_is_frozen() -> None:
    model = _sample_model()
    with pytest.raises(dataclasses.FrozenInstanceError):
        model.schema_version = "16"  # type: ignore[misc]


def test_nested_nodes_are_frozen() -> None:
    entity = _sample_model().sources[0].entities[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        entity.columns[0].name = "renamed"  # type: ignore[misc]


def test_model_round_trips_field_values() -> None:
    model = _sample_model()
    entity = model.sources[0].entities[0]
    assert entity.qualified_table == "public.users"
    assert entity.columns[0].pii_categories == ("contact",)
    assert entity.joins[0].on_clause == '"workspace"."id" = "user"."workspace_id"'
    assert entity.metrics[0].agg == "count"
