# Observability

The observability layer is a thin substrate that exposes everything
Schema Brain does in response to an MCP request, so a human can see —
in real time — what an agent is touching, what got refused, and what
got returned. It has three pieces:

1. **An internal event bus.** A `JsonlEventBus` that any tool handler
   can `emit(Event(...))` into.
2. **A user-facing tail.** `schemabrain tail` reads the bus and pretty-
   prints each event so the operator can watch alongside the agent.
3. **(Future) An OTel exporter.** A separate `schemabrain[otel]` extra
   that ships the same events as `gen_ai.execute_tool` spans into
   Langfuse / Phoenix / OpenLIT / otel-tui. Not in this release.

## What gets logged

Every tool call emits exactly one event on completion (success or
failure). Server lifecycle (start, stop, schema-version mismatch)
emits a separate event kind.

### Tool-call event shape

```json
{
  "timestamp": "2026-05-17T14:32:07.114523Z",
  "server_session_id": "11111111-2222-3333-4444-555555555555",
  "kind": "tool_call",
  "tool_name": "find_relevant_tables",
  "args_summary": {"query": "customer churn"},
  "status": "success",
  "error_kind": null,
  "duration_ms": 47.3,
  "result_summary": {"matches": 3},
  "event_subtype": null,
  "message": null
}
```

| Field | Meaning |
|---|---|
| `timestamp` | ISO 8601 UTC with microsecond precision and trailing `Z`. |
| `server_session_id` | UUID generated when the serve process started. Use this to group events across a single `serve` run. |
| `kind` | `"tool_call"` for one of the 9 MCP tools; `"server_event"` for lifecycle markers. |
| `tool_name` | The MCP tool name (e.g. `describe_table`, `get_metric`). |
| `args_summary` | The keyword arguments the agent passed, after redaction (see below). |
| `status` | Mirrors the Charter response envelope: `success` / `empty` / `partial` / `degraded` / `error` / `refused`. |
| `error_kind` | When `status` is `error` or `refused`, the structured error kind (e.g. `unknown_name`, `pii_blocked`). |
| `duration_ms` | Wall-clock latency of the tool call. |
| `result_summary` | A small per-tool dict — counts, fingerprints — extracted from the response data. |

### Server-event shape

```json
{
  "timestamp": "2026-05-17T14:32:00.000000Z",
  "server_session_id": "11111111-2222-3333-4444-555555555555",
  "kind": "server_event",
  "tool_name": null,
  "args_summary": null,
  "status": null,
  "error_kind": null,
  "duration_ms": null,
  "result_summary": null,
  "event_subtype": "server_start",
  "message": "schemabrain serve started (session ...)"
}
```

`event_subtype` is one of:

- `server_start` — emitted before the stdio transport accepts the first
  request.
- `server_stop` — emitted in a `finally` block, so `KeyboardInterrupt`
  still produces a stop event.
- `schema_version_mismatch` — reserved for a future check.
- `events_path_init` — reserved for a future check.

## Redaction

Tool arguments pass through an `EventRedactor` BEFORE the event line
hits disk. Four rules apply per-value (keys are never modified):

1. **Connection URLs** — any string matching `^(postgresql|postgres|mysql|sqlite)(\+\w+)?://`
   becomes `<redacted-connection-url>`.
2. **Long strings** — anything larger than 2 KiB becomes
   `<truncated:N bytes>`.
3. **`get_metric` filter values** — every value inside a `filters`
   dict becomes `<value>` (filter values are user PII by default —
   email, customer id, etc.).
4. **Email-shaped strings** — anything matching `^[^\s@]+@[^\s@]+\.[^\s@]+$`
   becomes `<email>`.

The redactor is conservative-but-incomplete by design. A user passing
an SSN or token as a positional or plain string argument still leaks
into the events file. Treat the events file as the same trust
boundary as your shell history — local-only, don't post it publicly
without review.

## File layout

The default path is `~/.schemabrain/events.jsonl`. Override with
`--events-path PATH` (on both `serve` and `tail`) or the
`SCHEMABRAIN_EVENTS_PATH` environment variable. Flag wins over env,
env wins over default.

The directory is created mode `0700`, the file mode `0600` — same
posture as the host config from `schemabrain init`.

The file rotates at 10 MiB. On overflow:

- The active file is renamed to `<path>.1`.
- A fresh active file starts on the next emit.
- Only one rotation is kept; older `.1` files are dropped.

`schemabrain tail` follows the active file and detects rotation via
inode change, re-opening the new file when it appears.

## Failure semantics

The bus is lossy by design. If `emit()` fails — disk full, permission
revoked, anything — the failure is caught, logged once per error-kind
to stderr, and the event is dropped. The agent's tool call still
returns normally; we never fail a request because the log layer
failed.

When the v2 audit table writes from the same bus (different consumer,
durable semantics), that path will guarantee durability for the
audit-grade events while the JSONL tail remains lossy.

## CLI cheat sheet

```bash
# Default — follow live, last 5 minutes, pretty
schemabrain tail

# JSON for piping
schemabrain tail --json | jq 'select(.status == "refused")'

# Print history and exit
schemabrain tail --no-follow --since 1h

# Point at a non-default events file
schemabrain tail --events-path /tmp/my-events.jsonl

# Disable emission entirely on the server side
schemabrain serve --no-events --url-env DATABASE_URL --store-path ./schemabrain.db
```

## Integrating with existing observability stacks

For now, the recommendation is to tail `events.jsonl` and ship the
JSON to your stack:

```bash
# Stream into a log shipper that supports stdin
schemabrain tail --json | your-log-shipper

# Or have your filebeat / promtail / vector tail the file directly
filebeat -c filebeat.yml  # configured to tail ~/.schemabrain/events.jsonl
```

A native OpenTelemetry exporter is on the roadmap and will ship as
an optional `schemabrain[otel]` extra. It will map each event to a
`gen_ai.execute_tool` span and emit through standard OTLP, so any
backend that understands OTel `gen_ai.*` conventions (Langfuse,
Phoenix, OpenLIT, otel-tui, otel-desktop-viewer, Datadog, ...) will
work without per-backend code.
