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

from quilldb.errors import PoolExhaustedError
from quilldb.storage.pager import Pager


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
            return entry.data


        # Check happens BEFORE inserting the new page, so hitting capacity
        # exactly (not just exceeding it) is what must trigger an eviction.
        if len(self._cache) >= self._capacity:
            self._evict_one()


        data = self._pager.read_page(page_id)
        self._cache[page_id] = _Entry(data=data, pin_count=1, dirty=False)
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
