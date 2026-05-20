# Schema Brain v0.3.0 manual production-DB smoke — 2026-05-18

**Tester:** Arun K C (with Claude Code)
**Build under test:** `schemabrain-0.3.0-py3-none-any.whl` (built from `main` @ `7a95664`)
**Environment:** macOS arm64, Python 3.11.15, fresh venv, `pip install <wheel>` (closest fidelity to a real `pip install schemabrain` without tagging + publishing first)
**Targets:** Pagila, Northwind-Postgres, synthetic PII mockup, AdventureWorks-for-Postgres, synthetic reserved-keyword schema (SportsDB skipped — PR #32/#42 already cover reserved-keyword paths and AW covers table-count + multi-schema)

## Top-line verdict

**Would I tell a friend to install this?** Yes, with one caveat: don't point it at a schema that has `xml` columns until v0.3.1.

The new-user journey (`pip install` → `init` → `inspect` → apply YAMLs → `check`) works cleanly on 4 of the 5 targets. AdventureWorks crashes the indexer on `xml` columns — a real BLOCKER for any Postgres user with a legacy schema that uses xml-typed columns (more common in SOAP-integration and Microsoft-origin DBs than in greenfield Postgres). Mitigation: drop the xml columns at the source, OR add an opt-out flag.

Outside the xml crash, Schema Brain handled every weird real-world surface I threw at it: partitioned tables (Pagila), composite PKs (Pagila + Northwind), self-referential FKs (refused cleanly with a clear message), cross-schema joins (AdventureWorks), reserved-keyword table + column names (synthetic), embedded spaces and `%` in column names (synthetic). Init is fast (1–6 s for schemas under 70 tables on a warm cache).

The PII classifier is the second axis with real findings — several false positives and false negatives that affect real production schemas, none of which crash the tool but several of which leak misleading tags into the audit fingerprint.

---

## Findings — categorised

### BLOCKER (must fix before users without xml columns are surprised — or document the exclusion clearly)

**B1. `xml` columns crash the profiler with an unhandled traceback.**
- File: `schemabrain/profiler/postgres.py:157` (`_fetch_counts`)
- Repro: any Postgres schema with one or more columns of type `xml`. AdventureWorks has 7 base-table xml columns plus 18 view-derived xml columns. Init exits non-zero with a 60-line Python traceback ending in `psycopg.errors.UndefinedFunction: could not identify an equality operator for type xml`.
- Trigger SQL: `SELECT COUNT(DISTINCT resume) AS d_2, ... FROM humanresources.jobcandidate` — Postgres can't compare xml values for equality.
- Mitigation options:
  - Best: detect column type before emitting `COUNT(DISTINCT col)`; for `xml` (and any other no-equality types), emit only `COUNT(col)` (non-null count, no distinct).
  - Defensive: wrap `_fetch_counts` in a try/except; on `UndefinedFunction`, retry without the offending column. Falls back gracefully.
  - Quick band-aid: add an `--exclude-types xml,hstore,...` flag to `index`. Documents the limitation without fixing it.
- User-facing impact: any user with a legacy Postgres schema (Microsoft-origin DBs, SOAP-era apps, document stores) cannot install Schema Brain at all. The traceback gives no guidance.

### SHOULD-FIX (open issues; fix in v0.3.1)

**S1. PII classifier false positive: `<noun>_name` columns in non-PII tables.**
- File: `schemabrain/profiler/pii.py` (heuristic classifier)
- Repro: `product_name` in a non-PII table tagged as `pii (contact)`. Pagila's `category.name` and `language.name` would likely hit the same bug. Any e-commerce catalog with `product_name`, `brand_name`, `category_name` would have its catalog flagged as PII.
- Impact: false PII tags inflate audit row category sets, change fingerprint digests, and trigger `--pii-block contact` refusals where the agent is touching genuinely public catalog data.
- Mitigation: the classifier should consider table context. A simple win: require `name` matches to co-occur with at least one OTHER PII signal in the same table before firing (e.g., the table must also have an `email`/`ssn`/`phone` column). Conservative but reduces false positives dramatically.

**S2. PII classifier false positive: `<token>_id` integer FKs.**
- Repro: `address_id` (BIGINT, FK to addresses) tagged as `pii (contact)` because the column name contains "address". Same will fire on `email_template_id`, `ssn_validator_id`, `medical_form_id`, etc.
- Mitigation: skip PII tagging when the column is a BIGINT/INTEGER ending in `_id` AND has an FK constraint to another table. The FK column is a reference, not the PII data itself.

**S3. PII classifier wrong category: `date_of_birth` / `birthdate` → `pii (contact)`.**
- Repro: `date_of_birth` in the synthetic PII mockup AND `birthdate` in AdventureWorks both tagged as `pii (contact)`. DOB is a HIPAA Safe Harbor identifier and a GDPR demographic-protected attribute — the category should be `demographic_protected` or `government_id`, not `contact`.
- Mitigation: route `birth` / `dob` / `date_of_birth` patterns to `demographic_protected` explicitly.

**S4. PII classifier false negatives.** Several columns that should be tagged aren't:
- `drivers_license` → `public` (should be `government_id`)
- `face_embedding BYTEA` → `public` (should be `biometric`)
- `insurance_id` → `public` (should be `health` or `financial`)
- `age` (INTEGER, on health_records) → `public` (should be `demographic_protected`)
- `patient_id` (FK INTEGER) — same FK ambiguity as S2; in a `health_records` table the FK IS a HIPAA identifier
- Mitigation: expand the regex set; consider table context (a column in a `health_records` table picks up category bias toward `health`).

**S5. Stale "wk-15" roadmap reference in user-visible error message.**
- File: `schemabrain/core/join.py:24` (and 4 other internal references)
- Repro: `joins apply <self-referential-join.yaml>` returns `error in <file>: canonical-join validation failed: self-joins are not supported at v1 (got source_entity == target_entity == 'employee'); deferred to v1 wk-15 alongside grain-aware metrics`.
- Why it's a problem: v1 just shipped AS v0.3.0. The "wk-15" reference points at a roadmap-internal milestone that has already concluded — it's noise to an external user.
- Mitigation: replace with a forward-looking version reference ("deferred to a future release; track at github.com/...") or drop the parenthetical entirely.

### NICE-TO-HAVE (back on the roadmap)

**N1. No `--exclude-schemas` or `--exclude-tables` flag on `index`.**
- Without one, a user can't ergonomically skip the offending part of a schema. AdventureWorks has ~20 views in the `pe`, `hr`, `pr`, `pu`, `sa` "convenience-view" schemas. Schema Brain currently indexes them all as if they were tables — including the xml-bearing views, which is partly how B1 surfaces. If `--exclude-schemas pe,hr,pr,pu,sa` existed, the AW xml crash could be sidestepped by users without code changes.
- Not load-bearing for v0.3.0 — defer.

**N2. `DOMAIN` data types render as the literal word "DOMAIN", not the underlying domain definition.**
- AdventureWorks uses `Flag` (a domain over `bit`). Schema Brain's inspect shows columns of these types as type `DOMAIN`, not `Flag` or `bit`. Mostly informational — doesn't break anything — but a power user inspecting an unfamiliar schema gets less information than they could.

**N3. `init` install time is dominated by fastembed model load (~67MB ONNX).**
- First-run on a cold venv: 5–6 s for a 15-table schema. ~3 s of that is the fastembed init for the column-embedding pipeline. Documented in the README; no action needed for v0.3.0.

---

## Per-database results

### 1. Pagila (DVD rental, partitioned tables) — PASS

```
22 raw tables (\dt) → 15 logical tables after partition dedup
87 columns
Init runtime: 5.8s (cold venv, includes fastembed init)
```

- ✓ **Partition deduplication works.** `payment` is a partitioned table with 7 monthly partitions (`payment_p2022_01` .. `_p2022_07`). Schema Brain correctly collapses these into the parent partitioned table.
- ✓ **Composite PK from partition key surfaces correctly.** `inspect payment` shows BOTH `payment_id` and `payment_date` as `pk` columns (Postgres requires the partition key to be part of the PK on a partitioned table).
- ✓ **Inspect cross-source sentinel.** `inspect` without `--source` shows `— columns (use --source to count)` — the PR #49 design holds.
- ✓ **Entity apply + canonical join + metric apply round-trip.** Applied 2 entities (`customer`, `payment`), 1 metric (`total_revenue` on payment.amount), 1 join (`customer_payments`). All landed clean.
- ✓ **Drift detection.** Dropped `payment.amount` from the live DB; `check` correctly reports `measure_column_missing public.payment.amount` and exits 1.
- ⚠️ **S2:** `address_id` (integer FK) in `customer` tagged `pii (contact)`. False positive.

### 2. Northwind-Postgres (classic OLTP, composite PKs) — PASS

```
14 tables · 92 columns
Init runtime: 1.1s (warm fastembed cache)
```

- ✓ **Composite-PK junction tables (`order_details`, `customer_customer_demo`, `employee_territories`) accept single-column identity declarations** at the entity layer. The entity definition is one identity column; the actual composite PK on the source survives.
- ✓ **Standard FK joins** (`order → customer`, `order_detail → order`) apply cleanly.
- ✓ **Check clean** against 4 entities + 1 metric + 2 joins.
- ⚠️ **S5: self-join refusal references "wk-15"** — `employees.reports_to → employees.employee_id` is the classic manager-employee self-ref. Refused with stale roadmap reference.

### 3. Synthetic PII-heavy mockup (classifier validation) — PASS, but classifier has bugs

```
5 tables (users, payments, health_records, auth_sessions, non_pii_things) · 66 columns
Designed to hit all 12 ADR-0001 PII categories.
```

**Wins:** classifier correctly tagged 11 of 12 categories on the right column subset — `contact` (email/phone/name/address), `biometric` (fingerprint_hash), `government_id` (ssn/tax_id/passport_number), `location` (lat/long), `financial` (amount_cents), `payment_card` (credit_card_number/iban/bank_account_number/routing_number), `health` (diagnosis_code/medication_list/blood_type), `credential` (session_token/api_key/refresh_token), `online_identifier` (ip_address), `demographic_protected` (gender/nationality).

**Bugs:** S1 (`product_name` in `non_pii_things` flagged as PII), S3 (`date_of_birth` → wrong category), S4 (`drivers_license`, `face_embedding`, `insurance_id`, `age`, `patient_id` missed). See SHOULD-FIX section.

### 4. AdventureWorks-for-Postgres (multi-schema, 68 tables, xml columns) — BLOCKED on B1, PASS after workaround

```
5 schemas (humanresources, person, production, purchasing, sales)
68 base tables + ~20 derived views (plus convenience-view schemas pe/hr/pr/pu/sa)
449 columns after dropping xml columns
Init runtime (post-workaround): 5.5s
```

- ❌ **B1: xml columns crash the indexer.** AW has 7 xml columns in base tables. Init exits with an unhandled traceback. After manually `ALTER TABLE ... DROP COLUMN <xml_col> CASCADE` on all 7, init succeeds.
- ✓ **Multi-schema introspection.** Once past B1, all 68 tables across 5 schemas indexed cleanly.
- ✓ **Cross-schema entity apply.** Applied `employee` (humanresources) + `business_entity` (person).
- ✓ **Cross-schema canonical join.** `employee.businessentityid → business_entity.businessentityid` (cross-schema) — applied + verified via `inspect employee` showing the join in the related-entities section.
- ✓ **Check** clean against 2 cross-schema entities + 1 cross-schema join.
- ⚠️ **S3 confirmed:** `birthdate` (DATE) in `humanresources.employee` tagged `pii (contact)` — same wrong-category bug as the PII mockup's `date_of_birth`.
- ⚠️ **N2:** `Flag` domain types render as the literal word `DOMAIN` in inspect.

### 5. Reserved-keyword stress (synthetic) — PASS

```
Table: "order" with columns "user", "select", "from", "weird column with spaces",
"percent (%) of revenue"
```

- ✓ **Reserved keyword table name (`order`)** — indexed, entity-applied, inspect-rendered.
- ✓ **Reserved keyword column names (`user`, `select`, `from`)** — all 6 columns surface correctly.
- ✓ **Embedded space (`weird column with spaces`)** — rendered correctly in inspect.
- ✓ **Embedded `%` and parens (`percent (%) of revenue`)** — PR #42's qualified-name parser fix verified.
- ✓ **PII classifier correctly tags `percent (%) of revenue` as `pii (financial)`** — even though the column name has a `%` literal, the keyword match for "revenue" fires.

PR #32 + PR #42 are doing what they claim.

---

## Recommendations for the v0.3.0 → v0.3.1 plan

In priority order:

1. **Fix B1 (xml profiler crash).** Three lines in `schemabrain/profiler/postgres.py` to skip `COUNT(DISTINCT col)` on no-equality types. Without this fix, any user with a real legacy Postgres schema is locked out. **Smallest, highest-leverage patch.**

2. **Replace S5 (stale wk-15 reference) — cosmetic but user-visible.** 5 grep-replaces in `schemabrain/core/join.py` + `schemabrain/core/store.py`. Trivial.

3. **PII classifier improvements (S1 + S2 + S3 + S4).** This is the v0.3.1 / v0.4 work that the polish-synthesis memo flagged for "after wk-17." Several distinct bugs; each is a small regex change but accuracy of the classifier is load-bearing for the v2 safety-wedge positioning. Worth a dedicated PR with the synthetic PII mockup turned into a regression-test fixture.

4. **Defer N1/N2/N3.** Document the xml workaround in CHANGELOG / docs/setup.md once B1 is fixed.

## Tag decision (morning of 2026-05-18, pre-PR-#55/#56)

**Recommend: fix B1 (xml profiler crash) in a quick v0.3.0 → v0.3.1 patch before tagging.** A v0.3.0 PyPI release that crashes on common legacy Postgres surfaces is the kind of bug that gets flagged in the first 100 issues and damages first-impression trust. The fix is small enough to land in one PR. After that, tag v0.3.0 with the xml fix included.

S5 (wk-15 reference) can ride along in the same patch. PII classifier improvements should be a separate dedicated PR.

---

# Re-smoke verification — same day, after PR #55 + PR #56 merged

**Wheel under test:** `schemabrain-0.3.0-py3-none-any.whl` built from `main @ 6b4d471` (sha256 `2906881dca0a61df1db8048b16cfbc7b486f9808dec0eaf55e5ecae990f7cb6d`).
**Database:** Postgres 16.11 (alpine) on `localhost:5434`, fresh container.

The original smoke surfaced 1 BLOCKER + 5 SHOULD-FIX + 3 NICE-TO-HAVE. PR #55 (B1 + S5) and PR #56 (S1–S4) shipped the same day. This section captures the re-walk against the same 5 targets to confirm the fixes hold and nothing else regressed.

## Status of prior findings

| ID | Status | Verified on |
|----|--------|-------------|
| B1 | **FIXED** (PR #55) | AW-like fixture with 4 base-table xml columns indexed in 1.2 s, exit 0, no `DROP COLUMN` workaround |
| S1 | **FIXED** (PR #56) | PII mockup `catalog_item`: `product_name`/`brand_name`/`category_name`/`language_name` → all `public` |
| S2 | **FIXED** (PR #56) | Pagila `customer.address_id` → `public` (was `pii (contact)`); PII mockup `payment_record.address_id` → `public`; `health_record.patient_id` → `pii (health)` (FK-safe survives); `auth_session.session_id` → `pii (credential, online_identifier)` (both FK-safe) |
| S3 | **FIXED** (PR #56) | PII mockup `user_record.date_of_birth` + AW-like `humanresources.employee.birthdate` → both `pii (demographic_protected)` |
| S4 | **FIXED** (PR #56) | `drivers_license` → `government_id`; `face_embedding` → `biometric`; `age` → `demographic_protected`; `patient_id` + `insurance_id` → `health` |
| S5 | **FIXED** (PR #55) | Northwind self-ref join refused with: `self-joins are not supported (got source_entity == target_entity == 'employee'). Workaround: model each side as a separate entity (e.g.` `manager` `and` `direct_report` `for an employee reports-to graph), then define the canonical join on the FK column from one side.` No `wk-15` reference |

All 6 prior findings confirmed shipped.

## Per-target verification

### 1. Pagila — PASS (15 tables, 87 columns, 3.7 s index)
- ✓ Partition dedup still works (22 → 15).
- ✓ Composite PK from partition key surfaces correctly (`payment_id` + `payment_date` both `pk`).
- ✓ Cross-source sentinel still renders correctly.
- ✓ Drift detection: dropped `payment.amount`, `check` reports `measure_column_missing public.payment.amount`, exit 1.
- ✓ **S2 confirmed on real schema:** `customer.address_id INTEGER` → `public` (was `pii (contact)`). Real PII (`first_name`/`last_name`/`email`) still correctly tagged `pii (contact)`.
- ✓ Related-entities orientation flip verified (outgoing from `payment`, incoming from `customer`).

### 2. Northwind — PASS (14 tables, 92 columns, 1.6 s index)
- ✓ Composite-PK junction tables still accept single-column identity.
- ✓ **S5 confirmed FIXED:** self-ref refusal message is clean + actionable.

### 3. Synthetic PII mockup — PASS (5 tables, 66 columns, 1.2 s index)

All 12 PIICategory values correctly classified on live Postgres reflection. PK exemption verified (`health_record.id BIGINT pk` stays `public`). Documented over-tags reproducible (`patient_satisfaction_score` → `health`, `ip_address` → `contact + online_identifier`) — intentional breadth-over-precision.

### 4. AdventureWorks-like synthetic — PASS (5 schemas, 7 tables, 68 columns, 4 xml columns, 1.2 s index, exit 0)

> **Fixture note:** the full AdventureWorks-for-Postgres is a 250 MB+ download with CSV bundles. For the purpose of re-verifying that the v0.3.0 build no longer crashes on `xml` columns (PR #55 B1), this re-walk used a smaller synthetic fixture that reproduces the same regression signal: multi-schema layout, `xml`-typed base-table columns, cross-schema FKs, and a `DOMAIN` type. The morning smoke already confirmed the full AW path works after the manual `DROP COLUMN xml` workaround; the only thing left to confirm was that the workaround is no longer needed.

- ✓ **B1 confirmed FIXED.** 4 base-table xml columns (`person.person.additionalcontactinfo`, `person.person.demographics`, `humanresources.jobcandidate.resume`, `sales.salesorderheader.territoryxml`) indexed cleanly. Pre-PR-#55, the same shape produced a 60-line `psycopg.errors.UndefinedFunction: could not identify an equality operator for type xml`.
- ✓ Cross-schema entity + join applied; `check` reports 3 healthy entities + 1 healthy join.
- ✓ **S3 confirmed on AW shape:** `humanresources.employee.birthdate DATE` → `pii (demographic_protected)`.
- ⚠️ **N2 still present:** `DOMAIN` types still render as the literal word "DOMAIN" (`salariedflag` / `currentflag`). Not gating.
- ℹ️ **xml columns render as `NULL` in inspect** (`additionalcontactinfo NULL nullable public`). SQLAlchemy reflects `xml` as `NullType`, which stringifies to `"NULL"` — the same shape PR #55's `_NO_EQUALITY_TYPES` catch-all skip relies on. Not a crash; cosmetic display gap only. New N5 below.

### 5. Reserved-keyword stress — PASS (1 table, 6 columns)
- ✓ Table name `order`, column names `user`/`select`/`from`, embedded space, embedded `%` + parens — all render correctly.
- ✓ PII classifier still tags `percent (%) of revenue` → `pii (financial)` via "revenue" keyword.
- ✓ PR #32 + PR #42 fixes hold.

## New findings from the re-smoke (NICE-TO-HAVE; none gating)

### N4 (new). Entity YAML rejects the quoted form `public."order"` for reserved-keyword tables.

```
error: parsing entity_order.yaml: binding.single_table:
       qualified_table must be 'schema.table' form (got 'public."order"')
```

The unquoted form `public.order` works and matches what `inspect` displays. But a user reaching for the idiomatic Postgres quoted form first hits a misleading error. Single-touch fix in `schemabrain/entities/yaml_grammar.py` — widen the error to mention that reserved-keyword tables don't need quoting in this grammar (Schema Brain handles SQL-level quoting automatically).

Defer to v0.3.1.

### N5 (new — display-only). `xml` columns render as `NULL` in inspect.

Cosmetic. See AW-like target above. Single-touch fix in the inspect renderer or upstream where `NullType` is observed. Defer to v0.3.1 alongside N2.

## Updated tag decision (evening of 2026-05-18, post-PR-#55/#56)

**SHIP v0.3.0 to PyPI now.** No SHOULD-FIX remains. The five NICE-TO-HAVE items (N1 `--exclude-schemas`, N2 `DOMAIN` rendering, N3 fastembed cold-start, N4 quoted-form error message, N5 xml display name) can bundle into a v0.3.1 polish PR — none of them block the tag.

Recommended announcement framing: "0.3.0 — the alpha that survives legacy Postgres surfaces (xml columns, reserved keywords, partitioned tables, multi-schema, composite PKs, embedded special chars in column names) and tags 12 PII categories accurately under HIPAA Safe Harbor + GDPR-compatible defaults."

## Re-smoke artifacts

Disposable; under `/tmp/sb_smoke_2026_05_22/` (per-target subdirs `target_pagila/`, `target_northwind/`, `target_pii_mockup/`, `target_adventureworks/`, `target_reserved_keyword/`, plus wheel + downloaded fixtures). Postgres container `sb-smoke-pg-2026-05-22` on `localhost:5434` is also disposable — `docker rm -f sb-smoke-pg-2026-05-22 && docker volume prune && rm -rf /tmp/sb_smoke_2026_05_22` cleans everything.

> The `2026-05-22` directory name is a misnomer left over from a date-pick mistake during this re-smoke — the work happened on 2026-05-18, same day as PR #55 + PR #56. The path is disposable and will be deleted; the report is consolidated here in the original same-day file.
