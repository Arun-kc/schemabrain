"""SF-002 — empty PII tag data must not silently degrade to "public"
when ``--pii-block`` enforcement is active.

Background:

    ``schemabrain/pii/propagation.py:41-63`` — ``propagate([])``
    returns ``("public", frozenset())``. Standing alone that's the
    correct v1 baseline ("nothing PII-sensitive touched"), but the
    ``get_metric`` call site could not tell that result apart from
    "tag-fetch came back empty because the source was never
    classified" — silently fail-opening on misconfigured sources.

Expected SECURE behaviour:

    The fix lives at the call site (``mcp/get_metric.py``), not in
    the ``propagate`` helper itself: ``propagate`` retains its pure
    "fold sensitivities and categories" contract, and
    ``_resolve_pii_categories`` checks whether the underlying tag
    lookup returned ANY rows before delegating. When zero tag rows
    exist AND ``pii_block`` enforcement is active, the call refuses
    with ``PiiBlockedError`` rather than letting the empty lookup
    pose as confirmed-clean data. The opt-out path is the existing
    ``--pii-block ''`` (no enforcement) flag.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.metric import Metric, MetricMeasure
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.mcp.get_metric import get_metric_impl
from schemabrain.pii.propagation import propagate
from schemabrain.semantic.compiler import PiiBlockedError

pytestmark = [pytest.mark.firewall_bypass]

SRC = "sf_002_source"


def _seed_minimal_table_entity_metric(store: SQLiteStore) -> None:
    """Seed an unclassified source: table + entity + metric, no PII tags."""
    users = Table(
        name="users",
        schema_name="public",
        columns=(
            Column(
                name="id",
                table_name="users",
                schema_name="public",
                data_type="BIGINT",
                nullable=False,
                ordinal_position=1,
                is_primary_key=True,
            ),
            Column(
                name="email",
                table_name="users",
                schema_name="public",
                data_type="TEXT",
                nullable=False,
                ordinal_position=2,
            ),
        ),
    )
    store.write_table(users, source_connection_id=SRC)
    store.write_entity(
        Entity(
            name="user",
            description="",
            binding=SingleTableBinding(qualified_table="public.users"),
            identity="id",
        ),
        source_connection_id=SRC,
    )
    store.write_metric(
        Metric(
            name="email_count",
            description="",
            entity="user",
            measure=MetricMeasure(agg="count", column="email"),
            time_dimension=None,
            time_grains=(),
        ),
        source_connection_id=SRC,
    )


class _FakeExecutor:
    """Stub MetricExecutor — records every call so the test can assert
    the SQL never ran on the fail-closed path."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def execute(self, sql: str, params: dict) -> list[dict]:
        self.calls.append((sql, params))
        return [{"email_count": 0}]


def test_propagate_empty_input_returns_public_baseline_by_design() -> None:
    """Locks the ``propagate`` helper's stable contract: empty input
    legitimately returns the public baseline. The fix is NOT to change
    this — the helper is a pure fold, and "no PII columns touched" is
    a legitimate state. The call-site test below is where the
    fail-closed semantic is enforced.
    """
    assert propagate([]) == ("public", frozenset())


def test_unclassified_source_with_pii_block_refuses_at_call_site(tmp_path: Path) -> None:
    """The load-bearing fix: when ``--pii-block`` is non-empty AND the
    store returns zero tag rows for every column touched, the call
    refuses rather than letting the empty lookup silently degrade to
    confirmed-clean. Before the fix this call returned a successful
    ``MetricResult`` with ``pii_categories=()``, skipping every
    downstream PII gate."""
    from schemabrain.mcp import get_metric as _gm

    _gm._empty_tag_table_warned.clear()

    store = SQLiteStore(tmp_path / "sb.db")
    try:
        _seed_minimal_table_entity_metric(store)
        executor = _FakeExecutor()
        with pytest.raises(PiiBlockedError) as exc_info:
            get_metric_impl(
                store=store,
                executor=executor,
                source_connection_id=SRC,
                name="email_count",
                pii_block=frozenset({"contact"}),  # type: ignore[arg-type]
            )
        # Refusal carries the operator's policy so they can see what
        # would have blocked. SQL must never have emitted.
        assert exc_info.value.blocked_categories == ("contact",), (
            "BYPASS: get_metric on an unclassified source with "
            "--pii-block enforcement active proceeded as if the "
            "tag-table emptiness was confirmed non-PII. Empty tag "
            "data + active policy must fail closed at the call "
            "site so the firewall doesn't silently degrade."
        )
        assert executor.calls == []
    finally:
        store.close()


def test_unclassified_source_without_pii_block_remains_warning_only(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Locks the no-enforcement path: empty ``pii_block`` = operator
    opted out of policy = existing warning behaviour is preserved.
    The fail-closed branch must only fire when the operator opted IN
    to enforcement — otherwise the fix would surface as a regression
    in the legitimate ``--no-pii-classify`` workflow."""
    from schemabrain.mcp import get_metric as _gm

    _gm._empty_tag_table_warned.clear()

    store = SQLiteStore(tmp_path / "sb.db")
    try:
        _seed_minimal_table_entity_metric(store)
        executor = _FakeExecutor()
        result = get_metric_impl(
            store=store,
            executor=executor,
            source_connection_id=SRC,
            name="email_count",
            # No pii_block — enforcement off.
        )
        stderr = capsys.readouterr().err
        assert "no PII tags found" in stderr, (
            "the existing one-shot warning must still fire when the "
            "operator opted out of enforcement"
        )
        assert result.pii_categories == ()
        # SQL DID run — opt-out path is unchanged.
        assert len(executor.calls) == 1
    finally:
        store.close()
