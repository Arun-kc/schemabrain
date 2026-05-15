"""Tests for `SQLiteStore.search_embeddings_topk` (Phase A Commit 3).

The method is the new bulk-fetch retrieval seam: one SQL SELECT, one
NumPy matmul, one top-k. It replaces the per-table N+1 SQL + Python-loop
cosine path that `EmbeddingRetriever` used through Slice 4-B.

Contract under test (mirrors `store_protocol.Store.search_embeddings_topk`):

    Input  : query_vector: list[float], source_connection_id: str, k: int
    Output : list[(schema, table, column, cosine_score)]
             sorted by score descending, ties broken by
             (schema, table, column) ascending. Length <= k. No padding
             when fewer than k embeddings exist.

Tests pin the contract surface — score correctness, validation,
isolation, and tie-break determinism — NOT the implementation. The
caller-visible behavior must not change when Commit 4 swaps full sort
for `np.argpartition`.
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


class TestEmptyStore:
    def test_no_rows_anywhere_returns_empty(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            result = store.search_embeddings_topk(
                query_vector=list(_unit(0)),
                source_connection_id="src1",
                k=10,
            )
            assert result == []

    def test_tables_but_no_embeddings_returns_empty(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            store.write_table(_table("users", ("id",)), source_connection_id="src1")
            result = store.search_embeddings_topk(
                query_vector=list(_unit(0)),
                source_connection_id="src1",
                k=10,
            )
            assert result == []


class TestBasicRanking:
    def test_returns_top_k_by_descending_score(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _write_with_embeddings(
                store,
                "t",
                {"col0": _unit(0), "col1": _unit(1), "col2": _unit(2)},
            )
            result = store.search_embeddings_topk(
                query_vector=list(_unit(0)),
                source_connection_id="src1",
                k=2,
            )
            assert len(result) == 2
            assert result[0][:3] == ("public", "t", "col0")
            assert result[0][3] == pytest.approx(1.0, abs=1e-5)
            # Second is one of the orthogonal columns scoring 0.0.
            assert result[1][3] == pytest.approx(0.0, abs=1e-6)

    def test_k_larger_than_M_returns_all(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _write_with_embeddings(store, "t", {"a": _unit(0), "b": _unit(1)})
            result = store.search_embeddings_topk(
                query_vector=list(_unit(0)),
                source_connection_id="src1",
                k=100,
            )
            assert len(result) == 2

    def test_score_is_cosine_similarity(self, tmp_path: Path) -> None:
        # 45-degree vector vs axis 0 → cosine = 1 / sqrt(2).
        with SQLiteStore(tmp_path / "s.db") as store:
            _write_with_embeddings(store, "t", {"v": (1.0, 1.0, 0.0, 0.0)})
            result = store.search_embeddings_topk(
                query_vector=list(_unit(0)),
                source_connection_id="src1",
                k=1,
            )
            assert result[0][3] == pytest.approx(1 / math.sqrt(2), abs=1e-5)

    def test_unit_vectors_yield_cosine_1(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _write_with_embeddings(store, "t", {"c": _unit(2)})
            result = store.search_embeddings_topk(
                query_vector=list(_unit(2)),
                source_connection_id="src1",
                k=1,
            )
            assert result[0][3] == pytest.approx(1.0, abs=1e-5)

    def test_returns_full_4_tuple_shape(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _write_with_embeddings(store, "t", {"c": _unit(0)})
            result = store.search_embeddings_topk(
                query_vector=list(_unit(0)),
                source_connection_id="src1",
                k=1,
            )
            assert len(result) == 1
            row = result[0]
            assert len(row) == 4
            schema, table, column, score = row
            assert (schema, table, column) == ("public", "t", "c")
            assert isinstance(score, float)


class TestSourceIsolation:
    def test_unknown_source_returns_empty(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _write_with_embeddings(store, "t", {"c": _unit(0)})
            result = store.search_embeddings_topk(
                query_vector=list(_unit(0)),
                source_connection_id="DOES_NOT_EXIST",
                k=10,
            )
            assert result == []

    def test_separates_two_sources(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _write_with_embeddings(store, "t", {"c": _unit(0)}, source_connection_id="src1")
            _write_with_embeddings(store, "t", {"c": _unit(1)}, source_connection_id="src2")

            r1 = store.search_embeddings_topk(
                query_vector=list(_unit(0)),
                source_connection_id="src1",
                k=10,
            )
            r2 = store.search_embeddings_topk(
                query_vector=list(_unit(1)),
                source_connection_id="src2",
                k=10,
            )
            # src1 query parallel to axis 0 → 1.0. src2 column parallel
            # to axis 1; src1 query is orthogonal → 0.0 (not returned in
            # r1 because rows under src2 are filtered out, not because
            # of score).
            assert len(r1) == 1
            assert r1[0][:3] == ("public", "t", "c")
            assert r1[0][3] == pytest.approx(1.0, abs=1e-5)
            assert len(r2) == 1
            assert r2[0][3] == pytest.approx(1.0, abs=1e-5)


class TestOrdering:
    def test_ties_break_by_column_ascending_within_table(self, tmp_path: Path) -> None:
        # 3 columns all on axis 0; all score 1.0. Tiebreak by column name
        # ascending: aa, mm, zz.
        with SQLiteStore(tmp_path / "s.db") as store:
            _write_with_embeddings(
                store,
                "t",
                {"zz": _unit(0), "aa": _unit(0), "mm": _unit(0)},
            )
            result = store.search_embeddings_topk(
                query_vector=list(_unit(0)),
                source_connection_id="src1",
                k=10,
            )
            assert [r[2] for r in result] == ["aa", "mm", "zz"]

    def test_ties_break_by_table_ascending_across_tables(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _write_with_embeddings(store, "tb", {"c": _unit(0)})
            _write_with_embeddings(store, "ta", {"c": _unit(0)})
            result = store.search_embeddings_topk(
                query_vector=list(_unit(0)),
                source_connection_id="src1",
                k=10,
            )
            assert [r[1] for r in result] == ["ta", "tb"]

    def test_truncation_at_k_boundary_is_deterministic(self, tmp_path: Path) -> None:
        # 5 columns all score 1.0. k=2 must always return the first 2 by
        # alpha order, and must do so reproducibly across calls.
        with SQLiteStore(tmp_path / "s.db") as store:
            _write_with_embeddings(
                store,
                "t",
                {f"c{i}": _unit(0) for i in range(5)},
            )
            r1 = store.search_embeddings_topk(
                query_vector=list(_unit(0)),
                source_connection_id="src1",
                k=2,
            )
            r2 = store.search_embeddings_topk(
                query_vector=list(_unit(0)),
                source_connection_id="src1",
                k=2,
            )
            assert r1 == r2
            assert [r[2] for r in r1] == ["c0", "c1"]

    def test_mixed_scores_sort_by_descending_score_then_alpha(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _write_with_embeddings(
                store,
                "t",
                {
                    "low": _unit(1),  # cosine 0.0 vs axis-0 query
                    "high_b": _unit(0),  # cosine 1.0
                    "high_a": _unit(0),  # cosine 1.0
                },
            )
            result = store.search_embeddings_topk(
                query_vector=list(_unit(0)),
                source_connection_id="src1",
                k=10,
            )
            # high_a, high_b at 1.0 (alpha order), then low at 0.0.
            assert [r[2] for r in result] == ["high_a", "high_b", "low"]


class TestValidation:
    def test_k_zero_raises(self, tmp_path: Path) -> None:
        with (
            SQLiteStore(tmp_path / "s.db") as store,
            pytest.raises(ValueError, match=r"k must be >= 1"),
        ):
            store.search_embeddings_topk(
                query_vector=list(_unit(0)),
                source_connection_id="src1",
                k=0,
            )

    def test_k_negative_raises(self, tmp_path: Path) -> None:
        with (
            SQLiteStore(tmp_path / "s.db") as store,
            pytest.raises(ValueError, match=r"k must be >= 1"),
        ):
            store.search_embeddings_topk(
                query_vector=list(_unit(0)),
                source_connection_id="src1",
                k=-1,
            )

    def test_empty_query_vector_raises(self, tmp_path: Path) -> None:
        with (
            SQLiteStore(tmp_path / "s.db") as store,
            pytest.raises(ValueError, match=r"query_vector"),
        ):
            store.search_embeddings_topk(
                query_vector=[],
                source_connection_id="src1",
                k=10,
            )

    def test_nan_in_query_raises(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _write_with_embeddings(store, "t", {"c": _unit(0)})
            with pytest.raises(ValueError, match=r"finite"):
                store.search_embeddings_topk(
                    query_vector=[1.0, float("nan"), 0.0, 0.0],
                    source_connection_id="src1",
                    k=10,
                )

    def test_inf_in_query_raises(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _write_with_embeddings(store, "t", {"c": _unit(0)})
            with pytest.raises(ValueError, match=r"finite"):
                store.search_embeddings_topk(
                    query_vector=[1.0, float("inf"), 0.0, 0.0],
                    source_connection_id="src1",
                    k=10,
                )

    def test_zero_norm_query_raises(self, tmp_path: Path) -> None:
        # Zero-norm query vector has no direction; cosine is undefined.
        # Fail loud rather than silently return everything tied at 0.
        with SQLiteStore(tmp_path / "s.db") as store:
            _write_with_embeddings(store, "t", {"c": _unit(0)})
            with pytest.raises(ValueError, match=r"zero[- ]?norm"):
                store.search_embeddings_topk(
                    query_vector=[0.0, 0.0, 0.0, 0.0],
                    source_connection_id="src1",
                    k=10,
                )

    def test_dimension_mismatch_raises(self, tmp_path: Path) -> None:
        # Store has 4-dim vectors. Query is 8-dim. A user who re-runs eval
        # with a different embedder dimension needs a loud failure, not
        # silent garbage cosines.
        with SQLiteStore(tmp_path / "s.db") as store:
            _write_with_embeddings(store, "t", {"c": _unit(0)})
            with pytest.raises(ValueError, match=r"dimension"):
                store.search_embeddings_topk(
                    query_vector=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                    source_connection_id="src1",
                    k=10,
                )

    def test_empty_store_does_not_validate_dimension(self, tmp_path: Path) -> None:
        # With no stored rows, query dimension is irrelevant — short-
        # circuit to [] without raising.
        with SQLiteStore(tmp_path / "s.db") as store:
            result = store.search_embeddings_topk(
                query_vector=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                source_connection_id="src1",
                k=10,
            )
            assert result == []


class TestZeroNormStored:
    """A zero-norm stored vector is degenerate — score it 0.0 (not NaN).

    `ColumnEmbedding.__post_init__` does not forbid zero-norm vectors,
    so the store must defensively handle the case. Filtering 0.0 is the
    caller's job; `EmbeddingRetriever` already drops zero-score tables.
    """

    def test_zero_norm_stored_scores_zero_not_nan(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _write_with_embeddings(
                store,
                "t",
                {"good": _unit(0), "dead": (0.0, 0.0, 0.0, 0.0)},
            )
            result = store.search_embeddings_topk(
                query_vector=list(_unit(0)),
                source_connection_id="src1",
                k=10,
            )
            scores = {r[2]: r[3] for r in result}
            assert scores["good"] == pytest.approx(1.0, abs=1e-5)
            assert scores["dead"] == pytest.approx(0.0, abs=1e-7)
            # Critical: the zero-norm row must NOT produce NaN.
            assert not math.isnan(scores["dead"])


class TestStoreSideFinitenessGuards:
    """Defense in depth against NaN/inf in stored vectors.

    Primary guard is at the WRITE boundary (`write_table_embeddings`
    rejects non-finite entries). Read path adds a secondary clamp so
    NaN/inf scores cannot escape even if a future store bypass or
    direct SQL poke inserted a poisoned blob. Per silent-failure-hunter
    audit 2026-05-15 (CRITICAL — NaN scores could rank #1 silently
    because Python's `score <= 0.0` filter doesn't catch NaN).
    """

    def test_write_rejects_nan_in_vector(self, tmp_path: Path) -> None:
        import math as _math

        from schemabrain.core.embedding import ColumnEmbedding

        store = SQLiteStore(tmp_path / "s.db")
        store.write_table(_table("t", ("c",)), source_connection_id="src1")
        bad = ColumnEmbedding(
            vector=(1.0, _math.nan, 0.0, 0.0),
            model="test-emb",
            dimension=4,
        )
        with pytest.raises(ValueError, match=r"non-finite"):
            store.write_table_embeddings(
                "public", "t", source_connection_id="src1", embeddings={"c": bad}
            )

    def test_write_rejects_inf_in_vector(self, tmp_path: Path) -> None:
        import math as _math

        from schemabrain.core.embedding import ColumnEmbedding

        store = SQLiteStore(tmp_path / "s.db")
        store.write_table(_table("t", ("c",)), source_connection_id="src1")
        bad = ColumnEmbedding(
            vector=(1.0, _math.inf, 0.0, 0.0),
            model="test-emb",
            dimension=4,
        )
        with pytest.raises(ValueError, match=r"non-finite"):
            store.write_table_embeddings(
                "public", "t", source_connection_id="src1", embeddings={"c": bad}
            )

    def test_read_side_clamp_nan_scores_to_zero(self, tmp_path: Path) -> None:
        # Simulate a bypass: insert a NaN-containing blob directly via
        # SQL, bypassing `write_table_embeddings`'s guard. The read
        # path must clamp the resulting NaN score to 0.0, not let it
        # rank #1.
        import struct

        with SQLiteStore(tmp_path / "s.db") as store:
            _write_with_embeddings(store, "t", {"good": _unit(0)})
            conn = store._require_conn()
            poisoned = (1.0, float("nan"), 0.0, 0.0)
            blob = struct.pack(f"<{len(poisoned)}f", *poisoned)
            conn.execute(
                "INSERT INTO column_embeddings "
                "(schema_name, table_name, column_name, source_connection_id, "
                "model, dimension, vector, embedded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("public", "t", "poisoned", "src1", "test-emb", 4, blob, 0),
            )
            conn.commit()

            result = store.search_embeddings_topk(
                query_vector=list(_unit(0)),
                source_connection_id="src1",
                k=10,
            )
            scores = {r[2]: r[3] for r in result}
            # `good` is the only row that should score 1.0; `poisoned`
            # must be clamped to 0.0 rather than NaN.
            assert scores["good"] == pytest.approx(1.0, abs=1e-5)
            assert scores["poisoned"] == 0.0
            assert not math.isnan(scores["poisoned"])

    def test_blob_length_mismatch_raises(self, tmp_path: Path) -> None:
        # Stored dimension column says 4, but the blob holds 5 floats
        # (20 bytes vs expected 16). `np.frombuffer` would silently
        # read 4 of the 5 floats and produce a wrong-but-plausible
        # vector. The blob-length guard must catch this.
        import struct

        with SQLiteStore(tmp_path / "s.db") as store:
            _write_with_embeddings(store, "t", {"good": _unit(0)})
            conn = store._require_conn()
            five_floats = (0.1, 0.2, 0.3, 0.4, 0.5)
            blob_5 = struct.pack(f"<{len(five_floats)}f", *five_floats)
            # Dimension column lies: claims dim=4 but blob is 5 floats.
            conn.execute(
                "INSERT INTO column_embeddings "
                "(schema_name, table_name, column_name, source_connection_id, "
                "model, dimension, vector, embedded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("public", "t", "liar", "src1", "test-emb", 4, blob_5, 0),
            )
            conn.commit()

            with pytest.raises(ValueError, match=r"blob length"):
                store.search_embeddings_topk(
                    query_vector=list(_unit(0)),
                    source_connection_id="src1",
                    k=10,
                )


class TestCoveringIndexUsed:
    """The covering index `idx_col_emb_src` is what makes the
    bulk-fetch SELECT a B-tree seek instead of a full scan. Verify
    SQLite's query planner actually picks it up — without the index,
    the Commit 4 p95 < 100ms gate is at risk.
    """

    def test_query_plan_uses_idx_col_emb_src(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _write_with_embeddings(store, "t", {"c": _unit(0)})
            conn = store._require_conn()
            rows = conn.execute(
                "EXPLAIN QUERY PLAN "
                "SELECT schema_name, table_name, column_name, dimension, vector "
                "FROM column_embeddings "
                "WHERE source_connection_id = ? "
                "ORDER BY schema_name, table_name, column_name",
                ("src1",),
            ).fetchall()
            # Plan rows have a `detail` column describing the access
            # path. The covering index name must show up.
            plan_text = " ".join(row["detail"] for row in rows)
            assert "idx_col_emb_src" in plan_text, (
                f"Expected idx_col_emb_src in query plan, got: {plan_text}"
            )


class TestMixedDimensionInStore:
    """Defensive guard: if two rows in `column_embeddings` somehow have
    different dimensions under one `source_connection_id`, raise with
    "dimension" in the message rather than silently producing garbage.

    Can happen if the embedder model was swapped mid-index (rare; not a
    happy path) or a manual DB poke inserted a row with the wrong dim.
    The test bypasses `ColumnEmbedding`'s constructor invariant by
    writing directly via SQL to force the divergent state.
    """

    def test_mixed_stored_dimensions_raises_with_dimension_in_message(self, tmp_path: Path) -> None:
        import struct

        with SQLiteStore(tmp_path / "s.db") as store:
            _write_with_embeddings(store, "t", {"a": _unit(0), "b": _unit(1)})
            # Inject a 5-dim row under the same source — bypasses
            # ColumnEmbedding's dim-vs-len check.
            conn = store._require_conn()
            five_dim = (0.1, 0.2, 0.3, 0.4, 0.5)
            blob = struct.pack(f"<{len(five_dim)}f", *five_dim)
            conn.execute(
                "INSERT INTO column_embeddings "
                "(schema_name, table_name, column_name, source_connection_id, "
                "model, dimension, vector, embedded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("public", "t", "c", "src1", "test-emb", 5, blob, 0),
            )
            conn.commit()

            # The first row read sets the expected dim. Whether the
            # 5-dim row is the row-0 or a later row depends on SQL
            # ORDER BY (schema, table, column). With column names
            # ("a", "b", "c") under one table, "a" wins row 0 → dim 4.
            # Then "c" at dim 5 trips the per-row guard at line 917.
            with pytest.raises(ValueError, match=r"dimension"):
                store.search_embeddings_topk(
                    query_vector=list(_unit(0)),
                    source_connection_id="src1",
                    k=10,
                )


class TestMultipleTables:
    def test_aggregates_across_tables(self, tmp_path: Path) -> None:
        # Three tables, one column each, on distinct axes. Query on
        # axis 1 → orders wins.
        with SQLiteStore(tmp_path / "s.db") as store:
            _write_with_embeddings(store, "users", {"email": _unit(0)})
            _write_with_embeddings(store, "orders", {"user_id": _unit(1)})
            _write_with_embeddings(store, "products", {"sku": _unit(2)})
            result = store.search_embeddings_topk(
                query_vector=list(_unit(1)),
                source_connection_id="src1",
                k=3,
            )
            assert result[0][:3] == ("public", "orders", "user_id")
            assert result[0][3] == pytest.approx(1.0, abs=1e-5)
            # Other two tie at 0.0.
            tail_tables = sorted(r[1] for r in result[1:])
            assert tail_tables == ["products", "users"]
