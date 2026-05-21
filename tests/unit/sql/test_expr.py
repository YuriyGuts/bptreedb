import pytest

from bptreedb.sql.expr import BinOp
from bptreedb.sql.expr import Case
from bptreedb.sql.expr import ColumnRef
from bptreedb.sql.expr import FuncCall
from bptreedb.sql.expr import IsNull
from bptreedb.sql.expr import Literal
from bptreedb.sql.expr import UnaryOp
from bptreedb.sql.expr import evaluate


def _lit(v):
    return Literal(v)


# `(left, right) -> expected` for 3VL truth tables. None = SQL NULL.
_AND_TABLE = [
    ((True, True), True),
    ((True, False), False),
    ((False, False), False),
    ((True, None), None),
    ((None, True), None),
    ((False, None), False),
    ((None, False), False),
    ((None, None), None),
]
_OR_TABLE = [
    ((True, True), True),
    ((True, False), True),
    ((False, False), False),
    ((True, None), True),
    ((None, True), True),
    ((False, None), None),
    ((None, False), None),
    ((None, None), None),
]


@pytest.mark.parametrize(("operands", "expected"), _AND_TABLE)
def test_three_valued_and(operands, expected):
    left, right = operands
    assert evaluate(BinOp("AND", _lit(left), _lit(right)), ()) == expected


@pytest.mark.parametrize(("operands", "expected"), _OR_TABLE)
def test_three_valued_or(operands, expected):
    left, right = operands
    assert evaluate(BinOp("OR", _lit(left), _lit(right)), ()) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, False),
        (False, True),
        (None, None),
    ],
)
def test_not(value, expected):
    assert evaluate(UnaryOp("NOT", _lit(value)), ()) == expected


def test_comparisons_with_null_yield_null():
    # Any comparison with NULL is NULL.
    assert evaluate(BinOp("=", _lit(5), _lit(None)), ()) is None
    assert evaluate(BinOp("<", _lit(None), _lit(5)), ()) is None


def test_arithmetic_propagates_null():
    assert evaluate(BinOp("+", _lit(None), _lit(2)), ()) is None
    assert evaluate(BinOp("*", _lit(2), _lit(None)), ()) is None


def test_division_by_zero_is_null():
    # SQL: anything / 0 is NULL, not an error.
    assert evaluate(BinOp("/", _lit(5), _lit(0)), ()) is None
    assert evaluate(BinOp("%", _lit(5), _lit(0)), ()) is None


def test_division_uses_float_semantics():
    assert evaluate(BinOp("/", _lit(5), _lit(2)), ()) == 2.5


def test_is_null():
    assert evaluate(IsNull(_lit(None)), ()) is True
    assert evaluate(IsNull(_lit(5)), ()) is False
    assert evaluate(IsNull(_lit(None), negate=True), ()) is False
    assert evaluate(IsNull(_lit(5), negate=True), ()) is True


def test_case_picks_first_true_branch():
    expr = Case(
        whens=(
            (BinOp("=", _lit(1), _lit(2)), _lit("a")),  # false
            (BinOp("=", _lit(2), _lit(2)), _lit("b")),  # true
            (_lit(True), _lit("c")),  # never reached
        ),
        else_=_lit("else"),
    )
    assert evaluate(expr, ()) == "b"


def test_case_returns_else_when_no_branch_matches():
    expr = Case(whens=((_lit(False), _lit("a")),), else_=_lit("fallback"))
    assert evaluate(expr, ()) == "fallback"


def test_case_returns_null_when_no_match_and_no_else():
    expr = Case(whens=((_lit(False), _lit("a")),))
    assert evaluate(expr, ()) is None


def test_coalesce_picks_first_non_null():
    expr = FuncCall("COALESCE", (_lit(None), _lit(None), _lit("x"), _lit("y")))
    assert evaluate(expr, ()) == "x"


def test_coalesce_returns_null_when_all_null():
    expr = FuncCall("COALESCE", (_lit(None), _lit(None)))
    assert evaluate(expr, ()) is None


def test_string_concat():
    assert evaluate(BinOp("||", _lit("a"), _lit("b")), ()) == "ab"
    # NULL propagates.
    assert evaluate(BinOp("||", _lit("a"), _lit(None)), ()) is None


def test_column_ref_reads_from_input_row():
    assert evaluate(ColumnRef(2), (10, 20, 30, 40)) == 30


def test_filter_semantics_only_true_passes():
    # GIVEN three rows
    rows = [(1,), (2,), (3,)]
    # AND a predicate `col > 1`
    pred = BinOp(">", ColumnRef(0), _lit(1))
    # WHEN we evaluate it on each row
    passing = [r for r in rows if evaluate(pred, r) is True]
    # THEN only rows where the predicate is TRUE pass (NULL and FALSE drop).
    assert passing == [(2,), (3,)]
