"""Session 6 (week6-concurrency.md §39): the transfer stress test -- the
week's headline deliverable. Money moves between `accounts` rows under real
thread contention; the invariant is that the total never changes, not that
every individual transfer succeeds.

Also the first real exercise of busy_timeout and the LockManager's FIFO
waiter queue (both built in earlier sessions) under actual load instead of
one deliberately provoked scenario -- a bug that only shows up under
contention still has to survive here before session 7's 20x flake hunt.
"""

import random
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import quilldb
from quilldb.api.connection import Connection
from quilldb.api.database import Database
from quilldb.btree.validate import validate_btree
from quilldb.errors import DeadlockError


def setup_accounts(tmp_path: Path, accounts: int, each: int) -> Database:
    """A fresh database with an `accounts` table, `accounts` rows each
    starting at balance `each`.
    """
    bootstrap = quilldb.connect(str(tmp_path / "t.db"))
    bootstrap.execute("CREATE TABLE accounts (id INTEGER, balance INTEGER)")
    for account_id in range(accounts):
        bootstrap.execute("INSERT INTO accounts VALUES (?, ?)", (account_id, each))
    return bootstrap.db


def all_balances(db: Database) -> list[int]:
    """Every account's current balance, read through a fresh throwaway
    Connection.

    Closing it is safe now that Database ref-counts its Connections and only
    closes the shared pager when the LAST one closes (NOTES.md B6-4) --
    setup_accounts' bootstrap connection is never closed, so this never is.
    """
    conn = db.connect()
    balances = []
    for row in conn.execute("SELECT balance FROM accounts").fetchall():
        assert isinstance(row[0], int)
        balances.append(row[0])
    conn.close()
    return balances


def sum_balances(db: Database) -> int:
    """A Python-side fold, NOT `SELECT SUM(balance)` -- aggregates are week
    7; there is no SUM in the tokenizer yet, and writing this as SQL would
    turn the week's headline deliverable into a week-7 dependency.
    """
    return sum(all_balances(db))


def validate_all_btrees(db: Database) -> None:
    """Raises BTreeInvariantError if any table's b-tree is structurally
    unsound; returns None if every one is fine. (validate_btree() itself is
    raise-on-failure, not a bool/`.is_valid` -- see btree/validate.py.)
    """
    for table in db.catalog.list_tables():
        validate_btree(db.pager, db.pool, table.root_page)


def sqlite3_integrity_check(path: Path) -> str:
    result = subprocess.run(
        ["sqlite3", str(path), "PRAGMA integrity_check;"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip()


def transfer(conn: Connection, src: int, dst: int, amount: int) -> None:
    """Move `amount` from account `src` to account `dst`, atomically.

    Runs inside one explicit transaction so the debit and credit either
    both happen or neither does.

    Raises:
        DeadlockError: propagated to the caller, whose job is to retry --
            some transfers legitimately losing a deadlock race under real
            contention is expected, not a bug (test_sum_of_balances_never_changes's
            own docstring says so).
    """
    conn.execute("BEGIN")
    try:
        # One conditional UPDATE does the balance check and the debit
        # together (`WHERE balance >= ?`), so no SELECT-then-UPDATE lock
        # upgrade is needed -- and the debit's rowcount says whether the
        # credit should happen at all.
        debited = conn.execute(
            "UPDATE accounts SET balance = balance - ? WHERE id = ? AND balance >= ?",
            (amount, src, amount),
        )
        if debited.rowcount > 0:
            conn.execute("UPDATE accounts SET balance = balance + ? WHERE id = ?", (amount, dst))

    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@pytest.mark.slow
@pytest.mark.timeout(300)
def test_sum_of_balances_never_changes(tmp_path: Path) -> None:
    """THE deliverable. 8 threads, 10k transfers between random accounts.

    The invariant is not that every transfer succeeded -- some will hit
    DeadlockError, and that's correct behaviour under a real lock manager
    facing genuine contention. The invariant is that money is neither
    created nor destroyed (chapter 15 §15.0), and that no account goes
    negative.

    timeout(300), not the roadmap's 120: every commit is 4 real fsync()
    calls (Journal.commit_barrier()'s durability protocol -- fsync the
    journal, fsync its directory, fsync the journal again -- plus
    Pager.sync()'s own fsync of the database file, chapter 13's real
    SQLite protocol, not padding), and the single-writer design serializes
    every one of the 10k transfers behind it -- more threads can't help,
    since only one commit is ever in flight system-wide. Measured directly
    (instrumented acquire()/fsync() with timestamps): a steady, un-degrading
    ~44 commits/sec the whole run, ~227s minimum for 10k -- genuinely slow,
    not a hang. Already `slow`-marked and excluded from a bare `pytest`.
    """
    db = setup_accounts(tmp_path, accounts=50, each=1000)
    before = sum_balances(db)

    def worker(seed: int) -> None:
        rng = random.Random(seed)
        for _ in range(1250):
            src, dst = rng.sample(range(50), 2)
            for _attempt in range(5):  # retry on deadlock: that's the contract
                try:
                    transfer(db.connect(), src, dst, rng.randint(1, 10))
                    break
                except DeadlockError:
                    continue

    with ThreadPoolExecutor(8) as pool:
        list(pool.map(worker, range(8)))

    assert sum_balances(db) == before
    validate_all_btrees(db)
    assert min(all_balances(db)) >= 0  # no account went negative

    db_path = db.pager.path
    # No close() needed before the check: every commit already stamped the
    # header into page 1 and fsynced, and the bootstrap connection keeps the
    # Database open anyway (all_balances' note).
    assert db_path is not None
    assert sqlite3_integrity_check(db_path) == "ok"
