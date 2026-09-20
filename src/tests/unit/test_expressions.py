"""Evaluator tests: SQL NULL semantics, arithmetic, comparisons, LIKE.


Exhaustive rather than spot-checked where the input space is small: the
AND/OR tables have nine cases each, so enumerating all nine costs nothing
and is the only way to catch the two cells that surprise people
(`FALSE AND NULL` is FALSE, `TRUE OR NULL` is TRUE). Hand-picked examples
are exactly how you'd miss them.


Arithmetic expectations are the values sqlite3 actually returns, not what
Python's operators return -- several differ, and the parametrized cases
below record which.
"""


import itertools

import pytest

from quilldb.codec.record import encode_record
from quilldb.errors import ColumnNotFoundError, TypeMismatchError
from quilldb.exec.expressions import evaluate, sql_like, where_passes
from quilldb.sql.ast import DataType
from quilldb.sql.binder import (
    BoundBinaryOp,
    BoundColumn,
    BoundExpression,
    BoundIsNull,
    BoundLiteral,
    BoundUnaryOp,
)


def _lit(value: object) -> BoundLiteral:
    return BoundLiteral(value)




def _binary(left: object, operator: str, right: object) -> BoundBinaryOp:
    return BoundBinaryOp(_lit(left), operator, _lit(right))




def _eval(expression: BoundExpression, row: tuple[object, ...] = ()) -> object:
    return evaluate(expression, row)




# =====================================================================
# Leaves
# =====================================================================




def test_literal_evaluates_to_itself() -> None:
    assert _eval(_lit(42)) == 42
    assert _eval(_lit("ada")) == "ada"
    assert _eval(_lit(None)) is None




def test_column_reads_its_index_from_the_row() -> None:
    column = BoundColumn(1, "name", DataType.TEXT)
    assert _eval(column, (1, "ada", 36)) == "ada"




def test_column_index_past_the_end_of_the_row_raises() -> None:
    column = BoundColumn(5, "ghost", DataType.TEXT)
    with pytest.raises(ColumnNotFoundError):
        _eval(column, (1, "ada"))




# =====================================================================
# Three-valued logic: the full AND / OR / NOT tables
# =====================================================================


_TRUTH_INPUTS = [1, 0, None]  # TRUE, FALSE, unknown


_AND_TABLE = {
    (1, 1): 1, (1, 0): 0, (1, None): None,
    (0, 1): 0, (0, 0): 0, (0, None): 0,      # FALSE overpowers unknown
    (None, 1): None, (None, 0): 0, (None, None): None,
}


_OR_TABLE = {
    (1, 1): 1, (1, 0): 1, (1, None): 1,      # TRUE overpowers unknown
    (0, 1): 1, (0, 0): 0, (0, None): None,
    (None, 1): 1, (None, 0): None, (None, None): None,
}




@pytest.mark.parametrize("left, right", list(itertools.product(_TRUTH_INPUTS, _TRUTH_INPUTS)))
def test_and_truth_table_is_exhaustive(left: int | None, right: int | None) -> None:
    assert _eval(_binary(left, "AND", right)) == _AND_TABLE[(left, right)]




@pytest.mark.parametrize("left, right", list(itertools.product(_TRUTH_INPUTS, _TRUTH_INPUTS)))
def test_or_truth_table_is_exhaustive(left: int | None, right: int | None) -> None:
    assert _eval(_binary(left, "OR", right)) == _OR_TABLE[(left, right)]




@pytest.mark.parametrize("operand, expected", [(1, 0), (0, 1), (None, None)])
def test_not_truth_table_is_exhaustive(operand: object, expected: object) -> None:
    assert _eval(BoundUnaryOp("NOT", _lit(operand))) == expected




def test_and_or_treat_any_nonzero_number_as_true() -> None:
    # SQL has no boolean type; `WHERE 7` is valid and true.
    assert _eval(_binary(7, "AND", 1)) == 1
    assert _eval(_binary(-3, "OR", 0)) == 1




def test_and_short_circuits_on_a_false_left_operand() -> None:
    """A decisive left operand means the right side is never evaluated, so
    an expression that would raise doesn't get the chance to abort a scan
    over rows that already failed the first test. `poison` is only poison
    here because this evaluator refuses text+int -- real sqlite3's affinity
    would coerce `'a'` to 0 and never raise, so this behavior is quilldb's
    own answer, not something observable in the reference implementation.
    """
    poison = BoundBinaryOp(_lit("a"), "+", _lit(1))
    assert _eval(BoundBinaryOp(_lit(0), "AND", poison)) == 0




def test_or_short_circuits_on_a_true_left_operand() -> None:
    poison = BoundBinaryOp(_lit("a"), "+", _lit(1))
    assert _eval(BoundBinaryOp(_lit(1), "OR", poison)) == 1




def test_and_still_evaluates_the_right_side_when_the_left_is_not_decisive() -> None:
    poison = BoundBinaryOp(_lit("a"), "+", _lit(1))
    with pytest.raises(TypeMismatchError):
        _eval(BoundBinaryOp(_lit(1), "AND", poison))
    with pytest.raises(TypeMismatchError):
        _eval(BoundBinaryOp(_lit(None), "AND", poison))




def test_or_still_evaluates_the_right_side_when_the_left_is_not_decisive() -> None:
    poison = BoundBinaryOp(_lit("a"), "+", _lit(1))
    with pytest.raises(TypeMismatchError):
        _eval(BoundBinaryOp(_lit(0), "OR", poison))
    with pytest.raises(TypeMismatchError):
        _eval(BoundBinaryOp(_lit(None), "OR", poison))




# =====================================================================
# NULL propagation through comparisons and arithmetic
# =====================================================================




@pytest.mark.parametrize("operator", ["=", "!=", "<", "<=", ">", ">="])
def test_every_comparison_with_null_is_null(operator: str) -> None:
    assert _eval(_binary(None, operator, 1)) is None
    assert _eval(_binary(1, operator, None)) is None




def test_null_equals_null_is_null_not_true() -> None:
    """The headline case: NULL means "unknown", so two unknowns are not
    known to be equal. This is why `IS NULL` has to exist.
    """
    assert _eval(_binary(None, "=", None)) is None




@pytest.mark.parametrize("operator", ["+", "-", "*", "/", "%"])
def test_every_arithmetic_operator_propagates_null(operator: str) -> None:
    assert _eval(_binary(None, operator, 1)) is None
    assert _eval(_binary(1, operator, None)) is None




def test_like_with_null_is_null() -> None:
    assert _eval(_binary(None, "LIKE", "a%")) is None
    assert _eval(_binary("abc", "LIKE", None)) is None




def test_unary_minus_on_null_is_null() -> None:
    assert _eval(BoundUnaryOp("-", _lit(None))) is None




# =====================================================================
# IS NULL: the one operator that never returns NULL
# =====================================================================




def test_is_null_is_never_null() -> None:
    assert _eval(BoundIsNull(_lit(None), False)) == 1
    assert _eval(BoundIsNull(_lit(1), False)) == 0




def test_is_not_null_is_never_null() -> None:
    assert _eval(BoundIsNull(_lit(None), True)) == 0
    assert _eval(BoundIsNull(_lit(1), True)) == 1




def test_where_null_equals_null_rejects_but_is_null_accepts() -> None:
    """Concretely why `= NULL` is a bug and `IS NULL` is the fix."""
    assert where_passes(_eval(_binary(None, "=", None))) is False
    assert where_passes(_eval(BoundIsNull(_lit(None), False))) is True




# =====================================================================
# Comparisons return integers, and order across storage classes
# =====================================================================




def test_comparisons_return_integers_not_bools() -> None:
    result = _eval(_binary(1, "=", 1))
    assert result == 1
    assert type(result) is int  # SQLite: typeof(1=1) is 'integer'




@pytest.mark.parametrize(
    "left, operator, right, expected",
    [
        (1, "=", 1, 1), (1, "=", 2, 0),
        (1, "!=", 2, 1), (1, "!=", 1, 0),
        (1, "<", 2, 1), (2, "<", 1, 0),
        (1, "<=", 1, 1), (2, "<=", 1, 0),
        (2, ">", 1, 1), (1, ">", 2, 0),
        (1, ">=", 1, 1), (1, ">=", 2, 0),
        (1, "<", 1.5, 1),          # int vs float compares numerically
        ("a", "<", "b", 1), ("a", "=", "a", 1),
        (b"\x00", "=", b"\x00", 1),
    ],
)
def test_comparisons(left: object, operator: str, right: object, expected: int) -> None:
    assert _eval(_binary(left, operator, right)) == expected




@pytest.mark.parametrize(
    "left, operator, right, expected",
    [
        # sqlite3-verified: numbers sort before text, text before blobs.
        (1, "=", "a", 0),
        (1, "<", "a", 1),
        ("a", "<", 1, 0),
        ("a", "<", b"\x00", 1),
    ],
)
def test_cross_class_comparison_matches_sqlite_ordering(
    left: object, operator: str, right: object, expected: int
) -> None:
    assert _eval(_binary(left, operator, right)) == expected




# =====================================================================
# Arithmetic: the values sqlite3 returns, not Python's
# =====================================================================




@pytest.mark.parametrize(
    "left, operator, right, expected",
    [
        (1, "+", 2, 3),
        (5, "-", 3, 2),
        (4, "*", 3, 12),
        # Integer division TRUNCATES TOWARD ZERO; Python's // would floor.
        (7, "/", 2, 3),
        (-7, "/", 2, -3),   # Python: -4
        (7, "/", -2, -3),   # Python: -4
        (-7, "/", -2, 3),
        (5.0, "/", 2, 2.5),
        # `%` takes the sign of the dividend; Python's % takes the divisor's.
        (-7, "%", 3, -1),   # Python: 2
        (7, "%", -3, 1),    # Python: -2
        (7, "%", 3, 1),
        (7.5, "%", 2, 1.0),  # casts to int, but result is real
    ],
)
def test_arithmetic_matches_sqlite(
    left: object, operator: str, right: object, expected: object
) -> None:
    assert _eval(_binary(left, operator, right)) == expected




def test_float_modulo_returns_a_real() -> None:
    result = _eval(_binary(7.5, "%", 2))
    assert result == 1.0
    assert isinstance(result, float)  # sqlite3: typeof(7.5%2) is 'real'




@pytest.mark.parametrize("operator", ["/", "%"])
def test_division_by_zero_is_null_not_an_exception(operator: str) -> None:
    assert _eval(_binary(1, operator, 0)) is None




def test_float_division_by_zero_is_also_null() -> None:
    assert _eval(_binary(1.0, "/", 0.0)) is None




@pytest.mark.parametrize("operand, expected", [(5, -5), (-5, 5), (2.5, -2.5)])
def test_unary_minus(operand: object, expected: object) -> None:
    assert _eval(BoundUnaryOp("-", _lit(operand))) == expected




# =====================================================================
# 64-bit overflow: SQL integers have a ceiling, Python's don't
# =====================================================================


_MAX_INT64 = 9223372036854775807
_MIN_INT64 = -9223372036854775808




@pytest.mark.parametrize(
    "left, operator, right",
    [
        (_MAX_INT64, "+", 1),
        (_MAX_INT64, "-", -1),
        (_MAX_INT64, "*", 2),
        (_MAX_INT64, "+", _MAX_INT64),
        (_MAX_INT64, "*", _MAX_INT64),
        (_MIN_INT64, "-", 1),
        (_MIN_INT64, "/", -1),   # the one overflowing division: |quotient| is MAX+1
    ],
)
def test_integer_overflow_becomes_a_real(left: int, operator: str, right: int) -> None:
    """sqlite3 computes integer arithmetic in 64 bits and falls back to REAL
    rather than erroring or wrapping. Verified bitwise-identical to sqlite3's
    own doubles, and `typeof` is 'real' on both sides.
    """
    result = _eval(_binary(left, operator, right))
    assert isinstance(result, float)




def test_unary_minus_overflows_only_for_the_most_negative_integer() -> None:
    # |MIN_INT64| is one past MAX_INT64, so this is the single input where
    # negation cannot stay an integer.
    assert _eval(BoundUnaryOp("-", _lit(_MIN_INT64))) == float(-_MIN_INT64)
    assert isinstance(_eval(BoundUnaryOp("-", _lit(_MIN_INT64))), float)
    # Everything short of it stays exact.
    assert _eval(BoundUnaryOp("-", _lit(_MAX_INT64))) == _MIN_INT64 + 1
    assert isinstance(_eval(BoundUnaryOp("-", _lit(_MAX_INT64))), int)




@pytest.mark.parametrize(
    "left, operator, right, expected",
    [
        (_MAX_INT64, "*", 0, 0),
        (_MAX_INT64, "-", 1, _MAX_INT64 - 1),
        (_MAX_INT64, "%", -1, 0),        # % is bounded by its divisor, never overflows
        (_MIN_INT64, "%", -1, 0),
        (_MIN_INT64, "+", _MAX_INT64, -1),
    ],
)
def test_results_that_still_fit_stay_integers(
    left: int, operator: str, right: int, expected: int
) -> None:
    result = _eval(_binary(left, operator, right))
    assert result == expected
    assert isinstance(result, int)




def test_an_overflowed_result_is_still_a_storable_value() -> None:
    """The point of converting rather than letting a Python bigint through:
    an out-of-range int is not a Value, and only surfaced later as a bare
    ValueError from inside the record encoder, aborting a scan sqlite3 would
    have finished.
    """
    result = _eval(_binary(_MAX_INT64, "+", 1))
    encode_record((result,))  # must not raise




def test_nested_unary_minus() -> None:
    assert _eval(BoundUnaryOp("-", BoundUnaryOp("-", _lit(5)))) == 5




def test_unary_plus_is_identity() -> None:
    assert _eval(BoundUnaryOp("+", _lit(5))) == 5




# =====================================================================
# Type errors: refuse rather than coerce (documented divergence)
# =====================================================================




@pytest.mark.parametrize(
    "left, operator, right",
    [
        (1, "+", "a"),        # sqlite3 gives 1 via affinity; we refuse
        ("a", "+", 1),
        (1, "*", b"blob"),
        (1, "-", True),       # bool is not a number here
        (True, "/", 1),
    ],
)
def test_arithmetic_with_non_numeric_operands_raises(
    left: object, operator: str, right: object
) -> None:
    with pytest.raises(TypeMismatchError):
        _eval(_binary(left, operator, right))




@pytest.mark.parametrize("left, right", [(1, "a%"), ("abc", 1), (b"x", "a%")])
def test_like_with_non_text_operands_raises(left: object, right: object) -> None:
    with pytest.raises(TypeMismatchError):
        _eval(_binary(left, "LIKE", right))




def test_unary_minus_on_text_raises() -> None:
    with pytest.raises(TypeMismatchError):
        _eval(BoundUnaryOp("-", _lit("abc")))




# =====================================================================
# LIKE
# =====================================================================




@pytest.mark.parametrize(
    "value, pattern, expected",
    [
        ("abc", "abc", True),
        ("abc", "a%", True),
        ("abc", "%c", True),
        ("abc", "%b%", True),
        ("abc", "a_c", True),
        ("abc", "a_", False),        # _ is exactly one, so a_ needs length 2
        ("ab", "a_", True),
        ("abc", "_bc", True),
        ("abc", "%", True),
        ("", "%", True),             # % matches zero characters
        ("", "", True),
        ("abc", "", False),
        ("", "_", False),
        ("abc", "abd", False),
        ("abc", "%d%", False),
        ("abcabc", "%c%c", True),    # requires backtracking
        ("aaa", "%a", True),
    ],
)
def test_sql_like_patterns(value: str, pattern: str, expected: bool) -> None:
    assert sql_like(value, pattern) is expected




@pytest.mark.parametrize(
    "value, pattern",
    [("ABC", "abc"), ("abc", "ABC"), ("AbC", "a_c"), ("ABC", "a%")],
)
def test_like_is_ascii_case_insensitive(value: str, pattern: str) -> None:
    """sqlite3: 'ABC' LIKE 'abc' is 1 -- LIKE's documented ASCII default."""
    assert sql_like(value, pattern) is True




def test_like_treats_regex_metacharacters_literally() -> None:
    """The reason this isn't a regex translation: sqlite3 returns 1 for
    `'.*' LIKE '.*'` and 0 for `'anything' LIKE '.*'`.
    """
    assert sql_like(".*", ".*") is True
    assert sql_like("anything", ".*") is False
    assert sql_like("(", "(") is True      # invalid regex, valid LIKE pattern
    assert sql_like("a+b", "a+b") is True




def test_like_does_not_blow_up_on_an_adversarial_pattern() -> None:
    """A naive recursive matcher goes exponential here; the DP table is
    O(n * m) and returns immediately.
    """
    assert sql_like("a" * 40, "%a%a%a%a%a%b") is False
    assert sql_like("a" * 40, "%a%a%a%a%a%a") is True




def test_like_through_evaluate_returns_an_integer() -> None:
    assert _eval(_binary("ada", "LIKE", "a%")) == 1
    assert _eval(_binary("bob", "LIKE", "a%")) == 0




# =====================================================================
# where_passes
# =====================================================================




@pytest.mark.parametrize(
    "value, expected",
    [
        (1, True), (0, False), (None, False),
        (7, True), (-3, True), (0.0, False), (2.5, True),
        ("abc", False),      # sqlite3 coerces non-numeric text to 0
        ("", False),
        (b"x", False),
    ],
)
def test_where_passes(value: object, expected: bool) -> None:
    assert where_passes(value) is expected




def test_a_predicate_and_its_negation_both_reject_a_null_row() -> None:
    """SQL's most surprising consequence, and it is correct: these two
    queries do not partition the table, because a NULL age makes both
    predicates NULL.
    """
    row = (1, "ada", None)
    age = BoundColumn(2, "age", DataType.INTEGER)
    predicate = BoundBinaryOp(age, ">", _lit(30))


    assert where_passes(_eval(predicate, row)) is False
    assert where_passes(_eval(BoundUnaryOp("NOT", predicate), row)) is False




# =====================================================================
# Composition: a realistic bound WHERE clause over a row
# =====================================================================




def test_a_compound_predicate_over_a_row() -> None:
    row = (1, "ada", 36)
    name = BoundColumn(1, "name", DataType.TEXT)
    age = BoundColumn(2, "age", DataType.INTEGER)


    predicate = BoundBinaryOp(
        BoundBinaryOp(age, ">=", _lit(30)),
        "AND",
        BoundBinaryOp(name, "LIKE", _lit("a%")),
    )
    assert where_passes(_eval(predicate, row)) is True


    assert where_passes(_eval(predicate, (2, "bob", 36))) is False
    assert where_passes(_eval(predicate, (3, "amy", 20))) is False




def test_projection_expression_over_a_row() -> None:
    row = (1, "ada", 36)
    age = BoundColumn(2, "age", DataType.INTEGER)
    assert _eval(BoundBinaryOp(age, "+", _lit(1)), row) == 37