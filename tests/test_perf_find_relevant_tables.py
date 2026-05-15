"""Performance gate: `find_relevant_tables` p95 < 100ms at 10k columns.

The Charter target for v1: an MCP `find_relevant_tables` call must
return in under 100ms at the p95 across a store of 10,000 indexed
columns (the stretch shape — typical donor schemas are 200-2000
columns). Without this gate, a regression could silently degrade
retrieval latency until it shows up in agent traces.

Setup:
  - Module-scoped SQLite store seeded ONCE with 100 tables x 100
    columns = 10,000 random 384-dim float32 embeddings. Random vectors
    with a fixed seed for reproducibility; not realistic semantically,
    but the cosine ranking algorithm and bulk-fetch + matmul + sort
    cost are insensitive to vector content.
  - Each table has descriptions for its 100 columns (so the per-top-10
    `get_table_descriptions` calls land on populated rows, matching
    the production path).
  - Fake embedder returns a fixed normalized 384-dim vector. We measure
    the retrieval cost, NOT ONNX inference (that's a separate gate).

Marked `slow` so the default `pytest` run skips it. The CI `slow` job
(or local `pytest -m slow`) runs it and fails the build if p95 breaches
the 100ms target. Tuned `min_rounds` high enough that p95 is stable
across runs on commodity hardware (M-class macbooks, x86_64 GitHub
runners).
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest

from schemabrain.core.description import ColumnDescription
from schemabrain.core.embedding import ColumnEmbedding
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.mcp.find_relevant_tables import find_relevant_tables_impl

# Production embedder dimension (all-MiniLM-L6-v2 family).
_EMBEDDING_DIM = 384

# 100 tables x 100 columns each = 10_000 embedded columns total — the
# Charter "stretch" scale. Vary table sizes here if a future regression
# is suspected to be column-count-per-table sensitive.
_NUM_TABLES = 100
_COLS_PER_TABLE = 100

_SOURCE_ID = "perf-sid"

# Charter targets — see module docstring.
_MEDIAN_TARGET_S = 0.050  # 50ms typical
_P95_TARGET_S = 0.100  # 100ms p95 — the Charter gate

# pytest-benchmark hyperparams. 50 rounds x ~25ms median ~= 1.25s of
# benchmark time on top of the ~1.5s fixture seeding. The higher
# count absorbs single-round OS scheduling spikes on shared CI runners
# (GitHub x86_64 hosts are slower than M-class macbooks on SQLite I/O
# and have higher latency variance).
_MIN_ROUNDS = 50


def _random_unit_vector(rng: np.random.Generator) -> tuple[float, ...]:
    """Random 384-dim unit vector (cosine of self == 1.0)."""
    v = rng.standard_normal(_EMBEDDING_DIM).astype(np.float32)
    v /= np.linalg.norm(v)
    return tuple(v.tolist())


class _FixedVectorEmbedder:
    """Returns the same fixed vector regardless of input text.

    Production embedder cost is excluded from the gate by design: the
    `find_relevant_tables` call path is `embedder.embed(query) → store
    bulk-fetch + matmul → top-k → per-result description fetch`, and
    this gate measures everything AFTER the embedder. ONNX/fastembed
    latency has its own (separate) gate.
    """

    model_name = "fixed-test-embedder"
    dimension = _EMBEDDING_DIM

    def __init__(self, vector: tuple[float, ...]) -> None:
        self._vector = vector

    def embed(self, text: str) -> tuple[float, ...]:
        # `text` is intentionally ignored — see class docstring.
        del text
        return self._vector


@pytest.fixture(scope="module")
def seeded_store(tmp_path_factory: pytest.TempPathFactory) -> SQLiteStore:
    """Build a SQLiteStore with 10,000 random embeddings + descriptions.

    Module-scoped so the seeding cost (a few seconds) is paid once
    across the whole benchmark file. The store is read-only from the
    benchmark's perspective — no benchmark mutates it.
    """
    rng = np.random.default_rng(seed=20260515)
    db_path = tmp_path_factory.mktemp("perf") / "bench.db"
    store = SQLiteStore(db_path)

    for ti in range(_NUM_TABLES):
        table_name = f"t_{ti:03d}"
        col_names = tuple(f"c_{ci:03d}" for ci in range(_COLS_PER_TABLE))
        columns = tuple(
            Column(
                name=col_names[ci],
                table_name=table_name,
                schema_name="public",
                data_type="TEXT",
                nullable=False,
                ordinal_position=ci + 1,
            )
            for ci in range(_COLS_PER_TABLE)
        )
        store.write_table(
            Table(name=table_name, schema_name="public", columns=columns),
            source_connection_id=_SOURCE_ID,
        )
        embeddings = {
            col_names[ci]: ColumnEmbedding(
                vector=_random_unit_vector(rng),
                model="fixed-test-embedder",
                dimension=_EMBEDDING_DIM,
            )
            for ci in range(_COLS_PER_TABLE)
        }
        store.write_table_embeddings(
            "public", table_name, source_connection_id=_SOURCE_ID, embeddings=embeddings
        )
        descriptions = {
            col_names[ci]: ColumnDescription(
                text=f"Description for {col_names[ci]} on {table_name}",
                model="fake-llm",
                prompt_version="perf",
                input_tokens=10,
                cached_input_tokens=0,
                output_tokens=5,
                cost_usd=0.0001,
            )
            for ci in range(_COLS_PER_TABLE)
        }
        store.write_table_descriptions(
            "public", table_name, source_connection_id=_SOURCE_ID, descriptions=descriptions
        )

    yield store
    store.close()


@pytest.fixture(scope="module")
def fixed_query_vector() -> tuple[float, ...]:
    """One stable query vector used across all benchmark runs."""
    rng = np.random.default_rng(seed=20260515 + 1)
    return _random_unit_vector(rng)


@pytest.mark.slow
class TestFindRelevantTablesPerfGate:
    """Charter gate: p95 < 100ms at 10k columns."""

    def test_seeded_store_has_10k_embeddings(self, seeded_store: SQLiteStore) -> None:
        # Smoke-check the fixture matches the documented scale.
        conn = seeded_store._require_conn()
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM column_embeddings WHERE source_connection_id = ?",
            (_SOURCE_ID,),
        ).fetchone()["n"]
        assert count == _NUM_TABLES * _COLS_PER_TABLE

    def test_find_relevant_tables_p95_under_100ms(
        self,
        seeded_store: SQLiteStore,
        fixed_query_vector: tuple[float, ...],
        benchmark: object,
    ) -> None:
        embedder = _FixedVectorEmbedder(fixed_query_vector)

        def _run() -> int:
            hits = find_relevant_tables_impl(
                store=seeded_store,
                source_connection_id=_SOURCE_ID,
                embedder=embedder,
                query="benchmark query",
                limit=10,
            )
            return len(hits)

        # `pedantic` for explicit round control — the default
        # `benchmark(...)` mode auto-stops when its time budget is
        # exhausted, which can produce as few as 5-10 rounds at 20ms
        # per call. That's too few samples for a stable p95. Force a
        # fixed round count so the gate is reproducible across runs.
        result = benchmark.pedantic(  # type: ignore[attr-defined]
            _run, rounds=_MIN_ROUNDS, iterations=1, warmup_rounds=2
        )
        assert result == 10  # 10k cols → at least 10 distinct tables

        # Pull raw samples and compute p95 ourselves — pytest-benchmark's
        # Metadata.stats exposes mean/median/min/max/q1/q3 but no q95
        # directly. `stats.data` is the raw list of per-round times.
        samples = list(benchmark.stats.stats.data)  # type: ignore[attr-defined]
        median_s = float(np.median(samples))
        p95_s = float(np.percentile(samples, 95))

        # Median is the headroom check; p95 is the Charter gate.
        assert median_s < _MEDIAN_TARGET_S, (
            f"median {median_s * 1000:.1f}ms exceeds target "
            f"{_MEDIAN_TARGET_S * 1000:.0f}ms — retrieval has regressed"
        )
        assert p95_s < _P95_TARGET_S, (
            f"p95 {p95_s * 1000:.1f}ms exceeds Charter gate "
            f"{_P95_TARGET_S * 1000:.0f}ms at "
            f"{_NUM_TABLES * _COLS_PER_TABLE} columns"
        )


@pytest.mark.slow
class TestSeedTimingNotInsanelySlow:
    """The fixture itself should seed in well under a minute. This is
    not a hard performance gate — it's a sanity check so a future
    regression in `write_table_embeddings` doesn't silently turn the
    benchmark fixture into a multi-minute setup.
    """

    def test_seeding_completes_under_60_seconds(self, tmp_path: Path) -> None:
        # Smaller scale (10 x 10 = 100 cols) — the per-row cost is what
        # matters, scaled estimation does the rest.
        rng = np.random.default_rng(seed=20260515)
        store = SQLiteStore(tmp_path / "small.db")
        sid = "perf-sid"
        n_tables = 10
        n_cols = 10
        t0 = time.perf_counter()
        for ti in range(n_tables):
            table_name = f"t_{ti:03d}"
            col_names = tuple(f"c_{ci:03d}" for ci in range(n_cols))
            columns = tuple(
                Column(
                    name=col_names[ci],
                    table_name=table_name,
                    schema_name="public",
                    data_type="TEXT",
                    nullable=False,
                    ordinal_position=ci + 1,
                )
                for ci in range(n_cols)
            )
            store.write_table(
                Table(name=table_name, schema_name="public", columns=columns),
                source_connection_id=sid,
            )
            store.write_table_embeddings(
                "public",
                table_name,
                source_connection_id=sid,
                embeddings={
                    col_names[ci]: ColumnEmbedding(
                        vector=_random_unit_vector(rng),
                        model="fixed-test-embedder",
                        dimension=_EMBEDDING_DIM,
                    )
                    for ci in range(n_cols)
                },
            )
        per_row_s = (time.perf_counter() - t0) / (n_tables * n_cols)
        # 10k rows scaled: per_row * 10000 < 60s.
        projected_full_seed_s = per_row_s * _NUM_TABLES * _COLS_PER_TABLE
        store.close()
        assert projected_full_seed_s < 60.0, (
            f"Projected full seed time {projected_full_seed_s:.1f}s "
            f"exceeds 60s threshold; write_table_embeddings may have "
            f"regressed"
        )
