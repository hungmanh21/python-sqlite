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
"""

from quilldb.constants import FILE_HEADER_SIZE, SCHEMA_ROOT_PAGE
from quilldb.storage.bufferpool import BufferPool
from quilldb.storage.pager import Pager
from quilldb.txn.journal import Journal


class Transaction:
    """One transaction's worth of state.

    Owns: the set of pages already journalled, and the original page_count
    so rollback can truncate. Does NOT own the journal's on-disk lifecycle
    beyond calling into it -- Journal itself still owns begin()/replay()/
    commit_barrier()/delete().
    """

    def __init__(self, pager: Pager, pool: BufferPool, journal: Journal) -> None:
        self._pager = pager
        self._pool = pool
        self._journal = journal
        self._journalled: set[int] = set()
        self._page_count_before = pager.page_count
        self._active = True
        self.barrier_passed = False  # read by Pager.write_page's assertion

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

        data = self._pager.read_page(page_id)
        self._journal.record_original(page_id, bytes(data))
        self._journalled.add(page_id)

    def commit(self) -> None:
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

        flush_all() has to precede sync(), or there is nothing on disk yet
        to fsync. delete() has to be last: a crash between sync() and
        delete() just leaves a redundant valid journal that replay() would
        apply harmlessly (every record is an idempotent assignment); a crash
        before sync() with the journal already deleted would leave nothing
        to recover a genuinely incomplete write, which is unrecoverable.
        """
        with self._pool.pinned_for_write(SCHEMA_ROOT_PAGE) as page:
            page[:FILE_HEADER_SIZE] = self._pager.header_bytes()

        self._journal.commit_barrier()
        self.barrier_passed = True

        self._pool.flush_all()
        self._pager.sync()
        self._journal.delete()

    def rollback(self) -> None:
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
          5. pool.clear() -- the pool's cache now describes a database that
             changed underneath it via steps 1-2, neither of which went
             through get_page_for_write/write_page. Every cached entry,
             dirty or not, is stale.
          6. pager.reload_header() -- step 5 dropped the cache, but the
             pager's in-memory FileHeader (freelist_trunk, freelist_count,
             change_counter, schema_cookie) is a separate object that
             truncate() only partially fixed (it resets the freelist fields,
             not change_counter/schema_cookie). Re-read it from the
             now-restored file so it matches what's on disk.

        Steps 5 and 6 are the ones people drop -- truncate() fixes
        page_count and nothing else; the pool and the in-memory header both
        still need to be told the file moved.
        """
        self._journal.replay(self._pager)
        self._pager.truncate(self._page_count_before)
        self._pager.sync()
        self._journal.delete()
        self._pool.clear()
        self._pager.reload_header()
