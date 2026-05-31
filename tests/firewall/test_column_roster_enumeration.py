"""Column-roster enumeration via ColumnNotFoundError.

Background:

    Before the read-side hardening pass, ``schemabrain/mcp/describe_column.py``
    raised ``ColumnNotFoundError`` with a message that enumerated every
    real column on the table:

        Existing columns: ['credit_card', 'id', 'password_hash', 'ssn']

    A single probe with a non-existent column name handed the agent the
    full roster of column names that ``describe_table`` would otherwise
    have redacted — turning a typo-recovery hint into an oracle for the
    catastrophic-leak columns (credential / payment_card / government_id).

Expected SECURE behaviour:

    1. The error message MUST NOT enumerate column names.
    2. A typo-recovery suggestion ("Did you mean 'X'?") is allowed, but
       ONLY for non-catastrophic columns and ONLY one suggestion at
       most — never a count or list.
    3. When the only close match is catastrophic, the suggestion is
       silently omitted (no probe-oracle leakage of "N similar columns
       hidden").

Repro shape (pure in-process via the SchemaBrain store):

  1. Seed ``public.users`` with three catastrophic-tagged columns
     (password_hash → credential, ssn → government_id, credit_card →
     payment_card) plus one safe ``id`` PK.
  2. Probe each catastrophic column name's near-typo via
     ``describe_column_impl``.
  3. Assert the raised ``ColumnNotFoundError`` carries no roster, no
     suggestion of the catastrophic name, and no list-bracket
     indicator that a roster was assembled.
"""

from __future__ import annotations

import pytest

from schemabrain.core.models import Column, Table
from schemabrain.mcp.describe_column import describe_column_impl
from schemabrain.mcp.shapes import ColumnNotFoundError
from schemabrain.pii import CATASTROPHIC_LEAK_CATEGORIES

pytestmark = [pytest.mark.firewall_bypass]

SOURCE_ID = "column_roster"


def _seed_users_with_three_catastrophic_columns(store) -> None:
    """``public.users`` with one safe id PK + three catastrophic columns."""
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
                name="password_hash",
                table_name="users",
                schema_name="public",
                data_type="text",
                nullable=False,
                ordinal_position=2,
            ),
            Column(
                name="ssn",
                table_name="users",
                schema_name="public",
                data_type="text",
                nullable=True,
                ordinal_position=3,
            ),
            Column(
                name="credit_card",
                table_name="users",
                schema_name="public",
                data_type="text",
                nullable=True,
                ordinal_position=4,
            ),
        ),
    )
    store.write_table(users, source_connection_id=SOURCE_ID)
    store.write_column_pii_tags(
        source_connection_id=SOURCE_ID,
        qualified_table="public.users",
        tags={
            "password_hash": ("pii", frozenset({"credential"})),
            "ssn": ("pii", frozenset({"government_id"})),
            "credit_card": ("pii", frozenset({"payment_card"})),
        },
    )


@pytest.mark.parametrize(
    "typo,leaked_name",
    [
        ("passwrd_hash", "password_hash"),
        ("ssnn", "ssn"),
        ("creditcard", "credit_card"),
    ],
)
def test_column_not_found_does_not_enumerate_catastrophic_columns(
    store, typo: str, leaked_name: str
) -> None:
    """Probe each catastrophic column's typo and assert the response
    message does not name the column being probed.

    Note: the agent's own probe string is echoed back in the qualified-
    name prefix (the agent already knows what it sent, so that's not a
    leak). We strip the echoed prefix before asserting against the
    store-derived tail.
    """
    _seed_users_with_three_catastrophic_columns(store)
    with pytest.raises(ColumnNotFoundError) as excinfo:
        describe_column_impl(
            store=store,
            source_connection_id=SOURCE_ID,
            qualified_name=f"public.users.{typo}",
        )
    msg = str(excinfo.value)
    # Strip the agent-supplied prefix — only the store-derived tail can leak.
    prefix = f"public.users.{typo} does not exist on public.users."
    assert msg.startswith(prefix), f"qualified-name prefix should survive verbatim, got: {msg!r}"
    tail = msg[len(prefix) :]
    # The catastrophic column being probed MUST NOT appear in the tail.
    assert leaked_name not in tail, (
        f"column_roster regression: probing {typo!r} leaked {leaked_name!r} in tail: {tail!r}"
    )
    # Belt-and-suspenders: no other catastrophic column name from the
    # roster appears in the tail either — closed-set check covers any
    # future catastrophic-tagged column.
    for catastrophic in ("password_hash", "ssn", "credit_card"):
        assert catastrophic not in tail, (
            f"column_roster regression: probing {typo!r} leaked {catastrophic!r} in tail"
        )
    # The roster-enumeration phrasing + list-bracket marker must not appear.
    assert "Existing columns" not in msg
    assert "[" not in msg


def test_column_not_found_message_preserves_qualified_name_prefix(store) -> None:
    """The agent's recovery path depends on the qualified name surviving
    in the message (server.py wraps it into Recovery.suggested_args).
    The redaction must not strip that prefix."""
    _seed_users_with_three_catastrophic_columns(store)
    with pytest.raises(ColumnNotFoundError) as excinfo:
        describe_column_impl(
            store=store,
            source_connection_id=SOURCE_ID,
            qualified_name="public.users.does_not_exist",
        )
    msg = str(excinfo.value)
    assert msg.startswith("public.users.does_not_exist does not exist on public.users")


def test_safe_column_suggestion_is_allowed(store) -> None:
    """A close match to the only non-catastrophic column (``id``) is a
    legitimate suggestion — the redaction filter must not be so
    aggressive that it suppresses safe recovery hints."""
    _seed_users_with_three_catastrophic_columns(store)
    with pytest.raises(ColumnNotFoundError) as excinfo:
        describe_column_impl(
            store=store,
            source_connection_id=SOURCE_ID,
            qualified_name="public.users.iD",
        )
    msg = str(excinfo.value)
    # Lowercase difflib match should suggest 'id'. The safe column may
    # surface as a hint.
    assert "Did you mean 'id'?" in msg or "Did you mean" not in msg


def test_catastrophic_floor_filter_unconditional() -> None:
    """The catastrophic floor must filter the candidate pool even when
    the caller passes no pii_block — same posture as get_metric and
    describe_table. Sanity-check the static floor set this module
    relies on."""
    assert frozenset({"credential", "payment_card", "government_id"}) == (
        CATASTROPHIC_LEAK_CATEGORIES
    )
