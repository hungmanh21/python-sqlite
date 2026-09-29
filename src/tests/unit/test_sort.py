"""Direct tests for exec/sort.py's Sort and exec/operators.py's Limit,
against a hand-built child operator -- isolates the sorting/limiting
mechanics themselves from the cost-based access-path pipeline underneath a
real quilldb.connect() query, the same reasoning test_aggregate.py's
_Rows-based HashAggregate tests use.

ORDER BY/LIMIT/OFFSET's binder-level resolution (ordinal keys, hidden
trailing columns, DISTINCT/JOIN interaction) is exercised in
test_binder.py; this file's own end-to-end section is the thin layer on
top that proves the two actually compose through a real Connection.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

import quilldb
from quilldb.errors import SortLimitExceededError
from quilldb.exec.expressions import Row
from quilldb.exec.operators import Limit, Operator
from quilldb.exec.sort import MAX_SORT_ROWS, Sort
from quilldb.sql.binder import BoundOrderKey


class _Rows(Operator):
    """The smallest possible child for exercising Sort/Limit directly --
    see test_aggregate.py's _Rows for the identical reasoning."""

    def __init__(self, rows: list[Row]) -> None:
        self._rows = rows
        self._iterator: Iterator[Row] | None = None

    def open(self, outer: Row = ()) -> None:
        self._iterator = iter(self._rows)

    def next(self) -> Row | None:
        assert self._iterator is not None, "open() was never called"
        return next(self._iterator, None)

    def close(self) -> None:
        self._iterator = None

    def explain(self, depth: int = 0, verbose: bool = False) -> str:
        return "Rows"


def _run(operator: Operator) -> list[Row]:
    operator.open()
    try:
        rows = []
        row = operator.next()
        while row is not None:
            rows.append(row)
            row = operator.next()
        return rows
    finally:
        operator.close()


# =====================================================================
# Sort
# =====================================================================


def test_sort_single_key_ascending() -> None:
    rows: list[Row] = [("b",), ("a",), ("c",)]
    sort = Sort(_Rows(rows), (BoundOrderKey(0, descending=False),))
    assert _run(sort) == [("a",), ("b",), ("c",)]


def test_sort_single_key_descending() -> None:
    rows: list[Row] = [("b",), ("a",), ("c",)]
    sort = Sort(_Rows(rows), (BoundOrderKey(0, descending=True),))
    assert _run(sort) == [("c",), ("b",), ("a",)]


def test_sort_mixed_direction_multi_key() -> None:
    # ORDER BY region ASC, age DESC
    rows: list[Row] = [
        ("east", 30),
        ("west", 20),
        ("east", 10),
        ("west", 40),
    ]
    sort = Sort(_Rows(rows), (BoundOrderKey(0, descending=False), BoundOrderKey(1, descending=True)))
    assert _run(sort) == [
        ("east", 30),
        ("east", 10),
        ("west", 40),
        ("west", 20),
    ]


def test_sort_null_sorts_before_every_other_value_ascending() -> None:
    rows: list[Row] = [(1,), (None,), (0,)]
    sort = Sort(_Rows(rows), (BoundOrderKey(0, descending=False),))
    assert _run(sort) == [(None,), (0,), (1,)]


def test_sort_null_sorts_last_when_descending() -> None:
    rows: list[Row] = [(1,), (None,), (0,)]
    sort = Sort(_Rows(rows), (BoundOrderKey(0, descending=True),))
    assert _run(sort) == [(1,), (0,), (None,)]


def test_sort_is_stable_when_every_key_ties() -> None:
    rows: list[Row] = [(1, "first"), (1, "second"), (1, "third")]
    sort = Sort(_Rows(rows), (BoundOrderKey(0, descending=False),))
    assert _run(sort) == rows


def test_sort_empty_input_produces_no_rows() -> None:
    sort = Sort(_Rows([]), (BoundOrderKey(0, descending=False),))
    assert _run(sort) == []


def test_sort_over_max_sort_rows_raises() -> None:
    rows: list[Row] = [(i,) for i in range(MAX_SORT_ROWS + 1)]
    sort = Sort(_Rows(rows), (BoundOrderKey(0, descending=False),))
    with pytest.raises(SortLimitExceededError):
        _run(sort)


# =====================================================================
# Limit
# =====================================================================


def test_limit_caps_the_number_of_rows() -> None:
    rows: list[Row] = [(1,), (2,), (3,)]
    limit = Limit(_Rows(rows), limit=2)
    assert _run(limit) == [(1,), (2,)]


def test_limit_zero_returns_no_rows() -> None:
    rows: list[Row] = [(1,), (2,), (3,)]
    limit = Limit(_Rows(rows), limit=0)
    assert _run(limit) == []


def test_limit_none_returns_every_row() -> None:
    rows: list[Row] = [(1,), (2,), (3,)]
    limit = Limit(_Rows(rows), limit=None)
    assert _run(limit) == rows


def test_offset_skips_leading_rows() -> None:
    rows: list[Row] = [(1,), (2,), (3,)]
    limit = Limit(_Rows(rows), limit=None, offset=1)
    assert _run(limit) == [(2,), (3,)]


def test_limit_and_offset_together() -> None:
    rows: list[Row] = [(1,), (2,), (3,), (4,)]
    limit = Limit(_Rows(rows), limit=2, offset=1)
    assert _run(limit) == [(2,), (3,)]


def test_offset_past_the_end_returns_no_rows() -> None:
    rows: list[Row] = [(1,), (2,)]
    limit = Limit(_Rows(rows), limit=None, offset=10)
    assert _run(limit) == []


def test_limit_pulls_from_child_lazily() -> None:
    """The short-circuit claim itself: next() must not drain the child
    beyond what's actually returned -- exactly what lets `LIMIT 1` over a
    million-row scan read only a handful of pages (week7-query-processing.md
    §44), proven here without needing an actual million-row table."""

    class _CountingRows(Operator):
        def __init__(self, n: int) -> None:
            self.n = n
            self.pulled = 0
            self._next = 0

        def open(self, outer: Row = ()) -> None:
            self._next = 0

        def next(self) -> Row | None:
            if self._next >= self.n:
                return None
            self.pulled += 1
            self._next += 1
            return (self._next,)

        def close(self) -> None:
            pass

        def explain(self, depth: int = 0, verbose: bool = False) -> str:
            return "CountingRows"

    child = _CountingRows(1_000_000)
    limit = Limit(child, limit=1)
    limit.open()
    try:
        assert limit.next() == (1,)
        assert child.pulled == 1
    finally:
        limit.close()


# =====================================================================
# ORDER BY + LIMIT/OFFSET end to end, through quilldb.connect()
# =====================================================================


def _seed(db: "quilldb.Connection") -> None:
    db.execute("CREATE TABLE t (region TEXT, amount INTEGER)")
    for region, amount in [("east", 30), ("west", 20), ("east", 10), ("west", 40)]:
        db.execute("INSERT INTO t VALUES (?, ?)", (region, amount))


def test_order_by_single_column_end_to_end(tmp_path: Path) -> None:
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        _seed(db)
        rows = db.execute("SELECT region, amount FROM t ORDER BY amount").fetchall()
        assert rows == [("east", 10), ("west", 20), ("east", 30), ("west", 40)]


def test_order_by_desc_end_to_end(tmp_path: Path) -> None:
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        _seed(db)
        rows = db.execute("SELECT amount FROM t ORDER BY amount DESC").fetchall()
        assert rows == [(40,), (30,), (20,), (10,)]


def test_order_by_a_column_not_in_the_select_list_end_to_end(tmp_path: Path) -> None:
    # Exercises the hidden-column strip: `region` never appears in the
    # output row, only in the row Sort itself sorts on.
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        _seed(db)
        rows = db.execute("SELECT amount FROM t ORDER BY region, amount").fetchall()
        assert rows == [(10,), (30,), (20,), (40,)]


def test_order_by_ordinal_end_to_end(tmp_path: Path) -> None:
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        _seed(db)
        rows = db.execute("SELECT region, amount FROM t ORDER BY 2 DESC").fetchall()
        assert rows == [("west", 40), ("east", 30), ("west", 20), ("east", 10)]


def test_limit_end_to_end(tmp_path: Path) -> None:
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        _seed(db)
        rows = db.execute("SELECT amount FROM t ORDER BY amount LIMIT 2").fetchall()
        assert rows == [(10,), (20,)]


def test_offset_end_to_end(tmp_path: Path) -> None:
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        _seed(db)
        rows = db.execute("SELECT amount FROM t ORDER BY amount LIMIT 2 OFFSET 1").fetchall()
        assert rows == [(20,), (30,)]


def test_limit_with_no_order_by_reads_without_sorting(tmp_path: Path) -> None:
    # No ORDER BY -- build_operator() never adds a Sort, so this is a pure
    # Limit-over-SeqScan short-circuit through a real Connection, the
    # complement of test_limit_pulls_from_child_lazily's hand-built version.
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        _seed(db)
        rows = db.execute("SELECT amount FROM t LIMIT 1").fetchall()
        assert len(rows) == 1
