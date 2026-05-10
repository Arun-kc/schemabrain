"""Tests for content-addressable fingerprint hashing.

Fingerprints are the cache key for LLM-enriched descriptions. They MUST be:
- Deterministic across Python invocations (no dict iteration, no salts).
- Sensitive to every field that should invalidate enrichment.
- Insensitive to the input ordering of inputs that should NOT invalidate
  (e.g. FK target ordering, sample value ordering — the data is the same).
- Stable across releases (golden hashes lock the serialization format).
"""

from __future__ import annotations

from schemabrain.core.fingerprint import (
    column_semantic_fingerprint,
    column_structural_fingerprint,
    fk_targets_for_column,
    table_structural_fingerprint,
)
from schemabrain.core.models import Column, ForeignKey, Table
from schemabrain.profiler.stats import ColumnStats

# Test sentinel for the prompt-version arg. Real callers pass
# `schemabrain.enrichment.prompts.PROMPT_VERSION`; here we keep the
# tests independent of the live prompt module so we can vary the value
# explicitly when testing prompt-version invalidation.
_PV = "test_v"


def _make_column(
    name: str = "id",
    *,
    table_name: str = "users",
    schema_name: str = "public",
    data_type: str = "BIGINT",
    nullable: bool = False,
    ordinal_position: int = 1,
    default: str | None = None,
    is_primary_key: bool = True,
) -> Column:
    return Column(
        name=name,
        table_name=table_name,
        schema_name=schema_name,
        data_type=data_type,
        nullable=nullable,
        ordinal_position=ordinal_position,
        default=default,
        is_primary_key=is_primary_key,
    )


def _make_table(
    *,
    name: str = "users",
    schema_name: str = "public",
    columns: tuple[Column, ...] = (),
    foreign_keys: tuple[ForeignKey, ...] = (),
) -> Table:
    if not columns:
        columns = (_make_column("id", table_name=name, schema_name=schema_name),)
    return Table(
        name=name,
        schema_name=schema_name,
        columns=columns,
        foreign_keys=foreign_keys,
    )


class TestFkTargetsForColumn:
    def test_no_fks_returns_empty_tuple(self) -> None:
        table = _make_table()
        assert fk_targets_for_column(table, "id") == ()

    def test_single_column_fk_returns_one_target(self) -> None:
        cols = (
            _make_column("id", table_name="orders"),
            _make_column(
                "user_id",
                table_name="orders",
                ordinal_position=2,
                is_primary_key=False,
            ),
        )
        fks = (
            ForeignKey(
                name="orders_user_id_fkey",
                source_columns=("user_id",),
                target_schema="public",
                target_table="users",
                target_columns=("id",),
            ),
        )
        table = _make_table(name="orders", columns=cols, foreign_keys=fks)
        assert fk_targets_for_column(table, "user_id") == ("public.users.id",)

    def test_composite_fk_pairs_source_and_target_positionally(self) -> None:
        cols = (
            _make_column("org_id", table_name="org_members", is_primary_key=True),
            _make_column(
                "user_id",
                table_name="org_members",
                ordinal_position=2,
                is_primary_key=True,
            ),
        )
        fks = (
            ForeignKey(
                name="org_members_to_membership",
                source_columns=("org_id", "user_id"),
                target_schema="public",
                target_table="memberships",
                target_columns=("organization_id", "member_id"),
            ),
        )
        table = _make_table(name="org_members", columns=cols, foreign_keys=fks)
        assert fk_targets_for_column(table, "org_id") == ("public.memberships.organization_id",)
        assert fk_targets_for_column(table, "user_id") == ("public.memberships.member_id",)

    def test_column_with_multiple_fks_returns_all_sorted(self) -> None:
        cols = (
            _make_column(
                "user_id",
                table_name="audit",
                is_primary_key=False,
            ),
        )
        fks = (
            ForeignKey(
                name="audit_to_users",
                source_columns=("user_id",),
                target_schema="public",
                target_table="users",
                target_columns=("id",),
            ),
            ForeignKey(
                name="audit_to_archived_users",
                source_columns=("user_id",),
                target_schema="archive",
                target_table="users",
                target_columns=("legacy_id",),
            ),
        )
        table = _make_table(name="audit", columns=cols, foreign_keys=fks)
        # Sorted lexicographically: 'archive.users.legacy_id' < 'public.users.id'
        assert fk_targets_for_column(table, "user_id") == (
            "archive.users.legacy_id",
            "public.users.id",
        )

    def test_unknown_column_returns_empty_tuple(self) -> None:
        table = _make_table()
        assert fk_targets_for_column(table, "nonexistent") == ()


class TestColumnStructuralFingerprint:
    def test_returns_64_char_hex(self) -> None:
        col = _make_column()
        fp = column_structural_fingerprint(col, ())
        assert len(fp) == 64
        assert all(c in "0123456789abcdef" for c in fp)

    def test_same_inputs_produce_same_fingerprint(self) -> None:
        col_a = _make_column()
        col_b = _make_column()
        assert column_structural_fingerprint(col_a, ()) == column_structural_fingerprint(col_b, ())

    def test_name_change_invalidates(self) -> None:
        a = column_structural_fingerprint(_make_column("id"), ())
        b = column_structural_fingerprint(_make_column("user_id"), ())
        assert a != b

    def test_data_type_change_invalidates(self) -> None:
        a = column_structural_fingerprint(_make_column(data_type="BIGINT"), ())
        b = column_structural_fingerprint(_make_column(data_type="INTEGER"), ())
        assert a != b

    def test_nullable_change_invalidates(self) -> None:
        a = column_structural_fingerprint(_make_column(nullable=False), ())
        b = column_structural_fingerprint(_make_column(nullable=True), ())
        assert a != b

    def test_default_change_invalidates(self) -> None:
        a = column_structural_fingerprint(_make_column(default=None), ())
        b = column_structural_fingerprint(_make_column(default="42"), ())
        assert a != b

    def test_default_none_distinct_from_string_none(self) -> None:
        # Defensive: default=None must produce a different hash from
        # default='None' (the literal four-character string), since one
        # represents "no default" and the other a literal default value.
        a = column_structural_fingerprint(_make_column(default=None), ())
        b = column_structural_fingerprint(_make_column(default="None"), ())
        assert a != b

    def test_position_change_invalidates(self) -> None:
        a = column_structural_fingerprint(_make_column(ordinal_position=1), ())
        b = column_structural_fingerprint(_make_column(ordinal_position=2), ())
        assert a != b

    def test_is_primary_key_change_invalidates(self) -> None:
        a = column_structural_fingerprint(_make_column(is_primary_key=False), ())
        b = column_structural_fingerprint(_make_column(is_primary_key=True), ())
        assert a != b

    def test_fk_targets_change_invalidates(self) -> None:
        col = _make_column("user_id", is_primary_key=False)
        a = column_structural_fingerprint(col, ())
        b = column_structural_fingerprint(col, ("public.users.id",))
        assert a != b

    def test_fk_targets_order_does_not_affect_fingerprint(self) -> None:
        col = _make_column("user_id", is_primary_key=False)
        a = column_structural_fingerprint(col, ("a.t.c", "b.t.c", "c.t.c"))
        b = column_structural_fingerprint(col, ("c.t.c", "a.t.c", "b.t.c"))
        assert a == b

    def test_golden_hash_locks_serialization_format(self) -> None:
        # If this hash ever changes, every previously cached enrichment
        # silently invalidates. That is a breaking change and must be
        # accompanied by a schema_version bump in the store.
        col = Column(
            name="email",
            table_name="users",
            schema_name="public",
            data_type="TEXT",
            nullable=False,
            ordinal_position=2,
            default=None,
            is_primary_key=False,
        )
        fp = column_structural_fingerprint(col, ("public.contacts.email",))
        assert fp == "ecc0542ecf4002a4d11c23c3dc2ad080bd84f49536b47aade000012c8e6f56be"


class TestColumnSemanticFingerprint:
    def test_returns_64_char_hex(self) -> None:
        stats = ColumnStats(
            column_name="email",
            total_rows=10,
            null_count=0,
            distinct_count=10,
            sample_values=("a", "b"),
        )
        fp = column_semantic_fingerprint(_make_column(), (), stats, _PV)
        assert len(fp) == 64

    def test_includes_structural_signal(self) -> None:
        # Changing a structural field must change the semantic fingerprint
        # because the semantic hash incorporates the structural one.
        stats = ColumnStats(
            column_name="x",
            total_rows=1,
            null_count=0,
            distinct_count=1,
            sample_values=("a",),
        )
        a = column_semantic_fingerprint(_make_column(data_type="TEXT"), (), stats, _PV)
        b = column_semantic_fingerprint(_make_column(data_type="BIGINT"), (), stats, _PV)
        assert a != b

    def test_sample_change_invalidates(self) -> None:
        col = _make_column()
        stats_a = ColumnStats(
            column_name="x",
            total_rows=1,
            null_count=0,
            distinct_count=1,
            sample_values=("a",),
        )
        stats_b = ColumnStats(
            column_name="x",
            total_rows=1,
            null_count=0,
            distinct_count=1,
            sample_values=("b",),
        )
        a = column_semantic_fingerprint(col, (), stats_a, _PV)
        b = column_semantic_fingerprint(col, (), stats_b, _PV)
        assert a != b

    def test_sample_order_does_not_affect_fingerprint(self) -> None:
        col = _make_column()
        stats_a = ColumnStats(
            column_name="x",
            total_rows=2,
            null_count=0,
            distinct_count=2,
            sample_values=("a", "b"),
        )
        stats_b = ColumnStats(
            column_name="x",
            total_rows=2,
            null_count=0,
            distinct_count=2,
            sample_values=("b", "a"),
        )
        a = column_semantic_fingerprint(col, (), stats_a, _PV)
        b = column_semantic_fingerprint(col, (), stats_b, _PV)
        assert a == b

    def test_none_stats_distinct_from_empty_stats(self) -> None:
        col = _make_column()
        empty_stats = ColumnStats(
            column_name="x",
            total_rows=0,
            null_count=0,
            distinct_count=0,
        )
        a = column_semantic_fingerprint(col, (), None, _PV)
        b = column_semantic_fingerprint(col, (), empty_stats, _PV)
        assert a != b

    def test_shape_change_invalidates(self) -> None:
        # Locked-in promise: shape_patterns are part of the semantic
        # fingerprint. A column that flips from `999-99-9999` shapes to
        # `aaa@aaaa.aaa` shapes is a meaning shift even if the literal
        # samples don't overlap, and must re-enrich.
        col = _make_column()
        stats_a = ColumnStats(
            column_name="x",
            total_rows=2,
            null_count=0,
            distinct_count=2,
            sample_values=("foo", "bar"),
            shape_patterns=("999-99-9999",),
        )
        stats_b = ColumnStats(
            column_name="x",
            total_rows=2,
            null_count=0,
            distinct_count=2,
            sample_values=("foo", "bar"),
            shape_patterns=("aaa@aaaa.aaa",),
        )
        assert column_semantic_fingerprint(col, (), stats_a, _PV) != column_semantic_fingerprint(
            col, (), stats_b, _PV
        )

    def test_shape_pattern_order_does_not_affect_fingerprint(self) -> None:
        col = _make_column()
        stats_a = ColumnStats(
            column_name="x",
            total_rows=3,
            null_count=0,
            distinct_count=3,
            sample_values=("a", "b", "c"),
            shape_patterns=("a", "9", "A"),
        )
        stats_b = ColumnStats(
            column_name="x",
            total_rows=3,
            null_count=0,
            distinct_count=3,
            sample_values=("a", "b", "c"),
            shape_patterns=("9", "A", "a"),
        )
        assert column_semantic_fingerprint(col, (), stats_a, _PV) == column_semantic_fingerprint(
            col, (), stats_b, _PV
        )

    def test_empty_prompt_version_raises(self) -> None:
        # An empty prompt_version would produce a consistent but
        # meaningless cache key — refuse rather than poison the cache.
        col = _make_column()
        stats = ColumnStats(
            column_name="x",
            total_rows=1,
            null_count=0,
            distinct_count=1,
            sample_values=("a",),
        )
        import pytest

        with pytest.raises(ValueError, match="prompt_version"):
            column_semantic_fingerprint(col, (), stats, "")

    def test_prompt_version_change_invalidates(self) -> None:
        # The whole point of baking prompt_version into the semantic
        # fingerprint: bumping it must re-enrich every cached column,
        # because the prompt template changed and old descriptions are
        # now generated from a different contract.
        col = _make_column()
        stats = ColumnStats(
            column_name="x",
            total_rows=1,
            null_count=0,
            distinct_count=1,
            sample_values=("a",),
        )
        a = column_semantic_fingerprint(col, (), stats, "v1")
        b = column_semantic_fingerprint(col, (), stats, "v2")
        assert a != b

    def test_count_changes_alone_do_not_invalidate(self) -> None:
        # Per the plan, sample values (not raw counts) drive semantic
        # invalidation. A column whose row count grew but whose sample
        # values didn't change must keep its cached enrichment.
        col = _make_column()
        stats_small = ColumnStats(
            column_name="x",
            total_rows=10,
            null_count=2,
            distinct_count=3,
            sample_values=("a", "b", "c"),
        )
        stats_large = ColumnStats(
            column_name="x",
            total_rows=1000,
            null_count=200,
            distinct_count=3,
            sample_values=("a", "b", "c"),
        )
        a = column_semantic_fingerprint(col, (), stats_small, _PV)
        b = column_semantic_fingerprint(col, (), stats_large, _PV)
        assert a == b

    def test_golden_hash_locks_semantic_serialization_format(self) -> None:
        # Companion to the structural golden test. The semantic payload
        # gained a `shapes` field after the first review pass; locking
        # this hash prevents another silent format drift slipping through.
        col = Column(
            name="email",
            table_name="users",
            schema_name="public",
            data_type="TEXT",
            nullable=False,
            ordinal_position=2,
            default=None,
            is_primary_key=False,
        )
        stats = ColumnStats(
            column_name="email",
            total_rows=10,
            null_count=2,
            distinct_count=8,
            sample_values=("a@x.io", "b@x.io"),
            shape_patterns=("a@a.aa",),
        )
        fp = column_semantic_fingerprint(col, ("public.contacts.email",), stats, _PV)
        assert fp == "aea5ae8a4366a0d84a504349e13ab1f6a227b35f8429bd185d3d1f9232be3859"


class TestTableStructuralFingerprint:
    def test_returns_64_char_hex(self) -> None:
        table = _make_table()
        fp = table_structural_fingerprint(table)
        assert len(fp) == 64

    def test_identical_tables_produce_identical_hash(self) -> None:
        a = table_structural_fingerprint(_make_table())
        b = table_structural_fingerprint(_make_table())
        assert a == b

    def test_adding_column_invalidates(self) -> None:
        base = _make_table(
            columns=(_make_column("id", table_name="users"),),
        )
        extended = _make_table(
            columns=(
                _make_column("id", table_name="users"),
                _make_column(
                    "email",
                    table_name="users",
                    data_type="TEXT",
                    nullable=False,
                    ordinal_position=2,
                    is_primary_key=False,
                ),
            ),
        )
        assert table_structural_fingerprint(base) != table_structural_fingerprint(extended)

    def test_dropping_column_invalidates(self) -> None:
        full = _make_table(
            columns=(
                _make_column("id", table_name="users"),
                _make_column(
                    "email",
                    table_name="users",
                    data_type="TEXT",
                    nullable=False,
                    ordinal_position=2,
                    is_primary_key=False,
                ),
            ),
        )
        trimmed = _make_table(columns=(_make_column("id", table_name="users"),))
        assert table_structural_fingerprint(full) != table_structural_fingerprint(trimmed)

    def test_swapping_column_positions_invalidates(self) -> None:
        # If two columns swap their ordinal positions (id moves to
        # position 2, email moves to position 1) that IS a schema change
        # — Postgres lays the table out differently and analytic queries
        # using positional refs will break. Pair with
        # `test_column_tuple_order_does_not_affect_fingerprint`, which
        # locks the *opposite*: reordering the tuple alone (positions
        # unchanged) must NOT invalidate.
        a = _make_table(
            columns=(
                _make_column("id", table_name="users", ordinal_position=1),
                _make_column(
                    "email",
                    table_name="users",
                    data_type="TEXT",
                    nullable=False,
                    ordinal_position=2,
                    is_primary_key=False,
                ),
            ),
        )
        b = _make_table(
            columns=(
                _make_column(
                    "email",
                    table_name="users",
                    data_type="TEXT",
                    nullable=False,
                    ordinal_position=1,
                    is_primary_key=False,
                ),
                _make_column("id", table_name="users", ordinal_position=2),
            ),
        )
        assert table_structural_fingerprint(a) != table_structural_fingerprint(b)

    def test_column_tuple_order_does_not_affect_fingerprint(self) -> None:
        # `Table` validates unique ordinal_position but does NOT enforce
        # that the tuple is sorted. A connector that yields columns in
        # a different tuple order between runs (same logical schema, same
        # positions) must produce the same hash. The fingerprint sorts
        # internally by ordinal_position to guarantee that.
        col_id = _make_column("id", table_name="users", ordinal_position=1)
        col_email = _make_column(
            "email",
            table_name="users",
            data_type="TEXT",
            nullable=False,
            ordinal_position=2,
            is_primary_key=False,
        )
        forward = _make_table(columns=(col_id, col_email))
        reverse = _make_table(columns=(col_email, col_id))
        assert table_structural_fingerprint(forward) == table_structural_fingerprint(reverse)

    def test_schema_name_change_invalidates(self) -> None:
        a = table_structural_fingerprint(
            _make_table(
                schema_name="public",
                columns=(_make_column("id", schema_name="public"),),
            )
        )
        b = table_structural_fingerprint(
            _make_table(
                schema_name="audit",
                columns=(_make_column("id", schema_name="audit"),),
            )
        )
        assert a != b

    def test_adding_fk_invalidates(self) -> None:
        cols = (
            _make_column("id", table_name="orders"),
            _make_column(
                "user_id",
                table_name="orders",
                ordinal_position=2,
                is_primary_key=False,
            ),
        )
        no_fk = _make_table(name="orders", columns=cols, foreign_keys=())
        with_fk = _make_table(
            name="orders",
            columns=cols,
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
        assert table_structural_fingerprint(no_fk) != table_structural_fingerprint(with_fk)

    def test_golden_hash_locks_table_serialization(self) -> None:
        # Same intent as the column golden hash: lock in the serialization
        # so an accidental format change is caught immediately.
        cols = (
            Column(
                name="id",
                table_name="users",
                schema_name="public",
                data_type="BIGINT",
                nullable=False,
                ordinal_position=1,
                default=None,
                is_primary_key=True,
            ),
            Column(
                name="email",
                table_name="users",
                schema_name="public",
                data_type="TEXT",
                nullable=False,
                ordinal_position=2,
                default=None,
                is_primary_key=False,
            ),
        )
        table = Table(name="users", schema_name="public", columns=cols)
        fp = table_structural_fingerprint(table)
        assert fp == "d537c42144ccfb66f4de3542dbd1a6751e57e2bf4664bda9cd992930d257ddf8"
