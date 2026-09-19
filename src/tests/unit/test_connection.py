"""Connection/Cursor tests: the public surface, not the internals below it.


Everything here goes through `quilldb.connect()` the way an actual caller
would -- no Pager, BufferPool, or Catalog constructed directly. Lower
layers already have their own focused tests; this file is about the seam
those layers are hidden behind: one execute() shape for three different
statement kinds, streaming that survives the handoff to Cursor, and the
DB-API fetch/close contract from docs/implementation/week-3-sql.md §20.
"""


from pathlib import Path

import pytest

import quilldb
from quilldb.btree.index import IndexBTree
from quilldb.errors import (
    ColumnNotFoundError,
    ParameterCountError,
    TableNotFoundError,
    TypeMismatchError,
    UniqueViolationError,
)
from quilldb.plan.statistics import default_table_stats

_USERS_SQL = "CREATE TABLE users (id INTEGER, name TEXT, age INTEGER)"




def _outstanding_pins(connection: quilldb.Connection) -> int:
    return sum(entry.pin_count for entry in connection.pool._cache.values())




# =====================================================================
# The roadmap's four-line example, and :memory:
# =====================================================================




def test_the_public_four_line_example_runs() -> None:
    db = quilldb.connect(":memory:")
    db.execute(_USERS_SQL)
    db.execute("INSERT INTO users VALUES (?, ?, ?)", (1, "ada", 36))
    assert db.execute("SELECT name FROM users WHERE age > 30").fetchall() == [("ada",)]
    db.close()




def test_memory_creates_no_filesystem_entry(tmp_path: Path) -> None:
    db = quilldb.connect(":memory:")
    db.execute(_USERS_SQL)
    db.close()
    assert list(tmp_path.iterdir()) == []




def test_a_path_literally_named_memory_is_a_real_file(tmp_path: Path) -> None:
    """Only the exact string ":memory:" is special; a Path spelled the same
    way is an ordinary path on disk.
    """
    path = tmp_path / ":memory:"
    db = quilldb.connect(path)
    db.close()
    assert path.exists()




# =====================================================================
# CREATE TABLE survives close and reopen
# =====================================================================




def test_create_table_survives_close_and_reopen(tmp_path: Path) -> None:
    path = tmp_path / "demo.db"


    with quilldb.connect(path) as db:
        db.execute(_USERS_SQL)
        db.execute("INSERT INTO users VALUES (?, ?, ?)", (1, "ada", 36))
        db.execute("INSERT INTO users VALUES (?, ?, ?)", (2, "linus", None))


    with quilldb.connect(path) as db:
        cursor = db.execute("SELECT name FROM users WHERE age > ?", (30,))
        description = cursor.description
        assert description is not None
        assert description[0][0] == "name"
        assert cursor.fetchall() == [("ada",)]




def test_reopening_an_existing_file_does_not_recreate_it(tmp_path: Path) -> None:
    path = tmp_path / "demo.db"
    with quilldb.connect(path) as db:
        db.execute(_USERS_SQL)


    with quilldb.connect(path) as db:
        db.execute("INSERT INTO users VALUES (?, ?, ?)", (1, "ada", 36))


    with quilldb.connect(path) as db:
        assert db.execute("SELECT * FROM users").fetchall() == [(1, "ada", 36)]




# =====================================================================
# rowcount per statement kind
# =====================================================================




def test_rowcount_is_zero_for_create_table() -> None:
    db = quilldb.connect(":memory:")
    assert db.execute(_USERS_SQL).rowcount == 0
    db.close()




def test_rowcount_is_one_for_insert() -> None:
    db = quilldb.connect(":memory:")
    db.execute(_USERS_SQL)
    assert db.execute("INSERT INTO users VALUES (?, ?, ?)", (1, "ada", 36)).rowcount == 1
    db.close()




def test_rowcount_is_negative_one_for_select() -> None:
    db = quilldb.connect(":memory:")
    db.execute(_USERS_SQL)
    assert db.execute("SELECT * FROM users").rowcount == -1
    db.close()




# =====================================================================
# description
# =====================================================================




def test_description_is_none_for_create_table_and_insert() -> None:
    db = quilldb.connect(":memory:")
    assert db.execute(_USERS_SQL).description is None
    assert db.execute("INSERT INTO users VALUES (?, ?, ?)", (1, "ada", 36)).description is None
    db.close()




def test_description_names_a_plain_column() -> None:
    db = quilldb.connect(":memory:")
    db.execute(_USERS_SQL)
    cursor = db.execute("SELECT name, age FROM users")
    description = cursor.description
    assert description is not None
    assert description[0][0] == "name"
    assert description[1][0] == "age"
    db.close()




def test_description_reconstructs_a_computed_expression() -> None:
    db = quilldb.connect(":memory:")
    db.execute(_USERS_SQL)
    cursor = db.execute("SELECT age + 1 FROM users")
    description = cursor.description
    assert description is not None
    assert description[0][0] == "age + 1"
    db.close()




def test_description_expands_star() -> None:
    db = quilldb.connect(":memory:")
    db.execute(_USERS_SQL)
    cursor = db.execute("SELECT * FROM users")
    description = cursor.description
    assert description is not None
    assert [column[0] for column in description] == ["id", "name", "age"]
    db.close()




# =====================================================================
# fetchone / fetchmany / fetchall
# =====================================================================




def test_select_on_an_empty_table_returns_no_rows() -> None:
    db = quilldb.connect(":memory:")
    db.execute(_USERS_SQL)
    assert db.execute("SELECT * FROM users").fetchall() == []
    db.close()




def test_fetchone_keeps_returning_none_after_exhaustion() -> None:
    db = quilldb.connect(":memory:")
    db.execute(_USERS_SQL)
    db.execute("INSERT INTO users VALUES (?, ?, ?)", (1, "ada", 36))


    cursor = db.execute("SELECT name FROM users")
    assert cursor.fetchone() == ("ada",)
    assert cursor.fetchone() is None
    assert cursor.fetchone() is None
    db.close()




def test_fetchmany_default_size_is_one() -> None:
    db = quilldb.connect(":memory:")
    db.execute(_USERS_SQL)
    for row in [(1, "ada", 36), (2, "bob", 41), (3, "amy", 20)]:
        db.execute("INSERT INTO users VALUES (?, ?, ?)", row)


    cursor = db.execute("SELECT name FROM users")
    assert cursor.fetchmany() == [("ada",)]
    assert cursor.fetchmany(2) == [("bob",), ("amy",)]
    assert cursor.fetchmany(2) == []
    db.close()




def test_fetchmany_zero_returns_an_empty_list_without_consuming_a_row() -> None:
    db = quilldb.connect(":memory:")
    db.execute(_USERS_SQL)
    db.execute("INSERT INTO users VALUES (?, ?, ?)", (1, "ada", 36))


    cursor = db.execute("SELECT name FROM users")
    assert cursor.fetchmany(0) == []
    assert cursor.fetchone() == ("ada",)
    db.close()




def test_fetchmany_negative_size_raises() -> None:
    db = quilldb.connect(":memory:")
    db.execute(_USERS_SQL)
    cursor = db.execute("SELECT * FROM users")
    with pytest.raises(ValueError):
        cursor.fetchmany(-1)
    db.close()




def test_fetchall_drains_every_remaining_row() -> None:
    db = quilldb.connect(":memory:")
    db.execute(_USERS_SQL)
    for row in [(1, "ada", 36), (2, "bob", 41), (3, "amy", 20)]:
        db.execute("INSERT INTO users VALUES (?, ?, ?)", row)


    cursor = db.execute("SELECT name FROM users")
    assert cursor.fetchone() == ("ada",)
    assert cursor.fetchall() == [("bob",), ("amy",)]
    db.close()




def test_a_failed_fetch_closes_the_cursor_instead_of_masking_the_error() -> None:
    """A row that raises leaves the cursor CLOSED, not merely exhausted --
    otherwise a caller that catches the error and keeps pulling would see a
    plain None on the next call, indistinguishable from a query that simply
    ran out of rows.
    """
    db = quilldb.connect(":memory:")
    db.execute(_USERS_SQL)
    db.execute("INSERT INTO users VALUES (?, ?, ?)", (1, "poison", None))


    cursor = db.execute("SELECT name + 1 FROM users")
    with pytest.raises(TypeMismatchError):
        cursor.fetchone()
    with pytest.raises(ValueError):
        cursor.fetchone()
    db.close()




# =====================================================================
# close()
# =====================================================================




def test_fetching_after_close_raises() -> None:
    db = quilldb.connect(":memory:")
    db.execute(_USERS_SQL)
    cursor = db.execute("SELECT * FROM users")
    cursor.close()
    with pytest.raises(ValueError):
        cursor.fetchone()
    db.close()




def test_cursor_close_is_idempotent() -> None:
    db = quilldb.connect(":memory:")
    db.execute(_USERS_SQL)
    cursor = db.execute("SELECT * FROM users")
    cursor.close()
    cursor.close()
    db.close()




def test_connection_close_is_idempotent() -> None:
    db = quilldb.connect(":memory:")
    db.close()
    db.close()




def test_executing_after_connection_close_raises() -> None:
    db = quilldb.connect(":memory:")
    db.close()
    with pytest.raises(ValueError):
        db.execute(_USERS_SQL)




# =====================================================================
# One open cursor per connection, and the pins that come with it
# =====================================================================




def test_a_new_execute_closes_the_previous_open_cursor() -> None:
    db = quilldb.connect(":memory:")
    db.execute(_USERS_SQL)
    db.execute("INSERT INTO users VALUES (?, ?, ?)", (1, "ada", 36))


    first = db.execute("SELECT * FROM users")
    assert _outstanding_pins(db) > 0  # the SeqScan is holding its path


    second = db.execute("SELECT * FROM users")  # abandons `first` mid-scan
    with pytest.raises(ValueError):  # `first` was closed out from under it
        first.fetchone()
    assert second.fetchall() == [(1, "ada", 36)]
    db.close()




def test_no_pins_survive_a_full_session(tmp_path: Path) -> None:
    db = quilldb.connect(tmp_path / "demo.db")
    db.execute(_USERS_SQL)
    for row in [(1, "ada", 36), (2, "bob", 41)]:
        db.execute("INSERT INTO users VALUES (?, ?, ?)", row)
    db.execute("SELECT * FROM users").fetchall()
    assert _outstanding_pins(db) == 0
    db.close()




# =====================================================================
# Failures happen before any cursor opens
# =====================================================================




def test_unknown_table_raises_before_a_cursor_opens() -> None:
    db = quilldb.connect(":memory:")
    with pytest.raises(TableNotFoundError):
        db.execute("SELECT * FROM ghosts")
    assert _outstanding_pins(db) == 0
    db.close()




def test_unknown_column_raises_before_a_cursor_opens() -> None:
    db = quilldb.connect(":memory:")
    db.execute(_USERS_SQL)
    with pytest.raises(ColumnNotFoundError):
        db.execute("SELECT ghost FROM users")
    assert _outstanding_pins(db) == 0
    db.close()




def test_wrong_parameter_count_raises_before_a_cursor_opens() -> None:
    db = quilldb.connect(":memory:")
    db.execute(_USERS_SQL)
    with pytest.raises(ParameterCountError):
        db.execute("SELECT * FROM users WHERE age > ?")
    assert _outstanding_pins(db) == 0
    db.close()




# =====================================================================
# CREATE INDEX, through the same execute() seam as CREATE TABLE
# =====================================================================




def test_create_index_runs_through_execute_and_leaves_no_pins() -> None:
    db = quilldb.connect(":memory:")
    db.execute(_USERS_SQL)
    cursor = db.execute("CREATE INDEX idx_name ON users (name)")
    assert cursor.rowcount == 0
    assert cursor.description is None
    assert _outstanding_pins(db) == 0
    db.close()




def test_create_index_backfills_rows_inserted_before_it_existed() -> None:
    db = quilldb.connect(":memory:")
    db.execute(_USERS_SQL)
    for row in [(1, "ada", 36), (2, "bob", 41)]:
        db.execute("INSERT INTO users VALUES (?, ?, ?)", row)


    db.execute("CREATE UNIQUE INDEX idx_name ON users (name)")


    # No index-aware query path yet (that's step 2) -- this just proves the
    # backfill didn't corrupt anything a plain SELECT can already see.
    assert db.execute("SELECT name FROM users WHERE age > 30").fetchall() == [("ada",), ("bob",)]
    db.close()




def test_create_index_survives_close_and_reopen(tmp_path: Path) -> None:
    path = tmp_path / "demo.db"
    with quilldb.connect(path) as db:
        db.execute(_USERS_SQL)
        db.execute("INSERT INTO users VALUES (?, ?, ?)", (1, "ada", 36))
        db.execute("CREATE INDEX idx_name ON users (name)")


    with quilldb.connect(path) as db:
        assert [i.name for i in db.catalog.indexes_for("users")] == ["idx_name"]




def test_create_unique_index_on_duplicate_data_raises_before_a_cursor_opens() -> None:
    db = quilldb.connect(":memory:")
    db.execute(_USERS_SQL)
    db.execute("INSERT INTO users VALUES (?, ?, ?)", (1, "ada", 36))
    db.execute("INSERT INTO users VALUES (?, ?, ?)", (2, "ada", 41))


    with pytest.raises(UniqueViolationError):
        db.execute("CREATE UNIQUE INDEX idx_name ON users (name)")
    assert _outstanding_pins(db) == 0
    assert db.catalog.indexes_for("users") == ()
    db.close()




def test_create_index_on_missing_table_raises_before_a_cursor_opens() -> None:
    db = quilldb.connect(":memory:")
    with pytest.raises(TableNotFoundError):
        db.execute("CREATE INDEX idx_ghost ON ghosts (col)")
    assert _outstanding_pins(db) == 0
    db.close()




# =====================================================================
# Insert index maintenance, through the same execute() seam
# =====================================================================




def test_insert_after_create_index_keeps_the_index_current() -> None:
    db = quilldb.connect(":memory:")
    db.execute(_USERS_SQL)
    db.execute("CREATE INDEX idx_name ON users (name)")
    db.execute("INSERT INTO users VALUES (?, ?, ?)", (1, "ada", 36))


    index = db.catalog.indexes_for("users")[0]
    assert list(IndexBTree(db.pager, db.pool, index.root_page, n_key_columns=1, unique=False).seek_eq(["ada"])) == [1]
    db.close()




def test_insert_violating_a_unique_index_raises_before_a_cursor_opens() -> None:
    db = quilldb.connect(":memory:")
    db.execute(_USERS_SQL)
    db.execute("CREATE UNIQUE INDEX idx_name ON users (name)")
    db.execute("INSERT INTO users VALUES (?, ?, ?)", (1, "ada", 36))


    with pytest.raises(UniqueViolationError):
        db.execute("INSERT INTO users VALUES (?, ?, ?)", (2, "ada", 41))


    assert _outstanding_pins(db) == 0
    assert db.execute("SELECT * FROM users").fetchall() == [(1, "ada", 36)]
    db.close()




# =====================================================================
# DELETE, through the same execute() seam
# =====================================================================




def test_delete_removes_matching_rows_and_reports_rowcount() -> None:
    db = quilldb.connect(":memory:")
    db.execute(_USERS_SQL)
    for row in [(1, "ada", 36), (2, "bob", 20), (3, "amy", 41)]:
        db.execute("INSERT INTO users VALUES (?, ?, ?)", row)


    cursor = db.execute("DELETE FROM users WHERE age < 30")
    assert cursor.rowcount == 1
    assert cursor.description is None
    assert db.execute("SELECT * FROM users").fetchall() == [(1, "ada", 36), (3, "amy", 41)]
    db.close()




def test_delete_with_no_where_removes_every_row_and_leaves_no_pins() -> None:
    db = quilldb.connect(":memory:")
    db.execute(_USERS_SQL)
    db.execute("INSERT INTO users VALUES (?, ?, ?)", (1, "ada", 36))


    cursor = db.execute("DELETE FROM users")
    assert cursor.rowcount == 1
    assert _outstanding_pins(db) == 0
    assert db.execute("SELECT * FROM users").fetchall() == []
    db.close()




def test_delete_keeps_an_index_current() -> None:
    db = quilldb.connect(":memory:")
    db.execute(_USERS_SQL)
    db.execute("CREATE INDEX idx_name ON users (name)")
    db.execute("INSERT INTO users VALUES (?, ?, ?)", (1, "ada", 36))


    db.execute("DELETE FROM users WHERE id = 1")


    index = db.catalog.indexes_for("users")[0]
    assert list(IndexBTree(db.pager, db.pool, index.root_page, n_key_columns=1, unique=False).seek_eq(["ada"])) == []
    db.close()




def test_delete_survives_close_and_reopen(tmp_path: Path) -> None:
    path = tmp_path / "demo.db"
    with quilldb.connect(path) as db:
        db.execute(_USERS_SQL)
        db.execute("INSERT INTO users VALUES (?, ?, ?)", (1, "ada", 36))
        db.execute("INSERT INTO users VALUES (?, ?, ?)", (2, "bob", 20))
        db.execute("DELETE FROM users WHERE id = 1")


    with quilldb.connect(path) as db:
        assert db.execute("SELECT * FROM users").fetchall() == [(2, "bob", 20)]




# =====================================================================
# UPDATE, through the same execute() seam
# =====================================================================




def test_update_sets_a_column_and_reports_rowcount() -> None:
    db = quilldb.connect(":memory:")
    db.execute(_USERS_SQL)
    db.execute("INSERT INTO users VALUES (?, ?, ?)", (1, "ada", 36))


    cursor = db.execute("UPDATE users SET age = 40 WHERE id = 1")
    assert cursor.rowcount == 1
    assert cursor.description is None
    assert db.execute("SELECT * FROM users").fetchall() == [(1, "ada", 40)]
    db.close()




def test_update_keeps_an_index_current() -> None:
    db = quilldb.connect(":memory:")
    db.execute(_USERS_SQL)
    db.execute("CREATE INDEX idx_name ON users (name)")
    db.execute("INSERT INTO users VALUES (?, ?, ?)", (1, "ada", 36))


    db.execute("UPDATE users SET name = 'ines' WHERE id = 1")


    index = db.catalog.indexes_for("users")[0]
    ibt = IndexBTree(db.pager, db.pool, index.root_page, n_key_columns=1, unique=False)
    assert list(ibt.seek_eq(["ada"])) == []
    assert list(ibt.seek_eq(["ines"])) == [1]
    db.close()




def test_update_violating_a_unique_index_raises_before_a_cursor_opens() -> None:
    db = quilldb.connect(":memory:")
    db.execute(_USERS_SQL)
    db.execute("CREATE UNIQUE INDEX idx_name ON users (name)")
    db.execute("INSERT INTO users VALUES (?, ?, ?)", (1, "ada", 36))
    db.execute("INSERT INTO users VALUES (?, ?, ?)", (2, "bob", 20))


    with pytest.raises(UniqueViolationError):
        db.execute("UPDATE users SET name = 'ada' WHERE id = 2")


    assert _outstanding_pins(db) == 0
    assert db.execute("SELECT * FROM users").fetchall() == [(1, "ada", 36), (2, "bob", 20)]
    db.close()




def test_update_survives_close_and_reopen(tmp_path: Path) -> None:
    path = tmp_path / "demo.db"
    with quilldb.connect(path) as db:
        db.execute(_USERS_SQL)
        db.execute("INSERT INTO users VALUES (?, ?, ?)", (1, "ada", 36))
        db.execute("UPDATE users SET age = 40 WHERE id = 1")


    with quilldb.connect(path) as db:
        assert db.execute("SELECT * FROM users").fetchall() == [(1, "ada", 40)]




# =====================================================================
# ANALYZE: stage-5 Step 6 -- bound, dispatched, and connected to real stats
# =====================================================================




def test_analyze_runs_through_execute_and_leaves_no_pins() -> None:
    db = quilldb.connect(":memory:")
    db.execute(_USERS_SQL)
    db.execute("INSERT INTO users VALUES (?, ?, ?)", (1, "ada", 36))


    cursor = db.execute("ANALYZE")
    assert cursor.rowcount == 0
    assert cursor.description is None
    assert _outstanding_pins(db) == 0
    assert db.stats.table_stats("users").row_count == 1
    db.close()




def test_analyze_with_a_target_measures_only_that_table() -> None:
    db = quilldb.connect(":memory:")
    db.execute(_USERS_SQL)
    db.execute("CREATE TABLE orders (id INTEGER, user_id INTEGER)")
    db.execute("INSERT INTO users VALUES (?, ?, ?)", (1, "ada", 36))


    db.execute("ANALYZE users")


    assert db.stats.table_stats("users").row_count == 1
    assert db.stats.table_stats("orders") == default_table_stats()  # never analyzed -- still the flat default
    db.close()




def test_analyze_unknown_target_raises_before_a_cursor_opens() -> None:
    db = quilldb.connect(":memory:")


    with pytest.raises(TableNotFoundError):
        db.execute("ANALYZE ghost")
    assert _outstanding_pins(db) == 0
    db.close()




def test_analyze_survives_close_and_reopen(tmp_path: Path) -> None:
    path = tmp_path / "demo.db"
    with quilldb.connect(path) as db:
        db.execute(_USERS_SQL)
        db.execute("INSERT INTO users VALUES (?, ?, ?)", (1, "ada", 36))
        db.execute("INSERT INTO users VALUES (?, ?, ?)", (2, "bob", 41))
        db.execute("ANALYZE")


    with quilldb.connect(path) as db:
        assert db.stats.table_stats("users").row_count == 2




def test_analyze_never_changes_query_results() -> None:
    """Chapter 12 §12.6 trap #3, at the Connection seam this time: whether
    or not ANALYZE has run, the same query returns the same rows --
    ANALYZE is only allowed to change which plan answers a query, never
    the answer itself.
    """
    db = quilldb.connect(":memory:")
    db.execute(_USERS_SQL)
    db.execute("CREATE INDEX idx_age ON users (age)")
    for row in [(1, "ada", 36), (2, "bob", 41), (3, "cleo", 22)]:
        db.execute("INSERT INTO users VALUES (?, ?, ?)", row)


    before = db.execute("SELECT * FROM users WHERE age > 30").fetchall()
    db.execute("ANALYZE")
    after = db.execute("SELECT * FROM users WHERE age > 30").fetchall()


    assert before == after == [(1, "ada", 36), (2, "bob", 41)]
    db.close()




# =====================================================================
# EXPLAIN / EXPLAIN ANALYZE: stage-5 Step 7
# =====================================================================


_USERS_WITH_AGE_INDEX_SQL = "CREATE INDEX idx_age ON users (age)"


def _connect_analyzed_users(n: int) -> quilldb.Connection:
    """A big-enough table that a real ANALYZE actually favors IndexScan
    over SeqScan for a selective equality (the same "too small to show a
    contrast" fact test_operators.py's own ANALYZE test runs into at a
    handful of rows -- see its docstring).
    """
    db = quilldb.connect(":memory:")
    db.execute(_USERS_SQL)
    db.execute(_USERS_WITH_AGE_INDEX_SQL)
    for i in range(1, n + 1):
        db.execute("INSERT INTO users VALUES (?, ?, ?)", (i, f"user_number_{i}_padded_for_size", i))
    db.execute("ANALYZE")
    return db




def test_explain_returns_one_row_one_column_without_running_the_query() -> None:
    db = _connect_analyzed_users(1000)


    cursor = db.execute("EXPLAIN SELECT * FROM users WHERE age = 7")
    assert _outstanding_pins(db) == 0  # nothing from `users` itself has been touched yet
    description = cursor.description
    assert description is not None
    assert description[0][0] == "QUERY PLAN"
    rows = cursor.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "Project\n└─ IndexScan idx_age (age = 7) est_rows=1 startup=8.00 cost=16.01"
    db.close()




def test_explain_shows_seq_scan_plainly_without_annotations() -> None:
    """Step 7 scopes the cost/row annotation to IndexScan specifically --
    a SeqScan's EXPLAIN line stays exactly what build_operator()'s own
    plain explain() already produced."""
    db = _connect_analyzed_users(1000)


    rows = db.execute("EXPLAIN SELECT * FROM users WHERE name = 'nobody'").fetchall()
    assert rows == [("Project\n└─ Filter\n   └─ SeqScan users",)]
    db.close()




def test_explain_unknown_table_raises_before_a_cursor_opens() -> None:
    db = quilldb.connect(":memory:")


    with pytest.raises(TableNotFoundError):
        db.execute("EXPLAIN SELECT * FROM ghost")
    assert _outstanding_pins(db) == 0
    db.close()




def test_explain_unknown_column_raises_before_a_cursor_opens() -> None:
    db = quilldb.connect(":memory:")
    db.execute(_USERS_SQL)


    with pytest.raises(ColumnNotFoundError):
        db.execute("EXPLAIN SELECT ghost FROM users")
    db.close()




def test_explain_analyze_runs_the_query_and_appends_actual_rows() -> None:
    db = _connect_analyzed_users(1000)


    rows = db.execute("EXPLAIN ANALYZE SELECT * FROM users WHERE age = 7").fetchall()
    assert len(rows) == 1
    lines = rows[0][0].split("\n")
    assert lines[0] == "Project"
    assert lines[1] == "└─ IndexScan idx_age (age = 7) est_rows=1 startup=8.00 cost=16.01"
    assert lines[2].startswith("actual_rows=1 elapsed=")
    db.close()




def test_explain_analyze_leaves_no_pins_after_draining() -> None:
    db = _connect_analyzed_users(1000)


    db.execute("EXPLAIN ANALYZE SELECT * FROM users WHERE age = 7").fetchall()
    assert _outstanding_pins(db) == 0
    db.close()




def test_plain_explain_does_not_run_the_query() -> None:
    """The other half of EXPLAIN ANALYZE's contrast: plain EXPLAIN must
    never execute the plan, so `actual_rows` never appears and nothing
    from `users` gets pinned even after fetchall() drains the one
    QUERY PLAN row.
    """
    db = _connect_analyzed_users(1000)


    rows = db.execute("EXPLAIN SELECT * FROM users WHERE age = 7").fetchall()
    assert "actual_rows" not in rows[0][0]
    assert _outstanding_pins(db) == 0
    db.close()