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
        self._txn_id_lock = threading.Lock()
        self._next_txn_id = 0
        # How many Connections are open. The file closes when the LAST one
        # does -- any earlier and it closes underneath its siblings
        # (NOTES.md B6-4).
        self._connections_lock = threading.Lock()
        self._open_connections = 0
        self._closed = False

    def next_txn_id(self) -> int:
        """Hand out a unique, increasing transaction id. Latched: two
        Connections on different threads can call this at the same instant,
        and two transactions sharing one id would let release_all() free
        the wrong locks (week6-concurrency.md's shared-state table).
        """
        with self._txn_id_lock:
            txn_id = self._next_txn_id
            self._next_txn_id += 1
        return txn_id

    def connect(self) -> "Connection":
        """A new Connection bound to the calling thread.

        Raises:
            ValueError: every earlier Connection has already closed, and
                the file with them.
        """
        from quilldb.api.connection import (  # avoids a cycle: Connection type-hints Database
            Connection,
        )

        with self._connections_lock:
            if self._closed:
                raise ValueError("database is closed")
            self._open_connections += 1
        return Connection(self)

    def _connection_closed(self) -> None:
        """Called once by each Connection.close(). The last one out flushes
        the pool and closes the file -- the same thing every single-
        Connection caller's close() has always done, now only when no other
        Connection still depends on it.

        Every closing Connection has already rolled back its own open
        transaction, so by the time the count reaches zero there is no
        writer left for flush_all() to write pre-barrier pages for.
        """
        with self._connections_lock:
            self._open_connections -= 1
            last = self._open_connections == 0
            if last:
                self._closed = True
        if last:
            self.pool.flush_all()
            self.pager.close()


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
