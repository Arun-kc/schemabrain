# Releasing SchemaBrain

How a version goes from `main` to an installable, **verified** release. The
publish pipeline ([.github/workflows/publish.yml](.github/workflows/publish.yml))
is manual-dispatch only — every publish is an intentional act — and now gates
the production image on a post-publish smoke that installs the real artifact and
boots it.

## What "verified" means here

A green Publish run no longer means "the upload returned 200." The pipeline has
two verification halves:

1. **Pre-upload (build job)** — the freshly built wheel must bundle the full
   dashboard static export (every surface route + the CSP hash manifest). This
   is what catches an empty-dashboard wheel (the 0.4.0 break).
2. **Post-upload (verify job)** — installs `schemabrain[ui]==<version>` from the
   **live index** into a clean venv, asserts `schemabrain --version` and
   `importlib.metadata.version("schemabrain")` both equal the published version,
   runs `demo --showcase` (engine smoke), then boots the dashboard sidecar and
   hits `/api/health`, `/api/graph`, and `/overview`. It runs for **both**
   targets, so a TestPyPI dress-rehearsal exercises the identical gate. A
   failure here blocks the Docker image build.

The one thing CI cannot do for you is the live MCP host smoke — that stays a
human step, recorded in the checklist below.

## Pre-flight

- [ ] `main` is green and up to date; working tree clean.
- [ ] `pyproject.toml` `version` is the version you intend to ship.
- [ ] `CHANGELOG.md` has a dated section for that version, with the link-ref
      defined, and `[Unreleased]` is empty or dropped.
- [ ] `uv lock` is current (`uv lock` produces no diff).

## Sequence

1. **Tag** the release commit on `main`:
   ```bash
   git tag v<version>          # e.g. v0.6.0
   git push origin v<version>
   ```

2. **TestPyPI dress-rehearsal** — Actions → **Publish** → Run workflow →
   `target = testpypi`. Wait for the run to go green; the **verify** job is the
   proof the pipeline (build → upload → install → boot) works end to end on a
   throwaway index. TestPyPI versions are immutable — if this version was
   already pushed there, bump or skip the rehearsal.

3. **Production publish** — Actions → **Publish** → Run workflow →
   `target = pypi`, `version = <version>` (the `version` input is required for
   the Docker image tag). This runs build → publish → **verify** (now against
   real PyPI) → Docker. All four jobs must be green.

4. **Confirm on PyPI**: <https://pypi.org/project/schemabrain/> shows
   `<version>`. (The verify job already installed it from there, but eyeball it.)

5. **Cut a GitHub Release** for the `v<version>` tag. This is what triggers
   [.github/workflows/publish-mcp.yml](.github/workflows/publish-mcp.yml)
   (`on: release: published`) to sync `server.json` and publish to the MCP
   registry. After it runs, confirm the registry entry resolves.

6. **Live MCP host smoke** (human — the half CI can't cover):
   - [ ] Fresh machine/venv: `pip install 'schemabrain[ui]==<version>'`.
   - [ ] Wire a real host (e.g. Claude Desktop) per `docs/setup/`.
   - [ ] The agent can call a read tool (e.g. `find_relevant_tables`) and gets a result.
   - [ ] A blocked call (catastrophic-PII `get_metric`) is refused with structured recovery.
   - [ ] `schemabrain dashboard` opens and the audit row for the refusal verifies against the Merkle root.
   - [ ] Record pass/fail in the GitHub Release notes (or this PR's thread).

## Why manual dispatch (not tag-triggered)

Free-plan private repos cannot set Required-Reviewer protection on GitHub
Environments, so a tag-triggered chain would have no human gate between TestPyPI
and PyPI, and PyPI versions are immutable forever. Manual dispatch keeps each
publish deliberate. Once the package is well established this can convert to a
release-triggered design.
