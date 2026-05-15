"""Retriever Protocol + concrete implementations.

The eval runner programs against the `Retriever` Protocol so we can
add new strategies without touching the runner, report, or golden files.

Concrete implementations:
  - `KeywordRetriever` — tokenizes query and each table's combined
    corpus (table name + column names + description text), ranks by
    overlap count. The Week-3 baseline; cheap, no embedder required.
  - `EmbeddingRetriever` — embeds the query once, then delegates to
    `Store.search_embeddings_topk` for a single bulk-fetch + NumPy
    matmul cosine across all stored column embeddings under the
    configured source. Aggregates the column-level scores to a
    per-table MAX (a single very-relevant column is strong evidence the
    table is relevant). Drops zero-score tables. Tables indexed without
    embeddings are silently skipped — they simply don't appear in the
    bulk-fetch rows.

Per-table-MAX vs per-table-MEAN aggregation: max is the right default
for "find tables relevant to this query" because schemas frequently
have one or two highly-relevant columns plus many irrelevant ones —
mean would dilute the signal. Mean might be revisited if MCP traffic
shows the opposite pattern.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np

from schemabrain.core.store_protocol import Store
from schemabrain.enrichment.embeddings import Embedder

# Bulk-fetch cap for the column-level top-k pulled from the store. Big
# enough that any v1-scale source (< 10k embedded columns total) is
# fully covered in a single round trip; small enough that an unexpected
# 100k-column outlier doesn't burn memory unbounded. When the store has
# fewer rows than the cap, `search_embeddings_topk` returns all of them
# (no padding) — so this constant is an upper bound, not a floor.
# Revisit when MCP traffic shows we routinely cross 10k columns: at that
# scale the right move is column-side ANN (sqlite-vec or hnswlib), not
# raising this constant.
_BULK_FETCH_COLUMN_K = 10_000

# Stopwords to drop before scoring overlap. Kept short on purpose: most
# database semantics are nouns and verbs, and over-aggressive filtering
# would accidentally drop meaningful tokens like "table", "id", "key"
# that frequently appear in real queries about schemas.
_STOP_WORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "are",
        "but",
        "not",
        "you",
        "all",
        "can",
        "her",
        "was",
        "our",
        "out",
        "from",
        "with",
        "this",
        "that",
        "have",
        "has",
        "had",
        "what",
        "when",
        "where",
        "which",
        "who",
        "how",
        "why",
        "into",
        "onto",
        "would",
        "could",
        "should",
        "about",
        "been",
        "being",
        "were",
        "does",
        "did",
    }
)

# Minimum token length AFTER tokenization. "id" is 2 chars but is a
# load-bearing keyword in DB queries, so we set the floor at 2 not 3 —
# the trade-off is that a few noise tokens like "to", "of", "is" sneak
# through, mitigated by the stopword set above.
_MIN_TOKEN_LEN = 2


def _tokenize(text: str) -> set[str]:
    """Lowercase, split on non-alphanumeric, drop short + stopword tokens.

    Underscores are treated as word boundaries so `user_id` yields both
    `user` and `id` — without that, compound column names hide their
    keywords from the matcher.
    """
    raw = re.findall(r"[a-z0-9]+", text.lower())
    return {t for t in raw if len(t) >= _MIN_TOKEN_LEN and t not in _STOP_WORDS}


@runtime_checkable
class Retriever(Protocol):
    """Retrieval contract used by the eval runner.

    Implementations return a ranked list of qualified table names
    (`schema.table`), best-first, capped at `limit`. An empty list is a
    valid answer when nothing matches.
    """

    def retrieve(self, query: str, *, limit: int) -> list[str]:
        """Return ranked qualified table names for the query."""
        ...


@dataclass(frozen=True)
class KeywordRetriever:
    """Keyword-overlap retriever over the local SQLite store.

    For each table in the configured source, builds a token corpus from
    the table name, column names, and column description text (when
    enrichment has run). Ranks tables by the size of the intersection
    between the query's tokens and each table's corpus.

    No TF-IDF, no stemming, no synonyms. This is the "we have to ship
    SOMETHING that returns ranked tables today" baseline.
    """

    store: Store
    source_connection_id: str

    def retrieve(self, query: str, *, limit: int) -> list[str]:
        if limit <= 0:
            return []
        query_tokens = _tokenize(query)
        if not query_tokens:
            return []

        scored: list[tuple[int, str, str]] = []
        for schema, table in self.store.list_tables(source_connection_id=self.source_connection_id):
            corpus = self._build_corpus(schema, table)
            score = len(query_tokens & corpus)
            if score > 0:
                scored.append((score, schema, table))

        # Sort by descending score, then by qualified name for stable
        # ordering when scores tie (reproducible test output).
        scored.sort(key=lambda x: (-x[0], x[1], x[2]))
        return [f"{s}.{t}" for _, s, t in scored[:limit]]

    def _build_corpus(self, schema: str, table: str) -> set[str]:
        tokens = _tokenize(schema) | _tokenize(table)
        # Always fold in EVERY column name so that columns without
        # descriptions (`--no-enrich` stores or partially-enriched tables
        # where the cap was hit before all columns finished) still
        # contribute their name as a search signal. `get_table` cannot
        # return None here: `list_tables` just yielded this exact
        # (schema, table) under the same `source_connection_id`, and the
        # store is a single-process SQLite read — no concurrent deleter
        # can race. Use a conditional raise (not `assert`) so the guard
        # survives a `-O` run.
        tbl = self.store.get_table(schema, table, source_connection_id=self.source_connection_id)
        if tbl is None:  # pragma: no cover — invariant from list_tables
            raise RuntimeError(
                f"store inconsistency: list_tables returned ({schema!r}, {table!r}) "
                f"under source {self.source_connection_id!r} but get_table did not"
            )
        for c in tbl.columns:
            tokens |= _tokenize(c.name)
        # Then layer in description text for whichever columns are enriched.
        descs = self.store.get_table_descriptions(
            schema, table, source_connection_id=self.source_connection_id
        )
        for desc in descs.values():
            tokens |= _tokenize(desc.text)
        return tokens


@dataclass(frozen=True)
class EmbeddingRetriever:
    """Cosine-similarity retriever over locally-stored column embeddings.

    Embeds the query once with `embedder`, then delegates to
    `Store.search_embeddings_topk` for a single bulk-fetch + NumPy
    matmul cosine across every stored column embedding under the
    configured source. Aggregates the column-level scores to per-table
    MAX; tables with no embeddings are silently skipped (the bulk-fetch
    simply has no rows for them). Zero-score tables are excluded —
    they're noise that crowds out the actual signal.

    Cost characteristic: ONE embedder call per `retrieve()`, ONE SQL
    SELECT (no per-table N+1), one NumPy matmul. The Charter target
    (p95 < 100ms at 10k columns) is gated by the pytest-benchmark
    suite at `tests/test_perf_find_relevant_tables.py`.
    """

    store: Store
    source_connection_id: str
    embedder: Embedder

    def retrieve(self, query: str, *, limit: int) -> list[str]:
        if limit <= 0:
            return []
        # Skip the embedder call entirely on empty/whitespace queries —
        # otherwise we'd waste an ONNX inference and the store would
        # then refuse the zero-norm vector with a ValueError, which is
        # the right behavior at its layer but the wrong UX at this one.
        if not query.strip():
            return []

        query_vec = self.embedder.embed(query)

        # Zero-norm query → no direction → no signal. The Store would
        # raise ValueError if we forwarded this; the retriever's
        # contract is "treat embedder degeneracy as empty result, don't
        # crash the CLI". Pre-check using the SAME float32 norm the
        # Store uses, so a vector with subnormal entries that round
        # to zero in float32 (but not in Python float64) doesn't slip
        # past this check and trip the Store's ValueError.
        if float(np.linalg.norm(np.asarray(query_vec, dtype=np.float32))) == 0.0:
            return []

        # `search_embeddings_topk` returns at most k rows but never pads,
        # so over-asking is safe — when the store holds fewer rows we
        # get all of them. The store raises ValueError on dimension
        # mismatch; the eval CLI surfaces that to the user as
        # "embedder swapped — wipe and re-index".
        col_rows = self.store.search_embeddings_topk(
            list(query_vec),
            source_connection_id=self.source_connection_id,
            k=_BULK_FETCH_COLUMN_K,
        )

        # Aggregate column scores to per-table MAX.
        by_table: dict[tuple[str, str], float] = {}
        for schema, table, _col, score in col_rows:
            if score <= 0.0:
                continue
            prev = by_table.get((schema, table))
            if prev is None or score > prev:
                by_table[(schema, table)] = score

        scored = [(score, schema, table) for (schema, table), score in by_table.items()]
        scored.sort(key=lambda x: (-x[0], x[1], x[2]))
        return [f"{s}.{t}" for _, s, t in scored[:limit]]


__all__ = ["EmbeddingRetriever", "KeywordRetriever", "Retriever"]
