---
title: "MCP Charter v1.2"
description: "The locked public design contract every SchemaBrain MCP tool implements — status taxonomy, envelope shape, recovery hints."
---

# SchemaBrain MCP Charter v1.2.0

> **Status:** locked 2026-05-12 as the public design contract for SchemaBrain's
> MCP surface. Living document; version bumps governed by the Versioning section
> below. All MCP tools shipped from v0.5 onward conform to this charter unless
> explicitly noted in their docstring.
>
> **Minor v1.2.0 (2026-05-23):** additive — 2D trust signal. New optional
> `Provenance.inference_method` Literal (closed: `manually_authored |
> llm_suggested | fk_constraint | dbt_import | observed_in_query_log`)
> names HOW each fact was derived. New optional `Provenance.validation_state`
> Literal (closed: `draft | applied | confirmed`) names HOW VALIDATED that
> fact is. The orthogonal axes replace the pre-1.2 behaviour where every
> producer hardcoded `confidence="HIGH"` regardless of derivation (which
> conflated FK-derived joins with LLM-guessed metrics on the same scale).
> The `confidence` field stays — its value is now derived from the 2D
> signal via `derive_confidence()`. Old clients reading only `confidence`
> see a more honest 1D label; new clients can read the 2D signal directly.
> All changes are backward-compatible with v1.0 / v1.1 clients. The wire
> `charter_version` field bumps from `"1.1"` to `"1.2"`. Full type spec
> in `schemabrain/mcp/envelope.py`.
>
> **Minor v1.1.0 (2026-05-15):** additive — three new ErrorKinds
> (`pii_blocked`, `policy_blocked`, `allowlist_violation`); reserved
> `refused` status in the Status literal (no v0.5 / v1 tool emits it
> — v2's `execute` / `validate_query` are the first producers); two
> new optional `Recovery` fields (`suggested_rewrite`, `widening_hint`)
> as the shape v2's refuse-with-rewrite path will populate. All
> changes are backward-compatible with v1.0 clients. The wire
> `charter_version` field bumps from `"1.0"` to `"1.1"`.
>
> **Patch v1.0.1 (2026-05-15):** clarification-only — replaced internal
> milestone references with the substantive trigger they stood for
> (query-log mining surfacing realistic agent intents). No shape change.

## Preamble

This charter is the design law for SchemaBrain's MCP server. It exists because
**every existing semantic layer and database catalog was designed for humans
first** (analysts, BI tools, data engineers), with MCP retrofitted on top.
SchemaBrain is the opposite: the primary consumer of every tool is an LLM, and
the design choices follow from that.

This document is for three audiences:

1. **Contributors** adding or modifying MCP tools — every PR is reviewed against
   the principles and enforcement levels below.
2. **Operators** integrating SchemaBrain into agent stacks — the response
   envelope and per-tool metadata are the stable contracts you can build on.
3. **Other MCP authors** — SchemaBrain commits publicly to these principles
   because no canonical "agent-first MCP design" reference exists yet.
   Adoption, criticism, and divergence are all welcome.

### What "agent-first" means concretely

Six design choices follow from "the primary consumer is an LLM":

| Choice | Human-first server | SchemaBrain |
|---|---|---|
| Definition entry | Hand-authored YAML / API docs | Auto-inferred from schema + behavior |
| Response shape | Optimized for human parsing | Optimized for LLM composition |
| Tool descriptions | What the tool does | When to use it, when not to, what to combine it with |
| Errors | Stack traces / exception types | Recovery contracts (kind + message + next-call hint) |
| Confidence | Implicit / trust the operator | Explicit `HIGH | MEDIUM | LOW` with provenance |
| Update model | Tied to model deploy | Continuous re-index from observed warehouse traffic |

The five principles below are the load-bearing details.

---

## Principles

### 1. Status enum, not boolean

Every tool response carries a `status` enum with six values. The
sixth, `refused`, was reserved in v1.1 and is emitted today by
`get_metric` on the PII-block path; the type contract is stable for
additional producers as they ship. A boolean `ok` / `error` split
silently lumps partial responses and empty results into "success,"
which is the false-positive trap that turns into a backstab in
production.

```
status: "success" | "empty" | "partial" | "degraded" | "error" | "refused"
```

| Status | Meaning |
|---|---|
| `success` | Tool ran, returned the requested data. |
| `empty` | Tool ran, no matches / no data — *not* an error. e.g. `find_relevant_tables` returned zero hits. |
| `partial` | Tool ran, returned some data with caveats. e.g. an enrichment job timed out mid-table; here is what completed. |
| `degraded` | Tool ran via a fallback path. e.g. keyword retriever used because the embedding store was unavailable. |
| `error` | Tool could not process. Always paired with a populated `error` object. |
| `refused` | Tool ran cleanly and chose to refuse — typically because the query would touch PII or violate an allowlist. Always paired with a populated `error` object using one of `pii_blocked` / `policy_blocked` / `allowlist_violation`. Emitted today by `get_metric` on the PII-block path (`pii_blocked`); `policy_blocked` and `allowlist_violation` are reserved producers. |

**❌ Wrong:**
```json
{"data": [], "error": null}
// Was this a real "no matches found" or a silent miss? Agent can't tell.
```

**✅ Right:**
```json
{"status": "empty", "data": [], "error": null,
 "follow_up_hints": ["list_indexed_schemas"]}
```

---

### 2. Tool descriptions are "use when" statements, not API docs

API docs describe what a tool does. Agent-first descriptions describe
**when to use it, when not to, and what to combine it with.** This is the
single highest-leverage place to influence agent tool-choice behavior — the
LLM never reads your code, but it reads every tool description on every turn.

Three-rule structure for every tool description:

1. **Lead with "Use this when…"** — orients the LLM's tool-choice mental model
   in the first sentence.
2. **Include "Use X instead when…" or "Don't use when…"** — disambiguates
   against neighbour tools.
3. **Name 1–2 common compositions** — encodes workflow into the description so
   the LLM falls into the right flow naturally.

**❌ Wrong:**
> Returns information about a database table, including its columns,
> data types, and foreign keys.

**✅ Right:**
> **Use this when** the user names a specific table (e.g. "show me the
> orders table"). Returns columns with types, foreign keys, and an
> LLM-generated description. **Use `find_relevant_tables` instead when**
> the user describes the table semantically ("the table with customer
> data") rather than by name. **Common compositions:** chain
> `find_relevant_tables → describe_table` for semantic-to-structural
> queries; chain `describe_table → describe_column` to drill into a
> specific column's join graph.

#### Verification

A February 2026 arXiv study of 856 real-world MCP tools ([*Smelly MCP Tool
Descriptions*](https://arxiv.org/html/2602.14878v1)) found that 97% have at
least one description quality "smell" — most commonly Unclear Purpose,
Missing Usage Guidelines, and Unstated Limitations. The three-rule structure
above directly attacks the first two; the lint rule (Enforcement level 1) is
the cheap mechanical check.

Tool descriptions are also tested via blind agent eval: same descriptions,
fixed query set, run against Claude / GPT / Gemini. Tool-choice agreement
and end-to-end task success rate are tracked over time; the threshold is a
calibration knob, not a hardcoded floor, with the first baseline measured
once query-log mining surfaces realistic agent intents (see Open items
for the staging plan). See Enforcement level 3.

---

### 3. Errors are prompts for the next tool call

Every error returns three things: **what failed, why, and what to try next.**
No stack traces, no exception type names, no Python-side jargon. An error is
the agent's opportunity to recover — give it the recovery path.

Error contract:

```
error: {
  kind: <one of the registered error kinds — see registry below>,
  message: <one human-readable sentence>,
  recovery: {
    suggested_tool: <name of the tool the agent should call next, if any>,
    suggested_args: <args for that tool>,
    fuzzy_matches: [<list of plausible alternatives>]
  }
}
```

**❌ Wrong:**
```json
{"error": "Table not found"}
// Agent has no way to recover except giving up or asking the user.
```

**✅ Right:**
```json
{
  "status": "error",
  "error": {
    "kind": "unknown_name",
    "message": "Table 'user' not found in the indexed schema.",
    "recovery": {
      "suggested_tool": "find_relevant_tables",
      "suggested_args": {"query": "user"},
      "fuzzy_matches": ["users", "user_profiles", "auth.users"]
    }
  }
}
```

#### Initial error-kind registry

The full registry is maintained in code (Pydantic Literal on the `kind` field).
v1.0 ships with these kinds; additions are minor-version bumps.

| Kind | When |
|---|---|
| `unknown_name` | Caller referenced a name that doesn't exist (table, column, schema). |
| `malformed_name` | Caller passed a name that violates the expected shape (e.g. bare `orders` instead of `schema.orders`). |
| `missing_credential` | A required credential (env var, config) is absent at call time. |
| `index_not_ready` | A query hit the MCP server before `schemabrain index` ran successfully. |
| `schema_drift` | The store and the live source disagree about object existence. |
| `cost_cap_exceeded` | The configured `--max-cost` was reached mid-call. |
| `internal_error` | A bug; the agent should not retry. Logged for repair. |

---

### 4. Confidence is HIGH/MEDIUM/LOW with per-field provenance

Confidence is reported as a three-bucket enum, not a raw float. Buckets
force the server to commit to a trust judgment instead of pushing raw scores
into the LLM's reasoning chain. Floats are kept internally for sorting and
calibration; the API surface buckets at the boundary.

Note: this is a design choice, not a research finding. The published
calibration literature is split — proper-scoring-rule RL with continuous
scores remains competitive on benchmarks. We chose buckets because they
expose a smaller surface for the LLM to over-interpret, and because the
threshold values are easier to tune from observed agent behavior than a
continuous scoring head.

```
confidence: "HIGH" | "MEDIUM" | "LOW" | null
```

| Bucket | Internal float range | Semantics |
|---|---|---|
| `HIGH` | ≥ 0.8 | Schema-sourced facts, declared FKs, exact name matches. |
| `MEDIUM` | 0.5 – 0.8 | LLM-generated descriptions with strong context; query-log-inferred joins with multiple observations. |
| `LOW` | < 0.5 | LLM-generated descriptions with weak context; single-observation inferences. |
| `null` | n/a | Confidence does not apply (e.g. on a structural facts-only response). |

Thresholds are a calibration knob — adjusted as agent task success data
accumulates.

**Provenance** is a per-field annotation on LLM-generated or inferred
content. Schema-sourced facts do not carry provenance — their source is
obvious.

```
provenance: {
  source: "schema" | "llm" | "inferred",
  model: <when source = "llm", the model name + version>,
  observed_in: <when source = "inferred", count + first-seen / last-seen>
}
```

**❌ Wrong:**
```json
{"description": "User account record", "score": 0.847}
// Agent has to interpret 0.847; no way to tell if it's schema-sourced or LLM-generated.
```

**✅ Right:**
```json
{
  "description": "User account record",
  "confidence": "MEDIUM",
  "provenance": {"source": "llm", "model": "claude-haiku-4-5"}
}
```

---

### 5. Tools document composition patterns

Most useful agent behavior over SchemaBrain is multi-tool: discover, then
describe, then drill in. The charter declares **canonical workflows** so the
LLM doesn't have to derive them from scratch every session.

Composition patterns live in two places:

1. Inside each tool description (Principle 2 already requires "name 1–2
   common compositions").
2. In an aggregated workflow reference (this section) for the cases that
   span more than two tools.

#### Canonical workflows (v1.0)

| User intent | Workflow |
|---|---|
| "What's in this database?" | `list_indexed_schemas` → `find_relevant_tables(query="*")` |
| "Tell me about a domain (e.g. 'revenue')" | `find_relevant_tables` → `describe_table` (top 1–3 hits) → `describe_column` for any low-confidence descriptions |
| "How do these tables relate?" | `suggest_joins` → `describe_table` on any bridge tables |
| "I want to aggregate something" | `list_metrics` → `describe_entity` (for the bound entity) → `get_metric` |
| "Show me how others have queried this" | `get_example_queries(table_or_column)` |

**Why declare these explicitly:** without them, every agent re-derives the
workflow from scratch on every session, and the derivation is fragile across
model families. Encoding the workflows once removes that variance.

---

## Specs

### Response envelope

Every MCP tool returns a Pydantic-typed object conforming to this shape:

```
{
  status: "success" | "empty" | "partial" | "degraded" | "error",
  data: <tool-specific Pydantic model | null on error>,
  error: <Error object | null on success>,
  confidence: "HIGH" | "MEDIUM" | "LOW" | null,
  provenance: <Provenance object | null>,
  follow_up_hints: [<tool name>, ...] | null
}
```

`follow_up_hints` is the lightweight version of composition: the tool names
1–3 next tools the agent might want to call. The agent is free to ignore
them, but they reduce the chance of dead-end branches.

#### Transport integration

SchemaBrain delivers the envelope inside MCP's `structuredContent` field,
with a serialized JSON mirror in `content[0].text` for backward compatibility
with clients that don't yet read `structuredContent`. The envelope shape is
published as each tool's `outputSchema` so spec-compliant clients can
validate without an out-of-band Pydantic schema. See the
[MCP specification on tool results](https://modelcontextprotocol.io/specification/2025-11-25/server/tools).

#### Response size discipline

Per [Anthropic's published guidance](https://www.anthropic.com/engineering/writing-tools-for-agents),
tool responses should stay under ~25k tokens unless explicitly necessary.
Tools that can return large payloads expose a `response_format` parameter:

```
response_format: "concise" | "detailed"
```

`concise` returns the minimum useful payload (top match, summary fields).
`detailed` returns the full structured response. Default is `concise` so
agents opt in to larger payloads only when needed.

Applies to `find_relevant_tables`, `describe_table`, and (when shipped)
`get_example_queries`. Tools that always return small payloads
(`describe_column`, `suggest_joins` at low `max_hops`) need not implement it.

### Per-tool metadata

Each tool exposes metadata *alongside* its response (not inside it — that
would pay token cost on every call). The metadata is fetched once per session
by the MCP transport layer.

```
{
  name: <tool identifier>,
  description: <"Use this when..." string, conforming to Principle 2>,
  cost_hint: {
    tokens_estimate: <typical response size in tokens>,
    dollars_estimate: <typical $ cost; null if free>
  },
  latency_hint: "fast" | "moderate" | "slow",

  // SchemaBrain semantic fields
  idempotent: <bool>,
  side_effects: "none" | "read" | "write",

  // Canonical MCP spec annotations — emitted alongside ours so spec-compliant
  // clients can drive confirmation prompts, graduated trust, and routing
  // decisions without parsing SchemaBrain-specific fields.
  readOnlyHint: <bool>,
  destructiveHint: <bool>,
  idempotentHint: <bool>,
  openWorldHint: <bool>,

  charter_version: "1.0"
}
```

Hint semantics:

- `latency_hint`: `fast` < 100ms, `moderate` 100ms–1s, `slow` ≥ 1s.
- `idempotent`: safe to retry without observable change in outcome.
- `side_effects`: `none` = pure compute, `read` = touches the store / source,
  `write` = mutates the store. Only `read` / `none` on the MCP tool surface;
  `write` reserved for future surfaces (e.g. operator-side `apply` / `import`).

Canonical MCP hint mapping (SchemaBrain emits both layers):

| SchemaBrain field | Canonical MCP hint |
|---|---|
| `side_effects: "none"` | `readOnlyHint: true`, `destructiveHint: false`, `openWorldHint: false` |
| `side_effects: "read"` | `readOnlyHint: true`, `destructiveHint: false`, `openWorldHint: true` |
| `side_effects: "write"` | `readOnlyHint: false`, `destructiveHint: true`, `openWorldHint: true` |
| `idempotent: true` | `idempotentHint: true` |
| `idempotent: false` | `idempotentHint: false` |

The canonical hints are defined in the [MCP tool annotations
specification (March 2026)](https://blog.modelcontextprotocol.io/posts/2026-03-16-tool-annotations/).
Well-behaved clients use them to drive UX choices like confirmation prompts
before destructive actions. SchemaBrain emits both layers so spec-compliant
clients get what they expect while agents reading our finer-grained semantics
get the richer information.

---

## Versioning

The charter follows semver:

- **Patch (1.0.0 → 1.0.1)** — clarification, typo fixes, examples added. No
  shape change.
- **Minor (1.0 → 1.1)** — additive changes. New error kinds, new optional
  envelope fields, new principles that don't invalidate prior ones. Backward
  compatible.
- **Major (1.x → 2.0)** — breaking changes. Removing fields, changing field
  types, retiring principles. Backward compatibility is guaranteed within a
  major version only.

Every tool's metadata includes its `charter_version`. The wire field
emits the **shape contract** version (`major.minor` only — e.g. `"1.0"`,
`"1.1"`, `"1.2"`, `"2.0"`); patch bumps are documentation-only and do not change
the wire emission. A consumer pinning on `"1.0"` therefore receives all
1.0.x doc clarifications transparently. Consumers can pin or negotiate.
SchemaBrain commits to maintaining the most-recent two major versions
simultaneously when a major bump occurs.

---

## Enforcement

Three levels, two always-on, one at phase boundaries.

| Level | What it checks | Cost | Cadence |
|---|---|---|---|
| **1. Description lint** | Each tool description starts with "Use this when…", names at least one composition, stays under 500 chars. | $0 | every PR |
| **2. Envelope schema** | Every tool response Pydantic-validates against the envelope. Status enum is honored. Required fields are present. | $0 | every PR |
| **3. Blind agent eval** | Fixed query set run against Claude, GPT, Gemini. Tool-choice agreement and end-to-end task success rate tracked over time. Initial baseline measured once query-log mining surfaces realistic agent intents; thresholds are a calibration knob, not a hardcoded floor. | ~$5–10 / run | phase boundary (end of v0.5, end of v1, end of v2, …) |

Levels 1 and 2 run in CI and gate every PR. Level 3 is a quality gate at
phase boundaries, not per-commit — running it on every PR would burn API
dollars for no compounding benefit between feature batches.

Levels 1 and 2 are implemented as a single script — `scripts/charter_lint.py` —
wired into the `lint-and-unit` job in `.github/workflows/ci.yml`. The script
loads the live FastMCP server, applies the four Principle 2 description rules
above, then round-trips each tool's happy path through `ToolResponse` Pydantic
validation. Contributors can reproduce the gate locally with
`python scripts/charter_lint.py`; rule logic lives in pure functions that are
unit-tested in `tests/test_charter_lint.py`.

---

## Anti-pattern style

This charter does **not** maintain a standalone anti-pattern section. Each
principle above pairs its rule with one ❌ / ✅ example. Anti-patterns are
illustrations of principles, not their own discipline.

Rationale: standalone anti-pattern sections (1) tend to multiply unbounded
as the project ages, (2) read as judgment of other MCP servers in the
ecosystem, and (3) drift in tone from instructional to preachy.

---

## Open items (deferred to future minor versions)

These are known gaps in v1.0. Each will land in a minor version when its
implementation reaches readiness.

- **Error-kind registry expansion** — v1.0 shipped 7 kinds; v1.1
  added 3 (`pii_blocked`, `policy_blocked`, `allowlist_violation`)
  for the refuse-before-execute taxonomy. Real-world agent traffic
  will surface more (especially around partial results, rate-limiting,
  transient failures). Further additions remain minor bumps.
- **`refused` status producers** — first producer landed in v0.4
  (`get_metric` PII-block path emits `refused` + `pii_blocked`).
  `policy_blocked` and `allowlist_violation` remain reserved; the
  `Recovery` shape gained `suggested_rewrite` and `widening_hint`
  fields in v1.1 to support the refuse-with-rewrite and
  refuse-with-widening-hint paths future producers will populate.
- **Eval query set** — the fixed query set used for Level 3 enforcement
  is defined and frozen once the query-log mining feature surfaces
  realistic agent intents from real workloads. Until then, Level 3 runs
  on a hand-curated starter set.
- **`charter_version` negotiation protocol** — v1.0 publishes the version
  in metadata; explicit client-side negotiation is deferred until multiple
  major versions exist.
- **Cost-hint baselines** — `cost_hint` fields ship in v1.0, but the
  numbers are extrapolations until measured against the 2026-05-11 cost
  anchors and beyond.
- **Code-execution surface (paradigm watch)** — Anthropic's November 2025
  [code execution with MCP](https://www.anthropic.com/engineering/code-execution-with-mcp)
  reframes tools as code APIs loaded on demand. SchemaBrain's
  `find_relevant_tables` → `describe_table` chain is a candidate for a
  single `schemabrain.py` module exposing typed Python functions to a
  code-executing agent. Decision deferred until v0.7 once query-log data
  confirms which agent composition patterns dominate. Flagged here so we
  don't appear blind to the paradigm shift.

---

## How to propose changes

Open a PR against this file with:

1. The principle / spec being changed.
2. The motivation (one paragraph — what agent behavior surfaces the gap).
3. The proposed semver bump (patch / minor / major).
4. Backward-compatibility impact, if any.

Discussion happens in the PR. Acceptance requires reviewer sign-off plus a
Level 3 agent eval run if the change touches tool descriptions or response
shape.

---

## Acknowledgements

Principle 2's three-rule "Use this when…" structure and the per-tool cost /
latency metadata block draw heavily on [Anthropic's *Writing effective tools
for AI agents* (September 2025)](https://www.anthropic.com/engineering/writing-tools-for-agents).

The "errors are prompts for the next tool call" framing operationalizes
[the MCP specification's statement](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)
that tool errors should be "actionable feedback that language models can use
to self-correct."

The response envelope shape is inspired by JSON-RPC's success / error duality
and GraphQL's extension fields. The canonical MCP hint integration follows
the [tool annotations specification (March 2026)](https://blog.modelcontextprotocol.io/posts/2026-03-16-tool-annotations/).

The Feb 2026 study of MCP tool description quality
([arXiv 2602.14878](https://arxiv.org/html/2602.14878v1)) provided the
measured baseline cited in Principle 2's verification block.

This is a living document and SchemaBrain's most public design commitment.
Pull requests welcome.
