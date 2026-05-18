"""Shared pytest fixtures.

The Postgres-backed fixtures here boot a single ephemeral container per
test session via testcontainers, then seed it with a known schema. Tests
using these fixtures must be marked `@pytest.mark.integration` so that
contributors without Docker can skip them with `-m "not integration"`.
"""

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer


@pytest.fixture(scope="session")
def _pg_container() -> Iterator[PostgresContainer]:
    with PostgresContainer("postgres:16-alpine", driver="psycopg") as container:
        yield container


@pytest.fixture(scope="session")
def pg_url(_pg_container: PostgresContainer) -> str:
    return _pg_container.get_connection_url()


@pytest.fixture(scope="session")
def seeded_pg_url(pg_url: str) -> str:
    """Connection URL for a Postgres seeded with a small known schema.

    Schema (single creation, session-wide):

      public.orgs (id PK, name NOT NULL)
      public.users (id PK, email NOT NULL, created_at NOT NULL DEFAULT now())
      public.org_members (org_id FK->orgs.id, user_id FK->users.id,
                          role nullable, composite PK on (org_id, user_id))
      audit.events (id PK, payload nullable)
      partitioning.events_by_region (id BIGINT, region TEXT, composite PK,
                                     PARTITION BY LIST (region))
        partitioning.events_us (PARTITION OF events_by_region FOR VALUES IN ('us'))
        partitioning.events_eu (PARTITION OF events_by_region FOR VALUES IN ('eu'))
    """
    engine = create_engine(pg_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE orgs (
                        id BIGSERIAL PRIMARY KEY,
                        name TEXT NOT NULL
                    );
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE users (
                        id BIGSERIAL PRIMARY KEY,
                        email TEXT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    );
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE org_members (
                        org_id BIGINT NOT NULL REFERENCES orgs(id),
                        user_id BIGINT NOT NULL REFERENCES users(id),
                        role TEXT,
                        PRIMARY KEY (org_id, user_id)
                    );
                    """
                )
            )
            conn.execute(text("CREATE SCHEMA audit;"))
            conn.execute(
                text(
                    """
                    CREATE TABLE audit.events (
                        id BIGSERIAL PRIMARY KEY,
                        payload TEXT
                    );
                    """
                )
            )
            conn.execute(text("CREATE SCHEMA partitioning;"))
            conn.execute(
                text(
                    """
                    CREATE TABLE partitioning.events_by_region (
                        id BIGINT NOT NULL,
                        region TEXT NOT NULL,
                        payload TEXT,
                        PRIMARY KEY (id, region)
                    ) PARTITION BY LIST (region);
                    """
                )
            )
            conn.execute(
                text(
                    "CREATE TABLE partitioning.events_us "
                    "PARTITION OF partitioning.events_by_region FOR VALUES IN ('us');"
                )
            )
            conn.execute(
                text(
                    "CREATE TABLE partitioning.events_eu "
                    "PARTITION OF partitioning.events_by_region FOR VALUES IN ('eu');"
                )
            )
    finally:
        engine.dispose()
    return pg_url


@pytest.fixture(scope="session")
def profiling_pg_url(pg_url: str) -> str:
    """Connection URL for a Postgres seeded with a row-populated profiling schema.

    Schema (single creation, session-wide; isolated from `seeded_pg_url`):

      profiling.empty_table        -- 0 rows
      profiling.users_profile      -- 6 rows; mixes PII, mixed nulls, all-null,
                                      long values, repeating shapes, and a
                                      regression row (id=6) where a credit-card
                                      number sits inside a 1100-char bio so we
                                      can exercise the truncation/redaction order.
      profiling."weird name"       -- 2 rows, identifiers that exercise quoting
                                      ("select" reserved word, "x;DROP" injection-shaped).
                                      Uses the 'profiling' schema; if a future
                                      seeded_pg_url test ever needs that schema
                                      name, both fixtures must coordinate.
    """
    engine = create_engine(pg_url)
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE SCHEMA profiling;"))
            conn.execute(
                text(
                    """
                    CREATE TABLE profiling.empty_table (
                        id BIGINT,
                        name TEXT
                    );
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE profiling.users_profile (
                        id BIGINT NOT NULL,
                        email TEXT NOT NULL,
                        middle_name TEXT,
                        phone TEXT,
                        bio TEXT
                    );
                    """
                )
            )
            long_bio = "x" * 200
            # Regression row for the truncation-then-redaction PII bug: the
            # CC number sits ~40 chars in, so a naive `value[:50]` then
            # `redact_pii` would split it and leak the trailing digits.
            cc_at_position_40 = ("x" * 40) + "4111111111111111" + ("y" * 1100)
            conn.execute(
                text(
                    """
                    INSERT INTO profiling.users_profile
                        (id, email, middle_name, phone, bio)
                    VALUES
                        (1, 'alice@acme.com', NULL, '555-123-4567', 'short'),
                        (2, 'bob@acme.com',   NULL, '555-987-6543', :long_bio),
                        (3, 'carol@acme.com', NULL, NULL,           'medium length bio'),
                        (4, 'dave@acme.com',  NULL, '555-111-2222', NULL),
                        (5, 'eve@acme.com',   NULL, NULL,           NULL),
                        (6, 'frank@acme.com', NULL, NULL,           :cc_bio)
                    """
                ),
                {"long_bio": long_bio, "cc_bio": cc_at_position_40},
            )
            conn.execute(
                text('CREATE TABLE profiling."weird name" ("select" TEXT, "x;DROP" TEXT);')
            )
            conn.execute(
                text(
                    'INSERT INTO profiling."weird name" ("select", "x;DROP") '
                    "VALUES ('a', 'b'), ('c', 'd');"
                )
            )
            # Regression table for the `%`-in-identifier bug: psycopg's
            # pyformat paramstyle treats `%` as a parameter marker, so
            # routing identifier-only SQL through `text()` double-escapes
            # any `%` baked into a column name (`%` -> `%%` -> `%%%%`)
            # and the query dies in Postgres parse. Real example:
            # BIRD's California Schools DB has columns like
            # `Percent (%) Eligible Free (K-12)`.
            conn.execute(
                text(
                    'CREATE TABLE profiling."pct_columns" '
                    '("Win %" INT, "Percent (%) Eligible" TEXT, "no_percent" INT);'
                )
            )
            conn.execute(
                text(
                    'INSERT INTO profiling."pct_columns" '
                    '("Win %", "Percent (%) Eligible", "no_percent") '
                    "VALUES (50, 'yes', 1), (75, 'no', 2), (NULL, 'yes', 3);"
                )
            )
            # Regression table for the no-equality column-type bug: the
            # 2026-05-18 manual smoke against AdventureWorks crashed the
            # profiler with `psycopg.errors.UndefinedFunction: could not
            # identify an equality operator for type xml` because the
            # batched profile query emitted `COUNT(DISTINCT col)` against
            # an `xml` column. The fix is to detect no-equality types and
            # substitute `NULL::bigint` for the DISTINCT slot. Two such
            # types here — `xml` (AW's actual offender) and `point` (a
            # representative geometric type) — to lock in that `_supports_
            # distinct` short-circuits on more than just xml.
            conn.execute(
                text(
                    "CREATE TABLE profiling.no_equality_types ("
                    "id BIGINT NOT NULL, "
                    "doc XML, "
                    "loc POINT"
                    ");"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO profiling.no_equality_types (id, doc, loc) "
                    "VALUES "
                    "(1, '<r><a/></r>'::xml, '(1,1)'::point), "
                    "(2, '<r><b/></r>'::xml, '(2,2)'::point), "
                    "(3, NULL, NULL);"
                )
            )
    finally:
        engine.dispose()
    return pg_url
