"""Event-bus substrate powering the tail pane and future audit/OTel consumers.

Three modules live here:

  - `event`      — `Event` frozen dataclass + JSON line serialisation.
  - `redactor`   — `EventRedactor` strips credentials and PII-shaped
                   values from `args_summary` before write.
  - `bus`        — `JsonlEventBus` open/append/rotate on a JSONL file.
  - `extractors` — per-tool `result_summary` extractors keyed by tool
                   name, with a safe default.

The bus emits each `Event` as one JSON line. The same shape is read by
the `schemabrain tail` CLI and (once it lands) the audit-table writer.
Emission is best-effort: a disk-full or permission failure logs once
to stderr and drops the event rather than failing the tool call.
"""

from __future__ import annotations

from schemabrain.observability.bus import EventBus, JsonlEventBus, NullEventBus
from schemabrain.observability.event import (
    Event,
    EventKind,
    EventStatus,
    ServerEventSubtype,
)
from schemabrain.observability.extractors import (
    default_result_extractor,
    get_result_extractor,
)
from schemabrain.observability.instrument import instrument, now_iso_utc
from schemabrain.observability.redactor import EventRedactor

__all__ = [
    "Event",
    "EventBus",
    "EventKind",
    "EventRedactor",
    "EventStatus",
    "JsonlEventBus",
    "NullEventBus",
    "ServerEventSubtype",
    "default_result_extractor",
    "get_result_extractor",
    "instrument",
    "now_iso_utc",
]
