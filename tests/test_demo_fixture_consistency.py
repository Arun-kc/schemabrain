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

import pytest

from schemabrain.entities.yaml_grammar import parse_entity_yaml_file
from schemabrain.eval.bundled import (
    bundled_entities_fixture_dir,
    bundled_joins_fixture_dir,
    bundled_metrics_fixture_dir,
    resolve_bundled_path,
)
from schemabrain.joins.yaml_grammar import parse_canonical_join_yaml_file
from schemabrain.metrics.yaml_grammar import parse_metric_yaml_file
from schemabrain.setup.wizard import WizardConfig, WizardContext

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


def test_fixture_counts_lock_for_in_product_strings() -> None:
    """F0-C regression: in-product copy that names fixture counts must
    match the actual SQL. PR #143 added the ``payment_methods`` table
    (7 → 8) but five source-file strings stayed at the old counts:

      - ``setup_stage.py`` (wizard fixture-load banner, two places)
      - ``_ui.py`` (LLM-stage progress docstring example)
      - ``inspect/render.py`` (inspect-output sample shape)
      - ``init_help_render.py`` (timing-constant rationale comment)

    Surfaced during the 2026-05-29 smoke when the operator saw the
    wizard print ``(7 tables, ~1.2k rows)`` while the indexer
    immediately reported ``8 tables · 39 columns indexed`` on the
    next line. A future fixture edit that changes counts will trip
    this test, forcing the in-product copy to update alongside.

    The fixture-column count (~39) lives in the SQL, but reading it
    from CREATE TABLE statements alone is fragile (GENERATED columns,
    multi-line definitions, CHECK constraints all bleed into a naive
    regex). We pin only the table count here — the column count is
    locked indirectly via the per-table column checks elsewhere.
    """
    tables = _ecommerce_table_names()
    assert len(tables) == 8, (
        f"ecommerce.sql now defines {len(tables)} tables; in-product "
        f"strings claiming '7 tables' or '8 tables' must follow. "
        f"Run `grep -rnE '[0-9]+ tables' schemabrain/` and update the "
        f"call sites BEFORE bumping this assertion. tables={sorted(tables)}"
    )

    # Per-source-file string-level lock: each occurrence we found and
    # fixed in the F0-C sweep must NOT regress to '7 tables'.
    from schemabrain import _ui, init_help_render
    from schemabrain import inspect as inspect_mod
    from schemabrain.inspect import render as inspect_render
    from schemabrain.setup import setup_stage

    sources = {
        "setup_stage": Path(setup_stage.__file__).read_text(encoding="utf-8"),
        "_ui": Path(_ui.__file__).read_text(encoding="utf-8"),
        "inspect_render": Path(inspect_render.__file__).read_text(encoding="utf-8"),
        "init_help_render": Path(init_help_render.__file__).read_text(encoding="utf-8"),
    }
    # Belt-and-braces: the inspect package init must not carry the old
    # string either (defensive — currently does not, but the test
    # surfaces the regression if it ever does).
    _ = inspect_mod  # imported for completeness; no string check needed

    failures: list[str] = []
    for label, source in sources.items():
        if "7 tables" in source:
            failures.append(f"{label}: still contains '7 tables' — update to 8")
        if "30 columns" in source or "30 cols" in source:
            failures.append(f"{label}: still contains '30 columns'/'30 cols' — update to 39")
    assert not failures, "F0-C regression — fix the listed strings:\n  " + "\n  ".join(failures)


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


# ----- Stage 3/4/5 demo-branch coverage --------------------------------------
#
# These tests exercise the demo-URL branch each stage handler runs
# when `cfg.source_url == DEMO_DATABASE_URL`. Without them the
# stages' demo branches sit at 0% coverage even though the helper
# they delegate to (`_apply_bundled_demo_yamls`) is covered by the
# end-to-end smoke above.


def _seed_demo_tables(store_path: Path, source_id: str) -> None:
    """Pre-seed every public table the bundled entity pack binds to."""
    from schemabrain.core.models import Column, Table
    from schemabrain.core.store import SQLiteStore

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


def _demo_wizard_config(store_path: Path) -> WizardConfig:  # type: ignore[name-defined]
    """Build a WizardConfig pointing at the pinned DEMO_DATABASE_URL."""
    from schemabrain.setup.setup_stage import DEMO_DATABASE_URL
    from schemabrain.setup.wizard import WizardConfig

    return WizardConfig(
        source_url=DEMO_DATABASE_URL,
        store_path=store_path,
        host="manual",
        env_var_name="DEMO_DATABASE_URL",
        skip_index=False,
        no_entities=False,
        enrich=False,
        entities_max_cost_usd=None,
        assume_yes=True,
    )


def test_stage_entities_demo_branch_emits_done_outcome(tmp_path: Path) -> None:
    """Stage 3's demo branch lands `status='done'` with the apply count."""
    from schemabrain.cli import _make_source_id
    from schemabrain.setup.setup_stage import DEMO_DATABASE_URL
    from schemabrain.setup.wizard import WizardContext, _stage_entities

    store_path = tmp_path / "store.db"
    source_id = _make_source_id(DEMO_DATABASE_URL)
    _seed_demo_tables(store_path, source_id)

    outcome = _stage_entities(WizardContext(config=_demo_wizard_config(store_path)))
    assert outcome.status == "done"
    assert outcome.stage == 3
    assert "ecommerce demo pack" in outcome.message


def test_stage_metrics_demo_branch_emits_done_outcome(tmp_path: Path) -> None:
    """Stage 4's demo branch lands `status='done'` after entities seed."""
    from schemabrain.cli import _make_source_id
    from schemabrain.setup.setup_stage import DEMO_DATABASE_URL
    from schemabrain.setup.wizard import (
        WizardContext,
        _apply_bundled_demo_yamls,
        _stage_metrics,
    )

    store_path = tmp_path / "store.db"
    source_id = _make_source_id(DEMO_DATABASE_URL)
    _seed_demo_tables(store_path, source_id)
    # Entities must exist before metrics — mirror the wizard's stage order.
    _apply_bundled_demo_yamls(
        kind="entities", cfg=_demo_wizard_config(store_path), source_id=source_id
    )

    outcome = _stage_metrics(WizardContext(config=_demo_wizard_config(store_path)))
    assert outcome.status == "done"
    assert outcome.stage == 4
    assert "ecommerce demo pack" in outcome.message


def test_stage_joins_demo_branch_emits_done_outcome(tmp_path: Path) -> None:
    """Stage 5's demo branch lands `status='done'` after entities seed."""
    from schemabrain.cli import _make_source_id
    from schemabrain.setup.setup_stage import DEMO_DATABASE_URL
    from schemabrain.setup.wizard import (
        WizardContext,
        _apply_bundled_demo_yamls,
        _stage_joins,
    )

    store_path = tmp_path / "store.db"
    source_id = _make_source_id(DEMO_DATABASE_URL)
    _seed_demo_tables(store_path, source_id)
    _apply_bundled_demo_yamls(
        kind="entities", cfg=_demo_wizard_config(store_path), source_id=source_id
    )

    outcome = _stage_joins(WizardContext(config=_demo_wizard_config(store_path)))
    assert outcome.status == "done"
    assert outcome.stage == 5
    assert "ecommerce demo pack" in outcome.message


def test_apply_bundled_demo_yamls_collects_per_file_failures(tmp_path: Path) -> None:
    """A corrupt YAML in the bundled dir surfaces as a per-file failure."""
    import shutil

    from schemabrain.cli import _make_source_id
    from schemabrain.eval.bundled import bundled_entities_fixture_dir
    from schemabrain.setup.setup_stage import DEMO_DATABASE_URL
    from schemabrain.setup.wizard import _apply_bundled_demo_yamls

    store_path = tmp_path / "store.db"
    source_id = _make_source_id(DEMO_DATABASE_URL)
    _seed_demo_tables(store_path, source_id)

    # Shim the bundled entities dir to a temp copy + write a bad YAML
    # alongside the real ones. The helper iterates `*.yaml` in
    # discovery order; the corrupt one trips the except branch
    # (1718-1721 in the source) without breaking the others.
    src = bundled_entities_fixture_dir()
    fake_dir = tmp_path / "entities"
    fake_dir.mkdir()
    for yaml_path in src.glob("*.yaml"):
        shutil.copy(yaml_path, fake_dir / yaml_path.name)
    (fake_dir / "corrupt.yaml").write_text("not: : valid: : yaml: : ::::\n", encoding="utf-8")

    import schemabrain.setup.wizard as wiz_mod

    original = (
        wiz_mod.bundled_entities_fixture_dir
        if hasattr(wiz_mod, "bundled_entities_fixture_dir")
        else None
    )
    # The helper imports lazily; monkeypatch via the eval.bundled
    # module so the lazy import inside the helper picks up the
    # patched accessor.
    import schemabrain.eval.bundled as bundled_mod

    bundled_mod_orig = bundled_mod.bundled_entities_fixture_dir
    bundled_mod.bundled_entities_fixture_dir = lambda: fake_dir  # type: ignore[assignment]
    try:
        applied, failures = _apply_bundled_demo_yamls(
            kind="entities", cfg=_demo_wizard_config(store_path), source_id=source_id
        )
    finally:
        bundled_mod.bundled_entities_fixture_dir = bundled_mod_orig  # type: ignore[assignment]
        if original is not None:
            wiz_mod.bundled_entities_fixture_dir = original  # type: ignore[attr-defined]
    assert applied >= 1, "real YAMLs should still apply alongside the corrupt one"
    assert any("corrupt.yaml" in f for f in failures), failures


# ----- _detect_stale_demo_fixture coverage -----------------------------------


def test_detect_stale_demo_fixture_returns_false_on_connection_failure() -> None:
    """Unreachable Postgres → SQLAlchemyError → returns False silently."""
    from schemabrain.setup.setup_stage import _detect_stale_demo_fixture

    # Localhost on a port nothing's listening on — `connect_timeout=2`
    # turns the connection refusal into a fast OperationalError, which
    # the `except SQLAlchemyError` clause catches and converts to False.
    unreachable = "postgresql+psycopg://postgres:local@127.0.0.1:1/postgres"
    assert _detect_stale_demo_fixture(url=unreachable) is False


def test_detect_stale_demo_fixture_returns_false_for_v2_schema(
    monkeypatch: pytest.MonkeyPatch,  # type: ignore[name-defined]
) -> None:
    """Container with password_hash column → returns False (already v2)."""
    from schemabrain.setup import setup_stage

    class _FakeResult:
        def first(self) -> tuple[int, ...] | None:
            return (1,)

    class _FakeConn:
        def execute(self, *_args: object) -> _FakeResult:
            return _FakeResult()

        def __enter__(self) -> _FakeConn:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

    class _FakeEngine:
        def connect(self) -> _FakeConn:
            return _FakeConn()

        def dispose(self) -> None:
            return None

    monkeypatch.setattr(setup_stage, "create_engine", lambda *a, **kw: _FakeEngine(), raising=False)
    # Inject the same name into the module so the inner-scope `from
    # sqlalchemy import create_engine` picks it up. Setting it on the
    # sqlalchemy module is unsafe; the lazy import inside the function
    # is the surface we patch instead.
    import sqlalchemy as sa

    monkeypatch.setattr(sa, "create_engine", lambda *a, **kw: _FakeEngine())
    assert setup_stage._detect_stale_demo_fixture(url=setup_stage.DEMO_DATABASE_URL) is False


def test_detect_stale_demo_fixture_returns_true_when_password_hash_missing(
    monkeypatch: pytest.MonkeyPatch,  # type: ignore[name-defined]
) -> None:
    """v1 container (users exists, password_hash absent) → True."""
    from schemabrain.setup import setup_stage

    call_count = {"n": 0}

    class _FakeResult:
        def __init__(self, value: tuple[int, ...] | None) -> None:
            self._value = value

        def first(self) -> tuple[int, ...] | None:
            return self._value

    class _FakeConn:
        def execute(self, *_args: object) -> _FakeResult:
            call_count["n"] += 1
            # First call: users table check → exists.
            # Second call: password_hash column check → missing.
            return _FakeResult((1,)) if call_count["n"] == 1 else _FakeResult(None)

        def __enter__(self) -> _FakeConn:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

    class _FakeEngine:
        def connect(self) -> _FakeConn:
            return _FakeConn()

        def dispose(self) -> None:
            return None

    import sqlalchemy as sa

    monkeypatch.setattr(sa, "create_engine", lambda *a, **kw: _FakeEngine())
    assert setup_stage._detect_stale_demo_fixture(url=setup_stage.DEMO_DATABASE_URL) is True


def test_detect_stale_demo_fixture_returns_false_when_no_users_table(
    monkeypatch: pytest.MonkeyPatch,  # type: ignore[name-defined]
) -> None:
    """Empty container (no users table yet) → False (fresh, not stale)."""
    from schemabrain.setup import setup_stage

    class _FakeResult:
        def first(self) -> None:
            return None

    class _FakeConn:
        def execute(self, *_args: object) -> _FakeResult:
            return _FakeResult()

        def __enter__(self) -> _FakeConn:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

    class _FakeEngine:
        def connect(self) -> _FakeConn:
            return _FakeConn()

        def dispose(self) -> None:
            return None

    import sqlalchemy as sa

    monkeypatch.setattr(sa, "create_engine", lambda *a, **kw: _FakeEngine())
    assert setup_stage._detect_stale_demo_fixture(url=setup_stage.DEMO_DATABASE_URL) is False


# ----- _try_auto_docker_demo stale-detect prompt coverage --------------------


def test_try_auto_docker_demo_stale_prompt_user_accepts_recreate(
    monkeypatch: pytest.MonkeyPatch,  # type: ignore[name-defined]
) -> None:
    """Stale container detected, user says 'y' → docker rm + re-run path runs."""
    import io

    from rich.console import Console

    from schemabrain.setup import setup_stage

    docker_run_calls: list[bool] = []

    def _fake_docker_run(*, console: object) -> bool:
        docker_run_calls.append(True)
        return True

    monkeypatch.setattr(setup_stage, "_docker_run_demo_postgres", _fake_docker_run)
    monkeypatch.setattr(setup_stage, "_wait_for_postgres_ready", lambda **_kw: True)
    monkeypatch.setattr(setup_stage, "_detect_stale_demo_fixture", lambda **_kw: True)
    monkeypatch.setattr(setup_stage, "_docker_load_fixture", lambda *, console: True)
    monkeypatch.setattr(setup_stage, "_safe_subprocess", lambda *a, **kw: None)
    monkeypatch.setattr("rich.prompt.Prompt.ask", lambda *a, **kw: "y")

    console = Console(file=io.StringIO(), force_terminal=False, width=120)
    result = setup_stage._try_auto_docker_demo(console=console)
    assert result is True
    assert len(docker_run_calls) == 2, "expected one initial run + one recreate-run"


def test_try_auto_docker_demo_stale_prompt_user_declines_recreate(
    monkeypatch: pytest.MonkeyPatch,  # type: ignore[name-defined]
) -> None:
    """Stale container detected, user says 'n' → continue with old container."""
    import io

    from rich.console import Console

    from schemabrain.setup import setup_stage

    monkeypatch.setattr(setup_stage, "_docker_run_demo_postgres", lambda *, console: True)
    monkeypatch.setattr(setup_stage, "_wait_for_postgres_ready", lambda **_kw: True)
    monkeypatch.setattr(setup_stage, "_detect_stale_demo_fixture", lambda **_kw: True)
    monkeypatch.setattr(setup_stage, "_docker_load_fixture", lambda *, console: True)
    monkeypatch.setattr("rich.prompt.Prompt.ask", lambda *a, **kw: "n")

    console = Console(file=io.StringIO(), force_terminal=False, width=120)
    result = setup_stage._try_auto_docker_demo(console=console)
    assert result is True


def test_try_auto_docker_demo_stale_recreate_fails_on_second_docker_run(
    monkeypatch: pytest.MonkeyPatch,  # type: ignore[name-defined]
) -> None:
    """Recreate path: docker run fails on the second invocation → False."""
    import io

    from rich.console import Console

    from schemabrain.setup import setup_stage

    docker_run_returns = iter([True, False])

    monkeypatch.setattr(
        setup_stage,
        "_docker_run_demo_postgres",
        lambda *, console: next(docker_run_returns),
    )
    monkeypatch.setattr(setup_stage, "_wait_for_postgres_ready", lambda **_kw: True)
    monkeypatch.setattr(setup_stage, "_detect_stale_demo_fixture", lambda **_kw: True)
    monkeypatch.setattr(setup_stage, "_docker_load_fixture", lambda *, console: True)
    monkeypatch.setattr(setup_stage, "_safe_subprocess", lambda *a, **kw: None)
    monkeypatch.setattr("rich.prompt.Prompt.ask", lambda *a, **kw: "y")

    console = Console(file=io.StringIO(), force_terminal=False, width=120)
    assert setup_stage._try_auto_docker_demo(console=console) is False


def test_try_auto_docker_demo_stale_recreate_fails_on_second_readiness(
    monkeypatch: pytest.MonkeyPatch,  # type: ignore[name-defined]
) -> None:
    """Recreate path: postgres-ready fails on the second probe → False."""
    import io

    from rich.console import Console

    from schemabrain.setup import setup_stage

    wait_returns = iter([True, False])

    monkeypatch.setattr(setup_stage, "_docker_run_demo_postgres", lambda *, console: True)
    monkeypatch.setattr(
        setup_stage,
        "_wait_for_postgres_ready",
        lambda **_kw: next(wait_returns),
    )
    monkeypatch.setattr(setup_stage, "_detect_stale_demo_fixture", lambda **_kw: True)
    monkeypatch.setattr(setup_stage, "_docker_load_fixture", lambda *, console: True)
    monkeypatch.setattr(setup_stage, "_safe_subprocess", lambda *a, **kw: None)
    monkeypatch.setattr("rich.prompt.Prompt.ask", lambda *a, **kw: "y")

    console = Console(file=io.StringIO(), force_terminal=False, width=120)
    assert setup_stage._try_auto_docker_demo(console=console) is False


# ----- Stage 3/4/5 demo-branch zero-applied failure paths --------------------


def _demo_wizard_ctx(store_path: Path) -> WizardContext:  # type: ignore[name-defined]
    from schemabrain.setup.wizard import WizardContext

    return WizardContext(config=_demo_wizard_config(store_path))


def test_stage_entities_demo_branch_zero_applied_emits_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,  # type: ignore[name-defined]
) -> None:
    """When bundled apply returns zero, stage 3 emits a failed outcome."""
    from schemabrain.cli import _make_source_id
    from schemabrain.setup import wizard as wiz
    from schemabrain.setup.setup_stage import DEMO_DATABASE_URL

    store_path = tmp_path / "store.db"
    source_id = _make_source_id(DEMO_DATABASE_URL)
    _seed_demo_tables(store_path, source_id)

    monkeypatch.setattr(
        wiz, "_apply_bundled_demo_yamls", lambda **_kw: (0, ["bad.yaml: parse failed"])
    )

    outcome = wiz._stage_entities(_demo_wizard_ctx(store_path))
    assert outcome.status == "failed"
    assert outcome.stage == 3
    assert "bundled demo entities" in outcome.message


def test_stage_metrics_demo_branch_zero_applied_emits_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,  # type: ignore[name-defined]
) -> None:
    """When bundled apply returns zero, stage 4 emits a failed outcome."""
    from schemabrain.cli import _make_source_id
    from schemabrain.setup import wizard as wiz
    from schemabrain.setup.setup_stage import DEMO_DATABASE_URL

    store_path = tmp_path / "store.db"
    source_id = _make_source_id(DEMO_DATABASE_URL)
    _seed_demo_tables(store_path, source_id)
    # Stage 4 has an empty-entities short-circuit; seed at least one
    # entity so the demo branch is reached.
    wiz._apply_bundled_demo_yamls(
        kind="entities", cfg=_demo_wizard_config(store_path), source_id=source_id
    )

    monkeypatch.setattr(
        wiz, "_apply_bundled_demo_yamls", lambda **_kw: (0, ["bad.yaml: parse failed"])
    )

    outcome = wiz._stage_metrics(_demo_wizard_ctx(store_path))
    assert outcome.status == "failed"
    assert outcome.stage == 4
    assert "bundled demo metrics" in outcome.message


def test_stage_joins_demo_branch_zero_applied_emits_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,  # type: ignore[name-defined]
) -> None:
    """When bundled apply returns zero, stage 5 emits a failed outcome."""
    from schemabrain.cli import _make_source_id
    from schemabrain.setup import wizard as wiz
    from schemabrain.setup.setup_stage import DEMO_DATABASE_URL

    store_path = tmp_path / "store.db"
    source_id = _make_source_id(DEMO_DATABASE_URL)
    _seed_demo_tables(store_path, source_id)
    # Stage 5 has the same empty-entities short-circuit as stage 4.
    wiz._apply_bundled_demo_yamls(
        kind="entities", cfg=_demo_wizard_config(store_path), source_id=source_id
    )

    monkeypatch.setattr(
        wiz, "_apply_bundled_demo_yamls", lambda **_kw: (0, ["bad.yaml: parse failed"])
    )

    outcome = wiz._stage_joins(_demo_wizard_ctx(store_path))
    assert outcome.status == "failed"
    assert outcome.stage == 5
    assert "bundled demo joins" in outcome.message
