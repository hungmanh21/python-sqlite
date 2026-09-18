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
from quilldb.errors import (
    ColumnNotFoundError,
    ParameterCountError,
    TableNotFoundError,
    TypeMismatchError,
    UniqueViolationError,
)


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