"""Evaluate bound expressions using SQL NULL and boolean semantics.


Pure by construction: (expression, row) -> value, no pager, no cursor, no
state. That's what lets the whole NULL surface be tested exhaustively with
tuples instead of databases, and it's why Filter/Project (exec/operators.py)
stay trivial -- all the subtlety lives here, all the resource management
lives there.


SQL NULL IS NOT PYTHON None WITH PYTHON OPERATORS. NULL means "unknown", so
`NULL = NULL` is NULL, not True: two unknown values aren't known to be
equal. One rule generates every three-valued table below -- if the answer
is the same regardless of what the unknown turns out to be, you get a
definite answer, otherwise NULL. That's why `FALSE AND NULL` is FALSE
(false either way) while `TRUE AND NULL` is NULL.


Every constant below was verified against the sqlite3 CLI rather than
assumed, because several differ from Python's operators:


    -7 / 2   -> -3    Python's // floors to -4
    -7 % 3   -> -1    Python's % gives 2
    7.5 % 2  -> 1.0   % casts operands to int, result is real if either was
    1 / 0    -> NULL  Python raises ZeroDivisionError
    1 = 1    -> 1     an INTEGER, not a bool: SQL has no boolean type
    MAX + 1  -> 9.22337203685478e+18
                      integer arithmetic is 64-bit and falls back to REAL on
                      overflow; Python's ints are arbitrary precision and
                      would silently produce a value the file format cannot
                      store at all (see _fit_int64)


DELIBERATE DIVERGENCES from real SQLite, all in the same direction (refuse
rather than silently coerce), consistent with binder.py's declared-type
policy:


    1 + 'a'          SQLite gives 1 (text casts to 0); we raise
                     TypeMismatchError. Implementing affinity would turn
                     garbage into a number silently.
    WHERE '1'        SQLite treats it as true; we treat all text as false.


Comparisons do NOT diverge: SQLite's storage-class ordering
(numeric < text < blob) is ten lines and lossless, so `1 < 'a'` gives 1
here exactly as it does there. That's where fidelity is cheapest and
matters most, since comparisons are what WHERE clauses are made of.
"""


from typing import Any

from quilldb.codec.record import Value
from quilldb.errors import ColumnNotFoundError, TypeMismatchError, UnsupportedFeatureError
from quilldb.sql.binder import (
    BoundBinaryOp,
    BoundColumn,
    BoundExpression,
    BoundIsNull,
    BoundLiteral,
    BoundUnaryOp,
)

type Row = tuple[Value, ...]
type TruthValue = bool | None


# SQLite's storage-class sort order (fileformat/lang_expr): NULL sorts before
# everything, then numbers, then text, then blobs. Comparing across classes
# compares these ranks instead of the values.
_NUMERIC_RANK = 1
_TEXT_RANK = 2
_BLOB_RANK = 3


_ARITHMETIC = frozenset({"+", "-", "*", "/", "%"})
_COMPARISONS = frozenset({"=", "!=", "<", "<=", ">", ">="})


# SQL integers are 64-bit; Python's are not. Every integer result below is
# funnelled through _fit_int64 so that difference can't leak out of here.
_MIN_INT64 = -(2**63)
_MAX_INT64 = 2**63 - 1




def _fit_int64(value: float) -> Value:
    """Return `value`, as a float if it's an integer too large to store.


    (`float` here covers int too, via PEP 484's numeric tower -- same
    convention as _divide/_modulo below. The isinstance check is what
    actually distinguishes them.)


    SQLite does integer arithmetic in 64 bits and falls back to REAL when a
    result doesn't fit, so `MAX + 1` is 9.22337203685478e+18 rather than an
    error or a wrapped negative. Python has no such ceiling, which makes this
    the one place the evaluator can produce a value that is *not a Value* --
    an int outside the range serial_type_for() is able to encode.


    Without this, the overflow surfaces nowhere near its cause: evaluate()
    happily returns 9223372036854775808, and the failure appears later as a
    bare ValueError from inside the record encoder, aborting a scan that
    SQLite would have finished. Converting here keeps every result storable
    and matches the reference, at the cost of precision -- which is exactly
    the trade SQLite already made.
    """
    if isinstance(value, int) and not _MIN_INT64 <= value <= _MAX_INT64:
        return float(value)
    return value




def evaluate(expression: BoundExpression, row: Row) -> Value:
    """Evaluate one expression against one row.


    Args:
        expression: a bound expression -- every column already an index,
            every parameter already a value (see sql/binder.py).
        row: the current row's column values, positionally.
    Returns:
        The result. Python None represents SQL NULL. Comparisons and
        boolean operators return the INTEGERS 1 and 0, not bools, matching
        SQLite's lack of a boolean type.
    Raises:
        TypeMismatchError: an operator received incompatible non-NULL
            operands (see this module's divergence notes).
        ColumnNotFoundError: the row is shorter than a BoundColumn's index
            -- a binder/executor disagreement, not a user error.
        UnsupportedFeatureError: an unrecognized operator or node type.
    """
    if isinstance(expression, BoundLiteral):
        return expression.value


    if isinstance(expression, BoundColumn):
        if expression.index >= len(row):
            raise ColumnNotFoundError(
                f"column {expression.name!r} is at index {expression.index} "
                f"but the row has only {len(row)} values"
            )
        return row[expression.index]


    if isinstance(expression, BoundIsNull):
        # The one operator that can never return NULL: "is this value the
        # unknown one?" is always decidable, which is exactly why SQL needs
        # `IS NULL` as separate syntax from `= NULL`.
        is_null = evaluate(expression.operand, row) is None
        return int(is_null != expression.negated)


    if isinstance(expression, BoundUnaryOp):
        return _unary(expression, row)


    if isinstance(expression, BoundBinaryOp):
        return _binary(expression, row)


    raise UnsupportedFeatureError(f"cannot evaluate a {type(expression).__name__}")




def where_passes(value: Value) -> bool:
    """WHERE keeps only TRUE. FALSE and NULL both reject the row.


    Numeric-but-not-1 counts as true, because SQL has no boolean type and
    `WHERE 1` is valid SQL matching every row. Text and blobs are false --
    SQLite would coerce non-numeric text to 0, which agrees; a numeric
    string like '1' is the documented divergence.


    This is where three-valued logic collapses back to two, and the reason
    `WHERE age > 30` and `WHERE NOT (age > 30)` together do NOT return every
    row: a NULL age makes both predicates NULL, so the row is rejected
    twice.
    """
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return False




def sql_like(value: str, pattern: str) -> bool:
    """Match `value` against a LIKE `pattern`: `%` is zero or more
    characters, `_` is exactly one.


    NOT implemented by translating to a regular expression, for two
    independent reasons:


    1. Correctness. The pattern is user SQL. `LIKE '.*'` must match the
       literal two-character string ".*" (verified: sqlite3 returns 0 for
       `'anything' LIKE '.*'`), but interpolated into a regex it matches
       everything. `LIKE '('` is a valid SQL pattern and an invalid regex.
    2. Complexity. `%` needs backtracking, and a naive recursive matcher
       goes exponential -- `'%a%a%a%a%a%b'` against thirty a's hangs. The
       table below is O(len(value) * len(pattern)) with no backtracking.


    Matching is case-insensitive for ASCII, which is SQLite's documented
    default ('ABC' LIKE 'abc' is 1). Non-ASCII characters compare exactly,
    also matching SQLite -- its built-in LIKE doesn't know Unicode case.
    """
    # matched[j] answers "does the value prefix consumed so far match the
    # first j characters of the pattern?" -- one row of the DP table, rolled
    # forward one value character at a time.
    matched = [False] * (len(pattern) + 1)
    matched[0] = True  # empty value matches empty pattern
    for j in range(1, len(pattern) + 1):
        # An all-`%` pattern prefix can still match an empty value.
        matched[j] = matched[j - 1] and pattern[j - 1] == "%"


    for value_char in value:
        nxt = [False] * (len(pattern) + 1)
        # nxt[0] stays False: a non-empty value never matches an empty pattern.
        for j in range(1, len(pattern) + 1):
            pattern_char = pattern[j - 1]
            if pattern_char == "%":
                # Either `%` absorbs this character (nxt[j - 1] having already
                # matched it) or it matched nothing and we carry the previous row.
                nxt[j] = nxt[j - 1] or matched[j]
            elif pattern_char == "_":
                nxt[j] = matched[j - 1]
            else:
                nxt[j] = matched[j - 1] and _like_chars_equal(pattern_char, value_char)
        matched = nxt


    return matched[len(pattern)]




def _like_chars_equal(a: str, b: str) -> bool:
    """ASCII-case-insensitive character equality, SQLite's LIKE default."""
    if a == b:
        return True
    return a.isascii() and b.isascii() and a.lower() == b.lower()




def _unary(expression: BoundUnaryOp, row: Row) -> Value:
    operand = evaluate(expression.operand, row)


    if expression.operator == "NOT":
        truth = _truth(operand)
        return None if truth is None else int(not truth)


    if expression.operator in ("+", "-"):
        if operand is None:
            return None
        number = _require_number(operand, f"unary {expression.operator!r}")
        # Negation overflows for MIN_INT64 alone, whose magnitude is one past
        # MAX_INT64; unary + can never change a value's representability.
        return number if expression.operator == "+" else _fit_int64(-number)


    raise UnsupportedFeatureError(f"unknown unary operator {expression.operator!r}")




def _binary(expression: BoundBinaryOp, row: Row) -> Value:
    operator = expression.operator


    # AND/OR are handled before the NULL short-circuit below, because they
    # are the two operators where a definite operand can outvote an unknown
    # one: `FALSE AND NULL` is FALSE, `TRUE OR NULL` is TRUE.
    #
    # They also SHORT-CIRCUIT, which is not just an optimisation here. A
    # decisive left operand means the right side is never evaluated, so
    # `WHERE age > 150 AND <expression that would raise>` returns FALSE for
    # the rows that fail the first test rather than aborting the scan. This
    # can't be observed the same way in real sqlite3 -- its type affinity
    # coerces text operands (`'a' + 1` is 1, not an error), so nothing on
    # the right side ever raises for sqlite3 to skip. What IS verified
    # against sqlite3 is the truth table itself (all nine AND/OR cells);
    # the short-circuit is this evaluator's own answer to a case sqlite3's
    # semantics never put to the test.
    if operator == "AND":
        left = _truth(evaluate(expression.left, row))
        if left is False:
            return 0
        right = _truth(evaluate(expression.right, row))
        if right is False:
            return 0
        return None if left is None or right is None else 1


    if operator == "OR":
        left = _truth(evaluate(expression.left, row))
        if left is True:
            return 1
        right = _truth(evaluate(expression.right, row))
        if right is True:
            return 1
        return None if left is None or right is None else 0


    left_value = evaluate(expression.left, row)
    right_value = evaluate(expression.right, row)


    # Everything else propagates NULL unconditionally: an unknown operand
    # makes the result unknown.
    if left_value is None or right_value is None:
        return None


    if operator == "LIKE":
        if not isinstance(left_value, str) or not isinstance(right_value, str):
            raise TypeMismatchError("LIKE needs text on both sides")
        return int(sql_like(left_value, right_value))


    if operator in _COMPARISONS:
        return int(_compare(operator, left_value, right_value))


    if operator in _ARITHMETIC:
        return _arithmetic(operator, left_value, right_value)


    raise UnsupportedFeatureError(f"unknown binary operator {operator!r}")




def _truth(value: Value) -> TruthValue:
    """Collapse a value to TRUE / FALSE / unknown for boolean context.


    Returns None for SQL NULL -- which is what makes the three-valued
    tables in _binary() readable as "unknown" rather than as a magic
    sentinel.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value != 0
    return False  # text/blob: see where_passes' divergence note




def _require_number(value: Value, context: str) -> int | float:
    """A non-NULL numeric operand, or TypeMismatchError.


    bool is refused alongside text and blobs: SQLite has no boolean storage
    class, so `True` arriving here means a caller confused a flag for a
    number (binder.py refuses it at the same boundary for the same reason).
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeMismatchError(f"{context} needs a number, got {type(value).__name__}")
    return value




def _storage_rank(value: Value) -> int:
    if isinstance(value, (int, float)):
        return _NUMERIC_RANK
    if isinstance(value, str):
        return _TEXT_RANK
    return _BLOB_RANK




def _compare(operator: str, left: Value, right: Value) -> bool:
    """SQLite's cross-class comparison: numbers < text < blobs.


    Within a class the values compare directly; across classes only the
    ranks do, so `1 < 'a'` is true without either operand being coerced.
    """
    left_rank, right_rank = _storage_rank(left), _storage_rank(right)
    if left_rank != right_rank:
        return _apply_ordering(operator, left_rank, right_rank)


    # Equal ranks mean equal storage classes, so the values are mutually
    # comparable -- narrowed explicitly so the ordering helper never has to
    # compare a str against an int.
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return _apply_ordering(operator, left, right)
    if isinstance(left, str) and isinstance(right, str):
        return _apply_ordering(operator, left, right)
    if isinstance(left, bytes) and isinstance(right, bytes):
        return _apply_ordering(operator, left, right)


    raise TypeMismatchError(
        f"cannot compare {type(left).__name__} with {type(right).__name__}"
    )




def _apply_ordering(operator: str, a: Any, b: Any) -> bool:
    """Apply one comparison operator to two already-proven-comparable values.


    Any is deliberate and contained: every caller above has narrowed both
    operands to the same storage class, so re-expressing that fact in the
    type system here would cost a generic protocol for no added safety.
    """
    if operator == "=":
        return bool(a == b)
    if operator == "!=":
        return bool(a != b)
    if operator == "<":
        return bool(a < b)
    if operator == "<=":
        return bool(a <= b)
    if operator == ">":
        return bool(a > b)
    if operator == ">=":
        return bool(a >= b)
    raise UnsupportedFeatureError(f"unknown comparison operator {operator!r}")




def _arithmetic(operator: str, left: Value, right: Value) -> Value:
    a = _require_number(left, f"operator {operator!r}")
    b = _require_number(right, f"operator {operator!r}")


    if operator == "+":
        return _fit_int64(a + b)
    if operator == "-":
        return _fit_int64(a - b)
    if operator == "*":
        return _fit_int64(a * b)
    if operator == "/":
        return _divide(a, b)
    return _modulo(a, b)




def _divide(a: float, b: float) -> Value:
    """Division by zero is NULL, not an exception -- verified against
    sqlite3, and letting ZeroDivisionError escape would abort a whole scan
    that SQLite would have finished.


    INTEGER operands divide with truncation TOWARD ZERO (-7 / 2 is -3),
    which is not what Python's `//` does (it floors to -4); float operands
    use ordinary float division. The annotations say `float` because PEP
    484's numeric tower makes that include int -- the isinstance check
    below is what actually distinguishes the two cases.


    Integer division overflows in exactly one place: MIN_INT64 / -1, whose
    true quotient is one past MAX_INT64.
    """
    if b == 0:
        return None
    if isinstance(a, int) and isinstance(b, int):
        magnitude = abs(a) // abs(b)
        return _fit_int64(-magnitude if (a < 0) != (b < 0) else magnitude)
    return a / b




def _modulo(a: float, b: float) -> Value:
    """`%` casts both operands to integers, takes the sign of the DIVIDEND
    (-7 % 3 is -1, where Python gives 2), and yields a real if either
    operand was real (7.5 % 2 is 1.0). All three verified against sqlite3.
    """
    int_a, int_b = int(a), int(b)  # int() truncates toward zero, like a C cast
    if int_b == 0:
        return None
    remainder = abs(int_a) % abs(int_b)
    if int_a < 0:
        remainder = -remainder
    if isinstance(a, float) or isinstance(b, float):
        return float(remainder)
    return remainder