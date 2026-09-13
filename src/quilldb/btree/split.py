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