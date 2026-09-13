"""validate_btree(): the structural safety net for a table b-tree (§6.6).


Every check here re-derives, from scratch, a fact insert()/split.py's
functions are SUPPOSED to guarantee -- key order, child key ranges, uniform
height, no page owned twice, no overflow cycle. Nothing in the insert path
calls this; it exists so a test (or a human, mid-debugging) can ask "is this
tree actually well-formed?" as one question, instead of inferring an answer
indirectly from a wrong search() result or an infinite scan.


Split bugs are invisible at the point of failure (§6.4/§6.1): a leaf split
that gets the "keys <= separator go left" boundary backwards by one key
still produces a tree that *parses* -- every page is individually well
formed -- and only fails later, on the one key that silently became
unreachable. validate_btree() converts that into an immediate, precise
failure at the point the bad structure was actually written, not whenever a
lookup happens to land on the missing key.


Table b-tree only: a page whose type is LEAF_INDEX or INTERIOR_INDEX is
itself a violation here, not something this module knows how to descend
into (there is no comparable validate_index_btree() yet).


Some checks below are free, already enforced by code this module calls
rather than duplicating:
    - Page bounds (1 <= page_id <= pager.page_count): pool.get_page() ->
      Pager.read_page() already raises PageOutOfRangeError.
    - Cell bounds (a cell's offset/length inside its page): parse_page()
      already raises MalformedCellError for every page visited.
    - Overflow cycles: read_overflow_chain() already raises
      OverflowCycleError if a chain revisits a page.
The checks that are NOT free -- because nothing else in this codebase has a
reason to compute them -- are the ones this function actually has to write:
key order, child key ranges, page types, uniform height, and no page reached
more than once while walking the tree.
"""


from quilldb.btree.cells import decode_interior_table_cell, decode_leaf_table_cell
from quilldb.constants import PageType
from quilldb.errors import BTreeInvariantError
from quilldb.storage.bufferpool import BufferPool
from quilldb.storage.overflow import read_overflow_chain
from quilldb.storage.page import parse_page
from quilldb.storage.pager import Pager




def validate_btree(pager: Pager, pool: BufferPool, root: int) -> None:
    """Walk the whole table b-tree rooted at `root` and confirm every
    structural invariant search()/insert() depend on silently holds.


    Args:
        pager: supplies page_count; pool.get_page() already bounds-checks
            every page number against it, so you don't need to check
            page_id ranges yourself.
        pool: reads every page reachable from root, the same way search()
            and insert() do (get_page + unpin -- no writes, so `dirty` is
            never True here).
        root: the b-tree's root page number.
    Raises:
        BTreeInvariantError: any of the following is violated:
            - Page types: every leaf visited is LEAF_TABLE; every interior
              page (including root, if it's not itself a leaf) is
              INTERIOR_TABLE. LEAF_INDEX/INTERIOR_INDEX are never valid here.
            - Key order: a LEAF_TABLE page's rowids are strictly ascending;
              an INTERIOR_TABLE page's separators are strictly ascending.
            - Child key ranges (§6.1's boundary convention): every key
              under cells[i]'s child is <= cells[i]'s separator and (if
              i > 0) > cells[i-1]'s separator; every key under right_child
              is > the last separator on the page.
            - Reachability / no duplicate ownership: no page number is
              visited more than once while walking the tree -- a page
              reachable from two different (child_slot, parent) pairs, or
              twice from the same parent, means some subtree's ownership is
              ambiguous, which a tree must never allow.
            - Uniform height: every leaf is the same number of levels below
              `root`.
        PageOutOfRangeError, InvalidPageTypeError, MalformedCellError:
            propagated unchanged from pool.get_page()/parse_page() if a
            page reference is out of range or a page is internally
            malformed -- see module docstring for why these are this
            module's problem to trigger but not to re-implement.
        OverflowCycleError: propagated unchanged from read_overflow_chain()
            if a spilled leaf cell's overflow chain loops (see
            decode_leaf_table_cell's overflow_page field).
    """
    visited: set[int] = set()


    def walk(page_id: int, low: int | None, high: int | None) -> int:
        """Validate the subtree rooted at `page_id`, whose keys must all
        satisfy low < key <= high (either bound None means unbounded), and
        return its leaf depth (a leaf's own depth is 1).
        """
        if page_id in visited:
            raise BTreeInvariantError(f"page {page_id} is reachable more than once")
        visited.add(page_id)


        raw = pool.get_page(page_id)
        try:
            body = parse_page(raw)
        finally:
            pool.unpin(page_id)


        if body.page_type not in (PageType.LEAF_TABLE, PageType.INTERIOR_TABLE):
            raise BTreeInvariantError(f"page {page_id} has non-table page type {body.page_type!r}")


        if body.page_type is PageType.LEAF_TABLE:
            prev_key: int | None = None
            for cell in body.cells:
                rowid, total_payload_len, local_payload, overflow_page = decode_leaf_table_cell(cell)
                if prev_key is not None and rowid <= prev_key:
                    raise BTreeInvariantError(f"page {page_id}: rowid {rowid} out of order after {prev_key}")
                if low is not None and rowid <= low:
                    raise BTreeInvariantError(f"page {page_id}: rowid {rowid} <= lower bound {low}")
                if high is not None and rowid > high:
                    raise BTreeInvariantError(f"page {page_id}: rowid {rowid} > upper bound {high}")
                if overflow_page:
                    read_overflow_chain(pager, pool, overflow_page, total_payload_len - len(local_payload))
                prev_key = rowid
            return 1


        # INTERIOR_TABLE: each cell's child covers (bound, separator], where
        # `bound` starts at this page's own `low` and becomes each separator
        # in turn; right_child covers everything past the last separator.
        prev_key = None
        bound = low
        depths = []
        for cell in body.cells:
            child, separator = decode_interior_table_cell(cell)
            if prev_key is not None and separator <= prev_key:
                raise BTreeInvariantError(f"page {page_id}: separator {separator} out of order after {prev_key}")
            if low is not None and separator <= low:
                raise BTreeInvariantError(f"page {page_id}: separator {separator} <= lower bound {low}")
            if high is not None and separator > high:
                raise BTreeInvariantError(f"page {page_id}: separator {separator} > upper bound {high}")
            depths.append(walk(child, bound, separator))
            bound = separator
            prev_key = separator


        depths.append(walk(body.right_child, bound, high))


        if len(set(depths)) != 1:
            raise BTreeInvariantError(f"page {page_id}: children have uneven heights {depths}")


        return 1 + depths[0]


    walk(root, None, None)