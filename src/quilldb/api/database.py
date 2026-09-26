"""ONE per file, shared by every thread that opens it -- the split
week6-concurrency.md session 3 asks for (SS36, "the split that is the
design"). `Connection` (api/connection.py) stays thread-owned and NOT
thread-safe; everything here is the opposite.

Session map:
  session 3 (this file): Database owns the pager, pool, catalog, stats, and
    lock manager -- Connection becomes a thin per-thread view onto them.
  session 5: LockManager.acquire()/release_all() get wired into real
    statement execution, which is what the txn-id counter below is for.
"""

import threading
from pathlib import Path
from typing import TYPE_CHECKING

from quilldb.catalog.catalog import Catalog
from quilldb.constants import FILE_HEADER_SIZE, SCHEMA_ROOT_PAGE
from quilldb.plan.analyze import StatisticsCatalog
from quilldb.storage.bufferpool import BufferPool
from quilldb.storage.pager import Pager
from quilldb.txn.locks import LockManager
from quilldb.txn.recovery import recover_if_needed

if TYPE_CHECKING:
    from quilldb.api.connection import Connection

_MEMORY_PATH = ":memory:"


class Database:
    """Owns everything global: the pager, the buffer pool, the lock
    manager, and the one shared Catalog every Connection reads through.

    `connect()` is the only way to get a `Connection` -- each is bound to
    whichever thread calls it (Connection._owner_thread).
    """

    def __init__(self, pager: Pager, pool: BufferPool, catalog: Catalog, stats: StatisticsCatalog) -> None:
        self.pager = pager
        self.pool = pool
        self.catalog = catalog
        self.stats = stats
        self.lock_manager = LockManager()
        self._writer_txn = None  # renamed from pool._txn -- set/cleared under "__writer__" (SS37.2)
        self._txn_id_lock = threading.Lock()
        self._next_txn_id = 0

    def next_txn_id(self) -> int:
        """Hand out a unique, increasing transaction id.

        TODO(human): two Connections on different threads can call this at
        the same instant -- the shared-state table in week6-concurrency.md
        names exactly this ("two transactions with one id -> release_all
        frees the wrong locks"). self._txn_id_lock exists for you to use.
        Not wired into Connection._begin() yet -- that's session 5's job,
        once LockManager.acquire() actually needs an id to call with.
        """
        with self._txn_id_lock:
            txn_id = self._next_txn_id
            self._next_txn_id += 1
        return txn_id

    def connect(self) -> "Connection":
        """A new Connection bound to the calling thread."""
        from quilldb.api.connection import (  # avoids a cycle: Connection type-hints Database
            Connection,
        )

        return Connection(self)


def open_database(path: str | Path) -> Database:
    """Open an existing database file or create a new one.

    The exact string ":memory:" selects Pager.memory() and never creates a
    file -- anything else, including a Path spelled ":memory:", is a real
    path on disk. Same convention connect() has always used.
    """
    if path == _MEMORY_PATH:
        pager = Pager.memory()
    else:
        path = Path(path)
        pager = Pager.open(path) if path.exists() else Pager.create(path)

    # Recovery completes before the pool exists -- there is no cache to
    # invalidate because there is no cache yet (week5-transactions.md).
    recover_if_needed(pager.path, pager)
    pool = BufferPool(pager)
    catalog = Catalog(pager, pool)
    catalog.load()

    page_count_before_stats = pager.page_count
    stats = StatisticsCatalog(pager, pool, catalog)
    if pager.page_count != page_count_before_stats:
        # StatisticsCatalog just bootstrapped quill_stat1 via
        # catalog.create_table() directly, with no surrounding Transaction
        # -- which bumped the in-memory header (schema_cookie, page_count)
        # without ever writing it into page 1's bytes, the way
        # Transaction.commit() does at line 101-102 of transaction.py.
        # Left unsynced, page 1's on-disk header still describes the
        # pre-bootstrap file: the first real transaction's will_modify(1)
        # would journal that stale header as "original", and rolling back
        # would restore it -- resetting page_count under quill_stat1's own
        # already-committed root page and corrupting it. Sync and flush now,
        # the same shape commit() uses, so this bootstrap write is durable
        # and self-consistent before any Connection can begin a transaction.
        with pool.pinned_for_write(SCHEMA_ROOT_PAGE) as page:
            page[:FILE_HEADER_SIZE] = pager.header_bytes()
        pool.flush_all()
        pager.sync()

    return Database(pager, pool, catalog, stats)
