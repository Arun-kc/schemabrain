"""Internal URL sanitisation for Postgres engine construction.

Co-located with the connectors that consume it so any caller —
CLI, library, test — that builds a Postgres-backed engine gets
the filter automatically, without remembering to call a CLI-side
helper.

The contract: any URL handed to SQLAlchemy `create_engine` (directly
or via `PostgresDataSource` / `PostgresProfiler`) must first pass
through `safe_engine_url(...)`. The raw URL flows to anything that
needs identity (e.g. a source-id hash) so a smuggled URL and a clean
URL pointing at the same database produce the same identity.
"""

from __future__ import annotations

import logging
from typing import NewType
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# URL query-string parameters allowed to flow through to `create_engine`.
# SQLAlchemy passes URL query parameters through to the libpq layer,
# and a malicious connection string like `?options=-c statement_timeout=1`
# smuggles session config the agent never asked for. The allowlist is
# what's explicitly permitted; we add to it when a real use case shows
# up, never on speculation.
URL_QUERY_PARAM_ALLOWLIST: frozenset[str] = frozenset(
    {"sslmode", "sslrootcert", "application_name"}
)

# Distinct return type so a future call site can't accidentally pass
# the raw URL to `create_engine` instead of the filtered one — the
# type checker catches the substitution at review time. Zero runtime
# cost.
SafeEngineUrl = NewType("SafeEngineUrl", str)

_logger = logging.getLogger("schemabrain.security.safe_url")


def safe_engine_url(url: str) -> SafeEngineUrl:
    """Return `url` with non-allowlisted query parameters stripped.

    Used everywhere Schema Brain hands a URL to SQLAlchemy
    `create_engine` (directly or via `PostgresDataSource` /
    `PostgresProfiler`). The raw URL is preserved by callers that
    need a source-identity hash (e.g. `_make_source_id`) so a
    smuggled URL hashes the same as its clean counterpart pointing
    at the same database.

    Dropped parameter names are emitted at DEBUG severity (visible
    under `-vv`) so an operator who passes a legitimate-but-unrecognised
    param like `connect_timeout=30` can find the dropped key in their
    logs. The default verbosity stays silent — telling an attacker
    which payload key was detected gives away the filter's shape.

    See `URL_QUERY_PARAM_ALLOWLIST` for the explicit set of params
    that survive the filter.
    """
    parsed = urlparse(url)
    if not parsed.query:
        return SafeEngineUrl(url)
    # `parse_qsl` with `keep_blank_values=True` preserves `?key=`
    # forms that some libpq driver variants treat as meaningful.
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    safe_pairs = [(k, v) for (k, v) in pairs if k in URL_QUERY_PARAM_ALLOWLIST]
    dropped = sorted({k for (k, _) in pairs} - URL_QUERY_PARAM_ALLOWLIST)
    if dropped:
        _logger.debug("dropped non-allowlisted URL query params: %s", ",".join(dropped))
    safe_query = urlencode(safe_pairs)
    return SafeEngineUrl(urlunparse(parsed._replace(query=safe_query)))
