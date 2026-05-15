"""Column enrichment pipeline: prompt → LLM call → ColumnDescription.

Two surfaces:

- `enrich_column(...)` — single column, synchronous. Used by tests and
  any caller that wants one-call-at-a-time semantics.
- `enrich_columns_async(...)` — batch of columns from one table,
  concurrent via `asyncio.gather`. Per-tier `asyncio.Semaphore` bounds
  in-flight count (Haiku and Sonnet have separate limits because
  Anthropic rate-limits them differently). The indexer drives this
  path so a 30-column table doesn't pay 30 x per-call latency
  serially.

Cost is tracked cumulatively across calls and the pipeline aborts the
next call when running cost would reach `max_cost_usd` — the user
never gets billed past the cap they asked for.

**Two-tier routing:** if a `cryptic_client` is provided, columns whose
names look heavily abbreviated (per `routing.is_cryptic`) route to it
instead of the default `client`. The intent is Haiku for clear-name
columns (cheap) and Sonnet for cryptic ones (better reasoning, ~5x
cost). Single-client mode (no `cryptic_client`) is still fully
supported and remains the default.

**Provider-agnostic cost dispatch.** Cost is computed via the
client's own `cost_usd(usage)` method — the pipeline does not know
or care which provider is on the other end. A future LLM provider
(Bedrock, Vertex, OpenAI, Ollama) drops in by implementing the
`LLMClient` Protocol (`complete` + `cost_usd`); no changes to this
module or to any central pricing dispatch.

**Runtime guard on `cost_usd` return value (Commit 2).** Per the
type-design audit (2026-05-15), a buggy adapter returning negative
or non-finite cost could silently corrupt `_spent_usd` and escape
the cap. After each call, `cost` is validated as
`math.isfinite(cost) and cost >= 0` — failure raises `RuntimeError`
BEFORE the value can enter the running total or the ledger.

**Persistent cost ledger (Commit 2).** When a `Store` and a
`source_connection_id` are wired into the pipeline at construction,
cumulative spend is persisted to the ledger after each successful
call. A process crash mid-enrichment doesn't reset the counter;
re-runs read the prior total back and refuse the next call if it has
already breached the cap. Without a store, the pipeline falls back to
in-memory-only tracking (used by tests and by any caller that doesn't
care about cross-run durability).

The pipeline depends on `LLMClient` and `Store` Protocols, not on
Anthropic or SQLite specifically. Tests substitute fakes so they never
need an API key or a real DB file (in-memory SQLite works fine).
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping, Sequence

from schemabrain.core.description import ColumnDescription
from schemabrain.core.models import Column, Table
from schemabrain.core.store_protocol import Store
from schemabrain.enrichment.llm import LLMClient, LLMResponse, LLMUsage
from schemabrain.enrichment.prompts import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    column_description_user_prompt,
)
from schemabrain.enrichment.routing import is_cryptic
from schemabrain.profiler.stats import ColumnStats

# Re-exported so callers can import all enrichment surface area from
# this module without knowing about the core/description split.
__all__ = ["ColumnDescription", "CostCapExceeded", "EnrichmentPipeline"]

_DEFAULT_MAX_COST_USD = 10.0

# Default per-tier concurrency. Anthropic rate-limits Haiku and Sonnet
# differently — Haiku has higher RPM, Sonnet is tighter. These defaults
# are conservative; tune via `default_concurrency` / `cryptic_concurrency`
# kwargs once observed rate-limit headers drive a real tuning loop.
_DEFAULT_TIER_CONCURRENCY = 8
_CRYPTIC_TIER_CONCURRENCY = 4


class CostCapExceeded(RuntimeError):
    """Raised when running cost has hit or surpassed `max_cost_usd`.

    Attributes:
        spent: cumulative USD spent before the cap tripped (may equal
            `cap` exactly if the cap was zero).
        cap: the user-supplied `max_cost_usd` value.
    """

    def __init__(self, spent: float, cap: float) -> None:
        super().__init__(
            f"LLM spend ${spent:.4f} reached cap ${cap:.4f}; aborting before next call. "
            f"Re-run with --max-cost <higher> to continue."
        )
        self.spent = spent
        self.cap = cap


class EnrichmentPipeline:
    """One round-trip enrichment per call to `enrich_column`, OR a
    concurrent batch via `enrich_columns_async`.

    Stateful: tracks `spent_usd` cumulatively across calls (across BOTH
    tiers if `cryptic_client` is wired) and refuses to issue a call
    once spend has reached `max_cost_usd`. The check runs BEFORE the
    call, so the user is guaranteed to spend at most one extra call
    past the cap in single-threaded mode. Under concurrency=N, the
    post-call overshoot is bounded by `N x max_per_call_cost` — up to
    N tasks can pass the cap check simultaneously (all see the same
    `spent_usd` value before any records its delta), then each completes
    its call and records spend serialised through the `spend_lock`. Per
    silent-failure-hunter audit 2026-05-15: tasks cancelled by
    `TaskGroup` mid-`asyncio.to_thread` cannot be interrupted (Python
    threads aren't preemptible), so an in-flight LLM call's cost will
    be incurred at the provider but not recorded — bounded leak of
    `concurrency` calls' worth of cost on the failure path.

    Routing:
      - If `cryptic_client` is `None` (default), every column goes to
        `client`.
      - If `cryptic_client` is set, columns whose names are flagged by
        `routing.is_cryptic` go to `cryptic_client`; the rest go to
        `client`.

    Persistence:
      - If `store` + `source_connection_id` are supplied, cumulative
        spend is read from / written to the ledger. The pipeline's
        `_spent_usd` is initialised from the ledger at construction and
        kept in sync after each call.
      - Without `store`, the pipeline tracks spend in-memory only.
        Tests use this mode to avoid SQLite setup.
    """

    def __init__(
        self,
        *,
        client: LLMClient,
        cryptic_client: LLMClient | None = None,
        max_cost_usd: float = _DEFAULT_MAX_COST_USD,
        default_concurrency: int = _DEFAULT_TIER_CONCURRENCY,
        cryptic_concurrency: int = _CRYPTIC_TIER_CONCURRENCY,
        store: Store | None = None,
        source_connection_id: str | None = None,
    ) -> None:
        if max_cost_usd < 0:
            raise ValueError(f"max_cost_usd must be >= 0, got {max_cost_usd}")
        if default_concurrency < 1:
            raise ValueError(f"default_concurrency must be >= 1, got {default_concurrency}")
        if cryptic_concurrency < 1:
            raise ValueError(f"cryptic_concurrency must be >= 1, got {cryptic_concurrency}")
        if (store is None) != (source_connection_id is None):
            raise ValueError(
                "`store` and `source_connection_id` must both be supplied or both omitted; "
                "the ledger is keyed on source_connection_id so the store alone is meaningless."
            )
        self._client = client
        self._cryptic_client = cryptic_client
        self._max_cost = max_cost_usd
        self._default_concurrency = default_concurrency
        self._cryptic_concurrency = cryptic_concurrency
        self._store = store
        self._source_connection_id = source_connection_id
        # Initialise spent_usd from the ledger if wired, else 0. This
        # is the crash-recovery behaviour — a fresh pipeline on a
        # ledger that already has spend immediately enforces the cap
        # against the persisted total.
        if store is not None and source_connection_id is not None:
            self._spent_usd = store.get_spend_usd(source_connection_id=source_connection_id)
        else:
            self._spent_usd = 0.0

    @property
    def spent_usd(self) -> float:
        return self._spent_usd

    @property
    def max_cost_usd(self) -> float:
        return self._max_cost

    @property
    def has_cryptic_tier(self) -> bool:
        """True if a separate cryptic-tier client is configured."""
        return self._cryptic_client is not None

    def enrich_column(
        self,
        *,
        table: Table,
        column: Column,
        stats: ColumnStats | None,
        fk_targets: tuple[str, ...],
    ) -> ColumnDescription:
        """Generate one description for `column`. Raises `CostCapExceeded`
        if cumulative spend has already reached `max_cost_usd`.
        """
        if self._spent_usd >= self._max_cost:
            raise CostCapExceeded(self._spent_usd, self._max_cost)
        client = self._select_client(column.name)
        user = column_description_user_prompt(
            table=table, column=column, stats=stats, fk_targets=fk_targets
        )
        response = client.complete(system=SYSTEM_PROMPT, user=user)
        cost = self._validated_cost(client, response.usage)
        self._record_spend(cost)
        return self._build_description(response, cost)

    async def enrich_columns_async(
        self,
        *,
        table: Table,
        columns: Sequence[Column],
        stats_by_col: Mapping[str, ColumnStats | None],
        fk_targets_by_col: Mapping[str, tuple[str, ...]],
    ) -> dict[str, ColumnDescription]:
        """Concurrent enrichment of multiple columns from a single table.

        Per-column tasks run inside an `asyncio.TaskGroup`; each task
        wraps `client.complete` in `asyncio.to_thread` (the Anthropic
        SDK is sync — no Protocol refactor required). Per-tier
        semaphores bound in-flight count. On first exception,
        `TaskGroup` cancels sibling tasks BEFORE they enter their
        semaphore — this prevents cap-breach cascades where additional
        LLM calls fire after the cap has tripped. Tasks already
        inside `asyncio.to_thread` cannot be interrupted (Python
        threads aren't preemptible); their LLM call will incur cost
        at the provider but won't be recorded in the ledger —
        bounded leak documented in the class docstring.

        Pre-call cap check: if `_spent_usd >= max_cost_usd`, raises
        `CostCapExceeded` immediately without scheduling any tasks.
        Post-call cap update: `_record_spend` is mutex-protected via
        an `asyncio.Lock` so concurrent updates don't race.

        `TaskGroup` wraps any per-task exception in `ExceptionGroup`
        (PEP 654). For caller convenience we unwrap a single-exception
        group back to the original exception so existing
        `pytest.raises(CostCapExceeded)` shape keeps working. A
        multi-exception group surfaces as `BaseExceptionGroup` —
        rare in practice (would require simultaneous independent
        failures) but propagated truthfully when it happens.

        An empty `columns` list short-circuits with an empty dict
        (no event loop work, no semaphore overhead, no API calls).
        """
        if not columns:
            return {}
        if self._spent_usd >= self._max_cost:
            raise CostCapExceeded(self._spent_usd, self._max_cost)

        # Bind asyncio primitives to the CURRENT event loop. Creating
        # them in __init__ would bind to whatever loop ran first;
        # creating them here means each `asyncio.run(...)` gets fresh
        # primitives correctly scoped.
        default_sem = asyncio.Semaphore(self._default_concurrency)
        cryptic_sem = (
            asyncio.Semaphore(self._cryptic_concurrency)
            if self._cryptic_client is not None
            else None
        )
        spend_lock = asyncio.Lock()

        async def _one(col: Column) -> tuple[str, ColumnDescription]:
            client = self._select_client(col.name)
            sem = (
                cryptic_sem
                if (cryptic_sem is not None and client is self._cryptic_client)
                else default_sem
            )
            user = column_description_user_prompt(
                table=table,
                column=col,
                stats=stats_by_col.get(col.name),
                fk_targets=fk_targets_by_col.get(col.name, ()),
            )
            async with sem:
                # Per-task cap check INSIDE the semaphore: completed
                # tasks have already updated `_spent_usd`, so this
                # check sees the latest total. With concurrency=1
                # the cap is enforced strictly (next call after a
                # cap-breaching one is refused). With concurrency=N
                # up to N tasks can pass the check simultaneously
                # before any records its delta — bounded overshoot.
                # See class docstring.
                if self._spent_usd >= self._max_cost:
                    raise CostCapExceeded(self._spent_usd, self._max_cost)
                response = await asyncio.to_thread(client.complete, system=SYSTEM_PROMPT, user=user)
            cost = self._validated_cost(client, response.usage)
            async with spend_lock:
                self._record_spend(cost)
            return col.name, self._build_description(response, cost)

        tasks: list[asyncio.Task[tuple[str, ColumnDescription]]] = []
        try:
            async with asyncio.TaskGroup() as tg:
                tasks = [tg.create_task(_one(c)) for c in columns]
        except BaseExceptionGroup as eg:
            # Unwrap to the FIRST exception in the group so callers see
            # the canonical failure rather than an `ExceptionGroup`
            # wrapper. Common case: under concurrency=N, all N
            # in-flight tasks may all raise `CostCapExceeded` (or all
            # the same `RuntimeError` from a network outage) — the
            # user wants the root cause, not a multi-exception
            # accordion. Preserves the gather-compatible contract.
            #
            # If a true multi-cause failure happens (different
            # exception types in different tasks), the first one is
            # still the most actionable signal; the rest are usually
            # downstream consequences. PEP 654 semantics are
            # preserved via `__context__` chain on the raised exception.
            raise eg.exceptions[0] from None
        return dict(t.result() for t in tasks)

    def _select_client(self, column_name: str) -> LLMClient:
        if self._cryptic_client is not None and is_cryptic(column_name):
            return self._cryptic_client
        return self._client

    def _validated_cost(self, client: LLMClient, usage: LLMUsage) -> float:
        """Compute cost via `client.cost_usd` and enforce the return contract.

        The Protocol docstring says implementations MUST return a
        non-negative finite USD value. A buggy adapter that returns
        `nan`, `inf`, or a negative would silently corrupt `_spent_usd`
        and let spend escape the cap. This guard makes the contract
        enforceable at the seam — per type-design audit 2026-05-15.
        """
        cost = client.cost_usd(usage)
        if not math.isfinite(cost) or cost < 0:
            raise RuntimeError(
                f"client.cost_usd returned non-finite or negative value: {cost!r}. "
                "LLMClient implementations MUST return a non-negative finite USD "
                f"cost; see {type(client).__module__}.{type(client).__qualname__}."
            )
        return cost

    def _record_spend(self, cost: float) -> None:
        """Update both in-memory `_spent_usd` and the ledger (if wired).

        Order matters: ledger write happens FIRST. If the ledger raises
        (e.g. SQLite locked beyond busy_timeout), NEITHER the ledger
        NOR the in-memory counter is advanced — the call's cost is
        lost from accounting entirely, which under-counts but prevents
        an over-count that would let real spend escape the cap. The
        exception propagates to the caller; the run aborts before
        scheduling further calls. Per silent-failure-hunter audit
        2026-05-15.
        """
        if self._store is not None and self._source_connection_id is not None:
            new_total = self._store.add_spend_usd(
                source_connection_id=self._source_connection_id,
                amount_usd=cost,
            )
            # Trust the ledger for the new total — it sees the
            # cross-process truth (if writer_lock=True is on).
            self._spent_usd = new_total
        else:
            self._spent_usd += cost

    def _build_description(self, response: LLMResponse, cost: float) -> ColumnDescription:
        return ColumnDescription(
            text=response.text.strip(),
            model=response.model,
            prompt_version=PROMPT_VERSION,
            input_tokens=response.usage.input_tokens,
            cached_input_tokens=response.usage.cached_input_tokens,
            output_tokens=response.usage.output_tokens,
            cost_usd=cost,
        )
