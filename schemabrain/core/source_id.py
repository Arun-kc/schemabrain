"""Stable short identifiers for source databases.

Extracted from ``schemabrain.cli`` so the dashboard sidecar (and any
future caller) can compute the canonical source_id from a connection
URL without depending on the CLI module. The hash is deterministic:
the same DB always produces the same source_id regardless of which
user, port-form, or trailing-slash variant indexed it.

Why this matters: a connection URL contains credentials. The store
keys all rows by ``source_id`` (a credential-free SHA-256 short
hash). Echoing the raw URL back over HTTP would leak the DB password.
Use :func:`make_source_id` to obtain the safe identifier instead.
"""

from __future__ import annotations

import hashlib
from urllib.parse import urlparse

# Postgres URL schemes accepted by the project, with their default
# port. Kept in sync with the CLI's accepted-scheme list.
POSTGRES_SCHEMES: dict[str, int] = {
    "postgresql": 5432,
    "postgres": 5432,
    "postgresql+psycopg": 5432,
    "postgresql+psycopg2": 5432,
    "postgresql+asyncpg": 5432,
}

# 16 hex chars = 64 bits of entropy. Per-DB uniqueness target:
# small populations (<1000 DBs) keep birthday-collision probability
# ~10^-14. Bump this if these IDs ever cross a multi-tenant boundary.
SOURCE_ID_LENGTH = 16


def canonical_url(url: str) -> str:
    """Return the credential-free, port-normalized form of a connection URL.

    Strips username + password, normalizes the default port, and trims
    trailing slashes from the path so URLs that point at the same
    database hash to the same canonical form.

    Raises ``ValueError`` for empty input, missing scheme, or an
    unsupported scheme.
    """
    parsed = urlparse(url)
    if not parsed.scheme:
        raise ValueError(f"Invalid connection URL (no scheme): {url!r}")
    if parsed.scheme not in POSTGRES_SCHEMES:
        raise ValueError(
            f"Unsupported scheme {parsed.scheme!r}; expected one of {sorted(POSTGRES_SCHEMES)}"
        )
    port = parsed.port or POSTGRES_SCHEMES[parsed.scheme]
    host = parsed.hostname or ""
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{host}:{port}{path}"


def make_source_id(url: str) -> str:
    """Stable short identifier for a source DB, derived from its URL.

    The output is a 16-char hex digest of the canonical URL form
    (credentials stripped, port normalized). Safe to expose over HTTP
    or log — contains no secrets.
    """
    canonical = canonical_url(url)
    return hashlib.sha256(canonical.encode()).hexdigest()[:SOURCE_ID_LENGTH]


def looks_like_connection_url(value: str) -> bool:
    """True if ``value`` looks like a DB connection URL with a scheme.

    Used by callers that accept either a URL (which must be hashed)
    or a pre-computed source_id (which is already safe to echo).
    """
    return "://" in value and any(value.startswith(s + "://") for s in POSTGRES_SCHEMES)
