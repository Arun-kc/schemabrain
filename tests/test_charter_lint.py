"""Tests for the SchemaBrain Charter v1.0 CI lint.

The lint script lives at `scripts/charter_lint.py` and is the PR-gate
enforcement for Charter §Enforcement Levels 1 and 2. The contracts it
checks are *also* pinned by unit tests in `test_mcp_server.py` — those
tests prove the running server is currently compliant; this lint
prevents future drift at PR review time.

Two surfaces under test:

  - **Pure rule functions** — each Level 1 rule is a pure function that
    takes (tool_name, description, siblings) and returns a
    `Violation | None`. Tests feed crafted strings to verify each rule
    fires on its violation and stays silent on compliant input.
  - **End-to-end CLI** — the script can be run as `python
    scripts/charter_lint.py`; the CLI exits 0 on a clean repo (the
    current four MCP tools are charter-compliant) and 1 when any rule
    fires. Sanity-checked via subprocess.
"""

from __future__ import annotations

import asyncio
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from pydantic import BaseModel

from schemabrain.mcp.envelope import ToolResponse

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LINT_PATH = _REPO_ROOT / "scripts" / "charter_lint.py"


# Concrete monomorphic alias for `ToolResponse[None]` at module scope.
# FastMCP's signature inspector evaluates annotations by name in the
# function's module — a generic subscripted in the function body fails
# forward-reference resolution. Defining a real alias here lets the
# refusing-tool helper declare a usable return type.
_RefusedResponse = ToolResponse[None]


class _NotAnEnvelope(BaseModel):
    """Pydantic model used by the synthetic-tool test fixture.

    Module-scoped because FastMCP's signature inspector evaluates the
    return-type annotation by name — a function-local class fails
    forward-reference resolution. See `_build_server_with_raw_dict_tool`.
    """

    foo: str


def _load_lint_module() -> ModuleType:
    """Import `scripts/charter_lint.py` as a module.

    The module is registered in `sys.modules` before `exec_module` so
    `@dataclass` can resolve `from __future__ import annotations`
    forward references against the module's globals — otherwise
    dataclass field-type resolution dereferences `cls.__module__` and
    crashes when the module isn't yet visible.
    """
    spec = importlib.util.spec_from_file_location("schemabrain_charter_lint", _LINT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def lint() -> ModuleType:
    return _load_lint_module()


_SIBLINGS = frozenset({"describe_table", "describe_column", "suggest_joins"})

_COMPLIANT_DESC = (
    "Use this when the user describes a table semantically. Returns "
    "ranked hits with cosine scores. Use `describe_table` instead when "
    "the user names a specific table. Common composition: chain to "
    "`describe_table`."
)


class TestRuleUseThisWhen:
    def test_passes_on_compliant_prefix(self, lint: ModuleType) -> None:
        assert lint.check_use_this_when("find_relevant_tables", _COMPLIANT_DESC, _SIBLINGS) is None

    def test_passes_on_case_insensitive_prefix(self, lint: ModuleType) -> None:
        # Charter prose uses "Use this when" capitalized, but the rule
        # is intentionally case-insensitive so a stylistic edit
        # ("USE THIS WHEN…") doesn't break CI.
        desc = "USE THIS WHEN x. Use describe_table instead when y. " + "x" * 50
        assert lint.check_use_this_when("foo", desc, _SIBLINGS) is None

    def test_fires_when_prefix_missing(self, lint: ModuleType) -> None:
        violation = lint.check_use_this_when(
            "find_relevant_tables",
            "Returns ranked tables. " + _COMPLIANT_DESC,
            _SIBLINGS,
        )
        assert violation is not None
        assert violation.tool == "find_relevant_tables"
        assert violation.rule == "use_this_when"
        assert "use this when" in violation.detail.lower()

    def test_fires_on_empty_description(self, lint: ModuleType) -> None:
        violation = lint.check_use_this_when("foo", "", _SIBLINGS)
        assert violation is not None
        assert violation.rule == "use_this_when"


class TestRuleDisambiguation:
    def test_passes_on_instead_when(self, lint: ModuleType) -> None:
        assert lint.check_disambiguation("foo", _COMPLIANT_DESC, _SIBLINGS) is None

    def test_passes_on_dont_use_when(self, lint: ModuleType) -> None:
        # Charter Principle 2 Rule 2 explicitly allows either phrase.
        desc = "Use this when X. Don't use when Y. Common composition: chain to `describe_table`."
        assert lint.check_disambiguation("foo", desc, _SIBLINGS) is None

    def test_fires_when_no_disambiguation(self, lint: ModuleType) -> None:
        desc = "Use this when X. Common composition: chain to `describe_table`."
        violation = lint.check_disambiguation("foo", desc, _SIBLINGS)
        assert violation is not None
        assert violation.rule == "disambiguation"
        assert violation.tool == "foo"


class TestRuleComposition:
    def test_passes_when_sibling_named(self, lint: ModuleType) -> None:
        assert lint.check_composition("find_relevant_tables", _COMPLIANT_DESC, _SIBLINGS) is None

    def test_fires_when_no_sibling_referenced(self, lint: ModuleType) -> None:
        desc = "Use this when X. Use something else instead when Y. Returns data."
        violation = lint.check_composition("find_relevant_tables", desc, _SIBLINGS)
        assert violation is not None
        assert violation.rule == "composition"

    def test_self_reference_does_not_count(self, lint: ModuleType) -> None:
        # A tool description that mentions only its own name doesn't
        # satisfy Principle 2 Rule 3 — composition means *neighbour*
        # tool, not self.
        desc = (
            "Use this when X. Use this instead when Y. Always call "
            "find_relevant_tables. Returns data."
        )
        violation = lint.check_composition("find_relevant_tables", desc, _SIBLINGS)
        assert violation is not None
        assert violation.rule == "composition"


class TestRuleLength:
    def test_passes_under_500_chars(self, lint: ModuleType) -> None:
        assert lint.check_length("foo", "x" * 499, _SIBLINGS) is None

    def test_passes_at_exactly_500_chars(self, lint: ModuleType) -> None:
        # 500 is the cap — equal is fine, only strictly-greater fires.
        assert lint.check_length("foo", "x" * 500, _SIBLINGS) is None

    def test_fires_over_500_chars(self, lint: ModuleType) -> None:
        violation = lint.check_length("foo", "x" * 501, _SIBLINGS)
        assert violation is not None
        assert violation.rule == "length"
        assert "501" in violation.detail


class TestLintDescriptions:
    def test_returns_empty_for_compliant_tools(self, lint: ModuleType) -> None:
        # Stub mimicking FastMCP's tool listing shape: each entry has
        # `name` and `description`.
        tools = [
            lint.ToolSummary(name="find_relevant_tables", description=_COMPLIANT_DESC),
        ]
        assert lint.lint_descriptions(tools) == []

    def test_aggregates_violations_across_tools(self, lint: ModuleType) -> None:
        tools = [
            lint.ToolSummary(
                name="describe_table",
                description="No charter prefix. Returns data.",
            ),
            lint.ToolSummary(name="find_relevant_tables", description=_COMPLIANT_DESC),
        ]
        violations = lint.lint_descriptions(tools)
        assert len(violations) >= 1
        assert any(v.tool == "describe_table" for v in violations)
        assert not any(v.tool == "find_relevant_tables" for v in violations)

    def test_one_tool_can_trip_multiple_rules(self, lint: ModuleType) -> None:
        tools = [
            lint.ToolSummary(name="bad_tool", description="x" * 600),
        ]
        violations = lint.lint_descriptions(tools)
        rules = {v.rule for v in violations}
        # 600 chars of "x" trips: use_this_when, disambiguation,
        # composition, length — all four.
        assert rules == {"use_this_when", "disambiguation", "composition", "length"}


class TestLintEnvelopeValidation:
    def test_real_server_envelopes_validate(self, lint: ModuleType, tmp_path: Path) -> None:
        # Build the actual MCP server with an in-memory store + stub
        # embedder, invoke each tool's happy path, and Pydantic-validate
        # the structured envelope.
        violations = lint.lint_envelope_validation(_build_test_server(tmp_path))
        assert violations == []

    def test_violation_reported_when_envelope_malformed(self, lint: ModuleType) -> None:
        # Synthetic server with a tool that returns a raw dict missing
        # every required envelope field. The lint must surface this as
        # an `envelope_schema` violation; the detail must mention the
        # Pydantic validation path (not a generic crash) so we know the
        # right code is being exercised, not a side-effect of FastMCP's
        # serialization quirks.
        synthetic = _build_server_with_raw_dict_tool()
        violations = lint.lint_envelope_validation(synthetic)
        assert violations, "expected at least one envelope violation"
        assert any(v.rule == "envelope_schema" for v in violations)
        assert any("ToolResponse validation" in v.detail for v in violations), (
            f"violation must come from the Pydantic path; got {[v.detail for v in violations]!r}"
        )

    def test_pydantic_validation_rejects_non_envelope_dict(self, lint: ModuleType) -> None:
        # Independent of FastMCP: prove that `ToolResponse.model_validate`
        # itself rejects a dict missing the required `status` field. If
        # this ever passes, the Pydantic contract has loosened and the
        # lint's Level 2 check would silently accept malformed envelopes.
        from pydantic import ValidationError

        from schemabrain.mcp.envelope import ToolResponse

        with pytest.raises(ValidationError):
            ToolResponse.model_validate({"foo": "bar"})

    def test_reserved_refused_status_is_violation(self, lint: ModuleType) -> None:
        """v1.1 reserves `refused` for v2 producers. A current tool
        emitting it is a charter violation caught by the lint's
        reserved_status rule."""
        synthetic = _build_server_emitting_refused()
        violations = lint.lint_envelope_validation(synthetic)
        assert any(v.rule == "reserved_status" for v in violations), (
            f"expected a reserved_status violation; got "
            f"{[(v.rule, v.detail) for v in violations]!r}"
        )

    def test_sibling_tools_match_live_server(self, lint: ModuleType, tmp_path: Path) -> None:
        # The composition rule depends on `_SIBLING_TOOLS` being a
        # complete mirror of the live server's tool roster. If a 5th
        # tool ships without an update here, the composition rule
        # becomes silently underconstrained. This test is the cheap
        # backstop that closes the drift gap.
        server = _build_test_server(tmp_path)
        live_names = frozenset(t.name for t in asyncio.run(server.list_tools()))
        assert live_names == lint._SIBLING_TOOLS, (
            f"_SIBLING_TOOLS drift: lint expects {sorted(lint._SIBLING_TOOLS)}, "
            f"live server exposes {sorted(live_names)}"
        )


class TestCLI:
    def test_exits_zero_on_clean_repo(self) -> None:
        # The four MCP tools currently shipping are charter-compliant
        # (proved by `test_mcp_server.py`); the lint must agree.
        result = subprocess.run(
            [sys.executable, str(_LINT_PATH)],
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
        )
        assert result.returncode == 0, (
            f"charter_lint.py exited {result.returncode}; "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )

    def test_main_returns_nonzero_when_violations(self, lint: ModuleType) -> None:
        # Drive `main` with an injected loader that returns a bad tool.
        bad_tools = [lint.ToolSummary(name="x", description="not compliant")]
        rc = lint.main(_load_tools=lambda: bad_tools, _check_envelopes=False)
        assert rc == 1

    def test_main_returns_zero_when_clean(self, lint: ModuleType) -> None:
        # `_COMPLIANT_DESC` references `describe_table` as a sibling,
        # so use it as `find_relevant_tables`'s own description (a
        # tool that *isn't* describe_table) to satisfy the
        # composition rule.
        good = [
            lint.ToolSummary(name="find_relevant_tables", description=_COMPLIANT_DESC),
        ]
        rc = lint.main(_load_tools=lambda: good, _check_envelopes=False)
        assert rc == 0


# ---------- Helpers below this line ----------


def _build_test_server(tmp_path: Path):
    """Stand up a real FastMCP server with one indexed table.

    Mirrors `server_with_one_table` in `test_mcp_server.py` so the lint
    sees the same wiring the unit tests exercise.
    """
    from schemabrain.core.embedding import ColumnEmbedding
    from schemabrain.core.models import Column, Table
    from schemabrain.core.store import SQLiteStore
    from schemabrain.mcp import build_server

    class _StubEmbedder:
        model_name = "test-emb"
        dimension = 4

        def embed(self, text: str) -> tuple[float, ...]:
            del text
            return (1.0, 0.0, 0.0, 0.0)

    store = SQLiteStore(tmp_path / "store.db")
    sid = "src1"
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
            "email": ColumnEmbedding(vector=(1.0, 0.0, 0.0, 0.0), model="test-emb", dimension=4)
        },
    )
    return build_server(store=store, source_connection_id=sid, embedder=_StubEmbedder())


def _build_server_emitting_refused():
    """Synthetic FastMCP server with a tool that returns a structurally
    valid `ToolResponse[...]` envelope using the v1.1 reserved
    `refused` status. The envelope itself Pydantic-validates cleanly
    (refused is in the Status literal); the lint catches the
    reserved-status violation as a separate rule.
    """
    from mcp.server.fastmcp import FastMCP

    from schemabrain.mcp.envelope import Recovery, ToolError, ToolResponse

    app = FastMCP("refused-emitting")

    @app.tool(
        description=(
            "Use this when testing the reserved-refused-status lint rule. "
            "Don't use when not. Use find_relevant_tables instead when "
            "not testing the lint."
        )
    )
    def refusing_tool() -> _RefusedResponse:
        return ToolResponse[None](
            status="refused",
            data=None,
            error=ToolError(
                kind="pii_blocked",
                message="Refused for the test.",
                recovery=Recovery(),
            ),
        )

    return app


def _build_server_with_raw_dict_tool():
    """A synthetic FastMCP server whose tool returns a Pydantic model
    that is NOT a `ToolResponse`. FastMCP serializes any Pydantic
    return as structured content, so `call_tool` returns the expected
    `(content, structured_dict)` tuple — the unpack succeeds and the
    lint's `ToolResponse.model_validate(...)` is the *only* thing that
    can reject it. This pins the test to the Pydantic path, not to
    FastMCP's broad-except crash behavior on raw dict returns.

    The return-type model is module-scoped (`_NotAnEnvelope`) because
    FastMCP's signature inspector evaluates the return annotation by
    name; a function-local class fails forward-reference resolution.
    """
    from mcp.server.fastmcp import FastMCP

    app = FastMCP("synthetic")

    @app.tool(description="Use this when testing the lint. Use describe_table instead when not.")
    def bad_tool() -> _NotAnEnvelope:
        return _NotAnEnvelope(foo="bar")

    return app
