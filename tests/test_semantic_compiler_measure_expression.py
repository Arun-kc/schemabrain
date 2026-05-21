"""Tests for the composite-measure-expression parser/renderer.

Schema Brain v0.3.0 supports only `agg(single_column)` measures. v0.4
adds composite expressions like `SUM(unit_price * quantity)` via a
strict whitelist grammar: identifier-shaped column names, integer +
float literals, unary `-`, binary `+ - * /`, and parens. Anything
outside the whitelist (function calls, comparisons, attribute access,
subscripts, boolean ops) is rejected at parse time so the emitter can
never see free-form text.

The parser uses Python's stdlib `ast.parse(mode='eval')` and walks the
result with a node-type whitelist. The renderer walks the same AST
and emits the SQL fragment with double-quoted column identifiers and
alias prefixes — the same quoting discipline the single-column emit
path already uses.
"""

from __future__ import annotations

import pytest

from schemabrain.semantic.compiler.measure_expression import (
    MalformedMeasureExpressionError,
    parse_measure_expression,
    render_measure_expression,
)

# ----- parser ---------------------------------------------------------------


class TestParseValidExpressions:
    """The whitelist must accept the canonical shapes operators reach for."""

    def test_bare_column_reference(self) -> None:
        parsed = parse_measure_expression("unit_price")
        assert parsed.expression == "unit_price"
        assert parsed.columns == frozenset({"unit_price"})

    def test_multiplication_of_two_columns(self) -> None:
        parsed = parse_measure_expression("unit_price * quantity")
        assert parsed.columns == frozenset({"unit_price", "quantity"})

    def test_subtraction_with_literal(self) -> None:
        parsed = parse_measure_expression("revenue - 100")
        assert parsed.columns == frozenset({"revenue"})

    def test_division_of_columns(self) -> None:
        parsed = parse_measure_expression("revenue / orders")
        assert parsed.columns == frozenset({"revenue", "orders"})

    def test_parenthesised_arithmetic(self) -> None:
        parsed = parse_measure_expression("(a + b) * c")
        assert parsed.columns == frozenset({"a", "b", "c"})

    def test_unary_negation(self) -> None:
        parsed = parse_measure_expression("-discount")
        assert parsed.columns == frozenset({"discount"})

    def test_float_literal(self) -> None:
        parsed = parse_measure_expression("rate * 0.01")
        assert parsed.columns == frozenset({"rate"})

    def test_negative_integer_literal(self) -> None:
        parsed = parse_measure_expression("price - -1")
        assert parsed.columns == frozenset({"price"})

    def test_columns_set_deduplicated(self) -> None:
        # If the same column appears multiple times the set carries it once.
        parsed = parse_measure_expression("a + a + a")
        assert parsed.columns == frozenset({"a"})


class TestParseRejectedExpressions:
    """Anything outside the whitelist must raise — no silent fallthrough
    to the emitter."""

    @pytest.mark.parametrize(
        "expr",
        [
            "abs(x)",  # function call
            "x.amount",  # attribute access
            "x[0]",  # subscript
            "x > 0",  # comparison
            "x and y",  # boolean op
            "x or y",
            "not x",
            "x if y else z",  # ternary
            "x % y",  # mod operator (not in whitelist)
            "x ** y",  # power (not in whitelist)
            "x // y",  # floor div (not in whitelist)
            "x & y",  # bitwise (not in whitelist)
            "x | y",
            "x ^ y",
            "lambda x: x",
        ],
    )
    def test_disallowed_node_rejected(self, expr: str) -> None:
        with pytest.raises(MalformedMeasureExpressionError):
            parse_measure_expression(expr)

    def test_syntax_error_rejected(self) -> None:
        with pytest.raises(MalformedMeasureExpressionError):
            parse_measure_expression("unit_price *")

    def test_empty_string_rejected(self) -> None:
        with pytest.raises(MalformedMeasureExpressionError):
            parse_measure_expression("")

    def test_string_literal_rejected(self) -> None:
        # A naked string literal could otherwise be smuggled into the
        # SQL — the parser must reject any non-numeric constant.
        with pytest.raises(MalformedMeasureExpressionError):
            parse_measure_expression("'hello'")

    def test_bool_literal_rejected(self) -> None:
        # `True` / `False` parse as `ast.Constant` with bool value; the
        # walker has to reject them explicitly so they don't masquerade
        # as numerics (Python `isinstance(True, int)` is True).
        with pytest.raises(MalformedMeasureExpressionError):
            parse_measure_expression("True")

    def test_invalid_identifier_in_column_position_rejected(self) -> None:
        # The Python parser would never produce `ast.Name('1foo')` —
        # that's a syntax error first. But identifiers like `True` /
        # `False` / `None` are keywords that parse to `ast.Constant`
        # or `ast.Name`; we want any column-position node that
        # references a Python builtin to be either harmless (because
        # we reject string constants) or caught here.
        with pytest.raises(MalformedMeasureExpressionError):
            parse_measure_expression("None")

    def test_dollar_sign_in_identifier_rejected(self) -> None:
        # Postgres unquoted identifiers permit `$` (and our `_IDENT_RE`
        # does too), but Python's `ast.parse` does not — `$` is not in
        # the Python identifier alphabet. For columns with `$` in the
        # name, operators must use the single-column `column=` field
        # instead of `expression=`. Documented as a known limitation.
        with pytest.raises(MalformedMeasureExpressionError):
            parse_measure_expression("row$amount * qty")


# ----- renderer -------------------------------------------------------------


class TestRender:
    """The renderer turns the parsed AST into a SQL fragment with the same
    double-quoting + alias-prefix discipline as the single-column path."""

    def test_render_bare_column(self) -> None:
        parsed = parse_measure_expression("revenue")
        out = render_measure_expression(parsed.ast_node, quoted_anchor_alias='"order"')
        assert out == '"order"."revenue"'

    def test_render_multiplication(self) -> None:
        parsed = parse_measure_expression("unit_price * quantity")
        out = render_measure_expression(parsed.ast_node, quoted_anchor_alias='"order_item"')
        assert out == '("order_item"."unit_price" * "order_item"."quantity")'

    def test_render_subtraction_with_literal(self) -> None:
        parsed = parse_measure_expression("revenue - 100")
        out = render_measure_expression(parsed.ast_node, quoted_anchor_alias='"order"')
        assert out == '("order"."revenue" - 100)'

    def test_render_parenthesised(self) -> None:
        parsed = parse_measure_expression("(a + b) * c")
        out = render_measure_expression(parsed.ast_node, quoted_anchor_alias='"t"')
        assert out == '(("t"."a" + "t"."b") * "t"."c")'

    def test_render_unary_negation(self) -> None:
        parsed = parse_measure_expression("-discount")
        out = render_measure_expression(parsed.ast_node, quoted_anchor_alias='"o"')
        assert out == '(-"o"."discount")'

    def test_render_float_literal(self) -> None:
        parsed = parse_measure_expression("rate * 0.01")
        out = render_measure_expression(parsed.ast_node, quoted_anchor_alias='"t"')
        assert out == '("t"."rate" * 0.01)'

    def test_render_keyword_column_quoted_safe(self) -> None:
        # Postgres reserved words like `order` are safe because the
        # renderer double-quotes everything. The parser doesn't
        # block them because the SQL layer can handle them.
        parsed = parse_measure_expression("order * line")
        out = render_measure_expression(parsed.ast_node, quoted_anchor_alias='"t"')
        assert out == '("t"."order" * "t"."line")'


class TestMalformedErrorShape:
    """The error carries structured fields the MCP layer can lift into
    the `recovery` envelope without re-parsing the message."""

    def test_error_carries_expression_and_reason(self) -> None:
        with pytest.raises(MalformedMeasureExpressionError) as ctx:
            parse_measure_expression("abs(x)")
        assert ctx.value.expression == "abs(x)"
        assert "Call" in ctx.value.reason or "not allowed" in ctx.value.reason

    def test_error_pickles_round_trip(self) -> None:
        # The error needs to round-trip through pickle so the audit
        # layer can serialise refusal events later (parallel to the
        # PR-6h.3 frozen-dataclass error pickling work).
        import pickle

        try:
            parse_measure_expression("abs(x)")
        except MalformedMeasureExpressionError as exc:
            data = pickle.dumps(exc)
            roundtripped = pickle.loads(data)
            assert roundtripped.expression == "abs(x)"
            assert roundtripped.reason == exc.reason
