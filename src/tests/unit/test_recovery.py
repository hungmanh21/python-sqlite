"""Automatic hot-journal recovery (chapter 14, week5-transactions.md
session 5).

There is no FaultyFile yet -- that's session 6. "Crash" here means exactly
what session 5's own done-when criterion asks for: get a transaction past
commit_barrier() and onto disk, then abandon the connection without ever
reaching journal.delete() (the one thing Transaction.commit() does last),
and check that reopening the same path is indistinguishable from the
transaction never having run.
"""

import pathlib

import quilldb
from quilldb.constants import FILE_HEADER_SIZE, PAGE_SIZE, SCHEMA_ROOT_PAGE
from quilldb.storage.pager import Pager
from quilldb.txn.journal import Journal
from quilldb.txn.recovery import recover_if_needed


def _journal_path(db_path: pathlib.Path) -> pathlib.Path:
    return db_path.with_name(db_path.name + "-journal")


def _crash_after_barrier(path: pathlib.Path, rows: range) -> None:
    """Push a real transaction past commit_barrier() and all the way to
    disk, then abandon it before the final journal.delete() -- exactly the
    window a process death leaves a HOT journal open in.
    """
    db = quilldb.connect(str(path))
    db.execute("BEGIN")
    for i in rows:
        db.execute("INSERT INTO t VALUES (?, ?)", (i, f"v{i}"))
    txn = db._txn
    assert txn is not None

    with db.pool.pinned_for_write(SCHEMA_ROOT_PAGE) as page:
        page[:FILE_HEADER_SIZE] = db.pager.header_bytes()
    txn._journal.commit_barrier()
    txn.barrier_passed = True
    db.pool.flush_all()
    db.pager.sync()
    # No journal.delete(), no db.close(): this IS the simulated crash.


def test_recovery_restores_pre_transaction_state(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "t.db"
    db = quilldb.connect(str(path))
    db.execute("CREATE TABLE t (id INTEGER, v TEXT)")
    db.close()

    _crash_after_barrier(path, range(500))
    assert _journal_path(path).exists()

    db = quilldb.connect(str(path))
    assert db.execute("SELECT id FROM t").fetchall() == []
    assert not _journal_path(path).exists()
    db.close()


def test_recovery_truncates_a_grown_file(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "t.db"
    db = quilldb.connect(str(path))
    db.execute("CREATE TABLE t (id INTEGER, v TEXT)")
    db.close()
    size_before = path.stat().st_size

    _crash_after_barrier(path, range(2000))
    assert path.stat().st_size > size_before

    db = quilldb.connect(str(path))
    db.close()
    assert path.stat().st_size == size_before


def test_recovery_is_idempotent(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "t.db"
    db = quilldb.connect(str(path))
    db.execute("CREATE TABLE t (id INTEGER, v TEXT)")
    db.close()

    _crash_after_barrier(path, range(500))

    pager = Pager.open(path)
    assert recover_if_needed(path, pager) is True
    assert recover_if_needed(path, pager) is False
    pager.close()


def test_recovery_deletes_an_inert_journal_without_replaying(tmp_path: pathlib.Path) -> None:
    """A journal that never reached commit_barrier() (magic and nRec both
    still zero) means the crash happened before ANY database write was
    permitted -- recovery deletes it and changes nothing else.
    """
    path = tmp_path / "t.db"
    db = quilldb.connect(str(path))
    db.execute("CREATE TABLE t (id INTEGER)")
    db.close()
    before = path.read_bytes()

    journal = Journal(path)
    journal.begin(page_count_before=len(before) // PAGE_SIZE)
    journal.record_original(1, before[:PAGE_SIZE])
    # Abandoned here, before commit_barrier(): magic/nRec are still zero.

    assert _journal_path(path).exists()
    db = quilldb.connect(str(path))
    assert not _journal_path(path).exists()
    assert path.read_bytes() == before
    db.close()


def test_recovery_on_memory_database_is_a_noop() -> None:
    assert recover_if_needed(None, Pager.memory()) is False


def test_recovery_runs_automatically_inside_connect(tmp_path: pathlib.Path) -> None:
    """The whole point: nobody calls recover_if_needed() themselves."""
    path = tmp_path / "t.db"
    db = quilldb.connect(str(path))
    db.execute("CREATE TABLE t (id INTEGER, v TEXT)")
    db.execute("INSERT INTO t VALUES (1, 'ada')")
    db.close()

    _crash_after_barrier(path, range(10))
    db = quilldb.connect(str(path))
    assert db.execute("SELECT id FROM t").fetchall() == [(1,)]
    db.close()
