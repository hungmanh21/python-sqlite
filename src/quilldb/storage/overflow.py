"""Overflow chain I/O: the linked list of pages holding a payload's spilled tail.


    page A:  [ next -> B  (u32) ][ up to USABLE_SIZE - 4 bytes of content ]
    page B:  [ next -> C  (u32) ][ ... ]
    page C:  [ next ->  0 (u32) ][ ... ]           <- 0 = end of chain, same
                                                       "0 means none" convention
                                                       as freelist_trunk/right_child


cells.py's local_payload_size() decides *how many bytes* stay local vs. spill;
this module is what actually walks the resulting chain on disk. It takes both
a Pager (to allocate/free pages) and a BufferPool (to read/write their bytes)
rather than inventing a combined type -- that's the same pair test_bufferpool.py
already wires together at every call site.


See docs/theory/btree/06-b-tree-mechanics.md §6.5.
"""


import math

from quilldb.constants import USABLE_SIZE
from quilldb.errors import OverflowCycleError
from quilldb.storage.bufferpool import BufferPool
from quilldb.storage.pager import Pager

CONTENT_PER_PAGE = USABLE_SIZE - 4  # 4 bytes spent on the next-page pointer




def write_overflow_chain(pager: Pager, pool: BufferPool, data: bytes) -> int:
    """Write `data` across as many freshly-allocated overflow pages as it needs.


    Args:
        pager: allocates each page in the chain via allocate_page().
        pool: writes each page's bytes (get_page_for_write + unpin(), so the
            pages participate in the shared cache like any other page).
        data: the spilled payload bytes -- everything past what cells.py kept
            local. Never empty: a payload that didn't spill has no chain to
            write in the first place.
    Returns:
        The page number of the first page in the chain.
    Raises:
        ValueError: data is empty.
    """
    if not data:
        raise ValueError("cannot write an overflow chain for empty data")


    page_count = math.ceil(len(data) / CONTENT_PER_PAGE)


    # A page's `next` pointer must be known BEFORE that page is written, but
    # allocate_page() only hands out numbers in the order you ask for them.
    # So allocate the whole chain up front, then write it in a second pass.
    page_ids = [pager.allocate_page() for _ in range(page_count)]


    for i, page_id in enumerate(page_ids):
        next_page_id = page_ids[i + 1] if i + 1 < page_count else 0


        # On every page but the last, this slice is exactly CONTENT_PER_PAGE
        # bytes. On the last page the stop index runs past len(data) --
        # Python silently clips it, so `chunk` comes out shorter with no
        # special case needed.
        chunk = data[i * CONTENT_PER_PAGE : (i + 1) * CONTENT_PER_PAGE]


        raw = pool.get_page_for_write(page_id)
        raw[0:4] = next_page_id.to_bytes(4, "big")
        raw[4 : 4 + len(chunk)] = chunk
        pool.unpin(page_id)


    return page_ids[0]




def read_overflow_chain(pager: Pager, pool: BufferPool, first_page: int, total_len: int) -> bytes:
    """Read exactly `total_len` bytes back out of the chain starting at `first_page`.


    Args:
        pager: unused directly, but kept symmetric with write_overflow_chain --
            free_overflow_chain below is the one that actually needs it.
        pool: reads each page's bytes.
        first_page: the page number write_overflow_chain returned, or a
            cell's decoded overflow_page field.
        total_len: exact byte count to return. The last page in the chain may
            hold padding past this point -- this is what bounds the read, not
            the page's own content.
    Returns:
        Exactly `total_len` bytes, in order.
    Raises:
        OverflowCycleError: the chain revisits a page number it already
            walked. Only possible if the file is corrupt -- tracking visited
            page ids in a set catches this regardless of chain length, unlike
            a hop-count cap, which a short loop could still slip past.
    """
    content = bytearray(total_len)
    visited: set[int] = set()
    current_page = first_page
    offset = 0


    while current_page != 0:
        if current_page in visited:
            raise OverflowCycleError(f"page {current_page} revisited -- overflow chain loops")
        visited.add(current_page)


        raw = pool.get_page(current_page)
        next_page = int.from_bytes(raw[0:4], "big")


        # Bounded by what's actually left to read, not by how full this page
        # is -- the real last page may hold fewer than CONTENT_PER_PAGE bytes
        # of genuine content, with padding past that we must not include.
        chunk_len = min(CONTENT_PER_PAGE, total_len - offset)
        content[offset : offset + chunk_len] = raw[4 : 4 + chunk_len]
        pool.unpin(current_page)


        offset += chunk_len
        current_page = next_page


    return bytes(content)




def free_overflow_chain(pager: Pager, pool: BufferPool, first_page: int) -> None:
    """Free every page in the overflow chain starting at `first_page`.


    The delete-side counterpart to write_overflow_chain: a deleted row's
    local cell is gone the moment its leaf is repacked, but the chain
    holding the rest of its payload is a separate set of pages that
    nothing else points at once the cell is gone -- BTree.delete() must
    reclaim them explicitly, or they leak (never corrupt, just permanently
    unusable space, the same failure test_space_is_reused_not_just_freed
    is designed to catch for leaf pages).


    Args:
        pager: frees each page via free_page() once the pool no longer
            caches it.
        pool: reads each page's `next` pointer before it's discarded --
            same get_page/unpin pair read_overflow_chain uses, since a
            page mid-chain may still be sitting in the shared cache.
        first_page: a cell's decoded overflow_page field. A no-op if 0
            (the payload never spilled, so there's no chain to free).
    Raises:
        OverflowCycleError: the chain revisits a page number it already
            walked -- same corruption signal as read_overflow_chain.
    """
    visited: set[int] = set()
    current_page = first_page


    while current_page != 0:
        if current_page in visited:
            raise OverflowCycleError(f"page {current_page} revisited -- overflow chain loops")
        visited.add(current_page)


        raw = pool.get_page(current_page)
        next_page = int.from_bytes(raw[0:4], "big")
        pool.unpin(current_page)
        pool.discard(current_page)
        pager.free_page(current_page)


        current_page = next_page
