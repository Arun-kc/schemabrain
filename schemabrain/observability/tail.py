"""`schemabrain tail` reader + pretty-printer.

A polling tailer (no platform-specific inotify / FSEvents deps) that:

  - Seeks to `--since DURATION` (or `--since ISO-8601`) when the file
    has content, else starts from EOF.
  - Polls the file for new bytes, parses each new line as one Event,
    and prints to stdout.
  - Detects rotation via inode change; re-opens the new active file
    when the previous inode disappears.
  - Renders events as colored, two-line records by default; switches
    to raw JSONL with `--json`.

The reader is intentionally line-oriented. A partial line at EOF is
held until the writer completes it on the next poll. This trades a
small (~100ms) display latency for atomicity.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rich.console import Console
from rich.markup import escape as _markup_escape

_DEFAULT_POLL_INTERVAL_S = 0.1

_DURATION_RE = re.compile(r"^(\d+)([smhd])$")


def parse_since(text: str, *, now: datetime | None = None) -> datetime:
    """Parse a since spec into an aware UTC datetime.

    Accepts:
      - Compact durations: "5m", "1h", "30s", "1d"
      - ISO 8601 with timezone: "2026-05-17T10:00:00Z" or with offset
    """
    now = now or datetime.now(UTC)
    m = _DURATION_RE.match(text)
    if m:
        amount = int(m.group(1))
        unit = m.group(2)
        if unit == "s":
            delta = timedelta(seconds=amount)
        elif unit == "m":
            delta = timedelta(minutes=amount)
        elif unit == "h":
            delta = timedelta(hours=amount)
        else:  # "d"
            delta = timedelta(days=amount)
        return now - delta
    # ISO 8601 — accept both trailing Z and timezone offsets.
    iso = text
    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError as exc:
        raise ValueError(
            f"could not parse --since {text!r}: expected duration "
            f"like '5m' or ISO 8601 like '2026-05-17T10:00:00Z'"
        ) from exc
    if dt.tzinfo is None:
        raise ValueError(
            f"--since {text!r}: ISO 8601 timestamp must include a "
            f"timezone (e.g. trailing 'Z' or '+00:00')"
        )
    return dt.astimezone(UTC)


def parse_event_timestamp(ts: str) -> datetime:
    """Parse an Event.timestamp string back to aware UTC datetime."""
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts).astimezone(UTC)


@dataclass
class TailOptions:
    events_path: Path
    since: datetime
    follow: bool
    json_mode: bool
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S


def render_event_pretty(event: dict, console: Console) -> None:
    """Render one event as a colored two-line record to the console.

    User-controlled values (args, result summaries, error kind, server
    message) are escaped against Rich markup so a tool argument like
    ``"[link=evil]click[/link]"`` cannot inject styling, hyperlinks,
    or layout-breaking control sequences into the operator's terminal.
    """
    ts = event.get("timestamp", "")
    short_ts = _short_timestamp(ts)
    kind = event.get("kind")
    if kind == "server_event":
        subtype = _markup_escape(event.get("event_subtype") or "server_event")
        message = _markup_escape(event.get("message") or "")
        console.print(
            f"[dim italic]{short_ts}  ▪ {subtype}: {message}[/]",
            highlight=False,
        )
        return
    # tool_call
    tool = event.get("tool_name") or "?"
    status = event.get("status") or ""
    duration_ms = event.get("duration_ms")
    args = event.get("args_summary") or {}
    result = event.get("result_summary") or {}
    error_kind = event.get("error_kind")
    args_str = _markup_escape(_format_args_inline(args))
    console.print(
        f"[dim]{short_ts}[/]  [bold cyan]{tool}[/]  {args_str}",
        highlight=False,
    )
    if status in {"error", "refused"}:
        arrow_color = "red" if status == "error" else "yellow"
        ek = f" {_markup_escape(error_kind)}" if error_kind else ""
        console.print(
            f"              [{arrow_color}]→ {status.upper()}{ek}[/]",
            highlight=False,
        )
    elif status == "degraded":
        result_str = _markup_escape(_format_args_inline(result))
        duration_str = f" in {duration_ms:.0f}ms" if duration_ms is not None else ""
        console.print(
            f"              [yellow]→ degraded[/] {result_str}{duration_str}",
            highlight=False,
        )
    else:
        result_str = _markup_escape(_format_args_inline(result))
        duration_str = f" in {duration_ms:.0f}ms" if duration_ms is not None else ""
        console.print(
            f"              [green]→[/] {result_str}{duration_str}",
            highlight=False,
        )


def _short_timestamp(iso: str) -> str:
    """Render the time portion of an ISO timestamp as HH:MM:SS.mmm."""
    try:
        dt = parse_event_timestamp(iso)
    except (ValueError, IndexError):
        return iso[:12] or "?"
    return dt.strftime("%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"


def _format_args_inline(d: dict) -> str:
    if not d:
        return ""
    parts: list[str] = []
    for k, v in d.items():
        if isinstance(v, str):
            v_str = v if len(v) < 60 else v[:57] + "…"
            parts.append(f"{k}={v_str!r}")
        else:
            parts.append(f"{k}={v}")
    return " ".join(parts)


class TailReader:
    """Polling JSONL tailer with rotation detection.

    Uses raw `os.open` + manual offset tracking to bypass Python's
    text-mode buffering, which caches EOF and doesn't reliably see
    bytes appended by another process between reads.
    """

    def __init__(self, options: TailOptions) -> None:
        self._opt = options
        self._fd: int | None = None
        self._offset: int = 0
        self._buf: bytes = b""
        self._current_inode: int | None = None

    def __enter__(self) -> TailReader:
        return self

    def __exit__(self, *_: object) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def stream(self) -> Iterator[dict]:
        """Yield parsed event dicts forever (when follow=True) or until
        EOF on the active file (when follow=False).

        Caller is responsible for stopping iteration (e.g. on
        KeyboardInterrupt).
        """
        self._open()
        if self._fd is None:
            if not self._opt.follow:
                return
            while self._fd is None:
                time.sleep(self._opt.poll_interval_s)
                self._open()
        for ev in self._read_new_events(filter_since=True):
            yield ev
        if not self._opt.follow:
            return
        while True:
            time.sleep(self._opt.poll_interval_s)
            self._handle_rotation_if_needed()
            for ev in self._read_new_events(filter_since=False):
                yield ev

    def _open(self) -> None:
        try:
            inode = os.stat(self._opt.events_path).st_ino
        except FileNotFoundError:
            return
        try:
            fd = os.open(self._opt.events_path, os.O_RDONLY)
        except OSError:  # pragma: no cover — race against deletion
            # Lost a race with deletion / permission flip between stat
            # and open. Stay in the "no fd yet" state and let the next
            # poll retry.
            return
        self._fd = fd
        self._offset = 0
        self._buf = b""
        self._current_inode = inode

    def _handle_rotation_if_needed(self) -> None:
        try:
            disk_inode = os.stat(self._opt.events_path).st_ino
        except FileNotFoundError:
            # File removed mid-tail. Close the stale fd, drop session
            # state, and warn once so the user knows the source is
            # gone. The next poll will retry _handle_rotation_if_needed
            # and pick up a replacement file via the inode-change
            # branch if one appears.
            self._warn_if_first_disappearance()
            self._close_fd()
            return
        if disk_inode == self._current_inode:
            return
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        try:
            self._fd = os.open(self._opt.events_path, os.O_RDONLY)
        except OSError:  # pragma: no cover — race against deletion
            # Replacement file vanished or became unreadable between
            # the stat and the open. Next poll will retry.
            return
        self._offset = 0
        self._buf = b""
        self._current_inode = disk_inode

    def _read_new_events(self, *, filter_since: bool) -> Iterator[dict]:
        if self._fd is None:
            return
        # Read all available bytes from the current offset.
        try:
            os.lseek(self._fd, self._offset, os.SEEK_SET)
        except OSError:  # pragma: no cover — fd became invalid mid-tail
            return
        chunks: list[bytes] = []
        while True:
            chunk = os.read(self._fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        chunk_bytes = b"".join(chunks)
        if not chunk_bytes and not self._buf:
            return
        data = self._buf + chunk_bytes
        # Split out complete lines; hold any partial trailing line in
        # the buffer until the next poll completes it.
        lines = data.split(b"\n")
        self._buf = lines[-1]
        complete_lines = lines[:-1]
        # Advance offset only by the bytes newly read from disk. The
        # buffered carry-over bytes were already counted on the poll
        # that first read them; counting them again would re-seek past
        # bytes we still need to deliver.
        self._offset += len(chunk_bytes)
        for line_bytes in complete_lines:
            try:
                line = line_bytes.decode("utf-8").strip()
            except UnicodeDecodeError:  # pragma: no cover — binary garbage
                continue
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if filter_since and not self._passes_since(event):
                continue
            yield event

    def _close_fd(self) -> None:
        if self._fd is not None:
            import contextlib as _ctx

            with _ctx.suppress(OSError):  # pragma: no cover — defensive
                os.close(self._fd)
            self._fd = None
        self._offset = 0
        self._buf = b""
        self._current_inode = None

    def _warn_if_first_disappearance(self) -> None:
        if self._fd is None:
            # Already warned for this disappearance episode.
            return
        import sys as _sys

        print(
            f"schemabrain tail: events file {self._opt.events_path} "
            f"disappeared; waiting for it to reappear...",
            file=_sys.stderr,
        )

    def _passes_since(self, event: dict) -> bool:
        ts = event.get("timestamp")
        if not ts:
            return True
        try:
            dt = parse_event_timestamp(ts)
        except ValueError:
            return True
        return dt >= self._opt.since
