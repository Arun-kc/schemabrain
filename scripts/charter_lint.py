"""Schema Brain MCP Charter v1.0 — CI lint.

Promotes Charter §Enforcement Levels 1 and 2 from unit-test-level
assertions to a PR-gate check. Runs in CI before the unit suite so a
contributor-facing failure shows up fast and points at the exact
description / envelope contract that drifted.

Level 1 — description lint (per `docs/agent-ux-charter.md` §Enforcement):
  - description starts with "Use this when…" (case-insensitive)
  - description includes "instead when" or "Don't use when"
  - description names at least one sibling tool ("composition")
  - description is ≤ 500 chars

Level 2 — envelope schema:
  - Building the live FastMCP server and round-tripping each tool's
    happy-path call validates the structured response against the
    `ToolResponse` Pydantic envelope. Drift (e.g. a tool added without
    the wrapper) trips here.

Exit codes:
  0 — all checks passed
  1 — at least one violation (prints a human-readable summary first)

Usage (local + CI):
  python scripts/charter_lint.py

The companion test file `tests/test_charter_lint.py` covers each rule
in isolation plus the CLI exit-code contract.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

# Make `schemabrain` importable when this script is invoked directly
# from the repo root (`python scripts/charter_lint.py`). When invoked
# via pytest the package is already on sys.path from the editable
# install — this path mutation is a no-op in that case.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pydantic import ValidationError  # noqa: E402

from schemabrain.mcp.envelope import ToolResponse  # noqa: E402

_MAX_DESCRIPTION_CHARS = 500
_USE_THIS_WHEN_PREFIX = "use this when"
_DISAMBIGUATION_MARKERS = ("instead when", "don't use when")
# Canonical v1.0 sibling set. Lives here (not derived from the server)
# because (a) the lint must be runnable standalone without spinning up
# the server, and (b) any change to the tool roster is a deliberate
# charter event that the contributor should update here on purpose.
_SIBLING_TOOLS = frozenset(
    {
        "find_relevant_tables",
        "describe_table",
        "describe_column",
        "suggest_joins",
    }
)


@dataclass(frozen=True)
class ToolSummary:
    """Minimal tool record the lint operates on.

    Kept independent of the FastMCP `Tool` type so unit tests can build
    one without standing up a server.
    """

    name: str
    description: str


@dataclass(frozen=True)
class Violation:
    """A single rule failure — one rule, one tool, one human message."""

    rule: str
    tool: str
    detail: str


_RuleFn = Callable[[str, str, frozenset[str]], "Violation | None"]


def check_use_this_when(name: str, description: str, _siblings: frozenset[str]) -> Violation | None:
    if description.lower().lstrip().startswith(_USE_THIS_WHEN_PREFIX):
        return None
    preview = description[:40].strip() or "<empty>"
    return Violation(
        rule="use_this_when",
        tool=name,
        detail=(
            f"description must start with 'Use this when…' "
            f"(Charter Principle 2 Rule 1); got {preview!r}"
        ),
    )


def check_disambiguation(
    name: str, description: str, _siblings: frozenset[str]
) -> Violation | None:
    lowered = description.lower()
    if any(marker in lowered for marker in _DISAMBIGUATION_MARKERS):
        return None
    return Violation(
        rule="disambiguation",
        tool=name,
        detail=(
            "description must include 'instead when' or 'Don't use when' "
            "(Charter Principle 2 Rule 2)"
        ),
    )


def check_composition(name: str, description: str, siblings: frozenset[str]) -> Violation | None:
    others = siblings - {name}
    if any(other in description for other in others):
        return None
    return Violation(
        rule="composition",
        tool=name,
        detail=(
            f"description must name at least one sibling tool from "
            f"{sorted(others)} (Charter Principle 2 Rule 3)"
        ),
    )


def check_length(name: str, description: str, _siblings: frozenset[str]) -> Violation | None:
    length = len(description)
    if length <= _MAX_DESCRIPTION_CHARS:
        return None
    return Violation(
        rule="length",
        tool=name,
        detail=(
            f"description is {length} chars; Charter Enforcement Level 1 "
            f"caps at {_MAX_DESCRIPTION_CHARS}"
        ),
    )


_DESCRIPTION_RULES: tuple[_RuleFn, ...] = (
    check_use_this_when,
    check_disambiguation,
    check_composition,
    check_length,
)


def lint_descriptions(tools: Iterable[ToolSummary]) -> list[Violation]:
    """Run every Level 1 rule against every tool. Returns the flat list
    of violations across tools and rules (in stable iteration order so
    CI output stays diffable)."""
    violations: list[Violation] = []
    for tool in tools:
        for rule in _DESCRIPTION_RULES:
            violation = rule(tool.name, tool.description, _SIBLING_TOOLS)
            if violation is not None:
                violations.append(violation)
    return violations


# --- Level 2: envelope schema -------------------------------------------------

# Each tool's happy-path argument shape. Kept tiny — Level 2 is a
# "does the envelope round-trip" smoke, not a behavior test, so an
# error envelope counts as a structural pass (the validator only
# rejects shape violations, e.g. missing `status`). `suggest_joins`
# with duplicate names returns a `malformed_name` envelope; the
# `ToolResponse` validates cleanly either way.
_HAPPY_PATH_ARGS: dict[str, dict[str, object]] = {
    "find_relevant_tables": {"query": "anything", "limit": 5},
    "describe_table": {"qualified_name": "public.users"},
    "describe_column": {"qualified_name": "public.users.email"},
    "suggest_joins": {"tables": ["public.users", "public.users"]},
}


def lint_envelope_validation(server) -> list[Violation]:
    """Call each registered tool with a happy-path arg set and verify
    the structured response Pydantic-validates against `ToolResponse`.
    """
    violations: list[Violation] = []
    tools = asyncio.run(server.list_tools())
    for tool in tools:
        args = _HAPPY_PATH_ARGS.get(tool.name, {})
        try:
            _content, structured = asyncio.run(server.call_tool(tool.name, args))
        except Exception as exc:
            violations.append(
                Violation(
                    rule="envelope_schema",
                    tool=tool.name,
                    detail=f"call_tool raised before returning an envelope: {exc!r}",
                )
            )
            continue
        try:
            ToolResponse.model_validate(structured)
        except ValidationError as exc:
            violations.append(
                Violation(
                    rule="envelope_schema",
                    tool=tool.name,
                    detail=(
                        "structured response failed ToolResponse validation: "
                        f"{exc.error_count()} error(s); first={exc.errors()[0]!r}"
                    ),
                )
            )
    return violations


def _live_tool_summaries(server) -> list[ToolSummary]:
    """Project the live server's tool list into the `ToolSummary` shape
    that Level 1 rules consume."""
    return [
        ToolSummary(name=t.name, description=t.description or "")
        for t in asyncio.run(server.list_tools())
    ]


def _build_minimal_server():
    """Spin up a FastMCP server backed by an indexed in-memory store.

    Used by both Level 1 (description metadata) and Level 2 (envelope
    round-trip). The single indexed table (`public.users(email)`) lets
    every tool's happy path return a real envelope without spinning
    up a Postgres container.
    """
    from schemabrain.core.embedding import ColumnEmbedding
    from schemabrain.core.models import Column, Table
    from schemabrain.core.store import SQLiteStore
    from schemabrain.mcp import build_server

    class _LintEmbedder:
        model_name = "lint-emb"
        dimension = 4

        def embed(self, text: str) -> tuple[float, ...]:
            del text
            return (1.0, 0.0, 0.0, 0.0)

    tmp_dir = tempfile.TemporaryDirectory()
    store = SQLiteStore(Path(tmp_dir.name) / "lint.db")
    sid = "lint-src"
    col = Column(
        name="email",
        table_name="users",
        schema_name="public",
        data_type="TEXT",
        nullable=False,
        ordinal_position=1,
    )
    store.write_table(
        Table(name="users", schema_name="public", columns=(col,)),
        source_connection_id=sid,
    )
    store.write_table_embeddings(
        "public",
        "users",
        source_connection_id=sid,
        embeddings={
            "email": ColumnEmbedding(vector=(1.0, 0.0, 0.0, 0.0), model="lint-emb", dimension=4)
        },
    )
    # The TemporaryDirectory is kept alive by attaching it to the
    # server — when the server is GC'd at end of run, the temp dir
    # cleans up too.
    server = build_server(store=store, source_connection_id=sid, embedder=_LintEmbedder())
    server._schemabrain_lint_tmp = tmp_dir  # type: ignore[attr-defined]
    return server


def _format_violations(violations: Iterable[Violation]) -> str:
    lines = ["Charter lint failures:"]
    for v in violations:
        lines.append(f"  [{v.rule}] {v.tool}: {v.detail}")
    return "\n".join(lines)


def main(
    *,
    _load_tools: Callable[[], list[ToolSummary]] | None = None,
    _check_envelopes: bool = True,
) -> int:
    """Run all charter lints. Returns POSIX exit code (0 ok, 1 fail).

    The underscore-prefixed kwargs are test injection points only —
    real callers (CI, contributors) invoke this as a plain script.

    A single FastMCP server is built when no `_load_tools` override is
    supplied — Level 1 reads its tool metadata and Level 2 round-trips
    it, so one TemporaryDirectory does the whole run.
    """
    if _load_tools is None:
        server = _build_minimal_server()
        tools = _live_tool_summaries(server)
    else:
        server = None
        tools = _load_tools()
    violations: list[Violation] = list(lint_descriptions(tools))
    if _check_envelopes:
        envelope_server = server if server is not None else _build_minimal_server()
        violations.extend(lint_envelope_validation(envelope_server))
    if violations:
        print(_format_violations(violations), file=sys.stderr)
        return 1
    print("Charter lint: OK", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
