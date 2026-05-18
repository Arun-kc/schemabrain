"""Tests for per-tool result extractors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from schemabrain.observability.extractors import (
    default_result_extractor,
    get_result_extractor,
)


@dataclass
class _FakeColumn:
    name: str


@dataclass
class _FakeTable:
    columns: list[_FakeColumn]
    token_estimate: int = 0


@dataclass
class _FakeEntity:
    name: str
    columns: list[_FakeColumn]


@dataclass
class _FakeMetricResult:
    rows: list[dict[str, Any]]
    fingerprint: str


@dataclass
class _FakeJoinInfo:
    name: str
    source_entity: str
    target_entity: str


@dataclass
class _FakeMatches:
    matches: list[Any]


@dataclass
class _FakeJoinsResult:
    paths: list[Any]


@dataclass
class _FakeQueriesResult:
    queries: list[Any]


@dataclass
class _FakeColumnDetail:
    qualified_name: str


class TestDefaultExtractor:
    def test_default_returns_empty_dict(self) -> None:
        assert default_result_extractor(None) == {}
        assert default_result_extractor("anything") == {}
        assert default_result_extractor({"k": "v"}) == {}


class TestRegistryLookup:
    def test_known_tools_have_extractors(self) -> None:
        for name in (
            "find_relevant_tables",
            "describe_table",
            "describe_column",
            "suggest_joins",
            "get_example_queries",
            "list_entities",
            "describe_entity",
            "resolve_join",
            "get_metric",
        ):
            assert get_result_extractor(name) is not default_result_extractor

    def test_unknown_tool_falls_back_to_default(self) -> None:
        assert get_result_extractor("not_a_tool") is default_result_extractor


class TestFindRelevantTables:
    def test_counts_matches(self) -> None:
        # The extractor operates on the envelope's `data` field, which
        # for `find_relevant_tables` is a bare `list[TableHit]` — not
        # an object with a `.matches` attribute. The previous
        # `.matches` probe was a copy-paste bug that always returned
        # `{}`; the fix counts the list directly.
        ext = get_result_extractor("find_relevant_tables")
        assert ext([1, 2, 3]) == {"matches": 3}

    def test_empty_list_counts_zero(self) -> None:
        ext = get_result_extractor("find_relevant_tables")
        assert ext([]) == {"matches": 0}

    def test_data_none(self) -> None:
        ext = get_result_extractor("find_relevant_tables")
        assert ext(None) == {}

    def test_extractor_swallows_unexpected_exceptions(self) -> None:
        """Mirror of the `find_relevant_entities` swallow test. The
        extractor MUST NEVER raise — audit + tail rendering depend on
        it returning `{}` on any unexpected shape.
        """

        class _LenRaiser:
            def __len__(self) -> int:
                raise RuntimeError("synthetic")

        ext = get_result_extractor("find_relevant_tables")
        assert ext(_LenRaiser()) == {}


class TestDescribeTable:
    def test_counts_columns_and_tokens(self) -> None:
        ext = get_result_extractor("describe_table")
        data = _FakeTable(columns=[_FakeColumn("a"), _FakeColumn("b")], token_estimate=380)
        assert ext(data) == {"columns": 2, "tokens": 380}

    def test_missing_token_estimate(self) -> None:
        ext = get_result_extractor("describe_table")
        data = _FakeTable(columns=[_FakeColumn("a")])
        result = ext(data)
        assert result["columns"] == 1


class TestDescribeColumn:
    def test_qualified_name_in_summary(self) -> None:
        ext = get_result_extractor("describe_column")
        result = ext(_FakeColumnDetail(qualified_name="public.users.email"))
        assert result == {"column": "public.users.email"}

    def test_missing_qualified_name(self) -> None:
        ext = get_result_extractor("describe_column")
        assert ext(object()) == {}


class TestSuggestJoins:
    def test_counts_paths(self) -> None:
        ext = get_result_extractor("suggest_joins")
        assert ext(_FakeJoinsResult(paths=["p1", "p2"])) == {"paths": 2}

    def test_missing_paths(self) -> None:
        ext = get_result_extractor("suggest_joins")
        assert ext(object()) == {}


class TestGetExampleQueries:
    def test_counts_queries(self) -> None:
        ext = get_result_extractor("get_example_queries")
        assert ext(_FakeQueriesResult(queries=[1, 2, 3])) == {"queries": 3}


class TestListEntities:
    def test_counts_list(self) -> None:
        ext = get_result_extractor("list_entities")
        assert ext(["e1", "e2", "e3"]) == {"entities": 3}

    def test_none_returns_empty(self) -> None:
        ext = get_result_extractor("list_entities")
        assert ext(None) == {}

    def test_non_list_returns_empty(self) -> None:
        ext = get_result_extractor("list_entities")
        # len() on non-sized raises → caught → {}
        assert ext(object()) == {}


class TestDescribeEntity:
    def test_name_and_columns(self) -> None:
        ext = get_result_extractor("describe_entity")
        data = _FakeEntity(name="customer", columns=[_FakeColumn("id"), _FakeColumn("email")])
        assert ext(data) == {"entity": "customer", "columns": 2}


class TestResolveJoin:
    def test_join_summary(self) -> None:
        ext = get_result_extractor("resolve_join")
        data = _FakeJoinInfo(
            name="customer_orders", source_entity="customer", target_entity="order"
        )
        assert ext(data) == {
            "join": "customer_orders",
            "from": "customer",
            "to": "order",
        }

    def test_partial_data_filters_out(self) -> None:
        ext = get_result_extractor("resolve_join")
        # Empty strings are falsy; treat as missing.
        data = _FakeJoinInfo(name="", source_entity="", target_entity="")
        assert ext(data) == {}


class TestGetMetric:
    def test_rows_and_fingerprint(self) -> None:
        ext = get_result_extractor("get_metric")
        data = _FakeMetricResult(rows=[{"x": 1}, {"x": 2}], fingerprint="fp-abc")
        assert ext(data) == {"rows": 2, "fingerprint": "fp-abc"}


@pytest.mark.parametrize(
    "tool_name",
    [
        "find_relevant_tables",
        "describe_table",
        "describe_column",
        "suggest_joins",
        "get_example_queries",
        "list_entities",
        "describe_entity",
        "resolve_join",
        "get_metric",
    ],
)
class TestExtractorsNeverRaise:
    def test_pathological_data_returns_empty_dict(self, tool_name: str) -> None:
        ext = get_result_extractor(tool_name)
        # Hostile inputs: random nonsense should never raise.
        for hostile in (None, 0, "", [], {}, object()):
            result = ext(hostile)
            assert isinstance(result, dict)


class _ExplodingMatches:
    """Data shape that has the attribute but raises on `len()`."""

    @property
    def matches(self) -> int:
        # Returns a non-sized value so `len()` raises.
        return 42

    @property
    def columns(self) -> int:
        return 42

    @property
    def paths(self) -> int:
        return 42

    @property
    def queries(self) -> int:
        return 42

    @property
    def rows(self) -> int:
        return 42

    @property
    def name(self) -> str:
        return "x"

    @property
    def source_entity(self) -> str:
        return "x"

    @property
    def target_entity(self) -> str:
        return "x"

    @property
    def qualified_name(self) -> str:
        return "x"

    @property
    def fingerprint(self) -> str:
        return "fp"


@pytest.mark.parametrize(
    "tool_name",
    [
        "find_relevant_tables",
        "describe_table",
        "suggest_joins",
        "get_example_queries",
        "describe_entity",
        "get_metric",
    ],
)
def test_extractor_handles_unlistable_attribute(tool_name: str) -> None:
    """If a tool returns a shape where `len(attr)` raises, the extractor
    must still return a dict rather than propagating the exception.
    """
    ext = get_result_extractor(tool_name)
    result = ext(_ExplodingMatches())
    assert isinstance(result, dict)


def test_list_entities_handles_unlistable() -> None:
    ext = get_result_extractor("list_entities")
    # len(42) raises TypeError -> caught -> {}
    assert ext(42) == {}


class _RaisingDescribeColumn:
    @property
    def qualified_name(self) -> str:
        raise RuntimeError("attribute access failed")


class _RaisingResolveJoin:
    @property
    def name(self) -> str:
        raise RuntimeError("attribute access failed")

    @property
    def source_entity(self) -> str:
        return "x"

    @property
    def target_entity(self) -> str:
        return "y"


def test_describe_column_handles_raising_attribute() -> None:
    ext = get_result_extractor("describe_column")
    assert ext(_RaisingDescribeColumn()) == {}


def test_resolve_join_handles_raising_attribute() -> None:
    ext = get_result_extractor("resolve_join")
    assert ext(_RaisingResolveJoin()) == {}
