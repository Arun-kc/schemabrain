"""End-to-end test: bundled ecommerce entity YAMLs round-trip cleanly.

Threads the full entity-foundation surface in one test:

  YAML files (bundled)
    -> `entities apply` CLI
    -> store v8 entities table
    -> MCP `list_entities` envelope
    -> MCP `describe_entity` envelope

The three bundled YAMLs (`customer`, `order`, `product`) double as
the demo's starter pack — agents seeing this for the first time can
copy/edit them as templates rather than authoring from scratch. This
test pins their shape so a refactor can't silently break the demo.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from schemabrain.cli import _make_source_id, main
from schemabrain.core.models import Column, ForeignKey, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.mcp import build_server
from schemabrain.mcp.envelope import ToolResponse

_BUNDLED_ENTITY_DIR = (
    Path(__file__).resolve().parent.parent
    / "schemabrain"
    / "eval"
    / "fixtures"
    / "entities"
    / "ecommerce"
)
_TEST_URL = "postgresql+psycopg://user:pw@localhost:5432/db"


class _StubEmbedder:
    model_name = "test"
    dimension = 4

    def embed(self, text: str) -> tuple[float, ...]:
        del text
        return (1.0, 0.0, 0.0, 0.0)


def _pk_column(name: str, table: str) -> Column:
    return Column(
        name=name,
        table_name=table,
        schema_name="public",
        data_type="bigint",
        nullable=False,
        ordinal_position=1,
        is_primary_key=True,
    )


def _ecommerce_tables() -> list[Table]:
    """The three tables our bundled entity YAMLs bind to.

    Minimal shape — id PK + one or two attribute columns. Mirrors the
    structural shape that `schemabrain index` would produce against
    `schemabrain/eval/fixtures/ecommerce.sql`, but inlined here so the
    test doesn't need a Postgres container.
    """
    users = Table(
        name="users",
        schema_name="public",
        columns=(
            _pk_column("id", "users"),
            Column(
                name="email",
                table_name="users",
                schema_name="public",
                data_type="text",
                nullable=False,
                ordinal_position=2,
            ),
        ),
    )
    orders = Table(
        name="orders",
        schema_name="public",
        columns=(
            _pk_column("id", "orders"),
            Column(
                name="user_id",
                table_name="orders",
                schema_name="public",
                data_type="bigint",
                nullable=False,
                ordinal_position=2,
            ),
        ),
        foreign_keys=(
            ForeignKey(
                name="orders_user_id_fkey",
                source_columns=("user_id",),
                target_schema="public",
                target_table="users",
                target_columns=("id",),
            ),
        ),
    )
    products = Table(
        name="products",
        schema_name="public",
        columns=(
            _pk_column("id", "products"),
            Column(
                name="sku",
                table_name="products",
                schema_name="public",
                data_type="text",
                nullable=False,
                ordinal_position=2,
            ),
        ),
    )
    return [users, orders, products]


# ----- bundled file shape ----------------------------------------------------


def test_bundled_yaml_files_exist() -> None:
    """The three demo entity YAMLs ship with the package."""
    assert (_BUNDLED_ENTITY_DIR / "customer.yaml").is_file()
    assert (_BUNDLED_ENTITY_DIR / "order.yaml").is_file()
    assert (_BUNDLED_ENTITY_DIR / "product.yaml").is_file()


# ----- full CLI -> store -> MCP round-trip -----------------------------------


def test_apply_all_three_fixtures_then_list_and_describe(tmp_path: Path) -> None:
    """End-to-end: apply CLI loads each fixture, MCP surfaces them.

    Once a user runs `schemabrain index` against their database,
    applying the bundled ecommerce YAMLs is the fastest path to
    seeing entities in the agent's tool surface.
    """
    store_path = tmp_path / "store.db"
    source_id = _make_source_id(_TEST_URL)

    # Seed the three bound tables directly (cheaper than running
    # `schemabrain index` against testcontainers Postgres).
    with SQLiteStore(store_path) as store:
        for table in _ecommerce_tables():
            store.write_table(table, source_connection_id=source_id)

    # Apply all three fixture YAMLs through the CLI surface.
    for fixture_name in ("customer.yaml", "order.yaml", "product.yaml"):
        exit_code = main(
            [
                "entities",
                "apply",
                str(_BUNDLED_ENTITY_DIR / fixture_name),
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
            ]
        )
        assert exit_code == 0, f"failed to apply {fixture_name}"

    # Round-trip via the MCP `list_entities` envelope.
    store = SQLiteStore(store_path)
    try:
        server = build_server(
            store=store,
            source_connection_id=source_id,
            embedder=_StubEmbedder(),
        )
        _content, list_structured = asyncio.run(server.call_tool("list_entities", {}))
        list_envelope = ToolResponse.model_validate(list_structured)
        assert list_envelope.status == "success"
        assert list_envelope.data is not None
        names = sorted(e["name"] for e in list_envelope.data)
        assert names == ["customer", "order", "product"]

        # Drill into one — `describe_entity("customer")` must surface
        # the bound table's column list.
        _content, desc_structured = asyncio.run(
            server.call_tool("describe_entity", {"name": "customer"})
        )
        desc_envelope = ToolResponse.model_validate(desc_structured)
        assert desc_envelope.status == "success"
        assert desc_envelope.data is not None
        assert desc_envelope.data["qualified_table"] == "public.users"
        assert desc_envelope.data["identity"] == "id"
        column_names = {c["name"] for c in desc_envelope.data["columns"]}
        assert column_names == {"id", "email"}
        # pii_sensitivity hardcoded to 'public' at this release.
        assert all(c["pii_sensitivity"] == "public" for c in desc_envelope.data["columns"])
    finally:
        store.close()
