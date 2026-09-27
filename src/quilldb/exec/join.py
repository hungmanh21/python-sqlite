"""NestedLoopJoin (chapter 17): the executor half of week 7's headline
feature.

The inner operator is RE-OPENED once per outer row -- which is what makes
this an index nested loop join for free when the inner is an IndexScan
parameterized by the outer row's join value: `inner.open(outer_row)` was
already the whole point of session 0.4's `open(outer)` retrofit, so this
operator does nothing IndexScan-specific at all. It just calls open() on
whatever inner it's handed, once per outer row, and lets IndexScan's own
`_seek_value(term, outer)` do the correlated part.

RESOURCES: `inner.open()` already resets rather than leaks (every
Operator's documented contract -- SeqScan.open()/IndexScan.open() both
call `self.close()` first), so re-opening the inner |outer| times is safe
by construction. This operator still calls `inner.close()` explicitly the
moment one outer row's inner stream is exhausted, rather than waiting for
the NEXT open() to do it implicitly -- so a caller that stops pulling
partway through (a LIMIT, an exception) never leaves the inner holding
pins for a row that's no longer current.
"""

from typing import Literal

from quilldb.exec.expressions import Row, evaluate, where_passes
from quilldb.exec.operators import Operator, _explain_line
from quilldb.sql.binder import BoundExpression


class NestedLoopJoin(Operator):
    """`outer` and `inner` are already-built operator trees (an inner join
    reorders freely -- see plan/search.py's enumerate_join_plans -- so
    "outer"/"inner" here means the join's own two sides, not the FROM
    clause's declared order). `on` is the join condition, already run
    through sql/binder.py's resolve_layout for the (outer_width +
    inner_width) combined row this operator produces -- None only for a
    comma join, which matches every row pair unconditionally.

    `inner_width` is the inner side's own row width, needed to NULL-extend
    an unmatched outer row for a LEFT JOIN even when the inner never
    produces a single row (an empty or entirely-filtered-out inner table
    still has a column count).
    """

    def __init__(
        self,
        outer: Operator,
        inner: Operator,
        on: BoundExpression | None,
        inner_width: int,
        join_type: Literal["inner", "left"] = "inner",
    ) -> None:
        self.outer = outer
        self.inner = inner
        self.on = on
        self.inner_width = inner_width
        self.join_type = join_type
        self._outer_row: Row | None = None
        self._matched = False

    def open(self, outer: Row = ()) -> None:
        self.outer.open(outer)
        self._outer_row = None
        self._matched = False

    def next(self) -> Row | None:
        try:
            while True:
                if self._outer_row is None:
                    self._outer_row = self.outer.next()
                    if self._outer_row is None:
                        return None
                    self._matched = False  # trap 2: reset on ADVANCE, not on open
                    self.inner.open(self._outer_row)

                inner_row = self.inner.next()
                if inner_row is None:
                    self.inner.close()  # trap 1: release this outer row's inner cursor now
                    exhausted_row = self._outer_row
                    was_matched = self._matched
                    self._outer_row = None
                    if self.join_type == "left" and not was_matched:
                        return exhausted_row + (None,) * self.inner_width
                    continue

                combined = self._outer_row + inner_row
                # trap 4: NULL is not a match -- where_passes rejects both
                # NULL and FALSE, so a NULL join key never joins to anything,
                # matched or not.
                if self.on is None or where_passes(evaluate(self.on, combined)):
                    self._matched = True
                    return combined
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        self.outer.close()
        self.inner.close()
        self._outer_row = None

    def explain(self, depth: int = 0, verbose: bool = False) -> str:
        label = "NestedLoopJoin" if self.join_type == "inner" else "NestedLoopJoin (LEFT)"
        return (
            _explain_line(depth, label)
            + "\n"
            + self.outer.explain(depth + 1, verbose)
            + "\n"
            + self.inner.explain(depth + 1, verbose)
        )
