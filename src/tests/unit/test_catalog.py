"""Tests for Catalog: durable schema metadata stored in sqlite_schema (page 1).


Exercises the catalog against a real Pager/BufferPool -- no fake storage
layer, since the whole point of storing schema in an ordinary table b-tree
is that sqlite3 itself must be able to read it back (the last two tests
below check exactly that).
"""


import random
import subprocess

import pytest

import quilldb
from quilldb.btree.btree import BTree
from quilldb.btree.cells import decode_leaf_table_cell, encode_leaf_table_cell
from quilldb.btree.index import IndexBTree
from quilldb.catalog.catalog import Catalog
from quilldb.catalog.schema import IndexSchema, TableSchema
from quilldb.codec.record import Value, decode_record, encode_record
from quilldb.constants import SCHEMA_ROOT_PAGE
from quilldb.errors import (
    ColumnNotFoundError,
    CorruptDatabaseError,
    IndexAlreadyExistsError,
    PageFullError,
    TableAlreadyExistsError,
    TableNotFoundError,
    UniqueViolationError,
    UnsupportedFeatureError,
)
from quilldb.sql.ast import CreateIndex, CreateTable
from quilldb.sql.parser import parse
from quilldb.storage.bufferpool import BufferPool
from quilldb.storage.page import PageType, parse_page, write_page_body
from quilldb.storage.pager import Pager, page_header_offset

_USERS_SQL = "CREATE TABLE users (id INTEGER, name TEXT, age INTEGER)"




def _catalog(pager: Pager, pool: BufferPool) -> Catalog:
    catalog = Catalog(pager, pool)
    catalog.load()
    return catalog




def _create_table(catalog: Catalog, sql: str) -> TableSchema:
    statement = parse(sql)
    assert isinstance(statement, CreateTable)
    return catalog.create_table(statement, sql)




def _create_index(catalog: Catalog, sql: str) -> IndexSchema:
    statement = parse(sql)
    assert isinstance(statement, CreateIndex)
    return catalog.create_index(statement, sql)




def _insert_row(pager: Pager, pool: BufferPool, table: TableSchema, rowid: int, values: tuple[Value, ...]) -> None:
    """Put one row directly into a table's b-tree, bypassing Insert/exec --
    the catalog layer has no INSERT of its own, and create_index()'s
    backfill only cares that TableCursor can scan rows, not how they got
    there.
    """
    BTree(pager, pool, table.root_page).insert(rowid, encode_record(values))




def _raw_catalog_rows(pager: Pager, pool: BufferPool) -> list[tuple[object, ...]]:
    """Decode page 1's rows straight from the bytes, bypassing Catalog -- so
    the format assertions don't depend on the code under test.
    """
    raw = pool.get_page(SCHEMA_ROOT_PAGE)
    body = parse_page(raw, page_header_offset(SCHEMA_ROOT_PAGE))
    pool.unpin(SCHEMA_ROOT_PAGE)
    rows = []
    for cell in body.cells:
        _, _, local_payload, _ = decode_leaf_table_cell(bytes(cell))
        rows.append(decode_record(local_payload))
    return rows




def _write_raw_catalog_row(pager: Pager, pool: BufferPool, rowid: int, payload: bytes) -> None:
    """Put an arbitrary (possibly malformed) row on page 1, for corruption tests."""
    with pool.pinned(SCHEMA_ROOT_PAGE, dirty=True) as raw:
        body = parse_page(raw, page_header_offset(SCHEMA_ROOT_PAGE))
        body.insert_cell(len(body.cells), encode_leaf_table_cell(rowid, len(payload), payload))
        write_page_body(raw, body)




# =====================================================================
# Empty catalog and basic creation
# =====================================================================




def test_new_file_loads_an_empty_catalog(tmp_path) -> None:
    pager = Pager.create(tmp_path / "t.db")
    pool = BufferPool(pager)
    catalog = _catalog(pager, pool)
    assert catalog.list_tables() == ()
    pager.close()




def test_create_table_allocates_an_empty_leaf_root(tmp_path) -> None:
    pager = Pager.create(tmp_path / "t.db")
    pool = BufferPool(pager)
    catalog = _catalog(pager, pool)


    schema = _create_table(catalog, _USERS_SQL)


    raw = pool.get_page(schema.root_page)
    body = parse_page(raw)
    pool.unpin(schema.root_page)
    assert body.page_type is PageType.LEAF_TABLE
    assert body.cell_count == 0
    pager.close()




def test_create_table_writes_the_exact_five_column_row(tmp_path) -> None:
    pager = Pager.create(tmp_path / "t.db")
    pool = BufferPool(pager)
    catalog = _catalog(pager, pool)


    schema = _create_table(catalog, _USERS_SQL)


    rows = _raw_catalog_rows(pager, pool)
    assert rows == [("table", "users", "users", schema.root_page, _USERS_SQL)]
    pager.close()




def test_get_table_returns_what_create_table_returned(tmp_path) -> None:
    pager = Pager.create(tmp_path / "t.db")
    pool = BufferPool(pager)
    catalog = _catalog(pager, pool)


    created = _create_table(catalog, _USERS_SQL)
    assert catalog.get_table("users") == created




def test_list_tables_is_sorted_by_casefolded_name(tmp_path) -> None:
    pager = Pager.create(tmp_path / "t.db")
    pool = BufferPool(pager)
    catalog = _catalog(pager, pool)


    _create_table(catalog, "CREATE TABLE Zebra (id INTEGER)")
    _create_table(catalog, "CREATE TABLE apple (id INTEGER)")
    _create_table(catalog, "CREATE TABLE Mango (id INTEGER)")


    assert [t.name for t in catalog.list_tables()] == ["apple", "Mango", "Zebra"]




# =====================================================================
# Case-insensitivity and name conflicts
# =====================================================================




def test_names_are_case_insensitive_but_preserve_original_spelling(tmp_path) -> None:
    pager = Pager.create(tmp_path / "t.db")
    pool = BufferPool(pager)
    catalog = _catalog(pager, pool)


    _create_table(catalog, "CREATE TABLE MyTable (id INTEGER)")


    found = catalog.get_table("mytable")
    assert found.name == "MyTable"




def test_duplicate_name_raises_without_leaking_a_page(tmp_path) -> None:
    pager = Pager.create(tmp_path / "t.db")
    pool = BufferPool(pager)
    catalog = _catalog(pager, pool)


    _create_table(catalog, _USERS_SQL)
    page_count_after_success = pager.page_count


    with pytest.raises(TableAlreadyExistsError):
        _create_table(catalog, "CREATE TABLE USERS (id INTEGER)")


    assert pager.page_count == page_count_after_success




def test_reserved_sqlite_prefix_raises_without_leaking_a_page(tmp_path) -> None:
    pager = Pager.create(tmp_path / "t.db")
    pool = BufferPool(pager)
    catalog = _catalog(pager, pool)
    page_count_before = pager.page_count


    with pytest.raises(UnsupportedFeatureError):
        _create_table(catalog, "CREATE TABLE sqlite_stats (id INTEGER)")


    assert pager.page_count == page_count_before




def test_get_unknown_table_raises(tmp_path) -> None:
    pager = Pager.create(tmp_path / "t.db")
    pool = BufferPool(pager)
    catalog = _catalog(pager, pool)


    with pytest.raises(TableNotFoundError):
        catalog.get_table("ghost")
    pager.close()




# =====================================================================
# Durability: close, reopen, reconstruct
# =====================================================================




def test_close_and_reopen_reconstructs_an_identical_schema(tmp_path) -> None:
    path = tmp_path / "t.db"
    pager = Pager.create(path)
    pool = BufferPool(pager)
    catalog = _catalog(pager, pool)


    created = _create_table(catalog, _USERS_SQL)
    pool.flush_all()
    pager.close()


    reopened_pager = Pager.open(path)
    reopened_pool = BufferPool(reopened_pager)
    reopened_catalog = _catalog(reopened_pager, reopened_pool)


    assert reopened_catalog.get_table("users") == created
    reopened_pager.close()




def test_close_and_reopen_with_multiple_tables(tmp_path) -> None:
    path = tmp_path / "t.db"
    pager = Pager.create(path)
    pool = BufferPool(pager)
    catalog = _catalog(pager, pool)


    _create_table(catalog, _USERS_SQL)
    _create_table(catalog, "CREATE TABLE posts (id INTEGER, body TEXT)")
    pool.flush_all()
    pager.close()


    reopened_pager = Pager.open(path)
    reopened_pool = BufferPool(reopened_pager)
    reopened_catalog = _catalog(reopened_pager, reopened_pool)


    assert [t.name for t in reopened_catalog.list_tables()] == ["posts", "users"]
    reopened_pager.close()




# =====================================================================
# Corruption: a malformed catalog row must fail cleanly, not crash
# =====================================================================




def test_malformed_catalog_row_raises_corrupt_database_error_not_index_error(tmp_path) -> None:
    pager = Pager.create(tmp_path / "t.db")
    pool = BufferPool(pager)


    # Hand-write a row with too few columns directly onto page 1 -- something
    # load() must reject, not something create_table() would ever produce.
    _write_raw_catalog_row(pager, pool, 1, encode_record(("table", "broken")))


    catalog = Catalog(pager, pool)
    with pytest.raises(CorruptDatabaseError):
        catalog.load()
    pager.close()




def test_catalog_row_naming_a_nonexistent_root_page_raises(tmp_path) -> None:
    pager = Pager.create(tmp_path / "t.db")
    pool = BufferPool(pager)


    bad = encode_record(("table", "ghost", "ghost", 9999, "CREATE TABLE ghost (id INTEGER)"))
    _write_raw_catalog_row(pager, pool, 1, bad)


    catalog = Catalog(pager, pool)
    with pytest.raises(CorruptDatabaseError):
        catalog.load()
    pager.close()




def test_catalog_row_whose_sql_creates_a_different_table_raises(tmp_path) -> None:
    pager = Pager.create(tmp_path / "t.db")
    pool = BufferPool(pager)


    mismatched = encode_record(("table", "users", "users", 1, "CREATE TABLE posts (id INTEGER)"))
    _write_raw_catalog_row(pager, pool, 1, mismatched)


    catalog = Catalog(pager, pool)
    with pytest.raises(CorruptDatabaseError):
        catalog.load()
    pager.close()




def test_non_table_non_index_schema_object_raises_unsupported_feature(tmp_path) -> None:
    pager = Pager.create(tmp_path / "t.db")
    pool = BufferPool(pager)


    a_view = encode_record(("view", "v_users", "users", 1, "CREATE VIEW v_users AS SELECT * FROM users"))
    _write_raw_catalog_row(pager, pool, 1, a_view)


    catalog = Catalog(pager, pool)
    with pytest.raises(UnsupportedFeatureError):
        catalog.load()
    pager.close()




def test_index_row_naming_a_nonexistent_table_raises_corrupt(tmp_path) -> None:
    """"index" is now a supported type -- but an index whose table was
    never created (or comes AFTER it in rowid order, which can't happen
    through create_index() but must still be rejected defensively) is
    still a corrupt file, not an unsupported feature.
    """
    pager = Pager.create(tmp_path / "t.db")
    pool = BufferPool(pager)


    an_index = encode_record(("index", "idx_users", "users", 1, "CREATE INDEX idx_users ON users (id)"))
    _write_raw_catalog_row(pager, pool, 1, an_index)


    catalog = Catalog(pager, pool)
    with pytest.raises(CorruptDatabaseError):
        catalog.load()
    pager.close()




def test_page_1_fills_up_with_a_clean_error_not_corruption(tmp_path) -> None:
    """The documented scope limit: the catalog lives on page 1 alone and never
    splits, so it has a hard ceiling. Running past it must raise, not corrupt.
    """
    pager = Pager.create(tmp_path / "t.db")
    pool = BufferPool(pager, capacity=64)
    catalog = _catalog(pager, pool)


    created = 0
    with pytest.raises(PageFullError):
        for i in range(500):
            sql = f"CREATE TABLE t{i} (id INTEGER, name TEXT, age INTEGER)"
            _create_table(catalog, sql)
            created += 1


    assert created == 53  # matches the number quoted in catalog.py's docstring


    # The failed create must not have damaged what was already there.
    pool.flush_all()
    reloaded = Catalog(pager, pool)
    reloaded.load()
    assert len(reloaded.list_tables()) == created
    pager.close()




def test_page_1_filling_up_leaves_a_file_sqlite3_still_calls_ok(tmp_path) -> None:
    """The failure path must not leak the root page it allocated.


    Reloading the catalog isn't enough to catch this -- the 53 surviving
    tables are readable either way. Only sqlite3 notices, because an
    unreferenced page is exactly what integrity_check looks for:
    "Page 55 is never used".
    """
    path = tmp_path / "t.db"
    pager = Pager.create(path)
    pool = BufferPool(pager, capacity=64)
    catalog = _catalog(pager, pool)


    with pytest.raises(PageFullError):
        for i in range(500):
            sql = f"CREATE TABLE t{i} (id INTEGER, name TEXT, age INTEGER)"
            _create_table(catalog, sql)


    pool.flush_all()
    pager.close()


    result = subprocess.run(
        ["sqlite3", str(path), "PRAGMA integrity_check;"],
        capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == "ok"




# =====================================================================
# sqlite3 itself must be able to read what we wrote
# =====================================================================




def test_sqlite3_sees_the_created_table_schema(tmp_path) -> None:
    path = tmp_path / "t.db"
    pager = Pager.create(path)
    pool = BufferPool(pager)
    catalog = _catalog(pager, pool)
    _create_table(catalog, _USERS_SQL)
    pool.flush_all()
    pager.close()


    result = subprocess.run(
        ["sqlite3", str(path), ".schema users"],
        capture_output=True, text=True, check=True,
    )
    assert "users" in result.stdout




def test_sqlite3_integrity_check_passes_after_create_table(tmp_path) -> None:
    path = tmp_path / "t.db"
    pager = Pager.create(path)
    pool = BufferPool(pager)
    catalog = _catalog(pager, pool)
    _create_table(catalog, _USERS_SQL)
    pool.flush_all()
    pager.close()


    result = subprocess.run(
        ["sqlite3", str(path), "PRAGMA integrity_check;"],
        capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == "ok"




# =====================================================================
# CREATE INDEX and backfill
# =====================================================================




def test_create_index_on_an_empty_table_allocates_an_empty_leaf_root(tmp_path) -> None:
    pager = Pager.create(tmp_path / "t.db")
    pool = BufferPool(pager)
    catalog = _catalog(pager, pool)
    _create_table(catalog, _USERS_SQL)


    index = _create_index(catalog, "CREATE INDEX idx_name ON users (name)")


    raw = pool.get_page(index.root_page)
    body = parse_page(raw)
    pool.unpin(index.root_page)
    assert body.page_type is PageType.LEAF_INDEX
    assert body.cell_count == 0
    pager.close()




def test_create_index_writes_the_exact_five_column_row(tmp_path) -> None:
    pager = Pager.create(tmp_path / "t.db")
    pool = BufferPool(pager)
    catalog = _catalog(pager, pool)
    _create_table(catalog, _USERS_SQL)
    sql = "CREATE INDEX idx_name ON users (name)"


    index = _create_index(catalog, sql)


    rows = _raw_catalog_rows(pager, pool)
    assert rows[-1] == ("index", "idx_name", "users", index.root_page, sql)
    pager.close()




def test_create_index_backfills_every_existing_row(tmp_path) -> None:
    pager = Pager.create(tmp_path / "t.db")
    pool = BufferPool(pager)
    catalog = _catalog(pager, pool)
    table = _create_table(catalog, _USERS_SQL)
    _insert_row(pager, pool, table, 1, (1, "Hanoi", 30))
    _insert_row(pager, pool, table, 2, (2, "Hue", 25))
    _insert_row(pager, pool, table, 3, (3, "Danang", 40))


    index = _create_index(catalog, "CREATE INDEX idx_name ON users (name)")


    ibt = IndexBTree(pager, pool, index.root_page, n_key_columns=1, unique=False)
    assert list(ibt.scan()) == [(["Danang"], 3), (["Hanoi"], 1), (["Hue"], 2)]
    pager.close()




def test_create_index_backfill_of_large_keys_survives_a_multi_level_split(tmp_path) -> None:
    """This workload used to be the one-level-split ceiling: a backfill of
    120 rows with 3000-byte keys overflowed IndexBTree.insert()'s cap, and
    create_index() freed its root page on a best-effort basis and re-raised
    PageFullError. With _promote_separator() the backfill completes instead,
    building a multi-level index.


    It is kept because it is the exact shape that caught the zero-cell
    interior page bug: 3000-byte keys leave only a handful of cells per
    interior page, so its splits land on split_interior_cells' peel path,
    where an empty half was previously possible. sqlite3 reads a zero-cell
    interior page as "database disk image is malformed" -- a hard parse
    error, not a soft integrity finding -- so only a real sqlite3 read
    catches it.
    """
    path = tmp_path / "t.db"
    db = quilldb.connect(str(path))
    db.execute("CREATE TABLE t (v TEXT)")


    random.seed(3)
    values = ["".join(random.choices("abcdefghijklmnopqrstuvwxyz", k=3000)) for _ in range(120)]
    for value in values:
        db.execute("INSERT INTO t VALUES (?)", (value,))


    db.execute("CREATE INDEX idx_v ON t (v)")  # must not raise


    # Every row is still reachable through the index it just built.
    probe = values[57]
    assert len(db.execute("SELECT v FROM t WHERE v = ?", (probe,)).fetchall()) == 1
    assert len(db.execute("SELECT v FROM t WHERE v >= ?", ("",)).fetchall()) == 120
    db.close()


    # check=False, unlike the other integrity_check calls in this file, is
    # deliberate: a malformed file makes sqlite3 exit non-zero with the
    # reason on STDERR, and check=True would raise CalledProcessError before
    # the assert below could report it. This is the test that caught the
    # zero-cell interior page, and it caught it by printing that stderr.
    result = subprocess.run(
        ["sqlite3", str(path), "PRAGMA integrity_check;"],
        capture_output=True, text=True, check=False,
    )
    assert result.stdout.strip() == "ok", result.stdout.strip() or result.stderr.strip()


def test_create_index_on_missing_table_raises(tmp_path) -> None:
    pager = Pager.create(tmp_path / "t.db")
    pool = BufferPool(pager)
    catalog = _catalog(pager, pool)


    with pytest.raises(TableNotFoundError):
        _create_index(catalog, "CREATE INDEX idx_name ON ghost (name)")
    pager.close()




def test_create_index_on_missing_column_raises_without_leaking_a_page(tmp_path) -> None:
    pager = Pager.create(tmp_path / "t.db")
    pool = BufferPool(pager)
    catalog = _catalog(pager, pool)
    _create_table(catalog, _USERS_SQL)
    page_count_before = pager.page_count


    with pytest.raises(ColumnNotFoundError):
        _create_index(catalog, "CREATE INDEX idx_ghost ON users (ghost)")


    assert pager.page_count == page_count_before
    pager.close()




def test_create_index_duplicate_name_raises_without_leaking_a_page(tmp_path) -> None:
    pager = Pager.create(tmp_path / "t.db")
    pool = BufferPool(pager)
    catalog = _catalog(pager, pool)
    _create_table(catalog, _USERS_SQL)
    _create_index(catalog, "CREATE INDEX idx_name ON users (name)")
    page_count_before = pager.page_count


    with pytest.raises(IndexAlreadyExistsError):
        _create_index(catalog, "CREATE INDEX idx_name ON users (age)")


    assert pager.page_count == page_count_before
    pager.close()




def test_create_index_colliding_with_a_table_name_raises(tmp_path) -> None:
    pager = Pager.create(tmp_path / "t.db")
    pool = BufferPool(pager)
    catalog = _catalog(pager, pool)
    _create_table(catalog, _USERS_SQL)


    with pytest.raises(IndexAlreadyExistsError):
        _create_index(catalog, "CREATE INDEX users ON users (name)")
    pager.close()




def test_create_index_reserved_prefix_raises_without_leaking_a_page(tmp_path) -> None:
    pager = Pager.create(tmp_path / "t.db")
    pool = BufferPool(pager)
    catalog = _catalog(pager, pool)
    _create_table(catalog, _USERS_SQL)
    page_count_before = pager.page_count


    with pytest.raises(UnsupportedFeatureError):
        _create_index(catalog, "CREATE INDEX sqlite_idx ON users (name)")


    assert pager.page_count == page_count_before
    pager.close()




def test_indexes_for_returns_only_that_tables_indexes_in_creation_order(tmp_path) -> None:
    pager = Pager.create(tmp_path / "t.db")
    pool = BufferPool(pager)
    catalog = _catalog(pager, pool)
    _create_table(catalog, _USERS_SQL)
    _create_table(catalog, "CREATE TABLE posts (id INTEGER, title TEXT)")


    _create_index(catalog, "CREATE INDEX idx_age ON users (age)")
    _create_index(catalog, "CREATE INDEX idx_title ON posts (title)")
    _create_index(catalog, "CREATE INDEX idx_name ON users (name)")


    assert [i.name for i in catalog.indexes_for("users")] == ["idx_age", "idx_name"]
    assert [i.name for i in catalog.indexes_for("posts")] == ["idx_title"]
    assert catalog.indexes_for("ghost") == ()
    pager.close()




# ---- UNIQUE indexes: the pre-check must run before anything is allocated --




def test_unique_index_on_clean_data_backfills_successfully(tmp_path) -> None:
    pager = Pager.create(tmp_path / "t.db")
    pool = BufferPool(pager)
    catalog = _catalog(pager, pool)
    table = _create_table(catalog, _USERS_SQL)
    _insert_row(pager, pool, table, 1, (1, "Hanoi", 30))
    _insert_row(pager, pool, table, 2, (2, "Hue", 25))


    index = _create_index(catalog, "CREATE UNIQUE INDEX idx_name ON users (name)")
    assert index.unique is True




def test_unique_index_rejects_a_pre_existing_duplicate_before_allocating_anything(tmp_path) -> None:
    pager = Pager.create(tmp_path / "t.db")
    pool = BufferPool(pager)
    catalog = _catalog(pager, pool)
    table = _create_table(catalog, _USERS_SQL)
    _insert_row(pager, pool, table, 1, (1, "Hanoi", 30))
    _insert_row(pager, pool, table, 2, (2, "Hanoi", 25))
    page_count_before = pager.page_count


    with pytest.raises(UniqueViolationError) as excinfo:
        _create_index(catalog, "CREATE UNIQUE INDEX idx_name ON users (name)")


    assert excinfo.value.key == ("Hanoi",)
    assert pager.page_count == page_count_before  # nothing allocated at all
    assert catalog.indexes_for("users") == ()




def test_unique_index_treats_int_and_float_as_the_same_key(tmp_path) -> None:
    pager = Pager.create(tmp_path / "t.db")
    pool = BufferPool(pager)
    catalog = _catalog(pager, pool)
    scores = _create_table(catalog, "CREATE TABLE scores (id INTEGER, score REAL)")
    _insert_row(pager, pool, scores, 1, (1, 4))
    _insert_row(pager, pool, scores, 2, (2, 4.0))


    with pytest.raises(UniqueViolationError):
        _create_index(catalog, "CREATE UNIQUE INDEX idx_score ON scores (score)")




def test_unique_index_allows_many_nulls(tmp_path) -> None:
    pager = Pager.create(tmp_path / "t.db")
    pool = BufferPool(pager)
    catalog = _catalog(pager, pool)
    table = _create_table(catalog, _USERS_SQL)
    _insert_row(pager, pool, table, 1, (1, None, 30))
    _insert_row(pager, pool, table, 2, (2, None, 25))


    index = _create_index(catalog, "CREATE UNIQUE INDEX idx_name ON users (name)")  # must not raise
    assert index.unique is True




# ---- durability ------------------------------------------------------




def test_close_and_reopen_reconstructs_an_index(tmp_path) -> None:
    path = tmp_path / "t.db"
    pager = Pager.create(path)
    pool = BufferPool(pager)
    catalog = _catalog(pager, pool)
    table = _create_table(catalog, _USERS_SQL)
    _insert_row(pager, pool, table, 1, (1, "Hanoi", 30))
    created = _create_index(catalog, "CREATE INDEX idx_name ON users (name)")
    pool.flush_all()
    pager.close()


    reopened_pager = Pager.open(path)
    reopened_pool = BufferPool(reopened_pager)
    reopened_catalog = _catalog(reopened_pager, reopened_pool)


    assert catalog is not reopened_catalog
    reconstructed = reopened_catalog.indexes_for("users")
    assert len(reconstructed) == 1
    assert reconstructed[0] == created
    reopened_pager.close()




# ---- sqlite3 itself must be able to read what we wrote ----------------




def test_sqlite3_sees_the_created_index_schema(tmp_path) -> None:
    path = tmp_path / "t.db"
    pager = Pager.create(path)
    pool = BufferPool(pager)
    catalog = _catalog(pager, pool)
    _create_table(catalog, _USERS_SQL)
    _create_index(catalog, "CREATE INDEX idx_name ON users (name)")
    pool.flush_all()
    pager.close()


    result = subprocess.run(
        ["sqlite3", str(path), ".schema users"],
        capture_output=True, text=True, check=True,
    )
    assert "idx_name" in result.stdout




def test_sqlite3_integrity_check_passes_after_create_index_with_backfill(tmp_path) -> None:
    path = tmp_path / "t.db"
    pager = Pager.create(path)
    pool = BufferPool(pager)
    catalog = _catalog(pager, pool)
    table = _create_table(catalog, _USERS_SQL)
    for i in range(1, 21):
        _insert_row(pager, pool, table, i, (i, f"user{i}", 20 + i))
    _create_index(catalog, "CREATE UNIQUE INDEX idx_name ON users (name)")
    pool.flush_all()
    pager.close()


    result = subprocess.run(
        ["sqlite3", str(path), "PRAGMA integrity_check;"],
        capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == "ok"