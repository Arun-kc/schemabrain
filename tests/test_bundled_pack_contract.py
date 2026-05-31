"""Exact-name contract for the bundled demo pack.

`test_demo_fixture_consistency.py` checks the pack GENERICALLY — every
entity binds to *some* table, every metric/join references *some*
entity, with floors of >=8 / >=7 / >=3 applied files. That lets a
rename, rebind, drop, add, or entity-repoint of a demo YAML slip
through silently as long as the floor still holds.

These tests pin the EXACT named surface of the default ("ecommerce")
pack: the precise set of entity / join / metric names, and for each
file its identifying fields (entity -> bound table + identity column,
join -> source/target entities, metric -> anchor entity). Any drift
becomes a deliberate, test-visible change.

They also lock the contract the Phase-1 `saas` pack must not break on
the ecommerce side: when the ADD-model registry in
`schemabrain.eval.bundled` gains a second pack, these pins guarantee
the ecommerce pack's named surface is unchanged. The wizard-wiring
tests at the bottom pin that the demo pack selected on `WizardConfig`
is the pack actually applied.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from schemabrain.entities.yaml_grammar import parse_entity_yaml_file
from schemabrain.eval.bundled import (
    bundled_entities_fixture_dir,
    bundled_joins_fixture_dir,
    bundled_metrics_fixture_dir,
)
from schemabrain.joins.yaml_grammar import parse_canonical_join_yaml_file
from schemabrain.metrics.yaml_grammar import parse_metric_yaml_file

# The exact named surface of the bundled ecommerce pack, verified
# against the YAML on disk. file stem -> identifying fields.
#   entities: stem -> (qualified_table, identity)
#   joins:    stem -> (source_entity, target_entity)
#   metrics:  stem -> anchor entity
_ENTITY_PINS: dict[str, tuple[str, str]] = {
    "address": ("public.addresses", "id"),
    "category": ("public.categories", "id"),
    "customer": ("public.users", "id"),
    "order": ("public.orders", "id"),
    "order_item": ("public.order_items", "id"),
    "payment_method": ("public.payment_methods", "id"),
    "product": ("public.products", "id"),
    # product_category is the junction-table entity; its identity is
    # product_id, NOT id — a real asymmetry worth pinning.
    "product_category": ("public.product_categories", "product_id"),
}
_JOIN_PINS: dict[str, tuple[str, str]] = {
    "customer_orders": ("order", "customer"),
    "customer_payment_methods": ("payment_method", "customer"),
    # order <-> address is joined by TWO canonical joins (billing +
    # shipping) — the multi-canonical-per-pair demo. Pin both.
    "order_billing_address": ("order", "address"),
    "order_shipping_address": ("order", "address"),
    "order_items_order": ("order_item", "order"),
    "order_items_product": ("order_item", "product"),
    # `product_category` is both an entity name and a join name; the
    # join links the junction entity to its category.
    "product_category": ("product_category", "category"),
}
_METRIC_PINS: dict[str, str] = {
    "customer_count": "order",
    "order_count": "order",
    "total_revenue": "order",
    # total_revenue_real anchors on order_item (line-level revenue),
    # NOT order — the composite-expression metric.
    "total_revenue_real": "order_item",
}


def _stems(directory: Path) -> set[str]:
    return {path.stem for path in directory.glob("*.yaml")}


class TestPackExactSets:
    """The pack is exactly these files — no silent drop or add."""

    def test_entity_pack_is_exactly_eight_named_files(self) -> None:
        assert _stems(bundled_entities_fixture_dir()) == set(_ENTITY_PINS)

    def test_join_pack_is_exactly_seven_named_files(self) -> None:
        assert _stems(bundled_joins_fixture_dir()) == set(_JOIN_PINS)

    def test_metric_pack_is_exactly_four_named_files(self) -> None:
        assert _stems(bundled_metrics_fixture_dir()) == set(_METRIC_PINS)


@pytest.mark.parametrize(
    "stem,qualified_table,identity",
    [(stem, t, i) for stem, (t, i) in sorted(_ENTITY_PINS.items())],
)
def test_entity_name_table_identity(stem: str, qualified_table: str, identity: str) -> None:
    """Each bundled entity pins its name, bound table, and identity column."""
    entity = parse_entity_yaml_file(bundled_entities_fixture_dir() / f"{stem}.yaml")
    assert entity.name == stem
    assert entity.qualified_table == qualified_table
    assert entity.identity == identity


@pytest.mark.parametrize(
    "stem,source_entity,target_entity",
    [(stem, s, t) for stem, (s, t) in sorted(_JOIN_PINS.items())],
)
def test_join_name_source_target(stem: str, source_entity: str, target_entity: str) -> None:
    """Each bundled join pins its name and source/target entities."""
    join = parse_canonical_join_yaml_file(bundled_joins_fixture_dir() / f"{stem}.yaml")
    assert join.name == stem
    assert join.source_entity == source_entity
    assert join.target_entity == target_entity


@pytest.mark.parametrize("stem,anchor", sorted(_METRIC_PINS.items()))
def test_metric_name_anchor_entity(stem: str, anchor: str) -> None:
    """Each bundled metric pins its name and anchor entity."""
    metric = parse_metric_yaml_file(bundled_metrics_fixture_dir() / f"{stem}.yaml")
    assert metric.name == stem
    assert metric.entity == anchor


# ----- wizard wiring: the selected demo pack is the pack applied ----------


def _minimal_demo_cfg(store_path: Path):  # type: ignore[no-untyped-def]
    """A WizardConfig pointing at the demo URL, demo_pack left default."""
    from schemabrain.setup.wizard import WizardConfig

    return WizardConfig(
        source_url="postgresql+psycopg://postgres:local@localhost:5433/postgres",
        store_path=store_path,
        host="manual",
        env_var_name="DEMO_DATABASE_URL",
        skip_index=False,
        no_entities=False,
        enrich=False,
        entities_max_cost_usd=None,
        assume_yes=True,
    )


def test_wizard_config_default_demo_pack_is_ecommerce(tmp_path: Path) -> None:
    """The dataclass default keeps today's single-pack behavior."""
    assert _minimal_demo_cfg(tmp_path / "store.db").demo_pack == "ecommerce"


@pytest.mark.parametrize(
    "kind,getter_name",
    [
        ("entities", "bundled_entities_fixture_dir"),
        ("joins", "bundled_joins_fixture_dir"),
        ("metrics", "bundled_metrics_fixture_dir"),
    ],
)
def test_apply_bundled_demo_yamls_forwards_demo_pack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    getter_name: str,
) -> None:
    """`_apply_bundled_demo_yamls` passes `cfg.demo_pack` to the dir getter.

    The helper does a deferred `from schemabrain.eval.bundled import ...`,
    so patching the attribute on the module is what the lazy import
    picks up. The spy returns an empty dir, so no store seeding is
    needed — we only assert the forwarded `pack` value.
    """
    import schemabrain.eval.bundled as bundled_mod
    from schemabrain.setup.wizard import _apply_bundled_demo_yamls

    recorded: dict[str, object] = {}
    empty_pack_dir = tmp_path / "pack"
    empty_pack_dir.mkdir()

    def _spy(pack: str | None = None) -> Path:
        recorded["pack"] = pack
        return empty_pack_dir

    monkeypatch.setattr(bundled_mod, getter_name, _spy)
    cfg = _minimal_demo_cfg(tmp_path / "store.db")
    applied, failures = _apply_bundled_demo_yamls(kind=kind, cfg=cfg, source_id="src-1")

    assert recorded["pack"] == "ecommerce"
    assert applied == 0
    assert failures == []


def test_apply_bundled_demo_yamls_unknown_pack_returns_partial_failure(tmp_path: Path) -> None:
    """An unregistered demo pack surfaces as a (0, [msg]) partial failure.

    The helper's contract is partial-success — it must not crash the
    wizard stage with an uncaught ValueError. Not reachable via the CLI
    today (demo_pack is always "ecommerce"), but pins the guard that
    makes the Phase-1 pack-selector wiring safe.
    """
    from dataclasses import replace

    from schemabrain.setup.wizard import _apply_bundled_demo_yamls

    cfg = replace(_minimal_demo_cfg(tmp_path / "store.db"), demo_pack="saas")
    applied, failures = _apply_bundled_demo_yamls(kind="entities", cfg=cfg, source_id="src-1")

    assert applied == 0
    assert len(failures) == 1
    assert "unknown demo pack" in failures[0]
    assert "saas" in failures[0]
