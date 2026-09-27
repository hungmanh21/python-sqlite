"""Aggregate function and HashAggregate tests (chapter 18 §18.5-§18.6,
week7-query-processing.md §42).

Two layers: AGGREGATES' init/step/final are pure functions, tested directly
with no Operator or database at all (the semantics table IS the spec, and
every row of it is a test here). HashAggregate itself is tested end to end
through quilldb.connect() -- the interesting behavior is the interaction
between the no-GROUP-BY single-row rule and a real access-path/WHERE
pipeline underneath it, which a hand-built child operator wouldn't exercise
honestly (test_join.py's own reasoning for going through Connection).
"""

from pathlib import Path

import pytest

import quilldb
from quilldb.codec.record import Value
from quilldb.errors import AggregateError, IntegerOverflowError
from quilldb.exec.aggregate import AGGREGATES


def _fold(func: str, values: list[Value]) -> Value:
    """Run one aggregate over a list of raw values, skipping nothing --
    each AggSpec's own step() is what decides whether NULL is skipped."""
    spec = AGGREGATES[func]
    state = spec.init()
    for value in values:
        state = spec.step(state, value)
    return spec.final(state)


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
