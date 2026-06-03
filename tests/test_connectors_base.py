"""Tests for the DataSource Protocol contract."""

from collections.abc import Iterable

from schemabrain.connectors.base import DataSource
from schemabrain.core.models import Table


class _FakeSource:
    """Minimal in-memory DataSource used only to verify the Protocol shape."""

    def __init__(self) -> None:
        self.closed = False

    def list_tables(self, schema: str | None = None) -> Iterable[tuple[str, str]]:
        yield ("public", "fake")

    def get_table(self, name: str, schema: str) -> Table:
        return Table(name=name, schema_name=schema)

    def estimated_row_count(self, name: str, schema: str) -> int | None:
        return None

    def close(self) -> None:
        self.closed = True


def test_fake_satisfies_data_source_protocol():
    fake = _FakeSource()
    assert isinstance(fake, DataSource)


def test_fake_can_be_used_through_protocol_type():
    def consume(source: DataSource) -> list[tuple[str, str]]:
        return list(source.list_tables())

    assert consume(_FakeSource()) == [("public", "fake")]
