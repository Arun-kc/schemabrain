# MCP Server Stress & DoS Report

**Scope**: DoS resilience, concurrency stress, and bypass attempts
against `--statement-timeout-ms` / `--max-rows-per-result`.
**SUT**: `schemabrain` HEAD, live
Postgres demo `sb-demo-pg` on `127.0.0.1:5433` (PG 16 alpine,
`max_connections=100` default).
**Test host**: macOS (Darwin 25.5.0), Python 3.11.15.

## Tooling & harness

- `tests/firewall/_harness.py` — in-process `build_server(...)` wired
  to a real `AuditWriter` + `EngineMetricExecutor` mirroring the
  `cli.py::_cmd_serve` engine construction (NullPool,
  `default_transaction_read_only=on`, `safe_engine_url`).
- `tests/firewall/stress_mcp_server.py` — `ThreadPoolExecutor` fan-out
  over `app.call_tool` with a 4×concurrency in-flight pipeline,
  parameterised by `--concurrency`, `--duration`, `--tool`. Each
  worker drives one fresh `asyncio` event loop per call (the cost is
  ~0.1ms, negligible vs MCP dispatch). Hard wall budget of 60s
  prevents hangs from running away.
- Three regression pins:
  - `test_perf_audit_chain_race.py` — FW-008 verification.
  - `test_perf_statement_timeout_enforcement.py` — IF-3 + FW-010.
  - `test_perf_max_rows_truncation.py` — SF-003 + memory-DoS path.

Postgres fixtures (under schema prefix `firewall_perf_*` per
coordination rule):

| Object | Definition |
|---|---|
| `firewall_perf.slow_view` | `SELECT pg_sleep(5)::text AS s, 1 AS id` |
| `firewall_perf.bigtab`    | 5,000 rows `(id INT, label TEXT)` |

## Throughput (read-side mix on indexed demo store)

| Concurrency | Duration | Tool | Submitted | Completed | RPS | p50 (ms) | p95 (ms) | p99 (ms) | Max (ms) |
|---|---|---|---|---|---|---|---|---|---|
| 50  | 10s | mix (find_rel/describe/list)             | 46,431 | 46,231 | 4,621 | 10.4 | 12.9 | 18.0 | 39.9 |
| 200 | 5s  | `find_relevant_tables`                    | 26,224 | 25,424 | 5,070 | 38.5 | 43.6 | 56.3 | 93.4 |
| 30  | 8s  | `find_relevant_tables` (read-side spam)   | 51,531 | 51,331 | 6,416 |  4.3 |  6.1 |  9.2 | 112.6 |

Throughput plateaus near **~6.5k rps** on the test host — the SQLite
write lock on `mcp_audit` is the saturating resource (`AuditWriter`
serialises all writes through `self._lock`). The flat plateau means
adding workers past ~50 produces ~no extra throughput and inflates
tail latency.

## Audit-chain integrity under burst (FW-008)

| Scenario | Calls | Audit rows written | Chain mismatches |
|---|---|---|---|
| `test_perf_audit_chain_race[32-200]`  |    200 |    200 | 0 |
| `test_perf_audit_chain_race[64-400]`  |    400 |    400 | 0 |
| 50-concurrency 10s burst              | 46,231 | 46,231 | 0 |
| 200-concurrency 5s burst              | 25,424 | 25,424 | 0 |
| 30-concurrency 8s read-side spam      | 51,331 | 51,331 | 0 |
| **Total cumulative**                  | **123,291** | **123,291** | **0** |

- **No dropped writes**: row count = submitted call count in every burst.
- **No chain breaks**: `walk_chain(full=True)` over 123,291 rows
  reports 0 mismatches.
- `AuditWriter._lock` + serialised in-memory `_last_chain_hash` mutation
  is **correct under concurrency**. FW-008 (medium-confidence audit
  finding) is **NOT exploitable** — the lock + commit-before-publish
  ordering inside `write()` closes the window.

**`audit verify` scaling**: 123,291 rows verify in 740ms
(166k rows/sec). At a sustained 6k rps workload, daily audit growth is
~520M rows — `audit verify` over a full day's chain would take ~50
minutes, which is acceptable for a once-a-day forensic check but
prohibitive for interactive use.

## Statement-timeout enforcement (IF-3, FW-010)

| Condition | Wall-clock | Result | Verdict |
|---|---|---|---|
| No timeout, `SELECT pg_sleep(3)`       | 3.0s | row returned | **NOT-ENFORCED** by default |
| Timeout 1000ms, `SELECT pg_sleep(10)`  | ~1.1s | `OperationalError: canceling statement due to statement timeout` | **ENFORCED** |
| Timeout 1000ms, `SELECT FROM firewall_perf.slow_view` | ~1.1s | timeout fires | **ENFORCED** on view path |
| URL `?options=-c statement_timeout=0` | n/a | param stripped by `safe_engine_url` | **BYPASS BLOCKED** |

- **IF-3 confirmed**: `schemabrain serve` ships with the timeout
  **unset** by default; a 3s `pg_sleep` runs to completion. Operationally
  this is weak for a v1.0 firewall — every operator must opt in.
- **FW-010 NOT exploitable on the executor path**: a view whose
  definition embeds `pg_sleep(5)` still aborts under a 1000ms timeout
  because Postgres applies `statement_timeout` to the outer statement.
- URL-smuggled `options=-c statement_timeout=0` is stripped by
  `safe_engine_url` before the engine constructs — the operator-set
  timeout remains authoritative.

**Final verdict**: `--statement-timeout-ms` is **ENFORCED when set;
NOT-CONFIGURED by default**. Recommend flipping `serve` to require an
explicit value (or default to a safe non-zero like 30000) before v1.0.

## Max-rows truncation (SF-003)

| Condition | Rows returned | Envelope signal |
|---|---|---|
| `max_rows=None`, 5000-row source | 5,000 | n/a |
| `max_rows=100`, 5000-row source  | 100 (head)   | none |
| `max_rows=10`, 5000-row source   | 10  (head)   | none |

- **Truncation FIRES correctly** — head of result, not random sample.
- **Truncation is SILENT**: `EngineMetricExecutor.execute` returns
  `list[dict]` with no marker. Server logs `WARNING: metric executor
  truncated result: 5000 rows -> 10`, but **the agent sees nothing**
  in the envelope — `status="success"`, no `degradation_reasons`
  entry, no row sentinel.
- **Memory-DoS angle**: the executor materialises **the full result
  set** (`[dict(row) for row in result.mappings()]`) before slicing
  to `max_rows`. A metric producing 10M rows peaks at 10M dicts in
  process RAM before the cap fires. The cap is a **payload guard, not
  a query-cost guard** (already documented in `threat-model.md` T3.2b,
  but the practical RAM ceiling is worth flagging).

**Final verdict**: `--max-rows-per-result` is
**ENFORCED-SILENTLY**. The agent has no in-band way to detect
truncation. Combined with the silent path, an LLM analysing 10
rows believes it has the full picture; a downstream business
decision derived from those 10 rows looks correct to the agent and
misleading to the human. **Recommend** lifting the executor log to a
`degradation_reasons=["truncated"]` envelope entry in v0.5.

## Connection-pool exhaustion (NullPool + PG `max_connections=100`)

500 concurrent `EngineMetricExecutor.execute("SELECT 1")` calls under
120 worker threads (PG default `max_connections=100`):

| Workers | Calls | Successes | Failures | Failure mode |
|---|---|---|---|---|
| 50  | 200 | 200 (100%) | 0   | — |
| 120 | 500 | 440 (88%)  | 60  | `FATAL: sorry, too many clients already` |

- NullPool opens + closes a fresh PG connection per call. When agent
  concurrency exceeds `max_connections`, calls fail **raw** with no
  retry, no queueing, no graceful degradation.
- An adversarial agent that spams `get_metric` can lock OUT
  legitimate operator queries by saturating Postgres slots. The PII
  firewall doesn't apply to operator-issued queries from other clients
  either, but the resulting denial-of-service still hits production
  visibility.
- **No application-layer rate limit** on the MCP tool surface
  (documented gap in T3.3 residual risk).

## Read-side spam (T3.3 verification)

51,331 `find_relevant_tables` calls in 8s on a single thread pool:

- Throughput: 6,416 rps sustained.
- p95 latency: 6.1ms (cached embedder, fast cosine).
- **No rate limit at the MCP layer** — every call writes one audit row.
- Audit DB grew to 31.7MB at 123,291 rows ≈ 256 bytes/row average.
- Embedding cache stays bounded (single embedder instance, fixed-size
  4-dim vector in test; production ONNX cache is per-process LRU).

## FAILED stress conditions

These are conditions where the SUT crashed, hung, leaked state, or
permitted a bypass:

1. **Connection-pool exhaustion (no graceful degradation)** — at 120
   concurrent workers vs PG `max_connections=100`, 12% of
   `EngineMetricExecutor.execute` calls fail with
   `OperationalError: sorry, too many clients already`. The error
   surfaces raw as `internal_error` to the agent; no backoff, no
   retry, no queueing.
2. **No default statement-timeout (IF-3)** — `schemabrain serve` with
   no flags lets a 3s `pg_sleep` run to completion. Firewall is opt-in.
3. **Silent max-rows truncation (SF-003)** — agent sees `status=
   success` envelope with a clipped payload and no signal.
4. **No MCP-layer rate limit** — 51k+ read-side calls in 8s land 51k
   audit rows. An LLM in a runaway tool-call loop will drive linear
   audit-DB growth until disk fills.
5. **Eager result materialisation (memory-DoS path)** — executor
   builds the full row list before applying `max_rows`. A 10M-row
   source query peaks at 10M dicts in memory before truncation.

These conditions did NOT occur (positive results):

- **No audit-chain breaks** at 123k concurrent writes — FW-008 is not
  exploitable.
- **No statement-timeout bypass** via URL `options=` or via slow-view
  embedding (FW-010 not exploitable on executor path).
- **No SQLite store corruption** at any tested concurrency level
  (WAL + serialised writer lock holds).
- **No server hang** at any tested concurrency level — the
  60-second hard wall budget never triggered.

## Recommendations (v0.5+)

1. Default `--statement-timeout-ms` to a non-zero value
   (e.g., 30000) and require an explicit `--no-statement-timeout`
   override to disable. Flips IF-3 from opt-in to opt-out.
2. Surface truncation in the envelope:
   `degradation_reasons=["truncated"]` when `len(rows) > max_rows`
   before clipping. Closes SF-003.
3. Push `LIMIT` to the SQL layer when `max_rows` is set. Eliminates
   the eager-materialisation memory-DoS path.
4. Replace NullPool with a bounded `QueuePool` (max=20, timeout=5s)
   so connection exhaustion produces a controlled `degraded` envelope
   instead of raw PG failures.
5. Add MCP-layer rate-limit middleware (per-source-connection-id
   token bucket, default 100 calls/sec) to bound runaway tool-call
   loops at the firewall boundary rather than at the audit-DB
   disk-full point.

## How to re-run

```bash
# Index demo schema once (creates ~/.schemabrain/store.db-equivalent)
export DBURL="postgresql+psycopg://postgres:local@127.0.0.1:5433/postgres"
uv run schemabrain index --url-env DBURL --store-path /tmp/sb-stress-store.db --no-enrich

# Find the auto-generated source_connection_id
SID=$(uv run python -c "import sqlite3; \
  print(sqlite3.connect('/tmp/sb-stress-store.db').execute( \
    'SELECT DISTINCT source_connection_id FROM tables').fetchone()[0])")

# Pinned regression suite
uv run pytest tests/firewall/ -v

# Ad-hoc stress
uv run python tests/firewall/stress_mcp_server.py \
  --concurrency 50 --duration 10 --tool mix --source-id "$SID"
```
