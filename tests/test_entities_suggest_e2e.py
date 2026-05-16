"""End-to-end smoke: ecommerce schema -> suggest pipeline -> bundled-fixture parity.

The package ships 3 bundled entity YAMLs (`customer.yaml`,
`order.yaml`, `product.yaml`) that the demo / quickstart load via
`entities apply`.
This test pins that a competent suggest run against the ecommerce
schema produces **the same three entities** — same bindings, same
identity columns — as those bundled fixtures.

The LLM is stubbed (FakeLLMClient) returning a canned response that
matches what a competent run would produce. This is a regression
gate: if a future refactor breaks the suggest pipeline OR breaks the
bundled fixtures, this test fails loudly.

Complements `test_entities_ecommerce_fixtures.py` (which covers
`entities apply` + MCP envelope round-trip for the same YAMLs).
This file covers the `entities suggest` end of the same loop.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from schemabrain.cli import _make_source_id, main
from schemabrain.core.models import Column, ForeignKey, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.enrichment.llm import FakeLLMClient
from schemabrain.entities.suggest import EntitySuggestionPipeline
from schemabrain.entities.yaml_grammar import parse_entity_yaml_file
from schemabrain.eval.entity_harness import ecommerce_fixture, run_entity_eval

_BUNDLED_DIR = (
    Path(__file__).resolve().parent.parent
    / "schemabrain"
    / "eval"
    / "fixtures"
    / "entities"
    / "ecommerce"
)
_TEST_URL = "postgresql+psycopg://user:pw@localhost:5432/db"

# Canned LLM response — matches the descriptions in the bundled YAMLs
# verbatim so parity assertions don't false-positive on cosmetic
# rephrasing. Confidence + rationale + pii_hints are envelope-only
# and don't have to match anything in the bundled files (they're not
# persisted there).
_ECOMMERCE_STUB_RESPONSE = """\
candidates:
  - name: customer
    description: A registered shopper account
    binding:
      single_table: public.users
    identity: id
    confidence: high
    rationale: users has id PK, NOT NULL email, referenced by orders.user_id
    pii_hints:
      email: pii
  - name: order
    description: One placed order, tied to a customer and a status
    binding:
      single_table: public.orders
    identity: id
    confidence: high
    rationale: orders has id PK, status enum, FK into users
    pii_hints: {}
  - name: product
    description: A purchasable product with SKU, name, and price
    binding:
      single_table: public.products
    identity: id
    confidence: high
    rationale: products has id PK, SKU, NOT NULL name
    pii_hints: {}
"""


# ----- shared fixtures -------------------------------------------------------


def _pk(name: str, table: str, *, ordinal: int = 1) -> Column:
    return Column(
        name=name,
        table_name=table,
        schema_name="public",
        data_type="bigint",
        nullable=False,
        ordinal_position=ordinal,
        is_primary_key=True,
    )


def _ecommerce_tables() -> list[Table]:
    """Mirror of the ecommerce.sql fixture, inline so tests don't need
    a Postgres container. Bound tables match the bundled YAML
    fixtures (`public.users`, `public.orders`, `public.products`).
    """
    users = Table(
        name="users",
        schema_name="public",
        columns=(
            _pk("id", "users"),
            Column(
                name="email",
                table_name="users",
                schema_name="public",
                data_type="text",
                nullable=False,
                ordinal_position=2,
            ),
        ),
    )
    orders = Table(
        name="orders",
        schema_name="public",
        columns=(
            _pk("id", "orders"),
            Column(
                name="user_id",
                table_name="orders",
                schema_name="public",
                data_type="bigint",
                nullable=False,
                ordinal_position=2,
            ),
        ),
        foreign_keys=(
            ForeignKey(
                name="orders_user_id_fkey",
                source_columns=("user_id",),
                target_schema="public",
                target_table="users",
                target_columns=("id",),
            ),
        ),
    )
    products = Table(
        name="products",
        schema_name="public",
        columns=(
            _pk("id", "products"),
            Column(
                name="sku",
                table_name="products",
                schema_name="public",
                data_type="text",
                nullable=False,
                ordinal_position=2,
            ),
        ),
    )
    return [users, orders, products]


def _seed_store_with_ecommerce(store_path: Path) -> None:
    source_id = _make_source_id(_TEST_URL)
    with SQLiteStore(store_path) as store:
        for table in _ecommerce_tables():
            store.write_table(table, source_connection_id=source_id)


@pytest.fixture
def ecommerce_stub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCHEMABRAIN_STUB_RESPONSE", _ECOMMERCE_STUB_RESPONSE)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


# ----- e2e: apply mode -------------------------------------------------------


class TestApplyModeAgainstEcommerce:
    def test_apply_lands_three_entities_with_suggested_origin(
        self,
        tmp_path: Path,
        ecommerce_stub_env: None,
    ) -> None:
        store_path = tmp_path / "store.db"
        _seed_store_with_ecommerce(store_path)

        exit_code = main(
            [
                "entities",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
                "--apply",
                "--provider",
                "stub",
            ]
        )
        assert exit_code == 0

        # All three entities landed with origin="suggested".
        source_id = _make_source_id(_TEST_URL)
        with SQLiteStore(store_path) as store:
            entities = store.list_entities(source_connection_id=source_id)

        by_name = {e.name: e for e in entities}
        assert set(by_name) == {"customer", "order", "product"}
        for entity in by_name.values():
            assert entity.origin == "suggested"

        # Bindings match the bundled YAML targets exactly.
        assert by_name["customer"].qualified_table == "public.users"
        assert by_name["order"].qualified_table == "public.orders"
        assert by_name["product"].qualified_table == "public.products"


# ----- e2e: out-dir mode + bundled-fixture parity ---------------------------


class TestOutDirParityWithBundledFixtures:
    def test_out_dir_yamls_match_bundled_fixtures_on_canonical_fields(
        self,
        tmp_path: Path,
        ecommerce_stub_env: None,
    ) -> None:
        """Regenerate the 3 bundled YAMLs via --out-dir; compare against the
        committed bundled fixtures on canonical Entity fields (name,
        description, binding, identity).

        `origin` is NOT compared: bundled fixtures default to "manual";
        suggest-generated YAMLs carry "suggested". The asymmetry is
        intentional — the same downstream entity can arrive via either
        path, and provenance distinguishes them.
        """
        store_path = tmp_path / "store.db"
        _seed_store_with_ecommerce(store_path)
        out_dir = tmp_path / "regenerated"

        exit_code = main(
            [
                "entities",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
                "--out-dir",
                str(out_dir),
                "--provider",
                "stub",
            ]
        )
        assert exit_code == 0

        for fixture_name in ("customer", "order", "product"):
            bundled = parse_entity_yaml_file(_BUNDLED_DIR / f"{fixture_name}.yaml")
            regenerated = parse_entity_yaml_file(out_dir / f"{fixture_name}.yaml")
            # Canonical fields match exactly — this is what catches
            # drift between the suggest pipeline and the demo fixtures.
            assert regenerated.name == bundled.name
            assert regenerated.description == bundled.description
            assert regenerated.qualified_table == bundled.qualified_table
            assert regenerated.identity == bundled.identity
            # Origin asymmetry is intentional.
            assert regenerated.origin == "suggested"
            assert bundled.origin == "manual"


# ----- e2e: dry-run mode ----------------------------------------------------


class TestDryRunAgainstEcommerce:
    def test_dry_run_output_names_all_three_entities(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        ecommerce_stub_env: None,
    ) -> None:
        store_path = tmp_path / "store.db"
        _seed_store_with_ecommerce(store_path)

        exit_code = main(
            [
                "entities",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
                "--dry-run",
                "--provider",
                "stub",
            ]
        )
        assert exit_code == 0
        out = capsys.readouterr().out
        # All three canonical entity names visible.
        for name in ("customer", "order", "product"):
            assert name in out
        # All three bound-table targets visible.
        for target in ("public.users", "public.orders", "public.products"):
            assert target in out
        # Cost summary line is present.
        assert "cost:" in out


# ----- e2e: eval harness perfect-precision floor ----------------------------


class TestEvalHarnessAgainstEcommerce:
    def test_curated_stub_scores_perfect_precision(self) -> None:
        """When the stub LLM returns the curated correct answer, the
        eval harness should report precision@3 = 1.0 against the
        bundled `ecommerce_fixture`.

        This is the floor for step 7's gate: if the harness can't
        score a perfect run as perfect, the gate would be unreachable
        even with a perfect LLM.
        """
        client = FakeLLMClient(text_provider=lambda _s, _u: _ECOMMERCE_STUB_RESPONSE)
        pipeline = EntitySuggestionPipeline(llm=client)

        results = run_entity_eval([ecommerce_fixture()], pipeline, top_k=3)

        assert len(results) == 1
        result = results[0]
        assert result.fixture_name == "ecommerce"
        assert result.precision_at_k == pytest.approx(1.0)
        assert result.recall == pytest.approx(1.0)
        assert result.passed(threshold=0.7) is True
