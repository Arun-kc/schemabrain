---
title: "PII policy"
description: "How SchemaBrain enforces PII categories, where the policy file lives, and how operators edit it."
---

# PII policy

The PII firewall has two halves: a **block set** (which categories the
firewall refuses at query time) and **per-column overrides** (operator
assertions that the heuristic classifier got a specific column wrong).
Both live in one YAML file in your project: `./schemabrain/pii_policy.yaml`.

This page walks through the layout, the CLI, and the common patterns.

## Where files live

A SchemaBrain project on disk looks like this:

```
your-project/
├── schemabrain.db          # SQLite store — `.gitignore`'d (build artifact)
└── schemabrain/            # project root for YAML — committed to git
    ├── entities/
    │   ├── customer.yaml
    │   └── order.yaml
    ├── metrics/
    │   ├── total_revenue.yaml
    │   └── order_count.yaml
    ├── joins/
    │   └── customer_orders.yaml
    └── pii_policy.yaml     # ← the policy file
```

The store is the runtime source of truth; the YAML is the
operator-editable projection of it. The store is a build artifact and
is excluded from git by default. The YAML directory is intended to be
committed, code-reviewed, and version-controlled — just like dbt's
`models/` directory.

`schemabrain init --emit-yaml-dir ./schemabrain` writes the full
directory layout, including a starter `pii_policy.yaml` seeded with
the catastrophic-leak defaults. `schemabrain apply ./schemabrain`
reads every YAML in the directory (entities, metrics, joins, AND
`pii_policy.yaml`) back into the store.

## The policy file

A minimal `pii_policy.yaml`:

```yaml
version: 1
block:
  - credential
  - payment_card
  - government_id
```

A real-world one with per-column overrides:

```yaml
version: 1
description: |
  PCI-DSS Q&A 1.1 — `card_number_last4` alone isn't sensitive,
  so we downgrade it to `internal` to keep checkout analytics working.
block:
  - credential
  - payment_card
  - government_id
column_overrides:
  public.payment_methods.card_number_last4:
    sensitivity: internal
    categories: []
  public.users.email:
    sensitivity: internal
    categories: []
```

### `block`

A list of [`PIICategory`](#pii-categories) values. When a column with
any of these categories appears in a `get_metric` plan, the firewall
returns a `pii_blocked` refusal instead of executing the query. The
`describe_*` family always blocks the catastrophic-leak floor
(`credential`, `payment_card`, `government_id`) regardless of `block`
— that's the minimum-decency line, not a policy setting.

`block: []` (an explicit empty list) **disables `get_metric` PII
enforcement entirely**. PII tags still flow into the audit row so
forensics is unaffected. Use this when you've classified your data
and confirmed analytics-only access is appropriate for every column.

### `column_overrides`

A mapping from `schema.table.column` to a `sensitivity` +
`categories` pair. Each override replaces the heuristic classifier's
output for that one column.

The most common pattern is **downgrading an over-tagged column**:
the heuristic flags `card_number_last4` as `payment_card` from the
column name, but PCI-DSS explicitly allows storing the last four
digits. Without an override, every analytics query that touches
this column would refuse.

```yaml
column_overrides:
  public.payment_methods.card_number_last4:
    sensitivity: internal       # operator-asserted: not personal data
    categories: []              # no PII categories carried
```

You can also **upgrade an under-tagged column** — the heuristic
matches column names against a closed rule table, so columns with
unusual names get tagged `public` by default. If you know
`public.app_state.session_token` carries credential data, assert it:

```yaml
column_overrides:
  public.app_state.session_token:
    sensitivity: pii
    categories:
      - credential
```

### `description`

Optional free-text annotation. Carried through git diffs so reviewers
see why a policy change landed without having to read the PR body.
Recommended for any non-obvious downgrade — future-you and your
security reviewer will both appreciate the breadcrumb.

## The CLI

### `schemabrain policy show`

Print the current policy: active block set, per-column tag listing
with provenance.

```bash
schemabrain policy show
# source:        my-db-source-abc123
# policy_path:   ./schemabrain/pii_policy.yaml
# block source:  yaml (./schemabrain/pii_policy.yaml)
# active block:  credential,government_id,payment_card
# catastrophic floor (always-on for describe_*): credential,government_id,payment_card
#
# per-column tags (4 rows):
#
#   public.payment_methods
#     * card_number_last4              internal      -                              operator  allowed
#   public.users
#       email                          pii           contact                        heuristic allowed
#       phone                          pii           contact                        heuristic allowed
#       password_hash                  pii           credential                     heuristic blocked
#
# legend: `*` = operator override · verdict columns are advisory
```

The `*` marker flags operator-asserted overrides. The verdict column
distinguishes `allowed` (not blocked), `describe_blocked` (refused by
`describe_*` via the catastrophic floor but allowed by `get_metric`),
and `blocked` (refused by both).

### `schemabrain policy apply <yaml>`

Load a `pii_policy.yaml` into the store. Writes the
`column_overrides` as `origin='operator'` rows; the `block:` field
stays in the YAML (read by `serve` at startup).

```bash
schemabrain policy apply ./schemabrain/pii_policy.yaml
# applied ./schemabrain/pii_policy.yaml: block=credential,government_id,payment_card;
# 2 column override(s) persisted to ./schemabrain.db for source my-db-source-abc123.
#   block set lives in YAML; `serve` reads it at startup.
```

You can also use the project-level apply, which picks up the policy
file alongside entities/metrics/joins:

```bash
schemabrain apply ./schemabrain
# schemabrain apply: ./schemabrain/
#   entities/: applied 4 file(s) (rc=0)
#   metrics/: applied 6 file(s) (rc=0)
#   joins/: applied 2 file(s) (rc=0)
#   pii_policy.yaml: applied (rc=0)
```

### `schemabrain policy tag override`

Apply one column override without editing YAML. Useful for quick
experiments; for permanent state, prefer the YAML round-trip so the
override is git-tracked.

```bash
schemabrain policy tag override public.payment_methods.card_number_last4 \
  --sensitivity=internal --categories=
```

### `schemabrain policy tag clear`

Remove a specific operator override. The heuristic row underneath is
untouched — next `schemabrain index` will re-classify the column.

```bash
schemabrain policy tag clear public.payment_methods.card_number_last4
```

### `schemabrain policy tag list`

Provenance-filterable listing of every PII tag for the source.

```bash
schemabrain policy tag list --origin=operator
```

## The `serve` integration

`schemabrain serve` reads `./schemabrain/pii_policy.yaml` at startup
when `--pii-block` is omitted:

```bash
schemabrain serve --url-env DATABASE_URL
# schemabrain serve: --pii-block read from ./schemabrain/pii_policy.yaml:
# credential,government_id,payment_card.
```

Explicit `--pii-block` always wins:

```bash
# Override the YAML for a one-off test run.
schemabrain serve --url-env DATABASE_URL --pii-block ''
# warning: --pii-block '' (explicit empty) disables refusal enforcement.
```

`--policy-path PATH` lets you point at a different file (e.g. a
per-environment policy in CI):

```bash
schemabrain serve --url-env DATABASE_URL --policy-path ./ci/pii_policy.yaml
```

## Common patterns

### Downgrading a false positive

The heuristic flags any column whose name contains `card`,
`account_number`, etc. as `payment_card`. If the column genuinely
isn't payment data (e.g. `gift_card_design_id`), downgrade it:

```yaml
column_overrides:
  public.products.gift_card_design_id:
    sensitivity: public
    categories: []
```

### Locking down a custom credential column

If your schema carries credential data in a column the heuristic
doesn't recognise (e.g. `meta.tenant_api_token` on a generic table),
escalate it:

```yaml
column_overrides:
  meta.tenant_api_token:
    sensitivity: pii
    categories:
      - credential
```

### Disabling enforcement entirely

For dev databases where every column is synthetic, an empty `block`
is the cleanest signal. Per-column tags still flow into audit rows
so you can still see what was touched.

```yaml
version: 1
description: Dev database — all data is synthetic, no enforcement.
block: []
```

### Per-environment policies

Keep one YAML per environment, swap with `--policy-path`:

```
schemabrain/
├── pii_policy.yaml            # production: strict
├── pii_policy.staging.yaml    # staging: looser block, more overrides
└── pii_policy.dev.yaml        # dev: empty block, all-synthetic
```

```bash
schemabrain serve --policy-path ./schemabrain/pii_policy.staging.yaml ...
```

## PII categories

The 12 categories the heuristic + override layer can express:

| Category | Examples |
|---|---|
| `contact` | email, phone, address |
| `financial` | account balance, transaction amount |
| `payment_card` | full PAN, CVV, expiry |
| `health` | diagnosis, medication, lab result |
| `genetic` | DNA sequence, genetic test result |
| `biometric` | fingerprint hash, face embedding |
| `behavioral` | clickstream, page visit |
| `online_identifier` | IP, user agent, device ID |
| `credential` | password hash, session token, API key |
| `government_id` | SSN, tax ID, passport number |
| `location` | GPS coordinates, IP-derived geo |
| `demographic_protected` | race, religion, sexual orientation |

The **catastrophic-leak floor** is `{credential, payment_card,
government_id}`. These are always refused by `describe_*` regardless
of `block:` — the operator can downgrade individual columns via
`column_overrides` but can't turn off the floor.

## Workflow recap

1. **`schemabrain init --emit-yaml-dir ./schemabrain`** — wizard
   indexes your database and emits the starter `pii_policy.yaml`
   alongside the entity/metric/join directories.
2. **Edit `./schemabrain/pii_policy.yaml`** in your editor — adjust
   `block`, add `column_overrides`.
3. **`schemabrain policy apply ./schemabrain/pii_policy.yaml`** —
   persist overrides to the store. Or use `schemabrain apply
   ./schemabrain` to apply everything in one pass.
4. **Restart `schemabrain serve`** — picks up the new `block` from
   the YAML.
5. **Commit** the YAML to git. Security review happens in PR.

## Related

- [Threat model](./threat-model.md) — how the block set + catastrophic
  floor compose with the `get_metric` and `describe_*` enforcement
  paths.
- [Setup](./setup.md) — full project initialisation walkthrough.
