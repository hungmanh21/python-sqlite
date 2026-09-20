"""Sargability: deciding which WHERE conjuncts can seek an index at all.


This module answers Failure 2 from chapter 12 -- "is this predicate shaped
like a search argument?" -- and nothing past it. Whether a sargable
predicate actually gets *used* by some index (Failure 1, the leading-column
rule) is planner.py's job; this module only decides, per conjunct in
isolation, whether a seek could ever be built from it.


Two mechanical steps get you the whole thing:


1. AND is free to split; OR is not (chapter 12 SS12.2's asymmetry -- you can
   drop an AND-ed term and re-check it later with a Filter, but dropping
   half an OR loses rows). So extract_conjuncts() flattens nested AND and
   stops at everything else, including OR, which comes back as one opaque
   conjunct that no index can seek on.
2. A single conjunct is sargable only if a bare column sits alone on one
   side of a comparison and the other side is provably column-free -- see
   classify_predicate(). This is also why `age + 0 = 30` and `NOT age = 5`
   fall out as non-sargable for free: `age + 0` is a BoundBinaryOp, not a
   BoundColumn, and this module never even looks inside a BoundUnaryOp
   wrapping a comparison.
"""


from dataclasses import dataclass

from quilldb.sql.binder import (
    BoundBinaryOp,
    BoundColumn,
    BoundExpression,
    BoundIsNull,
    BoundLiteral,
    BoundUnaryOp,
)

# SQLite's optoverview.html SS2 list, minus what this grammar can't even
# produce (IN, GLOB) and minus "!=" -- inequality-of-negation isn't in the
# documented list, and it can't be turned into one contiguous seek range.
_SARGABLE_COMPARISONS = frozenset({"=", "<", "<=", ">", ">="})


# Flip a comparison when the bare column turns out to be on the right:
# `5 > age` means the same thing as `age < 5`.
_FLIPPED = {"=": "=", "<": ">", "<=": ">=", ">": "<", ">=": "<="}




@dataclass(frozen=True)
class Predicate:
    """One WHERE conjunct, confirmed sargable and normalized column-first.


    `column` and `operator` are always in "column OP value" order, even if
    the original SQL wrote the column on the right -- callers never need to
    re-check which side was which. `source` keeps the original bound
    expression so a caller can still evaluate it as a Filter residual
    without reconstructing anything.
    """


    column: str
    operator: str  # one of "=", "<", "<=", ">", ">=", "IS", "LIKE"
    value: BoundExpression
    source: BoundExpression




def extract_conjuncts(where: BoundExpression | None) -> list[BoundExpression]:
    """Split `where` on top-level AND. OR, and everything else, stays whole.


    `None` (no WHERE clause) yields no conjuncts. A bare non-AND expression
    yields itself, unsplit -- including an OR, which is deliberately handed
    back as one opaque conjunct so classify_predicate() rejects it instead
    of a caller trying to seek on half of it.
    """
    if where is None:
        return []
    if isinstance(where, BoundBinaryOp) and where.operator == "AND":
        return extract_conjuncts(where.left) + extract_conjuncts(where.right)
    return [where]




def _is_column_free(expr: BoundExpression) -> bool:
    """True if no BoundColumn appears anywhere inside `expr`.


    This is the other half of sargability: the non-column side of a
    comparison must be computable without reading the row, or there is
    nothing for an index seek to compare against ahead of time.
    """
    if isinstance(expr, BoundColumn):
        return False
    if isinstance(expr, BoundLiteral):
        return True
    if isinstance(expr, BoundUnaryOp):
        return _is_column_free(expr.operand)
    if isinstance(expr, BoundBinaryOp):
        return _is_column_free(expr.left) and _is_column_free(expr.right)
    if isinstance(expr, BoundIsNull):
        return _is_column_free(expr.operand)
    return False




def classify_predicate(expr: BoundExpression) -> Predicate | None:
    """Sargable shape check for one conjunct. None if no index could ever
    seek on this, regardless of which columns are indexed.


    Handles exactly three shapes, matching optoverview.html SS2:
      - `column IS NULL` (not the negated form -- "IS NOT NULL" isn't in
        the documented list, because there's no contiguous NULL-exclusion
        range to seek).
      - `column OP expr` / `expr OP column` for OP in {=, <, <=, >, >=},
        with the non-column side column-free. The right-hand form is
        normalized by flipping OP.
      - `column LIKE 'prefix%'` -- sargable only when the pattern is a
        string literal with no leading wildcard; `LIKE '%x'` can't seek
        because there's no shared prefix to descend to.
    """
    if isinstance(expr, BoundIsNull):
        if expr.negated or not isinstance(expr.operand, BoundColumn):
            return None
        return Predicate(expr.operand.name, "IS", BoundLiteral(None), expr)


    if isinstance(expr, BoundBinaryOp):
        if expr.operator == "LIKE":
            if not isinstance(expr.left, BoundColumn):
                return None
            if not isinstance(expr.right, BoundLiteral) or not isinstance(expr.right.value, str):
                return None
            pattern = expr.right.value
            if pattern.startswith(("%", "_")):
                return None
            return Predicate(expr.left.name, "LIKE", expr.right, expr)


        if expr.operator not in _SARGABLE_COMPARISONS:
            return None
        if isinstance(expr.left, BoundColumn) and _is_column_free(expr.right):
            return Predicate(expr.left.name, expr.operator, expr.right, expr)
        if isinstance(expr.right, BoundColumn) and _is_column_free(expr.left):
            return Predicate(expr.right.name, _FLIPPED[expr.operator], expr.left, expr)
        return None


    return None