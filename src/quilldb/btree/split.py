"""split_cells: the pure partition-and-promote decision behind a leaf split.


docs/theory/btree/06-b-tree-mechanics.md §6.4's strongest structural suggestion: write
the split as a function with no Pager, no BufferPool, no page at all -- just two
parallel lists in, two lists and a key out. That turns the hardest logic in the
project into something exhaustively unit-testable (every cell count, every size
distribution, both the rightmost and middle cases) before a single page gets
touched by _split_leaf() (btree.py, a later task).


_split_leaf() is the only intended caller: it decodes a full leaf's cells into
(cells, keys), calls this, writes `left_cells` back into the original page
(which now has room), writes `right_cells` into a freshly allocated page, and
promotes `separator` into the parent -- three page writes built on top of a
decision this module made without touching any of them.


The boundary convention this function must not get backwards (§6.1): "keys <=
separator go left." So the separator this returns is always the LARGEST key
ending up in the left half, never the smallest key in the right half -- get
that one-off in either direction and descent still looks fine everywhere
except for the one key that lands exactly on the new separator, which becomes
silently unreachable.


Keys are generic (each function declares its own `KeyT`), not hardcoded to
`int`: neither function here ever
compares two keys, only indexes into the list and hands one back, so the same
partition logic serves btree/index.py's decoded-tuple keys too (index.py's
own comparisons -- descent, duplicate detection -- go through compare_keys,
never Python's `<`/`>`, exactly because a key can mix NULL/int/text/blob).
"""


def split_cells[KeyT](
    cells: list[bytes], keys: list[KeyT], is_rightmost: bool
) -> tuple[list[bytes], list[bytes], KeyT]:
    """Partition a full leaf's cells into a left and right half, and pick the
    separator that must be promoted into the parent.


    Args:
        cells: every cell currently on the full leaf, in ascending key order
            -- exactly page.cells for a LEAF_TABLE (or LEAF_INDEX) page.
        keys: keys[i] is cells[i]'s key, same order, same length, already
            sorted ascending. Kept separate from cells so this function never
            needs to know a cell's byte format (decode_leaf_table_cell vs.
            decode_leaf_index_cell) -- the caller already decoded that once,
            to find out the leaf was full in the first place.
        is_rightmost: True if the leaf being split is the rightmost child of
            its parent (reached via right_child, not a separator cell). When
            True, peel off only the single last cell instead of roughly half
            (chapter 05 §5.7's optimisation) -- on a strictly-ascending
            insert pattern this is the difference between ~50% and ~100%
            page occupancy, because the left page is never asked to give up
            cells it will never be inserted into again.
    Returns:
        (left_cells, right_cells, separator):
            left_cells + right_cells == cells, in order -- nothing added,
            dropped, or reordered. separator == keys[len(left_cells) - 1],
            the largest key ending up in left_cells, per §6.1's boundary
            convention. When is_rightmost is False, left_size is chosen so
            each half's serialized footprint (not just its cell count) is
            as close to half the total as possible -- see
            _closest_to_half_by_bytes. Ties favor left_cells.
    Raises:
        ValueError: fewer than 2 cells -- there's nothing to usefully split.
    """
    if len(cells) < 2:
        raise ValueError("Cannot split a leaf with only 1 cell")


    if is_rightmost:
        left_size = len(cells) - 1
    else:
        left_size = _closest_to_half_by_bytes(cells)


    left_cells = cells[:left_size]
    right_cells = cells[left_size:]
    separator = keys[left_size - 1]


    return (left_cells, right_cells, separator)




def _closest_to_half_by_bytes(cells: list[bytes]) -> int:
    """The left_size in [1, len(cells) - 1] whose left half's serialized
    footprint (cell bytes + its 2-byte pointer, matching PageBody.fits())
    is closest to half of `cells`' total footprint. Ties -- which only
    happen with identically-sized cells -- favor the LARGER left_size,
    matching split_cells' pre-existing "left gets the extra cell on an odd
    count" convention.


    Cell size is NOT uniform here the way a plain cell-count halving
    assumes: cells.py's two's-complement varint encoding makes a very
    negative rowid/separator's cell up to 9x bigger than a small positive
    one's. A pure count-based split can leave one half still short of room
    for whatever happens to land there -- a byte-aware split is what
    actually guarantees each half got roughly its fair share of the space.
    """
    sizes = [len(cell) + 2 for cell in cells]
    target = sum(sizes) / 2


    running = 0
    best_size, best_diff = 1, None
    for left_size in range(1, len(cells)):
        running += sizes[left_size - 1]
        diff = abs(running - target)
        if best_diff is None or diff <= best_diff:
            best_size, best_diff = left_size, diff


    return best_size




def split_interior_cells[KeyT](
    cells: list[bytes],
    keys: list[KeyT],
    children: list[int],
    right_child: int,
    is_rightmost: bool,
) -> tuple[list[bytes], int, list[bytes], int, KeyT]:
    """Partition a full interior page's cells into a left and right half,
    and pick the separator that must be promoted to ITS parent -- the
    cascading step _split_leaf() doesn't do yet (btree.py, a later task).


    This is NOT split_cells() again. A leaf split's separator is a COPY of
    an existing key -- it stays in left_cells AND gets promoted. An
    interior split's separator is CONSUMED: one whole cell -- one
    (child, key) pair -- disappears from both halves. Its key becomes the
    thing promoted; its child pointer doesn't vanish, it becomes the left
    half's own right_child (§6.4). Get this backwards -- copy the
    separator instead of consuming it, or drop the consumed cell's child
    pointer instead of promoting it as right_child -- and the split still
    "looks" plausible (right counts, right key order) while quietly
    losing an entire subtree.


    Args:
        cells: every cell currently on the full interior page, in
            ascending separator order -- exactly page.cells for an
            INTERIOR_TABLE (or INTERIOR_INDEX) page.
        keys: keys[i] is cells[i]'s separator, same order/length as
            cells. Kept separate from cells (like split_cells' keys) so
            this function never decodes a cell itself.
        children: children[i] is cells[i]'s child page number, same
            order/length as cells -- the other half of what the caller
            already decoded once to discover the page was full. This
            function needs it because, unlike split_cells, it can't
            treat every cell as a fully opaque blob: one cell's child
            pointer has to survive independently of its (now-promoted)
            key.
        right_child: the page's own right_child -- the (N+1)-th child,
            covering everything past keys[-1]. Not present in `children`.
        is_rightmost: True if this interior page is itself the rightmost
            child of ITS OWN parent (reached via right_child, not a
            separator cell) -- mirrors split_cells' rightmost
            optimisation: peel off only the last cell instead of
            splitting near the middle, since an append-heavy insert
            pattern keeps landing here.
    Returns:
        (left_cells, left_right_child, right_cells, right_right_child, separator):
            Exactly one cell -- call its index `m` -- is consumed:
            separator == keys[m] and left_right_child == children[m].
            Every other cell ends up, unchanged, in left_cells (indices
            < m) or right_cells (indices > m). right_right_child is
            always the page's original right_child -- the split never
            touches the rightmost subtree, only which page owns the
            pointer to it. When is_rightmost is True, m == len(cells) - 1
            (right_cells comes out empty). When is_rightmost is False, m
            is chosen so cells[:m] and cells[m + 1:] end up as close in
            serialized footprint as possible -- see
            _consume_closest_to_half_by_bytes. Ties favor a larger m.
    Raises:
        ValueError: fewer than 2 cells -- there's nothing to usefully
            split (mirrors split_cells).
    """
    if len(cells) < 2:
        raise ValueError("not enough cells to split")


    if is_rightmost:
        m = len(cells) - 1
    else:
        m = _consume_closest_to_half_by_bytes(cells)


    separator = keys[m]
    left_right_child = children[m]


    left_cells = cells[:m]
    right_cells = cells[m + 1 :]


    right_right_child = right_child


    return (left_cells, left_right_child, right_cells, right_right_child, separator)




def _consume_closest_to_half_by_bytes(cells: list[bytes]) -> int:
    """The consumed index m in [0, len(cells) - 1] whose two remaining
    halves -- cells[:m] and cells[m + 1:] -- end up closest in total
    serialized footprint. Same reasoning as split_cells' analogous helper:
    a separator's two's-complement varint can be up to 9x bigger than
    another's, so a plain count-based midpoint doesn't guarantee either
    half actually got a fair share of the freed bytes. Ties favor a
    larger m (more cells left), matching split_cells' left-biased
    tie-break for the same odd/even reason.
    """
    sizes = [len(cell) + 2 for cell in cells]
    total = sum(sizes)
    prefix = [0]
    for size in sizes:
        prefix.append(prefix[-1] + size)


    best_m, best_diff = 0, None
    for m in range(len(cells)):
        # cells[:m] sums to prefix[m]; cells[m + 1:] sums to total minus
        # prefix[m + 1] (that slice, plus the consumed cell at m, plus
        # cells[:m] together make up the whole page).
        diff = abs(prefix[m] + prefix[m + 1] - total)
        if best_diff is None or diff <= best_diff:
            best_m, best_diff = m, diff


    return best_m