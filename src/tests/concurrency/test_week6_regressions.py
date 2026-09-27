"""Regression tests for the week 6 review findings, NOTES.md B6-2 .. B6-9.

Each test is the smallest repro that failed before its fix. Most need no
threads at all: a Connection is bound to the thread that created it, but two
Connections created on the SAME thread can interleave statements freely,
which is enough to put one transaction in the gap another one opened.
"""

import subprocess
import threading
import time
from pathlib import Path

import pytest

from quilldb.api.database import open_database
from quilldb.errors import DeadlockError, LockTimeoutError, TableNotFoundError
from quilldb.txn.locks import LockManager, LockMode


def _sqlite3(path: Path, sql: str) -> str:
    result = subprocess.run(["sqlite3", str(path), sql], capture_output=True, text=True, check=False)
    return result.stdout.strip()


def test_rollback_undoes_writes_to_pages_committed_after_a_deferred_begin(tmp_path: Path) -> None:
    """B6-2: _page_count_before was snapshotted at BEGIN. Pages another
    connection committed afterwards looked "allocated by this transaction",
    were never journalled, and a rolled-back write to one of them survived.
    """
    path = tmp_path / "t.db"
    db = open_database(path)
    c1, c2 = db.connect(), db.connect()
    c1.execute("CREATE TABLE t (id INTEGER, s TEXT)")

    c2.execute("BEGIN")  # deferred: no locks, no snapshot that matters yet
    for i in range(200):
        c1.execute("INSERT INTO t VALUES (?, ?)", (i, "x" * 500))  # grows the file
    c2.execute("INSERT INTO t VALUES (?, ?)", (10_000, "ROLLED_BACK"))  # lands on a new page
    c2.execute("ROLLBACK")

    assert len(c1.execute("SELECT * FROM t").fetchall()) == 200
    c1.close()
    c2.close()
    assert _sqlite3(path, "SELECT count(*) FROM t WHERE s = 'ROLLED_BACK'") == "0"
    assert _sqlite3(path, "SELECT count(*) FROM t") == "200"
    assert _sqlite3(path, "PRAGMA integrity_check") == "ok"


def test_rollback_with_an_open_cursor_releases_every_lock(tmp_path: Path) -> None:
    """B6-3: transaction()'s rollback ran with a cursor still pinning a
    page the transaction had dirtied. pool.clear() raised "still pinned"
    after the journal was already gone, masking the caller's exception and
    leaving "__writer__" held forever.
    """
    db = open_database(tmp_path / "t.db")
    c1, c2 = db.connect(), db.connect()
    c1.execute("CREATE TABLE t (id INTEGER, s TEXT)")
    for i in range(300):
        c1.execute("INSERT INTO t VALUES (?, ?)", (i, "x" * 200))

    with pytest.raises(KeyError, match="app error"), c1.transaction():
        c1.execute("DELETE FROM t WHERE id = 1")  # dirties the first leaf
        cursor = c1.execute("SELECT * FROM t")
        cursor.fetchone()  # ... which this cursor now pins
        raise KeyError("app error")

    assert c1._txn is None
    assert not any(entry.holders for entry in db.lock_manager._locks.values())
    c2.busy_timeout = 0.5
    c2.execute("INSERT INTO t VALUES (1000, 'b')")  # no LockTimeoutError
    assert len(c2.execute("SELECT * FROM t").fetchall()) == 301  # the DELETE really rolled back


def test_closing_one_connection_leaves_its_siblings_usable(tmp_path: Path) -> None:
    """B6-4: Connection.close() closed the Database's shared pager."""
    path = tmp_path / "t.db"
    db = open_database(path)
    c1, c2 = db.connect(), db.connect()
    c1.execute("CREATE TABLE t (id INTEGER)")
    c1.close()

    c2.execute("INSERT INTO t VALUES (1)")
    assert c2.execute("SELECT id FROM t").fetchall() == [(1,)]

    c2.close()  # the last one out closes the file ...
    with pytest.raises(ValueError, match="database is closed"):
        db.connect()
    assert _sqlite3(path, "PRAGMA integrity_check") == "ok"  # ... cleanly


@pytest.mark.parametrize("attempt", range(5))
def test_cycle_closed_by_the_older_txn_aborts_the_youngest_promptly(attempt: int) -> None:
    """B6-5: when the OLDER transaction closed the cycle, it correctly chose
    the younger one as victim -- then went back to sleep without waking it.
    The victim only noticed at its own timeout, and whichever timer fired
    first lost: sometimes the older txn, with LockTimeoutError on a real
    cycle. Repeated because the old failure was timing-dependent.
    """
    lm = LockManager()
    lm.acquire(1, "A", LockMode.EXCLUSIVE)
    lm.acquire(2, "B", LockMode.EXCLUSIVE)
    outcome: dict[int, tuple[str, float]] = {}

    def run(txn_id: int, resource: str) -> None:
        started = time.monotonic()
        try:
            lm.acquire(txn_id, resource, LockMode.EXCLUSIVE, timeout=5.0)
            outcome[txn_id] = ("granted", time.monotonic() - started)
        except (DeadlockError, LockTimeoutError) as exc:
            outcome[txn_id] = (type(exc).__name__, time.monotonic() - started)
            lm.release_all(txn_id)  # what the caller's rollback does

    young = threading.Thread(target=run, args=(2, "A"))
    young.start()
    deadline = time.monotonic() + 2.0
    while lm._waiting_for.get(2) != "A":  # txn 2 is asleep BEFORE the cycle exists
        assert time.monotonic() < deadline
        time.sleep(0.005)
    old = threading.Thread(target=run, args=(1, "B"))
    old.start()  # txn 1 closes the cycle
    young.join(timeout=10)
    old.join(timeout=10)

    assert outcome[2][0] == "DeadlockError"
    assert outcome[1][0] == "granted"
    assert outcome[1][1] < 1.0  # detection, not the 5s timeout backstop


def test_a_timed_out_queue_head_wakes_the_waiters_behind_it() -> None:
    """B6-6: a waiter leaving the FIFO queue on timeout never notified the
    compatible waiters it had been blocking; they slept until their own
    timeouts.
    """
    lm = LockManager()
    lm.acquire(1, "R", LockMode.SHARED)
    granted_after: list[float] = []

    def writer() -> None:
        with pytest.raises(LockTimeoutError):
            lm.acquire(2, "R", LockMode.EXCLUSIVE, timeout=0.3)  # queue head, blocked by txn 1

    def reader() -> None:
        started = time.monotonic()
        lm.acquire(3, "R", LockMode.SHARED, timeout=5.0)  # FIFO: queued behind txn 2
        granted_after.append(time.monotonic() - started)

    w = threading.Thread(target=writer)
    w.start()
    deadline = time.monotonic() + 2.0
    while not lm._locks["R"].waiters:
        assert time.monotonic() < deadline
        time.sleep(0.005)
    r = threading.Thread(target=reader)
    r.start()
    w.join(timeout=10)
    r.join(timeout=10)

    assert granted_after and granted_after[0] < 1.5


def test_uncommitted_ddl_is_invisible_to_other_connections(tmp_path: Path) -> None:
    """B6-7: CREATE TABLE mutated the shared Catalog (and bumped the
    in-memory schema cookie) immediately, so another connection could bind
    against -- or catalog.load() page 1 in the middle of -- DDL that might
    still roll back.
    """
    db = open_database(tmp_path / "t.db")
    c1, c2 = db.connect(), db.connect()
    c2.busy_timeout = 0.2

    c1.execute("BEGIN")
    c1.execute("CREATE TABLE u (id INTEGER)")
    # EXPLAIN, not SELECT: a SELECT's scan would block on c1's EXCLUSIVE
    # lock on `u` anyway, hiding the leak. EXPLAIN plans without opening a
    # scan, so before the fix it happily bound -- and planned -- a table
    # that was about to be rolled back.
    with pytest.raises(LockTimeoutError):
        c2.execute("EXPLAIN SELECT * FROM u")  # waits on "__schema__", never sees u
    c1.execute("ROLLBACK")

    with pytest.raises(TableNotFoundError):
        c2.execute("SELECT * FROM u")


def test_analyze_waits_for_the_current_writer(tmp_path: Path) -> None:
    """B6-8: ANALYZE ran outside any transaction, so its quill_stat1 writes
    were journalled into whichever transaction held the pool hook -- here,
    c1's, whose ROLLBACK would then have silently undone them.
    """
    db = open_database(tmp_path / "t.db")
    c1, c2 = db.connect(), db.connect()
    c1.execute("CREATE TABLE t (id INTEGER)")
    c1.execute("INSERT INTO t VALUES (1)")

    c1.execute("BEGIN")
    c1.execute("INSERT INTO t VALUES (2)")
    c2.busy_timeout = 0.2
    with pytest.raises(LockTimeoutError):
        c2.execute("ANALYZE")
    c1.execute("ROLLBACK")

    c2.execute("ANALYZE")  # the writer is gone: runs, as its own transaction
    assert db.stats.table_stats("t").row_count == 1


def test_immediate_is_not_a_reserved_word(tmp_path: Path) -> None:
    """B6-9: making IMMEDIATE a keyword broke any column named `immediate`."""
    db = open_database(tmp_path / "t.db")
    conn = db.connect()
    conn.execute("CREATE TABLE flags (immediate INTEGER)")
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO flags VALUES (1)")
    conn.execute("COMMIT")
    assert conn.execute("SELECT immediate FROM flags").fetchall() == [(1,)]
