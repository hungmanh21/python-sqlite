"""Sargability tests: which WHERE conjuncts could ever seek an index,
independent of which indexes exist. Every case here traces to a named
failure or table row in chapter 12.
"""


from quilldb.plan.predicates import Predicate, classify_predicate, extract_conjuncts
from quilldb.sql.binder import BoundBinaryOp, BoundColumn, BoundIsNull, BoundLiteral, BoundUnaryOp

_A = BoundColumn(0, "a", None)  # data_type is irrelevant to sargability
_B = BoundColumn(1, "b", None)
_LIT1 = BoundLiteral(1)




def _eq(left, right):
    return BoundBinaryOp(left, "=", right)




# =====================================================================
# extract_conjuncts: AND splits, everything else stays whole
# =====================================================================




def test_no_where_yields_no_conjuncts():
    assert extract_conjuncts(None) == []




def test_bare_predicate_yields_itself():
    expr = _eq(_A, _LIT1)
    assert extract_conjuncts(expr) == [expr]




def test_and_splits_into_two_conjuncts():
    left, right = _eq(_A, _LIT1), _eq(_B, _LIT1)
    conjuncts = extract_conjuncts(BoundBinaryOp(left, "AND", right))
    assert conjuncts == [left, right]




def test_nested_and_flattens_fully():
    a, b, c = _eq(_A, _LIT1), _eq(_B, _LIT1), _eq(_A, BoundLiteral(2))
    nested = BoundBinaryOp(BoundBinaryOp(a, "AND", b), "AND", c)
    assert extract_conjuncts(nested) == [a, b, c]




def test_or_is_not_split_chapter12_section_2():
    """`a = 1 OR b = 2` comes back as ONE conjunct: dropping either OR
    branch loses rows, so OR must not be treated like AND (§12.2's
    asymmetry -- this is the property that makes AND planner-friendly).
    """
    expr = BoundBinaryOp(_eq(_A, _LIT1), "OR", _eq(_B, _LIT1))
    assert extract_conjuncts(expr) == [expr]




def test_and_of_or_keeps_the_or_conjunct_whole():
    or_expr = BoundBinaryOp(_eq(_A, _LIT1), "OR", _eq(_B, _LIT1))
    conjuncts = extract_conjuncts(BoundBinaryOp(or_expr, "AND", _eq(_B, _LIT1)))
    assert or_expr in conjuncts
    assert len(conjuncts) == 2




# =====================================================================
# classify_predicate: column = literal, both orderings, all comparisons
# =====================================================================




def test_column_equals_literal_is_sargable():
    expr = _eq(_A, _LIT1)
    predicate = classify_predicate(expr)
    assert predicate == Predicate("a", "=", _LIT1, expr)




def test_literal_equals_column_is_normalized_column_first():
    """`1 = a` means the same as `a = 1` -- classify_predicate must not
    require the SQL author to write the column on the left.
    """
    expr = _eq(_LIT1, _A)
    predicate = classify_predicate(expr)
    assert predicate == Predicate("a", "=", _LIT1, expr)




def test_flipped_inequality_swaps_the_operator_not_just_the_sides():
    """`5 > a` is `a < 5`, not `a > 5` -- the operator must flip with the
    sides, or the seek range would point the wrong way.
    """
    expr = BoundBinaryOp(BoundLiteral(5), ">", _A)
    predicate = classify_predicate(expr)
    assert predicate == Predicate("a", "<", BoundLiteral(5), expr)




def test_all_five_sargable_comparisons():
    for operator in ("=", "<", "<=", ">", ">="):
        expr = BoundBinaryOp(_A, operator, _LIT1)
        assert classify_predicate(expr) == Predicate("a", operator, _LIT1, expr)




def test_is_null_is_sargable():
    expr = BoundIsNull(_A, negated=False)
    assert classify_predicate(expr) == Predicate("a", "IS", BoundLiteral(None), expr)




def test_is_not_null_is_not_sargable():
    """Not in optoverview.html's list: there's no contiguous "everything
    except NULL" region to seek to, unlike a plain equality range.
    """
    assert classify_predicate(BoundIsNull(_A, negated=True)) is None




# =====================================================================
# Failure 2 (§12.1 / §12.2): sargability, not just "column appears"
# =====================================================================




def test_function_wrapped_column_is_not_sargable():
    """`lower(a) = 'x'` -- modeled here as a UnaryOp wrapping the column,
    standing in for any transform. The index is sorted by `a`, not by
    whatever the transform produces, so this must not classify.
    """
    expr = _eq(BoundUnaryOp("lower", _A), BoundLiteral("x"))
    assert classify_predicate(expr) is None




def test_arithmetic_on_column_is_not_sargable():
    """`age + 0 = 30` (chapter 12's own example): `age + 0` is a
    BoundBinaryOp, not a bare BoundColumn, so no seek is possible even
    though `age` appears and the query is logically equivalent to
    `age = 30`.
    """
    age = BoundColumn(0, "age", None)
    expr = _eq(BoundBinaryOp(age, "+", BoundLiteral(0)), BoundLiteral(30))
    assert classify_predicate(expr) is None




def test_column_compared_to_column_is_not_sargable():
    """Neither side is column-free, so there is no literal value to seek
    toward -- `WHERE a = b` can only be evaluated per row.
    """
    assert classify_predicate(_eq(_A, _B)) is None




def test_or_expression_is_not_sargable():
    expr = BoundBinaryOp(_eq(_A, _LIT1), "OR", _eq(_B, _LIT1))
    assert classify_predicate(expr) is None




def test_not_equal_is_not_sargable():
    """"!=" isn't in optoverview.html's documented list: excluding one
    value leaves two disjoint ranges, not one contiguous seek.
    """
    expr = BoundBinaryOp(_A, "!=", _LIT1)
    assert classify_predicate(expr) is None




# =====================================================================
# LIKE: sargable only for a literal prefix pattern (§12.2)
# =====================================================================




def test_like_with_trailing_wildcard_is_sargable():
    expr = BoundBinaryOp(_A, "LIKE", BoundLiteral("ada%"))
    predicate = classify_predicate(expr)
    assert predicate == Predicate("a", "LIKE", BoundLiteral("ada%"), expr)




def test_like_with_leading_wildcard_is_not_sargable():
    """A leading `%` has no shared prefix to descend to in the index --
    matches could be anywhere in the whole tree.
    """
    expr = BoundBinaryOp(_A, "LIKE", BoundLiteral("%ada"))
    assert classify_predicate(expr) is None




def test_like_against_a_parameterized_pattern_is_not_sargable():
    """The pattern must be a literal known at plan time -- if it's not
    even a string literal (modeled here by comparing to a column), there's
    no prefix to extract before execution.
    """
    expr = BoundBinaryOp(_A, "LIKE", _B)
    assert classify_predicate(expr) is None