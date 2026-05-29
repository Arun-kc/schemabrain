"""FW-002 (IF-1) — ``find_relevant_tables`` and
``find_relevant_entities`` leak raw catastrophic-leak column names.

Background:

    ``schemabrain/mcp/find_relevant_tables.py:102-117`` and
    ``find_relevant_entities.py:251-276`` do NOT consult ``pii_block``
    or the catastrophic-leak floor. They surface ``best_column`` and
    ``best_column_description`` raw. The ``describe_table`` module
    docstring explicitly claims the firewall applies at the agent-
    facing boundary — but the semantic-search surface bypasses it.

Expected SECURE behaviour:

    The two ``find_relevant_*`` tools are both agent-facing — they're
    the entry points an LLM uses to discover what tables exist.
    ``describe_table`` redacts ``password_hash`` → ``<redacted_credential_column_1>``
    before returning to the agent. The discovery tools must apply the
    same redaction floor — either (a) drop hits where the winning
    column is in the catastrophic-leak set, OR (b) swap the
    ``best_column`` field for the same placeholder ``describe_table``
    uses. Today they do neither.

Repro shape:

  1. Create a Postgres table ``users(password_hash, ssn, credit_card)``.
  2. Seed the SchemaBrain store with that table, column descriptions,
     and FakeEmbedder embeddings keyed on the descriptions.
  3. Call ``find_relevant_tables_impl`` with a query whose text
     matches one of the credential column descriptions exactly
     (cosine 1.0 against FakeEmbedder).
  4. Assert the response does NOT contain the raw column name —
     today it DOES.

Pure in-process — no real Postgres queries needed because the
firewall runs over the SchemaBrain store layer, not over live SQL.
The store is the source of truth ``find_relevant_tables`` consults.
"""

from __future__ import annotations

import pytest

from schemabrain.core.description import ColumnDescription
from schemabrain.core.embedding import ColumnEmbedding
from schemabrain.core.models import Column, Table
from schemabrain.mcp.find_relevant_tables import find_relevant_tables_impl
from schemabrain.pii import CATASTROPHIC_LEAK_CATEGORIES

pytestmark = [pytest.mark.firewall_bypass]

SOURCE_ID = "fw_002"

# Per-column axis vectors. Each column maps to a distinct unit basis
# vector so cosine = 1.0 when the query is the same key.
_DIM = 4
_AXIS = {
    "id": (1.0, 0.0, 0.0, 0.0),
    "password_hash": (0.0, 1.0, 0.0, 0.0),
    "ssn": (0.0, 0.0, 1.0, 0.0),
    "credit_card": (0.0, 0.0, 0.0, 1.0),
}


class _AxisEmbedder:
    """Scripted embedder: query text -> pre-registered unit vector.

    Avoids the FakeEmbedder hash-overflow path and lets each test
    target one column deterministically.
    """

    model_name = "firewall-axis"
    dimension = _DIM

    def __init__(self, script: dict[str, tuple[float, ...]]) -> None:
        self._script = script

    def embed(self, text: str) -> tuple[float, ...]:
        if text not in self._script:
            raise KeyError(f"unscripted embed text: {text!r}")
        return self._script[text]


def _seed_users_table_with_credentials(store) -> None:
    """Write a ``public.users`` table whose columns include credential PII.

    The columns mirror the the original repro: ``password_hash``,
    ``ssn`` (government_id), and ``credit_card`` (payment_card). Each
    gets a one-line description; the description text is also used as
    the embedding seed so a query with the same string scores cosine
    1.0 — deterministic without any real LLM.
    """
    table = Table(
        name="users",
        schema_name="public",
        columns=(
            Column(
                name="id",
                table_name="users",
                schema_name="public",
                data_type="bigint",
                nullable=False,
                ordinal_position=1,
                is_primary_key=True,
            ),
            Column(
                name="password_hash",
                table_name="users",
                schema_name="public",
                data_type="text",
                nullable=False,
                ordinal_position=2,
            ),
            Column(
                name="ssn",
                table_name="users",
                schema_name="public",
                data_type="text",
                nullable=True,
                ordinal_position=3,
            ),
            Column(
                name="credit_card",
                table_name="users",
                schema_name="public",
                data_type="text",
                nullable=True,
                ordinal_position=4,
            ),
        ),
    )
    store.write_table(table, source_connection_id=SOURCE_ID)

    descriptions = {
        "id": "Primary key",
        "password_hash": "bcrypt password hash for user authentication",
        "ssn": "user social security number",
        "credit_card": "stored credit card primary account number",
    }
    store.write_table_descriptions(
        schema_name="public",
        name="users",
        source_connection_id=SOURCE_ID,
        descriptions={
            col: ColumnDescription(
                text=text,
                model="fake-llm",
                prompt_version="v1",
                input_tokens=1,
                cached_input_tokens=0,
                output_tokens=1,
                cost_usd=0.0,
            )
            for col, text in descriptions.items()
        },
    )

    # Embed each column to its axis vector. A query that maps to the
    # same axis (via `_AxisEmbedder`'s script) wins cosine = 1.0
    # against that column and cosine = 0.0 against everything else.
    embeddings = {
        col: ColumnEmbedding(vector=_AXIS[col], model="firewall-axis", dimension=_DIM)
        for col in descriptions
    }
    store.write_table_embeddings(
        "public",
        "users",
        source_connection_id=SOURCE_ID,
        embeddings=embeddings,
    )

    # Also write PII tags — the bypass is that the discovery tool
    # ignores these. We write them so the test demonstrates the tags
    # exist in the store but aren't consulted at the discovery layer.
    store.write_column_pii_tags(
        source_connection_id=SOURCE_ID,
        qualified_table="public.users",
        tags={
            "password_hash": ("pii", frozenset({"credential"})),
            "ssn": ("pii", frozenset({"government_id"})),
            "credit_card": ("pii", frozenset({"payment_card"})),
        },
    )


@pytest.mark.parametrize(
    ("query", "leaked_column", "leaked_category"),
    [
        ("user passwords", "password_hash", "credential"),
        ("social security", "ssn", "government_id"),
        ("credit card numbers", "credit_card", "payment_card"),
    ],
)
def test_find_relevant_tables_does_not_leak_catastrophic_column_name(
    store, query: str, leaked_column: str, leaked_category: str
) -> None:
    """``find_relevant_tables`` MUST NOT surface a raw column name in the
    catastrophic-leak set.

    Today this fails — ``best_column`` carries ``password_hash`` /
    ``ssn`` / ``credit_card`` verbatim despite the column's tag being
    in ``CATASTROPHIC_LEAK_CATEGORIES``.
    """
    _seed_users_table_with_credentials(store)
    embedder = _AxisEmbedder({query: _AXIS[leaked_column]})

    assert leaked_category in CATASTROPHIC_LEAK_CATEGORIES, (
        f"sanity: test fixture should target a catastrophic-leak category, got {leaked_category!r}"
    )

    hits = find_relevant_tables_impl(
        store=store,
        source_connection_id=SOURCE_ID,
        embedder=embedder,
        query=query,
        limit=5,
    )

    # There should be at least one hit — sanity check that the seed
    # wired up.
    assert hits, f"sanity: expected at least one hit for query {query!r}"

    # The bypass surface: ``best_column`` carries the raw catastrophic
    # column name AND ``best_column_description`` carries its LLM-
    # generated semantic text. Both are surfaces an agent reads
    # directly. Assertion below is the SECURE shape; today it FAILS.
    leaking = [h for h in hits if h.best_column == leaked_column]
    assert not leaking, (
        f"BYPASS: find_relevant_tables surfaced the raw catastrophic-"
        f"leak column {leaked_column!r} (category={leaked_category}) "
        f"in best_column for query {query!r}. describe_table redacts "
        f"this column's name; the discovery tool must redact too. "
        f"Leaked TableHit shapes: {[(h.qualified_name, h.best_column, h.best_column_description) for h in leaking]}"
    )
