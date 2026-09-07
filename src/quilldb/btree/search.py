"""Search within a single table b-tree page: binary search a leaf, pick a child on an interior.


Both functions here are pure -- a PageBody in, an answer out, no Pager or BufferPool. That's
deliberate (docs/theory/06-b-tree-mechanics.md §6.1): it lets you test the search logic against
a page you built by hand, so a failure is unambiguously in search, not in the I/O or the parser
underneath it. btree.py is what adds the page-fetching loop on top of these.


One thing this codebase gets for free that raw SQLite doesn't: SQLite binary-searches the raw
2-byte pointer array and only decodes a cell once a candidate slot looks promising, because its
cells live in arbitrary disk order. Here, storage/page.py's parse_page() already walked that
pointer array once and returned PageBody.cells in key order -- so "binary search the slot array"
has already happened by the time you have a PageBody, and searching page.cells directly by index
is the same algorithm, just one layer higher.


The boundary convention (§6.1), which descent and split must forever agree on:


    Pointers left of separator X => all keys <= X; pointers right of X => all keys > X.


i.e. a key equal to a separator goes LEFT. Get this one-off in either direction and every leaf
still looks structurally valid -- exactly one key silently becomes unreachable.
"""


from quilldb.btree.cells import decode_interior_table_cell, decode_leaf_table_cell
from quilldb.storage.page import PageBody




def binary_search_leaf(page: PageBody, key: int) -> int | None:
    """Find `key` among a LEAF_TABLE page's cells.


    Args:
        page: page.page_type must be PageType.LEAF_TABLE. page.cells is in
            ascending rowid order (parse_page's guarantee), which is exactly
            what makes binary search valid here.
        key: the rowid to find.
    Returns:
        The index into page.cells whose rowid == key, or None if absent.
    """
    cells = page.cells
    lo, hi = 0, len(cells) - 1


    while lo <= hi:
        mid = lo + (hi - lo) // 2
        rowid, *_ = decode_leaf_table_cell(cells[mid])
        if rowid == key:
            return mid
        if key > rowid:
            lo = mid + 1
        else:
            hi = mid - 1


    return None




def child_for_key(page: PageBody, key: int) -> int:
    """Pick which child to descend into for `key`, on an INTERIOR_TABLE page.


    Args:
        page: page.page_type must be PageType.INTERIOR_TABLE. page.cells
            holds (child_page, separator) pairs in ascending separator order;
            page.right_child is the child past the last separator.
        key: the rowid being searched for.
    Returns:
        The child page number to descend into next: the child of the
        leftmost cell whose separator is >= key (per the §6.1 convention,
        key <= separator means "go left"), or page.right_child if key is
        greater than every separator on this page.
    """
    # Same shape as binary_search_leaf, but this is a boundary search, not an
    # exact-match one: narrow [lo, hi) to the leftmost index whose separator
    # is >= key. `key <= separator` keeps mid as a candidate (hi = mid) --
    # that's the §6.1 convention, an equal separator must stay a candidate
    # so an exact match still resolves left, not past it.
    cells = page.cells
    lo, hi = 0, len(cells)


    while lo < hi:
        mid = lo + (hi - lo) // 2
        _, separator = decode_interior_table_cell(cells[mid])
        if key <= separator:
            hi = mid
        else:
            lo = mid + 1


    # lo == len(cells) means every separator was < key -- nothing left of
    # right_child qualifies. Otherwise lo is the winning cell's index.
    if lo == len(cells):
        return page.right_child
    child_page, _ = decode_interior_table_cell(cells[lo])
    return child_page



