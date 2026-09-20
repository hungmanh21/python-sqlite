"""IndexScan tests: row production via an index seek + table lookup, and
the resource discipline around it (Step 1 of the stage-5 checklist).


Isolated from build_operator() on purpose -- these hand-build an AccessPath
directly, the same way test_operators.py exercises SeqScan/Filter/Project
before build_operator() enters the picture. The planner's own choice of
AccessPath is Step 2's concern, not this file's.
"""


from quilldb.catalog.catalog import Catalog
from quilldb.catalog.schema import IndexSchema, TableSchema
from quilldb.exec.operators import IndexScan, Insert, Operator
from quilldb.plan.planner import AccessPath, PlanCost
from quilldb.plan.predicates import Predicate
from quilldb.sql.ast import CreateIndex, CreateTable
from quilldb.sql.binder import BoundBinaryOp, BoundInsert, BoundLiteral, BoundUnaryOp, bind
from quilldb.sql.parser import parse
from quilldb.storage.bufferpool import BufferPool
from quilldb.storage.pager import Pager

_USERS_SQL = "CREATE TABLE users (id INTEGER, name TEXT, age INTEGER)"




def _db(tmp_path) -> tuple[Pager, BufferPool, Catalog]:
    pager = Pager.create(tmp_path / "t.db")
    pool = BufferPool(pager, capacity=64)
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
        indexes = catalog.indexes_for("users")
        operator = Insert(pager, pool, bound, indexes)
        operator.open()
        assert operator.next() is None
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




def _eq(column: str, value: object) -> Predicate:
    literal = BoundLiteral(value)
    return Predicate(column, "=", literal, literal)




def _cmp(column: str, operator: str, value: object) -> Predicate:
    literal = BoundLiteral(value)
    return Predicate(column, operator, literal, literal)




def _path(index: IndexSchema, *seek_terms: Predicate) -> AccessPath:
    return AccessPath("index_scan", index, seek_terms, ())




# =====================================================================
# equality seeks
# =====================================================================




def test_single_column_equality_seek_finds_the_matching_row(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    index = _create_index(catalog, "CREATE INDEX idx_age ON users (age)")
    _insert(pager, pool, catalog, (1, "ada", 36), (2, "bob", 20), (3, "amy", 41))


    path = _path(index, _eq("age", 20))
    rows = _drain(IndexScan(pager, pool, table, path))
    assert rows == [(2, "bob", 20)]
    pager.close()




def test_equality_seek_matching_nothing_returns_no_rows(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    index = _create_index(catalog, "CREATE INDEX idx_age ON users (age)")
    _insert(pager, pool, catalog, (1, "ada", 36))


    path = _path(index, _eq("age", 999))
    assert _drain(IndexScan(pager, pool, table, path)) == []
    pager.close()




def test_composite_equality_prefix_seeks_on_both_columns(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    index = _create_index(catalog, "CREATE INDEX idx_name_age ON users (name, age)")
    _insert(pager, pool, catalog, (1, "ada", 36), (2, "ada", 20), (3, "bob", 20))


    path = _path(index, _eq("name", "ada"), _eq("age", 20))
    rows = _drain(IndexScan(pager, pool, table, path))
    assert rows == [(2, "ada", 20)]
    pager.close()




def test_equality_seek_on_an_index_matching_many_rows_returns_all_of_them(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    index = _create_index(catalog, "CREATE INDEX idx_age ON users (age)")
    _insert(pager, pool, catalog, (1, "ada", 30), (2, "bob", 30), (3, "amy", 30), (4, "al", 40))


    path = _path(index, _eq("age", 30))
    rows = _drain(IndexScan(pager, pool, table, path))
    assert {r[0] for r in rows} == {1, 2, 3}
    pager.close()




# =====================================================================
# sandwiched inequality seeks (range)
# =====================================================================




def test_range_seek_between_two_bounds(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    index = _create_index(catalog, "CREATE INDEX idx_age ON users (age)")
    _insert(pager, pool, catalog, *[(i, f"u{i}", i) for i in range(1, 11)])


    path = _path(index, _cmp("age", ">", 3), _cmp("age", "<", 7))
    rows = _drain(IndexScan(pager, pool, table, path))
    assert sorted(r[2] for r in rows) == [4, 5, 6]
    pager.close()




def test_range_seek_with_only_a_lower_bound(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    index = _create_index(catalog, "CREATE INDEX idx_age ON users (age)")
    _insert(pager, pool, catalog, *[(i, f"u{i}", i) for i in range(1, 6)])


    path = _path(index, _cmp("age", ">=", 3))
    rows = _drain(IndexScan(pager, pool, table, path))
    assert sorted(r[2] for r in rows) == [3, 4, 5]
    pager.close()




def test_range_seek_with_a_leading_equality_prefix(tmp_path) -> None:
    """`name='ada' AND age>20` on index (name, age): the equality pins
    `name`, the inequality bounds `age` within that pin.
    """
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    index = _create_index(catalog, "CREATE INDEX idx_name_age ON users (name, age)")
    _insert(pager, pool, catalog, (1, "ada", 10), (2, "ada", 30), (3, "bob", 30))


    path = _path(index, _eq("name", "ada"), _cmp("age", ">", 20))
    rows = _drain(IndexScan(pager, pool, table, path))
    assert rows == [(2, "ada", 30)]
    pager.close()




def test_two_same_direction_inequalities_keep_the_tighter_bound(tmp_path) -> None:
    """`age>5 AND age>3` -- both `>`, so the leading-column rule can pack
    both into one column's seek_terms. The binding constraint is the
    TIGHTER one (age>5): whichever predicate happens to come last in
    seek_terms must not silently win over a stricter earlier one.
    """
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    index = _create_index(catalog, "CREATE INDEX idx_age ON users (age)")
    _insert(pager, pool, catalog, *[(i, f"u{i}", i) for i in range(1, 11)])


    path = _path(index, _cmp("age", ">", 5), _cmp("age", ">", 3))
    rows = _drain(IndexScan(pager, pool, table, path))
    assert sorted(r[2] for r in rows) == [6, 7, 8, 9, 10]
    pager.close()




def test_two_same_direction_inequalities_tighter_bound_wins_regardless_of_order(tmp_path) -> None:
    """Same as above but with the tighter bound written FIRST -- the
    result must not depend on seek_terms order.
    """
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    index = _create_index(catalog, "CREATE INDEX idx_age ON users (age)")
    _insert(pager, pool, catalog, *[(i, f"u{i}", i) for i in range(1, 11)])


    path = _path(index, _cmp("age", "<", 4), _cmp("age", "<", 8))
    rows = _drain(IndexScan(pager, pool, table, path))
    assert sorted(r[2] for r in rows) == [1, 2, 3]
    pager.close()




def test_tied_bound_prefers_the_exclusive_comparison(tmp_path) -> None:
    """`age>=5 AND age>5` tie on the same value -- the exclusive `>` is
    the stricter constraint and must win, excluding age=5.
    """
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    index = _create_index(catalog, "CREATE INDEX idx_age ON users (age)")
    _insert(pager, pool, catalog, *[(i, f"u{i}", i) for i in range(1, 11)])


    path = _path(index, _cmp("age", ">=", 5), _cmp("age", ">", 5))
    rows = _drain(IndexScan(pager, pool, table, path))
    assert sorted(r[2] for r in rows) == [6, 7, 8, 9, 10]
    pager.close()




# =====================================================================
# resource discipline
# =====================================================================




def test_index_scan_leaves_no_pins_after_exhaustion(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    index = _create_index(catalog, "CREATE INDEX idx_age ON users (age)")
    _insert(pager, pool, catalog, *[(i, f"u{i}", i) for i in range(1, 201)])


    path = _path(index, _cmp("age", ">", 0))
    _drain(IndexScan(pager, pool, table, path))
    assert _outstanding_pins(pool) == 0
    pager.close()




def test_index_scan_abandoned_partway_leaves_no_pins(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    index = _create_index(catalog, "CREATE INDEX idx_age ON users (age)")
    _insert(pager, pool, catalog, *[(i, f"u{i}", i) for i in range(1, 21)])


    path = _path(index, _cmp("age", ">", 0))
    scan = IndexScan(pager, pool, table, path)
    scan.open()
    assert scan.next() is not None
    scan.close()
    assert _outstanding_pins(pool) == 0
    pager.close()




def test_index_scan_next_keeps_returning_none_after_exhaustion(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    index = _create_index(catalog, "CREATE INDEX idx_age ON users (age)")
    _insert(pager, pool, catalog, (1, "ada", 36))


    path = _path(index, _eq("age", 36))
    scan = IndexScan(pager, pool, table, path)
    scan.open()
    assert scan.next() == (1, "ada", 36)
    assert scan.next() is None
    assert scan.next() is None
    scan.close()
    pager.close()




def test_index_scan_reopen_resets_without_leaking(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    index = _create_index(catalog, "CREATE INDEX idx_age ON users (age)")
    _insert(pager, pool, catalog, (1, "ada", 36), (2, "bob", 41))


    path = _path(index, _cmp("age", ">", 0))
    scan = IndexScan(pager, pool, table, path)
    scan.open()
    first = scan.next()
    scan.open()  # reset mid-scan
    assert scan.next() == first
    scan.close()
    assert _outstanding_pins(pool) == 0
    pager.close()




def test_index_scan_close_without_open_is_safe(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    index = _create_index(catalog, "CREATE INDEX idx_age ON users (age)")


    IndexScan(pager, pool, table, _path(index, _eq("age", 1))).close()
    pager.close()




def test_index_scan_explain_names_the_index(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    index = _create_index(catalog, "CREATE INDEX idx_age ON users (age)")


    scan = IndexScan(pager, pool, table, _path(index, _eq("age", 1)))
    assert scan.explain() == "IndexScan idx_age"
    pager.close()




def test_index_scan_explain_non_verbose_ignores_a_known_cost(tmp_path) -> None:
    """Stage-5 Step 7: verbose defaults to False, so an ordinary explain()
    call (debugging, or any of the plain-format tests above/in
    test_operators.py) keeps today's terse text even once a path has been
    fully costed -- only EXPLAIN itself asks for the annotated form.
    """
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    index = _create_index(catalog, "CREATE INDEX idx_age ON users (age)")
    path = AccessPath(
        "index_scan", index, (_eq("age", 7),), (), rows_fetched=1, est_rows=1, cost=PlanCost(startup=8.0, total=16.01)
    )


    scan = IndexScan(pager, pool, table, path)
    assert scan.explain() == "IndexScan idx_age"
    assert scan.explain(verbose=False) == "IndexScan idx_age"
    pager.close()




def test_index_scan_explain_verbose_shows_predicate_and_cost(tmp_path) -> None:
    """Chapter 12 §12.6's own EXPLAIN format: `IndexScan ix (col = val)
    est_rows=N startup=X.XX cost=Y.YY`."""
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    index = _create_index(catalog, "CREATE INDEX idx_age ON users (age)")
    path = AccessPath(
        "index_scan", index, (_eq("age", 7),), (), rows_fetched=1, est_rows=1, cost=PlanCost(startup=8.0, total=16.01)
    )


    scan = IndexScan(pager, pool, table, path)
    assert scan.explain(verbose=True) == "IndexScan idx_age (age = 7) est_rows=1 startup=8.00 cost=16.01"
    pager.close()




def test_index_scan_explain_verbose_joins_a_sandwich_seek_with_and(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    index = _create_index(catalog, "CREATE INDEX idx_age ON users (age)")
    path = AccessPath(
        "index_scan",
        index,
        (_cmp("age", ">=", 5), _cmp("age", "<=", 9)),
        (),
        rows_fetched=5,
        est_rows=5,
        cost=PlanCost(startup=8.0, total=20.05),
    )


    scan = IndexScan(pager, pool, table, path)
    assert scan.explain(verbose=True) == (
        "IndexScan idx_age (age >= 5 AND age <= 9) est_rows=5 startup=8.00 cost=20.05"
    )
    pager.close()




def test_index_scan_explain_verbose_without_cost_omits_startup_and_cost(tmp_path) -> None:
    """A hand-built AccessPath that never went through assign_cost() (cost
    still at its None default) shouldn't crash EXPLAIN -- just print what's
    known (est_rows) and skip what isn't.
    """
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    index = _create_index(catalog, "CREATE INDEX idx_age ON users (age)")


    scan = IndexScan(pager, pool, table, _path(index, _eq("age", 7)))
    assert scan.explain(verbose=True) == "IndexScan idx_age (age = 7) est_rows=0"
    pager.close()

# =====================================================================
# non-literal (but column-free) seek bounds, and NULL bounds
# =====================================================================




def test_a_negated_literal_bound_seeks_instead_of_crashing(tmp_path) -> None:
    """SQL has no negative literal -- `-1` binds as BoundUnaryOp('-') over
    BoundLiteral(1). An earlier version asserted `isinstance(value,
    BoundLiteral)` here and crashed on every `WHERE indexed_col >= -1`.
    plan/predicates.py's sargability test is `_is_column_free`, which
    deliberately admits any expression computable without a row, so the
    operator must evaluate the bound rather than refuse the index -- real
    sqlite3 plans this as SEARCH ... USING INDEX.
    """
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    index = _create_index(catalog, "CREATE INDEX idx_age ON users (age)")
    _insert(pager, pool, catalog, (1, "ada", -5), (2, "bob", -1), (3, "amy", 7))


    negative_one = BoundUnaryOp("-", BoundLiteral(1))
    predicate = Predicate("age", ">=", negative_one, negative_one)
    rows = _drain(IndexScan(pager, pool, table, _path(index, predicate)))


    assert [row[0] for row in rows] == [2, 3]
    assert _outstanding_pins(pool) == 0




def test_an_arithmetic_bound_is_folded_before_seeking(tmp_path) -> None:
    """Same contract as the unary case, one node deeper: `age = 2 + 5` is a
    BoundBinaryOp whose operands are both column-free, so it folds to 7.
    """
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    index = _create_index(catalog, "CREATE INDEX idx_age ON users (age)")
    _insert(pager, pool, catalog, (1, "ada", 7), (2, "bob", 20))


    sum_expr = BoundBinaryOp(BoundLiteral(2), "+", BoundLiteral(5))
    predicate = Predicate("age", "=", sum_expr, sum_expr)
    rows = _drain(IndexScan(pager, pool, table, _path(index, predicate)))


    assert [row[0] for row in rows] == [1]




def test_a_null_bound_yields_no_rows_rather_than_the_whole_table(tmp_path) -> None:
    """`age > NULL` is NULL, never true, so no row can match. Without an
    explicit guard the seek returns EVERY row instead: compare_keys orders
    NULL below every other value -- right for storing NULLs, wrong for a
    comparison bound -- so the range seek starts below the lowest key and
    walks the entire index. Wrong answers, silently.
    """
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    index = _create_index(catalog, "CREATE INDEX idx_age ON users (age)")
    _insert(pager, pool, catalog, (1, "ada", 36), (2, "bob", 20), (3, "amy", None))


    for operator in (">", ">=", "<", "<=", "="):
        rows = _drain(IndexScan(pager, pool, table, _path(index, _cmp("age", operator, None))))
        assert rows == [], f"`age {operator} NULL` must match nothing, got {rows}"
    assert _outstanding_pins(pool) == 0




def test_an_is_null_seek_still_matches_nulls(tmp_path) -> None:
    """The one operator excluded from the guard above. `IS` is how
    classify_predicate spells `column IS NULL`, and it is precisely the
    comparison that DOES match NULLs -- folding it into the unsatisfiable
    case would silently break a legitimate seek.
    """
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    index = _create_index(catalog, "CREATE INDEX idx_age ON users (age)")
    _insert(pager, pool, catalog, (1, "ada", 36), (2, "bob", None))


    predicate = Predicate("age", "IS", BoundLiteral(None), BoundLiteral(None))
    rows = _drain(IndexScan(pager, pool, table, _path(index, predicate)))


    assert [row[0] for row in rows] == [2]




def test_an_unsatisfiable_bound_closes_cleanly_without_leaking_pins(tmp_path) -> None:
    """The early return leaves the scan open but empty, so next() must keep
    answering None and close() must still release the table cursor it had
    already built before the guard fired.
    """
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    index = _create_index(catalog, "CREATE INDEX idx_age ON users (age)")
    _insert(pager, pool, catalog, (1, "ada", 36))


    operator = IndexScan(pager, pool, table, _path(index, _cmp("age", ">", None)))
    operator.open()
    assert operator.next() is None
    assert operator.next() is None
    operator.close()
    operator.close()  # idempotent
    assert _outstanding_pins(pool) == 0
