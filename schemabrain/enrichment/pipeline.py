"""Column enrichment pipeline: prompt → LLM call → ColumnDescription.

Coordinates a single round-trip per column. Cost is tracked
cumulatively across calls and the pipeline aborts the next call when
running cost would exceed `max_cost_usd` — the user never gets billed
past the cap they asked for.

The pipeline depends on `LLMClient`, not on Anthropic specifically. Tests
substitute `FakeLLMClient` so they never need an API key.
"""

from __future__ import annotations

from schemabrain.core.description import ColumnDescription
from schemabrain.core.models import Column, Table
from schemabrain.enrichment.llm import LLMClient, haiku_45_cost_usd
from schemabrain.enrichment.prompts import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    column_description_user_prompt,
)
from schemabrain.profiler.stats import ColumnStats

# Re-exported so callers can import all enrichment surface area from
# this module without knowing about the core/description split.
__all__ = ["ColumnDescription", "CostCapExceeded", "EnrichmentPipeline"]

_DEFAULT_MAX_COST_USD = 10.0


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
    """One round-trip enrichment per call to `enrich_column`.

    Stateful: tracks `spent_usd` cumulatively across calls and refuses
    to issue a call once spend has reached `max_cost_usd`. The check
    runs BEFORE the call, so the user is guaranteed to spend at most
    one extra call past the cap (worst case: the call that pushed spend
    over the line, then the next call is refused).
    """

    def __init__(
        self,
        *,
        client: LLMClient,
        max_cost_usd: float = _DEFAULT_MAX_COST_USD,
    ) -> None:
        if max_cost_usd < 0:
            raise ValueError(f"max_cost_usd must be >= 0, got {max_cost_usd}")
        self._client = client
        self._max_cost = max_cost_usd
        self._spent_usd = 0.0

    @property
    def spent_usd(self) -> float:
        return self._spent_usd

    @property
    def max_cost_usd(self) -> float:
        return self._max_cost

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
        user = column_description_user_prompt(
            table=table, column=column, stats=stats, fk_targets=fk_targets
        )
        response = self._client.complete(system=SYSTEM_PROMPT, user=user)
        cost = haiku_45_cost_usd(response.usage)
        self._spent_usd += cost
        return ColumnDescription(
            text=response.text.strip(),
            model=response.model,
            prompt_version=PROMPT_VERSION,
            input_tokens=response.usage.input_tokens,
            cached_input_tokens=response.usage.cached_input_tokens,
            output_tokens=response.usage.output_tokens,
            cost_usd=cost,
        )
