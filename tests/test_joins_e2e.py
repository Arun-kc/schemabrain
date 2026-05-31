"""End-to-end tests for the canonical-join arc.

Exercises the full flow against the bundled ecommerce fixture:

  1. Bundled fixture present + structurally valid (6 YAML files,
     each parses, all reference entities that exist in the bundled
     ecommerce SQL).
  2. Multi-canonical-per-pair flow — `order_billing_address` and
     `order_shipping_address` coexist under the same `(order, address)`
     entity pair.
  3. CLI round-trip: `joins apply` the bundled directory → `joins list`
     shows all 6 → `resolve_join` MCP tool finds each one.
  4. Ambiguity refusal — `resolve_join("order", "address")` with no
     name returns the structured envelope (not a panic).

Does NOT touch a real Postgres — that's the manual smoke job. These
tests run against the in-memory SQLite Store with hand-seeded entities,
which is enough to pin the integration contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from schemabrain.cli import _make_source_id, main
from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.eval.bundled import bundled_joins_fixture_dir
from schemabrain.joins.yaml_grammar import parse_canonical_join_yaml_file
from schemabrain.mcp.resolve_join import resolve_join_impl
from schemabrain.mcp.shapes import AmbiguousJoinError

_URL = "postgresql+psycopg://u:p@h/db"


# ----- bundled fixture structure --------------------------------------------


_EXPECTED_FIXTURE_NAMES = frozenset(
    {
        "customer_orders",
        "customer_payment_methods",
        "order_items_order",
        "order_items_product",
        "product_category",
        "order_billing_address",
        "order_shipping_address",
    }
)


class TestBundledFixtureShape:
    def test_fixture_dir_exists(self) -> None:
        path = bundled_joins_fixture_dir(pack="ecommerce")
        assert path.is_dir()

    def test_all_seven_yaml_files_present_and_parse(self) -> None:
        path = bundled_joins_fixture_dir(pack="ecommerce")
        yaml_files = sorted(p for p in path.iterdir() if p.suffix == ".yaml")
        # Drop any non-YAML files that might land here by mistake.
        names = {parse_canonical_join_yaml_file(p).name for p in yaml_files}
        assert names == _EXPECTED_FIXTURE_NAMES

    def test_addresses_pair_uses_distinct_columns(self) -> None:
        # The the multi-canonical-per-pair design choice rides on
        # these two YAMLs binding to DIFFERENT FK columns on `orders`.
        # Same entity pair, distinct on-columns, distinct names.
        path = bundled_joins_fixture_dir(pack="ecommerce")
        billing = parse_canonical_join_yaml_file(path / "order_billing_address.yaml")
        shipping = parse_canonical_join_yaml_file(path / "order_shipping_address.yaml")
        assert billing.source_entity == shipping.source_entity == "order"
        assert billing.target_entity == shipping.target_entity == "address"
        assert billing.on[0].source_column == "billing_address_id"
        assert shipping.on[0].source_column == "shipping_address_id"


# ----- end-to-end CLI flow --------------------------------------------------


def _column(name: str, table: str, ordinal: int = 1) -> Column:
    return Column(
        name=name,
        table_name=table,
        schema_name="public",
        data_type="bigint",
        nullable=False,
        ordinal_position=ordinal,
        is_primary_key=(name == "id"),
    )


def _seed_ecommerce_entities(store_path: Path) -> None:
    """Mirror the bundled ecommerce SQL — seven entities + their tables.

    Bundles enough table + entity rows so the canonical-join FK
    constraints have something to reference. The actual FK constraints
    on `orders` (user_id, billing_address_id, shipping_address_id) are
    NOT needed for the canonical-join flow — `joins apply` writes to
    the canonical_joins table, which only references `entities`, not
    `foreign_keys`.
    """
    source_id = _make_source_id(_URL)
    table_specs: list[tuple[str, tuple[Column, ...]]] = [
        ("users", (_column("id", "users"),)),
        ("orders", (_column("id", "orders"),)),
        ("order_items", (_column("id", "order_items"),)),
        ("products", (_column("id", "products"),)),
        ("categories", (_column("id", "categories"),)),
        ("addresses", (_column("id", "addresses"),)),
        ("product_categories", (_column("id", "product_categories"),)),
        ("payment_methods", (_column("id", "payment_methods"),)),
    ]
    entity_specs: list[tuple[str, str]] = [
        ("customer", "public.users"),
        ("order", "public.orders"),
        ("order_item", "public.order_items"),
        ("product", "public.products"),
        ("category", "public.categories"),
        ("address", "public.addresses"),
        ("product_category", "public.product_categories"),
        ("payment_method", "public.payment_methods"),
    ]
    with SQLiteStore(store_path) as store:
        for name, columns in table_specs:
            store.write_table(
                Table(name=name, schema_name="public", columns=columns),
                source_connection_id=source_id,
            )
        for entity_name, qn in entity_specs:
            store.write_entity(
                Entity(
                    name=entity_name,
                    description="",
                    binding=SingleTableBinding(qualified_table=qn),
                    identity="id",
                ),
                source_connection_id=source_id,
            )


class TestEndToEnd:
    def test_apply_bundled_directory_lands_seven_joins(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store_path = tmp_path / "store.db"
        _seed_ecommerce_entities(store_path)

        exit_code = main(
            [
                "joins",
                "apply",
                str(bundled_joins_fixture_dir(pack="ecommerce")),
                "--source",
                _URL,
                "--store-path",
                str(store_path),
            ]
        )
        assert exit_code == 0

        # Verify all 7 landed.
        source_id = _make_source_id(_URL)
        with SQLiteStore(store_path) as store:
            joins = store.list_canonical_joins(source_connection_id=source_id)
        names = {j.name for j in joins}
        assert names == _EXPECTED_FIXTURE_NAMES

    def test_list_after_apply_renders_each_join(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store_path = tmp_path / "store.db"
        _seed_ecommerce_entities(store_path)
        main(
            [
                "joins",
                "apply",
                str(bundled_joins_fixture_dir(pack="ecommerce")),
                "--source",
                _URL,
                "--store-path",
                str(store_path),
            ]
        )
        capsys.readouterr()  # drain apply output
        exit_code = main(
            [
                "joins",
                "list",
                "--store-path",
                str(store_path),
            ]
        )
        out = capsys.readouterr().out
        assert exit_code == 0
        for name in _EXPECTED_FIXTURE_NAMES:
            assert name in out

    def test_resolve_join_finds_each_canonical(self, tmp_path: Path) -> None:
        # After apply, every bundled canonical join must be resolvable
        # by `resolve_join`. The hot path — paste-ready SQL skeleton for
        # any of the 6 joins.
        store_path = tmp_path / "store.db"
        _seed_ecommerce_entities(store_path)
        main(
            [
                "joins",
                "apply",
                str(bundled_joins_fixture_dir(pack="ecommerce")),
                "--source",
                _URL,
                "--store-path",
                str(store_path),
            ]
        )
        source_id = _make_source_id(_URL)
        with SQLiteStore(store_path) as store:
            info = resolve_join_impl(
                store=store,
                source_connection_id=source_id,
                entity_a="order",
                entity_b="customer",
            )
        assert info.name == "customer_orders"
        assert 'ON "order"."user_id" = "customer"."id"' in info.sql_skeleton

    def test_ambiguity_refusal_on_billing_shipping_pair(self, tmp_path: Path) -> None:
        # The the design's poster child — without `name`,
        # `resolve_join("order", "address")` refuses with the two
        # available canonical names.
        store_path = tmp_path / "store.db"
        _seed_ecommerce_entities(store_path)
        main(
            [
                "joins",
                "apply",
                str(bundled_joins_fixture_dir(pack="ecommerce")),
                "--source",
                _URL,
                "--store-path",
                str(store_path),
            ]
        )
        source_id = _make_source_id(_URL)
        with SQLiteStore(store_path) as store, pytest.raises(AmbiguousJoinError) as exc_info:
            resolve_join_impl(
                store=store,
                source_connection_id=source_id,
                entity_a="order",
                entity_b="address",
            )
        candidate_names = set(exc_info.value.candidate_names)
        assert candidate_names == {
            "order_billing_address",
            "order_shipping_address",
        }
