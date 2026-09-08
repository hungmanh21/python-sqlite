"""BTree: the root-to-leaf descent, on top of search.py's per-page decisions.


search.py answers "where in THIS page" -- pure, no I/O. This module answers "which page is
the root, and which pages do I visit on the way down", which is the part that actually touches
the Pager and BufferPool. See docs/theory/06-b-tree-mechanics.md §6.1's `search()` sketch; this
is that sketch made to match this codebase's actual page-parsing and pinning API.


`insert()` reuses this same descent shape (TableCursor, in cursor.py, keeps the path stack this
function throws away after each hop -- insert doesn't need to keep it, since it never walks back
up). Split is not here yet: insert() raises PageFullError on a leaf with no room rather than
making room, which is deliberate -- §6.4's "easy case" comes before the split cascade.
"""


from quilldb.btree.cells import decode_leaf_table_cell, encode_leaf_table_cell, local_payload_size
from quilldb.btree.search import binary_search_leaf, child_for_key, leaf_slot_lower_bound
from quilldb.constants import PageType
from quilldb.errors import DuplicateRowIDError, PageFullError
from quilldb.storage.bufferpool import BufferPool
from quilldb.storage.overflow import write_overflow_chain
from quilldb.storage.page import parse_page, serialize_page
from quilldb.storage.pager import Pager




class BTree:
    """A table b-tree rooted at a fixed page."""


    def __init__(self, pager: Pager, pool: BufferPool, root: int) -> None:
        self.pager = pager
        self.pool = pool
        self.root = root


    def search(self, key: int) -> tuple[int, int] | None:
        """Find `key`'s cell, descending from the root.


        Args:
            key: the rowid to find.
        Returns:
            (page_id, slot) of the matching cell on a LEAF_TABLE page, where
            slot is the index binary_search_leaf found it at -- or None if no
            leaf in the tree contains `key`.
        Raises:
            InvalidPageTypeError, MalformedCellError: propagated unchanged
                from parse_page/decode_* if a page is corrupt.
        """
        page_id = self.root


        while True:
            raw = self.pool.get_page(page_id)
            body = parse_page(raw)
            self.pool.unpin(page_id)


            if body.page_type == PageType.LEAF_TABLE:
                slot = binary_search_leaf(body, key)
                return (page_id, slot) if slot is not None else None


            page_id = child_for_key(body, key)


    def insert(self, rowid: int, payload: bytes) -> None:
        """Insert a new row into the leaf it belongs on (§6.4's easy case only).


        Descends exactly like search() -- same child_for_key loop -- to the
        leaf `rowid` belongs on, then places a new cell there in sorted
        order. A payload too big for the page spills into an overflow chain
        first, via the same local_payload_size()/write_overflow_chain() pair
        cursor.record() already trusts to read one back.


        What's deliberately not here: splitting. If the target leaf has no
        room, this raises rather than making room -- that's a later task,
        and it needs split_cells() and separator promotion to exist first.


        Args:
            rowid: the new row's key. Must not already exist in the tree.
            payload: the row's full encoded record, any length.
        Raises:
            DuplicateRowIDError: `rowid` is already present in the tree.
            PageFullError: the target leaf has no room for the new cell.
            InvalidPageTypeError, MalformedCellError: propagated unchanged
                from parse_page/decode_* if a page is corrupt.
        """
        page_id = self.root


        while True:
            raw = self.pool.get_page(page_id)
            body = parse_page(raw)
            if body.page_type is PageType.LEAF_TABLE:
                break
            self.pool.unpin(page_id)
            page_id = child_for_key(body, rowid)


        # Keep the target leaf pinned while preparing its replacement. The
        # finally block releases it on every success and error path.
        dirty = False
        try:
            slot = leaf_slot_lower_bound(body, rowid)
            if slot < len(body.cells):
                existing_rowid = decode_leaf_table_cell(body.cells[slot])[0]
                if existing_rowid == rowid:
                    raise DuplicateRowIDError("Duplicate row id")


            local_len = local_payload_size(PageType.LEAF_TABLE, len(payload))
            local_payload = payload[:local_len]
            spills = local_len < len(payload)


            # Check space before allocating an overflow chain. The placeholder
            # has the same four-byte footprint as a real overflow page number.
            candidate = encode_leaf_table_cell(
                rowid,
                len(payload),
                local_payload,
                overflow_page=1 if spills else 0,
            )


            if not body.fits(len(candidate)):
                raise PageFullError("Cannot insert anymore cells")


            overflow_page = (
                write_overflow_chain(self.pager, self.pool, payload[local_len:])
                if spills
                else 0
            )
            cell = encode_leaf_table_cell(rowid, len(payload), local_payload, overflow_page)


            body.insert_cell(slot, cell)
            raw[:] = serialize_page(body)
            dirty = True
        finally:
            self.pool.unpin(page_id, dirty=dirty)