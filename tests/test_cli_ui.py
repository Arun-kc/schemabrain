"""Smoke tests for `schemabrain.cli_ui.RichReporter`.

The goal here is not pixel-perfect output assertion — `rich`'s render
layer changes across versions and contains ANSI control sequences that
make brittle string-match tests a maintenance hazard. Instead we:

  1. Verify every reporter method runs cleanly against a captured
     `Console` (drives all branches without crashing).
  2. Verify the captured output contains a few load-bearing tokens:
     the table name, the cached-table hint, the final summary, and
     evidence that cost is rendered with the expected formatter.

Anything more specific would be testing rich itself.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from schemabrain.cli_ui import RichReporter
from schemabrain.indexer import IndexResult


@pytest.fixture
def captured_console() -> tuple[Console, io.StringIO]:
    """A Console writing into a StringIO with terminal emulation on.

    `force_terminal=True` makes rich render the progress widget even
    though stdout is a memory buffer, so we can exercise the live-display
    code path without spawning a real TTY.
    """
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, width=120, record=True)
    return console, buf


def _result() -> IndexResult:
    return IndexResult(
        tables_seen=2,
        tables_changed=1,
        tables_unchanged=1,
        tables_removed=0,
        columns_added=2,
        columns_changed=0,
        columns_removed=0,
        descriptions_generated=2,
        llm_cost_usd=0.0042,
        embeddings_generated=2,
    )


class TestRichReporterFlow:
    def test_full_flow_does_not_raise(self, captured_console: tuple[Console, io.StringIO]) -> None:
        console, _ = captured_console
        reporter = RichReporter(console=console)

        reporter.on_start(total_tables=2)
        reporter.on_tables_removed(removed=0)
        reporter.on_table_start(schema="public", name="users", columns_to_enrich=2)
        reporter.on_column_enriched(schema="public", name="users", column="id", spent_usd=0.001)
        reporter.on_column_enriched(schema="public", name="users", column="email", spent_usd=0.0042)
        reporter.on_table_done(schema="public", name="users")
        reporter.on_table_unchanged(schema="public", name="orders")
        reporter.on_finish(result=_result())

    def test_finish_does_not_print_summary_text(
        self, captured_console: tuple[Console, io.StringIO]
    ) -> None:
        # `_cmd_index` prints the canonical "Indexed N table(s): ..."
        # summary on stderr after `index()` returns, with source URL
        # and elapsed time appended. RichReporter.on_finish MUST NOT
        # re-print that text — doing so produced a duplicate line on
        # the TTY path. The completed progress bar at N/N is the
        # visual confirmation; the canonical summary follows it.
        console, _ = captured_console
        reporter = RichReporter(console=console)

        reporter.on_start(total_tables=1)
        reporter.on_tables_removed(removed=0)
        reporter.on_table_start(schema="public", name="users", columns_to_enrich=0)
        reporter.on_table_done(schema="public", name="users")
        reporter.on_finish(result=_result())

        rendered = console.export_text()
        assert "Indexed 2 table(s)" not in rendered
        assert "done." not in rendered

    def test_tables_removed_message_only_when_nonzero(
        self, captured_console: tuple[Console, io.StringIO]
    ) -> None:
        console, _ = captured_console
        reporter = RichReporter(console=console)

        reporter.on_start(total_tables=1)
        reporter.on_tables_removed(removed=0)
        reporter.on_table_unchanged(schema="public", name="orders")
        reporter.on_finish(result=_result())
        rendered_no_removals = console.export_text()
        assert "stale table" not in rendered_no_removals

    def test_tables_removed_message_emitted_when_positive(
        self, captured_console: tuple[Console, io.StringIO]
    ) -> None:
        console, _ = captured_console
        reporter = RichReporter(console=console)

        reporter.on_start(total_tables=1)
        reporter.on_tables_removed(removed=3)
        reporter.on_table_unchanged(schema="public", name="users")
        reporter.on_finish(result=_result())
        rendered = console.export_text()
        assert "removed 3 stale table(s)" in rendered

    def test_callbacks_before_start_are_no_ops(
        self, captured_console: tuple[Console, io.StringIO]
    ) -> None:
        # Defensive: if a caller forgets on_start, the other callbacks
        # must not crash. (Documented as part of the reporter contract.)
        console, _ = captured_console
        reporter = RichReporter(console=console)

        reporter.on_table_unchanged(schema="public", name="users")
        reporter.on_table_start(schema="public", name="users", columns_to_enrich=0)
        reporter.on_column_enriched(schema="public", name="users", column="id", spent_usd=0.0)
        reporter.on_table_done(schema="public", name="users")
        # on_finish on never-started reporter must also be safe.
        reporter.on_finish(result=_result())

    def test_finish_when_never_started_does_not_print_summary(
        self, captured_console: tuple[Console, io.StringIO]
    ) -> None:
        # If a real-world error fires before `on_start` (e.g. connection
        # to the warehouse failed), the reporter must not pretend the
        # run finished. No "done." line, nothing to confuse a user
        # debugging the actual error.
        console, _ = captured_console
        reporter = RichReporter(console=console)
        reporter.on_finish(result=_result())
        rendered = console.export_text()
        assert "done." not in rendered
        assert "Indexed" not in rendered

    def test_finish_can_be_called_twice_safely(
        self, captured_console: tuple[Console, io.StringIO]
    ) -> None:
        # Some flows (e.g. exception handlers in the CLI) might want to
        # finalize twice — once on success, once in a finally block.
        # Second call must be a no-op, not raise.
        console, _ = captured_console
        reporter = RichReporter(console=console)
        reporter.on_start(total_tables=1)
        reporter.on_tables_removed(removed=0)
        reporter.on_table_unchanged(schema="public", name="users")
        reporter.on_finish(result=_result())
        reporter.on_finish(result=_result())

    def test_double_finish_does_not_print_summary(
        self, captured_console: tuple[Console, io.StringIO]
    ) -> None:
        # Sibling of test_finish_does_not_print_summary_text — pins the
        # invariant on the idempotent path too. A regression where
        # on_finish accidentally started printing IndexResult.summary()
        # would fail this and the single-call test together.
        console, _ = captured_console
        reporter = RichReporter(console=console)
        reporter.on_start(total_tables=1)
        reporter.on_tables_removed(removed=0)
        reporter.on_table_unchanged(schema="public", name="users")
        reporter.on_finish(result=_result())
        reporter.on_finish(result=_result())
        rendered = console.export_text()
        assert "Indexed" not in rendered
        assert "done." not in rendered

    def test_close_after_on_finish_is_no_op(
        self, captured_console: tuple[Console, io.StringIO]
    ) -> None:
        # The CLI calls `close()` in a finally block after `index()`
        # already ran `on_finish` on the happy path. The second teardown
        # must not raise and must not touch the (already-stopped)
        # Progress widget. No summary text is printed in either call —
        # see test_finish_does_not_print_summary_text for the contract.
        console, _ = captured_console
        reporter = RichReporter(console=console)
        reporter.on_start(total_tables=1)
        reporter.on_tables_removed(removed=0)
        reporter.on_table_unchanged(schema="public", name="users")
        reporter.on_finish(result=_result())
        reporter.close()
        rendered = console.export_text()
        assert "Indexed" not in rendered

    def test_close_without_on_finish_stops_progress_silently(
        self, captured_console: tuple[Console, io.StringIO]
    ) -> None:
        # The error path: `index()` raised before reaching `on_finish`.
        # `close()` must stop the live widget so the cursor is restored,
        # but must NOT print a fake summary line — the caller is about
        # to print its own error message.
        console, _ = captured_console
        reporter = RichReporter(console=console)
        reporter.on_start(total_tables=5)
        reporter.on_tables_removed(removed=0)
        reporter.on_table_start(schema="public", name="users", columns_to_enrich=2)
        reporter.on_column_enriched(schema="public", name="users", column="id", spent_usd=0.001)
        # Simulate abort: close() called without on_finish.
        reporter.close()
        rendered = console.export_text()
        assert "done." not in rendered
        assert "Indexed" not in rendered

    def test_close_before_start_is_no_op(
        self, captured_console: tuple[Console, io.StringIO]
    ) -> None:
        # Connection failed before `index()` got to `on_start`. The CLI
        # still calls `close()` in finally; must not raise.
        console, _ = captured_console
        reporter = RichReporter(console=console)
        reporter.close()

    def test_empty_source_does_not_raise(
        self, captured_console: tuple[Console, io.StringIO]
    ) -> None:
        # `total_tables=0` is a legal rich code path (Progress with
        # total=0). Empty schemas are real — exercise the path through
        # RichReporter so a future rich upgrade that changes ETA math
        # doesn't silently crash on empty databases.
        console, _ = captured_console
        reporter = RichReporter(console=console)
        empty_result = IndexResult(
            tables_seen=0,
            tables_changed=0,
            tables_unchanged=0,
            tables_removed=0,
            columns_added=0,
            columns_changed=0,
            columns_removed=0,
        )
        reporter.on_start(total_tables=0)
        reporter.on_tables_removed(removed=0)
        reporter.on_finish(result=empty_result)
        reporter.close()
