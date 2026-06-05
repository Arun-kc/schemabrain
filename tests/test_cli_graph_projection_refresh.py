"""The CLI semantic-write commands refresh the v15 graph projection.

Behavioural coverage for the ADR 0010 hook: after `entities apply` /
`joins apply` land rows, `GET /api/graph`'s persisted read-model
(`graph_nodes` / `graph_edges`) reflects them — without a separate
`index` run. A failed apply (nothing landed) leaves the projection
untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from schemabrain.cli import _make_source_id
from schemabrain.cli import main as cli_main
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore

_PG_URL = "postgresql+psycopg://u:p@localhost:5432/db"


def _table(store: SQLiteStore, name: str, source_id: str) -> None:
    store.write_table(
        Table(
            name=name,
            schema_name="public",
            columns=(
                Column(
                    name="id",
                    table_name=name,
                    schema_name="public",
                    data_type="bigint",
                    nullable=False,
                    ordinal_position=1,
                    is_primary_key=True,
                ),
            ),
        ),
        source_connection_id=source_id,
    )


def _entity_yaml(name: str, table: str) -> str:
    return (
        f"version: 1\nname: {name}\ndescription: ''\n"
        f"binding:\n  single_table: public.{table}\nidentity: id\n"
    )


def _join_yaml(name: str, source_entity: str, target_entity: str) -> str:
    return (
        f"version: 1\nname: {name}\ndescription: ''\n"
        f"source_entity: {source_entity}\ntarget_entity: {target_entity}\n"
        f'"on":\n  - source: id\n    target: id\ncardinality: many_to_one\n'
    )


@pytest.fixture
def seeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    """A store with two indexed tables for `_PG_URL`, and the env var set."""
    monkeypatch.setenv("SB_URL", _PG_URL)
    store_path = tmp_path / "store.db"
    source_id = _make_source_id(_PG_URL)
    with SQLiteStore(store_path) as store:
        _table(store, "orders", source_id)
        _table(store, "users", source_id)
    return store_path, source_id


def test_entities_apply_populates_graph_nodes(seeded: tuple[Path, str], tmp_path: Path) -> None:
    store_path, source_id = seeded
    (tmp_path / "order.yaml").write_text(_entity_yaml("order", "orders"))
    (tmp_path / "user.yaml").write_text(_entity_yaml("user", "users"))

    rc = cli_main(
        [
            "entities",
            "apply",
            str(tmp_path / "order.yaml"),
            str(tmp_path / "user.yaml"),
            "--url-env",
            "SB_URL",
            "--store-path",
            str(store_path),
        ]
    )
    assert rc == 0
    with SQLiteStore(store_path) as store:
        names = {n.entity_name for n in store.list_graph_nodes(source_connection_id=source_id)}
    assert names == {"order", "user"}


def test_joins_apply_populates_graph_edges(seeded: tuple[Path, str], tmp_path: Path) -> None:
    store_path, source_id = seeded
    (tmp_path / "order.yaml").write_text(_entity_yaml("order", "orders"))
    (tmp_path / "user.yaml").write_text(_entity_yaml("user", "users"))
    cli_main(
        [
            "entities",
            "apply",
            str(tmp_path / "order.yaml"),
            str(tmp_path / "user.yaml"),
            "--url-env",
            "SB_URL",
            "--store-path",
            str(store_path),
        ]
    )
    (tmp_path / "j.yaml").write_text(_join_yaml("order_to_user", "order", "user"))

    rc = cli_main(
        [
            "joins",
            "apply",
            str(tmp_path / "j.yaml"),
            "--url-env",
            "SB_URL",
            "--store-path",
            str(store_path),
        ]
    )
    assert rc == 0
    with SQLiteStore(store_path) as store:
        edges = store.list_graph_edges(source_connection_id=source_id)
    assert [e.join_name for e in edges] == ["order_to_user"]
    assert edges[0].canonical_path_rank == 1  # the only join is the spine


def test_failed_entities_apply_leaves_projection_untouched(
    seeded: tuple[Path, str], tmp_path: Path
) -> None:
    store_path, source_id = seeded
    # Binds to a table that was never indexed → FK failure → nothing applied.
    (tmp_path / "ghost.yaml").write_text(_entity_yaml("ghost", "not_indexed"))
    rc = cli_main(
        [
            "entities",
            "apply",
            str(tmp_path / "ghost.yaml"),
            "--url-env",
            "SB_URL",
            "--store-path",
            str(store_path),
        ]
    )
    assert rc == 1  # apply failed
    with SQLiteStore(store_path) as store:
        assert store.list_graph_nodes(source_connection_id=source_id) == []


def test_failed_joins_apply_leaves_projection_untouched(
    seeded: tuple[Path, str], tmp_path: Path
) -> None:
    store_path, source_id = seeded
    # A join whose endpoints are not present as entities → FK failure →
    # nothing applied → the projection refresh is skipped (the `if applied:`
    # false branch).
    (tmp_path / "j.yaml").write_text(_join_yaml("ghost_join", "order", "user"))
    rc = cli_main(
        [
            "joins",
            "apply",
            str(tmp_path / "j.yaml"),
            "--url-env",
            "SB_URL",
            "--store-path",
            str(store_path),
        ]
    )
    assert rc == 1  # apply failed (no `order`/`user` entities exist)
    with SQLiteStore(store_path) as store:
        assert store.list_graph_edges(source_connection_id=source_id) == []
