"""Tests for the `describe_entity` MCP tool.

Two layers tested here:

  - `describe_entity_impl` (pure function, store + source_connection_id +
    name in, `EntityDetail` out) — happy path, name validation,
    unknown-entity path, column descriptions propagating from the
    bound table's LLM-enriched descriptions.
  - The wired tool on the FastMCP server — envelope round-trip,
    `unknown_name` / `malformed_name` recovery routing, PII
    sensitivity field carried through (inert at this release).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from schemabrain.core.description import ColumnDescription
from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.mcp import build_server
from schemabrain.mcp.describe_entity import describe_entity_impl
from schemabrain.mcp.envelope import ToolResponse
from schemabrain.mcp.shapes import EntityDetail, EntityNotFoundError

# ----- helpers ---------------------------------------------------------------


def _users_table() -> Table:
    return Table(
        name="users",
        schema_name="public",
        columns=(
            Column(
                name="id",
                table_name="users",
                schema_name="public",
                data_type="bigint",
                nullable=False,
                ordinal_position=1,
                is_primary_key=True,
            ),
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


def _customer_entity(origin: str = "manual") -> Entity:
    return Entity(
        name="customer",
        description="A registered shopper",
        binding=SingleTableBinding(qualified_table="public.users"),
        identity="id",
        origin=origin,  # type: ignore[arg-type]
    )


class _StubEmbedder:
    model_name = "test"
    dimension = 4

    def embed(self, text: str) -> tuple[float, ...]:
        del text
        return (1.0, 0.0, 0.0, 0.0)


def _build_test_server(tmp_path: Path) -> tuple[object, SQLiteStore]:
    """Build a server with `public.users` seeded but no entities written."""
    store = SQLiteStore(tmp_path / "s.db")
    store.write_table(_users_table(), source_connection_id="sid")
    server = build_server(
        store=store,
        source_connection_id="sid",
        embedder=_StubEmbedder(),
    )
    return server, store


# ----- impl-level tests ------------------------------------------------------


class TestImplHappyPath:
    def test_returns_detail_with_columns(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            store.write_table(_users_table(), source_connection_id="sid")
            store.write_entity(_customer_entity(), source_connection_id="sid")
            detail = describe_entity_impl(
                store=store,
                source_connection_id="sid",
                name="customer",
            )
        assert isinstance(detail, EntityDetail)
        assert detail.name == "customer"
        assert detail.description == "A registered shopper"
        assert detail.qualified_table == "public.users"
        assert detail.identity == "id"
        assert detail.origin == "manual"
        # All columns of the bound table exposed (no column allowlist
        # in YAML grammar at this release).
        assert [c.name for c in detail.columns] == ["id", "email"]

    def test_columns_include_data_type_and_nullable(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            store.write_table(_users_table(), source_connection_id="sid")
            store.write_entity(_customer_entity(), source_connection_id="sid")
            detail = describe_entity_impl(
                store=store,
                source_connection_id="sid",
                name="customer",
            )
        id_col = next(c for c in detail.columns if c.name == "id")
        assert id_col.data_type == "bigint"
        assert id_col.nullable is False

    def test_pii_sensitivity_defaults_to_public(self, tmp_path: Path) -> None:
        """Columns without stored PII classification default to
        `("public", frozenset())` — the propagation helper's empty-input
        contract carries through to the wire shape."""
        with SQLiteStore(tmp_path / "s.db") as store:
            store.write_table(_users_table(), source_connection_id="sid")
            store.write_entity(_customer_entity(), source_connection_id="sid")
            detail = describe_entity_impl(
                store=store,
                source_connection_id="sid",
                name="customer",
            )
        assert all(c.pii_sensitivity == "public" for c in detail.columns)
        assert all(c.pii_categories == () for c in detail.columns)
        assert all(c.redacted is False for c in detail.columns)

    def test_pii_classification_propagates_when_stored(self, tmp_path: Path) -> None:
        """Charter v1.2 column-granular firewall: stored PII tags
        propagate through to `EntityColumn.pii_sensitivity` +
        `pii_categories`."""
        with SQLiteStore(tmp_path / "s.db") as store:
            store.write_table(_users_table(), source_connection_id="sid")
            store.write_entity(_customer_entity(), source_connection_id="sid")
            store.write_column_pii_tags(
                qualified_table="public.users",
                tags={
                    "email": ("pii", frozenset({"contact"})),
                },
                source_connection_id="sid",
            )
            detail = describe_entity_impl(
                store=store,
                source_connection_id="sid",
                name="customer",
            )
        email_col = next(c for c in detail.columns if c.name == "email")
        assert email_col.pii_sensitivity == "pii"
        assert email_col.pii_categories == ("contact",)
        # No pii_block set → no redaction even though the column is PII.
        assert email_col.redacted is False
        assert detail.redacted_columns == ()

    def test_column_descriptions_empty_when_no_enrichment(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            store.write_table(_users_table(), source_connection_id="sid")
            store.write_entity(_customer_entity(), source_connection_id="sid")
            detail = describe_entity_impl(
                store=store,
                source_connection_id="sid",
                name="customer",
            )
        assert all(c.description == "" for c in detail.columns)

    def test_column_descriptions_propagate(self, tmp_path: Path) -> None:
        """LLM-enriched descriptions on the bound table surface
        through `describe_entity.columns[i].description`."""
        with SQLiteStore(tmp_path / "s.db") as store:
            store.write_table(_users_table(), source_connection_id="sid")
            store.write_entity(_customer_entity(), source_connection_id="sid")
            store.write_table_descriptions(
                schema_name="public",
                name="users",
                source_connection_id="sid",
                descriptions={
                    "email": ColumnDescription(
                        text="Primary contact email",
                        model="m",
                        prompt_version="v",
                        input_tokens=1,
                        cached_input_tokens=0,
                        output_tokens=1,
                        cost_usd=0.0,
                    ),
                },
            )
            detail = describe_entity_impl(
                store=store,
                source_connection_id="sid",
                name="customer",
            )
        email_col = next(c for c in detail.columns if c.name == "email")
        assert email_col.description == "Primary contact email"

    @pytest.mark.parametrize("origin", ["manual", "suggested", "dbt_import"])
    def test_origin_propagates(self, tmp_path: Path, origin: str) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            store.write_table(_users_table(), source_connection_id="sid")
            store.write_entity(_customer_entity(origin=origin), source_connection_id="sid")
            detail = describe_entity_impl(
                store=store,
                source_connection_id="sid",
                name="customer",
            )
        assert detail.origin == origin

    def test_token_estimate_is_positive(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            store.write_table(_users_table(), source_connection_id="sid")
            store.write_entity(_customer_entity(), source_connection_id="sid")
            detail = describe_entity_impl(
                store=store,
                source_connection_id="sid",
                name="customer",
            )
        assert detail.token_estimate >= 1


# ----- impl error paths ------------------------------------------------------


class TestImplErrors:
    def test_unknown_entity_raises_not_found(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            store.write_table(_users_table(), source_connection_id="sid")
            with pytest.raises(EntityNotFoundError, match="ghost"):
                describe_entity_impl(
                    store=store,
                    source_connection_id="sid",
                    name="ghost",
                )

    @pytest.mark.parametrize(
        "bad_name",
        [
            "",
            "1customer",  # starts with digit
            "customer-1",  # hyphen
            "public.customer",  # dotted (agent passed table form)
            "customer ",  # trailing space
        ],
    )
    def test_malformed_name_raises_value_error(self, tmp_path: Path, bad_name: str) -> None:
        with (
            SQLiteStore(tmp_path / "s.db") as store,
            pytest.raises(ValueError),
        ):
            describe_entity_impl(
                store=store,
                source_connection_id="sid",
                name=bad_name,
            )

    def test_long_name_rejected(self, tmp_path: Path) -> None:
        """Bounded-identifier rule applies — keeps echoed error
        messages short under adversarial input."""
        with (
            SQLiteStore(tmp_path / "s.db") as store,
            pytest.raises(ValueError, match="too long"),
        ):
            describe_entity_impl(
                store=store,
                source_connection_id="sid",
                name="x" * 100,
            )

    def test_fk_invariant_violation_raises_runtime_error(self, tmp_path: Path) -> None:
        """If the bound table is missing despite the entity existing
        (only reachable via direct SQL corruption that bypasses FK),
        the impl must raise a real `RuntimeError` — not silently
        produce a None-deref later. Replaces the previous `assert`
        which would be stripped under `python -O`.
        """
        with SQLiteStore(tmp_path / "s.db") as store:
            store.write_table(_users_table(), source_connection_id="sid")
            store.write_entity(_customer_entity(), source_connection_id="sid")
            # Bypass the FK CASCADE by disabling FK enforcement on the
            # raw connection and deleting the table row directly. The
            # entity row remains, the bound table is gone — a state
            # the v8 schema prevents via FK but a corrupted store
            # could exhibit.
            conn = store._require_conn()
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute(
                "DELETE FROM tables WHERE schema_name = ? AND name = ? "
                "AND source_connection_id = ?",
                ("public", "users", "sid"),
            )
            conn.commit()
            conn.execute("PRAGMA foreign_keys = ON")
            with pytest.raises(RuntimeError, match="FK invariant"):
                describe_entity_impl(
                    store=store,
                    source_connection_id="sid",
                    name="customer",
                )


# ----- envelope round-trip tests ---------------------------------------------


class TestEnvelopeSuccess:
    def test_returns_success_envelope_with_columns(self, tmp_path: Path) -> None:
        server, store = _build_test_server(tmp_path)
        try:
            store.write_entity(_customer_entity(), source_connection_id="sid")
            _content, structured = asyncio.run(
                server.call_tool("describe_entity", {"name": "customer"})
            )
            envelope = ToolResponse.model_validate(structured)
        finally:
            store.close()
        assert envelope.status == "success"
        assert envelope.error is None
        assert envelope.data is not None
        assert envelope.data["name"] == "customer"
        assert envelope.data["qualified_table"] == "public.users"
        assert len(envelope.data["columns"]) == 2
        # Charter Principle 5: compositions.
        hints = envelope.follow_up_hints or ()
        assert "describe_table" in hints

    def test_envelope_carries_pii_sensitivity(self, tmp_path: Path) -> None:
        """Inert at this release (always 'public'); shape locked so future PII work
        can populate without retrofitting."""
        server, store = _build_test_server(tmp_path)
        try:
            store.write_entity(_customer_entity(), source_connection_id="sid")
            _content, structured = asyncio.run(
                server.call_tool("describe_entity", {"name": "customer"})
            )
            envelope = ToolResponse.model_validate(structured)
        finally:
            store.close()
        assert envelope.data is not None
        for col in envelope.data["columns"]:
            assert col["pii_sensitivity"] == "public"


class TestEnvelopeErrors:
    def test_unknown_entity_yields_unknown_name_with_recovery(self, tmp_path: Path) -> None:
        server, store = _build_test_server(tmp_path)
        try:
            _content, structured = asyncio.run(
                server.call_tool("describe_entity", {"name": "ghost"})
            )
            envelope = ToolResponse.model_validate(structured)
        finally:
            store.close()
        assert envelope.status == "error"
        assert envelope.error is not None
        assert envelope.error.kind == "unknown_name"
        # Recovery → list_entities so the agent can see what IS defined.
        assert envelope.error.recovery is not None
        assert envelope.error.recovery.suggested_tool == "list_entities"

    def test_malformed_name_yields_malformed_name_with_recovery(self, tmp_path: Path) -> None:
        server, store = _build_test_server(tmp_path)
        try:
            _content, structured = asyncio.run(
                server.call_tool("describe_entity", {"name": "public.customer"})
            )
            envelope = ToolResponse.model_validate(structured)
        finally:
            store.close()
        assert envelope.status == "error"
        assert envelope.error is not None
        assert envelope.error.kind == "malformed_name"
        assert envelope.error.recovery is not None
        assert envelope.error.recovery.suggested_tool == "list_entities"

    def test_empty_name_yields_malformed_name(self, tmp_path: Path) -> None:
        server, store = _build_test_server(tmp_path)
        try:
            _content, structured = asyncio.run(server.call_tool("describe_entity", {"name": ""}))
            envelope = ToolResponse.model_validate(structured)
        finally:
            store.close()
        assert envelope.status == "error"
        assert envelope.error is not None
        assert envelope.error.kind == "malformed_name"


class TestPiiBlockColumnRedaction:
    """Charter v1.2 column-granular firewall: when `--pii-block` is set
    on the server, columns whose stored PII categories intersect the
    blocked set are marked `redacted=True` with descriptions cleared.
    The agent still sees the column exists (no entity-level refusal);
    the policy applies at the column level only.
    """

    def _build_with_pii_block(
        self, tmp_path: Path, blocked: frozenset[str]
    ) -> tuple[object, SQLiteStore]:
        store = SQLiteStore(tmp_path / "s.db")
        store.write_table(_users_table(), source_connection_id="sid")
        store.write_entity(_customer_entity(), source_connection_id="sid")
        store.write_column_pii_tags(
            qualified_table="public.users",
            tags={
                "email": ("pii", frozenset({"contact"})),
                "id": ("public", frozenset()),
            },
            source_connection_id="sid",
        )
        store.write_table_descriptions(
            schema_name="public",
            name="users",
            source_connection_id="sid",
            descriptions={
                "email": ColumnDescription(
                    text="Primary contact email",
                    model="m",
                    prompt_version="v",
                    input_tokens=1,
                    cached_input_tokens=0,
                    output_tokens=1,
                    cost_usd=0.0,
                ),
            },
        )
        server = build_server(
            store=store,
            source_connection_id="sid",
            embedder=_StubEmbedder(),
            pii_block=blocked,  # type: ignore[arg-type]
        )
        return server, store

    def test_blocked_column_marked_redacted(self, tmp_path: Path) -> None:
        """The PII-tagged column ships with `redacted=True` and its
        description is cleared. The non-PII column is untouched.
        """
        server, store = self._build_with_pii_block(tmp_path, frozenset({"contact"}))
        try:
            _content, structured = asyncio.run(
                server.call_tool("describe_entity", {"name": "customer"})
            )
            envelope = ToolResponse.model_validate(structured)
        finally:
            store.close()
        assert envelope.status == "success"
        assert envelope.data is not None
        cols_by_name = {c["name"]: c for c in envelope.data["columns"]}
        assert cols_by_name["email"]["redacted"] is True
        assert cols_by_name["email"]["description"] == ""
        assert cols_by_name["id"]["redacted"] is False
        assert envelope.data["redacted_columns"] == ["email"]

    def test_unrelated_pii_block_no_redaction(self, tmp_path: Path) -> None:
        """A `pii_block` set that doesn't intersect any column's tags
        leaves every column unredacted — and the description survives.
        """
        server, store = self._build_with_pii_block(tmp_path, frozenset({"financial"}))
        try:
            _content, structured = asyncio.run(
                server.call_tool("describe_entity", {"name": "customer"})
            )
            envelope = ToolResponse.model_validate(structured)
        finally:
            store.close()
        cols_by_name = {c["name"]: c for c in envelope.data["columns"]}
        assert cols_by_name["email"]["redacted"] is False
        assert cols_by_name["email"]["description"] == "Primary contact email"
        assert envelope.data["redacted_columns"] == []

    def test_confidence_capped_at_medium_when_redacted(self, tmp_path: Path) -> None:
        """When at least one column is redacted, envelope `confidence`
        is capped at MEDIUM — the agent saw a partial view of the
        entity, so even a hand-confirmed entity row cannot report HIGH.
        """
        server, store = self._build_with_pii_block(tmp_path, frozenset({"contact"}))
        try:
            _content, structured = asyncio.run(
                server.call_tool("describe_entity", {"name": "customer"})
            )
            envelope = ToolResponse.model_validate(structured)
        finally:
            store.close()
        assert envelope.confidence == "MEDIUM"

    def test_impl_supports_pii_block_kwarg(self, tmp_path: Path) -> None:
        """The pure-function `describe_entity_impl` accepts `pii_block`
        directly — used by callers that build their own server scaffold
        (e.g. dry-run tooling, tests).
        """
        with SQLiteStore(tmp_path / "s.db") as store:
            store.write_table(_users_table(), source_connection_id="sid")
            store.write_entity(_customer_entity(), source_connection_id="sid")
            store.write_column_pii_tags(
                qualified_table="public.users",
                tags={"email": ("pii", frozenset({"contact"}))},
                source_connection_id="sid",
            )
            detail = describe_entity_impl(
                store=store,
                source_connection_id="sid",
                name="customer",
                pii_block=frozenset({"contact"}),
            )
        email_col = next(c for c in detail.columns if c.name == "email")
        assert email_col.redacted is True
        assert detail.redacted_columns == ("email",)
