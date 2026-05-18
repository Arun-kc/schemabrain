# Observability

The observability layer is a thin substrate that exposes everything
Schema Brain does in response to an MCP request, so a human can see —
in real time — what an agent is touching, what got refused, and what
got returned. It has three pieces:

1. **An internal event bus.** A `JsonlEventBus` that any tool handler
   can `emit(Event(...))` into.
2. **A user-facing tail.** `schemabrain tail` reads the bus and pretty-
   prints each event so the operator can watch alongside the agent.
3. **An optional OTel exporter.** The `schemabrain[otel]` extra ships
   each tool call as one `gen_ai.execute_tool` span to any
   OTLP/HTTP-speaking backend — Langfuse, Phoenix, OpenLIT, otel-tui,
   Datadog. Off by default; activated by setting
   `OTEL_EXPORTER_OTLP_ENDPOINT`.

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

A durable-store consumer is on the roadmap. That path will guarantee
durability for higher-trust events while the JSONL tail remains lossy
by design.

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

## Audit log (alpha)

Alongside the lossy JSONL bus, every MCP tool call writes one row to
the `mcp_audit` table inside the local SQLite store. The table is
append-only by three independent mechanisms — SQL triggers, a
write-only writer connection, and a per-row sha256 chain hash that
makes coherent tampering detectable against any external archive that
captured a prior hash.

The shape, the privacy guarantee, and the regulatory backing for the
PII taxonomy are documented in
[ADR 0001](adr/0001-audit-row-and-pii-taxonomy.md). Per that ADR, the
fingerprint primitive carries no row content, no column values, and
no identifying schema info — only structural metadata.

### Inspecting the table

```bash
# Verify the chain is intact (exit 0 = clean, exit 1 = mismatch).
schemabrain audit verify

# Walk every row past the first mismatch — forensic mode.
schemabrain audit verify --full

# List recent rows (pretty table).
schemabrain audit list

# List with filters.
schemabrain audit list --since 1h --status error --tool describe_table

# Machine-readable output.
schemabrain audit list --json | jq '.tool_name'
```

### Durability

The audit writer uses WAL + `synchronous=NORMAL` — the same posture
as the rest of the SQLite store. A crash within milliseconds of a
write can lose the last few rows; the chain hash still keeps the rest
of the table tamper-evident. Stricter fsync-on-write durability lives
on the roadmap.

### Single-process constraint

Run **one** `schemabrain serve` instance per store file. The audit
writer holds an in-memory `_last_chain_hash` that is recovered from
the table tail on startup. Two `serve` processes against the same
store would each compute the next `id` independently and race the
`INSERT` — the second loses with a `UNIQUE` constraint failure that
surfaces as a stderr `BUG` line and silently drops the audit row.

If you need horizontal scale, separate the source databases (one
store per source) until v3 hosted brings a multi-writer audit plane.

### Opting out

```bash
# No audit writes for this serve process.
schemabrain serve --no-audit --url-env DATABASE_URL --store-path ./schemabrain.db
```

If the writer cannot be constructed (read-only volume, missing parent
permissions), `serve` falls back to no-audit with a stderr warning —
the server is more useful without audit than not at all.

## PII classification (alpha)

`schemabrain index` runs a heuristic classifier over every column it
profiles, tagging columns with categories from the closed
12-category taxonomy in
[ADR 0001](adr/0001-audit-row-and-pii-taxonomy.md). The taxonomy is
regulator-derived (GDPR Arts. 4 + 9, CCPA/CPRA, HIPAA Safe Harbor,
PCI DSS, ISO 27018) so categories map cleanly onto compliance
boundaries instead of conflating them.

Tags live in a `column_pii_tags` SQLite table keyed by
`(source_connection_id, qualified_table, column_name)`. `get_metric`
bulk-reads tags for every column a plan touches (measure + time
bucket + group_by + filters), propagates by MAX-sensitivity +
UNION-categories (ADR §4), and writes the propagated set into the
audit row's `pii_categories` column. Two `get_metric` calls touching
different category sets therefore produce distinct `fingerprint`
digests — the field that pre-PR-#36 was a v1 constant.

### Enforcing a refusal policy

```bash
# Refuse any get_metric whose plan touches `contact` or `health`.
schemabrain serve --pii-block contact,health \
  --url-env DATABASE_URL --store-path ./schemabrain.db
```

The policy is *additive* (refuse calls touching these categories);
omitting `--pii-block` disables enforcement and tags still flow
through to `mcp_audit.pii_categories`. Unknown category names abort
startup with a clear error listing the 12 valid values — typos in
the operator config never silently fall through to "no PII
protection".

A blocked call returns a Charter `status="refused"` envelope with
`error.kind="pii_blocked"`. The audit row records:

- `status='refused'`
- `refusal_reason='pii_blocked'`
- `pii_categories` = the attempted set (so the audit shows *what
  was touched*, not just *that something was blocked*)
- `cost_class='refused'`

`--pii-block` with `--no-audit` emits a one-shot stderr warning at
startup: enforcement still happens (the agent sees the refusal
envelope), but the refused row never lands in `mcp_audit`.

### Opting out of classification

```bash
# Skip classification at index time; wipe any existing tags for
# tables touched this run.
schemabrain index ... --no-pii-classify
```

`get_metric` audit rows then record `pii_categories=''` and any
`--pii-block` policy on `serve` has nothing to act on. Use only
when local tag inference itself is unwanted (privacy-paranoid
environments).

## Integrating with existing observability stacks

Two paths, picked based on what you already run.

### Path 1 — tail the JSONL into a log shipper

```bash
# Stream into a log shipper that supports stdin
schemabrain tail --json | your-log-shipper

# Or have filebeat / promtail / vector tail the file directly
filebeat -c filebeat.yml  # configured to tail ~/.schemabrain/events.jsonl
```

Works without any extra dependencies. Best when you already have a
log-shipping pipeline (ELK, Loki, Splunk).

### Path 2 — emit OTel spans

Install the extra and set the OTLP endpoint:

```bash
pip install schemabrain[otel]
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
schemabrain serve --url-env DATABASE_URL --store-path ./schemabrain.db
```

Every MCP tool call becomes one span named `execute_tool` carrying:

- `gen_ai.system = "schemabrain"`
- `gen_ai.tool.name` = one of the 9 MCP tool names
- `gen_ai.tool.call.id` = audit fingerprint hex when present
- `schemabrain.session.id` = the serve process UUID
- `schemabrain.status` = Charter envelope status
- `schemabrain.error_kind` = error kind on failure
- `schemabrain.duration_ms` = wall-clock latency
- `schemabrain.result.*` = numeric / string fields from the tool's
  result summary (matches, columns, paths, rows, etc.)

Span status maps to OTel's status code:

| Charter `status` | OTel status |
|---|---|
| `success`, `empty`, `partial`, `degraded` | OK |
| `error`, `refused` | ERROR (description = `error_kind`) |

Spans are sent via OTLP over HTTP/protobuf. All standard OTLP env vars
work (`OTEL_EXPORTER_OTLP_HEADERS`, `OTEL_EXPORTER_OTLP_TIMEOUT`,
`OTEL_TRACES_SAMPLER`, etc.). When the extra is installed but the
endpoint env var is unset, span emission is silently off — zero
overhead. When the env var is set but the extra is not installed, a
one-shot stderr warning fires so the misconfiguration is visible.

The implementation never fails a tool call because of an OTel error:
exporter network failures, missing endpoints, or attribute-setting
crashes all degrade to a single stderr line and the agent gets the
tool response unchanged.

#### Backend-specific recipes

**Langfuse (self-hosted or cloud):**

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=https://us.cloud.langfuse.com/api/public/otel
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic <base64(pk:sk)>"
```

**Phoenix (Arize):**

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:6006
```

**OpenLIT:**

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
# OpenLIT auto-discovers gen_ai.* attributes; no extra config.
```

**otel-tui (terminal viewer):**

```bash
# In one terminal:
otel-tui

# In another:
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
schemabrain serve --url-env DATABASE_URL --store-path ./schemabrain.db
```

#### Limits

- Spans are NOT linked into any parent trace your agent harness is
  emitting. `serve` is a separate process; OTel context propagation
  across stdio MCP transport isn't standardised yet. Each tool call
  produces an orphan span — fine for backend dashboards, not for
  end-to-end agent traces.
- Tool arguments are NOT attached to spans, even after redaction.
  The events file (`~/.schemabrain/events.jsonl`) carries redacted
  args under tighter trust boundaries; span exporters can land in
  dashboards you don't fully control.
- The `gen_ai.*` semantic conventions are still experimental as of
  the OTel spec. One attribute-rename migration is expected before
  GA; we'll ship the new names in a minor release when the conventions
  stabilise.
