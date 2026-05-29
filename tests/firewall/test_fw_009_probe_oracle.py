"""FW-009 — ``describe_entity.pii_categories`` is a probe oracle.

Background:

    ``describe_entity.pii_categories`` ships the full category tuple
    even for redacted columns. Agent learns "column at ordinal 7 has
    categories (credential,)" — combined with data_type, this is an
    oracle to deduce the real column name.

Expected SECURE behaviour:

    A redacted column's response carries:
      - name      → placeholder (``<redacted_credential_column_1>``)
      - description → empty string
      - data_type   → preserved (operators rely on this for joins)
      - **pii_categories → must also be redacted / empty**

    Today, every other field is scrubbed, but ``pii_categories``
    leaks the exact category set. An agent that calls
    ``describe_entity('user')`` and sees:

        EntityColumn(
            name='<redacted_credential_column_1>',
            data_type='text',
            pii_categories=('credential',),  # ← oracle
            redacted=True,
        )

    knows the redacted column is a credential field of type ``text``.
    Combined with column-position ordinality, the agent can deduce
    the real name (``password_hash`` is the only credential ``text``
    column on most ``users`` tables) without ever reading it.

This is a probe oracle, not a direct data leak. The fix: when
``redacted=True``, ``pii_categories`` must be ``()`` (or carry a
generic ``('redacted',)`` placeholder) so the response is
information-equivalent across all redacted columns.

Pure in-process — no DB needed. Seed the store, call
``describe_entity_impl``, inspect the returned column.
"""

from __future__ import annotations

import pytest

from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.models import Column, Table
from schemabrain.mcp.describe_entity import describe_entity_impl

pytestmark = [pytest.mark.firewall_bypass]

SOURCE_ID = "fw_009"


def _seed_user_entity_with_credential_column(store) -> None:
    """``public.users`` with ``password_hash`` tagged credential, plus a
    confirmed entity binding so ``describe_entity`` works."""
    users = Table(
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
            Column(
                name="password_hash",
                table_name="users",
                schema_name="public",
                data_type="text",
                nullable=False,
                ordinal_position=3,
            ),
        ),
    )
    store.write_table(users, source_connection_id=SOURCE_ID)
    store.write_column_pii_tags(
        source_connection_id=SOURCE_ID,
        qualified_table="public.users",
        tags={
            "email": ("pii", frozenset({"contact"})),
            "password_hash": ("pii", frozenset({"credential"})),
        },
    )
    store.write_entity(
        Entity(
            name="user",
            description="",
            binding=SingleTableBinding(qualified_table="public.users"),
            identity="id",
        ),
        source_connection_id=SOURCE_ID,
    )


def test_describe_entity_does_not_leak_pii_categories_for_redacted_columns(store) -> None:
    """A redacted column's ``pii_categories`` MUST be empty so the
    response is information-equivalent across all redacted columns.

    Today the test fails — ``pii_categories=('credential',)`` ships
    alongside the placeholder name, giving the agent the exact
    category of the hidden column.
    """
    _seed_user_entity_with_credential_column(store)

    detail = describe_entity_impl(
        store=store,
        source_connection_id=SOURCE_ID,
        name="user",
    )

    # Find the redacted column. There's exactly one — password_hash.
    redacted_cols = [c for c in detail.columns if c.redacted]
    assert len(redacted_cols) == 1, (
        f"sanity: expected exactly one redacted column on user, "
        f"got {[c.name for c in redacted_cols]}"
    )
    rc = redacted_cols[0]

    # SECURE shape: every other field on the redacted column is scrubbed
    # (name → placeholder, description → ''); pii_categories must
    # likewise be empty. Otherwise it's a category oracle.
    assert rc.pii_categories == (), (
        f"BYPASS: redacted column carries pii_categories="
        f"{rc.pii_categories!r}. Name is masked to {rc.name!r} but "
        f"the category tuple leaks the exact PII family of the hidden "
        f"column — combined with data_type={rc.data_type!r}, the "
        f"agent can deduce the real column name. When redacted=True, "
        f"pii_categories must be ()."
    )


def test_describe_entity_redacted_column_data_type_unchanged_is_acceptable(store) -> None:
    """Control: data_type leaking is acceptable — operators need it.

    Locking the behaviour we want to PRESERVE so a remediation that
    over-eagerly strips every field doesn't break legitimate ops.
    """
    _seed_user_entity_with_credential_column(store)

    detail = describe_entity_impl(
        store=store,
        source_connection_id=SOURCE_ID,
        name="user",
    )

    redacted_cols = [c for c in detail.columns if c.redacted]
    assert redacted_cols, "sanity: at least one redacted column expected"
    rc = redacted_cols[0]

    # data_type preserved — this is the operator contract.
    assert rc.data_type, (
        "data_type must remain populated on redacted columns so "
        "operators can still see the type signature"
    )
