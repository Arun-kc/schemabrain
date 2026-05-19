"""Terminal UI primitives for `schemabrain` CLI.

Currently hosts `RichReporter`, the `rich`-powered `IndexReporter`
implementation used by `schemabrain index`. Kept in its own module so
the core `indexer.py` library stays free of any UI dependency — only
the CLI imports this file, and only when stderr is a terminal.

Design intent:

- Long-running indexing must never look silent. The progress bar is
  load-bearing for trust: a user piping `index` to a cold warehouse
  with hundreds of tables needs to see liveness, current table,
  cumulative LLM cost, and a credible ETA.
- Rich's `Progress.update(advance=1)` is the source of truth for ETA;
  we don't compute it ourselves. The `total` is `total_tables` set at
  start time, advanced by 1 each time a table is finished (either as
  unchanged or after `on_table_done`).
- Cost ticker reads `pipeline.spent_usd` via `on_column_enriched`. It
  stays stale during the 1-2s window of an in-flight LLM call, which
  is acceptable — the column count moves between updates and the
  spinner shows liveness.
"""

from __future__ import annotations

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeRemainingColumn,
)

from schemabrain._ui import make_console
from schemabrain.indexer import IndexResult


class RichReporter:
    """`IndexReporter` that renders a live progress bar to a console.

    Use only when output is a terminal (or when capturing for tests via
    a `Console(file=...)` instance). Rich's `Live` rewrites lines via
    cursor control, which floods log files when stderr is redirected;
    the CLI is responsible for choosing this reporter only when
    appropriate.
    """

    def __init__(self, *, console: Console | None = None) -> None:
        self._console = console if console is not None else make_console(stderr=True)
        self._progress: Progress | None = None
        self._task_id: TaskID | None = None
        self._cost_usd: float = 0.0
        # `_started` gates the on_finish summary so an exception before
        # on_start (e.g. connection failure) doesn't print "done." to a
        # user who saw no progress at all. `_finished` makes both
        # on_finish AND close idempotent — the CLI calls close() in a
        # finally after the success path already called on_finish.
        self._started: bool = False
        self._finished: bool = False

    def on_start(self, *, total_tables: int) -> None:
        self._started = True
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold cyan]{task.fields[table]}"),
            BarColumn(bar_width=None),
            MofNCompleteColumn(),
            TextColumn("•"),
            TimeRemainingColumn(),
            TextColumn("•"),
            TextColumn("[green]${task.fields[cost]:.4f}"),
            console=self._console,
            transient=False,
        )
        self._progress.start()
        self._task_id = self._progress.add_task(
            "indexing",
            total=total_tables,
            table="starting...",
            cost=0.0,
        )

    def on_tables_removed(self, *, removed: int) -> None:
        if removed > 0 and self._progress is not None:
            self._progress.console.print(f"[yellow]removed {removed} stale table(s) from cache")

    def on_table_unchanged(self, *, schema: str, name: str) -> None:
        if self._progress is None or self._task_id is None:
            return
        self._progress.update(
            self._task_id,
            advance=1,
            table=f"{schema}.{name} [dim](cached)",
            cost=self._cost_usd,
        )

    def on_table_start(self, *, schema: str, name: str, columns_to_enrich: int) -> None:
        if self._progress is None or self._task_id is None:
            return
        suffix = (
            f"[cyan]({columns_to_enrich} cols)" if columns_to_enrich > 0 else "[cyan](structural)"
        )
        self._progress.update(
            self._task_id,
            table=f"{schema}.{name} {suffix}",
            cost=self._cost_usd,
        )

    def on_column_enriched(self, *, schema: str, name: str, column: str, spent_usd: float) -> None:
        self._cost_usd = spent_usd
        if self._progress is None or self._task_id is None:
            return
        self._progress.update(self._task_id, cost=spent_usd)

    def on_table_done(self, *, schema: str, name: str) -> None:
        if self._progress is None or self._task_id is None:
            return
        self._progress.update(
            self._task_id,
            advance=1,
            table=f"{schema}.{name} [green]done",
            cost=self._cost_usd,
        )

    def on_finish(self, *, result: IndexResult) -> None:
        # Intentionally does NOT print a summary line. `_cmd_index`
        # prints the canonical summary on stderr after `index()`
        # returns (with source URL + elapsed time appended), so a
        # second "done. <summary>" line here would duplicate the same
        # text in the TTY path. The completed progress bar at N/N is
        # the visual confirmation of success; the canonical summary
        # follows it. The `result` argument is part of the Protocol
        # surface — other reporters may want it — but is unused here.
        del result
        if self._finished:
            return
        self._finished = True
        if self._progress is not None:
            self._progress.stop()
            self._progress = None
            self._task_id = None

    def close(self) -> None:
        """Tear down the live progress widget without printing a summary.

        Safe to call from a CLI `finally` block regardless of how the
        indexing run exited. If `on_finish` already ran, this is a
        no-op. If the run aborted before completion (`CostCapExceeded`,
        `KeyboardInterrupt`, connection failure), this cleanly stops
        rich's `Live` display so the terminal cursor is restored.
        """
        if self._finished:
            return
        self._finished = True
        if self._progress is not None:
            self._progress.stop()
            self._progress = None
            self._task_id = None
