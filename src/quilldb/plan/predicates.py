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
    table_ordinal: int = 0
    """Which table's column this predicate seeks -- 0 for every
    single-table query (BoundColumn.table_ordinal's own default), and the
    seeking table's FROM-clause position for a join predicate. `value` may
    still reference OTHER tables (an equijoin's other side, e.g. `u.id` in
    `o.user_id = u.id` when classified for `orders`) -- that's exactly what
    lets it become a correlated seek, evaluated against the outer row once
    a nested loop join re-opens the inner side per row (week7-query-
    processing.md session 0.4/§41).
    """




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




def _is_free_of(expr: BoundExpression, table_ordinal: int) -> bool:
    """True if no BoundColumn belonging to `table_ordinal` appears anywhere
    inside `expr`. A column from any OTHER table doesn't disqualify it --
    for a join predicate like `o.user_id = u.id`, `u.id` is free of
    `orders` even though it isn't column-free at all, and that's exactly
    what makes it a seek candidate when `orders` is the table being
    classified (week7-query-processing.md §43, "Predicates: which table,
    and where they go"). A single-table query never has a second
    table_ordinal in play, so calling this with `table_ordinal=0` (the
    default every BoundColumn carries) is the original column-free check,
    unchanged.
    """
    if isinstance(expr, BoundColumn):
        return expr.table_ordinal != table_ordinal
    if isinstance(expr, BoundLiteral):
        return True
    if isinstance(expr, BoundUnaryOp):
        return _is_free_of(expr.operand, table_ordinal)
    if isinstance(expr, BoundBinaryOp):
        return _is_free_of(expr.left, table_ordinal) and _is_free_of(expr.right, table_ordinal)
    if isinstance(expr, BoundIsNull):
        return _is_free_of(expr.operand, table_ordinal)
    return False


def referenced_tables(expr: BoundExpression) -> set[int]:
    """Every table_ordinal any BoundColumn in `expr` belongs to.

    Used by join planning to decide whether a WHERE/ON conjunct is usable
    yet: a conjunct can only be classified against a table once every OTHER
    table it references has already been placed earlier in the join order
    (plan/search.py's enumerate_join_plans).
    """
    if isinstance(expr, BoundColumn):
        return {expr.table_ordinal}
    if isinstance(expr, BoundLiteral):
        return set()
    if isinstance(expr, BoundUnaryOp):
        return referenced_tables(expr.operand)
    if isinstance(expr, BoundBinaryOp):
        return referenced_tables(expr.left) | referenced_tables(expr.right)
    if isinstance(expr, BoundIsNull):
        return referenced_tables(expr.operand)
    return set()


def classify_predicate(expr: BoundExpression, table_ordinal: int = 0) -> Predicate | None:
    """Sargable shape check for one conjunct, against one table. None if no
    index on `table_ordinal` could ever seek on this, regardless of which
    columns are indexed.


    Handles exactly three shapes, matching optoverview.html SS2:
      - `column IS NULL` (not the negated form -- "IS NOT NULL" isn't in
        the documented list, because there's no contiguous NULL-exclusion
        range to seek).
      - `column OP expr` / `expr OP column` for OP in {=, <, <=, >, >=},
        with the non-column side free of `table_ordinal`. The right-hand
        form is normalized by flipping OP.
      - `column LIKE 'prefix%'` -- sargable only when the pattern is a
        string literal with no leading wildcard; `LIKE '%x'` can't seek
        because there's no shared prefix to descend to.


    `table_ordinal` defaults to 0, matching every single-table BoundColumn
    -- a bare-table caller (bind_select's WHERE, DELETE, UPDATE) never has
    to pass it. A join caller passes the table currently being planned as
    an inner (or the base table as 0); the bare column identifying the seek
    must belong to exactly that table, and the other side just has to be
    free of it, not of every table (see _is_free_of).
    """
    if isinstance(expr, BoundIsNull):
        if expr.negated or not isinstance(expr.operand, BoundColumn):
            return None
        if expr.operand.table_ordinal != table_ordinal:
            return None
        return Predicate(expr.operand.name, "IS", BoundLiteral(None), expr, table_ordinal)


    if isinstance(expr, BoundBinaryOp):
        if expr.operator == "LIKE":
            if not isinstance(expr.left, BoundColumn) or expr.left.table_ordinal != table_ordinal:
                return None
            if not isinstance(expr.right, BoundLiteral) or not isinstance(expr.right.value, str):
                return None
            pattern = expr.right.value
            if pattern.startswith(("%", "_")):
                return None
            return Predicate(expr.left.name, "LIKE", expr.right, expr, table_ordinal)


        if expr.operator not in _SARGABLE_COMPARISONS:
            return None
        if (
            isinstance(expr.left, BoundColumn)
            and expr.left.table_ordinal == table_ordinal
            and _is_free_of(expr.right, table_ordinal)
        ):
            return Predicate(expr.left.name, expr.operator, expr.right, expr, table_ordinal)
        if (
            isinstance(expr.right, BoundColumn)
            and expr.right.table_ordinal == table_ordinal
            and _is_free_of(expr.left, table_ordinal)
        ):
            return Predicate(expr.right.name, _FLIPPED[expr.operator], expr.left, expr, table_ordinal)
        return None


    return None