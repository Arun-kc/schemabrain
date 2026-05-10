# Contributing to Schema Brain

Thanks for your interest. Schema Brain is pre-alpha and the public surface
is small, but the engineering bar is intentionally high — the goal is a
codebase that stays maintainable as it grows.

This document covers everything you need to send a good PR. If something
is unclear, open an issue rather than guessing.

## Quick reference

| Want to... | Run |
|---|---|
| Set up the dev environment | `uv sync --extra dev` |
| Run unit tests | `uv run pytest -m "not integration"` |
| Run integration tests (needs Docker) | `uv run pytest -m integration` |
| Run all tests with coverage | `uv run pytest --cov=schemabrain --cov-branch --cov-report=term-missing` |
| Lint | `uv run ruff check schemabrain tests` |
| Format | `uv run ruff format schemabrain tests` |
| Build the wheel | `uv build --wheel` |

## Dev environment

Schema Brain uses [uv](https://docs.astral.sh/uv/) for dependency
management. Install it first:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then clone and sync:

```bash
git clone git@github.com:Arun-kc/schemabrain.git
cd schemabrain
uv sync --extra dev
```

This installs Schema Brain plus the `dev` extras (`pytest`, `pytest-cov`,
`ruff`, `testcontainers`) into `.venv/`. Subsequent `uv run <cmd>`
invocations use that venv automatically.

### Python versions

| Platform | 3.11 | 3.12 |
|---|---|---|
| Linux x86_64 | Supported (CI) | Supported (CI) |
| Linux aarch64 | Supported | Supported |
| macOS arm64 | Supported (canonical local-dev target) | **Not supported** — `onnxruntime` (transitive via `fastembed`) has no wheel for this combo today. Use 3.11 on Apple Silicon. |
| macOS x86_64 | Best-effort | Best-effort |
| Windows | Not tested | Not tested |

The CI matrix is currently Linux only across both Python versions.
macOS 3.11 is verified by the maintainer locally.

### Postgres for integration tests

Integration tests boot ephemeral Postgres 16 containers via
[testcontainers](https://testcontainers-python.readthedocs.io/). You need
Docker (or Colima / Podman with the Docker socket exposed) running.
Without Docker, skip those tests with `-m "not integration"`.

## Code standards

### Test-driven, with high coverage

We aim for **100% line + branch coverage on production code**. This is
verified in CI with `--cov-fail-under=99` (one-percentage-point buffer
for genuine edge cases). Code without tests gets blocked at review.

The expected workflow for any non-trivial change:

1. **Write the test first** (RED). It should fail for the right reason.
2. **Implement the minimum to pass** (GREEN).
3. **Refactor for clarity** (IMPROVE) without changing behavior.
4. **Verify coverage** with `--cov-branch`; chase down any missed branches.

Tests use **pytest**. Mark integration tests with `@pytest.mark.integration`.
Don't mock the database in unit tests when an in-memory `SQLiteStore` will
do — it's almost always faster and catches real serialization bugs.

### Style

- **`ruff check` and `ruff format` must pass.** No exceptions. The CI gate
  is non-negotiable; please run both before pushing.
- **Type hints on every public signature.** `from __future__ import annotations`
  at the top of every file lets us use 3.10+ syntax (`X | None`) on 3.11.
- **Pydantic frozen models** for domain entities (`model_config = ConfigDict(frozen=True)`).
  Use `model_validator(mode="after")` for cross-field invariants.
- **Small files, focused modules.** Soft cap ~400 lines per file; hard cap
  ~800. If you're past 400, the right fix is usually to split, not to scroll.
- **Don't add abstractions for hypothetical future requirements.** A bug fix
  doesn't need surrounding refactor. Three similar lines beat a premature
  abstraction.
- **Don't add error handling for impossible scenarios.** Trust internal
  invariants and framework guarantees. Validate at system boundaries
  (user input, external APIs); not internally.
- **Comments explain WHY, not WHAT.** Default to no comment. Add one only
  when the why is non-obvious — a hidden constraint, a workaround for a
  specific bug, behavior that would surprise a reader.

### Architecture invariants

A few rules that exist for specific reasons — please don't violate without
opening an issue first:

- **Stores are single-writer; never mutate frozen domain models.** All
  domain types (`Table`, `Column`, `ForeignKey`, `ColumnDescription`,
  `ColumnEmbedding`) are frozen Pydantic models. Build a new one if you
  need to change a field.
- **MCP tool implementations live in `schemabrain/mcp/tools.py` as pure
  functions.** The FastMCP wiring in `server.py` is a thin adapter — keep
  it that way so the impls stay testable without touching the transport.
- **The `eval/golden_sets/ecommerce.json` set is ONE example domain, not
  a default.** Schema Brain is generic; the bundled e-commerce schema is
  there so the eval CLI works out of the box. Don't add e-commerce-specific
  logic to anything outside the `golden_sets/` and `fixtures/` directories.
- **Postgres URLs always use `postgresql+psycopg://`** (we use psycopg v3,
  not psycopg2). Bare `postgresql://` resolves to psycopg2 in SQLAlchemy
  and breaks.

## Commit + PR workflow

### Commit messages

Use [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <subject>

<body explaining the WHY, not the WHAT>
```

Types we use: `feat`, `fix`, `refactor`, `docs`, `test`, `chore`, `perf`,
`build`, `ci`. Scopes match the package layout (e.g. `mcp`, `core`,
`enrichment`, `eval`, `cli`).

The subject line should fit in 70 characters. The body should explain
why the change exists — what problem it solves, what alternatives were
considered, what tradeoffs were accepted. Look at recent commits
(`git log --format=fuller -5`) for the house style.

### PR checklist

Before requesting review:

- [ ] All tests pass (`uv run pytest`).
- [ ] Coverage holds (`uv run pytest --cov=schemabrain --cov-branch --cov-report=term`).
- [ ] Lint and format clean (`uv run ruff check schemabrain tests && uv run ruff format --check schemabrain tests`).
- [ ] CI passes on the PR (`gh pr checks`).
- [ ] PR description explains the WHY — not just what changed.
- [ ] If it's a user-facing change, README and/or docstrings updated.

### Review

The maintainer reviews every PR. Expect direct, opinionated feedback —
that's how the codebase stays coherent. If you disagree with a review
comment, push back; reasoned disagreement is welcome.

## Reporting bugs and requesting features

Use the issue templates in `.github/ISSUE_TEMPLATE/`. The bug template
asks for a reproduction; the feature template asks what problem you're
trying to solve. Issues without these get closed with a request to
re-open with the right info — not because we don't care, but because
under-specified issues take more time to triage than they save.

## License

By contributing, you agree that your contributions will be licensed under
the [MIT License](LICENSE) that covers the project. A CLA may be added
later if the project commercializes; contributors who want to be excluded
from any future relicense should say so on their PR.
