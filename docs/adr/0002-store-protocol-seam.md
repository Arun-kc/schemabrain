# ADR 0002 — Store Protocol as the universal persistence seam

- **Status:** Accepted
- **Date:** 2026-05-18
- **Charter version referenced:** v1.1

## Context

Schema Brain persists everything that survives a process restart in
one place: tables, columns, descriptions, embeddings, entities, joins,
metrics, example queries, audit rows, PII tags, the cost ledger, and
the source-config fingerprint. v1 ships a single concrete
implementation — `SQLiteStore` writing to `~/.schemabrain/store.db`
(or `--store-path` override). That's appropriate for v1: the local OSS
agent-side use case is the wedge.

But v3 (the hosted commercial L6 line in the roadmap) introduces a
multi-tenant persistence plane — likely Postgres, possibly with object
storage for embeddings. v2 also adds the `execute` and
`validate_query` tools, which want richer audit metadata than the v1
shape carries. And throughout v1 development, every layer needs to
test against a `Store` mock that doesn't touch SQLite — otherwise unit
tests fight the filesystem and the schema migrations.

Three pressures converge:

1. **v1 substrate must not constrain v3 transport.** A Schema Brain
   that requires SQLite forever cannot ship a hosted plane without a
   substrate migration that breaks every reader.
2. **Testability across the codebase.** Every CLI command, every MCP
   tool, every enrichment pass, and the audit writer all touch the
   store. They cannot each maintain a private mock; they need a single
   seam to substitute against.
3. **Forward-compat shape.** v1 adds new tables every other PR
   (entities, metrics, joins, audit, PII tags). The seam must grow
   additively — adding a method should never break an existing caller.

A Protocol-based seam, with one concrete v1 implementation and a
deliberate posture for extensions, is the durable answer.

## Decision

### 1. The seam is a Python `Protocol`, not an abstract base class

`schemabrain.core.store_protocol.Store` declares the interface every
persistence implementation conforms to. The Protocol is
`@runtime_checkable` so test fixtures can assert conformance via
`isinstance(store, Store)`. Concrete implementations do NOT inherit
from `Store` — duck typing via structural typing is enough.

Why Protocol over ABC:

- **No required base class.** A future in-memory mock, a third-party
  store, or v3's hosted backend can satisfy the Protocol without
  importing schemabrain's class hierarchy. Structural typing is the
  Python idiom for transport-agnostic interfaces.
- **Additive growth.** Adding a method to the Protocol does not break
  existing concrete implementations until callers actually call the
  new method. ABCs would require concrete classes to add the method
  immediately, blocking the protocol bump until every consumer
  catches up.
- **Mypy/pyright already enforce shape drift.** Signature divergence
  between the Protocol and any concrete class surfaces as a type
  error at the first call site that exercises the divergence.

### 2. v1 ships exactly one concrete implementation

`SQLiteStore` in `schemabrain/persistence/store.py` is the v1 backend.
A single concrete implementation:

- Keeps the test surface tractable (no matrix of `[SQLite, Postgres,
  in-memory] × [every layer]`).
- Makes the v1 migration story trivial — schemas advance by a single
  integer in the `meta` table; SQLiteStore's `__init__` runs `ALTER`
  scripts in order on open.
- Defers the multi-implementation tax until v3 has real consumers.

Tests that need a fake store today use ad-hoc `MagicMock` shims keyed
to the specific methods the test calls. No formal `InMemoryStore` is
maintained — that would compound the surface area to keep in sync.

### 3. Schema migrations are integer-versioned, validated on every open

`SQLiteStore.__init__` reads `meta.schema_version`, compares against
the version baked into the code, and:

- Equal → open normally.
- Code newer than store (forward upgrade) → run the gap migrations in
  order, bump the meta row, commit.
- Code older than store (downgrade) → raise
  `SchemaVersionMismatchError` — the operator must reinstall the
  matching `schemabrain` version or wipe the store. No best-effort
  read; the schema is the contract.

The current version is **14** (as of 0.4.0 — `inference_method` +
`validation_state` columns landed at v13→v14 in PR #95; composite-
expression `measure_expression` landed at v12→v13 in PR #91). Version
bumps are visible in `schemabrain/persistence/store.py` and
`CHANGELOG.md` together.

The `mcp_audit` reader paths (`audit list` / `audit verify`)
deliberately open the store with raw `sqlite3.connect` so they can
warn-and-continue on version drift rather than refuse. Per PR #43,
they distinguish three cases: meta-table missing (silent — pre-v11
store), meta-row missing (warn), version mismatch (warn with capped
echo). This is the only place the strict-mismatch contract is
relaxed, and the relaxation is documented inline.

### 4. Writers commit to identifiable types; readers return them

Every write method takes a `dataclass` from `schemabrain.core` (or a
`Mapping[str, ...]` of them). Every read method returns one of those
same types — never a raw `tuple` or a dict shaped like one. The
boundary is value-typed at both ends.

Examples:

- `write_entity(entity: Entity)` returns `None`.
- `get_entity(qualified_name: str) -> Entity | None`.
- `list_entities(source_id: str) -> Iterable[Entity]`.
- `write_metric(metric: Metric)` returns `None`.
- `list_canonical_joins(source_id: str) -> Iterable[CanonicalJoin]`.

This lets every layer above the store treat the persistence boundary
as a typed gateway, not a SQL layer. The compiler reads `Metric` +
`Entity` + `CanonicalJoin` and produces parameterised SQL; it never
has to know that those came from SQLite.

### 5. Three producer paths, one writer

For the entities table specifically, three producer paths converge on
`write_entity`:

1. **Manual YAML** — `schemabrain entities apply <yaml-path>`.
2. **LLM-suggested** — `schemabrain entities suggest --apply`.
3. **dbt-imported** — `schemabrain entities import dbt --apply`.

All three call the same `Store.write_entity`. The store enforces
exactly one invariant they all share: `entity.origin in
{"manual", "dbt", "llm_suggested"}`, persisted on the row. dbt-imported
rows additionally carry the `dbt_owned` flag — attempts to overwrite
a dbt-owned entity via the manual or LLM-suggested paths raise
`DbtOwnedEntityError` at the store boundary. The same pattern repeats
for metrics (manual / dbt / LLM-suggested) and joins (LLM-suggested
only today; manual + dbt-imported are pre-wired but not exposed).

### 6. The Protocol does NOT cover the audit writer

`AuditWriter` in `schemabrain/audit/writer.py` shares the same SQLite
file but holds its own connection — by design. The append-only
invariant in `mcp_audit` (ADR 0001) demands a write-only connection
with no exposed code path for UPDATE or DELETE construction.
`SQLiteStore` exposes read methods over `mcp_audit` (for
`audit list` / `audit verify`) but the WRITE surface lives in
`AuditWriter` alone.

The split is deliberate: a future v3 hosted backend may make the same
choice (separate writer service, separate connection pool) or
collapse them depending on transport. ADR 0001 documents the audit
invariants; this ADR documents the broader persistence shape that
sits underneath.

## Consequences

**What becomes possible:**

- v3's hosted Postgres backend can drop in by writing a new concrete
  class against the Protocol. Existing readers don't change.
- Unit tests across every layer use `MagicMock(spec=Store)` to substitute
  the persistence boundary without touching disk.
- Schema additions land in a single PR per table with a clear
  migration path: add the new dataclass to `schemabrain/core`, add
  the write+read methods to the Protocol, implement them in
  SQLiteStore behind a version bump, update consumers.

**What constrains future evolution:**

- Adding a method to the Protocol that is NOT additively safe (e.g.
  changing a return type, removing a method, narrowing an input type)
  is a breaking change. The versioning policy (ADR 0003) requires a
  major version bump for such changes — practically, this means we
  don't do them in `0.x` and defer to a careful 1.x → 2.x cycle.
- v3's hosted backend cannot use the SQLite schema-migration shape
  unchanged. The integer-versioned migration story works for a
  single-tenant local file; multi-tenant deployments need a richer
  migration substrate (per-tenant version pinning, online migrations,
  etc.). That work is scoped to v3, not v1.
- Tests that need finer-grained store behavior (e.g. simulating a
  write failure mid-call) build ad-hoc shims rather than maintaining
  a parallel `InMemoryStore`. This is fine at v1's scale; if test
  surface area grows past ~5K tests, reconsider.

**What remains deferred:**

- Whether v3 ships a `RemoteStore` (HTTP-backed) or extends `Store`
  with a transport-aware base class is a v3 architectural decision.
- The Protocol does not cover stream/blob persistence (large
  embeddings tables, query history archives) — those layers can use
  their own boundaries when they land.
- Cross-store federation (one schemabrain instance, multiple Store
  backends) is not modelled. v1 assumes one Store per process.

## References

- `schemabrain/core/store_protocol.py` — the Protocol definition.
- `schemabrain/persistence/store.py` — the SQLite implementation.
- ADR 0001 — audit row + PII taxonomy (which uses but does not
  formally extend this Protocol).
- ADR 0003 — versioning policy.
