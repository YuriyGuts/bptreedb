"""Expression AST and evaluator with SQL three-valued logic.

The planner translates `sqlglot` expression nodes into these classes once, after which
the evaluator runs without any further parsing. `evaluate` returns a `SQLValue` where
`None` represents SQL `NULL`. Boolean operators and comparisons follow Codd-style 3VL:
any operand that is `NULL` makes the result `NULL` unless logical short-circuiting
on `AND`/`OR` produces a definite answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from bptreedb.sql.errors import SQLTypeError
from bptreedb.sql.types import Row
from bptreedb.sql.types import SQLValue


class Expr:
    """Base class for the expression AST."""


@dataclass(frozen=True)
class Literal(Expr):
    """A constant SQL value."""

    value: SQLValue


@dataclass(frozen=True)
class ColumnRef(Expr):
    """A reference to a column in the current operator's input tuple, resolved to an index."""

    index: int


@dataclass(frozen=True)
class BinOp(Expr):
    """A binary operator: arithmetic, comparison, logical, or string concat."""

    op: str
    left: Expr
    right: Expr

    ARITHMETIC: ClassVar[frozenset[str]] = frozenset({"+", "-", "*", "/", "%"})
    COMPARISONS: ClassVar[frozenset[str]] = frozenset({"=", "<>", "<", "<=", ">", ">="})
    LOGICAL: ClassVar[frozenset[str]] = frozenset({"AND", "OR"})
    STRING: ClassVar[frozenset[str]] = frozenset({"||"})


@dataclass(frozen=True)
class UnaryOp(Expr):
    """A unary operator: NOT or unary minus."""

    op: str
    arg: Expr


@dataclass(frozen=True)
class IsNull(Expr):
    """`IS NULL` (negate=False) or `IS NOT NULL` (negate=True)."""

    arg: Expr
    negate: bool = False


@dataclass(frozen=True)
class Case(Expr):
    """`CASE WHEN ... THEN ... [ELSE ...] END`."""

    whens: tuple[tuple[Expr, Expr], ...]
    else_: Expr | None = None


@dataclass(frozen=True)
class FuncCall(Expr):
    """Scalar function call: `LOWER(x)`, `COALESCE(a, b)`, etc."""

    name: str
    args: tuple[Expr, ...]


@dataclass(frozen=True)
class Aggregate(Expr):
    """Aggregate placeholder. `COUNT(*)` has `arg=None`; `SUM(x)` etc. wrap an `Expr`."""

    name: str
    arg: Expr | None
    # Position in the `HashAggregate` operator's aggregate-output slots; set during planning.
    output_index: int = -1


def _is_truthy(value: SQLValue) -> bool | None:
    """Coerce a SQL value to a 2VL boolean for `Filter`/`HAVING`; returns `None` for NULL."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    # Numeric coercion mirrors SQLite: nonzero = true, zero = false. Strings are not
    # truthy by themselves in SQL — only an explicit boolean / comparison is.
    if isinstance(value, int | float):
        return value != 0
    raise SQLTypeError(f"cannot coerce {type(value).__name__} to boolean")


def evaluate(expr: Expr, row: Row) -> SQLValue:  # noqa: PLR0911
    """
    Evaluate `expr` against an input `row` and return a SQL value.

    Three-valued logic: any arithmetic/comparison/concat involving NULL yields NULL,
    except for the short-circuits in `AND`/`OR` (e.g. `NULL OR TRUE = TRUE`).
    """
    if isinstance(expr, Literal):
        return expr.value

    if isinstance(expr, ColumnRef):
        return row[expr.index]

    if isinstance(expr, BinOp):
        return _eval_binop(expr, row)

    if isinstance(expr, UnaryOp):
        return _eval_unary(expr, row)

    if isinstance(expr, IsNull):
        result = evaluate(expr.arg, row) is None
        return (not result) if expr.negate else result

    if isinstance(expr, Case):
        for cond, then in expr.whens:
            if _is_truthy(evaluate(cond, row)) is True:
                return evaluate(then, row)
        if expr.else_ is not None:
            return evaluate(expr.else_, row)
        return None

    if isinstance(expr, FuncCall):
        return _eval_function(expr, row)

    if isinstance(expr, Aggregate):
        # Aggregates are pre-computed by `HashAggregate` and surfaced as columns; if we get
        # here it means the planner forgot to rewrite them.
        raise SQLTypeError(f"aggregate {expr.name!r} not rewritten by planner")

    raise SQLTypeError(f"unsupported expression: {type(expr).__name__}")


def _eval_binop(expr: BinOp, row: Row) -> SQLValue:  # noqa: PLR0911, PLR0912
    op = expr.op

    # AND/OR have to short-circuit before evaluating the right side so the NULL-aware
    # truth table works: `FALSE AND NULL = FALSE`, `TRUE OR NULL = TRUE`.
    if op == "AND":
        left = _is_truthy(evaluate(expr.left, row))
        if left is False:
            return False
        right = _is_truthy(evaluate(expr.right, row))
        if right is False:
            return False
        if left is None or right is None:
            return None
        return True

    if op == "OR":
        left = _is_truthy(evaluate(expr.left, row))
        if left is True:
            return True
        right = _is_truthy(evaluate(expr.right, row))
        if right is True:
            return True
        if left is None or right is None:
            return None
        return False

    left_val = evaluate(expr.left, row)
    right_val = evaluate(expr.right, row)

    if left_val is None or right_val is None:
        return None

    if op in BinOp.ARITHMETIC:
        return _arithmetic(op, left_val, right_val)

    if op in BinOp.COMPARISONS:
        return _compare(op, left_val, right_val)

    if op == "||":
        if not isinstance(left_val, str) or not isinstance(right_val, str):
            left_name = type(left_val).__name__
            right_name = type(right_val).__name__
            raise SQLTypeError(f"|| expects TEXT operands, got {left_name} and {right_name}")
        return left_val + right_val

    raise SQLTypeError(f"unsupported binary operator: {op!r}")


def _arithmetic(op: str, left: SQLValue, right: SQLValue) -> SQLValue:  # noqa: PLR0911
    if isinstance(left, bool) or isinstance(right, bool):
        raise SQLTypeError("arithmetic on BOOLEAN is not allowed")
    if not isinstance(left, int | float) or not isinstance(right, int | float):
        left_name = type(left).__name__
        right_name = type(right).__name__
        raise SQLTypeError(
            f"arithmetic expects numeric operands, got {left_name} and {right_name}",
        )
    if op == "+":
        return left + right
    if op == "-":
        return left - right
    if op == "*":
        return left * right
    if op == "/":
        if right == 0:
            return None
        if isinstance(left, int) and isinstance(right, int):
            # SQL `/` is float division; truncating int division belongs in `%`/explicit casts.
            return left / right
        return left / right
    if op == "%":
        if right == 0:
            return None
        return left % right
    raise SQLTypeError(f"unknown arithmetic op {op!r}")


def _compare(op: str, left: SQLValue, right: SQLValue) -> bool:
    # Allow comparing int and float across types; reject mixing strings and numbers.
    left_is_num = isinstance(left, int | float) and not isinstance(left, bool)
    right_is_num = isinstance(right, int | float) and not isinstance(right, bool)
    if left_is_num and right_is_num:
        return _apply_comparison(op, left, right)
    if isinstance(left, bool) and isinstance(right, bool):
        return _apply_comparison(op, int(left), int(right))
    if type(left) is type(right):
        return _apply_comparison(op, left, right)
    raise SQLTypeError(
        f"cannot compare {type(left).__name__} and {type(right).__name__}",
    )


def _apply_comparison(op: str, left: SQLValue, right: SQLValue) -> bool:
    # `_compare` has already guarded that `left` and `right` are mutually orderable
    # (both numeric, or same type) — type checkers can't follow that across the call,
    # so we silence the unsupported-operator warnings here.
    if op == "=":
        return left == right
    if op == "<>":
        return left != right
    if op == "<":
        return left < right  # ty: ignore[unsupported-operator]
    if op == "<=":
        return left <= right  # ty: ignore[unsupported-operator]
    if op == ">":
        return left > right  # ty: ignore[unsupported-operator]
    if op == ">=":
        return left >= right  # ty: ignore[unsupported-operator]
    raise SQLTypeError(f"unknown comparison op {op!r}")


def _eval_unary(expr: UnaryOp, row: Row) -> SQLValue:
    value = evaluate(expr.arg, row)
    if expr.op == "NOT":
        truthy = _is_truthy(value)
        if truthy is None:
            return None
        return not truthy
    if expr.op == "-":
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise SQLTypeError(f"unary - expects numeric, got {type(value).__name__}")
        return -value
    raise SQLTypeError(f"unknown unary op {expr.op!r}")


def _eval_function(expr: FuncCall, row: Row) -> SQLValue:  # noqa: PLR0911
    name = expr.name.upper()

    if name == "COALESCE":
        for arg in expr.args:
            value = evaluate(arg, row)
            if value is not None:
                return value
        return None

    # Other functions propagate NULL.
    args = [evaluate(a, row) for a in expr.args]
    if any(a is None for a in args):
        return None

    if name == "LOWER":
        return _expect_text(args[0], "LOWER").lower()
    if name == "UPPER":
        return _expect_text(args[0], "UPPER").upper()
    if name == "LENGTH":
        return len(_expect_text(args[0], "LENGTH"))
    if name == "ABS":
        v = args[0]
        if isinstance(v, bool) or not isinstance(v, int | float):
            raise SQLTypeError(f"ABS expects numeric, got {type(v).__name__}")
        return abs(v)
    raise SQLTypeError(f"unsupported function: {expr.name!r}")


def _expect_text(value: SQLValue, fn: str) -> str:
    if not isinstance(value, str):
        raise SQLTypeError(f"{fn} expects TEXT, got {type(value).__name__}")
    return value
