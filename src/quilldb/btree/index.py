"""Index b-trees: the same B+tree with the payload thrown away.


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
from quilldb.constants import PageType
from quilldb.errors import PageFullError
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
    # TODO(human): one line. What does week 1's record encoder already
    # know how to do that makes this trivial?
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
    # TODO(human): for each of the first min(len(a), len(b)) columns, rank
    # the value's storage class (NULL / numeric / text / blob), compare
    # ranks first, and only compare same-class values directly. Return as
    # soon as one column decides the answer.
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


        Returns False if absent. Same uniform empty-page cascade as
        btree.py's BTree.delete() -- see that method's docstring for the
        two traps (removing a child from a parent is not "removing a row",
        and a page read through the pool must be discard()ed before it's
        freed) and the tolerated-single-child rule. The only real
        difference is that a "child" in the loop below is identified by
        slot, and an emptied interior page's own last cell/right_child is
        removed the same way regardless of whether its keys are ints or
        decoded tuples.
        """
        key = (*values, rowid)
        path = self._find_leaf(key)
        leaf_page_id, leaf_slot = path[-1]


        raw = self.pool.get_page(leaf_page_id)
        dirty = False
        try:
            body = parse_page(raw)
            if leaf_slot >= len(body.cells):
                return False
            found_key = self._decode_key(PageType.LEAF_INDEX, body.cells[leaf_slot])
            if compare_keys(found_key, key) != 0:
                return False
            _, _, overflow_page = decode_leaf_index_cell(body.cells[leaf_slot])


            body.delete_cell(leaf_slot)
            leaf_now_empty = not body.cells
            raw[:] = serialize_page(body)
            dirty = True
        finally:
            self.pool.unpin(leaf_page_id, dirty=dirty)


        if overflow_page:
            free_overflow_chain(self.pager, self.pool, overflow_page)


        if not leaf_now_empty or leaf_page_id == self.root:
            return True


        child_to_free = leaf_page_id
        level = len(path) - 2


        while True:
            parent_page_id, child_slot = path[level]
            parent_raw = self.pool.get_page(parent_page_id)
            dirty = False
            try:
                parent = parse_page(parent_raw)


                if child_slot < len(parent.cells):
                    parent.delete_cell(child_slot)
                elif parent.cells:
                    new_right_child = decode_interior_index_cell(parent.cells[-1])[0]
                    parent.delete_cell(len(parent.cells) - 1)
                    parent.right_child = new_right_child
                else:
                    parent.right_child = 0


                parent_is_empty = not parent.cells and parent.right_child == 0


                if level == 0 and parent_is_empty:
                    parent_raw[:] = serialize_page(PageBody(PageType.LEAF_INDEX))
                else:
                    parent_raw[:] = serialize_page(parent)
                dirty = True
            finally:
                self.pool.unpin(parent_page_id, dirty=dirty)


            self.pool.discard(child_to_free)
            self.pager.free_page(child_to_free)


            if level == 0 or not parent_is_empty:
                return True


            child_to_free = parent_page_id
            level -= 1


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


    def _next_leaf_path(self, path: list[tuple[int, int]]) -> list[tuple[int, int]] | None:
        """Ascend past an exhausted leaf until an ancestor has an unvisited
        next child, then descend leftmost from there. None if the whole
        tree is exhausted.
        """
        path = list(path)
        path.pop()
        while path:
            page_id, slot = path[-1]
            raw = self.pool.get_page(page_id)
            try:
                body = parse_page(raw)
                children = self._children(body)
            finally:
                self.pool.unpin(page_id)


            if slot + 1 < len(children):
                path[-1] = (page_id, slot + 1)
                return self._descend_leftmost(path, children[slot + 1])
            path.pop()


        return None


    def _scan_forward(self, path: list[tuple[int, int]]) -> Iterator[tuple[Value, ...]]:
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


            next_path = self._next_leaf_path(path)
            if next_path is None:
                return
            path = next_path


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
            left_cells, right_cells, separator = split_cells(combined_cells, combined_keys, use_append_split)


            if not (_fits_one_leaf_index_page(left_cells) and _fits_one_leaf_index_page(right_cells)):
                raise PageFullError(
                    "cannot split this index leaf: a key is too large to fit on either side of a "
                    "two-way split next to its neighbors -- needs three-way rebalancing, unimplemented"
                )


            # The separator's own cell is a FRESH, independent copy of its
            # payload -- including its own overflow chain, if it spills --
            # never a pointer shared with the leaf entry it was copied from.
            # Two cells that both thought they owned the same overflow chain
            # would double-free it the moment either one was deleted.
            separator_payload = encode_record(list(separator))
            sep_local_len = local_payload_size(PageType.INTERIOR_INDEX, len(separator_payload))
            sep_local_payload = separator_payload[:sep_local_len]
            sep_spills = sep_local_len < len(separator_payload)
            placeholder_separator_cell = encode_interior_index_cell(
                page_id, len(separator_payload), sep_local_payload, overflow_page=1 if sep_spills else 0
            )


            if parent is not None and not parent[2].fits(len(placeholder_separator_cell)):
                raise PageFullError(
                    "cannot promote this index leaf's split into its parent: the parent is "
                    "full and multi-level index splits aren't implemented yet"
                )


            if spills:
                overflow_page = write_overflow_chain(self.pager, self.pool, payload[local_len:])
                real_cell = encode_leaf_index_cell(len(payload), local_payload, overflow_page)
                if insert_slot < len(left_cells):
                    left_cells[insert_slot] = real_cell
                else:
                    right_cells[insert_slot - len(left_cells)] = real_cell


            right_page_id = self.pager.allocate_page()
            with self.pool.pinned(right_page_id, dirty=True) as right_raw:
                right_raw[:] = serialize_page(PageBody(PageType.LEAF_INDEX, cells=right_cells))


            sep_overflow_page = (
                write_overflow_chain(self.pager, self.pool, separator_payload[sep_local_len:]) if sep_spills else 0
            )
            new_left_cell = encode_interior_index_cell(
                page_id, len(separator_payload), sep_local_payload, sep_overflow_page
            )


            if parent is None:
                left_page_id = self.pager.allocate_page()
                with self.pool.pinned(left_page_id, dirty=True) as left_raw:
                    left_raw[:] = serialize_page(PageBody(PageType.LEAF_INDEX, cells=left_cells))


                new_left_cell = encode_interior_index_cell(
                    left_page_id, len(separator_payload), sep_local_payload, sep_overflow_page
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