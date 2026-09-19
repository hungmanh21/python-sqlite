"""ANALYZE: measuring a real on-disk B-tree instead of guessing.


Stages 1-4 of the cost-based pipeline (predicates.py -> planner.py ->
statistics.py -> cost.py -> search.py) are all driven by TableStats/
IndexStats, but nothing yet PRODUCES a real one -- every stats.py caller
either has a real number (nothing does, yet) or falls back to
default_table_stats()/default_index_stats(). This module is where a real
number comes from: walk the actual B-tree pages and count.


The walk shape mirrors btree/validate.py's validate_btree() -- same DFS
over LEAF_TABLE/INTERIOR_TABLE pages via pool.get_page()/parse_page() --
but with a different job: validate_btree() re-checks invariants (key
order, child ranges, uniform height) that insert()/split.py are already
supposed to guarantee; this walk trusts those invariants (uniform height
is already proven elsewhere) and only counts. No re-validation here, on
purpose -- that's validate_btree()'s job, not this module's.
"""


from quilldb.btree.cells import (
    decode_interior_index_cell,
    decode_interior_table_cell,
    decode_leaf_index_cell,
)
from quilldb.codec.record import Value, decode_record
from quilldb.constants import PageType
from quilldb.plan.statistics import IndexStats, TableStats
from quilldb.storage.bufferpool import BufferPool
from quilldb.storage.overflow import read_overflow_chain
from quilldb.storage.page import parse_page
from quilldb.storage.pager import Pager




def measure_table(pager: Pager, pool: BufferPool, root: int) -> TableStats:
    """Walk the whole table B-tree rooted at `root` and return real stats.


    Args:
        pager: unused directly here (pool.get_page() already bounds-checks
            every page number) -- kept in the signature to match every
            other function in this codebase that touches a B-tree
            (validate_btree, IndexBTree, TableCursor all take `pager` even
            when a given call doesn't strictly need it).
        pool: reads every page reachable from root, via get_page()/unpin(),
            the same as validate_btree().
        root: the table B-tree's root page number.


    Returns:
        TableStats with:
          - `row_count`: total LEAF_TABLE cells across every leaf visited
            (one cell == one row).
          - `page_count`: total pages visited, leaves and interior nodes
            alike -- this is what a SeqScan actually pays for in page
            reads (cost.py's `SEQ_PAGE_COST * table_stats.page_count`).
          - `height`: depth from `root` down to the first leaf reached.
            Uniform height across every leaf is already a proven B-tree
            invariant (validate_btree() checks it elsewhere); this walk
            only needs ONE path's depth, not every leaf's.


    A single-page table (root IS a leaf) has height 1, matching
    validate_btree()'s own convention: "a leaf's own depth is 1."
    """
    row_count = 0
    page_count = 0


    def walk(page_id: int) -> int:
        nonlocal row_count, page_count
        raw = pool.get_page(page_id)
        try:
            body = parse_page(raw)
        finally:
            pool.unpin(page_id)


        page_count += 1


        if body.page_type is PageType.LEAF_TABLE:
            row_count += len(body.cells)
            return 1


        depth = 0
        for cell in body.cells:
            child, _ = decode_interior_table_cell(cell)
            depth = walk(child)
        depth = walk(body.right_child)
        return 1 + depth


    height = walk(root)
    return TableStats(row_count=row_count, page_count=page_count, height=height)




def _decode_index_key(pager: Pager, pool: BufferPool, page_type: PageType, cell: bytes) -> tuple[Value, ...]:
    """Decode one index cell's payload back into a key (declared columns
    plus rowid, in that order), reassembling it from its overflow chain
    first if it spilled. Same shape as IndexBTree._decode_key (btree/index.py)
    -- duplicated in miniature here rather than imported, since that method
    is private to IndexBTree and this walk has no IndexBTree instance of
    its own to call it on (it only has raw pages).
    """
    if page_type is PageType.LEAF_INDEX:
        total_len, local, overflow_page = decode_leaf_index_cell(cell)
    else:
        _, total_len, local, overflow_page = decode_interior_index_cell(cell)


    payload = local
    if overflow_page:
        payload = local + read_overflow_chain(pager, pool, overflow_page, total_len - len(local))
    return decode_record(payload)




def measure_index(pager: Pager, pool: BufferPool, root: int, n_key_columns: int) -> IndexStats:
    """Walk the whole index B-tree rooted at `root` and return real stats.


    Args:
        pager, pool, root: same meaning as measure_table's.
        n_key_columns: the number of DECLARED key columns (excluding the
            rowid every stored key ends in) -- same parameter IndexBTree's
            constructor takes, needed here to know how many prefix lengths
            `rows_per_prefix` should report (chapter 12 §12.5: one average
            per prefix length from 1 to K).


    Returns:
        IndexStats with:
          - `row_count`: total LEAF_INDEX cells across every leaf (one
            entry per indexed row, same meaning as TableStats.row_count).
          - `height`, `leaf_pages`: same page-walk shape as measure_table
            -- `height` is depth to the first leaf, `leaf_pages` is the
            count of LEAF_INDEX pages specifically (not all pages, unlike
            TableStats.page_count -- cost.py's IndexScan formula only ever
            scales `leaf_pages`, never counts interior pages, so that's the
            only page count worth keeping here).
          - `rows_per_prefix`: computed below -- this is the TODO(human).


    The page walk below (first loop) mirrors measure_table's DFS exactly,
    just over LEAF_INDEX/INTERIOR_INDEX pages via decode_interior_index_cell
    instead of decode_interior_table_cell. It leaves `leaf_keys` populated
    with every stored key IN INDEX ORDER (leaves are visited left to right,
    and _decode_index_key preserves per-leaf cell order) -- ready for the
    TODO(human) below to consume without doing any of its own tree
    traversal.
    """
    row_count = 0
    leaf_pages = 0
    leaf_keys: list[tuple[Value, ...]] = []


    def walk(page_id: int) -> int:
        nonlocal row_count, leaf_pages
        raw = pool.get_page(page_id)
        try:
            body = parse_page(raw)
        finally:
            pool.unpin(page_id)


        if body.page_type is PageType.LEAF_INDEX:
            leaf_pages += 1
            row_count += len(body.cells)
            for cell in body.cells:
                leaf_keys.append(_decode_index_key(pager, pool, PageType.LEAF_INDEX, cell))
            return 1


        depth = 0
        for cell in body.cells:
            child, _, _, _ = decode_interior_index_cell(cell)
            depth = walk(child)
        depth = walk(body.right_child)
        return 1 + depth


    height = walk(root)
    rows_per_prefix = _rows_per_prefix(leaf_keys, n_key_columns)
    return IndexStats(row_count=row_count, rows_per_prefix=rows_per_prefix, height=height, leaf_pages=leaf_pages)




def _rows_per_prefix(leaf_keys: list[tuple[Value, ...]], n_key_columns: int) -> tuple[int, ...]:
    """The one genuinely new piece of arithmetic in ANALYZE (chapter 12
    SS12.5): for each prefix length p from 1 to `n_key_columns`, the AVERAGE
    number of consecutive keys in `leaf_keys` that share the same value
    across their first p columns.


    `leaf_keys` arrives already in index order (measure_index's page walk
    visits leaves left to right), each key a tuple of
    (declared column values..., rowid) -- so key[:p] is "this key's value
    at prefix length p," and equal-prefix keys are always CONSECUTIVE in
    this list (that's what "index order" means -- no need to sort or group
    by hash).


    Concretely, for `10000 100 10 2` on index (a,b,c): rows_per_prefix[0]
    is the average run length when grouping consecutive keys by `a` alone,
    rows_per_prefix[1] by `(a,b)`, rows_per_prefix[2] by `(a,b,c)`.


    Returns:
        A tuple of length `n_key_columns`, position p-1 holding the
        average run length at prefix length p. An empty `leaf_keys` (an
        unanalyzed or empty index) has no runs to average -- return 1 for
        every prefix length in that case, matching
        estimate_row_counts()'s own "clamp to at least 1" rule so a caller
        never divides by zero or sees a 0 average.
    """
    if not leaf_keys:
        return tuple(1 for _ in range(n_key_columns))


    result = []
    for p in range(1, n_key_columns + 1):
        run_lengths = []
        cur_val = leaf_keys[0][:p]
        cur_length = 1
        for key in leaf_keys[1:]:
            val = key[:p]
            if val == cur_val:
                cur_length += 1
            else:
                run_lengths.append(cur_length)
                cur_length = 1
                cur_val = val
        run_lengths.append(cur_length)  # the final run never hit the else branch above


        result.append(sum(run_lengths) // len(run_lengths))
    return tuple(result)