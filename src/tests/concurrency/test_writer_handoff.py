"""week6-concurrency.md session 5 (SS38): pager._txn/pool._txn is one shared
hook on Database, wired by whichever transaction currently holds
"__writer__" (Transaction.lock_for_write()). Connection._commit()/_rollback()
used to clear that hook unconditionally on the way out.

That was safe in week 5 -- one Connection, one Transaction, ever. It's wrong
now: Transaction.commit()/rollback() release "__writer__" (via
LockManager.release_all) near their own end, BEFORE Connection._commit()/
_rollback() reaches its own unwiring lines. A queued writer on another
connection can be granted "__writer__" and claim pager._txn/pool._txn for
itself in that gap, before the first transaction's cleanup runs -- which
then blindly nulls the hook out from under the new, legitimate writer. Any
further write that new writer makes silently skips will_modify() (dirty but
unjournalled), so rolling it back later would fail to restore it.

The window is a handful of bytecode instructions wide, so this forces the
interleaving deterministically (a patched LockManager.release_all) rather
than hoping a real scheduler hits it -- the same technique
test_deadlock.py's docstring flags this file (Connection-level locking
tests) as session 5's job to add.
"""

import threading

import pytest

import quilldb
from quilldb.txn.locks import LockManager


def test_commit_does_not_clear_a_hook_another_writer_already_claimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = quilldb.connect(":memory:")
    setup.execute("CREATE TABLE a (id INTEGER)")
    db = setup.db

    released = threading.Event()
    t2_wired = threading.Event()
    t1_commit_returned = threading.Event()
    target: dict[str, int] = {}

    real_release_all = LockManager.release_all

    def patched_release_all(self: LockManager, txn_id: int) -> None:
        real_release_all(self, txn_id)
        if txn_id == target.get("t1"):
            # "__writer__" is free now -- let txn 2 race in and wire itself
            # onto pager._txn/pool._txn before this call returns to
            # Connection._commit(), which is about to (wrongly) clear it.
            released.set()
            assert t2_wired.wait(timeout=5), "txn 2 never claimed the writer hook"

    monkeypatch.setattr(LockManager, "release_all", patched_release_all)

    results: dict[str, bool] = {}

    def writer1() -> None:
        conn = db.connect()
        conn.execute("BEGIN")
        conn.execute("INSERT INTO a VALUES (1)")
        target["t1"] = conn._txn.id  # type: ignore[union-attr]
        conn.execute("COMMIT")  # blocks mid-commit via the patch above
        t1_commit_returned.set()

    def writer2() -> None:
        assert released.wait(timeout=5), "txn 1 never released __writer__"
        conn = db.connect()
        conn.execute("BEGIN")
        conn.execute("INSERT INTO a VALUES (2)")
        results["hook_is_mine_right_after_insert"] = db.pager._txn is conn._txn
        t2_wired.set()
        assert t1_commit_returned.wait(timeout=5), "txn 1's COMMIT never returned"
        results["hook_survived_txn1s_cleanup"] = db.pager._txn is conn._txn
        conn.execute("COMMIT")
        results["txn2_committed"] = True

    t1 = threading.Thread(target=writer1)
    t2 = threading.Thread(target=writer2)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert results.get("hook_is_mine_right_after_insert") is True
    assert results.get("hook_survived_txn1s_cleanup") is True
    assert results.get("txn2_committed") is True

    # Both rows made it in, and the hook is quiet again -- nothing was lost
    # or left dangling by the handoff.
    cur = setup.execute("SELECT id FROM a")
    rows = []
    while (row := cur.fetchone()) is not None:
        rows.append(row[0])
    assert sorted(rows) == [1, 2]
    assert db.pager._txn is None
    assert db.pool._txn is None
