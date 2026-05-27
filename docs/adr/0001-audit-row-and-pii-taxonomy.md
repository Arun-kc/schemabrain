---
title: "ADR 0001 — Audit row + PII taxonomy"
description: "The audit row schema and the two-layer PII taxonomy (Sensitivity + PII category) shipped at v0.4."
---

# ADR 0001 — Audit row schema and PII taxonomy

- **Status:** Accepted
- **Date:** 2026-05-15
- **Charter version referenced:** v1.0.1

## Context

SchemaBrain today describes databases — it never executes queries against
them. Every MCP tool shipped through v0.5 returns derived metadata
(column descriptions, join paths, suggested example queries) and the
audit trail is implicit in the MCP transport logs.

That shape is about to change. The v2 line adds `execute` and
`validate_query` — tools that compile a `MetricDefinition` into a
parameterised SQL statement and run it against the source database. The
moment that lands, three new properties become load-bearing:

1. **Decisions need a record.** "This query was refused because it
   touched `pii:contact` and the caller's role is `analyst_no_pii`" is
   only useful if the record survives, is queryable, and can be
   produced for audit review months later.
2. **Records need to be tamper-evident.** An audit trail that can be
   silently rewritten is worse than no audit trail — it implies
   guarantees that don't hold.
3. **Records need privacy boundaries that match real regulation.** "PII"
   as a single flag conflates GDPR special categories with PCI DSS
   payment data with HIPAA PHI. Regulators audit on the categorical
   distinctions; the schema needs to express them.

The right time to lock this shape is **now**, before tool #5
(`get_example_queries`) ships as the first charter-v1.0.1-compliant
tool. Tool #5's description, recovery contract, and event stream all
need to reference the final audit row and the final PII taxonomy. Once
locked, the v1 implementation lands the table, and v2 widens the
writers; no retrofit required.

The fingerprint primitive used by audit rows has a separate visibility
trajectory (see Privacy-by-construction below) — this ADR documents
what the fingerprint **excludes**, not how it's computed.

## Decision

### 1. The `mcp_audit` table — 14-field DDL

Every MCP tool invocation that touches the source database, returns a
refusal, or completes a charter-defined status writes exactly one row
to `mcp_audit`. The table lives in the same SQLite store as the rest of
SchemaBrain's state.

```sql
CREATE TABLE mcp_audit (
    id                   INTEGER PRIMARY KEY,
    occurred_at          TIMESTAMP NOT NULL,
    source_connection_id TEXT      NOT NULL,
    caller_id            TEXT,
    tool_name            TEXT      NOT NULL,
    status               TEXT      NOT NULL
        CHECK (status IN ('success','empty','partial','degraded','error','refused')),
    refusal_reason       TEXT
        CHECK (refusal_reason IS NULL OR refusal_reason IN (
            'pii_blocked',
            'allowlist_violation',
            'fragment_unsafe',
            'cost_cap_exceeded',
            'ambiguous_resolution',
            'schema_drift'
        )),
    cost_class           TEXT      NOT NULL
        CHECK (cost_class IN ('small','medium','large','refused')),
    pii_categories       TEXT      NOT NULL DEFAULT '',
    ast_shape_hash       BLOB,
    rule_id              TEXT,
    fingerprint          BLOB      NOT NULL,
    fingerprint_version  TEXT      NOT NULL,
    chain_hash           BLOB      NOT NULL
);

CREATE INDEX idx_mcp_audit_occurred_at ON mcp_audit (occurred_at);
CREATE INDEX idx_mcp_audit_fingerprint ON mcp_audit (fingerprint);
```

Per-field semantics:

| # | Field | Purpose |
|---|---|---|
| 1 | `id` | Surrogate row id; monotonic |
| 2 | `occurred_at` | UTC timestamp of the event |
| 3 | `source_connection_id` | Which source DB the row applies to |
| 4 | `caller_id` | Agent identity / role at call time |
| 5 | `tool_name` | MCP tool name (e.g. `execute`, `validate_query`) |
| 6 | `status` | Charter envelope status enum |
| 7 | `refusal_reason` | One of six refusal kinds; NULL unless `status='refused'` |
| 8 | `cost_class` | Bucketed cost (`small`/`medium`/`large`/`refused`) |
| 9 | `pii_categories` | Sorted comma-separated `frozenset[PIICategory]`; `''` when none |
| 10 | `ast_shape_hash` | 32-byte SHA-256 identifying the structural pattern of a refused query — suitable for grouping similar refusals in audit review; NULL for non-refusal rows |
| 11 | `rule_id` | Structural rule identifier (`builtin:…`, `deployment:…`, `fleet:…`) |
| 12 | `fingerprint` | 32-byte SHA-256; the composition is documented in Privacy-by-construction below |
| 13 | `fingerprint_version` | `"fp-v1"` etc; supports formula migration |
| 14 | `chain_hash` | `sha256(prev_row.chain_hash ‖ canonical(this_row[1..13]))`; genesis row uses 32 zero bytes |

### 2. Append-only invariant

The audit table is append-only by three independent mechanisms. Any
single one being weak is acceptable; all three failing simultaneously
is what we design against.

**Mechanism A — SQL triggers reject UPDATE and DELETE.**

```sql
CREATE TRIGGER mcp_audit_no_update
BEFORE UPDATE ON mcp_audit
BEGIN
    SELECT RAISE(ABORT, 'mcp_audit is append-only');
END;

CREATE TRIGGER mcp_audit_no_delete
BEFORE DELETE ON mcp_audit
BEGIN
    SELECT RAISE(ABORT, 'mcp_audit is append-only');
END;
```

**Mechanism B — Role separation at the SQLite-attach layer.** Schema
Brain attaches the audit table through a write-only connection used
exclusively by the audit writer; read paths use a separate connection
that holds a read-only file lock. Both connections ship with the
audit implementation; the writer connection has no exposed code path
for UPDATE or DELETE construction.

**Mechanism C — Hash chain (`chain_hash` column).** Each row's
`chain_hash` is computed as `sha256(prev_row.chain_hash ‖
canonical_serialisation(this_row, fields 1–13))`. The genesis row uses
32 zero bytes for the previous hash. A `schemabrain audit verify` CLI
command (deferred to a later release) walks rows in order, recomputing
each chain hash and reporting the first mismatch.

The chain is **detection**, not prevention — an attacker with write
access to the file can rewrite the chain coherently. The defense is
that any external archive (file-system snapshot, S3 backup, off-host
copy) that captured a prior `chain_hash` can be compared against the
current row to detect tampering between the snapshot and now.

### 3. PII taxonomy — 4 sensitivities, 12 categories

```python
# schemabrain/pii/categories.py
Sensitivity = Literal["public", "internal", "confidential", "pii"]

PIICategory = Literal[
    "contact",                  # email, phone, address, name
    "financial",                # account balances, salary, transaction amounts
    "payment_card",             # PAN, CVV (PCI DSS scope; distinct from financial)
    "health",                   # diagnoses, medications, treatments (HIPAA PHI)
    "genetic",                  # DNA, genomic data
    "biometric",                # face, fingerprint, voice, iris
    "behavioral",               # browsing, purchase history, app usage
    "online_identifier",        # IP, cookies, device IDs, advertising IDs
    "credential",               # passwords, tokens, secrets, API keys
    "government_id",            # SSN, passport, driver's license, tax ID
    "location",                 # GPS, precise geolocation
    "demographic_protected",    # race, religion, sexual orientation, political views,
                                # trade-union membership (GDPR Art. 9 special categories)
]
```

The two layers are intentional:

- **Sensitivity (Layer 1)** drives role-based access decisions. "Role
  `analyst` may read `internal` and `confidential`; never `pii`." One
  axis, four values, ordered `public < internal < confidential < pii`.
- **Categories (Layer 2)** drive refusal patterns. "Role
  `customer_support` may read `pii:contact` but not `pii:financial` or
  `pii:government_id`." Set-valued (`frozenset[PIICategory]`),
  composable across joins.

Why 12 categories rather than a flat enum, and rather than the
3-category short-list (`public | confidential | pii`) some
implementations use:

| Category | Anchored in |
|---|---|
| `contact`, `financial`, `location`, `behavioral`, `online_identifier`, `government_id` | GDPR Arts. 4 + 9; CCPA/CPRA §1798.140(o) personal information categories |
| `health` | HIPAA Safe Harbor 18 identifiers; GDPR Art. 9 special categories |
| `genetic` | GDPR Art. 4(13); HIPAA |
| `biometric` | GDPR Art. 4(14); CCPA |
| `demographic_protected` | GDPR Art. 9 special categories (race, religion, sexual orientation, political views, trade-union membership) |
| `payment_card` | PCI DSS scope (separated from `financial` to reduce scope-creep on non-PCI financial data) |
| `credential` | ISO 27018 + general security practice; passwords / API keys / tokens / secrets |

The list is closed at 12 today but additive — extending it is a minor
charter bump. The current set covers GDPR, CCPA/CPRA, HIPAA, PCI DSS,
and ISO 27018 categorical distinctions without conflating
regulator-distinguished categories.

A Pydantic validator enforces the cross-layer invariant:

```python
# Sketch — exact form lands with the implementation
@model_validator(mode="after")
def _consistency(self) -> EntityColumn:
    if self.sensitivity != "pii" and self.pii_categories:
        raise ValueError("non-pii sensitivity cannot carry categories")
    if self.sensitivity == "pii" and not self.pii_categories:
        raise ValueError("pii sensitivity requires at least one category")
    return self
```

### 4. Propagation across joins and views

Tool authors will need a deterministic rule for "what PII categories
does this joined result carry?" The rule is:

- **Sensitivity propagates by MAX** under the ordering `public <
  internal < confidential < pii`. A view that pulls one column from a
  `public` table and one column from a `pii` table is `pii`. A view
  combining `internal` and `confidential` is `confidential`.
- **Categories propagate by UNION.** A view selecting `users.email`
  (`pii_categories={"contact"}`) and `transactions.amount`
  (`pii_categories={"financial"}`) has effective categories
  `{"contact", "financial"}`.

Propagation is computed at compile time inside
`schemabrain/compiler/` — the audit writer reads the compiler's already-
resolved category set rather than recomputing. This keeps the
compiler's grain check and the audit writer's PII bookkeeping aligned
by construction.

### 5. Privacy-by-construction

The fingerprint primitive (column 12, `fingerprint`) is the load-bearing
audit field for any future fleet-level aggregation. Its visibility
posture is explicit and asymmetric: this ADR documents what the
fingerprint **excludes**; the exact composition is not public until the
hosted fleet-aggregation product ships.

What the fingerprint excludes, and how the design enforces each
exclusion:

| Excluded | Why it's excluded | How the design enforces it |
|---|---|---|
| Row content (any column value from any source database) | Privacy lawsuit; trivial PII leakage | The fingerprint input is a frozen dataclass with no value-typed field; the type system forbids construction with row content |
| Column names from the source database | Schema names leak business context (`billing_invoice_line_items` reveals industry, ERP system) | Source-database identifier strings never reach the fingerprint input; the input has no identifier-string field |
| Literal SQL strings | Even normalised SQL can leak schema vocabulary | Raw SQL strings never reach the fingerprint input; the input has no string-typed SQL field |
| Caller / user / agent identity | The pattern matters across deployments; identity is per-tenant audit, not fleet signal | The fingerprint input has no caller field |
| Database / source identity | Tenant identity is per-row context (`source_connection_id`), not per-pattern | The fingerprint input has no source identifier |
| Timestamps | Time-correlation across tenants is a side channel | The fingerprint input has no time field; `occurred_at` lives in the audit row alongside, not inside, the fingerprint |

The single-paragraph guarantee — quotable verbatim:

> Fingerprints contain no row content, no column values, no identifying
> schema information. We publish what the fingerprint excludes rather
> than how it's computed — the same posture Cloudflare uses for WAF
> signatures and Snyk uses for vulnerability patterns. The exact
> composition will be published when our hosted fleet-aggregation
> product ships.

The CI lint that enforces the fingerprint-input field count is part of
the audit-implementation deliverable; the test asserts the field count
at the dataclass definition and fails on any addition. Adding a
fingerprint field is therefore a deliberate, reviewed action — not an
accidental drift.

## Implementation sequence

This ADR is design-only. The implementation deliverables, in order:

1. **PII taxonomy module first.** `schemabrain/pii/categories.py`
   lands with the `Sensitivity` and `PIICategory` Literal types and
   the propagation helpers. First consumers are the PII classifier and
   the entity blueprint — both of which need the taxonomy before the
   audit table needs it.
2. **Audit module next.** `schemabrain/audit/` lands with the
   `mcp_audit` DDL, the writer, the chain-hash computation, and the
   fingerprint primitive. Tool authors begin emitting audit events
   through a single `audit_write` helper.
3. **Chain verification CLI.** `schemabrain audit verify` walks the
   chain and reports the first mismatch. Deferred to a later release
   once real audit volume justifies it; the chain hash makes
   verification mathematically possible from day one of the audit
   module.
4. **v2 widens the writers.** `execute` and `validate_query` are the
   first tools to populate the `refusal_reason`, `ast_shape_hash`,
   and `rule_id` fields for refusal paths. Non-refusal v2 rows use
   `refusal_reason IS NULL` and `ast_shape_hash IS NULL`.

The ADR is locked **now** so tool #5 (`get_example_queries`) can
reference final field names, final refusal reasons, and final PII
category names in its description and recovery contract.

## Consequences

**What becomes possible:**

- Charter-v1.0.1 tools can name `refusal_reason` values in their
  recovery contracts (e.g. `Recovery(suggested_tool="describe_table")`
  paired with `refusal_reason="pii_blocked"`) and the values won't
  drift between tools.
- Enterprise legal review has a quotable privacy guarantee and a
  closed schema to validate against, separately from any code-level
  audit.
- The fleet-aggregation product can ship without renegotiating the
  audit shape — the schema is forward-compatible by construction
  through `fingerprint_version`.

**What changes for tool authors:**

- Every new MCP tool must define which `refusal_reason` values its
  refusal paths produce (subset of the six listed) and document them
  in the tool's docstring.
- Tools that touch source-database data compute their effective
  `pii_categories` through the compiler's propagation helper rather
  than hand-rolling set-union logic at the tool surface.
- Tools must not log row content into any other surface (stderr, MCP
  transport, exception messages) — the audit row is the only sanctioned
  per-call record, and its column constraints already forbid value
  fields.

**What remains deferred:**

- The exact write path for the audit row (single helper vs decorator)
  is an implementation choice at audit-module landing time.
- The `schemabrain audit verify` CLI shape is deferred to a later
  release; the chain hash makes verification mathematically possible
  from day one of the audit module.
- Cross-deployment fingerprint aggregation is the hosted product
  (v3+); the local audit table never aggregates beyond a single
  deployment.

## References

- [SchemaBrain MCP Charter v1.0.1](../agent-ux-charter.md) —
  status enum, refusal kinds, recovery contracts.
- GDPR Articles 4 (definitions) and 9 (special categories).
- CCPA / CPRA §1798.140(o) personal information categories.
- HIPAA Safe Harbor — 18 identifiers, 45 CFR §164.514(b)(2).
- PCI DSS scope guidance (payment card data isolation).
- ISO 27018:2019 (PII in public clouds).
- Cloudflare WAF signature posture (public exclusion list, private
  composition) — the analogous visibility model.
