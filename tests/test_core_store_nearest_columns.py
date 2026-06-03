"""Tests for `SQLiteStore.nearest_columns`.

`nearest_columns` is the column-to-column nearest-neighbour seam behind
the entity drilldown's "similar columns" field. Given one reference
column, it returns the top-`k` *other* columns most similar to it by
cosine over the stored embeddings — excluding the reference itself AND
every other column in its own table (table-mates are not "similar
elsewhere in the schema"; they're the same record).

It shares the cosine core (`_cosine_topk`) with `search_embeddings_topk`
so the two can never disagree on scoring, dimension validation, or
tiebreak ordering. The difference is the query source: this method
derives the query vector from a *stored* embedding rather than a
caller-supplied list.

Contract under test:

    Input  : schema_name, table_name, column_name, source_connection_id, k
    Output : list[(schema, table, column, cosine_score)]
             sorted by score descending, ties broken by
             (schema, table, column) ascending. Length <= k.
             Self + same-table rows never appear.

Degenerate inputs return `[]` (never raise): an empty store, a
reference column with no stored embedding, or a zero-norm reference
vector. A read-surface drilldown must render "no similar columns",
not a 500.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from schemabrain.core.embedding import ColumnEmbedding
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore

# Tiny fixed dimension keeps cosine geometry trivially auditable:
# parallel = 1.0, orthogonal = 0.0, 45-deg = 1/sqrt(2).
_DIM = 4


def _unit(idx: int) -> tuple[float, ...]:
    """One-hot unit vector of length _DIM with the 1 at position `idx`."""
    return tuple(1.0 if j == idx else 0.0 for j in range(_DIM))


def _emb(vec: tuple[float, ...], model: str = "test-emb") -> ColumnEmbedding:
    return ColumnEmbedding(vector=vec, model=model, dimension=len(vec))


def _table(name: str, cols: tuple[str, ...]) -> Table:
    return Table(
        name=name,
        schema_name="public",
        columns=tuple(
            Column(
                name=c,
                table_name=name,
                schema_name="public",
                data_type="TEXT",
                nullable=False,
                ordinal_position=i + 1,
            )
            for i, c in enumerate(cols)
        ),
    )


def _write_with_embeddings(
    store: SQLiteStore,
    table_name: str,
    embeddings: dict[str, tuple[float, ...]],
    *,
    source_connection_id: str = "src1",
) -> None:
    store.write_table(
        _table(table_name, tuple(embeddings.keys())),
        source_connection_id=source_connection_id,
    )
    store.write_table_embeddings(
        "public",
        table_name,
        source_connection_id=source_connection_id,
        embeddings={col: _emb(v) for col, v in embeddings.items()},
    )


class TestDegenerateInputs:
    def test_empty_store_returns_empty(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            result = store.nearest_columns(
                "public", "users", "email", source_connection_id="src1", k=5
            )
            assert result == []

    def test_reference_column_without_embedding_returns_empty(self, tmp_path: Path) -> None:
        # Other columns are embedded, but the reference column itself has
        # no stored vector — there is no query direction, so no similarity.
        with SQLiteStore(tmp_path / "s.db") as store:
            _write_with_embeddings(store, "orders", {"user_id": _unit(0)})
            result = store.nearest_columns(
                "public", "users", "email", source_connection_id="src1", k=5
            )
            assert result == []

    def test_zero_norm_reference_returns_empty(self, tmp_path: Path) -> None:
        # A zero-norm reference vector has no direction — cosine is
        # undefined. A drilldown must show "no similar columns", not 500.
        with SQLiteStore(tmp_path / "s.db") as store:
            _write_with_embeddings(
                store,
                "users",
                {"email": (0.0, 0.0, 0.0, 0.0)},
            )
            _write_with_embeddings(store, "orders", {"user_id": _unit(0)})
            result = store.nearest_columns(
                "public", "users", "email", source_connection_id="src1", k=5
            )
            assert result == []

    def test_only_reference_and_table_mates_returns_empty(self, tmp_path: Path) -> None:
        # The reference column is embedded, but every other embedded
        # column is in its own table — all excluded, nothing left.
        with SQLiteStore(tmp_path / "s.db") as store:
            _write_with_embeddings(
                store,
                "users",
                {"email": _unit(0), "name": _unit(0), "id": _unit(1)},
            )
            result = store.nearest_columns(
                "public", "users", "email", source_connection_id="src1", k=5
            )
            assert result == []


class TestExclusions:
    def test_self_never_appears(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _write_with_embeddings(store, "users", {"email": _unit(0)})
            _write_with_embeddings(store, "contacts", {"email_addr": _unit(0)})
            result = store.nearest_columns(
                "public", "users", "email", source_connection_id="src1", k=5
            )
            assert ("public", "users", "email") not in [r[:3] for r in result]

    def test_same_table_columns_excluded(self, tmp_path: Path) -> None:
        # `users.name` is identical to the query but in the same table →
        # must be excluded. `contacts.email_addr` (other table) is kept.
        with SQLiteStore(tmp_path / "s.db") as store:
            _write_with_embeddings(
                store,
                "users",
                {"email": _unit(0), "name": _unit(0)},
            )
            _write_with_embeddings(store, "contacts", {"email_addr": _unit(0)})
            result = store.nearest_columns(
                "public", "users", "email", source_connection_id="src1", k=5
            )
            triples = [r[:3] for r in result]
            assert ("public", "users", "name") not in triples
            assert ("public", "contacts", "email_addr") in triples


class TestRanking:
    def test_returns_other_table_columns_by_descending_score(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _write_with_embeddings(store, "users", {"email": _unit(0)})
            _write_with_embeddings(store, "contacts", {"email_addr": _unit(0)})  # 1.0
            _write_with_embeddings(store, "orders", {"total": _unit(1)})  # 0.0
            result = store.nearest_columns(
                "public", "users", "email", source_connection_id="src1", k=5
            )
            assert result[0][:3] == ("public", "contacts", "email_addr")
            assert result[0][3] == pytest.approx(1.0, abs=1e-5)
            assert result[-1][:3] == ("public", "orders", "total")
            assert result[-1][3] == pytest.approx(0.0, abs=1e-6)

    def test_score_is_cosine_similarity(self, tmp_path: Path) -> None:
        # contacts.x at 45 degrees to the reference axis-0 vector.
        with SQLiteStore(tmp_path / "s.db") as store:
            _write_with_embeddings(store, "users", {"email": _unit(0)})
            _write_with_embeddings(store, "contacts", {"x": (1.0, 1.0, 0.0, 0.0)})
            result = store.nearest_columns(
                "public", "users", "email", source_connection_id="src1", k=1
            )
            assert result[0][3] == pytest.approx(1 / math.sqrt(2), abs=1e-5)

    def test_k_caps_result_length(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _write_with_embeddings(store, "users", {"email": _unit(0)})
            _write_with_embeddings(store, "a", {"c": _unit(0)})
            _write_with_embeddings(store, "b", {"c": _unit(0)})
            _write_with_embeddings(store, "c", {"c": _unit(0)})
            result = store.nearest_columns(
                "public", "users", "email", source_connection_id="src1", k=2
            )
            assert len(result) == 2

    def test_k_larger_than_candidates_returns_all(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _write_with_embeddings(store, "users", {"email": _unit(0)})
            _write_with_embeddings(store, "contacts", {"email_addr": _unit(0)})
            result = store.nearest_columns(
                "public", "users", "email", source_connection_id="src1", k=100
            )
            assert len(result) == 1

    def test_returns_full_4_tuple_shape(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _write_with_embeddings(store, "users", {"email": _unit(0)})
            _write_with_embeddings(store, "contacts", {"email_addr": _unit(0)})
            result = store.nearest_columns(
                "public", "users", "email", source_connection_id="src1", k=1
            )
            assert len(result) == 1
            schema, table, column, score = result[0]
            assert (schema, table, column) == ("public", "contacts", "email_addr")
            assert isinstance(score, float)


class TestOrdering:
    def test_ties_break_by_table_then_column_ascending(self, tmp_path: Path) -> None:
        # All candidates score 1.0; deterministic alpha tiebreak across
        # (schema, table, column).
        with SQLiteStore(tmp_path / "s.db") as store:
            _write_with_embeddings(store, "users", {"email": _unit(0)})
            _write_with_embeddings(store, "zeta", {"c": _unit(0)})
            _write_with_embeddings(store, "alpha", {"c": _unit(0)})
            _write_with_embeddings(store, "mid", {"c": _unit(0)})
            result = store.nearest_columns(
                "public", "users", "email", source_connection_id="src1", k=10
            )
            assert [r[1] for r in result] == ["alpha", "mid", "zeta"]

    def test_truncation_at_k_is_deterministic(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _write_with_embeddings(store, "users", {"email": _unit(0)})
            for name in ("t0", "t1", "t2", "t3", "t4"):
                _write_with_embeddings(store, name, {"c": _unit(0)})
            r1 = store.nearest_columns("public", "users", "email", source_connection_id="src1", k=2)
            r2 = store.nearest_columns("public", "users", "email", source_connection_id="src1", k=2)
            assert r1 == r2
            assert [r[1] for r in r1] == ["t0", "t1"]


class TestSourceIsolation:
    def test_candidates_from_other_sources_invisible(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _write_with_embeddings(store, "users", {"email": _unit(0)}, source_connection_id="src1")
            _write_with_embeddings(
                store, "contacts", {"email_addr": _unit(0)}, source_connection_id="src2"
            )
            result = store.nearest_columns(
                "public", "users", "email", source_connection_id="src1", k=10
            )
            assert result == []

    def test_reference_resolved_within_its_own_source(self, tmp_path: Path) -> None:
        # The same (schema, table, column) exists in two sources with
        # different vectors. The query must use the src1 vector, so the
        # src1 candidate parallel to it wins.
        with SQLiteStore(tmp_path / "s.db") as store:
            _write_with_embeddings(store, "users", {"email": _unit(0)}, source_connection_id="src1")
            _write_with_embeddings(store, "contacts", {"c": _unit(0)}, source_connection_id="src1")
            _write_with_embeddings(store, "users", {"email": _unit(1)}, source_connection_id="src2")
            _write_with_embeddings(store, "contacts", {"c": _unit(1)}, source_connection_id="src2")
            result = store.nearest_columns(
                "public", "users", "email", source_connection_id="src1", k=10
            )
            assert result[0][:3] == ("public", "contacts", "c")
            assert result[0][3] == pytest.approx(1.0, abs=1e-5)


class TestValidation:
    def test_k_zero_raises(self, tmp_path: Path) -> None:
        with (
            SQLiteStore(tmp_path / "s.db") as store,
            pytest.raises(ValueError, match=r"k must be >= 1"),
        ):
            store.nearest_columns("public", "users", "email", source_connection_id="src1", k=0)

    def test_k_negative_raises(self, tmp_path: Path) -> None:
        with (
            SQLiteStore(tmp_path / "s.db") as store,
            pytest.raises(ValueError, match=r"k must be >= 1"),
        ):
            store.nearest_columns("public", "users", "email", source_connection_id="src1", k=-1)
