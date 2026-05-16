"""Tests for `_serialize_schema` — Table list to LLM-readable text.

The suggest pipeline can't ship the raw `Table` Pydantic models to the
LLM — it needs a compact text rendering the model can reason about
without burning input tokens on JSON noise. This helper produces the
canonical form: one table per block, columns indented, FKs after.

The shape pinned by these tests is intentionally minimal — just
structural facts (name, type, nullable, PK, FK target). Sample rows
and column descriptions are deferred; they multiply input cost without
clear precision gain at v1.
"""

from __future__ import annotations

from schemabrain.core.models import Column, ForeignKey, Table
from schemabrain.entities.suggest import _serialize_schema


def _col(
    name: str,
    table: str,
    *,
    data_type: str = "bigint",
    nullable: bool = True,
    is_pk: bool = False,
    ordinal: int = 1,
    schema: str = "public",
) -> Column:
    return Column(
        name=name,
        table_name=table,
        schema_name=schema,
        data_type=data_type,
        nullable=nullable,
        ordinal_position=ordinal,
        is_primary_key=is_pk,
    )


class TestSerializeSchemaBasics:
    def test_empty_input_returns_empty_string(self) -> None:
        assert _serialize_schema([]) == ""

    def test_single_minimal_table(self) -> None:
        users = Table(
            name="users",
            schema_name="public",
            columns=(_col("id", "users", is_pk=True, nullable=False),),
        )
        rendered = _serialize_schema([users])
        # Block header is `schema.table`, columns indented.
        assert "public.users" in rendered
        assert "id" in rendered

    def test_columns_render_type_and_constraints(self) -> None:
        users = Table(
            name="users",
            schema_name="public",
            columns=(
                _col("id", "users", data_type="bigint", is_pk=True, nullable=False, ordinal=1),
                _col("email", "users", data_type="text", nullable=False, ordinal=2),
                _col("nick", "users", data_type="text", nullable=True, ordinal=3),
            ),
        )
        rendered = _serialize_schema([users])
        # Type must appear so the LLM can reason about it.
        assert "bigint" in rendered
        assert "text" in rendered
        # NOT NULL marker on non-nullable columns; absent on nullable.
        assert "NOT NULL" in rendered
        # PK marker on PK columns.
        assert "PRIMARY KEY" in rendered

    def test_foreign_keys_render_with_target(self) -> None:
        users = Table(
            name="users",
            schema_name="public",
            columns=(_col("id", "users", is_pk=True, nullable=False),),
        )
        orders = Table(
            name="orders",
            schema_name="public",
            columns=(
                _col("id", "orders", is_pk=True, nullable=False, ordinal=1),
                _col("user_id", "orders", nullable=False, ordinal=2),
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
        rendered = _serialize_schema([users, orders])
        # FK line must encode source + target with schema-qualified target.
        assert "user_id" in rendered
        assert "public.users" in rendered

    def test_multiple_tables_are_separated(self) -> None:
        users = Table(
            name="users",
            schema_name="public",
            columns=(_col("id", "users", is_pk=True, nullable=False),),
        )
        products = Table(
            name="products",
            schema_name="public",
            columns=(_col("id", "products", is_pk=True, nullable=False),),
        )
        rendered = _serialize_schema([users, products])
        # Both table headers present; blank-line separator between blocks.
        assert "public.users" in rendered
        assert "public.products" in rendered
        # The separator keeps the two blocks visually distinct to the LLM
        # — flat unseparated columns confuse the model on multi-table input.
        assert "\n\n" in rendered

    def test_table_with_no_columns_still_renders_header(self) -> None:
        # Edge case: a table indexed before columns were captured (or one
        # with zero columns in a test fixture). The header alone is still
        # informative — better than silently dropping the table.
        empty_table = Table(name="placeholder", schema_name="public")
        rendered = _serialize_schema([empty_table])
        assert "public.placeholder" in rendered

    def test_composite_foreign_key_renders_all_columns(self) -> None:
        # Composite PK + composite FK is a real shape — make sure both
        # column lists appear in the rendering. If we dropped trailing
        # columns the LLM would miss the constraint.
        users = Table(
            name="users",
            schema_name="public",
            columns=(
                _col("tenant_id", "users", is_pk=True, nullable=False, ordinal=1),
                _col("id", "users", is_pk=True, nullable=False, ordinal=2),
            ),
        )
        memberships = Table(
            name="memberships",
            schema_name="public",
            columns=(
                _col("tenant_id", "memberships", nullable=False, ordinal=1),
                _col("user_id", "memberships", nullable=False, ordinal=2),
            ),
            foreign_keys=(
                ForeignKey(
                    name="memberships_users_fkey",
                    source_columns=("tenant_id", "user_id"),
                    target_schema="public",
                    target_table="users",
                    target_columns=("tenant_id", "id"),
                ),
            ),
        )
        rendered = _serialize_schema([users, memberships])
        assert "tenant_id" in rendered
        assert "user_id" in rendered
