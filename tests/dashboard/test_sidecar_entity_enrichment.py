"""Sidecar read-extension tests: enriched drilldown + entities rollup.

Pins the fields the entity drilldown sheet and the entities index
consume:

  - columns gain `data_type`, `description` (the free-text "meaning",
    null when un-enriched), and `similar` (column->column nearest
    neighbours, structured — no free-text reason that could leak);
  - joins gain a server-rendered, catastrophic-redacted `on_clause`,
    `cardinality`, `origin`, `inference_method`, and a derived
    `is_log_mined` flag (client-side ON assembly is gone);
  - the drilldown carries a `referenced_by` {metrics, joins} rollup;
  - `/api/entities` items carry `group`, `pii_level`, `metrics_count`,
    `joins_count`; the response carries a `summary` strip.

`rows` and `confidence` are deliberately ABSENT from the rollup: the
v15 columns that back them (`tables.estimated_row_count`,
`entities.bind_confidence`) have no writer yet, so surfacing them would
be permanent null noise. They arrive with their writer PR.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from schemabrain.dashboard.sidecar import is_ui_available

pytestmark = pytest.mark.skipif(
    not is_ui_available(),
    reason="`schemabrain[ui]` extra not installed",
)

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from schemabrain.core.store import SQLiteStore

_SRC = "test-source"
_DIM = 4


def _unit(idx: int) -> tuple[float, ...]:
    return tuple(1.0 if j == idx else 0.0 for j in range(_DIM))


def _col(table: str, name: str, ordinal: int, data_type: str = "text"):
    from schemabrain.core.models import Column

    return Column(
        schema_name="public",
        table_name=table,
        name=name,
        data_type=data_type,
        nullable=False,
        ordinal_position=ordinal,
    )


def _write_table(store: SQLiteStore, name: str, cols) -> None:
    from schemabrain.core.models import Table

    store.write_table(
        Table(schema_name="public", name=name, columns=cols),
        source_connection_id=_SRC,
    )


def _write_entity(
    store: SQLiteStore,
    name: str,
    table: str,
    *,
    identity: str = "id",
    group: str = "other",
    description: str = "",
) -> None:
    from schemabrain.core.entity import Entity, SingleTableBinding

    store.write_entity(
        Entity(
            name=name,
            description=description,
            binding=SingleTableBinding(qualified_table=f"public.{table}"),
            identity=identity,
            group=group,
        ),
        source_connection_id=_SRC,
    )


def _tag(store: SQLiteStore, table: str, tags: dict) -> None:
    from schemabrain.pii.categories import ColumnPiiTag

    store.write_column_pii_tags(
        source_connection_id=_SRC,
        qualified_table=f"public.{table}",
        tags={col: ColumnPiiTag(t) for col, t in tags.items()},
    )


def _describe(store: SQLiteStore, table: str, texts: dict[str, str]) -> None:
    from schemabrain.core.description import ColumnDescription

    store.write_table_descriptions(
        "public",
        table,
        source_connection_id=_SRC,
        descriptions={
            col: ColumnDescription(
                text=text,
                model="test-model",
                prompt_version="v1",
                input_tokens=0,
                cached_input_tokens=0,
                output_tokens=0,
                cost_usd=0.0,
            )
            for col, text in texts.items()
        },
    )


def _embed(store: SQLiteStore, table: str, vecs: dict[str, tuple[float, ...]]) -> None:
    from schemabrain.core.embedding import ColumnEmbedding

    store.write_table_embeddings(
        "public",
        table,
        source_connection_id=_SRC,
        embeddings={
            col: ColumnEmbedding(vector=v, model="test-emb", dimension=len(v))
            for col, v in vecs.items()
        },
    )


def _join(
    store: SQLiteStore,
    name: str,
    src_entity: str,
    tgt_entity: str,
    pairs: tuple[tuple[str, str], ...],
    *,
    cardinality=None,
    inference_method: str = "manually_authored",
) -> None:
    from schemabrain.core.join import CanonicalJoin, JoinColumnPair

    store.write_canonical_join(
        CanonicalJoin(
            name=name,
            description="",
            source_entity=src_entity,
            target_entity=tgt_entity,
            on=tuple(JoinColumnPair(source_column=s, target_column=t) for s, t in pairs),
            cardinality=cardinality,
            inference_method=inference_method,
        ),
        source_connection_id=_SRC,
    )


def _metric(store: SQLiteStore, name: str, entity: str, column: str) -> None:
    from schemabrain.core.metric import Metric, MetricMeasure

    store.write_metric(
        Metric(
            name=name,
            description="",
            entity=entity,
            measure=MetricMeasure(agg="count", column=column, expression=None),
            time_dimension=None,
            time_grains=(),
        ),
        source_connection_id=_SRC,
    )


def _seed_two_entity_store(store: SQLiteStore) -> None:
    """users <- orders, with PII, descriptions, embeddings, a log-mined join."""
    _write_table(
        store,
        "users",
        (
            _col("users", "id", 1, "integer"),
            _col("users", "email", 2),
            _col("users", "password_hash", 3),
        ),
    )
    _write_table(
        store,
        "orders",
        (
            _col("orders", "id", 1, "integer"),
            _col("orders", "user_id", 2, "integer"),
            _col("orders", "total", 3, "numeric"),
        ),
    )
    _write_entity(store, "user", "users", group="identity", description="A registered account.")
    _write_entity(store, "order", "orders", group="activity")
    _tag(
        store,
        "users",
        {
            "email": ("pii", frozenset({"contact"})),
            "password_hash": ("pii", frozenset({"credential"})),  # catastrophic
        },
    )
    _describe(store, "users", {"email": "The account holder's email address."})
    # email ~ orders.user_id (same axis); orders.total is orthogonal.
    _embed(store, "users", {"email": _unit(0)})
    _embed(store, "orders", {"user_id": _unit(0), "total": _unit(1)})
    # Log-mined many-to-one join order -> user on user_id = id.
    _join(
        store,
        "order_user",
        "order",
        "user",
        (("user_id", "id"),),
        cardinality="many_to_one",
        inference_method="observed_in_query_log",
    )
    _metric(store, "order_count", "order", "id")


class TestDrilldownColumns:
    def test_columns_carry_data_type_and_description(
        self, store_path: Path, client: TestClient
    ) -> None:
        from schemabrain.core.store import SQLiteStore

        with SQLiteStore(store_path) as store:
            _seed_two_entity_store(store)
        payload = client.get(f"/api/entities/user/columns?source_connection_id={_SRC}").json()
        cols = {c["name"]: c for c in payload["columns"]}
        assert cols["id"]["data_type"] == "integer"
        assert cols["email"]["data_type"] == "text"
        # The free-text description IS the column's human "meaning".
        assert cols["email"]["description"] == "The account holder's email address."
        # Un-enriched column → null description, not "".
        assert cols["password_hash"]["description"] is None

    def test_columns_carry_similar_nearest_neighbours(
        self, store_path: Path, client: TestClient
    ) -> None:
        from schemabrain.core.store import SQLiteStore

        with SQLiteStore(store_path) as store:
            _seed_two_entity_store(store)
        payload = client.get(f"/api/entities/user/columns?source_connection_id={_SRC}").json()
        cols = {c["name"]: c for c in payload["columns"]}
        similar = cols["email"]["similar"]
        # Structured entries only — no free-text reason field.
        assert similar[0]["table"] == "orders"
        assert similar[0]["column"] == "user_id"
        assert similar[0]["score"] == pytest.approx(1.0, abs=1e-5)
        assert set(similar[0].keys()) == {"schema", "table", "column", "score"}
        # Same-table column with no embedding has no similar neighbours.
        assert cols["password_hash"]["similar"] == []

    def test_similar_empty_when_no_embeddings(self, store_path: Path, client: TestClient) -> None:
        from schemabrain.core.store import SQLiteStore

        with SQLiteStore(store_path) as store:
            _write_table(store, "users", (_col("users", "id", 1, "integer"),))
            _write_entity(store, "user", "users")
        payload = client.get(f"/api/entities/user/columns?source_connection_id={_SRC}").json()
        assert payload["columns"][0]["similar"] == []


class TestDrilldownJoins:
    def test_join_carries_card_provenance_and_log_mined_flag(
        self, store_path: Path, client: TestClient
    ) -> None:
        from schemabrain.core.store import SQLiteStore

        with SQLiteStore(store_path) as store:
            _seed_two_entity_store(store)
        payload = client.get(f"/api/entities/user/columns?source_connection_id={_SRC}").json()
        join = payload["joins"][0]
        assert join["name"] == "order_user"
        assert join["cardinality"] == "many_to_one"
        assert join["inference_method"] == "observed_in_query_log"
        assert join["is_log_mined"] is True
        assert join["origin"] == "manual"

    def test_join_on_clause_is_server_rendered_and_quoted(
        self, store_path: Path, client: TestClient
    ) -> None:
        from schemabrain.core.store import SQLiteStore

        with SQLiteStore(store_path) as store:
            _seed_two_entity_store(store)
        payload = client.get(f"/api/entities/user/columns?source_connection_id={_SRC}").json()
        join = payload["joins"][0]
        # Quoted, entity-name aliases, byte-identical to the dict surface.
        assert join["on_clause"] == '"order"."user_id" = "user"."id"'
        # Client-side ON assembly is gone — no raw column pairs.
        assert "on" not in join

    def test_join_on_clause_redacts_catastrophic_fk_name(
        self, store_path: Path, client: TestClient
    ) -> None:
        # A join whose ON column is itself catastrophic must mask the name
        # in the rendered clause, matching the data-dictionary policy.
        from schemabrain.core.store import SQLiteStore

        with SQLiteStore(store_path) as store:
            _write_table(
                store,
                "users",
                (_col("users", "id", 1, "integer"), _col("users", "secret", 2)),
            )
            _write_table(
                store,
                "vault",
                (_col("vault", "id", 1, "integer"), _col("vault", "cred_ref", 2)),
            )
            _write_entity(store, "user", "users")
            _write_entity(store, "vault", "vault")
            _tag(store, "users", {"secret": ("pii", frozenset({"credential"}))})
            _join(store, "user_vault", "user", "vault", (("secret", "cred_ref"),))
        payload = client.get(f"/api/entities/user/columns?source_connection_id={_SRC}").json()
        join = payload["joins"][0]
        assert join["on_clause"] == '"user"."<redacted_column>" = "vault"."cred_ref"'


class TestDrilldownReferencedBy:
    def test_referenced_by_counts_match_served_lists(
        self, store_path: Path, client: TestClient
    ) -> None:
        from schemabrain.core.store import SQLiteStore

        with SQLiteStore(store_path) as store:
            _seed_two_entity_store(store)
        payload = client.get(f"/api/entities/order/columns?source_connection_id={_SRC}").json()
        assert payload["referenced_by"]["metrics"] == len(payload["metrics"])
        assert payload["referenced_by"]["joins"] == len(payload["joins"])
        assert payload["referenced_by"]["metrics"] == 1
        assert payload["referenced_by"]["joins"] == 1

    def test_drilldown_excludes_other_entities_metrics_and_joins(
        self, store_path: Path, client: TestClient
    ) -> None:
        # Discriminative: a metric + join anchored on a THIRD entity must
        # not bleed into `order`'s drilldown. Without a third entity (the
        # 2-entity seed has one metric + one join total), a dropped
        # `m.entity == name` / endpoint filter would pass unnoticed.
        from schemabrain.core.store import SQLiteStore

        with SQLiteStore(store_path) as store:
            _seed_two_entity_store(store)
            _write_table(
                store,
                "payments",
                (_col("payments", "id", 1, "integer"), _col("payments", "user_id", 2, "integer")),
            )
            _write_entity(store, "payment", "payments", group="billing")
            _metric(store, "payment_count", "payment", "id")
            _join(store, "payment_user", "payment", "user", (("user_id", "id"),))

        payload = client.get(f"/api/entities/order/columns?source_connection_id={_SRC}").json()
        # order anchors exactly its own metric + its own join — the
        # payment-scoped metric/join (which expose measure columns) stay out.
        assert [m["name"] for m in payload["metrics"]] == ["order_count"]
        assert [j["name"] for j in payload["joins"]] == ["order_user"]
        assert payload["referenced_by"] == {"metrics": 1, "joins": 1}


class TestDrilldownEntityHeader:
    def test_entity_carries_group_and_description(
        self, store_path: Path, client: TestClient
    ) -> None:
        from schemabrain.core.store import SQLiteStore

        with SQLiteStore(store_path) as store:
            _seed_two_entity_store(store)
        payload = client.get(f"/api/entities/user/columns?source_connection_id={_SRC}").json()
        assert payload["entity"]["group"] == "identity"
        assert payload["entity"]["description"] == "A registered account."


class TestEntitiesListRollup:
    def test_items_carry_group_pii_level_and_counts(
        self, store_path: Path, client: TestClient
    ) -> None:
        from schemabrain.core.store import SQLiteStore

        with SQLiteStore(store_path) as store:
            _seed_two_entity_store(store)
        payload = client.get(f"/api/entities?source_connection_id={_SRC}").json()
        items = {i["name"]: i for i in payload["items"]}
        user = items["user"]
        assert user["group"] == "identity"
        # users has a catastrophic credential column → pii_level "catastrophic".
        assert user["pii_level"] == "catastrophic"
        assert user["metrics_count"] == 0
        assert user["joins_count"] == 1
        order = items["order"]
        assert order["group"] == "activity"
        assert order["pii_level"] == "none"
        assert order["metrics_count"] == 1
        assert order["joins_count"] == 1

    def test_response_carries_summary_strip(self, store_path: Path, client: TestClient) -> None:
        from schemabrain.core.store import SQLiteStore

        with SQLiteStore(store_path) as store:
            _seed_two_entity_store(store)
        payload = client.get(f"/api/entities?source_connection_id={_SRC}").json()
        summary = payload["summary"]
        assert summary["entities"] == 2
        assert summary["metrics"] == 1
        assert summary["joins"] == 1
        # One of two entities carries a catastrophic column.
        assert summary["catastrophic_entities"] == 1

    def test_rows_and_confidence_absent_until_writer_lands(
        self, store_path: Path, client: TestClient
    ) -> None:
        # The v15 columns backing these have no writer; surfacing them
        # would be permanent null noise. They arrive with their writer PR.
        from schemabrain.core.store import SQLiteStore

        with SQLiteStore(store_path) as store:
            _seed_two_entity_store(store)
        payload = client.get(f"/api/entities?source_connection_id={_SRC}").json()
        item = payload["items"][0]
        assert "rows" not in item
        assert "confidence" not in item


class TestPiiLevelLadder:
    """The severity ladder `catastrophic > pii > confidential > internal >
    none`. The route tests cover the catastrophic + none ends; this pins
    the middle rungs the seeded store does not exercise."""

    @staticmethod
    def _level(*tags: tuple[str, frozenset[str]]) -> str:
        from schemabrain.dashboard.sidecar import _pii_level_from_tags
        from schemabrain.pii.categories import CATASTROPHIC_LEAK_CATEGORIES

        return _pii_level_from_tags(tags, catastrophic_categories=CATASTROPHIC_LEAK_CATEGORIES)

    def test_catastrophic_outranks_everything(self) -> None:
        # A catastrophic category wins even alongside lower sensitivities.
        assert (
            self._level(
                ("internal", frozenset()),
                ("pii", frozenset({"credential"})),
            )
            == "catastrophic"
        )

    def test_pii_without_catastrophic(self) -> None:
        assert self._level(("pii", frozenset({"contact"})), ("public", frozenset())) == "pii"

    def test_confidential_is_next(self) -> None:
        assert self._level(("confidential", frozenset()), ("internal", frozenset())) == (
            "confidential"
        )

    def test_internal_is_last_non_none(self) -> None:
        assert self._level(("internal", frozenset()), ("public", frozenset())) == "internal"

    def test_all_public_is_none(self) -> None:
        assert self._level(("public", frozenset()), ("public", frozenset())) == "none"

    def test_empty_is_none(self) -> None:
        assert self._level() == "none"
