"""FK column names leak through suggest_joins and resolve_join.

Background:

    Before the read-side hardening pass, ``schemabrain/mcp/suggest_joins.py``
    and ``schemabrain/mcp/resolve_join.py`` did not thread a
    ``pii_block`` parameter and did not apply
    ``redact_blocked_fk_columns`` to their public column-name surfaces:

      - ``JoinEdge.left_columns`` / ``right_columns`` (suggest_joins)
      - ``CanonicalJoinInfo.on[*].source_column`` / ``target_column``
        AND the ``sql_skeleton`` string (resolve_join)

    Both tools therefore handed an agent the raw catastrophic-leak
    column names that ``describe_table`` carefully masks — turning
    the join graph (and the paste-ready SQL JOIN skeleton) into the
    discovery path for password_hash / ssn / credit_card columns.

Expected SECURE behaviour:

    1. ``suggest_joins`` per-edge: every ``left_columns`` and
       ``right_columns`` entry whose source-side or target-side PII
       categories intersect ``pii_block | CATASTROPHIC_LEAK_CATEGORIES``
       must read ``<redacted_column>``.
    2. ``resolve_join`` per-pair: every ``on[*].source_column`` and
       ``target_column`` must be redacted by the same rule.
    3. ``resolve_join.sql_skeleton`` must be assembled FROM the
       redacted pair list — never from the raw column names.
    4. The default ``pii_block=frozenset()`` must still floor on the
       catastrophic triple (credential / payment_card / government_id).

Repro shape (pure in-process via the SchemaBrain store):

  1. Seed ``public.users`` with a credential-tagged ``password_hash``
     plus a regular ``id`` PK + safe ``email``.
  2. Seed ``public.sessions`` with an FK whose source column
     ``user_password_hash`` is itself credential-tagged and points
     at ``users.password_hash``.
  3. Call ``suggest_joins_impl`` and ``resolve_join_impl`` against
     this graph.
  4. Assert no catastrophic column name appears in any public field
     of either response — including the resolve_join sql_skeleton
     string.
"""

from __future__ import annotations

import pytest

from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.join import CanonicalJoin, JoinColumnPair
from schemabrain.core.models import Column, ForeignKey, Table
from schemabrain.mcp.resolve_join import resolve_join_impl
from schemabrain.mcp.suggest_joins import suggest_joins_impl

pytestmark = [pytest.mark.firewall_bypass]

SOURCE_ID = "join_fk_leak"


def _seed_users_with_credential_column(store) -> None:
    """``public.users`` with a credential-tagged ``password_hash``."""
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
        tags={"password_hash": ("pii", frozenset({"credential"}))},
    )


def _seed_sessions_with_credential_fk(store) -> None:
    """``public.sessions`` whose FK source column is itself catastrophic."""
    sessions = Table(
        name="sessions",
        schema_name="public",
        columns=(
            Column(
                name="id",
                table_name="sessions",
                schema_name="public",
                data_type="bigint",
                nullable=False,
                ordinal_position=1,
                is_primary_key=True,
            ),
            Column(
                name="user_password_hash",
                table_name="sessions",
                schema_name="public",
                data_type="text",
                nullable=False,
                ordinal_position=2,
            ),
        ),
        foreign_keys=(
            ForeignKey(
                name="sessions_user_password_hash_fkey",
                source_columns=("user_password_hash",),
                target_schema="public",
                target_table="users",
                target_columns=("password_hash",),
            ),
        ),
    )
    store.write_table(sessions, source_connection_id=SOURCE_ID)
    store.write_column_pii_tags(
        source_connection_id=SOURCE_ID,
        qualified_table="public.sessions",
        tags={"user_password_hash": ("pii", frozenset({"credential"}))},
    )


def _write_session_user_entities_and_join(store) -> None:
    """Entities + canonical join referencing the catastrophic FK columns."""
    store.write_entity(
        Entity(
            name="session",
            description="",
            binding=SingleTableBinding(qualified_table="public.sessions"),
            identity="id",
        ),
        source_connection_id=SOURCE_ID,
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
    store.write_canonical_join(
        CanonicalJoin(
            name="session_user_via_password",
            description="Synthetic: tests the catastrophic-FK-column redaction path.",
            source_entity="session",
            target_entity="user",
            on=(
                JoinColumnPair(
                    source_column="user_password_hash",
                    target_column="password_hash",
                ),
            ),
        ),
        source_connection_id=SOURCE_ID,
    )


CATASTROPHIC_NAMES = ("password_hash", "user_password_hash")


def _assert_no_catastrophic_substring_in(value: str | list[str], context: str) -> None:
    """Strict substring check: catastrophic column names must not appear
    anywhere in the given value, regardless of formatting."""
    haystack = value if isinstance(value, str) else " ".join(value)
    for name in CATASTROPHIC_NAMES:
        assert name not in haystack, (
            f"join_fk_leak regression: {name!r} leaked in {context}: {haystack!r}"
        )


def test_suggest_joins_redacts_catastrophic_fk_columns_on_both_sides(store) -> None:
    """suggest_joins between sessions and users must mask BOTH the
    source-side FK column (sessions.user_password_hash) AND the
    target-side referenced column (users.password_hash)."""
    _seed_users_with_credential_column(store)
    _seed_sessions_with_credential_fk(store)

    result = suggest_joins_impl(
        store=store,
        source_connection_id=SOURCE_ID,
        tables=["public.sessions", "public.users"],
    )

    assert len(result.paths) == 1
    edge = result.paths[0].edges[0]
    # Both sides carry catastrophic-tagged columns — both must mask.
    _assert_no_catastrophic_substring_in(edge.left_columns, "edge.left_columns")
    _assert_no_catastrophic_substring_in(edge.right_columns, "edge.right_columns")
    # Strict shape: every entry is the placeholder.
    assert edge.left_columns == ["<redacted_column>"]
    assert edge.right_columns == ["<redacted_column>"]


def test_suggest_joins_default_pii_block_still_floors(store) -> None:
    """A caller that omits pii_block (default frozenset()) must still
    see the catastrophic floor enforced. The floor IS the contract."""
    _seed_users_with_credential_column(store)
    _seed_sessions_with_credential_fk(store)

    result = suggest_joins_impl(
        store=store,
        source_connection_id=SOURCE_ID,
        tables=["public.sessions", "public.users"],
        # pii_block intentionally omitted.
    )
    edge = result.paths[0].edges[0]
    _assert_no_catastrophic_substring_in(edge.left_columns, "edge.left_columns (default)")
    _assert_no_catastrophic_substring_in(edge.right_columns, "edge.right_columns (default)")


def test_resolve_join_redacts_pair_and_skeleton(store) -> None:
    """resolve_join between session and user must mask BOTH
    on[*].source_column / target_column AND the sql_skeleton string."""
    _seed_users_with_credential_column(store)
    _seed_sessions_with_credential_fk(store)
    _write_session_user_entities_and_join(store)

    info = resolve_join_impl(
        store=store,
        source_connection_id=SOURCE_ID,
        entity_a="session",
        entity_b="user",
    )

    # The pair list must mask.
    assert info.on[0].source_column == "<redacted_column>"
    assert info.on[0].target_column == "<redacted_column>"
    # The sql_skeleton must NOT contain either catastrophic name.
    _assert_no_catastrophic_substring_in(info.sql_skeleton, "sql_skeleton")
    # And the placeholder must appear in the skeleton — proof the
    # skeleton was assembled from the redacted pairs.
    assert "<redacted_column>" in info.sql_skeleton


def test_resolve_join_default_pii_block_still_floors(store) -> None:
    """Same default-floor invariant for resolve_join's wire surface."""
    _seed_users_with_credential_column(store)
    _seed_sessions_with_credential_fk(store)
    _write_session_user_entities_and_join(store)

    info = resolve_join_impl(
        store=store,
        source_connection_id=SOURCE_ID,
        entity_a="session",
        entity_b="user",
        # pii_block intentionally omitted.
    )
    _assert_no_catastrophic_substring_in(info.sql_skeleton, "sql_skeleton (default)")
    assert info.on[0].source_column == "<redacted_column>"
    assert info.on[0].target_column == "<redacted_column>"
