"""Demo fixture consistency: bundled YAMLs must resolve against the SQL.

The wizard's demo path auto-applies every YAML under the bundled
fixtures directories. Any entity bound to a non-existent table, or any
metric/join referencing a non-existent entity, would surface to a
new user as a confusing "failed to apply X" message at the end of
`schemabrain init`. These tests pin the invariant so a future fixture
edit can't drift silently.

Layers checked:

  - Entities: `binding.single_table` resolves to a CREATE TABLE in
    `schemabrain/eval/fixtures/ecommerce.sql`.
  - Metrics: `entity:` references an entity in the bundled pack.
  - Joins: `source_entity` + `target_entity` reference entities in
    the bundled pack.

Static (no Postgres needed); runs in the standard pytest pass.
"""

from __future__ import annotations

import re
from pathlib import Path

from schemabrain.entities.yaml_grammar import parse_entity_yaml_file
from schemabrain.eval.bundled import (
    bundled_entities_fixture_dir,
    bundled_joins_fixture_dir,
    bundled_metrics_fixture_dir,
    resolve_bundled_path,
)
from schemabrain.joins.yaml_grammar import parse_canonical_join_yaml_file
from schemabrain.metrics.yaml_grammar import parse_metric_yaml_file

_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:public\.)?(\w+)",
    re.IGNORECASE,
)


def _ecommerce_table_names() -> frozenset[str]:
    sql = Path(resolve_bundled_path("ecommerce.sql")).read_text(encoding="utf-8")
    return frozenset(_CREATE_TABLE_RE.findall(sql))


def _bundled_entity_names() -> dict[str, Path]:
    return {path.stem: path for path in sorted(bundled_entities_fixture_dir().glob("*.yaml"))}


def test_every_bundled_entity_binds_to_an_ecommerce_table() -> None:
    tables = _ecommerce_table_names()
    failures: list[str] = []
    for yaml_path in sorted(bundled_entities_fixture_dir().glob("*.yaml")):
        entity = parse_entity_yaml_file(yaml_path)
        table = entity.qualified_table.replace("public.", "")
        if table not in tables:
            failures.append(f"{yaml_path.name}: binds to public.{table} (not in ecommerce.sql)")
    assert not failures, "\n".join(failures)


def test_every_bundled_metric_references_an_entity_in_the_pack() -> None:
    entity_names = set(_bundled_entity_names())
    failures: list[str] = []
    for yaml_path in sorted(bundled_metrics_fixture_dir().glob("*.yaml")):
        metric = parse_metric_yaml_file(yaml_path)
        if metric.entity not in entity_names:
            failures.append(
                f"{yaml_path.name}: anchors on entity {metric.entity!r} "
                f"(not in bundled entity pack)"
            )
    assert not failures, "\n".join(failures)


def test_every_bundled_join_references_entities_in_the_pack() -> None:
    entity_names = set(_bundled_entity_names())
    failures: list[str] = []
    for yaml_path in sorted(bundled_joins_fixture_dir().glob("*.yaml")):
        join = parse_canonical_join_yaml_file(yaml_path)
        if join.source_entity not in entity_names:
            failures.append(
                f"{yaml_path.name}: source_entity {join.source_entity!r} not in bundled entity pack"
            )
        if join.target_entity not in entity_names:
            failures.append(
                f"{yaml_path.name}: target_entity {join.target_entity!r} not in bundled entity pack"
            )
    assert not failures, "\n".join(failures)


def test_payment_methods_table_present_for_pii_demo() -> None:
    """The PII-firewall demo needs both v2 surfaces to be wheel-shipped."""
    sql = Path(resolve_bundled_path("ecommerce.sql")).read_text(encoding="utf-8")
    assert "payment_methods" in sql, (
        "payment_methods table missing from ecommerce.sql — the canonical-join "
        "PII propagation demo (users ↔ payment_methods) won't fire"
    )
    assert "password_hash" in sql, (
        "password_hash column missing from ecommerce.sql users table — the "
        "default --pii-block policy won't refuse the demo's first query"
    )


# ----- End-to-end smoke: the wizard's apply-bundled helper against a real store ----


def test_apply_bundled_demo_yamls_lands_full_pack(tmp_path: Path) -> None:
    """End-to-end: the wizard's demo branch applies entities + joins + metrics.

    Mirrors the wizard's stage 3/4/5 demo path against a temp SQLite
    store seeded with the ecommerce tables. Verifies the helper writes
    every bundled YAML successfully, with no per-file failures, and
    that the resulting store reflects the canonical-join PII-
    propagation surface (customer + payment_method present, with a
    join connecting them).
    """
    from dataclasses import replace

    from schemabrain.cli import _make_source_id
    from schemabrain.core.models import Column, Table
    from schemabrain.core.store import SQLiteStore
    from schemabrain.setup.wizard import WizardConfig, _apply_bundled_demo_yamls

    source_url = "postgresql+psycopg://postgres:local@localhost:5433/postgres"
    source_id = _make_source_id(source_url)
    store_path = tmp_path / "store.db"

    def _pk(name: str, table: str) -> Column:
        return Column(
            name=name,
            table_name=table,
            schema_name="public",
            data_type="bigint",
            nullable=False,
            ordinal_position=1,
            is_primary_key=True,
        )

    # Seed every table referenced by the bundled entity pack so the
    # FK guards in `Store.write_entity` pass. Mirror of the helper in
    # `tests/test_joins_e2e.py` but locally scoped to keep the smoke
    # standalone.
    with SQLiteStore(store_path) as store:
        for table_name in (
            "users",
            "orders",
            "order_items",
            "products",
            "categories",
            "addresses",
            "product_categories",
            "payment_methods",
        ):
            store.write_table(
                Table(name=table_name, schema_name="public", columns=(_pk("id", table_name),)),
                source_connection_id=source_id,
            )

    cfg = WizardConfig(
        source_url=source_url,
        store_path=store_path,
        host="manual",
        env_var_name="DEMO_DATABASE_URL",
        skip_index=False,
        no_entities=False,
        enrich=False,
        entities_max_cost_usd=None,
        assume_yes=True,
    )
    # Stage 3 — entities first (joins + metrics depend on them).
    cfg_with_path = replace(cfg, store_path=store_path)
    applied_e, failed_e = _apply_bundled_demo_yamls(
        kind="entities", cfg=cfg_with_path, source_id=source_id
    )
    assert failed_e == [], f"entity apply had per-file failures: {failed_e}"
    assert applied_e >= 8, f"expected >=8 bundled entities, applied {applied_e}"

    # Stage 5 — joins next (order matters: customer_payment_methods
    # references both customer and payment_method).
    applied_j, failed_j = _apply_bundled_demo_yamls(
        kind="joins", cfg=cfg_with_path, source_id=source_id
    )
    assert failed_j == [], f"join apply had per-file failures: {failed_j}"
    assert applied_j >= 7, f"expected >=7 bundled joins, applied {applied_j}"

    # Stage 4 — metrics last.
    applied_m, failed_m = _apply_bundled_demo_yamls(
        kind="metrics", cfg=cfg_with_path, source_id=source_id
    )
    assert failed_m == [], f"metric apply had per-file failures: {failed_m}"
    assert applied_m >= 3, f"expected >=3 bundled metrics, applied {applied_m}"

    # The canonical-PII-propagation surface: customer + payment_method
    # entities + a join connecting them must all be present in the
    # store so the firewall has something to propagate PII tags
    # across at query time.
    with SQLiteStore(store_path) as store:
        joins = store.list_canonical_joins(source_connection_id=source_id)
        join_names = {j.name for j in joins}
    assert "customer_payment_methods" in join_names
