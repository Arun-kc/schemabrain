"""`Event` frozen dataclass — the wire shape for the bus and tail.

Each emitted event is one row. Two `kind`s share the same dataclass with
disjoint fields; `__post_init__` enforces the union shape so a producer
can't construct a malformed mix.

Fields are intentionally aligned with the Charter response envelope
(`status`, `error.kind`) so any durable-store consumer can copy
straight through with zero shape conversion.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Literal, get_args

EventKind = Literal["tool_call", "server_event"]

# Mirrors `schemabrain.mcp.envelope.Status` deliberately — the bus does
# not import the Charter module so the observability package stays free
# of pydantic. Both Literals must move together when Charter bumps.
EventStatus = Literal[
    "success",
    "empty",
    "partial",
    "degraded",
    "error",
    "refused",
]

ServerEventSubtype = Literal[
    "server_start",
    "server_stop",
    "schema_version_mismatch",
    "events_path_init",
]

_KINDS = frozenset(get_args(EventKind))
_STATUSES = frozenset(get_args(EventStatus))
_SERVER_SUBTYPES = frozenset(get_args(ServerEventSubtype))
_FAILURE_STATUSES = frozenset({"error", "refused"})


@dataclass(frozen=True, slots=True)
class Event:
    """One row on the event bus.

    `kind="tool_call"` events populate the tool-side fields and leave
    `event_subtype` / `message` as None. `kind="server_event"` is the
    inverse. The validator below enforces the split.
    """

    timestamp: str
    server_session_id: str
    kind: EventKind

    # tool_call fields
    tool_name: str | None = None
    args_summary: dict[str, Any] | None = None
    status: EventStatus | None = None
    error_kind: str | None = None
    duration_ms: float | None = None
    result_summary: dict[str, Any] | None = None

    # server_event fields
    event_subtype: ServerEventSubtype | None = None
    message: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in _KINDS:
            raise ValueError(f"kind must be one of {sorted(_KINDS)}, got {self.kind!r}")

        if self.kind == "tool_call":
            if self.tool_name is None:
                raise ValueError("tool_call event requires tool_name")
            if self.status is None:
                raise ValueError("tool_call event requires status")
            if self.status not in _STATUSES:
                raise ValueError(f"status must be one of {sorted(_STATUSES)}, got {self.status!r}")
            if self.duration_ms is None:
                raise ValueError("tool_call event requires duration_ms")
            if self.event_subtype is not None:
                raise ValueError(
                    "tool_call event must not set event_subtype (server_event-only field)"
                )
            if self.message is not None:
                raise ValueError("tool_call event must not set message (server_event-only field)")
            if self.error_kind is not None and self.status not in _FAILURE_STATUSES:
                raise ValueError(
                    f"error_kind={self.error_kind!r} requires status in "
                    f"{sorted(_FAILURE_STATUSES)}, got status={self.status!r}"
                )
        else:  # server_event
            if self.event_subtype is None:
                raise ValueError("server_event event requires event_subtype")
            if self.event_subtype not in _SERVER_SUBTYPES:
                raise ValueError(
                    f"event_subtype must be one of {sorted(_SERVER_SUBTYPES)}, "
                    f"got {self.event_subtype!r}"
                )
            if self.message is None:
                raise ValueError("server_event event requires message")
            if self.tool_name is not None:
                raise ValueError("server_event must not set tool_name (tool_call-only field)")
            if self.status is not None:
                raise ValueError("server_event must not set status (tool_call-only field)")
            if self.duration_ms is not None:
                raise ValueError("server_event must not set duration_ms (tool_call-only field)")
            if self.error_kind is not None:
                raise ValueError("server_event must not set error_kind (tool_call-only field)")

    def to_json_line(self) -> str:
        """Serialise as one JSON line terminated by `\\n`.

        ``args_summary`` is a free-shape ``dict[str, Any]`` populated by
        the instrumentation layer from caller kwargs. MCP tool surfaces
        increasingly accept Pydantic ``BaseModel`` shapes (e.g.
        ``MetricOrderByArg`` for ``get_metric.order_by``), and
        ``dataclasses.asdict`` does NOT recurse into Pydantic models —
        it leaves them as-is in the resulting dict, where ``json.dumps``
        then raises ``TypeError``. The event bus catches that and drops
        the event silently after one stderr warning, so every metric
        with structured ``order_by`` args becomes invisible to
        ``schemabrain tail --follow`` and any downstream consumer of the
        bus stream. The ``default=`` hook below converts Pydantic models
        on the fly via their public ``model_dump()`` API (duck-typed so
        the observability package stays free of a Pydantic import).
        """
        return json.dumps(asdict(self), separators=(",", ":"), default=_json_default) + "\n"


def _json_default(obj: object) -> object:
    """``json.dumps`` fallback for shapes that survive ``asdict()``.

    Pydantic models expose ``model_dump()`` (v2) — duck-typed here so
    the observability package stays free of a Pydantic import. Frozen
    sets are emitted as sorted lists for deterministic wire shape (the
    JSON spec has no set type, and frozenset iteration order is hash-
    driven with PYTHONHASHSEED). Anything else falls through to a
    ``TypeError`` ``json.dumps`` will surface to the bus's drop path —
    so a new un-handled shape is loud, not silent.
    """
    model_dump = getattr(obj, "model_dump", None)
    if callable(model_dump):
        return model_dump()
    if isinstance(obj, (frozenset, set)):
        return sorted(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")
