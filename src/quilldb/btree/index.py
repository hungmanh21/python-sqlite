"""Index b-trees: a TRUE b-tree, unlike the table b-tree next door.


An index entry is a RECORD whose columns are (indexed values..., rowid) -- the
rowid is a record field with an integer serial type, NOT a bare varint
appended afterwards. See docs/theory/btree/11-index-b-trees.md §11.4, which
proves it: rowid 0 and 1 occupy ZERO body bytes (serial types 8 and 9), and no
varint can encode a value in zero bytes.


Pages are types 2 (interior index) and 10 (leaf index). Index leaf cells are
[payload-length varint][payload] -- no rowid varint, unlike table leaf cells
(cells.py). Unlike a table interior cell, an index interior cell carries the
FULL separator key (§11.6): the divider is itself a real entry, uncompressed.


Descent and duplicate-detection never compare keys with Python's `<`/`>` --
every comparison goes through compare_keys(), because a key can mix NULL,
numbers, text, and blob, and only compare_keys() knows SQLite's cross-type
order for those (§3.8). This mirrors btree.py's structure closely (same
_find_leaf/insert/split shape, same overflow-chain and pin-discipline
idioms) but is deliberately NOT a subclass or a shared base: a table
descends by comparing one int; an index descends by comparing a whole
decoded record, and forcing both through one abstraction would cost more
than the ~10 lines the two implementations actually share.


Scope cut, documented rather than silent: insert() splits at most ONE
level -- a full leaf splits and promotes directly into its parent if the
parent has room. If the parent is ALSO full, insert() raises PageFullError
rather than cascading further up the tree (the same "one level of split, no
cascade" starting point btree.py's own table insert() had before
_promote_separator existed). delete()'s empty-page cascade has no such
limit -- freeing pages back up an arbitrarily deep path is the same loop
regardless of how deep the tree got.
"""


from collections.abc import Iterator, Sequence
from typing import Any, cast

from quilldb.btree.cells import (
    decode_interior_index_cell,
    decode_leaf_index_cell,
    encode_interior_index_cell,
    encode_leaf_index_cell,
    local_payload_size,
)
from quilldb.btree.split import split_cells
from quilldb.codec.record import Value, decode_record, encode_record
from quilldb.constants import PAGE_SIZE, PageType
from quilldb.errors import BTreeInvariantError, PageFullError
from quilldb.storage.bufferpool import BufferPool
from quilldb.storage.overflow import free_overflow_chain, read_overflow_chain, write_overflow_chain
from quilldb.storage.page import PageBody, parse_page, serialize_page
from quilldb.storage.pager import Pager


def encode_index_key(values: Sequence[Value], rowid: int) -> bytes:
    """Build the index key for a row: the declared column values plus the
    rowid, as ONE record -- the rowid is a record field with its own serial
    type, not a bare varint bolted on afterwards. See this module's
    docstring and docs/theory/btree/11-index-b-trees.md §11.4, which proves
    it byte-for-byte: rowid 0 and 1 cost zero body bytes, and no varint can
    encode a value in zero bytes.


    Args:
        values: the indexed column values, in declared column order.
        rowid: the row this key points at.
    Returns:
        The encoded key -- exactly what a leaf/interior index cell's
        payload holds.
    """
    return encode_record((*values, rowid))




def compare_keys(a: Sequence[Value], b: Sequence[Value]) -> int:
    """Compare two decoded keys column by column, SQLite's cross-type order:
    NULL < numeric (int and float as ONE class) < text < blob (§3.8). BINARY
    collation only -- no case-folding, no locale.


    Compares min(len(a), len(b)) columns, not len(a) -- so a probe with
    fewer columns than a stored key still compares meaningfully. That's
    deliberate, and it's load-bearing for two things below: seek_eq() probes
    with only the leading columns (no rowid), against stored keys that
    always have one more column (the rowid) than the probe -- comparing
    just the shared prefix is exactly "do the leading columns match".
    Descent (_find_leaf) relies on the same trick to compare a probe against
    a full separator key.


    Args:
        a, b: decoded key tuples/lists -- e.g. decode_record()'s output, or
            a bare probe like ["Hanoi"].
    Returns:
        -1 if a < b, 0 if every compared column is equal, 1 if a > b.
    """
    def _rank(value: Value) -> int:
        if value is None:
            return 0
        if isinstance(value, (int, float)):
            return 1
        if isinstance(value, str):
            return 2
        # Value guarantees the only remaining type is bytes.
        return 3
   
    for item_a, item_b in zip(a, b):
        if item_a == item_b:
            continue


        rank_a = _rank(item_a)
        rank_b = _rank(item_b)


        if rank_a != rank_b:
            return -1 if rank_a < rank_b else 1


        # rank_a == rank_b here, so the two values are the same storage class
        # (or both numeric) and comparable at runtime -- Value's own type
        # can't express that to mypy, same narrowing gap _rowid_of works
        # around below.
        if cast(Any, item_a) < cast(Any, item_b):
            return -1
        return 1


    return 0
       
   




def _fits_one_leaf_index_page(cells: list[bytes]) -> bool:
    """Would `cells` serialize into one LEAF_INDEX page? Mirrors btree.py's
    _fits_one_leaf_page, checked ahead of a split so a caller can raise a
    clear error before anything is written.
    """
    return PageBody(PageType.LEAF_INDEX, cells=cells).free_bytes() >= 0




def _rowid_of(key: tuple[Value, ...]) -> int:
    """The rowid is always the last column of a decoded key, and it's
    always an int -- encode_index_key put it there as one. Value's own
    type (None | int | float | str | bytes) can't express that narrowing,
    so this is the one place that asserts it instead of threading a cast
    through every seek_eq/seek_range/scan call site.
    """
    return cast(int, key[-1])




class IndexBTree:
    def __init__(self, pager: Pager, pool: BufferPool, root: int, n_key_columns: int, *, unique: bool) -> None:
        """`n_key_columns` counts the DECLARED columns, excluding the rowid.


        `unique` changes only the conflict check (find_conflict), never the
        stored bytes -- a UNIQUE index is byte-identical to a non-unique one
        (§11.3).
        """
        self.pager = pager
        self.pool = pool
        self.root = root
        self.n_key_columns = n_key_columns
        self.unique = unique


    def insert(self, values: Sequence[Value], rowid: int) -> None:
        """Insert one (values, rowid) entry in key order.


        Raises:
            PageFullError: the target leaf was full and either a two-way
                split couldn't fit a too-large key on either side, or its
                parent has no room for the promoted separator -- see this
                module's docstring on the one-level-split scope cut.
        """
        key = (*values, rowid)
        payload = encode_index_key(values, rowid)
        path = self._find_leaf(key)
        page_id, slot = path[-1]


        raw = self.pool.get_page(page_id)
        dirty = False
        leaf_is_pinned = True
        try:
            body = parse_page(raw)
            local_len = local_payload_size(PageType.LEAF_INDEX, len(payload))
            local_payload = payload[:local_len]
            spills = local_len < len(payload)
            candidate = encode_leaf_index_cell(len(payload), local_payload, overflow_page=1 if spills else 0)


            if not body.fits(len(candidate)):
                self.pool.unpin(page_id)
                leaf_is_pinned = False
                self._split_leaf(page_id, path, key, payload)
                return


            overflow_page = write_overflow_chain(self.pager, self.pool, payload[local_len:]) if spills else 0
            cell = encode_leaf_index_cell(len(payload), local_payload, overflow_page)
            body.insert_cell(slot, cell)
            raw[:] = serialize_page(body)
            dirty = True
        finally:
            if leaf_is_pinned:
                self.pool.unpin(page_id, dirty=dirty)


    def seek_eq(self, values: Sequence[Value]) -> Iterator[int]:
        """Yield rowids whose leading columns equal `values`, in index order.


        This is a RANGE scan, not a point lookup: every stored key ends in a
        rowid, so no stored key ever equals a probe that omits it. Seeks to
        the first entry >= `values` and walks forward while the leading
        columns still match.
        """
        probe = tuple(values)
        rowids: list[int] = []
        for stored in self._scan_forward(self._find_leaf(probe)):
            if compare_keys(probe, stored) != 0:
                break
            rowids.append(_rowid_of(stored))
        return iter(rowids)


    def seek_range(
        self,
        low: Sequence[Value] | None = None,
        high: Sequence[Value] | None = None,
        *,
        low_inclusive: bool = True,
        high_inclusive: bool = True,
    ) -> Iterator[int]:
        """Yield rowids in key order within the bounds. None means unbounded."""
        low_probe = tuple(low) if low is not None else None
        high_probe = tuple(high) if high is not None else None


        path = self._find_leaf(low_probe) if low_probe is not None else self._descend_leftmost([], self.root)


        rowids: list[int] = []
        for stored in self._scan_forward(path):
            if low_probe is not None and not low_inclusive and compare_keys(low_probe, stored) == 0:
                continue
            if high_probe is not None:
                cmp_high = compare_keys(high_probe, stored)
                if cmp_high < 0 or (cmp_high == 0 and not high_inclusive):
                    break
            rowids.append(_rowid_of(stored))
        return iter(rowids)


    def scan(self) -> Iterator[tuple[list[Value], int]]:
        """Every entry in key order, as (key values, rowid). For validation."""
        path = self._descend_leftmost([], self.root)
        return iter([(list(stored[:-1]), _rowid_of(stored)) for stored in self._scan_forward(path)])


    def find_conflict(self, values: Sequence[Value]) -> int | None:
        """For UNIQUE indexes: the rowid of an existing entry with these key
        values, or None. NULL never conflicts -- SQL says NULLs aren't equal
        to each other, so a UNIQUE index accepts many NULL keys.
        """
        if any(v is None for v in values):
            return None
        return next(self.seek_eq(values), None)


    def delete(self, values: Sequence[Value], rowid: int) -> bool:
        """Remove the entry for exactly this (values, rowid).


        Returns False if absent. Unlike a table b-tree, the entry may live
        on an INTERIOR page: an index divider is itself a live entry
        (SS11.6), so there is no leaf copy to fall back on and deleting a
        divider is the classic delete-from-an-internal-node problem. The
        two cases split here and rejoin at _collapse_empty_leaf().
        """
        key = (*values, rowid)
        path, found_in_leaf = self._locate(key)
        if path is None:
            return False


        if not found_in_leaf:
            self._replace_divider_with_predecessor(path)
            return True


        leaf_page_id, leaf_slot = path[-1]
        raw = self.pool.get_page(leaf_page_id)
        dirty = False
        try:
            body = parse_page(raw)
            _, _, overflow_page = decode_leaf_index_cell(body.cells[leaf_slot])
            body.delete_cell(leaf_slot)
            leaf_now_empty = not body.cells
            raw[:] = serialize_page(body)
            dirty = True
        finally:
            self.pool.unpin(leaf_page_id, dirty=dirty)


        if overflow_page:
            free_overflow_chain(self.pager, self.pool, overflow_page)


        if leaf_now_empty:
            self._collapse_empty_leaf(path)
        return True


    def _locate(self, key: tuple[Value, ...]) -> tuple[list[tuple[int, int]] | None, bool]:
        """Find `key` exactly, anywhere in the tree.


        Returns (path, found_in_leaf). `path` is None when the key is
        absent. When found_in_leaf is False the path's last element is the
        INTERIOR page holding the entry, and its slot indexes the cell --
        the case a table b-tree never has to consider.
        """
        path: list[tuple[int, int]] = []
        page_id = self.root


        while True:
            current_page_id = page_id
            raw = self.pool.get_page(current_page_id)
            try:
                body = parse_page(raw)


                if body.page_type is PageType.LEAF_INDEX:
                    slot = self._leaf_lower_bound(body, key)
                    path.append((current_page_id, slot))
                    if slot >= len(body.cells):
                        return None, True
                    found = self._decode_key(PageType.LEAF_INDEX, body.cells[slot])
                    return (path, True) if compare_keys(found, key) == 0 else (None, True)


                slot = self._interior_slot(body, key)
                path.append((current_page_id, slot))
                # _interior_slot sends "probe <= separator" left, so an exact
                # match is always AT `slot`, never past it.
                if slot < len(body.cells):
                    separator = self._decode_key(PageType.INTERIOR_INDEX, body.cells[slot])
                    if compare_keys(separator, key) == 0:
                        return path, False
                page_id = self._children(body)[slot]
            finally:
                self.pool.unpin(current_page_id)


    def _rightmost_leaf_path(self, path: list[tuple[int, int]], page_id: int) -> list[tuple[int, int]]:
        """Mirror of _descend_leftmost: descend to the RIGHTMOST leaf of the
        subtree at `page_id`, appending one (page_id, slot) per level. An
        interior page's slot is len(cells) -- the right_child position --
        and the final leaf's slot indexes its last cell.
        """
        while True:
            current_page_id = page_id
            raw = self.pool.get_page(current_page_id)
            try:
                body = parse_page(raw)
                if body.page_type is PageType.LEAF_INDEX:
                    path.append((current_page_id, len(body.cells) - 1))
                    return path
                path.append((current_page_id, len(body.cells)))
                page_id = body.right_child
            finally:
                self.pool.unpin(current_page_id)


    def _replace_divider_with_predecessor(self, path: list[tuple[int, int]]) -> None:
        """Delete the entry stored in an INTERIOR cell, named by path[-1]'s
        (page_id, slot).


        A divider cannot simply be dropped: its cell also carries the child
        pointer for everything to its left, so removing the cell would
        orphan that whole subtree. The standard move is to overwrite the
        divider's PAYLOAD with a neighbouring entry promoted from a leaf --
        keeping the child pointer untouched -- and then delete that entry
        from the leaf it came from, where dropping a cell is safe.


        Helpers available:
            self._rightmost_leaf_path(list(path), child) -- path to the
                rightmost leaf of the subtree at `child`, whose last cell is
                the divider's in-order predecessor.
            self._children(body)[slot] -- the divider's left child.
            decode_leaf_index_cell / encode_interior_index_cell -- a leaf
                cell's (total_len, local, overflow_page) transplant into an
                interior cell unchanged, because both page types share one
                max-local formula.
            free_overflow_chain(self.pager, self.pool, page) -- release the
                divider's own spilled pages once it is overwritten.
            self._collapse_empty_leaf(leaf_path) -- run the empty-page
                cascade if the donor leaf just lost its last cell.
        """
        page_id, slot = path[-1]


        # The divider's child pointer is the one thing that must survive
        # untouched; its payload is what we are replacing, so its overflow
        # chain (if any) is about to become unowned.
        raw = self.pool.get_page(page_id)
        try:
            body = parse_page(raw)
            left_child, _, _, divider_overflow = decode_interior_index_cell(body.cells[slot])
        finally:
            self.pool.unpin(page_id)


        # PREDECESSOR, not successor: the rightmost entry of the left subtree
        # is the largest key still below the divider, so promoting it keeps
        # "everything left of this cell < its key" true without touching the
        # right subtree at all. The successor would work symmetrically, but
        # the left child is the pointer this cell already owns, so the
        # predecessor is the one reachable without consulting a sibling.
        leaf_path = self._rightmost_leaf_path(list(path), left_child)
        leaf_page_id, leaf_slot = leaf_path[-1]


        raw = self.pool.get_page(leaf_page_id)
        try:
            leaf = parse_page(raw)
            if not leaf.cells:
                raise BTreeInvariantError(
                    f"page {leaf_page_id}: the left subtree of the divider on page {page_id} "
                    f"ends in an empty leaf, so it has no predecessor to promote"
                )
            promoted_len, promoted_local, promoted_overflow = decode_leaf_index_cell(leaf.cells[leaf_slot])
        finally:
            self.pool.unpin(leaf_page_id)


        replacement = encode_interior_index_cell(left_child, promoted_len, promoted_local, promoted_overflow)


        # Check the swap fits BEFORE anything is mutated. The predecessor's
        # key can be longer than the divider it replaces, and serialize_page
        # only discovers an over-full page after the caller has committed to
        # the change -- which, with no rollback before week 5, would mean a
        # half-applied delete.
        raw = self.pool.get_page(page_id)
        dirty = False
        try:
            body = parse_page(raw)
            body.cells[slot] = replacement
            if body.used_bytes() > PAGE_SIZE:
                raise PageFullError(
                    f"cannot delete this index divider: its replacement key is longer and page "
                    f"{page_id} has no room -- needs interior rebalancing, unimplemented"
                )
            raw[:] = serialize_page(body)
            dirty = True
        finally:
            self.pool.unpin(page_id, dirty=dirty)


        # The promoted entry now lives upstairs and ONLY upstairs. Its
        # overflow chain moved with it -- exactly one owner, as always.
        raw = self.pool.get_page(leaf_page_id)
        dirty = False
        try:
            leaf = parse_page(raw)
            leaf.delete_cell(leaf_slot)
            leaf_now_empty = not leaf.cells
            raw[:] = serialize_page(leaf)
            dirty = True
        finally:
            self.pool.unpin(leaf_page_id, dirty=dirty)


        if divider_overflow:
            free_overflow_chain(self.pager, self.pool, divider_overflow)


        if leaf_now_empty:
            self._collapse_empty_leaf(leaf_path)


    def _collapse_empty_leaf(self, path: list[tuple[int, int]]) -> None:
        """Free a page that just lost its last cell, preserving the divider
        that pointed at it.


        This is where an index b-tree parts company with btree.py's cascade
        most sharply. There, an empty child is unlinked by deleting the
        parent cell that pointed at it, and nothing is lost because a table
        interior cell is pure routing. Here that cell is a live entry
        (SS11.6), so deleting it would silently drop a row from the index.


        So the divider DESCENDS instead -- the classic b-tree merge. The
        empty child is dropped and its divider is pushed into the sibling it
        used to separate, becoming an ordinary leaf entry there: prepended
        to the right sibling, or appended to the left one when the empty
        page was the rightmost child. Pages go away; entries never do.


        Scope cut, documented rather than silent: if the sibling has no room
        for the descending divider, the empty page is simply LEFT LINKED,
        empty. That wastes a page until something reuses it, and is
        deliberately preferred to raising -- a half-applied DELETE has no
        rollback to fall back on before week 5, so a delete that has already
        removed a cell must not then fail.
        """
        page_id, _ = path[-1]
        if page_id == self.root:
            return


        parent_page_id, child_slot = path[-2]


        parent_raw = self.pool.get_page(parent_page_id)
        parent_dirty = False
        unlink_parent_too = False
        freed = False
        try:
            parent = parse_page(parent_raw)
            children = self._children(parent)


            if not parent.cells:
                # A single-child interior page: no divider to rescue, so the
                # page itself is redundant once its only child is gone.
                parent.right_child = 0
                freed = True
                if parent_page_id == self.root:
                    parent_raw[:] = serialize_page(PageBody(PageType.LEAF_INDEX))
                else:
                    parent_raw[:] = serialize_page(parent)
                    unlink_parent_too = True
                parent_dirty = True
            else:
                if child_slot < len(parent.cells):
                    donor_slot, sibling_id, prepend = child_slot, children[child_slot + 1], True
                else:
                    donor_slot = len(parent.cells) - 1
                    sibling_id, prepend = children[donor_slot], False


                _, total_len, local, overflow_page = decode_interior_index_cell(parent.cells[donor_slot])
                descending = encode_leaf_index_cell(total_len, local, overflow_page)


                sibling_raw = self.pool.get_page(sibling_id)
                sibling_dirty = False
                merged = False
                try:
                    sibling = parse_page(sibling_raw)
                    if sibling.page_type is PageType.LEAF_INDEX and sibling.fits(len(descending)):
                        sibling.insert_cell(0 if prepend else len(sibling.cells), descending)
                        sibling_raw[:] = serialize_page(sibling)
                        sibling_dirty = True
                        merged = True
                finally:
                    self.pool.unpin(sibling_id, dirty=sibling_dirty)


                if merged:
                    parent.delete_cell(donor_slot)
                    if not prepend:
                        # The empty page WAS right_child; the sibling that
                        # absorbed the divider takes over that role.
                        parent.right_child = sibling_id
                    parent_raw[:] = serialize_page(parent)
                    parent_dirty = True
                    freed = True
        finally:
            self.pool.unpin(parent_page_id, dirty=parent_dirty)


        if freed:
            self.pool.discard(page_id)
            self.pager.free_page(page_id)
        if unlink_parent_too:
            self._collapse_empty_leaf(path[:-1])


    # ---- descent, keyed by compare_keys instead of int comparison --------


    def _decode_key(self, page_type: PageType, cell: bytes) -> tuple[Value, ...]:
        """Decode a leaf or interior index cell's payload back into a key,
        reassembling it across its overflow chain first if it spilled.
        """
        if page_type is PageType.LEAF_INDEX:
            total_len, local, overflow_page = decode_leaf_index_cell(cell)
        else:
            _, total_len, local, overflow_page = decode_interior_index_cell(cell)


        payload = local
        if overflow_page:
            payload = local + read_overflow_chain(self.pager, self.pool, overflow_page, total_len - len(local))
        return decode_record(payload)


    def _children(self, body: PageBody) -> list[int]:
        return [decode_interior_index_cell(cell)[0] for cell in body.cells] + [body.right_child]


    def _leaf_lower_bound(self, body: PageBody, probe: tuple[Value, ...]) -> int:
        """The leftmost index in body.cells whose stored key is >= probe."""
        lo, hi = 0, len(body.cells)
        while lo < hi:
            mid = (lo + hi) // 2
            stored = self._decode_key(PageType.LEAF_INDEX, body.cells[mid])
            if compare_keys(probe, stored) <= 0:
                hi = mid
            else:
                lo = mid + 1
        return lo


    def _interior_slot(self, body: PageBody, probe: tuple[Value, ...]) -> int:
        """Same boundary rule as btree.py's interior_slot_for_key (§6.1: a
        probe equal to a separator goes left), compared via compare_keys.
        """
        lo, hi = 0, len(body.cells)
        while lo < hi:
            mid = (lo + hi) // 2
            separator = self._decode_key(PageType.INTERIOR_INDEX, body.cells[mid])
            if compare_keys(probe, separator) <= 0:
                hi = mid
            else:
                lo = mid + 1
        return lo


    def _find_leaf(self, probe: tuple[Value, ...]) -> list[tuple[int, int]]:
        """Root-to-leaf path for where `probe` belongs -- same shape as
        BTree._find_leaf, one (page_id, child_slot) per level, ending in
        (leaf_page_id, insert_slot).
        """
        path: list[tuple[int, int]] = []
        page_id = self.root


        while True:
            current_page_id = page_id
            raw = self.pool.get_page(current_page_id)
            try:
                body = parse_page(raw)


                if body.page_type is PageType.LEAF_INDEX:
                    path.append((current_page_id, self._leaf_lower_bound(body, probe)))
                    return path


                slot = self._interior_slot(body, probe)
                path.append((current_page_id, slot))
                page_id = self._children(body)[slot]
            finally:
                self.pool.unpin(current_page_id)


    # ---- forward traversal, used by seek_eq/seek_range/scan --------------
    #
    # Every page touched below is pinned and unpinned within one call --
    # nothing is held open across a `yield` -- so an abandoned scan (a
    # caller that breaks out of a `for` loop early) can never leak a pin.


    def _descend_leftmost(self, path: list[tuple[int, int]], page_id: int) -> list[tuple[int, int]]:
        while True:
            current_page_id = page_id
            raw = self.pool.get_page(current_page_id)
            try:
                body = parse_page(raw)
                if body.page_type is PageType.LEAF_INDEX:
                    path.append((current_page_id, 0))
                    return path
                children = self._children(body)
                path.append((current_page_id, 0))
                page_id = children[0]
            finally:
                self.pool.unpin(current_page_id)


    def _scan_forward(self, path: list[tuple[int, int]]) -> Iterator[tuple[Value, ...]]:
        """Every entry from `path`'s position to the end of the tree, in key
        order -- interior entries included.


        This is a TRUE b-tree in-order walk, not a B+tree's leaf-to-leaf
        chain, because an index interior cell holds a live entry rather than
        a routing copy (SS11.6). The order is therefore


            child[0], cells[0], child[1], cells[1], ..., cells[n-1], right_child


        and an ancestor's cell is emitted on the way back UP out of the
        subtree it separates -- which is precisely when that key's turn
        comes. Skipping it, as a leaf-only walk does, silently drops one
        entry per interior cell from every scan, seek and uniqueness check
        built on this generator.


        Descent already agrees with this: _interior_slot sends a probe equal
        to a separator LEFT, so the subtree below is exhausted first and the
        separator itself follows in the right place.
        """
        path = list(path)
        while path:
            leaf_page_id, leaf_slot = path[-1]
            raw = self.pool.get_page(leaf_page_id)
            try:
                body = parse_page(raw)
                leaf_keys = [self._decode_key(PageType.LEAF_INDEX, cell) for cell in body.cells[leaf_slot:]]
            finally:
                self.pool.unpin(leaf_page_id)


            yield from leaf_keys


            # Ascend until an ancestor still has a cell to its right. Because
            # children == cells + [right_child], "slot < len(cells)" is both
            # "there is a separator here to emit" and "there is another child
            # after it" -- the two can never disagree.
            path.pop()
            descended = False
            while path:
                page_id, slot = path[-1]
                raw = self.pool.get_page(page_id)
                try:
                    body = parse_page(raw)
                    if slot < len(body.cells):
                        separator = self._decode_key(PageType.INTERIOR_INDEX, body.cells[slot])
                        next_child = self._children(body)[slot + 1]
                    else:
                        separator = None
                        next_child = 0
                finally:
                    self.pool.unpin(page_id)


                if separator is None:
                    path.pop()
                    continue


                yield separator
                path[-1] = (page_id, slot + 1)
                path = self._descend_leftmost(path, next_child)
                descended = True
                break


            if not descended:
                return


    # ---- one-level split (see module docstring for the scope cut) --------


    def _split_leaf(
        self, page_id: int, path: list[tuple[int, int]], key: tuple[Value, ...], payload: bytes
    ) -> None:
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
            keys = [self._decode_key(PageType.LEAF_INDEX, cell) for cell in body.cells]


            is_rightmost = parent is None or parent[3] == len(parent[2].cells)
            use_append_split = is_rightmost and (not keys or compare_keys(key, keys[-1]) > 0)


            local_len = local_payload_size(PageType.LEAF_INDEX, len(payload))
            local_payload = payload[:local_len]
            spills = local_len < len(payload)
            new_cell = encode_leaf_index_cell(len(payload), local_payload, overflow_page=1 if spills else 0)


            _, insert_slot = path[-1]
            combined_cells = body.cells[:insert_slot] + [new_cell] + body.cells[insert_slot:]
            combined_keys = keys[:insert_slot] + [key] + keys[insert_slot:]
            left_cells, right_cells, _separator = split_cells(combined_cells, combined_keys, use_append_split)


            # THE index-vs-table difference (SS11.6). split_cells() picks the
            # separator the way a TABLE leaf split needs it: as a COPY that
            # stays in left_cells and is also promoted. That is right when the
            # parent cell is pure routing ([child][rowid], no payload), because
            # then promoting it adds no entry. An index interior cell carries a
            # full record and IS a live entry, so copying would make the tree
            # gain one entry per split -- a duplicate row to anything reading
            # the file, and the reason `PRAGMA integrity_check` reports
            # "wrong # of entries in index". So the separator is CONSUMED here,
            # exactly as split_interior_cells() consumes one: it leaves the leaf
            # and lives only upstairs. A split rebalances; it never invents.
            promoted_index = len(left_cells) - 1
            promoted_cell = left_cells.pop()


            if not left_cells:
                raise PageFullError(
                    "cannot split this index leaf: promoting its separator would leave the left "
                    "page empty -- needs three-way rebalancing, unimplemented"
                )


            if not (_fits_one_leaf_index_page(left_cells) and _fits_one_leaf_index_page(right_cells)):
                raise PageFullError(
                    "cannot split this index leaf: a key is too large to fit on either side of a "
                    "two-way split next to its neighbors -- needs three-way rebalancing, unimplemented"
                )


            # A consumed separator hands its payload to the interior cell
            # wholesale -- including its overflow chain, whose pages simply
            # change owner. That is safe here precisely because it is a MOVE:
            # the leaf cell is gone, so exactly one cell points at the chain
            # and nothing can double-free it. It is also exact: LEAF_INDEX and
            # INTERIOR_INDEX share one max-local formula, so the local/spilled
            # boundary lands on the same byte for both and the local prefix
            # needs no re-cutting.
            promoted_len, promoted_local, promoted_overflow = decode_leaf_index_cell(promoted_cell)
            placeholder_separator_cell = encode_interior_index_cell(
                page_id, promoted_len, promoted_local, overflow_page=1 if promoted_overflow else 0
            )


            if parent is not None and not parent[2].fits(len(placeholder_separator_cell)):
                raise PageFullError(
                    "cannot promote this index leaf's split into its parent: the parent is "
                    "full and multi-level index splits aren't implemented yet"
                )


            if spills:
                overflow_page = write_overflow_chain(self.pager, self.pool, payload[local_len:])
                real_cell = encode_leaf_index_cell(len(payload), local_payload, overflow_page)
                # The new key may have landed on either side OR become the
                # promoted separator itself -- all three are reachable, since
                # split_cells chooses the boundary by bytes, not by slot.
                if insert_slot < promoted_index:
                    left_cells[insert_slot] = real_cell
                elif insert_slot == promoted_index:
                    promoted_cell = real_cell
                    promoted_len, promoted_local, promoted_overflow = decode_leaf_index_cell(real_cell)
                else:
                    right_cells[insert_slot - promoted_index - 1] = real_cell


            right_page_id = self.pager.allocate_page()
            with self.pool.pinned(right_page_id, dirty=True) as right_raw:
                right_raw[:] = serialize_page(PageBody(PageType.LEAF_INDEX, cells=right_cells))


            new_left_cell = encode_interior_index_cell(
                page_id, promoted_len, promoted_local, promoted_overflow
            )


            if parent is None:
                left_page_id = self.pager.allocate_page()
                with self.pool.pinned(left_page_id, dirty=True) as left_raw:
                    left_raw[:] = serialize_page(PageBody(PageType.LEAF_INDEX, cells=left_cells))


                new_left_cell = encode_interior_index_cell(
                    left_page_id, promoted_len, promoted_local, promoted_overflow
                )
                new_root = PageBody(PageType.INTERIOR_INDEX, cells=[new_left_cell], right_child=right_page_id)
                raw[:] = serialize_page(new_root)
                leaf_dirty = True
                return


            raw[:] = serialize_page(PageBody(PageType.LEAF_INDEX, cells=left_cells))
            leaf_dirty = True


            _, parent_raw, parent_body, child_slot = parent
            if is_rightmost:
                parent_body.insert_cell(child_slot, new_left_cell)
                parent_body.right_child = right_page_id
            else:
                _, old_len, old_local, old_overflow = decode_interior_index_cell(parent_body.cells[child_slot])
                parent_body.cells[child_slot] = encode_interior_index_cell(
                    right_page_id, old_len, old_local, old_overflow
                )
                parent_body.insert_cell(child_slot, new_left_cell)


            parent_raw[:] = serialize_page(parent_body)
            parent_dirty = True
        finally:
            self.pool.unpin(page_id, dirty=leaf_dirty)
            if parent is not None:
                self.pool.unpin(parent[0], dirty=parent_dirty)