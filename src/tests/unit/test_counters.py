"""The measurement surface chapter 19 SS19.2's benchmark reports from.


The contract that matters: a "page read" is a buffer-pool MISS. A hit
costs no I/O, so counting it would make the number depend on how warm the
cache happened to be rather than on the access path -- which is the whole
reason the benchmark reports page reads instead of seconds.
"""

import pathlib

import quilldb


def _build(path: pathlib.Path, rows: int, *, with_index: bool) -> None:
    db = quilldb.connect(str(path))
    db.execute("CREATE TABLE users (id INTEGER, email TEXT)")
    if with_index:
        db.execute("CREATE INDEX ix_email ON users (email)")
    for i in range(1, rows + 1):
        db.execute("INSERT INTO users VALUES (?, ?)", (i, f"u{i}@example.com"))
    db.close()


def test_reset_counters_zeroes_everything() -> None:
    db = quilldb.connect(":memory:")
    db.execute("CREATE TABLE t (id INTEGER)")
    db.execute("INSERT INTO t VALUES (1)")
    db.reset_counters()
    assert (db.pages_read, db.pages_cached, db.rows_examined) == (0, 0, 0)
    db.close()


def test_a_cache_hit_is_not_counted_as_a_page_read() -> None:
    """The distinction the whole metric rests on."""
    db = quilldb.connect(":memory:")
    db.execute("CREATE TABLE t (id INTEGER)")
    for i in range(1, 51):
        db.execute("INSERT INTO t VALUES (?)", (i,))


    # Everything is in the pool already, so re-scanning reads no pages.
    db.reset_counters()
    db.execute("SELECT * FROM t").fetchall()
    assert db.pages_read == 0
    assert db.pages_cached > 0
    db.close()


def test_rows_examined_counts_rows_looked_at_not_rows_returned(tmp_path) -> None:
    """A SeqScan examines the whole table however few rows match -- which
    is exactly the cost an index removes.
    """
    path = tmp_path / "t.db"
    _build(path, 200, with_index=False)


    db = quilldb.connect(str(path))
    db.reset_counters()
    rows = db.execute("SELECT id FROM users WHERE email = ?", ("u100@example.com",)).fetchall()
    assert len(rows) == 1
    assert db.rows_examined == 200
    db.close()




def test_an_index_examines_one_row_where_a_scan_examines_all(tmp_path) -> None:
    """The benchmark's claim, in miniature: same query, same data, and the
    index turns a whole-table examination into a single row.


    Note what is NOT asserted here. At 400 rows the table is about five
    pages, and descending an index costs pages of its own, so page reads
    do not separate at this size -- they can even tie. That is the honest
    shape of the result: an index saves ROWS EXAMINED immediately and
    PAGE READS only once the table is big enough for the descent to be
    cheaper than the scan. benchmarks/index_lookup.py carries the
    page-read claim, at 100,000 rows, where it is 2,622 against 7.
    """
    scanned = tmp_path / "scan.db"
    indexed = tmp_path / "index.db"
    _build(scanned, 400, with_index=False)
    _build(indexed, 400, with_index=True)


    results = []
    for path in (scanned, indexed):
        db = quilldb.connect(str(path))
        db.reset_counters()
        rows = db.execute("SELECT id FROM users WHERE email = ?", ("u200@example.com",)).fetchall()
        results.append((db.rows_examined, db.pages_read, rows))
        db.close()


    (scan_rows, scan_pages, scan_out), (index_rows, index_pages, index_out) = results
    assert scan_out == index_out == [(200,)]     # identical answers
    assert scan_rows == 400
    assert index_rows == 1
    assert index_pages <= scan_pages
