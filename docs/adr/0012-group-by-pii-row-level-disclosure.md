---
title: "ADR 0012 — group_by over a PII column is row-level disclosure"
description: "get_metric refuses grouping BY any PII-tagged column, independent of --pii-block policy, because the group keys are the column's raw values."
---

# ADR 0012 — `group_by` over a PII column is row-level disclosure

- **Status:** Accepted
- **Date:** 2026-06-11
- **Charter version referenced:** v1.2
- **Supersedes:** nothing. Extends the T1.6 raw-disclosure rule from ADR 0001's PII taxonomy to the `group_by` projection surface.

## Context

Through v0.5, `get_metric`'s PII firewall made a single binary decision: it
propagated the PII categories of every column the query touched (measure,
`time_dimension`, `group_by`, filter, `JOIN ON`) into one set and refused iff
that set intersected the effective block set (`--pii-block` unioned with the
always-on catastrophic floor `{credential, payment_card, government_id}`). The
gate keyed on **category membership only**.

This category-only gate had a consequence. An agent asked to
"list our users' email addresses" and called:

```
get_metric(name="user_count", group_by=["user.email"])
```

`email` classifies as `contact`. `contact` is **deliberately not** in the
catastrophic floor — there are legitimate aggregate analytics over contact data
(open rates by region), so blocking the category by default would break common
workflows (ADR 0001; `docs/mechanism/pii-taxonomy`). So the intersection was
empty and the call returned `status: "success"` with **all 72 raw emails** as
the group keys.

This was working as designed, and consistent with the *precise* documented
promise ("a query touching a **blocked** category refuses"). But it is the
wrong default for a product positioned as a PII-aware refusal firewall:

- `count(*) ... GROUP BY email` returns one row per distinct email — the group
  keys **are** the raw values. It is `SELECT DISTINCT email` in disguise:
  enumeration, not aggregation.
- The same vector applies to any non-catastrophic category. There is no
  value-level redaction of result rows and no cardinality cap, so any tagged
  column not in the operator's block set was enumerable through `group_by`.
- The code already recognised this in a comment ("`group_by` labels are
  row-level disclosure") but applied the reasoning only to justify unioning the
  catastrophic floor — never to non-floor categories.

There was already an exact precedent for the right shape: the **T1.6 MIN/MAX
guard** refuses `MIN`/`MAX` over any tagged column *independent of policy*,
because those aggregates return an actual row value. `group_by` projection is
the same class of disclosure on a different surface.

## Decision

`get_metric` refuses any metric that **groups BY a column carrying any
non-empty PII category**, independent of the operator's `--pii-block` policy.

- The check lives in `_resolve_pii_categories`
  (`schemabrain/mcp/get_metric.py`), placed alongside the MIN/MAX guard, and
  fires **before** SQL is emitted or executed. It raises `PiiBlockedError`
  → `status: "refused"`, `kind: "pii_blocked"`.
- It is **policy-independent**, exactly like MIN/MAX. Even `--pii-block ''`
  (the "block nothing" opt-out) does not allow it: raw PII values must never
  appear in output rows. `--pii-block` governs which *aggregate* categories
  may flow; it does not govern raw-value disclosure.
- It is scoped to the **`group_by` surface only**. Aggregating OVER a tagged
  column (`SUM`/`COUNT`/`AVG`) returns a derived number, and filtering BY a
  tagged column (`WHERE email = :v`) does not project raw values — both stay
  governed by the existing category-policy gate. Only the projection refuses.

The principle this generalises: **no raw PII values in `get_metric` output
rows, ever** — whether via `MIN`/`MAX` (T1.6) or `group_by` (T1.8).

## Consequences

- **The natural "list emails" question now refuses** with a clean structured
  `pii_blocked` envelope — the demo's worst beat becomes a real firewall
  moment. The catastrophic floor still hard-blocks credential/card/gov-id at
  every surface; this change closes the contact (and any non-floor) tier on
  the projection surface.
- **A benign-but-tagged column refuses too.** The name-based classifier tags
  `country_code` / `city` / `postal_code` as `contact`, so grouping by them
  now refuses even though a 2-letter country code discloses no individual.
  The escape hatch is to **reclassify the column `public`** (a per-column PII
  override), not to weaken the rule. Genuinely public dimensions like `region`
  already classify `public` and group freely. Safe-by-default with an explicit
  override is the correct firewall posture.
- **No new envelope status or `kind`.** The grammar stays closed; this rides
  the existing `pii_blocked` refusal.
- **Audit unchanged in shape.** The refusal writes a `status="refused"` /
  `refusal_reason="pii_blocked"` audit row before any query runs.
- Documented as threat **T1.8** in `docs/threat-model.md`, and in
  `docs/mechanism/pii-taxonomy` (§"Grouping by a PII column is row-level
  disclosure") and `docs/security.md`.
