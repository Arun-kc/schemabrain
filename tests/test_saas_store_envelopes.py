"""Full-envelope proof for the bundled SaaS demo pack.

Closes the P1 gap from the 2026-06-11 demo-maturity audit: the pack's
refusal / recovery / success behaviours were proven only at the
classifier or precondition level (``tests/pii/test_saas_freeze_gate.py``,
the join-island guard in ``test_demo_fixture_consistency.py``) — never
end-to-end through the ``get_metric`` MCP envelope against a SaaS store.

These tests drive ``build_server`` → ``call_tool`` over the offline
``build_saas_dictionary_store`` fixture (12 tables / 84 columns / real
``classify_column`` PII tags / the bundled YAML packs — no Postgres, no
network, no RNG) and assert the wire-level ``ToolResponse`` for every
demo beat:

  * PII floor refusal (credential) + the public-companion positive
    control — the §2.4 non-negotiable.
  * contact-tier refusal under an explicit ``--pii-block contact``
    operator policy + its public-column recovery.
  * ``unreachable_entity`` — the README "Sample session" hero
    (``usage_event`` is a deliberate join-island) AND a real uncovered
    FK edge (``user`` → ``billing_profile``).
  * ``ambiguous_time_dimension`` (S5) refusal + structured recovery.
    It fires on ``total_revenue_real`` — a line-item metric with no
    timestamp of its own that reaches four candidate timestamps via
    canonical joins — and does NOT fire on ``active_subscriptions``,
    which declares its dimension. (The engine is correct; the spec's
    original S5 metric choice was the bug. See the maturity audit.)
  * cross-entity leaderboard success (``total_revenue`` by
    ``plan.title``) — the firewall must ALLOW the legitimate join, not
    refuse it.
  * tamper-evident audit chain over a mixed refused / error / success
    history bound to the SaaS store.

Execution is stubbed (``_StubExecutor`` returns canned rows) because
every beat here is decided at PLAN time: the refusals fire before any
SQL runs, and the success beats assert the emitted SQL shape. Real
Postgres number-level execution stays in the manual smoke job.

Envelope-status contract (see ``mcp/envelope.py`` ``REFUSAL_KINDS``):
only ``pii_blocked`` / ``policy_blocked`` / ``allowlist_violation``
carry ``status="refused"``. Every other refusal kind — including
``ambiguous_time_dimension`` and ``unreachable_entity`` — is
``status="error"``.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from schemabrain.audit.verify import walk_chain
from schemabrain.audit.writer import AuditWriter
from schemabrain.core.store import SQLiteStore
from schemabrain.datadict.demo_store import build_saas_dictionary_store
from schemabrain.mcp.server import build_server
from schemabrain.pii import CATASTROPHIC_LEAK_CATEGORIES

# The catastrophic-leak floor is the realistic production default
# (`build_server` defaults to it; `init` blocks it). Every beat runs
# under it so the tests mirror the wire behaviour a real operator sees.
_CAT = CATASTROPHIC_LEAK_CATEGORIES
# Operator policy that widens the floor to also refuse the `contact`
# tier — the `schemabrain serve --pii-block contact` case.
_CONTACT_POLICY = frozenset(_CAT | {"contact"})


# ----- test doubles ----------------------------------------------------------


class _StubExecutor:
    """Canned-row stub that records every (sql, params) it executes so
    success beats can assert the compiler emitted the right SQL shape.
    """

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows if rows is not None else []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.max_rows: int | None = None

    def execute(self, sql_text: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.calls.append((sql_text, params))
        return self.rows


class _FakeEmbedder:
    """Embedder seam stand-in — none of these tools call it."""

    def embed(self, text: str) -> list[float]:  # pragma: no cover — not called
        return [0.0]


# ----- fixture + call helper -------------------------------------------------


@pytest.fixture(scope="module")
def saas_store(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str]:
    """Build the offline SaaS demo store once for the read-only beats.

    Returns (store_path, source_connection_id). Read-only: every test
    that uses it passes ``audit=None`` so the file is never mutated and
    the module-scoped share is safe. The audit-chain beat builds its
    own throwaway store instead.
    """
    store_path = tmp_path_factory.mktemp("saas_envelopes") / "saas.db"
    source_id = build_saas_dictionary_store(store_path)
    return store_path, source_id


def _call(
    store_path: Path,
    source_id: str,
    args: dict[str, Any],
    *,
    pii_block: frozenset[str],
    rows: list[dict[str, Any]] | None = None,
    audit: AuditWriter | None = None,
) -> tuple[dict[str, Any], _StubExecutor]:
    """Resolve one ``get_metric`` call through the full MCP server and
    return its structured ``ToolResponse`` plus the executor (so callers
    can inspect the emitted SQL on the success path).
    """
    executor = _StubExecutor(rows=rows)
    with SQLiteStore(store_path) as store:
        app = build_server(
            store=store,
            source_connection_id=source_id,
            embedder=_FakeEmbedder(),  # type: ignore[arg-type]
            metric_executor=executor,  # type: ignore[arg-type]
            pii_block=pii_block,
            audit_writer=audit,
        )
        _content, structured = asyncio.run(app.call_tool("get_metric", args))
    return structured, executor


# ----- PII enforcement -------------------------------------------------------


class TestPiiFloorRefusal:
    """The §2.4 non-negotiable: a catastrophic column is refused; its
    benign public companion on the same entity stays answerable.
    """

    def test_credential_group_by_is_refused(self, saas_store: tuple[Path, str]) -> None:
        store_path, source_id = saas_store
        structured, _ = _call(
            store_path,
            source_id,
            {"name": "user_count", "group_by": ["user.password_hash"]},
            pii_block=_CAT,
            rows=[{"user_count": 1}],
        )
        assert structured["status"] == "refused"
        error = structured["error"]
        assert error["kind"] == "pii_blocked"
        assert "credential" in error["pii_categories"]
        # Recovery points the agent at the entity it can safely inspect.
        assert error["recovery"]["suggested_tool"] == "describe_entity"
        assert error["recovery"]["suggested_args"] == {"name": "user"}

    def test_public_companion_column_is_answerable(self, saas_store: tuple[Path, str]) -> None:
        # `user.role` classifies public — grouping by it must succeed
        # under the same floor policy that refused `password_hash`.
        store_path, source_id = saas_store
        structured, executor = _call(
            store_path,
            source_id,
            {"name": "user_count", "group_by": ["user.role"]},
            pii_block=_CAT,
            rows=[{"role": "member", "user_count": 40}],
        )
        assert structured["status"] == "success"
        sql = executor.calls[-1][0]
        assert '"user"."role"' in sql


class TestContactTierPolicy:
    """Grouping BY a contact column (email / full_name) refuses regardless
    of policy: under an explicit `--pii-block contact` it trips the
    category-policy gate, and under the catastrophic floor alone it trips
    the policy-independent group_by row-disclosure guard. Either way the
    raw contact values never reach the result rows, while public columns
    still answer.
    """

    def test_contact_column_refused_under_contact_policy(
        self, saas_store: tuple[Path, str]
    ) -> None:
        store_path, source_id = saas_store
        structured, _ = _call(
            store_path,
            source_id,
            {"name": "user_count", "group_by": ["user.email"]},
            pii_block=_CONTACT_POLICY,
            rows=[{"user_count": 1}],
        )
        assert structured["status"] == "refused"
        error = structured["error"]
        assert error["kind"] == "pii_blocked"
        assert "contact" in error["pii_categories"]

    def test_contact_group_by_refused_even_under_floor_only_policy(
        self, saas_store: tuple[Path, str]
    ) -> None:
        # Grouping BY a contact column (email) projects raw emails into
        # the result rows as the group keys — that is row-level
        # disclosure, not aggregation. It is refused EVEN under the
        # catastrophic floor alone (no `contact` in the policy): the
        # group_by raw-disclosure guard is policy-independent, exactly
        # like the MIN/MAX guard. Aggregating over / filtering by a
        # contact column under the floor still answers (the
        # category-policy gate above governs those, and contact is not a
        # floor category) — only the group_by projection is refused.
        store_path, source_id = saas_store
        structured, executor = _call(
            store_path,
            source_id,
            {"name": "user_count", "group_by": ["user.email"]},
            pii_block=_CAT,
            rows=[{"email": "a@b.example.com", "user_count": 1}],
        )
        assert structured["status"] == "refused"
        error = structured["error"]
        assert error["kind"] == "pii_blocked"
        assert "contact" in error["pii_categories"]
        # Refused before any SQL executes — the email never leaves the DB.
        assert executor.calls == []


# ----- unreachable entity ----------------------------------------------------


class TestUnreachableEntity:
    """SchemaBrain refuses to fabricate a join path that was never
    modeled — the refusal-not-fabrication thesis at the join layer.
    """

    def test_usage_event_island_is_the_readme_hero_refusal(
        self, saas_store: tuple[Path, str]
    ) -> None:
        # The README "Sample session": `get_metric(usage_volume,
        # group_by=["plan.title"])`. `usage_event` ships with NO
        # canonical join (the deliberate island guarded in
        # test_demo_fixture_consistency), so there is no modeled path
        # from a usage event to a plan and the firewall refuses rather
        # than inventing one.
        store_path, source_id = saas_store
        structured, _ = _call(
            store_path,
            source_id,
            {"name": "usage_volume", "group_by": ["plan.title"]},
            pii_block=_CAT,
            rows=[{"x": 1}],
        )
        assert structured["status"] == "error"
        error = structured["error"]
        assert error["kind"] == "unreachable_entity"
        assert "usage_event" in error["message"]
        assert "not reachable" in error["message"]
        # Recovery hands the agent the exact resolve_join call to make.
        assert error["recovery"]["suggested_tool"] == "resolve_join"
        assert error["recovery"]["suggested_args"]["entity_a"] == "usage_event"
        assert error["recovery"]["suggested_args"]["entity_b"] == "plan"

    def test_usage_event_is_the_sole_remaining_island(self, saas_store: tuple[Path, str]) -> None:
        # After the workspace-hub joins were added (api_key /
        # support_ticket / billing_profile → workspace), `usage_event` is
        # the ONLY entity with no canonical join, so it is the only
        # unreachable target left. This pins that the island is
        # deliberate and singular — every OTHER entity now resolves
        # through the workspace hub (see TestWorkspaceHubJoins below).
        store_path, source_id = saas_store
        structured, _ = _call(
            store_path,
            source_id,
            {"name": "user_count", "group_by": ["usage_event.event_type"]},
            pii_block=_CAT,
            rows=[{"x": 1}],
        )
        assert structured["status"] == "error"
        assert structured["error"]["kind"] == "unreachable_entity"
        assert "usage_event" in structured["error"]["message"]


class TestWorkspaceHubJoins:
    """The api_key / support_ticket / billing_profile entities reach the
    `workspace` hub via canonical joins added to connect the graph. The
    billing_profile join is also a catastrophic-propagation edge: a
    benign column resolves, but a government_id column on the joined
    entity is refused — the firewall following the tag across the join.
    """

    def test_public_column_across_new_join_resolves(self, saas_store: tuple[Path, str]) -> None:
        # user → workspace → billing_profile (the new one_to_one join);
        # grouping by a benign PUBLIC column (legal_entity) resolves and
        # emits the join. (country_code / tax_id / national_id on this
        # table are PII-tagged and would refuse — see the next test and
        # TestContactTierPolicy for the group_by raw-disclosure guard.)
        store_path, source_id = saas_store
        structured, executor = _call(
            store_path,
            source_id,
            {"name": "user_count", "group_by": ["billing_profile.legal_entity"]},
            pii_block=_CAT,
            rows=[{"legal_entity": "Acme Inc", "user_count": 40}],
        )
        assert structured["status"] == "success"
        assert '"public"."billing_profiles"' in executor.calls[-1][0]

    def test_catastrophic_column_across_new_join_is_refused(
        self, saas_store: tuple[Path, str]
    ) -> None:
        # Same reachable path, but grouping by a government_id column —
        # the firewall propagates the `government_id` tag across the join
        # and refuses (catastrophic floor).
        store_path, source_id = saas_store
        structured, _ = _call(
            store_path,
            source_id,
            {"name": "user_count", "group_by": ["billing_profile.tax_id"]},
            pii_block=_CAT,
            rows=[{"x": 1}],
        )
        assert structured["status"] == "refused"
        error = structured["error"]
        assert error["kind"] == "pii_blocked"
        assert "government_id" in error["pii_categories"]


# ----- S5: ambiguous time dimension ------------------------------------------


class TestAmbiguousTimeDimension:
    """S5. A metric with no `time_dimension` of its own, asked for a
    `time_grain`, that reaches 2+ candidate timestamps via non-fan-out
    canonical joins → the firewall refuses to guess which one and ships
    the candidate list as structured recovery.
    """

    def test_line_item_metric_is_ambiguous(self, saas_store: tuple[Path, str]) -> None:
        # `total_revenue_real` anchors on `subscription_item`, which has
        # no timestamp column of its own. Via `subscription_items_
        # subscription` it reaches subscription.{started_at, canceled_at,
        # trial_ends_at}; one more hop reaches workspace.created_at — four
        # honest candidates. The engine refuses to pick.
        store_path, source_id = saas_store
        structured, _ = _call(
            store_path,
            source_id,
            {"name": "total_revenue_real", "time_grain": "month"},
            pii_block=_CAT,
            rows=[{"x": 1}],
        )
        assert structured["status"] == "error"
        error = structured["error"]
        assert error["kind"] == "ambiguous_time_dimension"
        # All three subscription-lifecycle candidates appear in the
        # human-readable candidate list.
        for candidate in (
            "subscription.started_at",
            "subscription.canceled_at",
            "subscription.trial_ends_at",
        ):
            assert candidate in error["message"]
        # Structured recovery: re-call get_metric with an explicit
        # time_dimension drawn from the candidate set.
        recovery = error["recovery"]
        assert recovery["suggested_tool"] == "get_metric"
        suggested = recovery["suggested_args"]["time_dimension"]
        assert suggested in error["message"]
        assert suggested.split(".", 1)[0] in {"subscription", "workspace"}

    def test_explicit_time_dimension_resolves(self, saas_store: tuple[Path, str]) -> None:
        # The recovery leg: passing the disambiguator resolves cleanly,
        # and the emitted SQL buckets by the chosen timestamp via the
        # (non-fan-out) join to subscription — so the SUM stays correct.
        store_path, source_id = saas_store
        structured, executor = _call(
            store_path,
            source_id,
            {
                "name": "total_revenue_real",
                "time_grain": "month",
                "time_dimension": "subscription.started_at",
            },
            pii_block=_CAT,
            rows=[{"bucket": "2026-01", "total_revenue_real": 1}],
        )
        assert structured["status"] == "success"
        sql = executor.calls[-1][0]
        assert "started_at" in sql
        assert '"public"."subscriptions"' in sql

    def test_declared_dimension_metric_is_not_ambiguous(self, saas_store: tuple[Path, str]) -> None:
        # The control + regression guard. `active_subscriptions` DECLARES
        # `time_dimension: subscription.started_at`, so "subscriptions
        # per month" resolves deterministically — it is NOT the S5 demo,
        # and must never be re-pointed at it. (Removing its declaration
        # would make it inherit workspace.created_at, not become
        # ambiguous, because own-table timestamps are excluded by
        # design — so "fixing" S5 on this metric is impossible.)
        store_path, source_id = saas_store
        structured, _ = _call(
            store_path,
            source_id,
            {"name": "active_subscriptions", "time_grain": "month"},
            pii_block=_CAT,
            rows=[{"bucket": "2026-01", "active_subscriptions": 3}],
        )
        assert structured["status"] == "success"


# ----- leaderboard success ---------------------------------------------------


class TestLeaderboardSuccess:
    """The firewall must ALLOW a legitimate cross-entity leaderboard, not
    refuse it — the positive half of the refusal story.
    """

    def test_revenue_by_plan_title_joins_and_groups(self, saas_store: tuple[Path, str]) -> None:
        # `total_revenue` anchors on `invoice`; grouping by `plan.title`
        # traverses invoice → subscription → plan (two non-fan-out
        # canonical joins). The envelope is `success` and the emitted SQL
        # carries both joins + the GROUP BY on the plan title.
        store_path, source_id = saas_store
        structured, executor = _call(
            store_path,
            source_id,
            {"name": "total_revenue", "group_by": ["plan.title"]},
            pii_block=_CAT,
            rows=[{"title": "Pro", "total_revenue": 100}],
        )
        assert structured["status"] == "success"
        sql = executor.calls[-1][0]
        assert '"public"."subscriptions"' in sql
        assert '"public"."plans"' in sql
        assert '"plan"."title"' in sql


# ----- tamper-evident audit chain over mixed history -------------------------


class TestAuditChainOverMixedHistory:
    """Bind the audit chain to the SaaS store: a realistic mix of
    refused / error / success `get_metric` calls writes one `mcp_audit`
    row each, and the sha256 hash chain still verifies clean.
    """

    def test_mixed_history_keeps_chain_intact(self, tmp_path: Path) -> None:
        # A throwaway store (this beat mutates mcp_audit, so it must not
        # share the module fixture).
        store_path = tmp_path / "saas_audit.db"
        source_id = build_saas_dictionary_store(store_path)

        writer = AuditWriter(store_path)
        try:
            # 1) PII floor refusal, 2) S5 ambiguous error,
            # 3) leaderboard success, 4) unreachable error.
            _call(
                store_path,
                source_id,
                {"name": "user_count", "group_by": ["user.password_hash"]},
                pii_block=_CAT,
                rows=[{"user_count": 1}],
                audit=writer,
            )
            _call(
                store_path,
                source_id,
                {"name": "total_revenue_real", "time_grain": "month"},
                pii_block=_CAT,
                rows=[{"x": 1}],
                audit=writer,
            )
            _call(
                store_path,
                source_id,
                {"name": "total_revenue", "group_by": ["plan.title"]},
                pii_block=_CAT,
                rows=[{"title": "Pro", "total_revenue": 100}],
                audit=writer,
            )
            _call(
                store_path,
                source_id,
                {"name": "usage_volume", "group_by": ["plan.title"]},
                pii_block=_CAT,
                rows=[{"x": 1}],
                audit=writer,
            )
        finally:
            writer.close()

        conn = sqlite3.connect(store_path)
        conn.row_factory = sqlite3.Row
        try:
            audit_rows = conn.execute(
                "SELECT id, tool_name, status, refusal_reason FROM mcp_audit ORDER BY id"
            ).fetchall()
            mismatches = list(walk_chain(conn, full=True))
        finally:
            conn.close()

        # Four calls -> four rows, in order, each tagged get_metric.
        assert [r["tool_name"] for r in audit_rows] == ["get_metric"] * 4
        # Only the PII block is a `refused` row carrying a refusal_reason;
        # the compiler-error kinds (ambiguous / unreachable) are `error`
        # with no refusal_reason; the leaderboard is `success`.
        assert audit_rows[0]["status"] == "refused"
        assert audit_rows[0]["refusal_reason"] == "pii_blocked"
        assert audit_rows[1]["status"] == "error"
        assert audit_rows[2]["status"] == "success"
        assert audit_rows[3]["status"] == "error"
        # The hash chain over the whole mixed history is intact.
        assert mismatches == []
