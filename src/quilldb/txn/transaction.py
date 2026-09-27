"""One open transaction's worth of state, sitting on the other end of the
`_txn` hook session 0 wired into `BufferPool.get_page_for_write()` and
`Pager.write_page()`'s barrier assertion.

Session map (docs/implementation/week5-transactions.md, "Week 5 sessions"):
  session 3 (this file): Transaction itself -- will_modify, commit,
    rollback -- plus no-steal in BufferPool._evict_one (storage/bufferpool.py).
  session 4: BEGIN / COMMIT / ROLLBACK, autocommit, db.transaction() --
    api/connection.py creates and owns a Transaction, assigns it to
    pool._txn, and calls commit()/rollback() from SQL statement dispatch.
  session 5: txn/recovery.py replays a hot journal at connect() time using
    the same Journal this file already depends on.

week6-concurrency.md session 5 (SS38) adds lock_for_read()/lock_for_write()
and commit()/rollback() releasing this transaction's locks (both done).
SS37.2's read-only-vs-journalling split lives in will_modify(): the
Journal is only constructed on the first write to a page that existed
before this transaction became the writer.

Lock hierarchy, always acquired in this order (docs/concurrency.md, "Lock
order"): "__schema__" -> "__writer__" -> tables. Every statement except
BEGIN/COMMIT/ROLLBACK takes "__schema__" before binding -- SHARED, or
EXCLUSIVE for CREATE TABLE/CREATE INDEX (NOTES.md B6-7).
"""

from quilldb.constants import FILE_HEADER_SIZE, SCHEMA_ROOT_PAGE
from quilldb.storage.bufferpool import BufferPool
from quilldb.storage.pager import Pager
from quilldb.txn.journal import Journal
from quilldb.txn.locks import LockManager, LockMode

SCHEMA_RESOURCE = "__schema__"
WRITER_RESOURCE = "__writer__"


class Transaction:
    """One transaction's worth of state.

    Owns: the set of pages already journalled, and the original page_count
    so rollback can truncate. Does NOT own the journal's on-disk lifecycle
    beyond calling into it -- Journal itself still owns begin()/replay()/
    commit_barrier()/delete().
    """

    def __init__(
        self,
        pager: Pager,
        pool: BufferPool,
        lock_manager: LockManager,
        txn_id: int,
        *,
        timeout: float | None = None,
    ) -> None:
        self._pager = pager
        self._pool = pool
        self._lock_manager = lock_manager
        self.id = txn_id
        self._timeout = timeout
        # None until will_modify() promotes this transaction on its first
        # real write (SS37.2) -- a transaction that only ever calls
        # lock_for_read stays this way for its whole life: there's nothing
        # for it to undo, so it never constructs a Journal or touches
        # pager._txn/pool._txn.
        self._journal: Journal | None = None
        self._journalled: set[int] = set()
        # Re-snapshotted by _become_writer() the moment this transaction
        # first holds "__writer__". The value here only matters for a
        # transaction that never writes, which never reads it.
        self._page_count_before = pager.page_count
        self._is_writer = False
        self._holds_schema_write = False
        self._active = True
        self.barrier_passed = False  # read by Pager.write_page's assertion

    def lock_schema_read(self) -> None:
        """SHARED on "__schema__", taken by Connection.execute() before
        binding any statement, so no statement ever binds against -- or
        catalog.load()s from page 1 while a writer is mid-way through --
        an uncommitted CREATE TABLE/CREATE INDEX (NOTES.md B6-7).
        """
        self._lock_manager.acquire(self.id, SCHEMA_RESOURCE, LockMode.SHARED, self._timeout)

    def lock_schema_write(self) -> None:
        """EXCLUSIVE on "__schema__", for CREATE TABLE/CREATE INDEX. Taken
        BEFORE "__writer__" (the lock hierarchy in this module's
        docstring): taken after it, a DDL holding "__writer__" would wait
        on every reader's SHARED "__schema__" while any of those readers
        that also wants to write waits on "__writer__" -- a deadlock
        between every DDL and every concurrent writer.
        """
        self._lock_manager.acquire(self.id, SCHEMA_RESOURCE, LockMode.EXCLUSIVE, self._timeout)
        self._holds_schema_write = True

    @property
    def holds_schema_write(self) -> bool:
        """True once lock_schema_write() succeeded -- i.e. this transaction
        may have changed the schema, so rolling it back has to resync the
        shared Catalog (Connection._end_txn step 4)."""
        return self._holds_schema_write

    def lock_for_read(self, table: str) -> None:
        """SHARED on `table`. Called from SeqScan/IndexScan.open() -- before
        either cursor touches a page, so a read never observes a writer's
        half-applied page (week6-concurrency.md SS38).
        """
        self._lock_manager.acquire(self.id, table, LockMode.SHARED, self._timeout)

    def lock_for_write(self, table: str) -> None:
        """EXCLUSIVE on `table`, for InsertOp/DeleteOp/UpdateOp.open(), and
        for CREATE TABLE/CREATE INDEX/ANALYZE's mutation bodies in
        connection.py.

        Global-writer-first ordering (chapter 16, docs/concurrency.md "Why
        a single writer"): EXCLUSIVE on "__writer__" before EXCLUSIVE on
        `table`, always this order, every write lock -- it's what keeps the
        wait-for graph a table-vs-table problem instead of a general one,
        which is what makes _detect_deadlock's youngest-victim rule sound.

        Also wires pager._txn/pool._txn onto self. This is the ONE correct
        place to do it (not will_modify(), and not eagerly in every
        _begin()): holding "__writer__" is what makes this transaction the
        single system-wide writer that hook is meant to name -- get there
        any other way and two concurrent transactions could both believe
        they're it. Setting it more than once (a second table, later in the
        same transaction) is harmless, so there's no guard against that.
        """
        self._acquire_writer()
        self._lock_manager.acquire(self.id, table, LockMode.EXCLUSIVE, self._timeout)
        self._pager._txn = self
        self._pool._txn = self

    def lock_immediate(self) -> None:
        """SHARED "__schema__", then EXCLUSIVE "__writer__", right now -- for
        `BEGIN IMMEDIATE` (week6-concurrency.md, "declare write intent up
        front"). A plain BEGIN leaves the "__writer__" acquisition until
        the first real write reaches lock_for_write(); IMMEDIATE moves it
        to the start of the transaction, so a transaction that knows it's
        going to write claims its place in the writer queue before doing
        any reads, instead of discovering -- possibly after a long run of
        SELECTs -- that another writer got there first.

        "__schema__" first because that's the lock hierarchy: every
        statement this transaction runs will take it anyway, and taking it
        AFTER "__writer__" would invert the order a concurrent DDL uses.

        Deliberately does NOT acquire any table's lock or wire
        pager._txn/pool._txn: which table gets written is still unknown at
        BEGIN time, and lock_for_write() does both of those correctly once
        a real write names one -- acquire()'s reentrancy means the
        "__writer__" grant taken here is simply already-held by then, at no
        extra cost.
        """
        self.lock_schema_read()
        self._acquire_writer()

    def _acquire_writer(self) -> None:
        """EXCLUSIVE on "__writer__", and -- the first time only -- snapshot
        page_count as this transaction's rollback point.

        The snapshot has to be taken HERE, not in __init__: a deferred
        BEGIN (or an autocommit statement queued behind another writer)
        can wait arbitrarily long between being constructed and becoming
        the writer, and every page some other writer commits in that gap
        would otherwise sit above _page_count_before -- will_modify()
        would skip journalling them as "allocated by this transaction",
        and rollback could not undo writes to them (NOTES.md B6-2).
        Holding "__writer__" is what makes page_count stop moving.
        """
        self._lock_manager.acquire(self.id, WRITER_RESOURCE, LockMode.EXCLUSIVE, self._timeout)
        if not self._is_writer:
            self._is_writer = True
            self._page_count_before = self._pager.page_count

    def will_modify(self, page_id: int) -> None:
        """Called before the first modification of a page, by
        `BufferPool.get_page_for_write()` -- the only route to a mutable
        page, so this is the only place a page's original bytes can be
        journalled before something overwrites them.

        A no-op if page_id is already in self._journalled: re-journalling
        would waste I/O and, worse, would journal this transaction's OWN
        modified bytes as if they were "original", corrupting rollback.

        A page allocated during this transaction (page_id >
        self._page_count_before) is tracked but never journalled: it didn't
        exist when the transaction began, so rollback's truncate() erases it
        for free.

        Otherwise, reads the page's current on-disk content with
        self._pager.read_page() -- not through the pool -- and hands it to
        self._journal.record_original(). pager.read_page(), not
        pool.get_page(): this is an internal read the caller never asked
        for, not a page that should count toward the pool's `misses`/`hits`
        benchmark stats, and pinning it here would leave a pin nobody is
        positioned to release (get_page_for_write's caller is about to pin
        it too, for the mutation itself).
        """
        if page_id in self._journalled:
            return

        if page_id > self._page_count_before:
            self._journalled.add(page_id)
            return

        # A page that existed before this transaction became the writer is
        # about to be overwritten -- the one case that needs journalling,
        # and SS37.2's promotion point on the first such write. The journal
        # records _page_count_before, not the live page_count: a growth-path
        # allocate_page() may already have bumped the latter, and recovery
        # must truncate to the same point rollback() does.
        if self._journal is None:
            self._journal = Journal(self._pager.path)
            self._journal.begin(self._page_count_before)
        data = self._pager.read_page(page_id)
        self._journal.record_original(page_id, bytes(data))
        self._journalled.add(page_id)

    def commit(self, *, release: bool = True) -> None:
        """Make every change in this transaction durable, then discard the
        journal -- the operation that turns "recoverable" into "permanent".

        In order, with no step skippable:
          1. Stamp the current in-memory header into page 1, THROUGH
             get_page_for_write so page 1's pre-transaction bytes get
             journalled like any other dirtied page. Has to come first: it
             is itself a write, and must be journalled and flushed like
             every other write before the barrier makes the journal valid.
          2. journal.commit_barrier() -- the journal is now valid.
             barrier_passed flips to True immediately after, so any
             database write from here on is allowed to actually happen.
          3. pool.flush_all() -- every dirty page, including the header page
             from step 1, now goes to the database file.
          4. pager.sync() -- fsync the database itself.
          5. journal.delete() -- THE commit point. Once this returns, the
             transaction is unconditionally done; there is nothing left for
             a crash to roll back even if the process dies on the next line.
          6. lock_manager.release_all() -- LAST, not first: strict 2PL
             (chapter 16 SS16.2) means a lock this transaction holds must
             stay held until the transaction is truly finished, and it
             isn't finished until the journal is gone.

        flush_all() has to precede sync(), or there is nothing on disk yet
        to fsync. delete() has to be last of the durability steps: a crash
        between sync() and delete() just leaves a redundant valid journal
        that replay() would apply harmlessly (every record is an idempotent
        assignment); a crash before sync() with the journal already deleted
        would leave nothing to recover a genuinely incomplete write, which
        is unrecoverable.

        A transaction that never dirtied a page has self._journal still
        None (SS37.2) -- nothing was ever written, so there is nothing to
        make durable; skip straight to releasing locks.

        `release=False` skips step 6 so Connection can unwire the pager/pool
        hook and resync its catalog while this transaction still holds its
        locks, then call release_locks() itself.
        """
        if self._journal is not None:
            with self._pool.pinned_for_write(SCHEMA_ROOT_PAGE) as page:
                page[:FILE_HEADER_SIZE] = self._pager.header_bytes()

            self._journal.commit_barrier()
            self.barrier_passed = True

            self._pool.flush_all()
            self._pager.sync()
            self._journal.delete()

        if release:
            self.release_locks()

    def rollback(self, *, release: bool = True) -> None:
        """Undo every change in this transaction, restoring the database to
        exactly its pre-BEGIN state.

        In order:
          1. journal.replay(pager) -- restores every journalled page's
             original bytes, through pager.restore_page().
          2. pager.truncate(page_count_before) -- erase any page this
             transaction allocated by growing the file. Must come AFTER
             replay: a page above the truncation point may still need
             restoring first (chapter 14 §14.5 bug 2) -- reversing the order
             would let truncate() cut off a page replay was about to fix.
          3. pager.sync() -- fsync the now-restored database file.
          4. journal.delete() -- discard the journal; rollback is as much a
             commit point as commit() is, and a crash after this line must
             find nothing left to redo.
          5. pool.clear(self._journalled) -- narrowed per week6-concurrency.md
             §37.5: steps 1-2 changed exactly the pages this transaction
             journalled (restored) or allocated-then-truncated (erased),
             bypassing get_page_for_write/write_page for both. Every OTHER
             cached page is still a faithful copy of the file, and a
             concurrent reader may have one pinned -- only this
             transaction's own pages are safe to drop.
          6. pager.reload_header() -- step 5 dropped the cache, but the
             pager's in-memory FileHeader (freelist_trunk, freelist_count,
             change_counter, schema_cookie) is a separate object that
             truncate() only partially fixed (it resets the freelist fields,
             not change_counter/schema_cookie). Re-read it from the
             now-restored file so it matches what's on disk.
          7. lock_manager.release_all() -- LAST, same reasoning as commit():
             this transaction isn't finished until the file is actually
             back to its pre-BEGIN state.

        Steps 5 and 6 are the ones people drop -- truncate() fixes
        page_count and nothing else; the pool and the in-memory header both
        still need to be told the file moved.

        A transaction that never dirtied a page has self._journal still
        None (SS37.2) -- there is nothing on disk to undo; skip straight to
        releasing locks.

        `release=False`: same as commit()'s.
        """
        if self._journal is not None:
            self._journal.replay(self._pager)
            self._pager.truncate(self._page_count_before)
            self._pager.sync()
            self._journal.delete()
            self._pool.clear(self._journalled)
            self._pager.reload_header()

        if release:
            self.release_locks()

    def release_locks(self) -> None:
        """Strict 2PL's single release point -- every lock this transaction
        holds, at once. Called by commit()/rollback() unless they were told
        release=False, in which case the caller owns calling this.
        """
        self._lock_manager.release_all(self.id)
