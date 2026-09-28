"""Aggregate function and HashAggregate tests (chapter 18 §18.5-§18.6,
week7-query-processing.md §42/session 4).

Three layers:

1. AGGREGATES' init/step/final are pure functions, tested directly with
   no Operator or database at all (the semantics table IS the spec, and
   every row of it is a test here).
2. HashAggregate's GROUP BY mechanics (real multi-group hashing, NULL
   keys, zero-groups-over-zero-rows) are tested directly against a
   hand-built child operator (_Rows below), with BoundColumn/BoundAggregate
   built by hand -- isolates the grouping algorithm itself from the
   cost-based access-path pipeline underneath a real quilldb.connect()
   query, the same reasoning Filter/Project's own unit tests use elsewhere.
3. Everything else -- aggregate-only select lists, GROUP BY with the key
   named in the select list, HAVING, DISTINCT -- goes end to end through
   quilldb.connect() -- the interaction between the no-GROUP-BY single-row
   rule and a real access-path/WHERE pipeline underneath it is worth
   exercising honestly (test_join.py's own reasoning for going through
   Connection).
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

import quilldb
from quilldb.codec.record import Value
from quilldb.errors import AggregateError, IntegerOverflowError, UnsupportedFeatureError
from quilldb.exec.aggregate import AGGREGATES, HashAggregate
from quilldb.exec.expressions import Row
from quilldb.exec.operators import Operator
from quilldb.sql.ast import DataType
from quilldb.sql.binder import BoundAggregate, BoundColumn


def _fold(func: str, values: list[Value]) -> Value:
    """Run one aggregate over a list of raw values, skipping nothing --
    each AggSpec's own step() is what decides whether NULL is skipped."""
    spec = AGGREGATES[func]
    state = spec.init()
    for value in values:
        state = spec.step(state, value)
    return spec.final(state)


class _Rows(Operator):
    """The smallest possible child for exercising HashAggregate directly:
    replays a fixed list of rows, nothing else. No pins, no b-tree, no
    binder -- see the module docstring for why that's necessary here, not
    just convenient.
    """

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
# The semantics table (§42), one row per test
# =====================================================================


def test_sum_over_zero_rows_is_null() -> None:
    assert _fold("sum", []) is None


def test_count_star_over_zero_rows_is_zero() -> None:
    assert _fold("count_star", []) == 0


def test_count_column_skips_nulls() -> None:
    assert _fold("count", [1, None, 2, None]) == 2


def test_count_star_counts_rows_including_nulls() -> None:
    assert _fold("count_star", [1, None, 2]) == 3


def test_avg_of_integers_is_real_not_truncated() -> None:
    assert _fold("avg", [1, 2]) == 1.5


def test_avg_sum_min_max_all_skip_nulls() -> None:
    assert _fold("sum", [None, 10, None, 20]) == 30
    assert _fold("avg", [None, 10, None, 20]) == 15.0
    assert _fold("min", [None, 5, None, 2]) == 2
    assert _fold("max", [None, 5, None, 2]) == 5


def test_min_max_over_mixed_types_use_sqlites_cross_type_order() -> None:
    # NULL < numeric < text < blob (btree/index.py's compare_keys) -- a
    # numeric value always sorts below any text value, regardless of the
    # order the values are seen in.
    assert _fold("min", [5, "a"]) == 5
    assert _fold("max", [5, "a"]) == "a"


def test_min_max_over_zero_rows_is_null() -> None:
    assert _fold("min", []) is None
    assert _fold("max", []) is None


def test_sum_of_integers() -> None:
    assert _fold("sum", [1, 2, 3]) == 6


def test_sum_of_all_integer_values_overflowing_int64_raises() -> None:
    # Unlike `+` (exec/expressions.py's _fit_int64), which silently
    # promotes an overflowing result to REAL, SUM() raises instead --
    # verified against sqlite3 (week7-query-processing.md §42).
    max_int64 = 2**63 - 1
    with pytest.raises(IntegerOverflowError):
        _fold("sum", [max_int64, 1])


def test_sum_never_raises_once_a_real_input_has_been_seen() -> None:
    # The moment any input has been a REAL, SUM behaves like ordinary
    # float addition forever after, however large the running total gets.
    max_int64 = 2**63 - 1
    result = _fold("sum", [1.0, max_int64, max_int64])
    assert result == pytest.approx(1.0 + max_int64 + max_int64)


# =====================================================================
# HashAggregate's GROUP BY mechanics, against a hand-built child (_Rows)
# -- see the module docstring for why this layer exists at all right now.
# =====================================================================


def test_hash_aggregate_groups_rows_by_key() -> None:
    rows: list[Row] = [("a", 1), ("b", 2), ("a", 3), ("b", 4), ("a", 5)]
    group_by = (BoundColumn(0, "region", DataType.TEXT),)
    aggregates = (BoundAggregate("sum", BoundColumn(1, "amount", DataType.INTEGER)),)
    results = _run(HashAggregate(_Rows(rows), group_by, aggregates))
    assert {(row[0], row[1]) for row in results} == {("a", 9), ("b", 6)}


def test_hash_aggregate_with_no_group_by_keys_still_produces_one_row_over_zero_input_rows() -> None:
    # Session 3's rule, unaffected by session 4's real grouping: an empty
    # group_by is the no-GROUP-BY case, always exactly one output row.
    aggregates = (BoundAggregate("count_star", None),)
    results = _run(HashAggregate(_Rows([]), (), aggregates))
    assert results == [(0,)]


def test_hash_aggregate_with_a_real_group_by_and_zero_input_rows_produces_zero_groups() -> None:
    # A REAL GROUP BY is the opposite: zero input rows means zero groups,
    # not one -- there is no key value to group an empty stream under.
    group_by = (BoundColumn(0, "region", DataType.TEXT),)
    aggregates = (BoundAggregate("count_star", None),)
    results = _run(HashAggregate(_Rows([]), group_by, aggregates))
    assert results == []


def test_hash_aggregate_groups_null_keys_together() -> None:
    rows: list[Row] = [(None, 1), (None, 2), ("x", 3)]
    group_by = (BoundColumn(0, "region", DataType.TEXT),)
    aggregates = (BoundAggregate("count_star", None),)
    results = _run(HashAggregate(_Rows(rows), group_by, aggregates))
    assert {(row[0], row[1]) for row in results} == {(None, 2), ("x", 1)}


def test_hash_aggregate_groups_by_a_composite_key() -> None:
    rows: list[Row] = [("a", 1, 10), ("a", 1, 20), ("a", 2, 30), ("b", 1, 40)]
    group_by = (BoundColumn(0, "region", DataType.TEXT), BoundColumn(1, "year", DataType.INTEGER))
    aggregates = (BoundAggregate("sum", BoundColumn(2, "amount", DataType.INTEGER)),)
    results = _run(HashAggregate(_Rows(rows), group_by, aggregates))
    assert {(key[:2], key[2]) for key in results} == {(("a", 1), 30), (("a", 2), 30), (("b", 1), 40)}


# =====================================================================
# HashAggregate, end to end through quilldb.connect()
# =====================================================================


def _outstanding_pins(connection: quilldb.Connection) -> int:
    return sum(entry.pin_count for entry in connection.pool._cache.values())


def test_aggregate_query_over_an_empty_table_returns_exactly_one_row(tmp_path: Path) -> None:
    """The aggregate bug people ship most often (§42): no GROUP BY means
    exactly one output row, even over zero input rows -- not zero rows.
    """
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        db.execute("CREATE TABLE t (id INTEGER, val INTEGER)")
        rows = db.execute("SELECT COUNT(*), MIN(val), MAX(val) FROM t").fetchall()
        assert rows == [(0, None, None)]


def test_count_and_avg_over_real_rows(tmp_path: Path) -> None:
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        db.execute("CREATE TABLE t (id INTEGER, val INTEGER)")
        db.execute("INSERT INTO t VALUES (1, 10)")
        db.execute("INSERT INTO t VALUES (2, 20)")
        db.execute("INSERT INTO t VALUES (3, NULL)")

        rows = db.execute("SELECT COUNT(*), COUNT(val), AVG(val) FROM t").fetchall()
        assert rows == [(3, 2, 15.0)]


def test_sum_over_real_rows_skips_nulls(tmp_path: Path) -> None:
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        db.execute("CREATE TABLE t (id INTEGER, val INTEGER)")
        db.execute("INSERT INTO t VALUES (1, 10)")
        db.execute("INSERT INTO t VALUES (2, 20)")
        db.execute("INSERT INTO t VALUES (3, NULL)")

        rows = db.execute("SELECT SUM(val) FROM t").fetchall()
        assert rows == [(30,)]


def test_where_is_applied_before_aggregation(tmp_path: Path) -> None:
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        db.execute("CREATE TABLE t (id INTEGER)")
        for i in range(5):
            db.execute("INSERT INTO t VALUES (?)", (i,))

        rows = db.execute("SELECT COUNT(*) FROM t WHERE id > 1").fetchall()
        assert rows == [(3,)]  # ids 2, 3, 4


def test_where_uses_an_index_when_one_exists(tmp_path: Path) -> None:
    """The aggregate path reuses the same cost-based access-path pipeline a
    plain SELECT does (_build_single_table_source) -- WHERE placement
    doesn't know or care that a HashAggregate sits on top instead of a
    Project.
    """
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        db.execute("CREATE TABLE t (id INTEGER, val TEXT)")
        db.execute("CREATE INDEX ix_id ON t (id)")
        for i in range(5):
            db.execute("INSERT INTO t VALUES (?, ?)", (i, chr(ord('a') + i)))
        db.execute("ANALYZE")

        rows = db.execute("SELECT COUNT(*), MIN(val), MAX(val) FROM t WHERE id > 1").fetchall()
        assert rows == [(3, "c", "e")]


def test_column_name_description_for_plain_select_is_unaffected(tmp_path: Path) -> None:
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        db.execute("CREATE TABLE t (id INTEGER)")
        cursor = db.execute("SELECT id FROM t")
        assert cursor.description == (("id",),)


def test_aggregate_description_names_each_call(tmp_path: Path) -> None:
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        db.execute("CREATE TABLE t (id INTEGER)")
        cursor = db.execute("SELECT COUNT(*), MIN(id) FROM t")
        assert cursor.description == (("COUNT(*)",), ("MIN(id)",))


def test_bare_column_with_no_group_by_is_rejected(tmp_path: Path) -> None:
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        db.execute("CREATE TABLE t (id INTEGER)")
        with pytest.raises(AggregateError):
            db.execute("SELECT id, COUNT(*) FROM t")


def test_aggregate_query_leaves_no_pins_behind(tmp_path: Path) -> None:
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        db.execute("CREATE TABLE t (id INTEGER)")
        for i in range(10):
            db.execute("INSERT INTO t VALUES (?)", (i,))

        db.execute("SELECT COUNT(*) FROM t").fetchall()
        assert _outstanding_pins(db) == 0


# =====================================================================
# GROUP BY, HAVING, DISTINCT, end to end (week 7 session 4)
# =====================================================================


def _seed_regions(db: quilldb.Connection) -> None:
    db.execute("CREATE TABLE t (region TEXT, amount INTEGER)")
    db.execute("INSERT INTO t VALUES ('east', 10)")
    db.execute("INSERT INTO t VALUES ('east', 20)")
    db.execute("INSERT INTO t VALUES ('west', 5)")


def test_group_by_produces_one_row_per_distinct_key(tmp_path: Path) -> None:
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        _seed_regions(db)
        rows = db.execute("SELECT COUNT(*) FROM t GROUP BY region").fetchall()
        assert sorted(rows) == [(1,), (2,)]


def test_group_by_with_zero_rows_produces_zero_groups(tmp_path: Path) -> None:
    # Unlike the no-GROUP-BY case, a real GROUP BY over an empty table
    # returns no rows at all -- there is no key to group under.
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        db.execute("CREATE TABLE t (region TEXT, amount INTEGER)")
        rows = db.execute("SELECT COUNT(*) FROM t GROUP BY region").fetchall()
        assert rows == []


def test_having_filters_out_groups(tmp_path: Path) -> None:
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        _seed_regions(db)
        rows = db.execute("SELECT COUNT(*) FROM t GROUP BY region HAVING COUNT(*) > 1").fetchall()
        assert rows == [(2,)]


def test_having_with_no_group_by_filters_the_single_row(tmp_path: Path) -> None:
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        _seed_regions(db)
        assert db.execute("SELECT COUNT(*) FROM t HAVING COUNT(*) > 10").fetchall() == []
        assert db.execute("SELECT COUNT(*) FROM t HAVING COUNT(*) > 1").fetchall() == [(3,)]


def test_distinct_removes_duplicate_rows(tmp_path: Path) -> None:
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        db.execute("CREATE TABLE t (region TEXT)")
        db.execute("INSERT INTO t VALUES ('east')")
        db.execute("INSERT INTO t VALUES ('east')")
        db.execute("INSERT INTO t VALUES ('west')")
        rows = db.execute("SELECT DISTINCT region FROM t").fetchall()
        assert sorted(rows) == [("east",), ("west",)]


def test_distinct_dedups_on_projected_columns_not_underlying_rows(tmp_path: Path) -> None:
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        _seed_regions(db)
        # Two 'east' rows differ in `amount`, but only `region` is
        # projected -- Distinct sits above Project, so they still collapse.
        rows = db.execute("SELECT DISTINCT region FROM t WHERE region = 'east'").fetchall()
        assert rows == [("east",)]


def test_distinct_combined_with_group_by_raises(tmp_path: Path) -> None:
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        _seed_regions(db)
        with pytest.raises(UnsupportedFeatureError):
            db.execute("SELECT DISTINCT COUNT(*) FROM t GROUP BY region")


def test_group_by_description_labels_the_aggregate(tmp_path: Path) -> None:
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        _seed_regions(db)
        cursor = db.execute("SELECT COUNT(*) FROM t GROUP BY region")
        assert cursor.description == (("COUNT(*)",),)


def test_group_by_query_leaves_no_pins_behind(tmp_path: Path) -> None:
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        _seed_regions(db)
        db.execute("SELECT COUNT(*) FROM t GROUP BY region").fetchall()
        assert _outstanding_pins(db) == 0


def test_headline_group_by_returns_the_key_alongside_its_aggregate(tmp_path: Path) -> None:
    """The canonical `SELECT key, agg(...) ... GROUP BY key` shape names
    its GROUP BY key in the select list -- exercises _group_by_key_index
    (sql/binder.py) end to end, not just the pure grouping mechanics
    _Rows already covers.
    """
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        _seed_regions(db)
        rows = db.execute("SELECT region, COUNT(*) FROM t GROUP BY region").fetchall()
        assert sorted(rows) == [("east", 2), ("west", 1)]


def test_group_by_key_column_name_is_case_insensitive(tmp_path: Path) -> None:
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        _seed_regions(db)
        rows = db.execute("SELECT REGION, COUNT(*) FROM t GROUP BY region").fetchall()
        assert sorted(rows) == [("east", 2), ("west", 1)]


def test_select_list_column_not_in_group_by_still_raises(tmp_path: Path) -> None:
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        _seed_regions(db)
        with pytest.raises(AggregateError):
            db.execute("SELECT amount, COUNT(*) FROM t GROUP BY region")
