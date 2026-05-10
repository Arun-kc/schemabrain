"""PostgreSQL DataSource backed by SQLAlchemy 2.0 reflection."""

from __future__ import annotations

from typing import Any

from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.exc import NoSuchTableError

from schemabrain.connectors.errors import TableNotFoundError
from schemabrain.core.models import Column, ForeignKey, Table

_SYSTEM_SCHEMA_PREFIX = "pg_"
_SYSTEM_SCHEMAS = frozenset({"information_schema"})


class PostgresDataSource:
    """Read-only DataSource for PostgreSQL.

    Skips system schemas (`pg_*` and `information_schema`) when listing
    tables across all schemas. Uses one SQLAlchemy `Engine` per instance;
    `close()` disposes it and is idempotent.
    """

    def __init__(self, url: str) -> None:
        self._engine: Engine | None = create_engine(url)

    def __enter__(self) -> PostgresDataSource:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def list_tables(self, schema: str | None = None) -> list[tuple[str, str]]:
        engine = self._require_engine()
        inspector = inspect(engine)
        if schema is not None:
            return [(schema, name) for name in inspector.get_table_names(schema=schema)]
        result: list[tuple[str, str]] = []
        for sch in inspector.get_schema_names():
            if self._is_system_schema(sch):
                continue
            for table_name in inspector.get_table_names(schema=sch):
                result.append((sch, table_name))
        return result

    def get_table(self, name: str, schema: str) -> Table:
        engine = self._require_engine()
        inspector = inspect(engine)
        try:
            cols_info = inspector.get_columns(name, schema=schema)
        except NoSuchTableError as e:
            raise TableNotFoundError(f"Table {schema}.{name} not found") from e

        pk_info = inspector.get_pk_constraint(name, schema=schema)
        pk_set = set(pk_info.get("constrained_columns") or [])

        columns = tuple(
            self._build_column(idx, raw, name, schema, pk_set) for idx, raw in enumerate(cols_info)
        )

        fk_info = inspector.get_foreign_keys(name, schema=schema)
        foreign_keys = tuple(
            self._build_foreign_key(raw, owning_table=name, owning_schema=schema) for raw in fk_info
        )

        return Table(
            name=name,
            schema_name=schema,
            columns=columns,
            foreign_keys=foreign_keys,
        )

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None

    @staticmethod
    def _is_system_schema(name: str) -> bool:
        return name.startswith(_SYSTEM_SCHEMA_PREFIX) or name in _SYSTEM_SCHEMAS

    @staticmethod
    def _build_column(
        index: int,
        raw: dict[str, Any],
        table_name: str,
        schema_name: str,
        pk_columns: set[str],
    ) -> Column:
        return Column(
            name=raw["name"],
            table_name=table_name,
            schema_name=schema_name,
            data_type=str(raw["type"]),
            nullable=bool(raw["nullable"]),
            ordinal_position=index + 1,
            default=raw.get("default"),
            is_primary_key=raw["name"] in pk_columns,
        )

    @staticmethod
    def _build_foreign_key(
        raw: dict[str, Any],
        *,
        owning_table: str,
        owning_schema: str,
    ) -> ForeignKey:
        source_columns = tuple(raw["constrained_columns"])
        return ForeignKey(
            name=raw.get("name") or f"fk_{owning_table}_{'_'.join(source_columns)}",
            source_columns=source_columns,
            target_schema=raw.get("referred_schema") or owning_schema,
            target_table=raw["referred_table"],
            target_columns=tuple(raw["referred_columns"]),
        )

    def _require_engine(self) -> Engine:
        if self._engine is None:
            raise RuntimeError("PostgresDataSource is closed")
        return self._engine
