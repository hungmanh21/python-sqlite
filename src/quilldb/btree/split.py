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
"""


def split_cells(
    cells: list[bytes], keys: list[int], is_rightmost: bool
) -> tuple[list[bytes], list[bytes], int]:
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
            convention. When is_rightmost is False and len(cells) is odd,
            the extra cell goes to left_cells.
    Raises:
        ValueError: fewer than 2 cells -- there's nothing to usefully split.
    """
    # TODO(human)
    if len(cells) < 2:
        raise ValueError("Cannot split a leaf with only 1 cell")
   
    left_cells, right_cells = [], []
   
    if is_rightmost:
        left_size = len(cells) - 1
        left_cells = cells[:left_size]
        right_cells = [cells[-1]]
   
    else:
        left_size = (len(cells) + 1) // 2
        left_cells = cells[:left_size]
        right_cells = cells[left_size:]
       
    seperator = keys[left_size - 1]


    return (left_cells, right_cells, seperator)




def split_interior_cells(
    cells: list[bytes],
    keys: list[int],
    children: list[int],
    right_child: int,
    is_rightmost: bool,
) -> tuple[list[bytes], int, list[bytes], int, int]:
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
            (right_cells comes out empty). When is_rightmost is False,
            m is near the middle -- exact tie-breaking on an odd cell
            count is your call; document whichever you pick.
    Raises:
        ValueError: fewer than 2 cells -- there's nothing to usefully
            split (mirrors split_cells).
    """
    # TODO(human)
    #
    # - Guard: if len(cells) < 2, raise ValueError (mirrors split_cells).
    #
    # - Pick m, the index of the cell that gets consumed:
    #     - is_rightmost=True  -> m = len(cells) - 1 (peel just the last
    #       cell; right_cells ends up empty)
    #     - is_rightmost=False -> m = somewhere near the middle. Same
    #       odd/even tie-break question as split_cells -- your call,
    #       document whichever you pick.
    #
    # - Read off cell m BEFORE slicing it out of anything:
    #     separator        = keys[m]
    #     left_right_child = children[m]
    #
    # - Slice around m -- note the "+1", unlike split_cells' plain
    #   left_size boundary, because index m itself belongs to neither half:
    #     left_cells  = cells[:m]
    #     right_cells = cells[m + 1:]
    #
    # - right_right_child is always the page's ORIGINAL right_child,
    #   unchanged -- the split never touches the rightmost subtree.
    #
    # - Return (left_cells, left_right_child, right_cells,
    #   right_right_child, separator) -- that exact order, matching the
    #   function's declared return type.
    if len(cells) < 2:
        raise ValueError("not enough cells to split")


    if is_rightmost:
        m = len(cells) - 1
    else:
        m = (len(cells) + 1) // 2
   
    seperator = keys[m]
    left_right_child = children[m]
   
    left_cells = cells[:m]
    right_cells = cells[m+1:]
   
    right_right_child = right_child
   
    return (left_cells, left_right_child, right_cells, right_right_child, seperator)