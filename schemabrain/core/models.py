"""Pydantic models for Schema Brain core entities.

These are the canonical, immutable representations of database objects after
introspection. Connectors produce these; the store persists them; MCP tools
return them.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Column(BaseModel):
    """A single column on a database table.

    Carries enough denormalized parent context (`table_name`, `schema_name`)
    to be self-describing in logs and flat storage, even when read without
    its parent `Table`. The `Table` model enforces that this context matches
    the parent it lives under.
    """

    model_config = ConfigDict(frozen=True)

    name: NonEmptyStr
    table_name: NonEmptyStr
    schema_name: NonEmptyStr
    data_type: NonEmptyStr
    nullable: bool
    ordinal_position: int = Field(..., ge=1)
    default: NonEmptyStr | None = None
    is_primary_key: bool = False


class ForeignKey(BaseModel):
    """A foreign key constraint from one table to another.

    Source columns reside on the table that *owns* this FK. Target columns
    reside on `target_schema.target_table`. For composite FKs the two column
    tuples are positionally aligned and must have equal length, and neither
    side may contain duplicates.
    """

    model_config = ConfigDict(frozen=True)

    name: NonEmptyStr
    source_columns: tuple[NonEmptyStr, ...] = Field(..., min_length=1)
    target_schema: NonEmptyStr
    target_table: NonEmptyStr
    target_columns: tuple[NonEmptyStr, ...] = Field(..., min_length=1)

    @model_validator(mode="after")
    def _validate_columns(self) -> ForeignKey:
        if len(self.source_columns) != len(self.target_columns):
            raise ValueError("source_columns and target_columns must have the same length")
        if len(set(self.source_columns)) != len(self.source_columns):
            raise ValueError("source_columns must not contain duplicates")
        if len(set(self.target_columns)) != len(self.target_columns):
            raise ValueError("target_columns must not contain duplicates")
        return self


class IncomingForeignKey(BaseModel):
    """A foreign key REFERENCING a column we're inspecting (back-reference).

    `source_qualified_name` is the table doing the referencing (`schema.table`
    pre-joined for caller convenience). `source_columns` are its columns
    that hold the foreign-key values; `target_columns` are the columns on
    the OTHER table — the one being inspected — that those values point at.

    Used by `describe_column` to surface "which other tables join into me
    on this column?" without forcing the caller to scan every other
    table's outgoing FKs.
    """

    model_config = ConfigDict(frozen=True)

    name: NonEmptyStr
    source_qualified_name: NonEmptyStr
    source_columns: tuple[NonEmptyStr, ...] = Field(..., min_length=1)
    target_columns: tuple[NonEmptyStr, ...] = Field(..., min_length=1)

    @model_validator(mode="after")
    def _validate_columns(self) -> IncomingForeignKey:
        if len(self.source_columns) != len(self.target_columns):
            raise ValueError("source_columns and target_columns must have the same length")
        if len(set(self.source_columns)) != len(self.source_columns):
            raise ValueError("source_columns must not contain duplicates")
        if len(set(self.target_columns)) != len(self.target_columns):
            raise ValueError("target_columns must not contain duplicates")
        return self


class Table(BaseModel):
    """A database table with its columns and outgoing foreign keys.

    Enforces these invariants on construction:
    - Every column's denormalized parent context (`table_name`, `schema_name`)
      matches this table's `name` and `schema_name`.
    - Column names are unique within the table.
    - Column ordinal positions are unique within the table.
    - Every FK source column refers to a column that exists on this table.
    """

    model_config = ConfigDict(frozen=True)

    name: NonEmptyStr
    schema_name: NonEmptyStr
    columns: tuple[Column, ...] = ()
    foreign_keys: tuple[ForeignKey, ...] = ()

    @model_validator(mode="after")
    def _validate_invariants(self) -> Table:
        for col in self.columns:
            if col.table_name != self.name or col.schema_name != self.schema_name:
                raise ValueError(
                    f"Column {col.name!r} parent context "
                    f"({col.schema_name}.{col.table_name}) does not match "
                    f"table ({self.schema_name}.{self.name})"
                )

        names = [col.name for col in self.columns]
        if len(names) != len(set(names)):
            duplicates = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(f"Duplicate column names: {duplicates}")

        positions = [col.ordinal_position for col in self.columns]
        if len(positions) != len(set(positions)):
            duplicates = sorted({p for p in positions if positions.count(p) > 1})
            raise ValueError(f"Duplicate ordinal positions: {duplicates}")

        column_names = set(names)
        for fk in self.foreign_keys:
            missing = set(fk.source_columns) - column_names
            if missing:
                raise ValueError(
                    f"Foreign key {fk.name!r} references unknown source columns: {sorted(missing)}"
                )

        return self

    @property
    def qualified_name(self) -> str:
        return f"{self.schema_name}.{self.name}"

    def get_column(self, name: str) -> Column | None:
        for col in self.columns:
            if col.name == name:
                return col
        return None

    def primary_key_columns(self) -> tuple[str, ...]:
        return tuple(col.name for col in self.columns if col.is_primary_key)

    def is_junction_table(self) -> bool:
        """True if this table looks like an M:N association table.

        A junction table represents a many-to-many association between
        two or more other tables. Joining other tables through it
        multiplies result rows — a frequent source of double-counting
        bugs in aggregate queries.

        Heuristic (conservative):

        - Composite primary key of >= 2 columns
        - Every primary-key column is the source of some outgoing FK
        - Those FKs target >= 2 distinct tables (qualified names)

        Tables with extra attribute columns on the association (a
        `role` field, a `created_at`, etc.) still qualify — only the PK
        coverage matters. Self-referential association tables where
        both FK columns point at the same target table do NOT qualify
        under this heuristic; they're a real edge case but uncommon
        enough to defer.
        """
        pk_cols = set(self.primary_key_columns())
        if len(pk_cols) < 2:
            return False
        fk_source_columns: set[str] = set()
        targets: set[str] = set()
        for fk in self.foreign_keys:
            if not any(col in pk_cols for col in fk.source_columns):
                continue
            fk_source_columns.update(fk.source_columns)
            targets.add(f"{fk.target_schema}.{fk.target_table}")
        if not pk_cols.issubset(fk_source_columns):
            return False
        return len(targets) >= 2

    def junction_target_tables(self) -> tuple[str, ...]:
        """Sorted qualified names of the tables this junction associates.

        Returns an empty tuple if `is_junction_table()` is False. The
        sort makes the prompt rendering deterministic — same invariant
        the description-fingerprint cache depends on.
        """
        if not self.is_junction_table():
            return ()
        pk_cols = set(self.primary_key_columns())
        targets: set[str] = set()
        for fk in self.foreign_keys:
            if not any(col in pk_cols for col in fk.source_columns):
                continue
            targets.add(f"{fk.target_schema}.{fk.target_table}")
        return tuple(sorted(targets))
