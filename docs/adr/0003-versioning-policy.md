# ADR 0003 — Versioning policy: strict-semver, conservative 1.0

- **Status:** Accepted
- **Date:** 2026-05-18

## Context

SchemaBrain ships on PyPI as `schemabrain`. Downstream agent
frameworks, MCP host configs, and tooling will pin version constraints
that survive across releases. The MCP tool signatures, CLI argument
shapes, and the Store Protocol (ADR 0002) are the public API surface.

Two version numbers are easy to conflate:

1. **The roadmap milestone.** Internal planning uses `v0` / `v1` /
   `v2` / `v3` as platform milestones: v0 was preview, v1 is the
   semantic layer end-state, v2 adds the `execute` line, v3 is the
   hosted commercial line.
2. **The semver number.** PyPI shows `0.1.0a1`, `0.2.0a1`, `0.3.0`,
   etc. — a release identifier with strict meaning under semver and
   PEP 440.

Marketing pressure is to ship `1.0.0` when the v1 roadmap milestone
lands. That collapses the two numbers and signals an API guarantee we
cannot honor — we have not yet had external users stress-test the
surface in production, and a tool rename in `2.0.0` three months
later is a worse signal than `0.3.0 → 0.4.0` would have been.

This ADR locks the rule so future contributors and reviewers don't
relitigate it.

## Decision

### 1. Stay on `0.x` until the API is battle-tested

`1.0.0` ships ONLY after BOTH:

- External users have used the MCP tool signatures, CLI args, and
  Store Protocol for at least several weeks without a forced break.
- Entity / metric / audit / join shapes have not had to change in
  response to real-user feedback.

Until both hold, every release stays on `0.x`. The asymmetric cost
sets the bias: premature `1.0` → loud breaking change later (bad);
late `1.0` → "still on 0.x" looks slightly unfinished (cheap, easily
addressed by a release post).

### 2. Bump rules inside `0.x`

| Trigger | Bump | Example |
|---|---|---|
| Bug-fix-only churn, no new public surface | alpha within minor | `0.2.0a1 → 0.2.0a2` |
| New layer / new MCP tool / new CLI command shipped | new minor | `0.2.0a1 → 0.3.0` |
| Alpha settled, no churn for a release cycle | stable minor | `0.3.0a* → 0.3.0` |
| Backwards-incompatible change to MCP / CLI / Store Protocol | new major | `1.0.0 → 2.0.0` (post-1.0 only) |

The "new minor" rule covers the typical v1 release cadence. New tools
in `0.x` do NOT need an alpha cycle first — the `a*` suffix is for
pre-stable churn within a minor, not for guarding every release.

### 3. The roadmap → semver mapping

| Roadmap milestone | Likely semver when shipped | Rationale |
|---|---|---|
| v1 (semantic layer end-state) | `0.3.0` (this release) | New tools, but API not battle-tested yet |
| v1 + 4–8 weeks of real usage with no breaks | `1.0.0rc1 → 1.0.0` | Earned stability |
| v2 (execute layer + fingerprint moat) | `1.x` or `2.0.0` if v1 API breaks | Depends on whether execute can be added without breaking entity/metric tools |
| v3 (hosted commercial L6) | Independent versioning | Hosted product, not the OSS package |

### 4. Honest about specs vs convention

This policy stacks two specs plus one convention. Surface the layering
so future contributors don't argue past each other.

**What is strict semver:**

- `0.x` semantics: "API not stable, anything MAY change."
- `1.0.0` semantics: "this defines the public API."
- Post-1.0 MAJOR / MINOR / PATCH meanings.

**What is Cargo / Rust convention, not semver spec:**

- Treating `0.MINOR` bumps as "new feature shipped" and `0.PATCH` as
  "fix only." Semver itself says nothing about bumping inside `0.x`;
  technically every `0.x` change could be a patch bump. We follow
  Cargo because it communicates intent.

**What is PEP 440, not semver:**

- `0.2.0a1` (no separator before `a`). Pure semver writes
  `0.2.0-alpha.1`. PyPI requires PEP 440, so we follow it.

**Acknowledge the tension:**

Semver's FAQ says *"If your software is being used in production, it
should probably already be 1.0.0."* SchemaBrain has live PyPI install
paths, so a strict reading pushes toward `1.0` sooner. We deliberately
choose "battle-tested" over "in production" as the `1.0` trigger.
That is an interpretive choice, not a spec mandate. The asymmetric
cost of breaking an agent-facing API justifies the conservative
reading.

### 5. When to revisit

This policy is settled. Re-litigate ONLY if:

- External users explicitly request a stable `1.0` for pinning.
- A framework integration requires a `1.x` line (e.g. an MCP host
  policy refuses pre-`1.0` packages).
- Substantive feedback from real deployments confirms the entity /
  metric / audit / join shapes are stable.

In any of those cases, this ADR is updated with a new "Superseded by"
status note pointing at the next ADR.

## Consequences

**What becomes possible:**

- Honest release notes: "v1 — the semantic layer is here" can ship
  as `0.3.0` without lying about API stability.
- Breaking changes inside `0.x` are cheap. A future `entities apply`
  rename that surfaces in user feedback at `0.3.0` lands cleanly in
  `0.4.0` without breaking the semver contract.
- Downstream pinning becomes a deliberate choice. Users pinning
  `>=0.3.0,<0.4.0` get an explicit signal that minor bumps may break.

**What changes for the release process:**

- Every PR that adds public surface (CLI command, MCP tool, Store
  Protocol method) is a minor bump. PRs that are bug-fix-only stay on
  the current minor and bump the alpha suffix.
- Release commits carry a single change to `pyproject.toml` plus a
  matching CHANGELOG entry. The Development Status classifier moves
  with the version: `Pre-Alpha` for `0.x.0a*`, `Alpha` for `0.x.0`
  past the first stable minor, `Beta` after the first six months of
  no-break stable releases, `Production/Stable` after `1.0`.

**What remains deferred:**

- The exact `1.0.0` trigger criteria — "several weeks" is
  intentionally vague; the call is made when the evidence is in.
- Whether v3's hosted line uses the same package or a separate one
  (e.g. `schemabrain-cloud`) is a v3 decision, not a versioning
  decision.

## References

- [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html).
- [PEP 440 — Version Identification and Dependency Specification](https://peps.python.org/pep-0440/).
- [Cargo's "Why does Cargo use SemVer?"](https://doc.rust-lang.org/cargo/reference/semver.html)
  — the source of the `0.MINOR` "new feature" convention.
- `project_versioning_policy.md` — the private memory file with
  decision provenance (this ADR is the public-facing distillation).
