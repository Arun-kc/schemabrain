# Mechanism: read-only by architecture

> **One-line claim:** Agents physically cannot emit a write through SchemaBrain — there is no tool that accepts arbitrary SQL, and no session flag the agent can flip.

Most "SQL firewalls" inspect the query an agent emits and try to block the unsafe ones at parse time. SchemaBrain ships a different mechanism: **no write surface in the binary at all.** The agent doesn't fail to write because we caught it — it fails because the API it would have used doesn't exist.

This page documents the four layers that make that claim load-bearing, with code citations you can verify yourself.

---

## Layer 1 — twelve tools, none of them accept SQL

The MCP server exposes exactly **12 tools** over stdio. Every one of them is a structured-argument call. None accepts a free-form SQL string.

```
$ grep -c '@app.tool' schemabrain/mcp/server.py
12
```

The 12 tools are:

| Tool | What it does |
|---|---|
| `find_relevant_tables` | Semantic search over indexed tables |
| `find_relevant_entities` | Semantic search over curated entities |
| `describe_table` | Physical-schema view of one table |
| `describe_column` | Physical-schema view of one column |
| `get_example_queries` | Surfaces example queries against a table/column |
| `suggest_joins` | Proposes joins between two tables |
| `list_entities` | Enumerates curated entities |
| `list_metrics` | Enumerates curated metrics |
| `list_joins` | Enumerates canonical joins |
| `describe_entity` | Semantic-layer view of one entity |
| `resolve_join` | Resolves a canonical join path |
| `get_metric` | Compiles and runs a metric query (the only execution path) |

There is no `execute_query`, no `run_sql`, no `validate_query`, no `raw_sql_read`. There is no path from an agent prompt to a write at your database, regardless of how cleverly the prompt is written.

**Compare:** the Anthropic reference Postgres MCP server ships an `execute_query` tool that accepts any SQL. Querybear ships a `query` tool with a parse-level safety check. We ship a different shape — the firewall is the *absence* of the tool, not a check on its input.

---

## Layer 2 — every SQL string is parameterized and built by SchemaBrain

The SQL that does flow to Postgres is built by SchemaBrain's compile path from the entity, metric, and canonical-join definitions you reviewed and applied during `init`. The agent never composes a SQL string; it composes a *tool call* whose arguments name structures the operator already validated.

When `get_metric(name="customer_revenue", group_by=["category.name"])` runs, the compiler walks the canonical-join graph, applies PII propagation (see [`/mechanism/pii-taxonomy`](pii-taxonomy.md)), and emits a parameterized SQL statement with bound parameters for every literal. The agent sees the resulting rows and a copy of the SQL that ran — but the SQL was never *its* input.

User-supplied identifiers (table names, column names) flow through [`_validate_ident`](../../schemabrain/mcp/_helpers.py) which enforces the regex `^[A-Za-z_][A-Za-z0-9_$]*$` and a 63-character bound (Postgres `NAMEDATALEN-1`). Any control character, quote, or escape sequence is rejected before reaching the query builder.

---

## Layer 3 — `default_transaction_read_only=on` at every connection

Belt-and-suspenders against a misconfigured downstream: every connection SchemaBrain opens sets the session-level Postgres flag that refuses writes at the *server*, not just at our layer.

```python
# schemabrain/connectors/postgres.py:57  (introspection / index)
connect_args={"options": "-c default_transaction_read_only=on"}

# schemabrain/cli.py:3128  (serve-side metric executor)
options_parts = ["-c default_transaction_read_only=on"]

# schemabrain/cli.py:3284  (query-log miner)
connect_args={"options": "-c default_transaction_read_only=on"}
```

All three database-touching engines set this. If SchemaBrain's code is somehow tricked into emitting an `INSERT`, Postgres itself refuses the transaction. The role you give SchemaBrain need not be a read-only role — though we recommend one — because the session is already locked read-only at the server.

---

## Layer 4 — no connection pooling, no session reuse

Postgres `session_authorization`, `search_path`, and other session settings can be poisoned by a previous connection's behavior if a connection pool reuses sockets. SchemaBrain uses SQLAlchemy's `NullPool` on all three engines — every database call opens a fresh connection and disposes of it.

```
$ grep -n 'poolclass=NullPool' schemabrain/cli.py schemabrain/connectors/postgres.py
schemabrain/connectors/postgres.py:56:            poolclass=NullPool,
schemabrain/cli.py:3126:            poolclass=NullPool,
schemabrain/cli.py:3283:        poolclass=NullPool,
```

There is no pool state to escape from. A misbehaving turn cannot leave a flag set for the next one.

---

## What this is *not*

We are precise about scope so reviewers can audit our claim against the code:

- **It is not a parse-level firewall.** We do not parse agent-emitted SQL because the agent doesn't emit SQL. If you need to allow agent-authored read SQL with parse-level enforcement, our mechanism does not cover that case — see [Querybear](https://querybear.com) for that shape.
- **It is not a guard against operator misconfiguration.** If you give SchemaBrain a Postgres role that has `CREATE`/`DROP` privileges and the session-level read-only flag is somehow stripped (e.g., an extension that overrides session options on connect), our defense becomes single-layer instead of double. We recommend a dedicated read-only role.
- **It is not a guard against side channels.** Read tools can be combined to enumerate the schema, which is the point of the product. PII propagation ([`/mechanism/pii-taxonomy`](pii-taxonomy.md)) is the separate mechanism that limits which *content* the agent can pull through that enumeration.
- **It is not eternal.** If a future version adds a `raw_sql_read` tool — currently not on the v0.5 roadmap — this page will be rewritten and a parse-level defense will become load-bearing for that surface.

---

## Verify it yourself

```bash
# Count the @app.tool decorators
grep -c '@app.tool' schemabrain/mcp/server.py
# 12

# List the tool names
grep -E '@_trace\(' schemabrain/mcp/server.py
# (12 decorators, names match the table above)

# Confirm no write tool exists
grep -E '(execute|insert|update|delete|run_sql|raw_sql)' schemabrain/mcp/server.py | grep '@app.tool'
# (empty — no matches)

# Confirm read-only session flag at all three layers
grep -n 'default_transaction_read_only' schemabrain/connectors/postgres.py schemabrain/cli.py

# Confirm NullPool at all three layers
grep -n 'poolclass=NullPool' schemabrain/connectors/postgres.py schemabrain/cli.py
```

---

## Related mechanisms

- [`/mechanism/pii-taxonomy`](pii-taxonomy.md) — what *content* the agent can pull through the read surface
- [`/mechanism/structured-recovery`](structured-recovery.md) — what the agent gets back when a read is refused
- [`/mechanism/audit-chain`](audit-chain.md) — how every call (including refusals) is recorded
- [`docs/threat-model.md`](../threat-model.md) — the full attack surface, including residual risks
