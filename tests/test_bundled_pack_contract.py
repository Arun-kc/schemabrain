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
_ECOMMERCE_ENTITY_PINS: dict[str, tuple[str, str]] = {
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
_ECOMMERCE_JOIN_PINS: dict[str, tuple[str, str]] = {
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
_ECOMMERCE_METRIC_PINS: dict[str, str] = {
    "customer_count": "order",
    "order_count": "order",
    "total_revenue": "order",
    # total_revenue_real anchors on order_item (line-level revenue),
    # NOT order — the composite-expression metric.
    "total_revenue_real": "order_item",
}

# The v0.5.0 DEFAULT pack: a B2B SaaS control plane (12 entities / 8
# joins / 5 metrics). FROZEN per
# docs/internal/v0.5.0_saas_demo_db_design_spec.md (operator-signed).
_SAAS_ENTITY_PINS: dict[str, tuple[str, str]] = {
    "workspace": ("public.workspaces", "id"),
    "plan": ("public.plans", "id"),
    "user": ("public.users", "id"),
    "subscription": ("public.subscriptions", "id"),
    "subscription_item": ("public.subscription_items", "id"),
    "payment_method": ("public.payment_methods", "id"),
    "invoice": ("public.invoices", "id"),
    "api_key": ("public.api_keys", "id"),
    "session": ("public.sessions", "id"),
    "usage_event": ("public.usage_events", "id"),
    "support_ticket": ("public.support_tickets", "id"),
    "billing_profile": ("public.billing_profiles", "id"),
}
_SAAS_JOIN_PINS: dict[str, tuple[str, str]] = {
    "workspace_users": ("user", "workspace"),
    "workspace_subscriptions": ("subscription", "workspace"),
    "subscription_plan": ("subscription", "plan"),
    "subscription_items_subscription": ("subscription_item", "subscription"),
    "invoice_workspace": ("invoice", "workspace"),
    "invoice_subscription": ("invoice", "subscription"),
    # Catastrophic-propagation joins: the JOIN result inherits the
    # payment_card / credential / government_id tag from the source table.
    "workspace_payment_methods": ("payment_method", "workspace"),
    "user_sessions": ("session", "user"),
    "workspace_api_keys": ("api_key", "workspace"),
    "workspace_billing_profile": ("billing_profile", "workspace"),
    # Plain hub join (no catastrophic column on support_tickets).
    "workspace_support_tickets": ("support_ticket", "workspace"),
}
_SAAS_METRIC_PINS: dict[str, str] = {
    "total_revenue": "invoice",
    # total_revenue_real anchors on subscription_item (line-level
    # revenue, unit_price_cents * seats) — the composite-expression metric.
    "total_revenue_real": "subscription_item",
    "active_subscriptions": "subscription",
    "user_count": "user",
    "usage_volume": "usage_event",
}

# pack name -> (entity pins, join pins, metric pins). Both registered
# packs are pinned so neither's named surface can drift silently.
_PACK_PINS: dict[
    str, tuple[dict[str, tuple[str, str]], dict[str, tuple[str, str]], dict[str, str]]
] = {
    "ecommerce": (_ECOMMERCE_ENTITY_PINS, _ECOMMERCE_JOIN_PINS, _ECOMMERCE_METRIC_PINS),
    "saas": (_SAAS_ENTITY_PINS, _SAAS_JOIN_PINS, _SAAS_METRIC_PINS),
}


def _stems(directory: Path) -> set[str]:
    return {path.stem for path in directory.glob("*.yaml")}


@pytest.mark.parametrize("pack", sorted(_PACK_PINS))
class TestPackExactSets:
    """Each pack is exactly its pinned files — no silent drop or add."""

    def test_entity_pack_is_exactly_its_named_files(self, pack: str) -> None:
        assert _stems(bundled_entities_fixture_dir(pack=pack)) == set(_PACK_PINS[pack][0])

    def test_join_pack_is_exactly_its_named_files(self, pack: str) -> None:
        assert _stems(bundled_joins_fixture_dir(pack=pack)) == set(_PACK_PINS[pack][1])

    def test_metric_pack_is_exactly_its_named_files(self, pack: str) -> None:
        assert _stems(bundled_metrics_fixture_dir(pack=pack)) == set(_PACK_PINS[pack][2])


@pytest.mark.parametrize(
    "pack,stem,qualified_table,identity",
    [
        (pack, stem, t, i)
        for pack, (ents, _j, _m) in sorted(_PACK_PINS.items())
        for stem, (t, i) in sorted(ents.items())
    ],
)
def test_entity_name_table_identity(
    pack: str, stem: str, qualified_table: str, identity: str
) -> None:
    """Each bundled entity pins its name, bound table, and identity column."""
    entity = parse_entity_yaml_file(bundled_entities_fixture_dir(pack=pack) / f"{stem}.yaml")
    assert entity.name == stem
    assert entity.qualified_table == qualified_table
    assert entity.identity == identity


@pytest.mark.parametrize(
    "pack,stem,source_entity,target_entity",
    [
        (pack, stem, s, t)
        for pack, (_e, joins, _m) in sorted(_PACK_PINS.items())
        for stem, (s, t) in sorted(joins.items())
    ],
)
def test_join_name_source_target(
    pack: str, stem: str, source_entity: str, target_entity: str
) -> None:
    """Each bundled join pins its name and source/target entities."""
    join = parse_canonical_join_yaml_file(bundled_joins_fixture_dir(pack=pack) / f"{stem}.yaml")
    assert join.name == stem
    assert join.source_entity == source_entity
    assert join.target_entity == target_entity


@pytest.mark.parametrize(
    "pack,stem,anchor",
    [
        (pack, stem, anchor)
        for pack, (_e, _j, metrics) in sorted(_PACK_PINS.items())
        for stem, anchor in sorted(metrics.items())
    ],
)
def test_metric_name_anchor_entity(pack: str, stem: str, anchor: str) -> None:
    """Each bundled metric pins its name and anchor entity."""
    metric = parse_metric_yaml_file(bundled_metrics_fixture_dir(pack=pack) / f"{stem}.yaml")
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


def test_wizard_config_default_demo_pack_is_saas(tmp_path: Path) -> None:
    """The dataclass default is the v0.5.0 saas pack (kept in sync with
    bundled.DEFAULT_PACK)."""
    from schemabrain.eval.bundled import DEFAULT_PACK

    assert _minimal_demo_cfg(tmp_path / "store.db").demo_pack == "saas"
    assert _minimal_demo_cfg(tmp_path / "store.db").demo_pack == DEFAULT_PACK


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

    # The default demo_pack is now "saas"; the helper forwards it verbatim.
    assert recorded["pack"] == "saas"
    assert applied == 0
    assert failures == []


def test_apply_bundled_demo_yamls_unknown_pack_returns_partial_failure(tmp_path: Path) -> None:
    """An unregistered demo pack surfaces as a (0, [msg]) partial failure.

    The helper's contract is partial-success — it must not crash the
    wizard stage with an uncaught ValueError. `saas` and `ecommerce` are
    both registered now, so the sentinel is a name that is NOT a
    registered pack.
    """
    from dataclasses import replace

    from schemabrain.setup.wizard import _apply_bundled_demo_yamls

    cfg = replace(_minimal_demo_cfg(tmp_path / "store.db"), demo_pack="nonexistent")
    applied, failures = _apply_bundled_demo_yamls(kind="entities", cfg=cfg, source_id="src-1")

    assert applied == 0
    assert len(failures) == 1
    assert "unknown demo pack" in failures[0]
    assert "nonexistent" in failures[0]
