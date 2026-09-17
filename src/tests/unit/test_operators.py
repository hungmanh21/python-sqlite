"""Operator tests: row production, and the resource discipline around it.


Two kinds of assertion here, and the second is the one that catches real
bugs. Row-level: does a scan come out in rowid order, does Filter drop the
right rows, does Project reorder correctly. Resource-level: after every
path -- exhaustion, early abandonment, an exception mid-scan -- are all pins
released? A leaked pin doesn't fail here, it fails much later as a
PoolExhaustedError somewhere unrelated, so it has to be asserted directly.
"""


import pytest


from quilldb.catalog.catalog import Catalog
from quilldb.catalog.schema import TableSchema
from quilldb.errors import TypeMismatchError
from quilldb.exec.operators import Filter, Insert, Operator, Project, SeqScan, build_operator
from quilldb.sql.ast import CreateTable, DataType
from quilldb.sql.binder import (
    BoundBinaryOp,
    BoundColumn,
    BoundInsert,
    BoundLiteral,
    BoundSelect,
    bind,
)
from quilldb.sql.parser import parse
from quilldb.storage.bufferpool import BufferPool
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




def _insert(pager: Pager, pool: BufferPool, catalog: Catalog, *rows: tuple[object, ...]) -> None:
    for row in rows:
        bound = bind(parse("INSERT INTO users VALUES (?, ?, ?)"), catalog, row)
        assert isinstance(bound, BoundInsert)
        operator = Insert(pager, pool, bound)
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


    plan = build_operator(_select(catalog, "SELECT name FROM users WHERE age > 30"), pager, pool)
    assert plan.explain() == "Project\n└─ Filter\n   └─ SeqScan users"
    pager.close()




def test_build_operator_omits_filter_without_a_where(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    _create_users(catalog)


    plan = build_operator(_select(catalog, "SELECT name FROM users"), pager, pool)
    assert plan.explain() == "Project\n└─ SeqScan users"
    pager.close()




def test_build_operator_builds_an_insert(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    _create_users(catalog)


    bound = bind(parse("INSERT INTO users VALUES (1, 'ada', 36)"), catalog)
    assert isinstance(bound, BoundInsert)
    plan = build_operator(bound, pager, pool)
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
    )
    assert _drain(plan) == [("ada", 37), ("amy", 42)]
    assert _outstanding_pins(pool) == 0
    pager.close()




def test_select_star_through_the_pipeline(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    _create_users(catalog)
    _insert(pager, pool, catalog, (1, "ada", 36))


    plan = build_operator(_select(catalog, "SELECT * FROM users"), pager, pool)
    assert _drain(plan) == [(1, "ada", 36)]
    pager.close()




def test_select_on_an_empty_table_returns_no_rows_not_an_error(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    _create_users(catalog)


    plan = build_operator(_select(catalog, "SELECT * FROM users WHERE age > 30"), pager, pool)
    assert _drain(plan) == []
    pager.close()