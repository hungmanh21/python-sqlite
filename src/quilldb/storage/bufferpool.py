"""A bounded, pinned, write-back cache of pages sitting in front of the Pager.


Caches raw PAGE_SIZE bytearrays keyed by page_id -- the same object is
returned to every caller asking for the same page, which is the property
that matters most here (see docs/theory/storage/04-the-buffer-pool.md §4.2 point 3):
two callers asking for page 47 must get one shared, mutable object, not two
independent copies that can silently clobber each other.


Three rules, in priority order (§4.9):
    1. Never evict a pinned page.
    2. Never drop a dirty page without writing it back first.
    3. Evict least-recently-used.
Only rule 3 is about performance. Getting 1 or 2 wrong loses data or
correctness, not speed -- test them accordingly.


Caches raw bytes rather than parsed PageBody objects -- matches SQLite's own
pcache/btree split, not just a shortcut. See ADR-001.
"""


from collections import OrderedDict
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from quilldb.errors import PageOutOfRangeError, PoolExhaustedError
from quilldb.storage.pager import PAGE_SIZE, Pager


@dataclass
class _Entry:
    """One cached page. Mutable (not frozen) -- pin_count and dirty are
    flipped in place on every get_page/unpin, not replaced wholesale.
    """
    data: bytearray
    pin_count: int
    dirty: bool




class BufferPool:
    def __init__(self, pager: Pager, capacity: int = 128) -> None:
        """A pool over `pager` holding at most `capacity` pages at once."""
        self._pager = pager
        self._capacity = capacity
        # OrderedDict doubles as the recency order: move_to_end() on every
        # touch marks "most recently used", so the LRU victim is always
        # whichever entry currently sits at the front.
        self._cache: OrderedDict[int, _Entry] = OrderedDict()


        # Chapter 19 SS19.2's benchmark metric. A "page read" is exactly a
        # pool MISS -- a hit costs no I/O and must not be counted, or the
        # number stops being the thing the access path determines and
        # starts reporting how warm the cache happened to be. Hits are
        # counted too, but only so a benchmark can state its hit rate;
        # the headline number is `misses`.
        self.hits = 0
        self.misses = 0


        # Rows pulled out of a scan operator. Not a buffer-pool concept --
        # it lives here because the pool is the one object every layer
        # already holds, so counting it costs no new plumbing through
        # build_operator. Paired with `misses` it gives chapter 19's two
        # headline numbers: page reads AND rows examined, which together
        # say whether an index was used and whether it helped.
        self.rows_examined = 0

        # Week 5, session 0: an inert hook. Nothing sets this yet -- it
        # stays None for the entire session, which is what keeps this
        # refactor a no-behavior-change land. Session 3 assigns a real
        # Transaction here, and get_page_for_write starts calling it.
        # Typed loosely (not "Transaction | None") because txn/ doesn't
        # exist in the tree yet -- tighten once it does.
        self._txn: Any = None


    def reset_counters(self) -> None:
        """Zero the hit/miss counters. A benchmark calls this immediately
        before the statement it is measuring.
        """
        self.hits = 0
        self.misses = 0
        self.rows_examined = 0


    def get_page(self, page_id: int) -> bytearray:
        """Fetch a page, pinning it.


        On a hit, returns the SAME bytearray object as any previous caller
        currently holding this page. On a miss, reads it from the pager,
        evicting a page if the pool is already at capacity.


        Returns:
            The page's bytearray. pin_count is incremented by one; the
            caller must eventually call unpin() exactly that many times.
        Raises:
            PoolExhaustedError: pool is at capacity and every cached page
                is pinned, so nothing is available to evict.
            PageOutOfRangeError: propagated from the pager on a miss.
        """
        if page_id in self._cache:
            entry = self._cache[page_id]
            entry.pin_count += 1
            self._cache.move_to_end(page_id)
            self.hits += 1
            return entry.data


        # Check happens BEFORE inserting the new page, so hitting capacity
        # exactly (not just exceeding it) is what must trigger an eviction.
        if len(self._cache) >= self._capacity:
            self._evict_one()


        data = self._pager.read_page(page_id)
        self._cache[page_id] = _Entry(data=data, pin_count=1, dirty=False)
        self.misses += 1
        return data


    def _evict_one(self) -> None:
        """Evict the LRU entry that isn't pinned. Rule 1 and 2 first,
        rule 3 (recency) only decides which unpinned entry among ties.
        """
        for candidate_id, candidate in self._cache.items():
            if candidate.pin_count > 0:
                continue
            if candidate.dirty:
                self._pager.write_page(candidate_id, candidate.data)
            # Stop iterating immediately: deleting from an OrderedDict
            # while still holding its iterator raises RuntimeError.
            del self._cache[candidate_id]
            return
        raise PoolExhaustedError("every cached page is pinned; nothing to evict")


    def unpin(self, page_id: int, dirty: bool = False) -> None:
        """Release one pin on `page_id`.


        Args:
            dirty: if True, marks the page dirty. Never clears a dirty flag
                that a previous unpin already set -- once dirty, a page
                stays dirty until it's written back.
        Raises:
            ValueError: page_id isn't cached, or has no outstanding pin.
        """
        if page_id not in self._cache:
            raise ValueError(f"page {page_id} is not cached")


        entry = self._cache[page_id]
        if entry.pin_count == 0:
            raise ValueError(f"page {page_id} has no outstanding pin")


        entry.pin_count -= 1
        # Sticky: only ever set to True here. Clearing it is flush's job.
        if dirty:
            entry.dirty = True


    def flush_page(self, page_id: int) -> None:
        """Write `page_id` back to the pager if it's dirty, then clear the
        dirty flag. Does not evict it or require it to be unpinned.


        A no-op if `page_id` isn't currently cached.
        """
        if page_id not in self._cache:
            return


        entry = self._cache[page_id]
        if entry.dirty:
            self._pager.write_page(page_id, entry.data)
            entry.dirty = False


    def flush_all(self) -> None:
        """flush_page every currently cached page."""
        for page_id in self._cache:
            self.flush_page(page_id)


    def discard(self, page_id: int) -> None:
        """Drop a page's cache entry without writing it back.


        For a page about to be freed: its in-memory content no longer
        matters, and letting a later flush write stale bytes over whatever
        allocate_page() hands out for the same page number next would
        corrupt an unrelated page. Same rule _evict_one enforces under LRU
        pressure (never drop a pinned page) -- this is that rule, invoked
        directly by a caller that knows a page is now garbage rather than
        waiting for eviction to notice.


        A no-op if page_id isn't cached (already evicted, or never touched
        through the pool at all -- e.g. a page catalog.py freed straight
        through the pager).


        Raises:
            ValueError: page_id is cached and still pinned.
        """
        entry = self._cache.get(page_id)
        if entry is None:
            return
        if entry.pin_count > 0:
            raise ValueError(f"page {page_id} is still pinned")
        del self._cache[page_id]

    def allocate_page(self) -> int:
        """Reuse a freed page, or grow the file. Contents are undefined.

        Session 0, Task 3: this is Pager.allocate_page's old body, moved up
        because it has to write through the pool now, and Pager can't see
        the pool (bufferpool.py imports Pager, not the other way around).
        Pager.allocate_page/free_page still exist untouched for now -- call
        sites don't switch over until this and free_page() below are
        implemented and tested in isolation. Same one-level freelist as
        before: header.freelist_trunk heads a singly-linked chain of freed
        pages, each holding only its own `next` pointer in its first 4 bytes
        (see the still-live Pager.allocate_page/free_page for the exact
        walkthrough while it lasts).

        Contract:
          Reuse path (freelist_trunk != 0):
            1. This page EXISTED before the transaction -- ordinary
               journalled content. Fetch it with get_page_for_write, so the
               original next-pointer bytes get journalled like anything else.
            2. Decode the `next` pointer (first 4 bytes). Point
               freelist_trunk at it, decrement freelist_count.
            3. Release your pin before returning. Every existing call site
               (catalog.py, btree.py, index.py, overflow.py) re-pins
               separately afterward to initialize the page's real content --
               same as it always has against Pager.allocate_page. Leaving a
               pin here means that second pin can never fully release it.
          Growth path (freelist_trunk == 0):
            1. Bump page_count by one -- this new page's number IS the new
               page_count.
            2. It has never existed on disk: nothing to journal, and
               get_page()/read_page() would ask the pager to read a page
               number past the OLD page_count and raise PageOutOfRangeError.
               Insert a fresh zeroed, DIRTY, UNPINNED entry directly into
               self._cache instead. Growing self._cache past self._capacity
               here is expected, not a bug -- see _evict_one and session 0
               §0.1's no-steal decision, which this task exists to unblock.

        You'll need Pager to expose enough of `freelist_trunk`,
        `freelist_count`, and `page_count` for this to work -- decide
        whether to reach into `self._pager._header` directly (Pager and
        BufferPool already cross that seam via read_page/write_page) or add
        a few narrow accessor methods to Pager instead (more encapsulated,
        and Task 4 is about to add header_bytes()/reload_header() there
        anyway, so there's precedent). Either is defensible; be consistent
        with whatever free_page() below ends up doing.
        """
        # TODO(human): implement allocate_page per the contract above.
        if self._pager._header.freelist_count > 0:
            # Reuse path
            page_id = self._pager._header.freelist_trunk
            page = self.get_page_for_write(page_id)
            next_pointer = int.from_bytes(page[:4], byteorder='big')
            self._pager._header.freelist_trunk = next_pointer
            self._pager._header.freelist_count -= 1
            self.unpin(page_id)
            return page_id
        else:
            self._pager._header.page_count += 1
            page_id = self._pager._header.page_count
            self._cache[page_id] = _Entry(data=bytearray(PAGE_SIZE), pin_count=0, dirty=True)
            return page_id

    def free_page(self, page_id: int) -> None:
        """Return a page to the freelist, THROUGH the pool.

        Retires the "writes through the pool's back" warning Pager.free_page
        used to carry permanently: the freelist-trunk write becomes an
        ordinary journalled page write like any other. This method is also
        the ONE place responsible for the stale-cache hazard that warning
        used to make every caller work around by hand -- btree.py's
        delete(), index.py's delete(), catalog.py's create_index, and
        overflow.py's free_overflow_chain all used to call
        self.pool.discard(page_id) themselves, immediately before
        self.pager.free_page(page_id), so a later flush of the page's old
        dirty content (real row data, before it was emptied) couldn't
        clobber the freelist-node bytes written here. Now that every call
        site goes through this method instead, discarding is this method's
        job alone.

        Contract:
          1. Reject page_id == 1 (ValueError) and out-of-range page_id
             (PageOutOfRangeError) -- same checks Pager.free_page had.
          2. Call self.discard(page_id) as your own first step. It already
             raises ValueError if the page is still pinned, which is the
             right signal to surface -- a caller freeing a page it's still
             holding open is a bug, not something to paper over.
          3. NOW fetch page_id with get_page_for_write -- with the stale
             cache entry gone, this reads the real on-disk content (what
             this page held before being freed), which is exactly what a
             journal needs to see as the "original" here.
          4. Overwrite it: next = old freelist_trunk in the first 4 bytes,
             the REST OF THE PAGE ZEROED. Not just the next-pointer bytes --
             test_freed_pages_are_fully_zeroed's whole point is that stale
             bytes at offset 4 get misread as a leaf count by real sqlite3.
          5. freelist_trunk = page_id, freelist_count += 1.
        """
        # TODO(human): implement free_page per the contract above.
        if page_id == 1:
            raise ValueError("Cannot free page 1 (header page)")
        if page_id > self._pager._header.page_count:
            raise PageOutOfRangeError(f"Cannot free page {page_id} (out of range)")
        self.discard(page_id)
        page_content = self.get_page_for_write(page_id)
        next_pointer = self._pager._header.freelist_trunk
        page_content[:4] = next_pointer.to_bytes(4, byteorder='big')
        page_content[4:] = bytearray(len(page_content) - 4)  # Zero the rest of the page
        self.unpin(page_id)
        self._pager._header.freelist_trunk = page_id
        self._pager._header.freelist_count += 1

    @contextmanager
    def pinned(self, page_id: int, dirty: bool = False) -> Generator[bytearray]:
        """get_page(page_id), yield it, unpin(page_id, dirty) on the way out
        -- including when the body raises.
        """
        page = self.get_page(page_id)
        try:
            yield page
        finally:
            self.unpin(page_id, dirty)

    def get_page_for_write(self, page_id: int) -> bytearray:
        """The ONLY way to obtain a page you intend to modify.

        This is the seam the week-5 journal hooks into: once every mutating
        call site goes through here instead of get_page(), there is exactly
        one place in the whole codebase that can hand out a page destined to
        change, which is what lets a journal (session 3+) guarantee it has
        seen a page's original bytes before anyone mutates it.

        Contract (docs/implementation/week5-transactions.md, Session 0 Task 1):
          1. If self._txn is not None, call self._txn.will_modify(page_id)
             BEFORE anything else touches the page -- that ordering is the
             entire point of this method existing.
          2. Pin the page. Reuse get_page() for the hit/miss/LRU logic --
             don't reimplement it here.
          3. Mark the entry dirty immediately, at acquisition -- not
             deferred to a later unpin(dirty=True). A caller of this method
             has already declared intent; unpin() should not need to repeat it.
          4. Return the same bytearray get_page() would've returned.

        This session self._txn is always None, so step 1 is a no-op and the
        method's only visible effect is "dirty starts at acquisition instead
        of at unpin." Confirm that's true with a test before moving on --
        it's the whole reason this task is separable from session 3.
        """
        if self._txn is not None:
            self._txn.will_modify(page_id)
        page = self.get_page(page_id)
        entry = self._cache[page_id]
        entry.dirty = True
        return page

    @contextmanager
    def pinned_for_write(self, page_id: int) -> Generator[bytearray]:
        """get_page_for_write(page_id), yield it, unpin(page_id) on the way
        out -- including when the body raises. dirty is already set by
        get_page_for_write, so unlike pinned(), there's no dirty= to pass.
        """
        page = self.get_page_for_write(page_id)
        try:
            yield page
        finally:
            self.unpin(page_id)