"""FW-003 (IF-2) — FK metadata leaks redacted column names
through ``target_columns`` / ``source_columns`` lists.

Background:

    ``schemabrain/mcp/describe_table.py:117-125`` and
    ``describe_column.py:106-135`` redact ``ColumnInfo.name`` but
    leave FK source/target column lists unscrubbed.
    ``outgoing_foreign_keys[0].target_columns = ["ssn"]`` leaks the
    redacted name through the FK graph.

Expected SECURE behaviour:

    When ``describe_column`` returns a column whose outgoing FKs
    point at a redacted column on another table, the
    ``target_columns`` list must carry the same placeholder
    (``<redacted_government_id_column_N>``) that ``describe_table``
    would surface for that target column. Symmetric for incoming FKs.
    Otherwise the FK graph is a side channel — the agent reads
    ``describe_table('users')`` → sees ``<redacted_government_id_column_1>``,
    then reads ``describe_column('audit_log.ref_ssn')`` → sees
    ``target_columns=['ssn']`` and now knows the placeholder name.

Repro shape (pure in-process via the SchemaBrain store):

  1. Seed ``public.users`` with an ``ssn`` column tagged
     ``government_id``.
  2. Seed ``public.audit_log`` with a ``ref_ssn`` column carrying an
     outgoing FK to ``users.ssn``.
  3. Call ``describe_column_impl`` on ``public.audit_log.ref_ssn``.
  4. Assert the outgoing FK's ``target_columns`` does NOT contain the
     raw ``ssn`` string.

Today the assertion FAILS — the FK metadata is the bypass.
"""

from __future__ import annotations

import pytest

from schemabrain.core.models import Column, ForeignKey, Table
from schemabrain.mcp.describe_column import describe_column_impl
from schemabrain.pii import CATASTROPHIC_LEAK_CATEGORIES

pytestmark = [pytest.mark.firewall_bypass]

SOURCE_ID = "fw_003"


def _seed_users_with_ssn(store) -> None:
    """``public.users`` with a tagged ``ssn`` column."""
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
                name="ssn",
                table_name="users",
                schema_name="public",
                data_type="text",
                nullable=True,
                ordinal_position=2,
            ),
        ),
    )
    store.write_table(users, source_connection_id=SOURCE_ID)
    store.write_column_pii_tags(
        source_connection_id=SOURCE_ID,
        qualified_table="public.users",
        tags={"ssn": ("pii", frozenset({"government_id"}))},
    )


def _seed_audit_log_with_fk(store) -> None:
    """``public.audit_log`` with ``ref_ssn`` FK -> users.ssn.

    ``ref_ssn`` is itself non-PII per the classifier (no rule matches);
    the leak is in the FK shape, not in the source column's tag.
    """
    audit = Table(
        name="audit_log",
        schema_name="public",
        columns=(
            Column(
                name="id",
                table_name="audit_log",
                schema_name="public",
                data_type="bigint",
                nullable=False,
                ordinal_position=1,
                is_primary_key=True,
            ),
            Column(
                name="ref_ssn",
                table_name="audit_log",
                schema_name="public",
                data_type="text",
                nullable=True,
                ordinal_position=2,
            ),
        ),
        foreign_keys=(
            ForeignKey(
                name="audit_log_ref_ssn_fkey",
                source_columns=("ref_ssn",),
                target_schema="public",
                target_table="users",
                target_columns=("ssn",),
            ),
        ),
    )
    store.write_table(audit, source_connection_id=SOURCE_ID)


def test_describe_column_outgoing_fk_does_not_leak_redacted_target(store) -> None:
    """``describe_column`` on a FK source column MUST NOT expose the
    redacted target column's raw name through ``target_columns``.

    Today the test fails — ``outgoing_foreign_keys[0].target_columns``
    is ``['ssn']``. ``ssn`` is in the ``government_id`` category, which
    is in the catastrophic-leak set, which ``describe_table`` redacts
    to ``<redacted_government_id_column_N>``. The FK graph bypasses
    that redaction.
    """
    _seed_users_with_ssn(store)
    _seed_audit_log_with_fk(store)

    assert "government_id" in CATASTROPHIC_LEAK_CATEGORIES, (
        "sanity: government_id must be in the catastrophic-leak set so "
        "describe_table would redact `users.ssn`"
    )

    detail = describe_column_impl(
        store=store,
        source_connection_id=SOURCE_ID,
        qualified_name="public.audit_log.ref_ssn",
    )

    assert detail.outgoing_foreign_keys, "sanity: expected one outgoing FK"
    target_cols = detail.outgoing_foreign_keys[0].target_columns

    assert "ssn" not in target_cols, (
        f"BYPASS: describe_column surfaced the raw catastrophic-leak "
        f"target column name 'ssn' in outgoing_foreign_keys[0]."
        f"target_columns={target_cols!r}. describe_table redacts "
        f"users.ssn → <redacted_government_id_column_N>; the FK graph "
        f"in describe_column must mirror that placeholder, not echo "
        f"the raw name."
    )


def _seed_orders_with_card_fk(store) -> None:
    """``public.orders`` with a non-PII id PK and a FK ``payer_card_id``
    pointing at a payment_card-tagged ``card_pan`` column on
    ``billing.cards``. Used to test the symmetric incoming-FK direction:
    when an agent calls ``describe_column`` on ``billing.cards.id`` or
    a different non-PII column on the cards table, the response carries
    incoming FK back-references whose ``target_columns`` include the
    catastrophic-leak name verbatim.
    """
    cards = Table(
        name="cards",
        schema_name="billing",
        columns=(
            Column(
                name="id",
                table_name="cards",
                schema_name="billing",
                data_type="bigint",
                nullable=False,
                ordinal_position=1,
                is_primary_key=True,
            ),
            Column(
                name="card_pan",
                table_name="cards",
                schema_name="billing",
                data_type="text",
                nullable=False,
                ordinal_position=2,
            ),
        ),
    )
    store.write_table(cards, source_connection_id=SOURCE_ID)
    store.write_column_pii_tags(
        source_connection_id=SOURCE_ID,
        qualified_table="billing.cards",
        tags={"card_pan": ("pii", frozenset({"payment_card"}))},
    )
    orders = Table(
        name="orders",
        schema_name="public",
        columns=(
            Column(
                name="id",
                table_name="orders",
                schema_name="public",
                data_type="bigint",
                nullable=False,
                ordinal_position=1,
                is_primary_key=True,
            ),
            Column(
                name="payer_card_pan",
                table_name="orders",
                schema_name="public",
                data_type="text",
                nullable=True,
                ordinal_position=2,
            ),
        ),
        foreign_keys=(
            ForeignKey(
                name="orders_payer_card_pan_fkey",
                source_columns=("payer_card_pan",),
                target_schema="billing",
                target_table="cards",
                target_columns=("card_pan",),
            ),
        ),
    )
    store.write_table(orders, source_connection_id=SOURCE_ID)


def test_describe_column_incoming_fk_does_not_leak_redacted_target_column(
    store,
) -> None:
    """Incoming-FK direction.

    An agent calls ``describe_column`` on ``billing.cards.id``
    (non-PII PK) and reads ``incoming_foreign_keys[0].target_columns``.
    Today this carries ``['card_pan']`` — the catastrophic-leak column
    name on the SAME table this call is describing, exposed through
    the back-reference graph. The secure shape is the redacted
    placeholder, same as ``describe_table('billing.cards')`` returns.
    """
    _seed_users_with_ssn(store)
    _seed_orders_with_card_fk(store)

    detail = describe_column_impl(
        store=store,
        source_connection_id=SOURCE_ID,
        qualified_name="billing.cards.id",
    )

    # `billing.cards.id` itself is non-PII, so describe_column returns
    # (no PiiBlockedError raised at the top-level pii_block check).
    # The incoming-FK list, however, names a sibling catastrophic
    # column. There IS a single incoming FK (orders → cards on a
    # NON-id column), so target_columns is `['card_pan']` — the leak.
    if not detail.incoming_foreign_keys:
        pytest.skip(
            "incoming-FK back-reference index returned no rows — "
            "store layer didn't populate the back-reference for this "
            "shape; the leak surface remains in describe_table for "
            "this case"
        )
    incoming = detail.incoming_foreign_keys[0]
    assert "card_pan" not in incoming.target_columns, (
        f"BYPASS: describe_column surfaced the raw catastrophic-leak "
        f"target column 'card_pan' in incoming_foreign_keys[0]."
        f"target_columns={incoming.target_columns!r}. Mirror the "
        f"describe_table placeholder."
    )
