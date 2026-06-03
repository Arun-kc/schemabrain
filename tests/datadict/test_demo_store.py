"""The offline SaaS fixture is faithful to saas.sql and the bundled packs.

These tests are the safety net under the byte-exact golden: if the
fixture drifts from `saas.sql` (a column added, a PII leg lost) or from
the bundled YAML packs, they fail here with a precise message rather than
as an inscrutable golden diff.
"""

from __future__ import annotations

import re
from pathlib import Path

from schemabrain.core.source_id import make_source_id
from schemabrain.core.store import SQLiteStore
from schemabrain.datadict.demo_store import (
    DICTIONARY_SOURCE_URL,
    SOURCE_ID,
    build_saas_dictionary_store,
)
from schemabrain.eval.bundled import (
    bundled_entities_fixture_dir,
    bundled_joins_fixture_dir,
    bundled_metrics_fixture_dir,
    resolve_bundled_path,
)

_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:public\.)?(\w+)",
    re.IGNORECASE,
)

# Expected catastrophic legs: column -> the catastrophic category it carries.
_CATASTROPHIC_LEGS = {
    ("public.users", "password_hash"): "credential",
    ("public.api_keys", "api_key_hash"): "credential",
    ("public.sessions", "session_token"): "credential",
    ("public.payment_methods", "card_number_last4"): "payment_card",
    ("public.billing_profiles", "tax_id"): "government_id",
    ("public.billing_profiles", "national_id"): "government_id",
}


def _saas_sql_table_names() -> frozenset[str]:
    sql = Path(resolve_bundled_path("saas.sql")).read_text(encoding="utf-8")
    return frozenset(_CREATE_TABLE_RE.findall(sql))


def test_source_id_is_pinned() -> None:
    # The golden + dogfood are keyed to this id; a URL/scheme edit that
    # shifts it must fail loudly here, not silently re-key the fixture.
    assert make_source_id(DICTIONARY_SOURCE_URL) == SOURCE_ID


def test_builder_writes_every_saas_table(tmp_path: Path) -> None:
    build_saas_dictionary_store(tmp_path / "dict.db")
    with SQLiteStore(tmp_path / "dict.db") as store:
        names = {name for _schema, name in store.list_tables(source_connection_id=SOURCE_ID)}
    assert names == _saas_sql_table_names()


# Per-table column counts transcribed from saas.sql. Pins the shape so a
# same-total intra-set column move (which the 84-total + table-set guards
# would miss) fails here, not as an opaque golden diff.
_EXPECTED_COLUMN_COUNTS = {
    "workspaces": 7,
    "plans": 7,
    "users": 8,
    "subscriptions": 8,
    "subscription_items": 5,
    "payment_methods": 8,
    "invoices": 8,
    "api_keys": 7,
    "sessions": 6,
    "usage_events": 6,
    "support_tickets": 7,
    "billing_profiles": 7,
}


def test_builder_transcribes_84_columns(tmp_path: Path) -> None:
    build_saas_dictionary_store(tmp_path / "dict.db")
    total = 0
    with SQLiteStore(tmp_path / "dict.db") as store:
        for schema, name in store.list_tables(source_connection_id=SOURCE_ID):
            table = store.get_table(schema, name, source_connection_id=SOURCE_ID)
            assert table is not None
            total += len(table.columns)
    assert total == 84
    assert sum(_EXPECTED_COLUMN_COUNTS.values()) == 84  # the map itself is consistent


def test_builder_per_table_column_counts(tmp_path: Path) -> None:
    build_saas_dictionary_store(tmp_path / "dict.db")
    with SQLiteStore(tmp_path / "dict.db") as store:
        counts = {}
        for schema, name in store.list_tables(source_connection_id=SOURCE_ID):
            table = store.get_table(schema, name, source_connection_id=SOURCE_ID)
            assert table is not None
            counts[name] = len(table.columns)
    assert counts == _EXPECTED_COLUMN_COUNTS


def test_builder_lights_all_three_catastrophic_legs(tmp_path: Path) -> None:
    build_saas_dictionary_store(tmp_path / "dict.db")
    with SQLiteStore(tmp_path / "dict.db") as store:
        for (qualified, column), expected_category in _CATASTROPHIC_LEGS.items():
            tags = store.get_column_pii_tags(
                source_connection_id=SOURCE_ID,
                qualified_table=qualified,
                columns=[column],
            )
            assert column in tags, f"{qualified}.{column} carries no PII tag"
            _sensitivity, categories = tags[column]
            assert expected_category in categories, (
                f"{qualified}.{column} expected {expected_category!r}, got {sorted(categories)}"
            )


def test_builder_applies_full_semantic_pack(tmp_path: Path) -> None:
    build_saas_dictionary_store(tmp_path / "dict.db")
    expected_entities = len(list(bundled_entities_fixture_dir(pack="saas").glob("*.yaml")))
    expected_joins = len(list(bundled_joins_fixture_dir(pack="saas").glob("*.yaml")))
    expected_metrics = len(list(bundled_metrics_fixture_dir(pack="saas").glob("*.yaml")))
    with SQLiteStore(tmp_path / "dict.db") as store:
        assert len(store.list_entities(source_connection_id=SOURCE_ID)) == expected_entities
        assert len(store.list_canonical_joins(source_connection_id=SOURCE_ID)) == expected_joins
        assert len(store.list_metrics(source_connection_id=SOURCE_ID)) == expected_metrics


def test_builder_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "dict.db"
    build_saas_dictionary_store(db)
    build_saas_dictionary_store(db)  # second run must not duplicate rows
    with SQLiteStore(db) as store:
        assert len(store.list_tables(source_connection_id=SOURCE_ID)) == 12
        assert len(store.list_entities(source_connection_id=SOURCE_ID)) == len(
            list(bundled_entities_fixture_dir(pack="saas").glob("*.yaml"))
        )
