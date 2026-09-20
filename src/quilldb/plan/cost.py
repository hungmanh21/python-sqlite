"""Stage 3 of the cost-based pipeline (chapter 12 §12.6): turning an
AccessPath's row estimates into a page-read-equivalent PlanCost.


This is the stage that actually makes the planner cost-based rather than
rule-based (§12.5's distinction): stage 1 decided which paths are LEGAL,
stage 2 estimated how many rows each path touches, and this stage prices
that in page reads so choose_access_path() (not yet built) has a number
to minimize. Three constants, transparent rather than copied from
SQLite's internal units:


  SEQ_PAGE_COST    = 1.0   -- a page reached by walking forward
  RANDOM_PAGE_COST = 4.0   -- a page reached by a fresh root-to-leaf descent
  CPU_PER_ROW       = 0.01 -- per-row comparison/eval cost, kept small so
                              I/O dominates until row counts get large


RANDOM_PAGE_COST must exceed SEQ_PAGE_COST, or the model isn't actually
page-oriented -- it would be counting page touches without distinguishing
random access from sequential. The 4:1 ratio is PostgreSQL's conventional
default, not a measurement of this project's disk.
"""


import math

from quilldb.plan.planner import AccessPath, PlanCost
from quilldb.plan.statistics import IndexStats, TableStats

SEQ_PAGE_COST = 1.0
RANDOM_PAGE_COST = 4.0
CPU_PER_ROW = 0.01




def assign_cost(path: AccessPath, stats: IndexStats | None, table_stats: TableStats) -> AccessPath:
    """Fill in `cost` (chapter 12 §12.6 stage 3).


    Call this AFTER estimate_row_counts() -- it reads `path.rows_fetched`,
    which stage 2 fills in and stage 1 leaves at 0.


    Args:
        path: an AccessPath with `rows_fetched`/`est_rows` already set by
            estimate_row_counts(). `cost` is still at its None default.
        stats: this path's index statistics, or None for a seq_scan (a
            seq_scan has no index, so it costs off `table_stats` alone).
        table_stats: the table's own shape -- `row_count`, `page_count`,
            and `height`, always available (falls back to
            default_table_stats() if unanalyzed).


    Returns:
        A NEW AccessPath (it's frozen) with `cost` set to a PlanCost.
        Every other field is unchanged from `path`.


    The two formulas, from §12.6:


        SeqScan   = SEQ_PAGE_COST * table_stats.page_count
                  + CPU_PER_ROW * table_stats.row_count


        IndexScan = RANDOM_PAGE_COST * stats.height              -- the seek
                  + SEQ_PAGE_COST * (leaf_pages_touched - 1)      -- further leaves
                  + path.rows_fetched * (RANDOM_PAGE_COST * table_stats.height)
                  + CPU_PER_ROW * path.rows_fetched


    `leaf_pages_touched` isn't stored directly -- a seek that only
    matches a fraction of the index's rows only walks that same fraction
    of its leaf pages. Scale `stats.leaf_pages` by
    `path.rows_fetched / stats.row_count`, round UP (a seek touching even
    one row still touches a whole page), and clamp to at least 1 for the
    same reason `rows_fetched` itself gets clamped: a path that touches
    zero pages would look free.


    The `- 1` on the leaf-page term matters: `stats.height` already
    counts the leaf level, so the root-to-leaf descent (the
    `RANDOM_PAGE_COST * stats.height` term) has already paid for the
    first leaf page. Only pages walked forward from there are sequential.


    `PlanCost.startup` is the cost paid before the first row: for a
    SeqScan that's 0.0 (the first page read produces rows immediately);
    for an IndexScan it's the seek alone, `RANDOM_PAGE_COST *
    stats.height` -- forming a plan without it would make the search
    numbers not match what a caller sees.


    `PlanCost.total` is startup plus everything else -- the number
    choose_access_path() ranks candidates on.


    quilldb doesn't yet track which columns a query needs, so there's no
    `covering` check here: every IndexScan is priced as if it must look
    up the full row, even when a real covering index would skip that.
    That's the conservative direction to be wrong in -- it can only make
    an index look worse than it is, never invisibly better.


    Worked check against chapter 12's own EXPLAIN example
    (`startup=12.00  cost=24.01`): stats.height == table_stats.height ==
    3, stats.leaf_pages == 100, stats.row_count == 10_000,
    path.rows_fetched == 1 gives leaf_pages_touched == 1 (so the
    `SEQ_PAGE_COST * (leaf_pages_touched - 1)` term drops to 0), startup
    == 4.0 * 3 == 12.0, and total == 12.0 + 0 + 1 * (4.0 * 3) + 0.01 * 1
    == 24.01.
    """
    if path.kind == 'seq_scan':
        cost = SEQ_PAGE_COST * table_stats.page_count + CPU_PER_ROW * table_stats.row_count
        startup = 0.0
    else:
        assert stats is not None, "index_scan requires stats -- only seq_scan costs off table_stats alone"
        leaf_pages_touched = max(1, math.ceil(stats.leaf_pages * path.rows_fetched / stats.row_count))
        cost = (
            SEQ_PAGE_COST * (leaf_pages_touched - 1)
            + path.rows_fetched * (RANDOM_PAGE_COST * table_stats.height)
            + CPU_PER_ROW * path.rows_fetched
        )
        startup = RANDOM_PAGE_COST * stats.height
    return AccessPath(
        kind=path.kind,
        index=path.index,
        seek_terms=path.seek_terms,
        residual=path.residual,
        rows_fetched=path.rows_fetched,
        est_rows=path.est_rows,
        cost=PlanCost(
            startup=startup,
            total=startup + cost
        )
    )