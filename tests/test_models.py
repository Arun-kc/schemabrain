"""Tests for schemabrain.core.models."""

import pytest
from pydantic import ValidationError

from schemabrain.core.models import Column, ForeignKey, Table


def _column(
    name: str,
    table_name: str = "users",
    schema_name: str = "public",
    data_type: str = "text",
    nullable: bool = True,
    ordinal_position: int = 1,
    is_primary_key: bool = False,
) -> Column:
    return Column(
        name=name,
        table_name=table_name,
        schema_name=schema_name,
        data_type=data_type,
        nullable=nullable,
        ordinal_position=ordinal_position,
        is_primary_key=is_primary_key,
    )


class TestColumn:
    def test_construction_with_required_fields(self):
        col = _column("id", data_type="integer", nullable=False, ordinal_position=1)
        assert col.name == "id"
        assert col.table_name == "users"
        assert col.schema_name == "public"
        assert col.data_type == "integer"
        assert col.nullable is False
        assert col.ordinal_position == 1
        assert col.default is None
        assert col.is_primary_key is False

    def test_with_default_and_primary_key(self):
        col = Column(
            name="id",
            table_name="users",
            schema_name="public",
            data_type="bigint",
            nullable=False,
            ordinal_position=1,
            default="nextval('users_id_seq')",
            is_primary_key=True,
        )
        assert col.default == "nextval('users_id_seq')"
        assert col.is_primary_key is True

    def test_immutable(self):
        col = _column("id", data_type="integer", nullable=False, ordinal_position=1)
        with pytest.raises(ValidationError):
            col.name = "renamed"  # type: ignore[misc]

    def test_rejects_zero_ordinal_position(self):
        with pytest.raises(ValidationError):
            _column("id", data_type="integer", nullable=False, ordinal_position=0)

    def test_rejects_negative_ordinal_position(self):
        with pytest.raises(ValidationError):
            _column("id", data_type="integer", nullable=False, ordinal_position=-1)

    def test_rejects_empty_name(self):
        with pytest.raises(ValidationError):
            _column("", data_type="integer", nullable=False, ordinal_position=1)

    def test_rejects_whitespace_only_name(self):
        with pytest.raises(ValidationError):
            _column("   ", data_type="integer", nullable=False, ordinal_position=1)

    def test_rejects_whitespace_only_data_type(self):
        with pytest.raises(ValidationError):
            _column("id", data_type="   ", nullable=False, ordinal_position=1)

    def test_rejects_whitespace_only_default(self):
        with pytest.raises(ValidationError):
            Column(
                name="id",
                table_name="users",
                schema_name="public",
                data_type="integer",
                nullable=False,
                ordinal_position=1,
                default="   ",
            )

    def test_strips_surrounding_whitespace_from_strings(self):
        col = _column(
            "  id  ",
            table_name="  users  ",
            data_type="  integer  ",
            nullable=False,
            ordinal_position=1,
        )
        assert col.name == "id"
        assert col.table_name == "users"
        assert col.data_type == "integer"


class TestForeignKey:
    def test_construction_single_column(self):
        fk = ForeignKey(
            name="users_org_id_fkey",
            source_columns=("org_id",),
            target_schema="public",
            target_table="orgs",
            target_columns=("id",),
        )
        assert fk.name == "users_org_id_fkey"
        assert fk.source_columns == ("org_id",)
        assert fk.target_schema == "public"
        assert fk.target_table == "orgs"
        assert fk.target_columns == ("id",)

    def test_composite_columns(self):
        fk = ForeignKey(
            name="orders_composite_fkey",
            source_columns=("tenant_id", "user_id"),
            target_schema="public",
            target_table="tenant_users",
            target_columns=("tenant_id", "id"),
        )
        assert fk.source_columns == ("tenant_id", "user_id")
        assert fk.target_columns == ("tenant_id", "id")

    def test_rejects_mismatched_column_count(self):
        with pytest.raises(ValidationError, match="same length"):
            ForeignKey(
                name="bad_fkey",
                source_columns=("a", "b"),
                target_schema="public",
                target_table="t",
                target_columns=("x",),
            )

    def test_rejects_empty_source_columns(self):
        with pytest.raises(ValidationError):
            ForeignKey(
                name="bad_fkey",
                source_columns=(),
                target_schema="public",
                target_table="t",
                target_columns=(),
            )

    def test_rejects_duplicate_source_columns(self):
        with pytest.raises(ValidationError, match="source_columns must not contain duplicates"):
            ForeignKey(
                name="bad_fkey",
                source_columns=("a", "a"),
                target_schema="public",
                target_table="t",
                target_columns=("x", "y"),
            )

    def test_rejects_duplicate_target_columns(self):
        with pytest.raises(ValidationError, match="target_columns must not contain duplicates"):
            ForeignKey(
                name="bad_fkey",
                source_columns=("a", "b"),
                target_schema="public",
                target_table="t",
                target_columns=("x", "x"),
            )

    def test_rejects_whitespace_only_column_name(self):
        with pytest.raises(ValidationError):
            ForeignKey(
                name="bad_fkey",
                source_columns=("   ",),
                target_schema="public",
                target_table="t",
                target_columns=("x",),
            )

    def test_immutable(self):
        fk = ForeignKey(
            name="users_org_id_fkey",
            source_columns=("org_id",),
            target_schema="public",
            target_table="orgs",
            target_columns=("id",),
        )
        with pytest.raises(ValidationError):
            fk.target_table = "other"  # type: ignore[misc]


class TestTable:
    def _id_col(self) -> Column:
        return _column(
            "id", data_type="bigint", nullable=False, ordinal_position=1, is_primary_key=True
        )

    def _email_col(self) -> Column:
        return _column("email", data_type="text", nullable=False, ordinal_position=2)

    def _org_id_col(self) -> Column:
        return _column("org_id", data_type="bigint", nullable=False, ordinal_position=3)

    def test_construction_minimal(self):
        table = Table(name="users", schema_name="public")
        assert table.name == "users"
        assert table.schema_name == "public"
        assert table.columns == ()
        assert table.foreign_keys == ()

    def test_construction_with_columns_and_fk(self):
        table = Table(
            name="users",
            schema_name="public",
            columns=(self._id_col(), self._email_col(), self._org_id_col()),
            foreign_keys=(
                ForeignKey(
                    name="users_org_id_fkey",
                    source_columns=("org_id",),
                    target_schema="public",
                    target_table="orgs",
                    target_columns=("id",),
                ),
            ),
        )
        assert len(table.columns) == 3
        assert len(table.foreign_keys) == 1

    def test_qualified_name(self):
        table = Table(name="users", schema_name="public")
        assert table.qualified_name == "public.users"

    def test_qualified_name_non_default_schema(self):
        table = Table(name="audit_log", schema_name="audit")
        assert table.qualified_name == "audit.audit_log"

    def test_get_column_returns_match(self):
        table = Table(
            name="users",
            schema_name="public",
            columns=(self._id_col(), self._email_col()),
        )
        col = table.get_column("email")
        assert col is not None
        assert col.name == "email"

    def test_get_column_returns_none_for_missing(self):
        table = Table(name="users", schema_name="public", columns=(self._id_col(),))
        assert table.get_column("does_not_exist") is None

    def test_primary_key_columns(self):
        table = Table(
            name="users",
            schema_name="public",
            columns=(self._id_col(), self._email_col()),
        )
        assert table.primary_key_columns() == ("id",)

    def test_primary_key_columns_empty_when_no_pk(self):
        table = Table(name="users", schema_name="public", columns=(self._email_col(),))
        assert table.primary_key_columns() == ()

    def _make_junction_table(self) -> Table:
        """Build org_members: composite PK (org_id, user_id), both FK out."""
        org_id = _column(
            "org_id",
            table_name="org_members",
            data_type="bigint",
            nullable=False,
            ordinal_position=1,
            is_primary_key=True,
        )
        user_id = _column(
            "user_id",
            table_name="org_members",
            data_type="bigint",
            nullable=False,
            ordinal_position=2,
            is_primary_key=True,
        )
        role = _column(
            "role", table_name="org_members", data_type="text", nullable=True, ordinal_position=3
        )
        return Table(
            name="org_members",
            schema_name="public",
            columns=(org_id, user_id, role),
            foreign_keys=(
                ForeignKey(
                    name="om_org_id_fkey",
                    source_columns=("org_id",),
                    target_schema="public",
                    target_table="orgs",
                    target_columns=("id",),
                ),
                ForeignKey(
                    name="om_user_id_fkey",
                    source_columns=("user_id",),
                    target_schema="public",
                    target_table="users",
                    target_columns=("id",),
                ),
            ),
        )

    def test_is_junction_table_true_for_classic_pattern(self):
        table = self._make_junction_table()
        assert table.is_junction_table() is True

    def test_is_junction_table_false_for_single_column_pk(self):
        table = Table(
            name="users", schema_name="public", columns=(self._id_col(), self._email_col())
        )
        assert table.is_junction_table() is False

    def test_is_junction_table_false_when_no_pk(self):
        table = Table(name="users", schema_name="public", columns=(self._email_col(),))
        assert table.is_junction_table() is False

    def test_is_junction_table_false_when_pk_not_fk(self):
        # Composite PK but neither column is a FK — not a junction.
        col_a = _column(
            "a",
            table_name="composite",
            data_type="bigint",
            nullable=False,
            ordinal_position=1,
            is_primary_key=True,
        )
        col_b = _column(
            "b",
            table_name="composite",
            data_type="bigint",
            nullable=False,
            ordinal_position=2,
            is_primary_key=True,
        )
        table = Table(name="composite", schema_name="public", columns=(col_a, col_b))
        assert table.is_junction_table() is False

    def test_is_junction_table_false_when_fks_target_same_table(self):
        # Self-referential composite-FK is an edge case we deliberately
        # exclude — both PK columns FK to the same target.
        parent_id = _column(
            "parent_id",
            table_name="user_hierarchy",
            data_type="bigint",
            nullable=False,
            ordinal_position=1,
            is_primary_key=True,
        )
        child_id = _column(
            "child_id",
            table_name="user_hierarchy",
            data_type="bigint",
            nullable=False,
            ordinal_position=2,
            is_primary_key=True,
        )
        table = Table(
            name="user_hierarchy",
            schema_name="public",
            columns=(parent_id, child_id),
            foreign_keys=(
                ForeignKey(
                    name="parent_fk",
                    source_columns=("parent_id",),
                    target_schema="public",
                    target_table="users",
                    target_columns=("id",),
                ),
                ForeignKey(
                    name="child_fk",
                    source_columns=("child_id",),
                    target_schema="public",
                    target_table="users",
                    target_columns=("id",),
                ),
            ),
        )
        assert table.is_junction_table() is False

    def test_is_junction_table_true_with_attribute_columns(self):
        # Junction with a `role` non-PK attribute column still qualifies.
        table = self._make_junction_table()
        assert any(not col.is_primary_key for col in table.columns)
        assert table.is_junction_table() is True

    def test_junction_target_tables_sorted_and_unique(self):
        table = self._make_junction_table()
        assert table.junction_target_tables() == ("public.orgs", "public.users")

    def test_junction_target_tables_empty_when_not_junction(self):
        table = Table(
            name="users", schema_name="public", columns=(self._id_col(), self._email_col())
        )
        assert table.junction_target_tables() == ()

    def test_rejects_empty_name(self):
        with pytest.raises(ValidationError):
            Table(name="", schema_name="public")

    def test_immutable(self):
        table = Table(name="users", schema_name="public")
        with pytest.raises(ValidationError):
            table.name = "renamed"  # type: ignore[misc]

    def test_rejects_column_with_mismatched_table_name(self):
        bad = _column(
            "id",
            table_name="orders",
            schema_name="public",
            data_type="bigint",
            nullable=False,
            ordinal_position=1,
        )
        with pytest.raises(ValidationError, match="parent context"):
            Table(name="users", schema_name="public", columns=(bad,))

    def test_rejects_column_with_mismatched_schema_name(self):
        bad = _column(
            "id",
            table_name="users",
            schema_name="audit",
            data_type="bigint",
            nullable=False,
            ordinal_position=1,
        )
        with pytest.raises(ValidationError, match="parent context"):
            Table(name="users", schema_name="public", columns=(bad,))

    def test_rejects_duplicate_column_names(self):
        col_a = _column("name", data_type="text", nullable=False, ordinal_position=1)
        col_b = _column("name", data_type="text", nullable=False, ordinal_position=2)
        with pytest.raises(ValidationError, match="Duplicate column names"):
            Table(name="users", schema_name="public", columns=(col_a, col_b))

    def test_rejects_duplicate_ordinal_positions(self):
        col_a = _column("a", data_type="text", nullable=False, ordinal_position=1)
        col_b = _column("b", data_type="text", nullable=False, ordinal_position=1)
        with pytest.raises(ValidationError, match="Duplicate ordinal positions"):
            Table(name="users", schema_name="public", columns=(col_a, col_b))

    def test_rejects_fk_referencing_unknown_column(self):
        fk = ForeignKey(
            name="bad_fkey",
            source_columns=("nonexistent",),
            target_schema="public",
            target_table="orgs",
            target_columns=("id",),
        )
        with pytest.raises(ValidationError, match="references unknown source columns"):
            Table(
                name="users",
                schema_name="public",
                columns=(self._id_col(),),
                foreign_keys=(fk,),
            )


class TestIncomingForeignKey:
    """The back-reference shape used by describe_column. Mirrors
    ForeignKey's column-list validators so a malformed back-reference
    can't sneak through.
    """

    def _ifk(
        self,
        *,
        source_columns: tuple[str, ...] = ("user_id",),
        target_columns: tuple[str, ...] = ("id",),
    ):
        from schemabrain.core.models import IncomingForeignKey

        return IncomingForeignKey(
            name="orders_user_id_fkey",
            source_qualified_name="public.orders",
            source_columns=source_columns,
            target_columns=target_columns,
        )

    def test_round_trips_basic_fields(self) -> None:
        ifk = self._ifk()
        assert ifk.name == "orders_user_id_fkey"
        assert ifk.source_qualified_name == "public.orders"
        assert ifk.source_columns == ("user_id",)
        assert ifk.target_columns == ("id",)

    def test_mismatched_lengths_rejected(self) -> None:
        with pytest.raises(ValueError, match="same length"):
            self._ifk(source_columns=("a", "b"), target_columns=("x",))

    def test_duplicate_source_columns_rejected(self) -> None:
        with pytest.raises(ValueError, match="source_columns must not contain duplicates"):
            self._ifk(source_columns=("a", "a"), target_columns=("x", "y"))

    def test_duplicate_target_columns_rejected(self) -> None:
        with pytest.raises(ValueError, match="target_columns must not contain duplicates"):
            self._ifk(source_columns=("a", "b"), target_columns=("x", "x"))
