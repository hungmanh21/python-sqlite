"""`ORDER BY`, in memory (chapter 18 §18.4, week7-query-processing.md §43).

Sort is a PIPELINE BREAKER, unlike everything else in exec/operators.py:
open() must see every row from the child before the first next() can
answer, because there is no way to know a row belongs at position 3
without having looked at every row that might come before it. Filter,
Project, and Distinct never need more than the one row in front of them;
Sort needs all of them, held in memory, which is exactly why
MAX_SORT_ROWS exists as a documented limit rather than something quilldb
silently tries to page to disk.

`limit`/`offset`-driven early termination (a bounded top-K heap, O(N log K)
time and O(K) memory instead of a full in-memory sort -- chapter 18 §18.4)
is NOT implemented here: Sort always materializes and sorts every input
row, and LIMIT/OFFSET are applied by exec/operators.py's Limit sitting on
top. That's a real, documented gap against the roadmap's stretch goal, not
an oversight -- see MAX_SORT_ROWS below.
"""

import functools

from quilldb.btree.index import compare_keys
from quilldb.errors import SortLimitExceededError
from quilldb.exec.expressions import Row
from quilldb.exec.operators import Operator, _explain_line
from quilldb.sql.binder import BoundOrderKey

MAX_SORT_ROWS = 1_000_000
"""A documented LIMITATION, not a crash: getting OOM-killed on a huge
ORDER BY is a bug, but refusing with an actionable message ("add an index
on the ORDER BY column") is a limitation quilldb owns on purpose, the same
policy IntegerOverflowError and PoolExhaustedError already follow
elsewhere in this codebase.
"""


class Sort(Operator):
    """Sort the child's entire output by `keys`, in memory.

    `keys` is a tuple of BoundOrderKey -- (index into the row, descending)
    pairs, evaluated left to right exactly like SQL's multi-key ORDER BY:
    later keys only break ties the earlier ones left. Comparison reuses
    btree/index.py's `compare_keys`, the same NULL < numeric < text < blob
    total order an index descent (and MIN/MAX, exec/aggregate.py) already
    needs -- ORDER BY needs a decidable answer for every pair of rows,
    unlike evaluate()'s three-valued `<`/`>`, which can return NULL.
    """

    def __init__(self, child: Operator, keys: tuple[BoundOrderKey, ...]) -> None:
        self.child = child
        self.keys = keys
        self._results: list[Row] = []
        self._position = 0

    def open(self, outer: Row = ()) -> None:
        self.child.open(outer)
        rows: list[Row] = []
        try:
            row = self.child.next()
            while row is not None:
                rows.append(row)
                if len(rows) > MAX_SORT_ROWS:
                    raise SortLimitExceededError(
                        f"ORDER BY requires sorting more than {MAX_SORT_ROWS} rows, "
                        "over the limit quilldb sorts in memory. Add an index on the "
                        "ORDER BY column to avoid sorting entirely."
                    )
                row = self.child.next()
        finally:
            self.child.close()
        rows.sort(key=functools.cmp_to_key(self._compare))
        self._results = rows
        self._position = 0

    def _compare(self, a: Row, b: Row) -> int:
        for key in self.keys:
            result = compare_keys([a[key.index]], [b[key.index]])
            if key.descending:
                result = -result
            if result != 0:
                return result
        return 0

    def next(self) -> Row | None:
        if self._position >= len(self._results):
            return None
        row = self._results[self._position]
        self._position += 1
        return row

    def close(self) -> None:
        self._results = []
        self._position = 0
        self.child.close()

    def explain(self, depth: int = 0, verbose: bool = False) -> str:
        keys = ", ".join(f"{key.index}{' DESC' if key.descending else ' ASC'}" for key in self.keys)
        return _explain_line(depth, f"Sort ({keys})") + "\n" + self.child.explain(depth + 1, verbose)
