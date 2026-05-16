"""Tests for the entity-shaped MCP response models.

`EntitySummary` and `EntityDetail` are the wire shapes for the
`list_entities` / `describe_entity` tools. `EntityColumn` is the
per-column shape exposed under `EntityDetail.columns`, carrying the
inert `pii_sensitivity` field that future PII-redaction work will
populate. The `EntityNotFoundError` exception is the recovery-routing
primitive for `describe_entity_impl` and mirrors `TableNotFoundError`.

These tests pin construction shape and frozenness; the MCP-envelope
behaviour (status mapping, error recovery hints) is covered by
test_mcp_list_entities + test_mcp_describe_entity.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from schemabrain.mcp.shapes import (
    EntityColumn,
    EntityDetail,
    EntityNotFoundError,
    EntitySummary,
)

# ----- EntitySummary ---------------------------------------------------------


class TestEntitySummary:
    def test_constructs_with_required_fields(self) -> None:
        summary = EntitySummary(
            name="customer",
            description="A registered shopper",
            qualified_table="public.users",
            identity="id",
            origin="manual",
        )
        assert summary.name == "customer"
        assert summary.description == "A registered shopper"
        assert summary.qualified_table == "public.users"
        assert summary.identity == "id"
        assert summary.origin == "manual"

    def test_is_frozen(self) -> None:
        summary = EntitySummary(
            name="customer",
            description="",
            qualified_table="public.users",
            identity="id",
            origin="manual",
        )
        with pytest.raises(ValidationError):
            summary.name = "order"  # type: ignore[misc]

    @pytest.mark.parametrize("origin", ["manual", "suggested", "dbt_import"])
    def test_accepts_all_origin_literals(self, origin: str) -> None:
        summary = EntitySummary(
            name="customer",
            description="",
            qualified_table="public.users",
            identity="id",
            origin=origin,  # type: ignore[arg-type]
        )
        assert summary.origin == origin

    def test_rejects_unknown_origin(self) -> None:
        with pytest.raises(ValidationError):
            EntitySummary(
                name="customer",
                description="",
                qualified_table="public.users",
                identity="id",
                origin="auto_inferred",  # type: ignore[arg-type]
            )

    def test_empty_description_is_valid(self) -> None:
        summary = EntitySummary(
            name="customer",
            description="",
            qualified_table="public.users",
            identity="id",
            origin="manual",
        )
        assert summary.description == ""


# ----- EntityColumn ----------------------------------------------------------


class TestEntityColumn:
    def test_constructs_with_required_fields(self) -> None:
        column = EntityColumn(
            name="id",
            data_type="bigint",
            nullable=False,
        )
        assert column.name == "id"
        assert column.data_type == "bigint"
        assert column.nullable is False

    def test_description_defaults_to_empty(self) -> None:
        column = EntityColumn(name="id", data_type="bigint", nullable=False)
        assert column.description == ""

    def test_pii_sensitivity_defaults_to_public(self) -> None:
        """Today hardcodes pii_sensitivity to 'public'; future PII work populates."""
        column = EntityColumn(name="id", data_type="bigint", nullable=False)
        assert column.pii_sensitivity == "public"

    @pytest.mark.parametrize("sensitivity", ["public", "internal", "confidential", "pii"])
    def test_accepts_all_sensitivity_literals(self, sensitivity: str) -> None:
        column = EntityColumn(
            name="email",
            data_type="text",
            nullable=False,
            pii_sensitivity=sensitivity,  # type: ignore[arg-type]
        )
        assert column.pii_sensitivity == sensitivity

    def test_rejects_unknown_sensitivity(self) -> None:
        with pytest.raises(ValidationError):
            EntityColumn(
                name="email",
                data_type="text",
                nullable=False,
                pii_sensitivity="restricted",  # type: ignore[arg-type]
            )

    def test_is_frozen(self) -> None:
        column = EntityColumn(name="id", data_type="bigint", nullable=False)
        with pytest.raises(ValidationError):
            column.name = "user_id"  # type: ignore[misc]


# ----- EntityDetail ----------------------------------------------------------


class TestEntityDetail:
    def test_constructs_with_required_fields(self) -> None:
        detail = EntityDetail(
            name="customer",
            description="A registered shopper",
            qualified_table="public.users",
            identity="id",
            origin="manual",
            columns=[
                EntityColumn(name="id", data_type="bigint", nullable=False),
                EntityColumn(name="email", data_type="text", nullable=False),
            ],
            token_estimate=80,
        )
        assert detail.name == "customer"
        assert len(detail.columns) == 2
        assert detail.token_estimate == 80

    def test_accepts_empty_column_list(self) -> None:
        """A bound table can have zero columns in principle (defensive)."""
        detail = EntityDetail(
            name="customer",
            description="",
            qualified_table="public.users",
            identity="id",
            origin="manual",
            columns=[],
            token_estimate=20,
        )
        assert detail.columns == []

    def test_is_frozen(self) -> None:
        detail = EntityDetail(
            name="customer",
            description="",
            qualified_table="public.users",
            identity="id",
            origin="manual",
            columns=[],
            token_estimate=20,
        )
        with pytest.raises(ValidationError):
            detail.name = "order"  # type: ignore[misc]

    def test_rejects_unknown_origin(self) -> None:
        with pytest.raises(ValidationError):
            EntityDetail(
                name="customer",
                description="",
                qualified_table="public.users",
                identity="id",
                origin="auto_inferred",  # type: ignore[arg-type]
                columns=[],
                token_estimate=20,
            )


# ----- EntityNotFoundError ---------------------------------------------------


class TestEntityNotFoundError:
    def test_is_lookup_error(self) -> None:
        """Mirrors `TableNotFoundError` — callers should be able to
        catch via `LookupError` if they want to treat all
        unknown-name lookups uniformly."""
        assert issubclass(EntityNotFoundError, LookupError)

    def test_carries_message(self) -> None:
        err = EntityNotFoundError("entity 'ghost' not found")
        assert "ghost" in str(err)
