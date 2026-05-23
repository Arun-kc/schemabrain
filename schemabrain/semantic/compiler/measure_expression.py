"""Composite-measure expression parser + SQL renderer.

v1 of the semantic layer accepted only `agg(single_column)` measures.
v2 adds composite arithmetic — `SUM(unit_price * quantity)` — via a
strict whitelist grammar parsed with Python's stdlib `ast` module.

The whitelist:
  - Column identifiers (`unit_price`, `qty`, `row$amount`) — same
    alphabet `_IDENT_RE` allows elsewhere in the codebase.
  - Integer literals (`1`, `100`).
  - Float literals (`0.01`, `1.5`).
  - Unary minus (`-discount`).
  - Binary `+`, `-`, `*`, `/`.
  - Parentheses for grouping (free — `ast.parse` handles them).

Everything else — function calls, attribute access, subscripts,
comparisons, boolean ops, ternaries, bool/string/None constants — is
rejected at parse time with `MalformedMeasureExpressionError`. The
emitter never sees free-form text; it walks the parsed AST and renders
each node with the same double-quoting + alias-prefix discipline as
the single-column emit path.

Security posture: the SQL injection surface is closed by construction.
The only strings that reach the emitter are identifier names already
validated against `_IDENT_RE`, which is the same gate `MetricMeasure.column`
has used since v1. Numeric literals render as their Python `str(int)` or
`repr(float)` — never as raw user-supplied text.

The parser is pure and side-effect-free. The returned
`ParsedMeasureExpression` carries the raw expression string (for
round-trip + storage), the parsed AST node (for rendering), and the
sorted column set (for PII propagation + column validation).
"""

from __future__ import annotations

import ast
import math
import re
from dataclasses import dataclass

# Same alphabet as `_IDENT_RE` in core/metric.py — identifiers must
# satisfy the Postgres unquoted-identifier shape. Reproduced here
# rather than imported so `core/metric.py` can import this module
# without a cycle.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


class MalformedMeasureExpressionError(ValueError):
    """Raised when a measure expression fails the whitelist check.

    Subclass of `ValueError` so callers that just want to catch "bad
    input" can do so without importing this module. The MCP envelope
    layer catches the specific class to lift `expression` + `reason`
    into the structured `recovery` payload.

    `expression` is the original source string (verbatim, even if the
    reason is a syntax error). `reason` is a single human-readable
    sentence naming the disallowed construct.
    """

    def __init__(self, expression: str, reason: str) -> None:
        self.expression = expression
        self.reason = reason
        super().__init__(f"malformed measure expression {expression!r}: {reason}")

    def __reduce__(self) -> tuple[type, tuple[str, str]]:
        # The default `Exception.__reduce__` pickles via `args` only,
        # which would render the round-trip exception unable to expose
        # `.expression` / `.reason` for structured re-handling. Explicit
        # reducer preserves the dataclass-style fields. Mirrors the
        # convention used by frozen-dataclass errors in
        # `semantic/compiler/plan.py`.
        return (self.__class__, (self.expression, self.reason))


@dataclass(frozen=True)
class ParsedMeasureExpression:
    """The result of parsing a composite measure expression.

    `expression` is the verbatim source string (for storage round-trip).
    `ast_node` is the parsed AST node — the emitter walks this to
    produce the SQL fragment. `columns` is the sorted-deduped set of
    column references found during parsing (used for PII propagation
    + compile-time column validation against the anchor table).
    """

    expression: str
    ast_node: ast.expr
    columns: frozenset[str]


# Node types the walker permits. Each branch corresponds to a SQL
# construct the renderer knows how to emit.
_ALLOWED_BINOPS: tuple[type[ast.operator], ...] = (
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
)


def parse_measure_expression(expression: str) -> ParsedMeasureExpression:
    """Parse `expression` and return a `ParsedMeasureExpression` or raise.

    The parser is strict by design — any construct outside the whitelist
    (function calls, subscripts, comparisons, boolean ops, etc.) raises
    `MalformedMeasureExpressionError` with a structured reason. The
    emitter relies on this contract: it never sees a node type it
    can't render.

    Two invariants enforced beyond the node-type whitelist:
      - Numeric literals must be finite (no `inf` / `-inf` / `nan`).
        Python's parser silently overflows `1e500` to `float('inf')`;
        without this guard `repr(inf)` would render the bare token
        `inf` into the emitted SQL, which Postgres can't interpret as
        a numeric and would surface as `internal_error` at execute time.
      - The expression must reference at least one column. A literal-
        only expression like `100` or `1 + 2` is semantically incoherent
        as a measure (would compile to `agg(100)` returning a constant
        per group) and is most likely a typo — reject it here so the
        operator gets a clear error rather than constant-value results.
    """
    if not expression or not expression.strip():
        raise MalformedMeasureExpressionError(expression, "expression is empty")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise MalformedMeasureExpressionError(expression, f"syntax error: {exc.msg}") from exc

    columns: set[str] = set()
    _walk(tree.body, expression, columns)
    if not columns:
        raise MalformedMeasureExpressionError(
            expression,
            "expression must reference at least one column "
            "(numeric-literal-only expressions are not a meaningful measure)",
        )
    return ParsedMeasureExpression(
        expression=expression,
        ast_node=tree.body,
        columns=frozenset(columns),
    )


def _walk(node: ast.expr, source: str, columns: set[str]) -> None:
    if isinstance(node, ast.Name):
        if not _IDENT_RE.fullmatch(node.id):  # pragma: no cover — defensive
            # Defence-in-depth: the Python parser already rejects every
            # non-identifier shape as `SyntaxError` upstream (verified by
            # `test_dollar_sign_in_identifier_rejected` + the syntax-error
            # parametrize). The branch is kept so a forward-compat parser
            # change can't silently let `1$foo` through, but it has no
            # reachable test path today.
            raise MalformedMeasureExpressionError(source, f"invalid column identifier {node.id!r}")
        columns.add(node.id)
        return
    if isinstance(node, ast.Constant):
        value = node.value
        # `bool` is a subclass of `int` in Python; check bool BEFORE int
        # so `True` / `False` are rejected as non-numeric literals.
        if isinstance(value, bool):
            raise MalformedMeasureExpressionError(
                source, f"boolean literal {value!r} is not allowed"
            )
        if isinstance(value, (int, float)):
            # `ast.parse` silently overflows large float literals to
            # `float('inf')` and similar arithmetic yields `nan`. The
            # renderer would emit `repr(inf)` / `repr(nan)` as the bare
            # tokens `inf` / `nan` into the SQL stream, which Postgres
            # can't parse as a numeric literal. Reject here so the
            # operator gets a clean `MalformedMeasureExpressionError`
            # instead of an execute-time `internal_error`.
            if isinstance(value, float) and not math.isfinite(value):
                raise MalformedMeasureExpressionError(
                    source,
                    f"non-finite float literal {value!r} is not allowed "
                    f"(numeric literals must be finite)",
                )
            return
        raise MalformedMeasureExpressionError(
            source, f"literal must be numeric (got {type(value).__name__}: {value!r})"
        )
    if isinstance(node, ast.BinOp):
        if not isinstance(node.op, _ALLOWED_BINOPS):
            raise MalformedMeasureExpressionError(
                source,
                f"operator {type(node.op).__name__} is not allowed (allowed: +, -, *, /)",
            )
        _walk(node.left, source, columns)
        _walk(node.right, source, columns)
        return
    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, ast.USub):
            raise MalformedMeasureExpressionError(
                source,
                f"unary operator {type(node.op).__name__} is not allowed "
                f"(only unary `-` is permitted)",
            )
        _walk(node.operand, source, columns)
        return
    raise MalformedMeasureExpressionError(source, f"node type {type(node).__name__} is not allowed")


# Postgres operator vocabulary for each whitelisted BinOp. The keys
# are the AST node TYPES (not instances) so the lookup is O(1).
_BINOP_SQL: dict[type[ast.operator], str] = {
    ast.Add: "+",
    ast.Sub: "-",
    ast.Mult: "*",
    ast.Div: "/",
}


def render_measure_expression(node: ast.expr, *, quoted_anchor_alias: str) -> str:
    """Render a parsed AST into a SQL fragment.

    `quoted_anchor_alias` is the already-quoted SQL alias for the
    metric's anchor entity (e.g. `'"order"'`). Every column reference
    renders as `{quoted_anchor_alias}."<column>"` — same shape the
    single-column emit path uses.

    Binary expressions render with explicit parens around the whole
    sub-expression so operator-precedence is unambiguous at every
    nesting level. Numeric literals render via Python's `str(int)` or
    `repr(float)` — no user-supplied text reaches the SQL.
    """
    if isinstance(node, ast.Name):
        return f'{quoted_anchor_alias}."{node.id}"'
    if isinstance(node, ast.Constant):
        value = node.value
        if isinstance(value, bool):  # pragma: no cover — defensive
            # The parser rejects bool constants; we should never reach
            # here. Belt-and-braces in case a future caller renders an
            # AST node it constructed directly without going through
            # `parse_measure_expression`.
            raise RuntimeError("bool literal reached renderer (parser bypassed)")
        if isinstance(value, int):
            return str(value)
        if isinstance(value, float):
            return repr(value)
        raise RuntimeError(  # pragma: no cover — defensive
            f"non-numeric literal {value!r} reached renderer (parser bypassed)"
        )
    if isinstance(node, ast.BinOp):
        op_sql = _BINOP_SQL.get(type(node.op))
        if op_sql is None:  # pragma: no cover — defensive
            raise RuntimeError(
                f"operator {type(node.op).__name__} reached renderer (parser bypassed)"
            )
        left = render_measure_expression(node.left, quoted_anchor_alias=quoted_anchor_alias)
        right = render_measure_expression(node.right, quoted_anchor_alias=quoted_anchor_alias)
        return f"({left} {op_sql} {right})"
    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, ast.USub):  # pragma: no cover — defensive
            raise RuntimeError(f"unary {type(node.op).__name__} reached renderer (parser bypassed)")
        operand = render_measure_expression(node.operand, quoted_anchor_alias=quoted_anchor_alias)
        return f"(-{operand})"
    raise RuntimeError(  # pragma: no cover — defensive
        f"node type {type(node).__name__} reached renderer (parser bypassed)"
    )


__all__ = [
    "MalformedMeasureExpressionError",
    "ParsedMeasureExpression",
    "parse_measure_expression",
    "render_measure_expression",
]
