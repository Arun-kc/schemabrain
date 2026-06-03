"""`build_dictionary` reads the store into a faithful, deterministic model.

Covers the anchoring algorithm (joins on both endpoints, metrics by
entity), the quoted ON-clause grammar, catastrophic-FK redaction, and the
two reachable edge branches (entity bound to an unindexed table; empty
store).
"""

from __future__ import annotations

from pathlib import Path

from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.join import CanonicalJoin, JoinColumnPair
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SCHEMA_VERSION, SQLiteStore
from schemabrain.datadict.aggregator import _provenance, build_dictionary
from schemabrain.datadict.demo_store import SOURCE_ID, build_saas_dictionary_store
from schemabrain.datadict.model import DictEntity, DictionaryModel


def _table(name: str, columns: tuple[tuple[str, bool], ...]) -> Table:
    """Build a minimal `public.<name>` table from (column_name, is_pk) pairs."""
    return Table(
        name=name,
        schema_name="public",
        columns=tuple(
            Column(
                name=col_name,
                table_name=name,
                schema_name="public",
                data_type="bigint",
                nullable=False,
                ordinal_position=index + 1,
                is_primary_key=is_pk,
            )
            for index, (col_name, is_pk) in enumerate(columns)
        ),
        foreign_keys=(),
    )


def _saas_model(tmp_path: Path) -> DictionaryModel:
    build_saas_dictionary_store(tmp_path / "dict.db")
    with SQLiteStore(tmp_path / "dict.db") as store:
        return build_dictionary(store=store, source_connection_id=SOURCE_ID)


def _entity(model: DictionaryModel, name: str) -> DictEntity:
    return next(e for e in model.sources[0].entities if e.name == name)


def test_build_dictionary_single_source(tmp_path: Path) -> None:
    model = _saas_model(tmp_path)
    assert model.schema_version == SCHEMA_VERSION == "15"
    assert len(model.sources) == 1
    assert model.sources[0].source_connection_id == SOURCE_ID
    assert len(model.sources[0].entities) == 12
    # Entities are name-ordered.
    names = [e.name for e in model.sources[0].entities]
    assert names == sorted(names)


def test_user_columns_carry_type_pii_and_identity(tmp_path: Path) -> None:
    user = _entity(_saas_model(tmp_path), "user")
    by_name = {c.name: c for c in user.columns}
    # Catastrophic credential column is surfaced WITH its classification
    # (parity with `inspect`; the dictionary's whole point).
    password = by_name["password_hash"]
    assert password.pii_sensitivity == "pii"
    assert "credential" in password.pii_categories
    # Identity column flagged + PK; ordinal order preserved.
    assert by_name["id"].is_identity is True
    assert by_name["id"].is_primary_key is True
    assert by_name["email"].is_identity is False
    assert user.columns[0].name == "id"


def test_joins_anchor_on_both_endpoints(tmp_path: Path) -> None:
    model = _saas_model(tmp_path)
    user_joins = {j.name for j in _entity(model, "user").joins}
    workspace_joins = {j.name for j in _entity(model, "workspace").joins}
    session_joins = {j.name for j in _entity(model, "session").joins}
    # workspace_users: source=user, target=workspace -> visible on both.
    assert "workspace_users" in user_joins
    assert "workspace_users" in workspace_joins
    # user_sessions: source=session, target=user -> visible on both.
    assert "user_sessions" in user_joins
    assert "user_sessions" in session_joins
    # Joins are name-ordered within an entity.
    user_join_names = [j.name for j in _entity(model, "user").joins]
    assert user_join_names == sorted(user_join_names)


def test_on_clause_uses_quoted_entity_alias_grammar(tmp_path: Path) -> None:
    user = _entity(_saas_model(tmp_path), "user")
    sessions = next(j for j in user.joins if j.name == "user_sessions")
    # source_entity=session, target_entity=user, on=(user_id -> id).
    assert sessions.on_clause == '"session"."user_id" = "user"."id"'
    assert sessions.cardinality == "many_to_one"
    assert sessions.provenance == "Operator-authored"  # manually_authored


def test_metrics_anchor_by_entity(tmp_path: Path) -> None:
    model = _saas_model(tmp_path)
    assert {m.name for m in _entity(model, "user").metrics} == {"user_count"}
    assert {m.name for m in _entity(model, "subscription").metrics} == {"active_subscriptions"}
    user_count = _entity(model, "user").metrics[0]
    assert user_count.agg == "count"
    assert user_count.measure == "id"
    assert user_count.measure_is_expression is False


def test_expression_metric_is_flagged(tmp_path: Path) -> None:
    item = _entity(_saas_model(tmp_path), "subscription_item")
    revenue = next(m for m in item.metrics if m.name == "total_revenue_real")
    assert revenue.measure_is_expression is True
    assert revenue.measure == "unit_price_cents * seats"
    assert revenue.time_dimension is None
    assert revenue.time_grains == ()


def test_on_clause_redacts_catastrophic_fk_column(tmp_path: Path) -> None:
    # No bundled join uses a catastrophic column (they are all FK ids),
    # so construct a synthetic join whose source column is credential-tagged
    # and assert the rendered ON clause masks it.
    db = tmp_path / "syn.db"
    with SQLiteStore(db) as store:
        # Tables first — `entities` has an FK onto `tables`.
        store.write_table(
            _table("a", (("id", True), ("secret_fk", False))), source_connection_id="syn"
        )
        store.write_table(_table("b", (("id", True),)), source_connection_id="syn")
        store.write_entity(
            Entity(name="a", description="", binding=SingleTableBinding("public.a"), identity="id"),
            source_connection_id="syn",
        )
        store.write_entity(
            Entity(name="b", description="", binding=SingleTableBinding("public.b"), identity="id"),
            source_connection_id="syn",
        )
        store.write_column_pii_tags(
            source_connection_id="syn",
            qualified_table="public.a",
            tags={"secret_fk": ("pii", frozenset({"credential"}))},
        )
        store.write_canonical_join(
            CanonicalJoin(
                name="ab",
                description="",
                source_entity="a",
                target_entity="b",
                on=(JoinColumnPair(source_column="secret_fk", target_column="id"),),
                cardinality="many_to_one",
            ),
            source_connection_id="syn",
        )
        model = build_dictionary(store=store, source_connection_id="syn")
    join = next(j for j in _entity(model, "a").joins if j.name == "ab")
    assert join.on_clause == '"a"."<redacted_column>" = "b"."id"'


def test_empty_store_yields_empty_model(tmp_path: Path) -> None:
    with SQLiteStore(tmp_path / "empty.db") as store:
        model = build_dictionary(store=store, source_connection_id=None)
    assert model.schema_version == "15"
    assert model.sources == ()


def test_walk_all_sources_when_source_is_none(tmp_path: Path) -> None:
    build_saas_dictionary_store(tmp_path / "dict.db")
    with SQLiteStore(tmp_path / "dict.db") as store:
        model = build_dictionary(store=store, source_connection_id=None)
    assert [s.source_connection_id for s in model.sources] == [SOURCE_ID]


def test_provenance_maps_known_and_falls_back_on_unknown() -> None:
    # Each of the five InferenceMethod values maps to a human label...
    assert _provenance("manually_authored") == "Operator-authored"
    assert _provenance("fk_constraint") == "FK-derived"
    assert _provenance("llm_suggested") == "Inferred"
    assert _provenance("observed_in_query_log") == "Log-mined"
    assert _provenance("dbt_import") == "Imported"
    # ...and an unknown value falls through verbatim (the future-taxonomy
    # safety valve; can't reach here today since inference_method is a
    # validated Literal, but the fallback is asserted like category_label's).
    assert _provenance("a_future_method") == "a_future_method"
