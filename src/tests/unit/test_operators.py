"""Operator tests: row production, and the resource discipline around it.


Two kinds of assertion here, and the second is the one that catches real
bugs. Row-level: does a scan come out in rowid order, does Filter drop the
right rows, does Project reorder correctly. Resource-level: after every
path -- exhaustion, early abandonment, an exception mid-scan -- are all pins
released? A leaked pin doesn't fail here, it fails much later as a
PoolExhaustedError somewhere unrelated, so it has to be asserted directly.
"""


import pathlib
import tempfile

import pytest

from quilldb.btree.cells import encode_leaf_table_cell
from quilldb.btree.index import IndexBTree
from quilldb.catalog.catalog import Catalog
from quilldb.catalog.schema import IndexSchema, TableSchema
from quilldb.codec.record import encode_record
from quilldb.constants import PageType
from quilldb.errors import PageFullError, TypeMismatchError, UniqueViolationError
from quilldb.exec.operators import (
    Delete,
    Filter,
    Insert,
    Operator,
    Project,
    SeqScan,
    Update,
    build_operator,
)
from quilldb.plan.analyze import StatisticsCatalog
from quilldb.sql.ast import CreateIndex, CreateTable, DataType
from quilldb.sql.binder import (
    BoundBinaryOp,
    BoundColumn,
    BoundDelete,
    BoundInsert,
    BoundLiteral,
    BoundSelect,
    BoundUpdate,
    bind,
)
from quilldb.sql.parser import parse
from quilldb.storage.bufferpool import BufferPool
from quilldb.storage.page import PageBody, serialize_page
from quilldb.storage.pager import Pager

_USERS_SQL = "CREATE TABLE users (id INTEGER, name TEXT, age INTEGER)"




def _db(tmp_path, capacity: int = 64) -> tuple[Pager, BufferPool, Catalog]:
    pager = Pager.create(tmp_path / "t.db")
    pool = BufferPool(pager, capacity=capacity)
    catalog = Catalog(pager, pool)
    catalog.load()
    return pager, pool, catalog




def _create_users(catalog: Catalog) -> TableSchema:
    statement = parse(_USERS_SQL)
    assert isinstance(statement, CreateTable)
    return catalog.create_table(statement, _USERS_SQL)




def _create_index(catalog: Catalog, sql: str) -> IndexSchema:
    statement = parse(sql)
    assert isinstance(statement, CreateIndex)
    return catalog.create_index(statement, sql)




def _insert(pager: Pager, pool: BufferPool, catalog: Catalog, *rows: tuple[object, ...]) -> None:
    for row in rows:
        bound = bind(parse("INSERT INTO users VALUES (?, ?, ?)"), catalog, row)
        assert isinstance(bound, BoundInsert)
        operator = Insert(pager, pool, bound, catalog.indexes_for("users"))
        operator.open()
        assert operator.next() is None  # an INSERT yields no rows
        operator.close()




def _drain(operator: Operator) -> list[tuple[object, ...]]:
    operator.open()
    try:
        rows: list[tuple[object, ...]] = []
        while True:
            row = operator.next()
            if row is None:
                return rows
            rows.append(row)
    finally:
        operator.close()




def _outstanding_pins(pool: BufferPool) -> int:
    return sum(entry.pin_count for entry in pool._cache.values())




def _select(catalog: Catalog, sql: str, *parameters: object) -> BoundSelect:
    bound = bind(parse(sql), catalog, parameters)
    assert isinstance(bound, BoundSelect)
    return bound




def _delete(catalog: Catalog, sql: str, *parameters: object) -> BoundDelete:
    bound = bind(parse(sql), catalog, parameters)
    assert isinstance(bound, BoundDelete)
    return bound




def _update(catalog: Catalog, sql: str, *parameters: object) -> BoundUpdate:
    bound = bind(parse(sql), catalog, parameters)
    assert isinstance(bound, BoundUpdate)
    return bound




# =====================================================================
# SeqScan
# =====================================================================




def test_scan_of_an_empty_table_returns_no_rows(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)


    assert _drain(SeqScan(pager, pool, table)) == []
    assert _outstanding_pins(pool) == 0
    pager.close()




def test_scan_returns_rows_in_rowid_order(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    _insert(pager, pool, catalog, (1, "ada", 36), (2, "bob", 41), (3, "amy", 20))


    assert _drain(SeqScan(pager, pool, table)) == [
        (1, "ada", 36),
        (2, "bob", 41),
        (3, "amy", 20),
    ]
    pager.close()




def test_scan_spans_a_multi_page_tree_in_order(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    rows = [(i, f"user{i}", i * 2) for i in range(1, 401)]
    _insert(pager, pool, catalog, *rows)


    scanned = _drain(SeqScan(pager, pool, table))
    assert scanned == rows
    assert pager.page_count > 2  # genuinely split beyond a single leaf
    assert _outstanding_pins(pool) == 0
    pager.close()




def test_scan_leaves_no_pins_after_exhaustion(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    _insert(pager, pool, catalog, *[(i, f"u{i}", i) for i in range(1, 201)])


    _drain(SeqScan(pager, pool, table))
    assert _outstanding_pins(pool) == 0
    pager.close()




def test_scan_abandoned_partway_leaves_no_pins(tmp_path) -> None:
    """The common real case: `fetchone()` then close, mid-tree."""
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    _insert(pager, pool, catalog, *[(i, f"u{i}", i) for i in range(1, 201)])


    scan = SeqScan(pager, pool, table)
    scan.open()
    assert scan.next() is not None
    assert _outstanding_pins(pool) > 0  # mid-scan, the cursor holds its path
    scan.close()
    assert _outstanding_pins(pool) == 0
    pager.close()




def test_scan_next_keeps_returning_none_after_exhaustion(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    _insert(pager, pool, catalog, (1, "ada", 36))


    scan = SeqScan(pager, pool, table)
    scan.open()
    assert scan.next() == (1, "ada", 36)
    assert scan.next() is None
    assert scan.next() is None
    scan.close()
    pager.close()




def test_scan_reopen_resets_without_leaking(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    _insert(pager, pool, catalog, (1, "ada", 36), (2, "bob", 41))


    scan = SeqScan(pager, pool, table)
    scan.open()
    assert scan.next() == (1, "ada", 36)
    scan.open()  # reset mid-scan
    assert scan.next() == (1, "ada", 36)
    scan.close()
    assert _outstanding_pins(pool) == 0
    pager.close()




def test_scan_reassembles_an_overflow_payload(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    big = "x" * 10_000
    _insert(pager, pool, catalog, (1, big, 1))


    rows = _drain(SeqScan(pager, pool, table))
    assert rows == [(1, big, 1)]
    assert _outstanding_pins(pool) == 0
    pager.close()




def test_a_capacity_two_pool_can_scan_a_multi_level_tree(tmp_path) -> None:
    """The scan must stream, not accumulate: with only two frames, anything
    that held pins for the whole walk would raise PoolExhaustedError.
    """
    pager, pool, catalog = _db(tmp_path, capacity=64)
    table = _create_users(catalog)
    _insert(pager, pool, catalog, *[(i, f"u{i}", i) for i in range(1, 601)])
    pool.flush_all()


    tiny_pool = BufferPool(pager, capacity=2)
    rows = _drain(SeqScan(pager, pool=tiny_pool, table=table))
    assert [r[0] for r in rows] == list(range(1, 601))
    assert _outstanding_pins(tiny_pool) == 0
    pager.close()




# =====================================================================
# Filter
# =====================================================================




def test_filter_keeps_only_matching_rows(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    _insert(pager, pool, catalog, (1, "ada", 36), (2, "bob", 20), (3, "amy", 41))


    age = BoundColumn(2, "age", DataType.INTEGER)
    predicate = BoundBinaryOp(age, ">", BoundLiteral(30))
    rows = _drain(Filter(SeqScan(pager, pool, table), predicate))
    assert [r[1] for r in rows] == ["ada", "amy"]
    pager.close()




def test_filter_skips_many_rows_before_returning_one(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    _insert(pager, pool, catalog, *[(i, f"u{i}", i) for i in range(1, 301)])


    id_column = BoundColumn(0, "id", DataType.INTEGER)
    predicate = BoundBinaryOp(id_column, "=", BoundLiteral(299))
    rows = _drain(Filter(SeqScan(pager, pool, table), predicate))
    assert rows == [(299, "u299", 299)]
    assert _outstanding_pins(pool) == 0
    pager.close()




def test_filter_rejects_rows_whose_predicate_is_null(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    _insert(pager, pool, catalog, (1, "ada", 36), (2, "bob", None))


    age = BoundColumn(2, "age", DataType.INTEGER)
    predicate = BoundBinaryOp(age, ">", BoundLiteral(30))
    assert [r[1] for r in _drain(Filter(SeqScan(pager, pool, table), predicate))] == ["ada"]
    pager.close()




def test_filter_matching_nothing_returns_no_rows(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    _insert(pager, pool, catalog, (1, "ada", 36))


    age = BoundColumn(2, "age", DataType.INTEGER)
    predicate = BoundBinaryOp(age, ">", BoundLiteral(1000))
    assert _drain(Filter(SeqScan(pager, pool, table), predicate)) == []
    assert _outstanding_pins(pool) == 0
    pager.close()




# =====================================================================
# Project
# =====================================================================




def test_project_reorders_columns(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    _insert(pager, pool, catalog, (1, "ada", 36))


    expressions = (
        BoundColumn(2, "age", DataType.INTEGER),
        BoundColumn(1, "name", DataType.TEXT),
    )
    assert _drain(Project(SeqScan(pager, pool, table), expressions)) == [(36, "ada")]
    pager.close()




def test_project_computes_expressions(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    _insert(pager, pool, catalog, (1, "ada", 36))


    age = BoundColumn(2, "age", DataType.INTEGER)
    expressions = (BoundBinaryOp(age, "+", BoundLiteral(1)),)
    assert _drain(Project(SeqScan(pager, pool, table), expressions)) == [(37,)]
    pager.close()




# =====================================================================
# Failure paths: an exception must not strand pins
# =====================================================================




def test_an_evaluator_failure_in_filter_closes_the_scan(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    _insert(pager, pool, catalog, *[(i, f"u{i}", i) for i in range(1, 51)])


    # name is TEXT; adding a number to it is the documented refusal.
    name = BoundColumn(1, "name", DataType.TEXT)
    predicate = BoundBinaryOp(name, "+", BoundLiteral(1))


    operator = Filter(SeqScan(pager, pool, table), predicate)
    operator.open()
    with pytest.raises(TypeMismatchError):
        operator.next()
    assert _outstanding_pins(pool) == 0  # closed itself and its child
    pager.close()




def test_an_evaluator_failure_in_project_closes_the_scan(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    _insert(pager, pool, catalog, (1, "ada", 36))


    name = BoundColumn(1, "name", DataType.TEXT)
    expressions = (BoundBinaryOp(name, "*", BoundLiteral(2)),)


    operator = Project(SeqScan(pager, pool, table), expressions)
    operator.open()
    with pytest.raises(TypeMismatchError):
        operator.next()
    assert _outstanding_pins(pool) == 0
    pager.close()




def test_a_failure_partway_through_a_long_scan_closes_the_scan(tmp_path) -> None:
    """The predicate succeeds for 200 rows then fails, so the cursor is deep
    in the tree holding a whole root-to-leaf path when it raises.
    """
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    rows: list[tuple[object, ...]] = [(i, f"u{i}", i) for i in range(1, 201)]
    rows.append((201, "poison", None))
    _insert(pager, pool, catalog, *rows)


    # Rows 1..150 fail the first comparison, so AND short-circuits to FALSE
    # and never evaluates the poisoned right side. Only once id > 150 does
    # `name + 1` get reached -- by which point the cursor is deep in the tree.
    age = BoundColumn(2, "age", DataType.INTEGER)
    name = BoundColumn(1, "name", DataType.TEXT)
    predicate = BoundBinaryOp(
        BoundBinaryOp(age, ">", BoundLiteral(150)),
        "AND",
        BoundBinaryOp(name, "+", BoundLiteral(1)),
    )


    operator = Filter(SeqScan(pager, pool, table), predicate)
    operator.open()
    with pytest.raises(TypeMismatchError):
        while operator.next() is not None:
            pass
    assert _outstanding_pins(pool) == 0
    pager.close()




# =====================================================================
# close() idempotence
# =====================================================================




def test_close_is_idempotent_for_every_operator(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    _insert(pager, pool, catalog, (1, "ada", 36))


    bound_insert = bind(parse("INSERT INTO users VALUES (9, 'x', 1)"), catalog)
    assert isinstance(bound_insert, BoundInsert)


    age = BoundColumn(2, "age", DataType.INTEGER)
    operators = [
        SeqScan(pager, pool, table),
        Filter(SeqScan(pager, pool, table), BoundBinaryOp(age, ">", BoundLiteral(1))),
        Project(SeqScan(pager, pool, table), (age,)),
        Insert(pager, pool, bound_insert),
    ]
    for operator in operators:
        operator.open()
        operator.close()
        operator.close()  # must not raise
        operator.close()
    assert _outstanding_pins(pool) == 0
    pager.close()




def test_close_without_open_is_safe(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    SeqScan(pager, pool, table).close()  # never opened
    pager.close()




# =====================================================================
# Insert
# =====================================================================




def test_insert_assigns_rowid_one_to_an_empty_table(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    _insert(pager, pool, catalog, (1, "ada", 36))


    from quilldb.btree.cursor import TableCursor


    with TableCursor(pager, pool, table.root_page) as cursor:
        cursor.first()
        assert cursor.rowid() == 1
    pager.close()




def test_insert_assigns_increasing_rowids(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    _insert(pager, pool, catalog, (1, "a", 1), (2, "b", 2), (3, "c", 3))


    from quilldb.btree.cursor import TableCursor


    seen = []
    with TableCursor(pager, pool, table.root_page) as cursor:
        cursor.first()
        while cursor.valid:
            seen.append(cursor.rowid())
            cursor.next()
    assert seen == [1, 2, 3]
    pager.close()




def test_insert_writes_exactly_once_even_if_next_is_called_repeatedly(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)


    bound = bind(parse("INSERT INTO users VALUES (1, 'ada', 36)"), catalog)
    assert isinstance(bound, BoundInsert)
    operator = Insert(pager, pool, bound)
    operator.open()
    for _ in range(5):
        assert operator.next() is None
    operator.close()


    assert _drain(SeqScan(pager, pool, table)) == [(1, "ada", 36)]
    pager.close()




def test_insert_leaves_no_pins(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    _create_users(catalog)
    _insert(pager, pool, catalog, *[(i, f"u{i}", i) for i in range(1, 101)])
    assert _outstanding_pins(pool) == 0
    pager.close()




# =====================================================================
# build_operator: the fixed Week 3 plan
# =====================================================================




def test_build_operator_builds_project_over_filter_over_seqscan(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    _create_users(catalog)


    plan = build_operator(_select(catalog, "SELECT name FROM users WHERE age > 30"), pager, pool, catalog)
    assert plan.explain() == "Project\n└─ Filter\n   └─ SeqScan users"
    pager.close()




def test_build_operator_omits_filter_without_a_where(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    _create_users(catalog)


    plan = build_operator(_select(catalog, "SELECT name FROM users"), pager, pool, catalog)
    assert plan.explain() == "Project\n└─ SeqScan users"
    pager.close()




def test_build_operator_chooses_index_scan_for_an_indexed_equality(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    _create_users(catalog)
    _create_index(catalog, "CREATE INDEX idx_age ON users (age)")
    _insert(pager, pool, catalog, *[(i, f"u{i}", i) for i in range(1, 21)])


    plan = build_operator(_select(catalog, "SELECT * FROM users WHERE age = 7"), pager, pool, catalog)
    assert plan.explain() == "Project\n└─ IndexScan idx_age"
    assert _drain(plan) == [(7, "u7", 7)]
    pager.close()




def test_build_operator_uses_real_analyzed_stats_to_choose_between_indexes(tmp_path) -> None:
    """Chapter 12's own ANALYZE demonstration (§12.6, stage-5 Step 6): a
    genuinely selective index (`age`, unique per row) is chosen over a
    genuinely unselective one (`name`, one of two values shared by every
    row) only once real numbers exist -- default_index_stats() treats
    every index as equally (10 rows/value) selective, so this contrast is
    only visible once StatisticsCatalog.analyze() has run.


    1,000 padded-string rows, not the usual handful: with only a page or
    two of data, a real (small) table height makes SeqScan cheap enough to
    beat any IndexScan outright, selective or not -- there's no contrast
    to observe until the table is big enough that a full scan actually
    costs more than a seek plus a handful of row fetches.
    """
    pager, pool, catalog = _db(tmp_path)
    _create_users(catalog)
    _create_index(catalog, "CREATE INDEX idx_name ON users (name)")
    _create_index(catalog, "CREATE INDEX idx_age ON users (age)")
    _insert(
        pager, pool, catalog,
        *[(i, "even_padded_for_size_xxx" if i % 2 == 0 else "odd_padded_for_size_xxxx", i) for i in range(1, 1001)],
    )
    stats = StatisticsCatalog(pager, pool, catalog)
    stats.analyze("users")


    selective_plan = build_operator(
        _select(catalog, "SELECT * FROM users WHERE age = 7"), pager, pool, catalog, stats
    )
    assert selective_plan.explain() == "Project\n└─ IndexScan idx_age"


    unselective_plan = build_operator(
        _select(catalog, "SELECT * FROM users WHERE name = 'even_padded_for_size_xxx'"), pager, pool, catalog, stats
    )
    assert unselective_plan.explain() == "Project\n└─ Filter\n   └─ SeqScan users"
    pager.close()




def test_build_operator_falls_back_to_seq_scan_without_a_matching_index(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    _create_users(catalog)
    _insert(pager, pool, catalog, (1, "ada", 36), (2, "bob", 20))


    plan = build_operator(_select(catalog, "SELECT * FROM users WHERE age = 20"), pager, pool, catalog)
    assert plan.explain() == "Project\n└─ Filter\n   └─ SeqScan users"
    assert _drain(plan) == [(2, "bob", 20)]
    pager.close()




def test_build_operator_filter_applies_only_the_residual_predicate(tmp_path) -> None:
    """`age > 10 AND name = 'u15'` on an index over `age` alone: the seek
    consumes `age > 10` (an IndexScan), `name = 'u15'` can't be part of
    that seek so it must survive as a Filter on top -- but ONLY that
    predicate, not the whole original WHERE clause re-evaluated.
    """
    pager, pool, catalog = _db(tmp_path)
    _create_users(catalog)
    _create_index(catalog, "CREATE INDEX idx_age ON users (age)")
    _insert(pager, pool, catalog, *[(i, f"u{i}", i) for i in range(1, 21)])


    plan = build_operator(
        _select(catalog, "SELECT * FROM users WHERE age > 10 AND name = 'u15'"), pager, pool, catalog
    )
    assert plan.explain() == "Project\n└─ Filter\n   └─ IndexScan idx_age"
    assert _drain(plan) == [(15, "u15", 15)]
    pager.close()




def test_build_operator_index_and_seq_scan_plans_return_identical_rows(tmp_path) -> None:
    """Trap #3 (chapter 12 SS12.6): the planner must never change results.
    Run the same WHERE clauses against a table with and without a matching
    index and assert identical row sets either way.
    """
    where_clauses = [
        "age = 12",
        "age > 15",
        "age >= 5 AND age <= 9",
        "age = 3 AND name = 'u3'",
        "age = 999",  # matches nothing
        "name = 'u10'",  # not indexed at all -- always a seq_scan
    ]


    def _rows_for(create_index: bool) -> dict[str, list[tuple[object, ...]]]:
        with tempfile.TemporaryDirectory() as tmp:
            pager, pool, catalog = _db(pathlib.Path(tmp))
            _create_users(catalog)
            if create_index:
                _create_index(catalog, "CREATE INDEX idx_age ON users (age)")
            _insert(pager, pool, catalog, *[(i, f"u{i}", i) for i in range(1, 21)])
            results = {}
            for clause in where_clauses:
                plan = build_operator(_select(catalog, f"SELECT * FROM users WHERE {clause}"), pager, pool, catalog)
                results[clause] = sorted(_drain(plan))
            pager.close()
            return results


    assert _rows_for(create_index=True) == _rows_for(create_index=False)




def test_build_operator_builds_an_insert(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    _create_users(catalog)


    bound = bind(parse("INSERT INTO users VALUES (1, 'ada', 36)"), catalog)
    assert isinstance(bound, BoundInsert)
    plan = build_operator(bound, pager, pool, catalog)
    assert isinstance(plan, Insert)
    assert plan.explain() == "Insert users"
    pager.close()




def test_the_whole_pipeline_end_to_end(tmp_path) -> None:
    """parse -> bind -> build_operator -> rows, which is every Week 3 layer
    except the public API.
    """
    pager, pool, catalog = _db(tmp_path)
    _create_users(catalog)
    _insert(
        pager, pool, catalog,
        (1, "ada", 36), (2, "bob", 20), (3, "amy", 41), (4, "al", None),
    )


    plan = build_operator(
        _select(catalog, "SELECT name, age + 1 FROM users WHERE age > ? AND name LIKE 'a%'", 30),
        pager,
        pool,
        catalog,
    )
    assert _drain(plan) == [("ada", 37), ("amy", 42)]
    assert _outstanding_pins(pool) == 0
    pager.close()




def test_select_star_through_the_pipeline(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    _create_users(catalog)
    _insert(pager, pool, catalog, (1, "ada", 36))


    plan = build_operator(_select(catalog, "SELECT * FROM users"), pager, pool, catalog)
    assert _drain(plan) == [(1, "ada", 36)]
    pager.close()




def test_select_on_an_empty_table_returns_no_rows_not_an_error(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    _create_users(catalog)


    plan = build_operator(_select(catalog, "SELECT * FROM users WHERE age > 30"), pager, pool, catalog)
    assert _drain(plan) == []
    pager.close()




# =====================================================================
# Insert: index maintenance
# =====================================================================




def _insert_via_build_operator(pager: Pager, pool: BufferPool, catalog: Catalog, row: tuple[object, ...]) -> None:
    bound = bind(parse("INSERT INTO users VALUES (?, ?, ?)"), catalog, row)
    assert isinstance(bound, BoundInsert)
    with build_operator(bound, pager, pool, catalog) as operator:
        operator.next()




def test_insert_adds_an_entry_to_every_index_on_the_table(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    _create_users(catalog)
    name_index = _create_index(catalog, "CREATE INDEX idx_name ON users (name)")
    age_index = _create_index(catalog, "CREATE INDEX idx_age ON users (age)")


    _insert_via_build_operator(pager, pool, catalog, (1, "ada", 36))


    assert list(IndexBTree(pager, pool, name_index.root_page, n_key_columns=1, unique=False).seek_eq(["ada"])) == [1]
    assert list(IndexBTree(pager, pool, age_index.root_page, n_key_columns=1, unique=False).seek_eq([36])) == [1]
    pager.close()




def test_insert_ignores_a_table_with_no_indexes(tmp_path) -> None:
    """Insert(indexes=()) is the default -- confirms a table with no
    indexes doesn't even try to touch one.
    """
    pager, pool, catalog = _db(tmp_path)
    _create_users(catalog)


    _insert_via_build_operator(pager, pool, catalog, (1, "ada", 36))


    assert _drain(SeqScan(pager, pool, catalog.get_table("users"))) == [(1, "ada", 36)]
    pager.close()




def test_insert_checks_unique_conflict_before_writing_anything(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    _insert_via_build_operator(pager, pool, catalog, (1, "ada", 36))
    _create_index(catalog, "CREATE UNIQUE INDEX idx_name ON users (name)")
    page_count_before = pager.page_count


    with pytest.raises(UniqueViolationError) as excinfo:
        _insert_via_build_operator(pager, pool, catalog, (2, "ada", 41))


    assert excinfo.value.index_name == "idx_name"
    assert excinfo.value.key == ("ada",)
    assert pager.page_count == page_count_before  # nothing allocated on the failure path
    assert _drain(SeqScan(pager, pool, table)) == [(1, "ada", 36)]  # the second row was never written
    pager.close()




def test_insert_allows_many_nulls_in_a_unique_indexed_column(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    _create_users(catalog)
    _create_index(catalog, "CREATE UNIQUE INDEX idx_name ON users (name)")


    _insert_via_build_operator(pager, pool, catalog, (1, None, 36))
    _insert_via_build_operator(pager, pool, catalog, (2, None, 41))  # must not raise


    assert _drain(SeqScan(pager, pool, catalog.get_table("users"))) == [(1, None, 36), (2, None, 41)]
    pager.close()




def test_insert_with_indexes_leaves_no_pins(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    _create_users(catalog)
    _create_index(catalog, "CREATE INDEX idx_name ON users (name)")


    for i in range(1, 21):
        _insert_via_build_operator(pager, pool, catalog, (i, f"u{i}", i))


    assert _outstanding_pins(pool) == 0
    pager.close()




# =====================================================================
# Delete
# =====================================================================




def _delete_via_build_operator(
    pager: Pager, pool: BufferPool, catalog: Catalog, sql: str, *parameters: object
) -> int:
    bound = _delete(catalog, sql, *parameters)
    with build_operator(bound, pager, pool, catalog) as operator:
        operator.next()
        return operator.rows_affected




def test_build_operator_builds_a_delete(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    _create_users(catalog)


    plan = build_operator(_delete(catalog, "DELETE FROM users WHERE age > 30"), pager, pool, catalog)
    assert isinstance(plan, Delete)
    assert plan.explain() == "Delete users"
    pager.close()




def test_delete_with_where_removes_only_matching_rows(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    _insert(pager, pool, catalog, (1, "ada", 36), (2, "bob", 20), (3, "amy", 41))


    affected = _delete_via_build_operator(pager, pool, catalog, "DELETE FROM users WHERE age < 30")
    assert affected == 1
    assert _drain(SeqScan(pager, pool, table)) == [(1, "ada", 36), (3, "amy", 41)]
    pager.close()




def test_delete_with_no_where_removes_every_row(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    _insert(pager, pool, catalog, (1, "ada", 36), (2, "bob", 20))


    affected = _delete_via_build_operator(pager, pool, catalog, "DELETE FROM users")
    assert affected == 2
    assert _drain(SeqScan(pager, pool, table)) == []
    pager.close()




def test_delete_matching_nothing_leaves_the_table_untouched(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    _insert(pager, pool, catalog, (1, "ada", 36))


    affected = _delete_via_build_operator(pager, pool, catalog, "DELETE FROM users WHERE age > 1000")
    assert affected == 0
    assert _drain(SeqScan(pager, pool, table)) == [(1, "ada", 36)]
    pager.close()




def test_delete_on_an_empty_table_returns_zero(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    _create_users(catalog)


    assert _delete_via_build_operator(pager, pool, catalog, "DELETE FROM users") == 0
    pager.close()




def test_delete_substitutes_a_parameter_in_where(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    _insert(pager, pool, catalog, (1, "ada", 36), (2, "bob", 20))


    affected = _delete_via_build_operator(pager, pool, catalog, "DELETE FROM users WHERE id = ?", 2)
    assert affected == 1
    assert _drain(SeqScan(pager, pool, table)) == [(1, "ada", 36)]
    pager.close()




def test_delete_removes_index_entries_for_deleted_rows_only(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    _create_users(catalog)
    name_index = _create_index(catalog, "CREATE INDEX idx_name ON users (name)")
    _insert_via_build_operator(pager, pool, catalog, (1, "ada", 36))
    _insert_via_build_operator(pager, pool, catalog, (2, "bob", 20))


    _delete_via_build_operator(pager, pool, catalog, "DELETE FROM users WHERE name = 'ada'")


    index = IndexBTree(pager, pool, name_index.root_page, n_key_columns=1, unique=False)
    assert list(index.seek_eq(["ada"])) == []
    assert list(index.seek_eq(["bob"])) == [2]
    pager.close()




def test_delete_leaves_no_pins(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    _create_users(catalog)
    _create_index(catalog, "CREATE INDEX idx_name ON users (name)")
    for i in range(1, 51):
        _insert_via_build_operator(pager, pool, catalog, (i, f"u{i}", i))


    _delete_via_build_operator(pager, pool, catalog, "DELETE FROM users WHERE age > 25")
    assert _outstanding_pins(pool) == 0
    pager.close()




# =====================================================================
# Update
# =====================================================================




def _update_via_build_operator(
    pager: Pager, pool: BufferPool, catalog: Catalog, sql: str, *parameters: object
) -> int:
    bound = _update(catalog, sql, *parameters)
    with build_operator(bound, pager, pool, catalog) as operator:
        operator.next()
        return operator.rows_affected




def test_build_operator_builds_an_update(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    _create_users(catalog)


    plan = build_operator(_update(catalog, "UPDATE users SET age = 1"), pager, pool, catalog)
    assert isinstance(plan, Update)
    assert plan.explain() == "Update users"
    pager.close()




def test_update_sets_a_column_on_matching_rows_only(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    _insert(pager, pool, catalog, (1, "ada", 36), (2, "bob", 20))


    affected = _update_via_build_operator(pager, pool, catalog, "UPDATE users SET age = 100 WHERE id = 1")
    assert affected == 1
    assert _drain(SeqScan(pager, pool, table)) == [(1, "ada", 100), (2, "bob", 20)]
    pager.close()




def test_update_with_no_where_updates_every_row(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    _insert(pager, pool, catalog, (1, "ada", 36), (2, "bob", 20))


    affected = _update_via_build_operator(pager, pool, catalog, "UPDATE users SET age = 0")
    assert affected == 2
    assert _drain(SeqScan(pager, pool, table)) == [(1, "ada", 0), (2, "bob", 0)]
    pager.close()




def test_update_expression_reads_the_old_value(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    _insert(pager, pool, catalog, (1, "ada", 36))


    _update_via_build_operator(pager, pool, catalog, "UPDATE users SET age = age + 1")
    assert _drain(SeqScan(pager, pool, table)) == [(1, "ada", 37)]
    pager.close()




def test_update_simultaneous_assignment_swaps_rather_than_clobbers(tmp_path) -> None:
    """SET id = age, age = id must both read the ORIGINAL row -- not each
    other's just-written value -- or this becomes (age, age) instead of a
    swap.
    """
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    _insert(pager, pool, catalog, (1, "ada", 36))


    _update_via_build_operator(pager, pool, catalog, "UPDATE users SET id = age, age = id")
    assert _drain(SeqScan(pager, pool, table)) == [(36, "ada", 1)]
    pager.close()




def test_update_rewrites_a_non_unique_index_when_the_key_changes(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    _create_users(catalog)
    name_index = _create_index(catalog, "CREATE INDEX idx_name ON users (name)")
    _insert_via_build_operator(pager, pool, catalog, (1, "ada", 36))


    _update_via_build_operator(pager, pool, catalog, "UPDATE users SET name = 'ines' WHERE id = 1")


    index = IndexBTree(pager, pool, name_index.root_page, n_key_columns=1, unique=False)
    assert list(index.seek_eq(["ada"])) == []
    assert list(index.seek_eq(["ines"])) == [1]
    pager.close()




def test_update_of_an_unindexed_column_leaves_the_index_alone(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    _create_users(catalog)
    name_index = _create_index(catalog, "CREATE INDEX idx_name ON users (name)")
    _insert_via_build_operator(pager, pool, catalog, (1, "ada", 36))


    _update_via_build_operator(pager, pool, catalog, "UPDATE users SET age = 99 WHERE id = 1")


    index = IndexBTree(pager, pool, name_index.root_page, n_key_columns=1, unique=False)
    assert list(index.seek_eq(["ada"])) == [1]
    pager.close()




def test_update_allows_setting_a_unique_column_to_its_own_current_value(tmp_path) -> None:
    """The row's OWN old index entry must not look like a conflict with
    its own new key -- the self-exclusion Update._apply()'s docstring
    calls out.
    """
    pager, pool, catalog = _db(tmp_path)
    _create_users(catalog)
    _create_index(catalog, "CREATE UNIQUE INDEX idx_name ON users (name)")
    _insert_via_build_operator(pager, pool, catalog, (1, "ada", 36))


    affected = _update_via_build_operator(pager, pool, catalog, "UPDATE users SET age = 40 WHERE id = 1")
    assert affected == 1
    pager.close()




def test_update_raises_unique_violation_before_writing_anything(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    _insert_via_build_operator(pager, pool, catalog, (1, "ada", 36))
    _insert_via_build_operator(pager, pool, catalog, (2, "bob", 20))
    _create_index(catalog, "CREATE UNIQUE INDEX idx_name ON users (name)")


    with pytest.raises(UniqueViolationError) as excinfo:
        _update_via_build_operator(pager, pool, catalog, "UPDATE users SET name = 'ada' WHERE id = 2")


    assert excinfo.value.index_name == "idx_name"
    assert _drain(SeqScan(pager, pool, table)) == [(1, "ada", 36), (2, "bob", 20)]
    pager.close()




def test_update_detects_a_conflict_between_two_rows_in_the_same_statement(tmp_path) -> None:
    """Row 2's new key collides with row 1's new key, not with anything
    that existed before this UPDATE started -- which only shows up if row
    1 is fully applied, index entry included, before row 2 is checked.
    """
    pager, pool, catalog = _db(tmp_path)
    _create_users(catalog)
    _insert_via_build_operator(pager, pool, catalog, (1, "ada", 36))
    _insert_via_build_operator(pager, pool, catalog, (2, "bob", 20))
    _create_index(catalog, "CREATE UNIQUE INDEX idx_name ON users (name)")


    with pytest.raises(UniqueViolationError):
        _update_via_build_operator(pager, pool, catalog, "UPDATE users SET name = 'same'")
    pager.close()




def _write_row_cell(rowid: int, name_len: int, age: int) -> bytes:
    """A LEAF_TABLE cell for (rowid, 'x' * name_len, age), a real encoded
    record -- unlike test_btree_splits.py's raw-bytes cells, this has to
    decode_record() cleanly, since Update reads it back through the real
    SeqScan-shaped path (_scan_matching_rows).
    """
    payload = encode_record((rowid, "x" * name_len, age))
    return encode_leaf_table_cell(rowid, len(payload), payload)




def test_update_restores_the_old_row_when_the_new_value_cannot_be_written(tmp_path) -> None:
    """docs/theory/btree/10-deletion-and-space-reuse.md Sec10.5 rule 3: an
    UPDATE must not leave a row deleted if re-inserting its new value
    fails. Hand-builds the same "no valid two-way split" leaf shape
    test_btree_splits.py's test_insert_raises_cleanly_when_no_two_way_
    split_exists uses (three cells sized so a too-big middle cell fits
    beside neither neighbor), sized via real encode_record() calls so
    Update's own decode_record() reads them back correctly.
    """
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)


    with pool.pinned(table.root_page, dirty=True) as raw:
        raw[:] = serialize_page(PageBody(
            PageType.LEAF_TABLE,
            cells=[
                _write_row_cell(1, 1892, 10),
                _write_row_cell(3, 44, 30),
                _write_row_cell(5, 1012, 50),
            ],
        ))


    with pytest.raises(PageFullError):
        _update_via_build_operator(pager, pool, catalog, "UPDATE users SET name = ? WHERE id = 3", "x" * 3272)


    assert _drain(SeqScan(pager, pool, table)) == [
        (1, "x" * 1892, 10),
        (3, "x" * 44, 30),
        (5, "x" * 1012, 50),
    ]
    pager.close()




def test_update_on_an_empty_table_returns_zero(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    _create_users(catalog)
    assert _update_via_build_operator(pager, pool, catalog, "UPDATE users SET age = 1") == 0
    pager.close()




def test_update_leaves_no_pins(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    _create_users(catalog)
    _create_index(catalog, "CREATE INDEX idx_name ON users (name)")
    for i in range(1, 31):
        _insert_via_build_operator(pager, pool, catalog, (i, f"u{i}", i))


    _update_via_build_operator(pager, pool, catalog, "UPDATE users SET age = age + 1")
    assert _outstanding_pins(pool) == 0
    pager.close()