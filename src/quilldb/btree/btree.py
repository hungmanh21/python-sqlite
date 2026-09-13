"""BTree: the root-to-leaf descent, on top of search.py's per-page decisions.


search.py answers "where in THIS page" -- pure, no I/O. This module answers "which page is
the root, and which pages do I visit on the way down", which is the part that actually touches
the Pager and BufferPool. See docs/theory/btree/06-b-tree-mechanics.md §6.1's `search()` sketch; this
is that sketch made to match this codebase's actual page-parsing and pinning API.


`insert()` descends through `_find_leaf()`, which records the page and selected child slot at
each level. The easy case uses only the final leaf entry; a future split uses the parent entries
to promote a separator back up the tree.
"""


from quilldb.btree.cells import (
    decode_interior_table_cell,
    decode_leaf_table_cell,
    encode_interior_table_cell,
    encode_leaf_table_cell,
    local_payload_size,
)
from quilldb.btree.search import (
    binary_search_leaf,
    child_for_key,
    interior_slot_for_key,
    leaf_slot_lower_bound,
)
from quilldb.btree.split import split_cells
from quilldb.constants import PageType
from quilldb.errors import DuplicateRowIDError, PageFullError
from quilldb.storage.bufferpool import BufferPool
from quilldb.storage.overflow import write_overflow_chain
from quilldb.storage.page import PageBody, parse_page, serialize_page
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
        """Insert a new row into the leaf it belongs on, splitting that leaf
        if it has no room (§6.4's easy case: one level of split, no cascade).


        Descends with `_find_leaf()` to the leaf `rowid` belongs on, then
        places a new cell there in sorted order. A payload too big for the
        page spills into an overflow chain first, via the same
        local_payload_size()/write_overflow_chain() pair cursor.record()
        already trusts to read one back. If the target leaf is full,
        `_split_leaf()` takes over instead of raising.


        What's deliberately not here: cascading. If a split's promoted
        separator doesn't fit on the parent either, this raises rather than
        splitting the parent too -- that's a later task.


        Args:
            rowid: the new row's key. Must not already exist in the tree.
            payload: the row's full encoded record, any length.
        Raises:
            DuplicateRowIDError: `rowid` is already present in the tree.
            PageFullError: the target leaf was full AND splitting it would
                need a promoted separator the parent has no room for either.
            InvalidPageTypeError, MalformedCellError: propagated unchanged
                from parse_page/decode_* if a page is corrupt.
        """
        path = self._find_leaf(rowid)
        page_id, slot = path[-1]
        raw = self.pool.get_page(page_id)
        body = parse_page(raw)


        # Keep the target leaf pinned while preparing its replacement. The
        # finally block releases it on every success and error path.
        dirty = False
        leaf_is_pinned = True
        try:
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
                # _split_leaf owns its own pins. Release this easy-case pin
                # before handing the unpinned path over to it.
                self.pool.unpin(page_id)
                leaf_is_pinned = False
                self._split_leaf(page_id, path, rowid, payload)
                return


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
            if leaf_is_pinned:
                self.pool.unpin(page_id, dirty=dirty)


    def _find_leaf(self, rowid: int) -> list[tuple[int, int]]:
        """Return the unpinned root-to-leaf path for where `rowid` belongs.


        Each interior entry is `(page_id, child_slot)`, where `child_slot`
        identifies the child selected by the same boundary rule as search().
        The final leaf entry is `(page_id, insert_slot)`. Split code can
        reload the recorded pages while promoting a separator upward.
        """
        path: list[tuple[int, int]] = []
        page_id = self.root


        while True:
            current_page_id = page_id
            raw = self.pool.get_page(current_page_id)
            try:
                body = parse_page(raw)


                if body.page_type is PageType.LEAF_TABLE:
                    path.append((current_page_id, leaf_slot_lower_bound(body, rowid)))
                    return path


                child_slot = interior_slot_for_key(body, rowid)
                path.append((current_page_id, child_slot))
                page_id = child_for_key(body, rowid)
            finally:
                self.pool.unpin(current_page_id)


    def _split_leaf(
        self, page_id: int, path: list[tuple[int, int]], rowid: int, payload: bytes
    ) -> None:
        """Split a full leaf, insert the new row into whichever half it
        belongs on, and promote a separator (§6.4's split, one level only --
        no cascade into a full parent yet, that's a later task).


        `path` came from `_find_leaf()` and holds no pins; this method owns
        every pin it acquires and must release every one, on every path.


        Two shapes, both covered by test_btree_splits.py:


        - `len(path) == 1`: `page_id` IS `self.root`, and the whole tree is
          one leaf. Two new pages are allocated (left and right); `self.root`
          keeps its page number (§6.4's "root page number never changes"
          trick) but is rewritten in place as the new INTERIOR_TABLE root,
          pointing at both.
        - `len(path) > 1`: the leaf has a parent at `path[-2] = (parent_id,
          child_slot)`. Only ONE new page is allocated (right); `page_id`
          itself is rewritten in place to hold the left half, and the
          separator is promoted into the parent at `child_slot`.


        Args:
            page_id: the full leaf `insert()` already found room to be
                missing on -- not yet re-pinned; `insert()` released its pin
                before calling this.
            path: `_find_leaf(rowid)`'s return value. `path[-1]` is
                `(page_id, insert_slot)` -- the same slot `insert()` would
                have used had the leaf not been full.
            rowid, payload: the row that didn't fit; see insert()'s Args.
        Raises:
            PageFullError: the parent has no room for the promoted
                separator. Checked BEFORE any page is written, so a raise
                here leaves the tree completely untouched -- cascading this
                into a further split is out of scope here, but failing
                cleanly instead of destroying the leaf's data isn't.
        """
        raw = self.pool.get_page(page_id)
        leaf_dirty = False


        parent: tuple[int, bytearray, PageBody, int] | None = None
        parent_dirty = False
        if len(path) > 1:
            parent_id, child_slot = path[-2]
            parent_raw = self.pool.get_page(parent_id)
            parent = (parent_id, parent_raw, parse_page(parent_raw), child_slot)


        try:
            body = parse_page(raw)
            keys = [decode_leaf_table_cell(cell)[0] for cell in body.cells]


            is_rightmost = parent is None or parent[3] == len(parent[2].cells)
            left_cells, right_cells, separator = split_cells(body.cells, keys, is_rightmost)


            # Promoting `separator` is the only parent mutation that can grow
            # the page (the non-rightmost branch's other edit is a same-size
            # replace) -- so this is the one check that must happen before
            # ANY write below. Getting this order wrong means a PageFullError
            # here would be raised only after the leaf was already
            # overwritten and the right half's page already allocated,
            # silently losing whichever cells landed in that orphaned page.
            if parent is not None:
                new_left_cell = encode_interior_table_cell(page_id, separator)
                if not parent[2].fits(len(new_left_cell)):
                    raise PageFullError(
                        "Cannot promote separator into a full parent; cascading split not implemented"
                    )


            local_len = local_payload_size(PageType.LEAF_TABLE, len(payload))
            overflow_page = (
                write_overflow_chain(self.pager, self.pool, payload[local_len:])
                if local_len < len(payload)
                else 0
            )
            new_cell = encode_leaf_table_cell(rowid, len(payload), payload[:local_len], overflow_page)


            # insert_slot is where the new cell would land in the ORIGINAL,
            # unsplit, sorted list -- so it's < len(left_cells) exactly when
            # the new key is smaller than every key that moved right.
            _, insert_slot = path[-1]
            if insert_slot < len(left_cells):
                left_cells = left_cells[:insert_slot] + [new_cell] + left_cells[insert_slot:]
            else:
                right_slot = insert_slot - len(left_cells)
                right_cells = right_cells[:right_slot] + [new_cell] + right_cells[right_slot:]


            right_page_id = self.pager.allocate_page()
            with self.pool.pinned(right_page_id, dirty=True) as right_raw:
                right_raw[:] = serialize_page(PageBody(PageType.LEAF_TABLE, cells=right_cells))


            if parent is None:
                # Root split: self.root (== page_id) keeps its page number
                # and becomes the new interior root; its old content moves
                # into a freshly allocated left leaf.
                left_page_id = self.pager.allocate_page()
                with self.pool.pinned(left_page_id, dirty=True) as left_raw:
                    left_raw[:] = serialize_page(PageBody(PageType.LEAF_TABLE, cells=left_cells))


                new_root = PageBody(
                    PageType.INTERIOR_TABLE,
                    cells=[encode_interior_table_cell(left_page_id, separator)],
                    right_child=right_page_id,
                )
                raw[:] = serialize_page(new_root)
                leaf_dirty = True
                return


            # Non-root split: page_id keeps the left half in place -- only
            # the right half needed a new page. Promote a separator into
            # the parent at child_slot.
            raw[:] = serialize_page(PageBody(PageType.LEAF_TABLE, cells=left_cells))
            leaf_dirty = True


            _, parent_raw, parent_body, child_slot = parent
            if is_rightmost:
                parent_body.insert_cell(child_slot, new_left_cell)
                parent_body.right_child = right_page_id
            else:
                _, old_separator = decode_interior_table_cell(parent_body.cells[child_slot])
                parent_body.cells[child_slot] = encode_interior_table_cell(right_page_id, old_separator)
                parent_body.insert_cell(child_slot, new_left_cell)


            parent_raw[:] = serialize_page(parent_body)
            parent_dirty = True
        finally:
            self.pool.unpin(page_id, dirty=leaf_dirty)
            if parent is not None:
                self.pool.unpin(parent[0], dirty=parent_dirty)