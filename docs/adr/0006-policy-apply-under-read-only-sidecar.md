---
title: "ADR 0006 — Policy editing under the read-only sidecar invariant"
description: "The dashboard Policy surface stages edits in the browser and 'applies' them by handing the operator the canonical YAML and the schemabrain policy apply command to run — the sidecar never mutates policy state, preserving the GET-only route-table invariant and the localhost-only, no-Node-in-prod posture."
---

# ADR 0006 — Policy editing under the read-only sidecar invariant

- **Status:** Accepted
- **Date:** 2026-06-03

## Context

The post-launch dashboard adds an editable **Policy** surface (`/policy`) — the
design handoff (`app/policy.jsx`) shows a per-column control, a live
`schemabrain.yaml` panel, a staged −/+ diff with an **Apply** button, and a
**Re-sync from store** button on a drift banner. Taken literally, both buttons
*write*: Apply mutates the enforcement policy, Re-sync reconciles the file to the
store.

That collides with a hard invariant the dashboard's read-only posture depends on
and that `tests/dashboard/test_invariants.py` pins:

- **The sidecar is read-only.** `assert_route_table_is_read_only`
  (`schemabrain/dashboard/sidecar.py`) rejects any route whose methods include
  `POST` / `PUT` / `PATCH` / `DELETE`; every `/api/*` route is `GET` / `HEAD` /
  `OPTIONS` only. The sidecar binds `127.0.0.1` and serves bytes; it never holds
  a writable handle to the operator's policy.
- **The policy file is the operator's, edited in their editor + VCS.** The
  canonical enforcement policy lives in `pii_policy.yaml` and is applied to the
  store by the operator running `schemabrain policy apply`. `schemabrain serve`
  resolves the policy **once at boot**; an out-of-band edit is exactly the drift
  the banner reports (ADR references the `_compute_policy_drift` mtime sentinel).

If the dashboard could write the policy, it would: (1) need a non-GET route,
breaking the invariant and widening the localhost attack surface from "read my
metadata" to "silently change what the firewall blocks"; (2) create a second
write path racing the file + `policy apply`, so the YAML in VCS and the store
could diverge with no audit trail; (3) require the sidecar to hold write
credentials to the store it currently only reads.

This is a one-way door for the whole editor surface — it decides what "Apply"
*is* — so it is locked here before the component is built.

## Decision

### 1. The sidecar stays read-only. No write route is added for policy.

The route-table invariant is non-negotiable. The Policy surface adds exactly one
new route, and it is a pure, side-effect-free **GET**:

- `GET /api/pii/policy/preview` — takes the operator's staged block set +
  column overrides as query parameters, constructs an in-memory `Policy`, and
  returns the canonical `policy_to_yaml(...)` rendering plus the line-level diff
  against the currently-active policy. It reads nothing it mutates and writes
  nothing. It is the server-side renderer the staged YAML panel and diff consume
  (see ADR 0007 for why rendering is server-side).

`assert_route_table_is_read_only` continues to pass; the existing
`test_invariants.py` assertion is extended to cover the new route.

### 2. "Apply" means copy-the-YAML + reveal-the-command, not write.

The Apply affordance does **not** persist anything. When the operator clicks
Apply on a non-empty staged diff, the dashboard:

1. **Copies the canonical YAML** (the full server-rendered `pii_policy.yaml`
   body for the staged policy) to the clipboard.
2. **Reveals the exact CLI command** to make it real —
   `schemabrain policy apply pii_policy.yaml` — alongside a one-line reminder
   that the running firewall picks the change up on the next
   `schemabrain serve` (re)start.

The operator pastes the YAML into their `pii_policy.yaml` (under VCS), runs the
command, and restarts serve. The dashboard is a **policy authoring aid**, not a
policy mutation surface: it does the tedious part (deriving correct canonical
YAML + showing the diff) and hands the durable, auditable action back to the
operator's own toolchain.

Staging remains pure client state: toggling a category or a column override
mutates a **pending** object, never the applied baseline. Discard clears
pending. Apply does not clear pending either — the staged state stays visible so
the operator can copy again after pasting; it clears only on a real refetch when
the underlying policy changes (i.e. after they actually ran the command and the
GET reflects it).

### 3. "Re-sync from store" is also copy-a-command, not a write.

The drift banner's reconciliation affordance follows the same rule. Drift means
the on-disk YAML changed after serve booted; the honest resolution is "restart
`schemabrain serve`" (or re-run `policy apply` then restart). The banner reveals
that command; it never reaches back and writes the file or the store.

### 4. Honest copy throughout — no button implies a capability we don't have.

Every actionable control on the surface is labelled for what it actually does:
"Apply" reads "copy YAML + command", the command is shown verbatim, and the
restart caveat is explicit. No control is styled as a live mutation that
silently no-ops or writes through a hidden path. This matches the launch's
no-fabricated-capability rule and the existing read-only PolicyView's footnote,
which already tells operators to edit `pii_policy.yaml` and run
`schemabrain policy apply`.

## Consequences

**What becomes possible:**

- An editor-grade Policy surface — stage, preview canonical YAML, see the diff,
  copy-to-apply — with **zero** change to the sidecar's read-only, localhost,
  no-Node posture.
- The `pii_policy.yaml` file stays the single source of truth, edited through the
  operator's editor + VCS + `policy apply`, so every policy change keeps its
  existing review/audit trail. The dashboard never becomes a second, unaudited
  write path.
- The route-table invariant test keeps the whole `/api/*` surface GET-only as a
  hard merge gate.

**What constrains future evolution:**

- **No one-click apply** from the dashboard while the read-only invariant holds.
  If a future hosted/authenticated transport is ever added (explicitly out of
  scope today, see the post-launch remote-MCP note), one-click apply could be
  revisited *behind that auth boundary* — never on the localhost byte-server.
- The surface is only as fresh as the GET it reads: after the operator runs
  `policy apply` + restarts serve, the dashboard reflects the new policy on its
  next refetch. There is no push; this is acceptable for an authoring aid.

**What remains deferred:**

- A `POST`-based apply behind authentication — tied to any future hosted
  transport, not to this PR.
- Writing the YAML file directly from a local-only helper (e.g. a separate
  privileged CLI bridge) — not pursued; the copy-and-run flow keeps one obvious
  apply path.

## References

- `schemabrain/dashboard/sidecar.py` — `assert_route_table_is_read_only`, the
  existing `GET /api/pii/policy` route, and the new `GET /api/pii/policy/preview`.
- `tests/dashboard/test_invariants.py` — the GET-only route-table merge gate.
- `schemabrain/pii/policy_yaml.py` — `policy_to_yaml` / `parse_policy_yaml`, the
  canonical YAML round-trip the preview route renders with.
- `schemabrain/cli.py` — `schemabrain policy apply` / `policy show`, the
  operator-side apply path the dashboard hands off to.
- `web/components/policy/PolicyEditor.tsx` — the editor whose Apply this governs;
  it supersedes the prior read-only policy view, which already documented the
  edit-YAML-then-`policy apply` workflow in its footnote.
- ADR 0005 — dashboard routing under static export (the read-only, localhost,
  no-Node serving contract this preserves).
- ADR 0007 — Policy editor control model (the levers whose Apply this governs).
