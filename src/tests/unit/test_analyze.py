"""measure_table tests: a real B-tree DFS walk, pinned against counts we
can independently verify by construction (chapter 12's own demand for a
provable number, not a guessed one).


Isolated from ANALYZE persistence/index measurement on purpose (Steps 4-5
of the stage-5 checklist) -- this file only proves the walker against a
real on-disk table.
"""


from quilldb.btree.cells import decode_interior_table_cell
from quilldb.catalog.catalog import Catalog
from quilldb.constants import PageType
from quilldb.exec.operators import Insert
from quilldb.plan.analyze import measure_index, measure_table
from quilldb.sql.ast import CreateIndex, CreateTable
from quilldb.sql.binder import BoundInsert, bind
from quilldb.sql.parser import parse
from quilldb.storage.bufferpool import BufferPool
from quilldb.storage.page import parse_page
from quilldb.storage.pager import Pager


_USERS_SQL = "CREATE TABLE users (id INTEGER, name TEXT, age INTEGER)"




def _db(tmp_path) -> tuple[Pager, BufferPool, Catalog]:
    pager = Pager.create(tmp_path / "t.db")
    pool = BufferPool(pager, capacity=64)
    catalog = Catalog(pager, pool)
    catalog.load()
    return pager, pool, catalog




def _create_users(catalog: Catalog):
    statement = parse(_USERS_SQL)
    assert isinstance(statement, CreateTable)
    return catalog.create_table(statement, _USERS_SQL)




def _create_index(catalog: Catalog, sql: str):
    statement = parse(sql)
    assert isinstance(statement, CreateIndex)
    return catalog.create_index(statement, sql)




def _insert(pager: Pager, pool: BufferPool, catalog: Catalog, *rows: tuple[object, ...]) -> None:
    for row in rows:
        bound = bind(parse("INSERT INTO users VALUES (?, ?, ?)"), catalog, row)
        assert isinstance(bound, BoundInsert)
        operator = Insert(pager, pool, bound, catalog.indexes_for("users"))
        operator.open()
        assert operator.next() is None
        operator.close()




def test_measure_table_empty_table_has_zero_rows_height_one(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)


    stats = measure_table(pager, pool, table.root_page)
    assert stats.row_count == 0
    assert stats.page_count == 1
    assert stats.height == 1
    pager.close()




def test_measure_table_single_page_counts_every_row(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    rows = [(i, f"u{i}", i) for i in range(1, 6)]
    _insert(pager, pool, catalog, *rows)


    stats = measure_table(pager, pool, table.root_page)
    assert stats.row_count == 5
    assert stats.page_count == 1
    assert stats.height == 1
    pager.close()




def test_measure_table_multi_page_counts_rows_and_pages(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    rows = [(i, f"user_number_{i}_padded_for_size", i) for i in range(1, 501)]
    _insert(pager, pool, catalog, *rows)


    stats = measure_table(pager, pool, table.root_page)
    assert stats.row_count == 500
    assert stats.page_count > 1
    assert stats.height >= 2
    pager.close()




def test_measure_table_page_count_matches_manual_page_walk(tmp_path) -> None:
    """Independent check of `page_count`: walk the same tree by hand via
    validate_btree()'s own page-visiting helper shape (pool.get_page +
    parse_page), counting every reachable page, and assert the two counts
    agree -- measure_table isn't allowed to under- or over-count.
    """
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    rows = [(i, f"user_number_{i}_padded_for_size", i) for i in range(1, 301)]
    _insert(pager, pool, catalog, *rows)


    visited: set[int] = set()


    def walk(page_id: int) -> None:
        visited.add(page_id)
        raw = pool.get_page(page_id)
        try:
            body = parse_page(raw)
        finally:
            pool.unpin(page_id)
        if body.page_type is PageType.LEAF_TABLE:
            return
        for cell in body.cells:
            child, _ = decode_interior_table_cell(cell)
            walk(child)
        walk(body.right_child)


    walk(table.root_page)


    stats = measure_table(pager, pool, table.root_page)
    assert stats.page_count == len(visited)
    pager.close()




def test_measure_table_leaves_no_pins(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    table = _create_users(catalog)
    rows = [(i, f"user_number_{i}_padded_for_size", i) for i in range(1, 301)]
    _insert(pager, pool, catalog, *rows)


    measure_table(pager, pool, table.root_page)
    assert sum(entry.pin_count for entry in pool._cache.values()) == 0
    pager.close()




# =====================================================================
# measure_index: rows_per_prefix, pinned against hand-counted expectations
# =====================================================================




def test_measure_index_single_column_every_value_distinct(tmp_path) -> None:
    """Every `age` is unique, so every run has length 1 -- the average at
    the only prefix length (1) is exactly 1.
    """
    pager, pool, catalog = _db(tmp_path)
    _create_users(catalog)
    index = _create_index(catalog, "CREATE INDEX idx_age ON users (age)")
    _insert(pager, pool, catalog, *[(i, f"u{i}", i) for i in range(1, 11)])


    stats = measure_index(pager, pool, index.root_page, n_key_columns=1)
    assert stats.row_count == 10
    assert stats.rows_per_prefix == (1,)
    pager.close()




def test_measure_index_single_column_known_run_lengths(tmp_path) -> None:
    """`age` values: three 10s, two 20s, one 30 -- three runs of lengths
    3, 2, 1. Average run length is (3+2+1)/3 == 2.
    """
    pager, pool, catalog = _db(tmp_path)
    _create_users(catalog)
    index = _create_index(catalog, "CREATE INDEX idx_age ON users (age)")
    _insert(
        pager, pool, catalog,
        (1, "a", 10), (2, "b", 10), (3, "c", 10),
        (4, "d", 20), (5, "e", 20),
        (6, "f", 30),
    )


    stats = measure_index(pager, pool, index.root_page, n_key_columns=1)
    assert stats.row_count == 6
    assert stats.rows_per_prefix == (2,)
    pager.close()




def test_measure_index_composite_prefix_averages(tmp_path) -> None:
    """Index on (name, age): name='ada' covers 4 rows (two age=10, two
    age=20), name='bob' covers 2 rows (both age=30) -- chosen so both
    prefix lengths average out to an exact integer, sidestepping any
    rounding-convention question.


    Prefix length 1 (name alone): one run of 4 ("ada") and one run of 2
    ("bob") -> average (4+2)/2 == 3.
    Prefix length 2 (name,age): (ada,10) run of 2, (ada,20) run of 2,
    (bob,30) run of 2 -> average (2+2+2)/3 == 2.
    """
    pager, pool, catalog = _db(tmp_path)
    _create_users(catalog)
    index = _create_index(catalog, "CREATE INDEX idx_name_age ON users (name, age)")
    _insert(
        pager, pool, catalog,
        (1, "ada", 10), (2, "ada", 10),
        (3, "ada", 20), (4, "ada", 20),
        (5, "bob", 30), (6, "bob", 30),
    )


    stats = measure_index(pager, pool, index.root_page, n_key_columns=2)
    assert stats.row_count == 6
    assert stats.rows_per_prefix == (3, 2)
    pager.close()




def test_measure_index_empty_index_defaults_every_prefix_to_one(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    _create_users(catalog)
    index = _create_index(catalog, "CREATE INDEX idx_age ON users (age)")


    stats = measure_index(pager, pool, index.root_page, n_key_columns=1)
    assert stats.row_count == 0
    assert stats.rows_per_prefix == (1,)
    pager.close()




def test_measure_index_multi_page_leaf_pages_and_height(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    _create_users(catalog)
    index = _create_index(catalog, "CREATE INDEX idx_age ON users (age)")
    _insert(pager, pool, catalog, *[(i, f"user_number_{i}_padded_for_size", i) for i in range(1, 301)])


    stats = measure_index(pager, pool, index.root_page, n_key_columns=1)
    assert stats.row_count == 300
    assert stats.leaf_pages >= 1
    assert stats.height >= 1
    assert stats.rows_per_prefix == (1,)  # every age is distinct
    pager.close()




def test_measure_index_leaves_no_pins(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    _create_users(catalog)
    index = _create_index(catalog, "CREATE INDEX idx_age ON users (age)")
    _insert(pager, pool, catalog, *[(i, f"user_number_{i}_padded_for_size", i) for i in range(1, 301)])


    measure_index(pager, pool, index.root_page, n_key_columns=1)
    assert sum(entry.pin_count for entry in pool._cache.values()) == 0
    pager.close()
