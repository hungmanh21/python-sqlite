"""quilldb.bench.concurrent -- throughput vs thread count, for the README.

Reads and writes are reported separately, on purpose. A read-only
statement takes SHARED, and SHARED locks coexist (docs/concurrency.md),
so reads never queue behind each other the way writes queue behind the
single writer -- but measured throughput does not climb with thread
count anyway, and that's the other honest number this script reports:
quilldb is pure Python, so CPython's GIL lets only one thread run
bytecode at a time regardless of how many are runnable, and every read
here is CPU-bound (tokenize, parse, walk the b-tree) with no I/O to
release the GIL during. More threads add scheduling overhead without
adding a second core's worth of work, so the read column is flat-to-
falling, not climbing -- the lock manager isn't the ceiling, the
interpreter is.

Every write transaction takes the global "__writer__" EXCLUSIVE lock
before any table lock (docs/concurrency.md, "Why a single writer") -- so
at most one commit is ever in flight system-wide, no matter how many
threads are submitting writes, and each commit pays for ~4 real fsync()
calls (Journal.commit_barrier()'s durability protocol + Pager.sync(),
see test_transfer_stress.py's docstring for the arithmetic). Write
throughput is therefore flat for a structural reason on top of the GIL:
even with free threading, only one write could be in flight. Reporting
both ceilings side by side, instead of only the number that looks good,
is chapter 16 SS16.7's point.

    python -m quilldb.bench.concurrent
"""

from __future__ import annotations

import pathlib
import random
import shutil
import tempfile
import threading
import time

import quilldb
from quilldb.api.database import Database
from quilldb.errors import DeadlockError

ACCOUNTS = 50
STARTING_BALANCE = 1000
THREAD_COUNTS = (1, 2, 4, 8)
WRITE_OPS_PER_THREAD = 25  # small on purpose: writes are serialized at ~1 commit/4 fsyncs
READ_OPS_PER_THREAD = 2000  # cheap, so give the GIL-vs-lock-manager question room to show up


def setup(path: pathlib.Path) -> Database:
    conn = quilldb.connect(str(path))
    conn.execute("CREATE TABLE accounts (id INTEGER, balance INTEGER)")
    for account_id in range(ACCOUNTS):
        conn.execute("INSERT INTO accounts VALUES (?, ?)", (account_id, STARTING_BALANCE))
    return conn.db


def read_worker(db: Database, ops: int, seed: int) -> None:
    rng = random.Random(seed)
    conn = db.connect()
    for _ in range(ops):
        account_id = rng.randrange(ACCOUNTS)
        conn.execute("SELECT balance FROM accounts WHERE id = ?", (account_id,)).fetchall()


def write_worker(db: Database, ops: int, seed: int) -> None:
    rng = random.Random(seed)
    conn = db.connect()
    for _ in range(ops):
        src, dst = rng.sample(range(ACCOUNTS), 2)
        amount = rng.randint(1, 10)
        for _attempt in range(5):  # retry on deadlock, same contract as the stress test
            conn.execute("BEGIN")
            try:
                debited = conn.execute(
                    "UPDATE accounts SET balance = balance - ? WHERE id = ? AND balance >= ?",
                    (amount, src, amount),
                )
                if debited.rowcount > 0:
                    conn.execute(
                        "UPDATE accounts SET balance = balance + ? WHERE id = ?", (amount, dst)
                    )
            except DeadlockError:
                conn.execute("ROLLBACK")
                continue
            else:
                conn.execute("COMMIT")
                break


def measure(kind: str, threads: int, tmp: pathlib.Path) -> float:
    """Wall-clock throughput in ops/sec for `threads` concurrent workers.

    Plain threading.Thread rather than concurrent.futures.ThreadPoolExecutor.
    (This module used to live in a loose benchmarks/ directory, where running
    it as a script put that directory first on sys.path and made
    `import concurrent.futures` resolve to this very file. Inside the package
    that hazard is gone, but there is no reason to change what works.)
    """
    db = setup(tmp / f"{kind}_{threads}.db")
    ops = WRITE_OPS_PER_THREAD if kind == "write" else READ_OPS_PER_THREAD
    worker = write_worker if kind == "write" else read_worker

    pool = [threading.Thread(target=worker, args=(db, ops, seed)) for seed in range(threads)]
    started = time.perf_counter()
    for t in pool:
        t.start()
    for t in pool:
        t.join()
    elapsed = time.perf_counter() - started

    return (threads * ops) / elapsed


def main() -> None:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="quilldb-bench-"))
    try:
        print("quilldb concurrency benchmark -- throughput vs thread count\n")
        print(f"{'threads':<10} {'read txn/sec':>14} {'write txn/sec':>15}")
        print("-" * 41)
        for threads in THREAD_COUNTS:
            read_rate = measure("read", threads, tmp)
            write_rate = measure("write", threads, tmp)
            print(f"{threads:<10} {read_rate:>14,.0f} {write_rate:>15,.1f}")

        print(
            "\nReads don't queue: SHARED locks coexist, so no reader ever waits on another. But"
            "\nthe read column doesn't climb either -- CPython's GIL runs one thread's bytecode"
            "\nat a time, and every read here is CPU-bound with nothing to release the GIL for,"
            "\nso more threads add scheduling overhead, not parallelism. Writes are flat for a"
            "\nstructural reason on top of that: every write takes the global \"__writer__\""
            "\nEXCLUSIVE lock before any table lock, so exactly one commit is ever in flight"
            "\nsystem-wide, and each commit pays ~4 real fsync() calls. See docs/concurrency.md,"
            "\n\"Why a single writer\"."
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
