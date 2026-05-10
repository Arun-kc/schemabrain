"""Retriever Protocol + a placeholder keyword-overlap implementation.

The eval runner programs against the `Retriever` Protocol so the
implementation can swap from "naive keyword overlap" today to
"embedding-based ANN retrieval" in Week 4-5 without touching the
runner, the report, or any golden file.

`KeywordRetriever` is intentionally tiny: it tokenizes the query and
each table's combined corpus (table name + column names + column
description text), then ranks tables by overlap count. It exists so
Week 3 can produce a baseline score that Week 4's real retrieval has
to beat — not because keyword overlap is a serious retrieval method.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from schemabrain.core.store import SQLiteStore

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
        tbl = self.store.get_table(
            schema, table, source_connection_id=self.source_connection_id
        )
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


__all__ = ["KeywordRetriever", "Retriever"]
