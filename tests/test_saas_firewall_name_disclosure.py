"""Firewall regression pins — catastrophic column-NAME disclosure.

Surfaced by the v0.5.0 pre-publish firewall sweep against the frozen SaaS
demo schema. The per-column body redaction (placeholders in ``columns``,
``pii_blocked`` refusals on direct/joined access) already held; these pins
lock the *secondary* surfaces that re-disclosed the real catastrophic
column NAME even though the value was protected:

  D1  ``describe_table`` / ``describe_entity`` ``redacted_columns`` listed
      the REAL names alongside the ``<redacted_*>`` placeholders in
      ``columns`` — defeating the placeholder. Now carries placeholders.
  D2  ``get_metric`` unknown-column errors (group_by / filter / measure)
      enumerated every column in ``Allowed columns: [...]`` and
      ``recovery.suggested_args.allowed_columns``, including the blocked
      ones. A blocked column is not a referenceable target, so it is now
      dropped from the hint.

The corpus under ``tests/firewall/`` is e-commerce-named; these pins use
the real SaaS catastrophic column names so a regression on the frozen demo
schema fails loudly. Pure-unit (synthetic store, no Postgres).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.metric import Metric, MetricMeasure
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.mcp import build_server
from schemabrain.mcp.describe_entity import describe_entity_impl
from schemabrain.mcp.describe_table import describe_table_impl

SRC = "saas_fw_src"

# The frozen SaaS catastrophic columns (real names that must NEVER reach
# the agent as plaintext) keyed by table, with their category + a benign
# companion that MUST stay visible (false-positive guard).
_CATASTROPHIC = {
    "public.users": {
        "password_hash": "credential",
    },
    "public.payment_methods": {
        "card_number_last4": "payment_card",
    },
    "public.billing_profiles": {
        "tax_id": "government_id",
        "national_id": "government_id",
    },
}
_ALL_CATASTROPHIC_NAMES = sorted({c for cols in _CATASTROPHIC.values() for c in cols})


def _col(name: str, table: str, ordinal: int, *, pk: bool = False) -> Column:
    return Column(
        name=name,
        table_name=table.split(".", 1)[1],
        schema_name="public",
        data_type="text",
        nullable=not pk,
        ordinal_position=ordinal,
        is_primary_key=pk,
    )


def _seed(store: SQLiteStore) -> None:
    """Seed three catastrophic-bearing SaaS tables + entities + a metric."""
    tables = {
        "public.users": ["id", "email", "full_name", "password_hash", "role"],
        "public.payment_methods": ["id", "workspace_id", "card_number_last4", "card_brand"],
        "public.billing_profiles": [
            "id",
            "workspace_id",
            "legal_entity",
            "tax_id",
            "national_id",
            "country_code",
        ],
    }
    entities = {
        "user": "public.users",
        "payment_method": "public.payment_methods",
        "billing_profile": "public.billing_profiles",
    }
    for qual, names in tables.items():
        tname = qual.split(".", 1)[1]
        store.write_table(
            Table(
                name=tname,
                schema_name="public",
                columns=tuple(_col(n, qual, i + 1, pk=(n == "id")) for i, n in enumerate(names)),
            ),
            source_connection_id=SRC,
        )
        cata = _CATASTROPHIC.get(qual, {})
        if cata:
            store.write_column_pii_tags(
                source_connection_id=SRC,
                qualified_table=qual,
                tags={col: ("pii", frozenset({cat})) for col, cat in cata.items()},
            )
    for ename, qual in entities.items():
        store.write_entity(
            Entity(
                name=ename,
                description="",
                binding=SingleTableBinding(qualified_table=qual),
                identity="id",
            ),
            source_connection_id=SRC,
        )
    store.write_metric(
        Metric(
            name="user_count",
            description="Number of user accounts.",
            entity="user",
            measure=MetricMeasure(agg="count", column="id"),
            time_dimension=None,
            time_grains=(),
        ),
        source_connection_id=SRC,
    )


class _StubEmbedder:
    model_name = "test"
    dimension = 4

    def embed(self, text: str) -> tuple[float, ...]:
        del text
        return (1.0, 0.0, 0.0, 0.0)


class _FakeExecutor:
    """Stub MetricExecutor — never actually called on the unknown-column
    path (the compiler raises before SQL emission), but ``get_metric`` is
    only registered/usable when an executor is configured.
    """

    max_rows: int | None = None

    def execute(self, sql: str, params: dict) -> list[dict]:  # pragma: no cover
        return [{"value": 0}]


# ---- D1: describe_table / describe_entity redacted_columns ------------------


class TestRedactedColumnsCarryNoRealName:
    def test_describe_table_redacted_columns_holds_no_real_catastrophic_name(
        self, tmp_path: Path
    ) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed(store)
            for qual, cata in _CATASTROPHIC.items():
                detail = describe_table_impl(
                    store=store, source_connection_id=SRC, qualified_name=qual
                )
                blob = json.dumps(detail.model_dump())
                for real_name in cata:
                    assert real_name not in detail.redacted_columns, (
                        f"NAME LEAK: describe_table({qual}).redacted_columns "
                        f"discloses real name {real_name!r}: {detail.redacted_columns}"
                    )
                    assert real_name not in blob, (
                        f"NAME LEAK: describe_table({qual}) body contains {real_name!r}"
                    )
                # the redaction still HAPPENED — placeholders present, count matches
                assert len(detail.redacted_columns) == len(cata)
                assert all(rc.startswith("<redacted_") for rc in detail.redacted_columns)

    def test_describe_table_keeps_benign_companions_visible(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed(store)
            detail = describe_table_impl(
                store=store, source_connection_id=SRC, qualified_name="public.payment_methods"
            )
            names = [c.name for c in detail.columns]
            assert "card_brand" in names  # benign — must stay visible
            assert "card_number_last4" not in names  # catastrophic — placeholdered

    def test_describe_entity_redacted_columns_holds_no_real_catastrophic_name(
        self, tmp_path: Path
    ) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed(store)
            for ename in ("user", "payment_method", "billing_profile"):
                detail = describe_entity_impl(store=store, source_connection_id=SRC, name=ename)
                blob = json.dumps(detail.model_dump())
                for real_name in _ALL_CATASTROPHIC_NAMES:
                    assert real_name not in detail.redacted_columns, (
                        f"NAME LEAK: describe_entity({ename}).redacted_columns "
                        f"discloses {real_name!r}: {detail.redacted_columns}"
                    )
                    assert real_name not in blob, (
                        f"NAME LEAK: describe_entity({ename}) body contains {real_name!r}"
                    )


# ---- D2: get_metric unknown-column allowed_columns enumeration --------------


def _call_tool(server: object, name: str, args: dict) -> dict:
    loop = asyncio.new_event_loop()
    try:
        _content, structured = loop.run_until_complete(server.call_tool(name, args))  # type: ignore[attr-defined]
        return structured
    finally:
        loop.close()


class TestUnknownColumnHintDropsBlockedColumns:
    def test_group_by_typo_does_not_enumerate_blocked_column(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed(store)
            server = build_server(
                store=store,
                source_connection_id=SRC,
                embedder=_StubEmbedder(),
                metric_executor=_FakeExecutor(),
            )
            env = _call_tool(server, "get_metric", {"name": "user_count", "group_by": ["user.zzz"]})
            blob = json.dumps(env)
            assert "password_hash" not in blob, (
                f"NAME LEAK: get_metric unknown-group_by enumerates the blocked "
                f"column in the error/recovery: {blob}"
            )
            # the hint still helps — benign columns survive
            err = env.get("error") or {}
            allowed = (
                (err.get("recovery") or {}).get("suggested_args", {}).get("allowed_columns", [])
            )
            assert "role" in allowed and "email" in allowed, (
                f"hint over-scrubbed benign columns: {allowed}"
            )
            assert "password_hash" not in allowed


# ---- D3: bundled SaaS metric descriptions must not name a catastrophic col --


class TestDemoMetricDescriptionsNameNoCatastrophicColumn:
    def test_no_bundled_saas_metric_description_names_a_catastrophic_column(self) -> None:
        from schemabrain.eval.bundled import bundled_metrics_fixture_dir
        from schemabrain.metrics.yaml_grammar import parse_metric_yaml_file

        catastrophic_names = (
            "password_hash",
            "api_key_hash",
            "session_token",
            "card_number_last4",
            "tax_id",
            "national_id",
        )
        offenders: list[str] = []
        for yaml_path in sorted(bundled_metrics_fixture_dir(pack="saas").glob("*.yaml")):
            metric = parse_metric_yaml_file(yaml_path)
            desc = (metric.description or "").lower()
            for name in catastrophic_names:
                if name in desc:
                    offenders.append(f"{yaml_path.name}: names {name!r}")
        assert not offenders, (
            "bundled SaaS metric description names a catastrophic column "
            f"(list_metrics would surface it to the agent): {offenders}"
        )
