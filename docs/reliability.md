# Reliability contract

Schema Brain's reliability targets, what they cover, and how each one maps
to an observable signal in the event bus or the audit log. The numbers are
operator-facing promises. They are aspirational at the current release —
we publish them so the conversation about reliability is concrete instead
of vague.

## Why publish targets we have not yet defended at scale

Schema Brain has shipped against a bundled six-table fixture and a
fifteen-table real-world schema (Pagila). The local p95 measurements for
read-side tools sit well inside the targets below. We have **no**
production-scale measurements yet — that comes when the first donor
schema connects.

Publishing the targets now serves three purposes:

1. **Contract clarity.** Operators know what to expect; we know what to
   defend.
2. **Regression budget.** Every PR that meaningfully changes a hot path
   is checked against these numbers. A target burned is a feature frozen.
3. **Honest framing.** The doc tells the truth about what is measured
   vs. what is assumed. A reliability story you cannot pressure-test is
   marketing.

## SLOs — read-side MCP tools

These tools read from the local SQLite store and the in-process
embedding matrix. They do not touch Postgres.

| Tool | p95 latency target | Error budget |
|---|---|---|
| `find_relevant_tables` | 250 ms | 0.5 % |
| `describe_table` | 50 ms | 0.1 % |
| `describe_column` | 50 ms | 0.1 % |
| `get_example_queries` | 100 ms | 0.5 % |
| `suggest_joins` | 200 ms | 0.5 % |
| `list_entities` | 100 ms | 0.5 % |
| `describe_entity` | 100 ms | 0.5 % |
| `resolve_join` | 150 ms | 0.5 % |

p95 is measured from MCP request receipt to envelope-serialise. The
error budget covers `status: error` envelopes — `status: empty` is a
correct response (no matching data), not an error.

## SLOs — execute-side MCP tools

`get_metric` is the only tool that emits SQL to the source database.
Its latency is bounded mostly by the Postgres execution time and the
operator's `statement_timeout`.

| Tool | p95 latency target | Error budget |
|---|---|---|
| `get_metric` | 1500 ms | 1.0 % |

The 1500 ms target assumes a small-result metric query on a healthy
source. Long-running aggregations are bounded by the source-side
`statement_timeout`, which Schema Brain sets to 30 s by default; a
query that exceeds it returns `status: error` with `error_kind:
statement_timeout` and counts against the error budget.

## SLOs — pipeline operations

| Operation | Target | Notes |
|---|---|---|
| `index` throughput | ≥ 100 columns / minute | with default Haiku 4.5 + local embedder |
| `serve` cold-start | < 800 ms | from process start to first tool ready |
| Audit-write success | 99.99 % | failures land in the events log |

Throughput scales near-linearly with column count, not table count
(see [architecture.md](architecture.md) for the measured anchors).
Cold-start is dominated by the fastembed ONNX load; baking the model
into the Docker image (see [setup.md](setup.md)) reduces first-run
cost on container deployments.

## What is measured today vs. what is aspirational

| Component | Measurement status |
|---|---|
| Read-side p95 against the bundled fixture | Measured locally; comfortably inside targets |
| Read-side p95 against Pagila (15 tables, 87 columns) | Measured locally; comfortably inside targets |
| `get_metric` p95 against a real schema | Not measured at scale |
| `index` throughput on a 200-table schema | Extrapolated from two anchors; see architecture.md |
| Audit-write success rate | No long-running soak test yet |

The promise is: when a donor schema lands and we have real anchors,
this table tightens or moves. The targets above do not move silently —
they get updated in a PR that names the new evidence.

## How to verify

Every target maps to an event recorded in `~/.schemabrain/events.jsonl`
(per the event bus introduced in the observability pane) or a row in
the `mcp_audit` table (per the audit-substrate work).

| Target | Where to look |
|---|---|
| Read-side latency | `event.kind == "tool_call"` records `duration_ms` |
| Read-side error budget | `event.status` values across a rolling window |
| `get_metric` latency | Same `tool_call` event; `tool_name == "get_metric"` |
| `index` throughput | `index_progress` events; columns / wall-clock |
| `serve` cold-start | `server_start` event timestamp vs. process start |
| Audit-write success | Absence of `audit_write_failed` events |

`schemabrain tail` surfaces these events live. `schemabrain audit list`
filters by tool and status. JSON-mode output on both subcommands
pipes cleanly into `jq` or whatever observability sink the operator
already runs.

## Error budget policy

If a target burns more than 50 % of its error budget in a rolling
seven-day window, the next PR shipped is a reliability fix, not a
feature. This is a discipline-by-convention policy at the current
release; once a hosted variant exists, the budget burn becomes a
hard gate on the deployment pipeline.

## What is explicitly out of scope

- **Availability of the source database.** Schema Brain is a client;
  source availability is the operator's existing infrastructure.
- **Availability of the Anthropic API.** `index` retries with
  exponential backoff and surfaces a guided error when retries are
  exhausted; the operator can re-run.
- **Disk space exhaustion under `~/.schemabrain/`.** The events file
  rotates at 10 MiB; the store grows with the schema. Disk monitoring
  is at the OS layer.
- **Network reliability between the agent and the MCP stdio server.**
  Stdio is in-process; the agent and Schema Brain share a parent.

## How this document is maintained

Reviewed at every release. New targets are added when a new tool ships.
Existing targets move when new evidence arrives; the PR that moves them
names the evidence. Operators who observe a sustained gap between the
target and the measured reality should file an issue.
