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
    finally:
        engine.dispose()
    return pg_url
