---
title: "Your project"
description: "What schemabrain creates in your project directory, the YAML files you own and edit, and the edit → apply → restart loop."
---

# Your SchemaBrain project

After `schemabrain init`, you have a working agent — but only one file on
disk. This page is the map of what SchemaBrain creates, the YAML you own
and edit, and how a change you make takes effect.

> **There is no `schemabrain.yaml`.** Configuration lives in three places:
> CLI flags, `SCHEMABRAIN_*` environment variables (auto-loaded from a
> `.env` in your working directory — see [Environment](#environment-and-env)),
> and the editable YAML projection described below. There is no single
> top-level config file.

## What `init` creates

A plain `schemabrain init` writes exactly two things:

| Path | What it is | Edit it? |
|---|---|---|
| `./schemabrain.db` | The local SQLite **store** — the runtime source of truth (schema metadata, embeddings, PII tags, entities / metrics / joins, audit chain). Rebuilt by `index`. | No — it's a build artifact. Add it to `.gitignore`. |
| your MCP host config | The `schemabrain` server entry the agent connects through (e.g. `claude_desktop_config.json`). | Rarely — `init` manages it. |

The ~67 MB embedding model is cached **outside** your project at
`~/.cache/fastembed/` (override with `SCHEMABRAIN_FASTEMBED_CACHE`), so it
is shared across projects and survives a `rm -rf` of the project dir.

That's it. **`init` does not write any editable YAML by default** — to get
the files you tune your policy and semantic layer in, you opt in.

## Getting the editable YAML

Pass `--emit-yaml-dir` to project the store into editable YAML:

```bash
schemabrain init --url-env DATABASE_URL --emit-yaml-dir ./schemabrain
```

(`init` reminds you of this in its closing "next steps" when you skip it.)
That writes:

```text
./schemabrain.db                  # the store (gitignore)
./schemabrain/
├── pii_policy.yaml               # block set + per-column PII overrides
├── entities/<name>.yaml          # one file per entity
├── metrics/<name>.yaml           # one file per metric
└── joins/<name>.yaml             # one file per canonical join
```

Commit the `./schemabrain/` directory to git; keep `./schemabrain.db`
ignored. The YAML is your source of truth; the store is the compiled
artifact you rebuild from it.

You can also hand-author these files from scratch (see
[Semantic layer](semantic-layer.md)) or generate candidates with
`schemabrain entities suggest --out-dir ./schemabrain/entities` (and the
matching `metrics` / `joins` subcommands).

## The edit → apply → restart loop

YAML on disk is not live until you load it into the store. Editing a file
and restarting your agent will show **no change** unless you apply it.

1. **Edit** a file under `./schemabrain/`.
2. **Apply** it to the store:
   - whole project: `schemabrain apply ./schemabrain`
   - just the policy: `schemabrain policy apply ./schemabrain/pii_policy.yaml`
   - one resource kind: `schemabrain entities apply ./schemabrain/entities` (and `metrics` / `joins`)
3. **Validate** the change resolves against your live schema:
   `schemabrain check --url-env DATABASE_URL`. Column references in
   metrics, joins, and entities are checked at **compile time**, not at
   apply time — so `apply` succeeding is not proof the layer works.
   `check` catches a typo'd column before your agent ever hits it.
4. **Restart `serve`** (and your MCP host) so the running server picks up
   the new policy. The dashboard is a read-only viewer of the store — it
   reflects what you applied; it does not edit the file.

One nuance worth knowing: the `block:` set in `pii_policy.yaml` is read by
`serve` at startup (no `apply` needed — just restart), while
`column_overrides` persist into the store on `policy apply`. When in doubt,
`policy apply` then restart covers both.

## Editing the PII policy

`pii_policy.yaml` controls what the firewall refuses:

```yaml
version: 1
# Categories the firewall blocks. The catastrophic floor
# (credential, payment_card, government_id) is ALWAYS enforced on top of
# this list — removing those lines does not disable it.
block:
  - payment_card
  - credential
  - government_id
# Replace the heuristic classifier's verdict for a single column.
# The common case is downgrading a false positive:
column_overrides:
  public.customer.email:
    sensitivity: internal     # operator-asserted: treat as non-personal here
    categories: []            # carries no PII categories
```

Operator overrides are **durable across a re-index**: `schemabrain index`
preserves your `column_overrides` and only re-classifies the columns you
have not asserted on. You can re-index to pick up a schema change without
losing your false-positive fixes. See [PII policy](pii-policy.md) for the
full grammar (including the catastrophic-downgrade guard).

## Environment and `.env`

SchemaBrain auto-loads a `.env` from your **current working directory** at
startup (shell exports always win; a missing `.env` is a silent no-op), so
you can keep credentials out of your shell history:

```dotenv
SCHEMABRAIN_DATABASE_URL=postgresql+psycopg://user:pass@host:5432/dbname
ANTHROPIC_API_KEY=sk-ant-...
```

`init` reads `SCHEMABRAIN_DATABASE_URL` by default and writes it into the
host snippet. The standalone CLI examples in these docs use
`--url-env DATABASE_URL` for brevity — `--url-env` takes the **name** of
the variable to read, so pass the name you actually set. Pick one name and
use it consistently (the URL form too — `index` and every later `apply` /
`serve` must use the same scheme/host/port, since the store keys
everything by a hash of the connection URL).

## A note on database credentials

`init`'s read-only check confirms the session can run under
`default_transaction_read_only=on` — it does **not** scope your
credentials. For your own database, connect with a **least-privilege,
SELECT-only role** rather than an admin/superuser URL. Read-only is
enforced architecturally regardless, but a narrow role is defense in
depth and keeps the connection honest.
