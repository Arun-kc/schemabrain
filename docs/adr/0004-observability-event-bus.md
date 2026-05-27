---
title: "ADR 0004 — Observability event bus"
description: "Three-consumer event bus (logs / audit / dashboard SSE) behind one publish call so the producer never knows who listens."
---

# ADR 0004 — Observability event bus as three-consumer substrate

- **Status:** Accepted
- **Date:** 2026-05-18

## Context

SchemaBrain has to expose, in real time, what an agent is doing
when it touches a database — for trust, for debugging, for demo
storytelling, and (when v2's `execute` line lands) for safety
auditability. The simple framing is "ship a UI." The right framing is
**agent activity observer**: show humans what the agent is doing, not
replace agent UX.

Three downstream consumers want the same per-call event stream:

1. **`schemabrain tail`** — pretty-printed terminal feed alongside
   the agent, shipped in v0.5.
2. **The `mcp_audit` table** (ADR 0001) — durable per-call record
   with PII categorical tags and tamper-evident chain hash.
3. **Optional OpenTelemetry emission** — `gen_ai.*` spans flowing to
   Langfuse / Phoenix / OpenLIT / otel-tui / Datadog for users who
   already run an observability stack.

Each consumer building its own emission path would duplicate the tool-
boundary instrumentation three times. The instrumentation has
non-trivial concerns — argument redaction, response-shape extraction,
error swallowing, fingerprint injection — that we cannot afford to
diverge across three call sites.

This ADR locks the substrate and the OTel emission decisions made for
v0.3.0 (PR #51).

## Decision

### 1. One event bus, three consumers

A single in-process event bus — `JsonlEventBus`, writing to
`~/.schemabrain/events.jsonl` — is the durable artifact. Every MCP
tool call goes through `@instrument`, which:

1. Wraps the tool invocation in an optional OTel span.
2. Runs the audit write (if enabled).
3. Constructs one `Event` and emits to the bus.

All three consumers read from this single path:

- `schemabrain tail` reads the JSONL file.
- `AuditWriter` writes to `mcp_audit` inside the same `@instrument`
  decorator, before the bus emit.
- The OTel emission (this ADR) wraps the call inside the same
  decorator, before audit and bus.

The bus is lossy by design — disk-full / permission failures are
caught, logged once per error-kind, and the event is dropped. The
audit table is durable; OTel emission is best-effort.

### 2. JSONL on disk, not Unix socket

The bus persists to disk (`~/.schemabrain/events.jsonl`) rather than
serving over an IPC socket. Rationale:

- **History across restarts.** Crash-restart and immediate `tail`
  must show the last few seconds before the crash.
- **Cross-platform simplicity.** Unix sockets are POSIX-only. JSONL
  works identically on macOS, Linux, and Windows.
- **`tail` can re-open files but not re-attach to dead sockets.** The
  rotation contract (rename to `.1`, fresh file on next emit) plus
  inode-change detection in `TailReader` handles long-running tails
  across rotation cleanly.

Trade-off: disk writes are slower than socket emits. Bounded
(10 MiB rotation) and single-tenant; well below the threshold where
disk I/O becomes the bottleneck.

### 3. OpenTelemetry is optional, gated by env var

Span emission activates when BOTH conditions hold:

- `schemabrain[otel]` extra is installed (pulls
  `opentelemetry-sdk` + `opentelemetry-exporter-otlp-proto-http`).
- `OTEL_EXPORTER_OTLP_ENDPOINT` is set in the environment.

When either is missing, the `@instrument` decorator skips span
emission entirely — a single `if tracer is not None` guard with zero
runtime overhead. `pip install schemabrain` (no extra) works
identically; OTel is genuinely opt-in.

When the env var is set but the extra is missing, a one-shot stderr
warning fires so the misconfiguration is visible. Without the warning,
a misconfigured operator would think OTel was on and silently emit
zero spans.

### 4. Spans live inside `@instrument`, not as a bus wrapper

The OTel span emission could theoretically be a `TracingEventBus`
wrapper that consumes from the bus and emits spans retroactively.
Rejected:

- Spans need to wrap the call lifetime for OTel context propagation
  to work. A retroactive span has a frozen `start_time` and cannot be
  the parent of any child span the tool's downstream code emits.
- The bus consumes events AFTER the tool returns. By that point the
  call boundary is gone.

The chosen design: `@instrument` accepts an optional
`tracer: Tracer | None` argument. When non-None, the tool invocation
is wrapped in `tracer.start_as_current_span("execute_tool")` —
attributes are set after the call completes, audit and bus emit
inside the span lifetime, and the span closes when the decorator
exits.

This means the OTel span lifetime covers fn() + audit write + bus
emit. Bus duration_ms covers only fn(). The asymmetry is acceptable
for v1: OTel dashboards see "what schemabrain did" as one span;
internal timing uses the bus's narrower measurement.

### 5. Attribute shape follows `gen_ai.*` semantic conventions

Attributes attached to each span:

| Attribute | Value |
|---|---|
| `gen_ai.system` | `"schemabrain"` |
| `gen_ai.tool.name` | One of the 9 MCP tool names |
| `gen_ai.tool.call.id` | Audit fingerprint hex (when `get_metric`) |
| `schemabrain.session.id` | `serve` process UUID |
| `schemabrain.duration_ms` | Wall-clock duration |
| `schemabrain.status` | Charter envelope status |
| `schemabrain.error_kind` | Error kind on `error`/`refused` |
| `schemabrain.result.*` | Result counts (matches, columns, paths, rows, fingerprint) |

Span status:

- Charter `success` / `empty` / `partial` / `degraded` → OTel `OK`.
- Charter `error` / `refused` → OTel `ERROR` with `description =
  error_kind` so dashboards can group failures by kind.

The OTel `gen_ai.*` conventions are still experimental as of the
2026.01 spec. A single helper —
`schemabrain.observability.otel.set_tool_span_attributes` — isolates
the attribute names. The next convention rename touches one helper.

### 6. Args are NOT placed on spans

Tool arguments go through `EventRedactor` and are persisted to the
local JSONL events file (under tighter local-only trust boundaries).
They are NOT attached to OTel spans. Span exporters land in
dashboards we don't control — Langfuse stores everything, Phoenix
stores everything, third-party observability vendors persist data
beyond their TTL claims. Different trust boundary; different posture.

A future `SCHEMABRAIN_OTEL_INCLUDE_ARGS=1` opt-in could relax this if
real users ask. Defer until then.

### 7. OTel failure is silent

OSError / network failure / attribute-setting crashes during span
emission NEVER fail the tool call. The decorator mirrors the bus's
`_safe_emit` discipline:

- OSError → log once per `otel:<tool>` key, drop, continue.
- Programming bug (AttributeError, ValueError) → log every
  occurrence with `schemabrain otel BUG in <tool>: ...` to stderr,
  drop, continue.

The agent gets the tool response unchanged. The operator sees the
stderr line and investigates.

## Consequences

**What becomes possible:**

- Users with existing observability stacks (Langfuse, Phoenix,
  OpenLIT, Datadog LLM Obs) integrate with zero per-backend code —
  just `pip install schemabrain[otel]` and set the env var.
- Demo storytelling has the terminal tail AND optional dashboard
  screenshots, depending on the audience.
- The v2 `execute` and `validate_query` refusal paths get visible
  spans in OTel dashboards (status=`refused`, error_kind=
  `pii_blocked` / `allowlist_violation` / etc.) the moment they ship.
- Generic OTel terminal viewers (otel-tui, otel-desktop-viewer)
  become free escape hatches for users wanting richer terminal views
  without us building one.

**What constrains future evolution:**

- The `gen_ai.*` semantic conventions are experimental. One
  attribute-rename migration is expected before they hit stable. We
  ship the new names in a minor release when the conventions
  stabilise; the helper isolates the impact.
- Spans are NOT linked into a parent trace the agent harness is
  emitting. `serve` is a separate process; OTel context propagation
  across stdio MCP transport is not standardised. Each tool call
  produces an orphan span. Acceptable for dashboard use cases;
  doesn't replace end-to-end agent traces.
- Tracer init uses a private `TracerProvider` rather than registering
  globally. An embedding agent harness that installed its own global
  provider must use `build_server(tracer=...)` directly to participate
  in their trace.

**What remains deferred:**

- `SCHEMABRAIN_OTEL_INCLUDE_ARGS=1` opt-in for args-on-spans.
- Custom `Resource` attributes beyond `service.name=schemabrain`.
- Sampling configuration knobs beyond the standard
  `OTEL_TRACES_SAMPLER` env var.
- Cross-process span linking (MCP transport doesn't propagate
  context; deferred to whenever MCP itself ships a convention).

## References

- `schemabrain/observability/otel.py` — the OTel substrate.
- `schemabrain/observability/instrument.py` — the `@instrument`
  decorator + span wrap.
- `docs/observability.md` — operator-facing integration recipes.
- ADR 0001 — audit row + PII taxonomy (the audit consumer of this
  bus).
- OpenTelemetry [GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/)
  (experimental as of 2026).
