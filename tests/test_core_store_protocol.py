"""Tests for the Store Protocol + the hardening on SQLiteStore.

Three responsibility lines covered here:

1. **Protocol conformance** — `SQLiteStore` satisfies the runtime
   `isinstance` check against `Store`. Any future store backend must
   pass the same check.

2. **PRAGMA hardening** — every PRAGMA documented on `SQLiteStore`
   is actually in effect on a freshly-opened connection. Each PRAGMA
   gets its own test pinning the resulting value via a `PRAGMA <name>;`
   round-trip — without these, a future contributor could quietly
   drop one and the regression would only surface as a hard-to-debug
   production behaviour change.

3. **Opt-in writer lock** — the `writer_lock=True` constructor flag
   serialises writes across processes via `BEGIN IMMEDIATE`. The
   async-enrichment cost-ledger path uses this flag.

Plus a guard that `SQLiteStore.search_embeddings_topk` exists on the
Protocol — the body itself is exercised by
`tests/test_core_store_search_embeddings_topk.py`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from schemabrain.core.store import SQLiteStore
from schemabrain.core.store_protocol import Store


class TestStoreProtocolConformance:
    """`SQLiteStore` must satisfy the Protocol via runtime `isinstance`.

    The Protocol is `@runtime_checkable`, so `isinstance(store, Store)`
    is a structural check on method presence. If a future refactor
    drops a method or renames it, this fires.
    """

    def test_sqlite_store_is_a_store(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "sb.db") as store:
            assert isinstance(store, Store)

    def test_in_memory_sqlite_store_is_a_store(self) -> None:
        with SQLiteStore(":memory:") as store:
            assert isinstance(store, Store)

    @pytest.mark.parametrize(
        "member_name",
        [
            "writer_lock",  # property
            "write_table",
            "get_table",
            "delete_table",
            "list_tables",
            "get_table_fingerprints",
            "write_table_fingerprints",
            "get_table_descriptions",
            "write_table_descriptions",
            "get_foreign_keys_targeting",
            "list_all_foreign_keys",
            "get_table_embeddings",
            "write_table_embeddings",
            "search_embeddings_topk",
            "close",
        ],
    )
    def test_protocol_exposes_member(self, member_name: str) -> None:
        # Pin every public member name individually so a future rename
        # surfaces the exact member that drifted, not "Protocol
        # doesn't match" in the aggregate. Parametrise-by-name keeps
        # the failure messages readable in CI logs.
        assert hasattr(Store, member_name), (
            f"Store Protocol is missing public member {member_name!r}"
        )


class TestPragmaHardening:
    """Every PRAGMA `SQLiteStore` promises is in effect on a fresh conn.

    The PRAGMA list:
      - foreign_keys = ON       (FK enforcement; off by default)
      - journal_mode = WAL      (concurrent readers + single writer)
      - synchronous = NORMAL    (WAL-compatible; FULL is overkill)
      - temp_store = MEMORY     (faster sorts/joins on retrieval)
      - mmap_size = 256 MB      (cache speedup for 10k-col stores)
      - busy_timeout = 5000 ms  (graceful wait when writer lock held)

    In-memory stores can't use WAL — SQLite forces the journal mode to
    MEMORY. That branch is tested separately.
    """

    def _pragma(self, store: SQLiteStore, name: str) -> object:
        # Pull the live PRAGMA value off the store's own connection.
        # Accessing `_conn` is a deliberate test-only handle — we want
        # to verify the SAME connection callers will use, not a fresh
        # one that might have different defaults.
        conn: sqlite3.Connection = store._require_conn()
        return conn.execute(f"PRAGMA {name};").fetchone()[0]

    def test_foreign_keys_is_on(self, tmp_path: Path) -> None:
        # FK enforcement is OFF by default in SQLite. We rely on FK
        # cascades for `delete_table` to clean up `columns`, `foreign_keys`,
        # `column_fingerprints`, `column_descriptions`, and
        # `column_embeddings`. Without this PRAGMA, a delete leaves
        # orphan rows.
        with SQLiteStore(tmp_path / "sb.db") as store:
            assert self._pragma(store, "foreign_keys") == 1

    def test_journal_mode_is_wal_for_file_backed(self, tmp_path: Path) -> None:
        # WAL allows concurrent readers while a writer holds the file,
        # which is critical for `serve` running alongside `index`.
        with SQLiteStore(tmp_path / "sb.db") as store:
            assert self._pragma(store, "journal_mode") == "wal"

    def test_journal_mode_falls_back_for_in_memory(self) -> None:
        # SQLite forces in-memory DBs to journal_mode=MEMORY regardless
        # of what we ask for. This test pins that we don't crash trying
        # to set WAL on an in-memory store.
        with SQLiteStore(":memory:") as store:
            assert self._pragma(store, "journal_mode") == "memory"

    def test_synchronous_skipped_for_in_memory(self) -> None:
        # The WAL group (journal_mode + synchronous) only applies to
        # file-backed stores. For in-memory stores, the ctor must NOT
        # issue `PRAGMA synchronous = NORMAL` — there's no fsync to
        # tune, and forcing NORMAL would create a misleading invariant
        # for anyone reading the PRAGMA list ("synchronous=NORMAL
        # means we're on WAL"). Pin that the in-memory path leaves
        # synchronous at SQLite's default for memory DBs, which is
        # FULL (2) at the time of this commit.
        with SQLiteStore(":memory:") as store:
            assert self._pragma(store, "synchronous") == 2

    def test_synchronous_is_normal(self, tmp_path: Path) -> None:
        # synchronous=NORMAL is the recommended WAL pairing. FULL is the
        # default and is overkill on WAL; OFF risks corruption on power
        # loss. NORMAL is the documented sweet spot for WAL stores.
        # SQLite reports this as an int: 0=OFF, 1=NORMAL, 2=FULL, 3=EXTRA.
        with SQLiteStore(tmp_path / "sb.db") as store:
            assert self._pragma(store, "synchronous") == 1

    def test_temp_store_is_memory(self, tmp_path: Path) -> None:
        # Faster sorts/joins for retrieval queries (the embedding
        # cosine path runs an ORDER BY over `column_embeddings`).
        # Values: 0=DEFAULT, 1=FILE, 2=MEMORY.
        with SQLiteStore(tmp_path / "sb.db") as store:
            assert self._pragma(store, "temp_store") == 2

    def test_mmap_size_is_at_least_256mb(self, tmp_path: Path) -> None:
        # SQLite may cap mmap_size below what we request based on the
        # platform's address space. Pin the request, not the literal:
        # accept anything >= our target since a smaller value is also
        # safe (just less speedup).
        with SQLiteStore(tmp_path / "sb.db") as store:
            assert self._pragma(store, "mmap_size") >= 268_435_456

    def test_busy_timeout_is_set(self, tmp_path: Path) -> None:
        # 5000ms — when the writer-lock path holds BEGIN IMMEDIATE,
        # readers that hit the lock should wait, not error. Pin the
        # exact value so a future drop to 0 (effectively disabling
        # the wait) is caught.
        with SQLiteStore(tmp_path / "sb.db") as store:
            assert self._pragma(store, "busy_timeout") == 5000


class TestWriterLock:
    """The opt-in `writer_lock` constructor flag.

    Off by default. Commit 1 stores the flag as a public attribute
    but `SQLiteStore`'s write methods do NOT yet branch on it. The
    contention test below exercises SQLite's `BEGIN IMMEDIATE`
    mechanism directly, proving the infrastructure works under load
    before Commit 2 wires the write methods to opt into it.
    """

    def test_writer_lock_off_by_default(self, tmp_path: Path) -> None:
        # No flag → flag is False. Pin via a public-ish attribute so
        # the inverse case (`writer_lock=True`) can also be observed.
        with SQLiteStore(tmp_path / "sb.db") as store:
            assert store.writer_lock is False

    def test_writer_lock_on_when_requested(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "sb.db", writer_lock=True) as store:
            assert store.writer_lock is True

    def test_writer_lock_serialises_concurrent_writers(self, tmp_path: Path) -> None:
        # Two processes both try to write to the same store with
        # writer_lock=True. The second writer must wait for the first
        # to commit. We simulate "two processes" via two separate
        # SQLiteStore instances pointed at the same file.
        db_path = tmp_path / "sb.db"
        # Create the store once so the schema is initialised.
        SQLiteStore(db_path).close()

        # Open two connections, both with the writer lock. Start a
        # BEGIN IMMEDIATE on the first; the second's attempt to also
        # BEGIN IMMEDIATE should return `OperationalError: database
        # is locked` after the 5-second busy_timeout. Use a tighter
        # timeout for the test to avoid 5 s wall-clock per run.
        store_a = SQLiteStore(db_path, writer_lock=True)
        store_b = SQLiteStore(db_path, writer_lock=True)
        # Tight busy_timeout for this contention probe only — the
        # production value (5000 ms) is fine but slow for a test.
        store_b._require_conn().execute("PRAGMA busy_timeout = 50;")
        try:
            store_a._require_conn().execute("BEGIN IMMEDIATE;")
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                store_b._require_conn().execute("BEGIN IMMEDIATE;")
        finally:
            store_a._require_conn().execute("ROLLBACK;")
            store_a.close()
            store_b.close()


class TestSearchEmbeddingsTopKProtocolSlot:
    """`search_embeddings_topk` exists on the Protocol.

    Body coverage lives in `test_core_store_search_embeddings_topk.py`.
    This class only pins the Protocol slot itself so a rename here
    surfaces independently from the body tests.
    """

    def test_search_embeddings_topk_in_protocol(self) -> None:
        assert hasattr(Store, "search_embeddings_topk")
