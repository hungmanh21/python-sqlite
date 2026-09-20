"""TableCursor: a movable position in a table b-tree (docs/theory/06-b-tree-mechanics.md §6.3).


search.py and btree.py only ever answer "where is this one key" and then let go of every page
they touched. A cursor is different: it *stays* somewhere, and has to be able to move to the
next or previous row without starting the descent over from the root. That means it needs the
whole root-to-leaf path, not just the leaf -- because when a leaf runs out of cells, finding the
next one means walking up to the parent, taking its next child, and redescending (§6.3).


One divergence from the theory chapter's `path: list[tuple[int, int]]` sketch, worth an ADR: this
codebase's BufferPool charges one pin per get_page() call and expects exactly one matching unpin()
(storage/bufferpool.py). Re-fetching an ancestor page just to inspect it while ascending would
double-pin it. So `path` here is `list[tuple[int, int, PageBody]]` -- each entry keeps the PageBody
it already parsed on the way down, so ascending never needs to touch the pool again until the
entry is popped and unpinned for good.


Pinning (§6.3 point 1): every page in `path` is held pinned from the moment it's added until it's
popped or the cursor is closed. Always close() a cursor when you're done with it -- or use it as a
context manager -- or an abandoned scan leaks every pin on its path forever.


Mutation policy (§6.3 point 3), decided now per the chapter's advice: a cursor makes no promises
across a mutation to its tree. There's no insert yet for this to matter against; it becomes load
-bearing the moment Task 4 exists, and is worth revisiting then rather than guessed at now.
"""


from typing import Self

from quilldb.btree.cells import decode_leaf_table_cell
from quilldb.btree.search import children_of_interior, interior_slot_for_key, leaf_slot_lower_bound
from quilldb.constants import PageType
from quilldb.storage.bufferpool import BufferPool
from quilldb.storage.overflow import read_overflow_chain
from quilldb.storage.page import PageBody, parse_page
from quilldb.storage.pager import Pager


class TableCursor:
    """A root-to-leaf path over one table b-tree, that can move forward or back."""


    def __init__(self, pager: Pager, pool: BufferPool, root: int) -> None:
        self.pager = pager
        self.pool = pool
        self.root = root
        self.path: list[tuple[int, int, PageBody]] = []  # (page_id, slot, body), root -> leaf


    def __enter__(self) -> Self:
        return self


    def __exit__(self, *exc_info: object) -> None:
        self.close()


    @property
    def valid(self) -> bool:
        """True if the cursor is positioned on a real row."""
        return bool(self.path)


    def close(self) -> None:
        """Unpin every page still held on `path`, leaving the cursor unpositioned."""
        for page_id, _, _ in self.path:
            self.pool.unpin(page_id)
        self.path = []


    def rowid(self) -> int:
        """The rowid at the cursor's current position.


        Raises:
            ValueError: the cursor isn't positioned (see `valid`).
        """
        if not self.path:
            raise ValueError("cursor is not positioned")
        _, slot, body = self.path[-1]
        rowid, *_ = decode_leaf_table_cell(body.cells[slot])
        return rowid


    def first(self) -> None:
        """Position at the smallest rowid in the tree (or leave the cursor
        unpositioned, if the tree is empty).
        """
        self.close()
        self._descend_to_leaf(self.root, rightmost=False)


    def last(self) -> None:
        """Position at the largest rowid in the tree (or leave the cursor
        unpositioned, if the tree is empty).
        """
        self.close()
        self._descend_to_leaf(self.root, rightmost=True)


    def next(self) -> bool:
        """Move to the next-larger rowid.


        Returns:
            True if the cursor is now positioned on a row. False if it just
            walked off the end of the tree -- the cursor is left
            unpositioned, same as if `first()` had never been called.
        """
        if not self.path:
            return False
        page_id, slot, body = self.path[-1]
        if slot + 1 < len(body.cells):
            self.path[-1] = (page_id, slot + 1, body)
            return True
        return self._advance()


    def prev(self) -> bool:
        """Move to the next-smaller rowid. Mirror image of `next()`."""
        if not self.path:
            return False
        page_id, slot, body = self.path[-1]
        if slot - 1 >= 0:
            self.path[-1] = (page_id, slot - 1, body)
            return True
        return self._retreat()


    def seek(self, key: int) -> bool:
        """Position at `key`, or at the smallest rowid greater than it if absent.


        Args:
            key: the rowid to look for.
        Returns:
            True if `key` itself was found. False otherwise -- the cursor is
            still left positioned at the next row up (so a range scan like
            `WHERE rowid >= key` can just call seek() then repeatedly
            next()), or unpositioned if no rowid in the tree is >= key.
        """
        self.close()
        page_id = self.root


        while True:
            raw = self.pool.get_page(page_id)
            body = parse_page(raw)


            if body.page_type == PageType.INTERIOR_TABLE:
                slot = interior_slot_for_key(body, key)
                self.path.append((page_id, slot, body))
                page_id = children_of_interior(body)[slot]
                continue


            idx = leaf_slot_lower_bound(body, key)
            if idx < len(body.cells):
                self.path.append((page_id, idx, body))
                row_id, _, _, _ = decode_leaf_table_cell(body.cells[idx])
                return row_id == key


            # This leaf is not retained in path, so release its pin before
            # _advance() resumes from the recorded ancestor slots. Its True
            # means "positioned on a later row", not "key matched".
            self.pool.unpin(page_id)
            self._advance()
            return False


    def record(self) -> bytes:
        """The full payload at the cursor's current position, reassembled
        across the overflow chain if it spilled.


        Raises:
            ValueError: the cursor isn't positioned (see `valid`).
        """
        if not self.path:
            raise ValueError("cursor is not positioned")


        _, slot, body = self.path[-1]
        _, total_payload_len, local_payload, overflow_page = decode_leaf_table_cell(body.cells[slot])
        if overflow_page == 0:
            return local_payload


        remaining_payload = read_overflow_chain(
            self.pager,
            self.pool,
            overflow_page,
            total_payload_len - len(local_payload),
        )
        return local_payload + remaining_payload


    def _descend_to_leaf(self, start_page_id: int, rightmost: bool) -> None:
        """Walk from `start_page_id` to a leaf, pinning and appending every
        page visited to self.path. Powers first()/last(), and the
        "redescend after bumping an ancestor" step of next()/prev().


        Args:
            start_page_id: where to start descending from -- not necessarily
                self.root; next()/prev() call this partway down the tree.
            rightmost: at each interior level, take the last child instead
                of the first. Leaves self.path unchanged (empty, if called
                from first()/last()) when the target leaf has no cells --
                an empty tree has nothing to position on.
        """
        page_id = start_page_id
        while True:
            raw = self.pool.get_page(page_id)
            body = parse_page(raw)


            if body.page_type == PageType.LEAF_TABLE:
                if not body.cells:
                    self.pool.unpin(page_id)
                    return
                slot = len(body.cells) - 1 if rightmost else 0
                self.path.append((page_id, slot, body))
                return


            kids = children_of_interior(body)
            slot = len(kids) - 1 if rightmost else 0
            self.path.append((page_id, slot, body))
            page_id = kids[slot]


    def _advance(self) -> bool:
        """Move past an exhausted leaf: pop ancestors until one has a next
        child to try, bump its slot, and redescend leftmost from there.


        Returns:
            True if a next row was found (self.path is positioned on it).
            False if the whole path was exhausted -- self.path is now
            empty, same end state as walking off the end of the tree.
        """
        while self.path:
            page_id, slot, body = self.path[-1]
            if body.page_type != PageType.LEAF_TABLE:
                kids = children_of_interior(body)
                if slot + 1 < len(kids):
                    self.path[-1] = (page_id, slot + 1, body)
                    self._descend_to_leaf(kids[slot + 1], rightmost=False)
                    return True


            self.pool.unpin(page_id)
            self.path.pop()


        return False


    def _retreat(self) -> bool:
        """Mirror image of _advance(): move past an exhausted-backward leaf."""
        while self.path:
            page_id, slot, body = self.path[-1]
            if body.page_type != PageType.LEAF_TABLE and slot > 0:
                self.path[-1] = (page_id, slot - 1, body)
                self._descend_to_leaf(
                    children_of_interior(body)[slot - 1],
                    rightmost=True,
                )
                return True
            self.pool.unpin(page_id)
            self.path.pop()


        return False



