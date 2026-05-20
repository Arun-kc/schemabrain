# Positioning audit — 2026-05-20

**Internal doc.** Captures the strategic shift articulated post-PR-#80: Schema
Brain is positioned as a **pluggable semantic + SQL firewall for agents**,
not a contributor-attracting OSS project. PR-3a's contributor-onboarding
scope is deferred until the product surface matches the positioning.

---

## The new framing

The user's stated priority order, 2026-05-20:

1. **Easy to use** — drop in front of any agent, it works
2. **Easy to understand** — value prop legible in seconds
3. **Pure pluggable semantic + SQL firewall for agents** — the unique
   positioning

Everything else (contributor onboarding, GH Discussions, CoC, charter,
good-first-issue tickets) waits until these three are honest.

---

## Score against current state

### 1. Easy to use — **broken**

| Surface | Reality | Gap |
|---|---|---|
| `pip install schemabrain` | PyPI has `0.1.0a1` + `0.2.0a1` only; README says `0.3.0`. New user runs the README command and gets a stale alpha. | **CRITICAL.** Either publish v0.3.0 to PyPI OR rewrite the install line. |
| Quickstart steps | 7+ steps: install, docker run, wait for pg, load fixture, export 2 envs, `schemabrain init`, restart Claude, ask test question. | Pluggable should be 2–3 steps. The `init` wizard already auto-runs docker + loads fixture from stage 0 (D2 from PR-2). The README hasn't caught up. |
| `--url-env DATABASE_URL` everywhere | Every documented command pipes through an env-var indirection. Users hit `bash: DATABASE_URL: unbound variable` before reaching the wizard. | UX #1 from the PR-2 onboarding audit. Wizard now prompts for URL — README should lead with bare `schemabrain init`. |
| `postgresql+psycopg://` driver prefix | Documented as a footnote; users paste pgAdmin's `postgresql://` and get a confusing error. | Already silently rewritten internally (PR #79 Round-3 fold) but the README still tells users to type `+psycopg`. Drop the friction from the docs. |

### 2. Easy to understand — **diluted**

| Surface | Reality | Gap |
|---|---|---|
| README headline (line 7) | "The agent never writes SQL. Schema Brain does, from definitions you control." | ✓ This IS the firewall framing. Good. |
| README sub-headline (line 9) | "Schema intelligence for AI agents on Postgres. Today: validated SQL from your schema. Tomorrow: a SQL-boundary safety layer..." | Competing framing — "schema intelligence" pulls against "firewall." Reader has to reconcile two product identities in the first 50 words. |
| "How it fits" section (line 561+) | Honestly admits: "Today the product is schema intelligence with a working semantic substrate. If you need PII-tagged refusal and parse-before-execute now, track the roadmap — this isn't ready yet." | Honest but undercuts the firewall positioning. Two options: (a) sharpen what IS firewall-shaped TODAY (validated SQL, PII tagging, audit log, `--pii-block`), or (b) keep the disclaimer but make it less prominent. |
| Roadmap section (line 601+) | v0.5 / v1 / v2 / v3 with "safety wedge" at v2. | Reinforces that the firewall is "later." If positioning is firewall-NOW, the v0.5 + v1 items need to be reframed as firewall primitives, not "substrate." |

### 3. Pluggable semantic + SQL firewall for agents — **partial**

What's actually firewall-shaped TODAY (validated against PR-2 smoke):

| Firewall property | Today | Buried? |
|---|---|---|
| Agent never executes raw SQL | ✓ `get_metric` compiles parameterized SQL; agent gets rows + the SQL it ran | Mentioned in headline but not the dominant pitch |
| Read-only enforced at source | ✓ Stage 1 of `init` validates read-only on Postgres | Hidden in stage-1 description |
| PII tagging + agent-visible refusal | ✓ `--pii-block contact,health` returns `status="refused"` with `error.kind="pii_blocked"` | Buried under "Observe the agent" — should be a top-line firewall property |
| Tamper-evident audit log | ✓ Append-only `mcp_audit` table, sha256 chain, `schemabrain audit verify` | Same as above |
| Pluggable into any agent | ✓ `examples/anthropic_demo.py` is 230 LOC, proves drop-in via Anthropic SDK + MCP stdio | Mentioned once at line 268, hidden inside "Inspect the MCP surface (optional)" |

What's NOT firewall-shaped today (v2 work):

- `validate_query` (parse agent-emitted SQL before execution)
- `execute` with hard caps (read-only role at DB layer + statement timeouts)
- Sub-query refusal with rewrite

---

## Concrete gap list (with file:line)

1. [README.md:16-20](../../README.md#L16-L20) — `pip install schemabrain` returns 0.2.0a1, not 0.3.0
2. [README.md:9](../../README.md#L9) — "Schema intelligence" sub-headline competes with firewall framing
3. [README.md:18](../../README.md#L18) — `--url-env DATABASE_URL` headline command leaks env-var indirection
4. [README.md:113](../../README.md#L113) — driver-prefix footnote is now solved internally; can drop the friction
5. [README.md:268](../../README.md#L268) — `examples/anthropic_demo.py` (the pluggable proof) buried inside an "optional" subsection
6. [README.md:466-475](../../README.md#L466-L475) — `--pii-block` firewall property under "Observe the agent" instead of top-line
7. [README.md:561-597](../../README.md#L561-L597) — "How it fits" disclaimer pushes firewall to "tomorrow"; reframe what's firewall-shaped today

---

## Proposed PR sequence (replaces PR-3a/3b/3c/3d)

| PR | Scope | Why now |
|---|---|---|
| **PR-3** (was 3a) | Install honesty + headline sharpening | Without working install, nothing else matters |
| **PR-4** | Quickstart cut to 3 steps (`pip install` → `schemabrain init` → restart Claude) | Wizard already auto-runs docker + loads fixture; README hasn't caught up |
| **PR-5** | Promote firewall properties: `--pii-block`, audit log, validated-SQL story above the fold | Currently buried under observability and roadmap |
| **PR-6** | Promote `examples/anthropic_demo.py` as the pluggable proof | 230-LOC drop-in demo is the strongest "pluggable" evidence we have |
| **DEFERRED** | All of original PR-3a (contributor surface), PR-3b (UX polish backlog), PR-3c (contributor recipes), PR-3d (--url-env rename RFC) | Per user direction; contributor onboarding waits until product surface lands |

---

## PR-3 first cut (proposed)

**Scope (one PR, ~150-200 LOC docs-only):**

1. README install line — replace `pip install schemabrain` with `uv pip install git+https://github.com/Arun-kc/schemabrain.git@main` (or whatever the PyPI publish status is when the PR opens — verify first). Add a note: "PyPI publish in progress; install from source until v0.3.0 lands on PyPI."
2. README sub-headline — drop "Schema intelligence for AI agents on Postgres" line. The headline ("The agent never writes SQL") is sharper alone.
3. README headline command — change `schemabrain init --url-env DATABASE_URL --store-path ./schemabrain.db` to bare `schemabrain init`. Wizard prompts for URL + store path on stage 0.
4. Driver-prefix footnote — remove the "must use `postgresql+psycopg://`" warning since PR #79 rewrites silently. Replace with one neutral line: "Schema Brain accepts standard Postgres connection URLs."

**Out of scope (later PRs):**

- Quickstart restructure (PR-4)
- Firewall-property promotion (PR-5)
- Anthropic-demo promotion (PR-6)
- Contributor surface (PR-3-old, fully deferred)

**Test plan:**

- `pip install` line works against a fresh shell on macOS (manual smoke)
- README renders correctly on GitHub
- All in-doc anchor links still resolve
- `schemabrain init` (no args) reaches stage 0 prompt successfully

---

## Decision needed from user

**Should PR-3's install line use:**

- (a) `pip install schemabrain` — assumes user has published or will publish v0.3.0 to PyPI between now and PR open
- (b) `uv pip install git+https://github.com/Arun-kc/schemabrain.git@main` — honest about the current state, works today
- (c) Both, with PyPI as primary and git+ as fallback for dev installs

The user previously stated they were "handling PyPI publish separately"
(per PR #68 memory). PR-3 ships faster if we pick (b) and the user
upgrades to (c) once PyPI lands.

---

## What this audit deliberately does NOT cover

- **Building v2 firewall primitives** (`validate_query`, `execute` with caps,
  sub-query refusal) — months of work; not a positioning fix.
- **Snowflake / BigQuery / MySQL connectors** — v1 roadmap; not positioning.
- **CLI cosmetic polish** — handled in design-system arc.
- **Test coverage / CI / lint** — already at 99%+ / all-green.

The audit's scope is: make the README and Quickstart honest to the
"pluggable semantic+SQL firewall for agents" positioning. Product
surface changes are downstream of positioning honesty.
