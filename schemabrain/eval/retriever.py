"""Retriever Protocol + concrete implementations.

The eval runner programs against the `Retriever` Protocol so we can
add new strategies without touching the runner, report, or golden files.

Concrete implementations:
  - `KeywordRetriever` — tokenizes query and each table's combined
    corpus (table name + column names + description text), ranks by
    overlap count. The Week-3 baseline; cheap, no embedder required.
  - `EmbeddingRetriever` — embeds the query once, computes cosine
    similarity against every stored column embedding, aggregates to a
    per-table score by MAX (a single very-relevant column is strong
    evidence the table is relevant). Drops zero-score tables. Tables
    indexed without embeddings are silently skipped.

Per-table-MAX vs per-table-MEAN aggregation: max is the right default
for "find tables relevant to this query" because schemas frequently
have one or two highly-relevant columns plus many irrelevant ones —
mean would dilute the signal. Mean might be revisited if MCP traffic
shows the opposite pattern.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from schemabrain.core.store import SQLiteStore
from schemabrain.enrichment.embeddings import Embedder

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

    store: SQLiteStore
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


def _cosine_similarity(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """Cosine similarity between two equal-length vectors.

    Returns 0.0 when either vector has zero norm — degenerate but
    well-defined: a zero-norm vector has no direction so "alignment with
    query" is undefined, and "no signal" (0.0) is the safe answer that
    just drops the table from positive-score results.

    Raises `ValueError` on length mismatch so an embedder-dimension
    drift between index time and retrieve time fails loudly instead of
    silently returning garbage cosines.
    """
    if len(a) != len(b):
        raise ValueError(
            f"cosine: vector dimension mismatch — query has {len(a)}, "
            f"stored has {len(b)}. The embedder used at retrieval time "
            f"differs from the one used at index time. Wipe the store "
            f"and re-index with the new embedder."
        )
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    # Lengths pre-checked above — no `strict=True` needed (would be dead).
    for ai, bi in zip(a, b):
        dot += ai * bi
        norm_a += ai * ai
        norm_b += bi * bi
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


@dataclass(frozen=True)
class EmbeddingRetriever:
    """Cosine-similarity retriever over locally-stored column embeddings.

    Embeds the query once with `embedder`, then computes cosine vs every
    stored column embedding for the configured source. Per-table score
    is the MAX cosine across that table's columns; tables with no
    embeddings are silently skipped (they were indexed with --no-embed,
    or pre-Slice-4-B, or hit the cap before embedding completed).
    Zero-score tables are excluded from the result list — they're noise
    that crowds out actual signal.

    Cost characteristic: ONE embedder call per `retrieve()`. Per-table
    cosine is pure Python over float tuples — ~1ms for 384-dim vectors
    across <1000 columns. sqlite-vec / numpy upgrade is a future slice
    if MCP traffic shows it's needed.
    """

    store: SQLiteStore
    source_connection_id: str
    embedder: Embedder

    def retrieve(self, query: str, *, limit: int) -> list[str]:
        if limit <= 0:
            return []
        # Skip the embedder call entirely on empty/whitespace queries —
        # otherwise we'd waste an ONNX inference and then have to handle
        # the zero-norm cosine downstream.
        if not query.strip():
            return []

        query_vec = self.embedder.embed(query)

        scored: list[tuple[float, str, str]] = []
        for schema, table in self.store.list_tables(source_connection_id=self.source_connection_id):
            embeddings = self.store.get_table_embeddings(
                schema, table, source_connection_id=self.source_connection_id
            )
            if not embeddings:
                continue
            best = max(_cosine_similarity(query_vec, e.vector) for e in embeddings.values())
            if best > 0.0:
                scored.append((best, schema, table))

        # Sort by descending score, then by qualified name for stable
        # ordering when scores tie (reproducible test + eval output).
        scored.sort(key=lambda x: (-x[0], x[1], x[2]))
        return [f"{s}.{t}" for _, s, t in scored[:limit]]


__all__ = ["EmbeddingRetriever", "KeywordRetriever", "Retriever"]
