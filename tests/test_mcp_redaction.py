"""Unit tests for `schemabrain.mcp._redaction`.

Covers the FK column-name scrubbing helper used by `describe_table`
and `describe_column` to mirror the column-list redaction policy onto
FK metadata. Direct unit tests because the helper is a small,
deterministic function on top of `store.get_column_pii_tags`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.mcp._redaction import redact_blocked_fk_columns
from schemabrain.pii import PIICategory

SOURCE_ID = "src1"


def _col(name: str, *, table_name: str, ordinal: int) -> Column:
    return Column(
        name=name,
        table_name=table_name,
        schema_name="public",
        data_type="TEXT",
        nullable=True,
        ordinal_position=ordinal,
    )


@pytest.fixture
def store_with_tagged_users(tmp_path: Path) -> SQLiteStore:
    """``public.users(id, ssn)`` with ``ssn`` tagged ``government_id``."""
    store = SQLiteStore(tmp_path / "store.db")
    users = Table(
        name="users",
        schema_name="public",
        columns=(
            _col("id", table_name="users", ordinal=1),
            _col("ssn", table_name="users", ordinal=2),
            _col("email", table_name="users", ordinal=3),
        ),
    )
    store.write_table(users, source_connection_id=SOURCE_ID)
    store.write_column_pii_tags(
        source_connection_id=SOURCE_ID,
        qualified_table="public.users",
        tags={
            "ssn": ("pii", frozenset({"government_id"})),
            "email": ("pii", frozenset({"contact"})),
        },
    )
    return store


class TestRedactBlockedFkColumns:
    def test_empty_input_short_circuits(self, store_with_tagged_users: SQLiteStore) -> None:
        """Empty column list returns unchanged without hitting the store."""
        result = redact_blocked_fk_columns(
            [],
            qualified_table="public.users",
            store=store_with_tagged_users,
            source_connection_id=SOURCE_ID,
            effective_block=frozenset({"government_id"}),
        )
        assert result == []

    def test_blocked_column_masked_to_placeholder(
        self, store_with_tagged_users: SQLiteStore
    ) -> None:
        """A column whose tags intersect ``effective_block`` is masked."""
        result = redact_blocked_fk_columns(
            ["ssn"],
            qualified_table="public.users",
            store=store_with_tagged_users,
            source_connection_id=SOURCE_ID,
            effective_block=frozenset({"government_id"}),
        )
        assert result == ["<redacted_column>"]

    def test_unblocked_column_passes_through(
        self, store_with_tagged_users: SQLiteStore
    ) -> None:
        """A column whose tags don't intersect ``effective_block`` is
        returned unchanged."""
        result = redact_blocked_fk_columns(
            ["id"],
            qualified_table="public.users",
            store=store_with_tagged_users,
            source_connection_id=SOURCE_ID,
            effective_block=frozenset({"government_id"}),
        )
        assert result == ["id"]

    def test_mixed_blocked_and_unblocked_preserves_order(
        self, store_with_tagged_users: SQLiteStore
    ) -> None:
        """A composite FK with one blocked + one unblocked column gets a
        per-column mask, preserving positional order so the join shape
        stays interpretable to the agent."""
        result = redact_blocked_fk_columns(
            ["id", "ssn", "email"],
            qualified_table="public.users",
            store=store_with_tagged_users,
            source_connection_id=SOURCE_ID,
            effective_block=frozenset({"government_id"}),
        )
        assert result == ["id", "<redacted_column>", "email"]

    def test_unknown_column_treated_as_public(
        self, store_with_tagged_users: SQLiteStore
    ) -> None:
        """A column the store has no tag-row for defaults to ``public`` and
        therefore passes through. Matches the propagation helper's
        empty-input contract."""
        result = redact_blocked_fk_columns(
            ["never_indexed"],
            qualified_table="public.users",
            store=store_with_tagged_users,
            source_connection_id=SOURCE_ID,
            effective_block=frozenset({"government_id"}),
        )
        assert result == ["never_indexed"]

    def test_effective_block_with_no_overlap_is_no_op(
        self, store_with_tagged_users: SQLiteStore
    ) -> None:
        """A tagged column whose categories don't intersect the
        operator's effective block set passes through. Pins the
        ``categories & effective_block`` semantics."""
        block: frozenset[PIICategory] = frozenset({"payment_card"})
        result = redact_blocked_fk_columns(
            ["ssn", "email"],
            qualified_table="public.users",
            store=store_with_tagged_users,
            source_connection_id=SOURCE_ID,
            effective_block=block,
        )
        assert result == ["ssn", "email"]
