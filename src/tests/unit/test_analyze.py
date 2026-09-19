"""measure_table tests: a real B-tree DFS walk, pinned against counts we
can independently verify by construction (chapter 12's own demand for a
provable number, not a guessed one).


Isolated from ANALYZE persistence/index measurement on purpose (Steps 4-5
of the stage-5 checklist) -- this file only proves the walker against a
real on-disk table.
"""


import pytest

from quilldb.btree.cells import decode_interior_table_cell
from quilldb.btree.cursor import TableCursor
from quilldb.catalog.catalog import Catalog
from quilldb.codec.record import decode_record
from quilldb.constants import PageType
from quilldb.errors import TableNotFoundError
from quilldb.exec.operators import Insert
from quilldb.plan.analyze import StatisticsCatalog, measure_index, measure_table
from quilldb.plan.statistics import (
    IndexStats,
    default_index_stats,
    default_table_stats,
    encode_stat1_row,
    parse_stat1,
)
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




# =====================================================================
# Step 5: quill_stat1 persistence + StatisticsCatalog
# =====================================================================




def test_encode_stat1_row_round_trips_through_parse_stat1(tmp_path) -> None:
    pager, _pool, catalog = _db(tmp_path)
    _create_users(catalog)
    index = _create_index(catalog, "CREATE INDEX idx_name_age ON users (name, age)")


    stats = IndexStats(row_count=6, rows_per_prefix=(3, 2))
    assert parse_stat1(encode_stat1_row(stats), index) == stats
    pager.close()




def test_statistics_catalog_creates_quill_stat1_table(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)


    StatisticsCatalog(pager, pool, catalog)


    table = catalog.get_table("quill_stat1")
    assert [c.name for c in table.columns] == ["tbl", "idx", "stat"]
    pager.close()




def test_statistics_catalog_reopen_does_not_recreate_table(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)


    StatisticsCatalog(pager, pool, catalog)
    StatisticsCatalog(pager, pool, catalog)  # must not raise TableAlreadyExistsError


    pager.close()




def test_table_stats_falls_back_to_default_before_analyze(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    _create_users(catalog)
    stats_catalog = StatisticsCatalog(pager, pool, catalog)


    assert stats_catalog.table_stats("users") == default_table_stats()
    pager.close()




def test_table_stats_unknown_table_raises(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    stats_catalog = StatisticsCatalog(pager, pool, catalog)


    with pytest.raises(TableNotFoundError):
        stats_catalog.table_stats("ghost")
    pager.close()




def test_index_stats_falls_back_to_default_before_analyze(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    _create_users(catalog)
    index = _create_index(catalog, "CREATE INDEX idx_age ON users (age)")
    stats_catalog = StatisticsCatalog(pager, pool, catalog)


    assert stats_catalog.index_stats(index) == default_index_stats(index, default_table_stats())
    pager.close()




def test_analyze_one_table_persists_real_table_and_index_stats(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    _create_users(catalog)
    index = _create_index(catalog, "CREATE INDEX idx_age ON users (age)")
    _insert(pager, pool, catalog, *[(i, f"u{i}", i) for i in range(1, 11)])
    stats_catalog = StatisticsCatalog(pager, pool, catalog)


    stats_catalog.analyze("users")


    assert stats_catalog.table_stats("users").row_count == 10
    index_stats = stats_catalog.index_stats(index)
    assert index_stats.row_count == 10
    assert index_stats.rows_per_prefix == (1,)  # every age is distinct
    pager.close()




def test_analyze_unknown_target_raises(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    stats_catalog = StatisticsCatalog(pager, pool, catalog)


    with pytest.raises(TableNotFoundError):
        stats_catalog.analyze("ghost")
    pager.close()




def test_analyze_with_no_target_measures_every_table(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    _create_users(catalog)
    _insert(pager, pool, catalog, *[(i, f"u{i}", i) for i in range(1, 4)])
    orders_sql = "CREATE TABLE orders (id INTEGER, user_id INTEGER)"
    orders_statement = parse(orders_sql)
    assert isinstance(orders_statement, CreateTable)
    catalog.create_table(orders_statement, orders_sql)
    stats_catalog = StatisticsCatalog(pager, pool, catalog)


    stats_catalog.analyze(None)


    assert stats_catalog.table_stats("users").row_count == 3
    assert stats_catalog.table_stats("orders").row_count == 0
    pager.close()




def test_analyze_rerun_replaces_rather_than_duplicates_rows(tmp_path) -> None:
    pager, pool, catalog = _db(tmp_path)
    _create_users(catalog)
    _insert(pager, pool, catalog, *[(i, f"u{i}", i) for i in range(1, 4)])
    stats_catalog = StatisticsCatalog(pager, pool, catalog)
    stats_catalog.analyze("users")


    _insert(pager, pool, catalog, (4, "u4", 4), (5, "u5", 5))
    stats_catalog.analyze("users")


    assert stats_catalog.table_stats("users").row_count == 5
    stat1_table = catalog.get_table("quill_stat1")
    with TableCursor(pager, pool, stat1_table.root_page) as cursor:
        cursor.first()
        matches = 0
        while cursor.valid:
            tbl, idx, _ = decode_record(cursor.record())
            if (tbl, idx) == ("users", None):
                matches += 1
            cursor.next()
    assert matches == 1
    pager.close()




def test_analyze_survives_reload(tmp_path) -> None:
    path = tmp_path / "t.db"
    pager = Pager.create(path)
    pool = BufferPool(pager, capacity=64)
    catalog = Catalog(pager, pool)
    catalog.load()
    _create_users(catalog)
    _create_index(catalog, "CREATE INDEX idx_age ON users (age)")
    _insert(pager, pool, catalog, *[(i, f"u{i}", i) for i in range(1, 8)])
    StatisticsCatalog(pager, pool, catalog).analyze("users")
    pool.flush_all()
    pager.close()


    reopened_pager = Pager.open(path)
    reopened_pool = BufferPool(reopened_pager)
    reopened_catalog = Catalog(reopened_pager, reopened_pool)
    reopened_catalog.load()
    reopened_stats = StatisticsCatalog(reopened_pager, reopened_pool, reopened_catalog)


    reopened_index = reopened_catalog.indexes_for("users")[0]
    assert reopened_stats.table_stats("users").row_count == 7
    assert reopened_stats.index_stats(reopened_index).row_count == 7
    reopened_pager.close()
