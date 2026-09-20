"""assign_cost tests: turning rows_fetched into a PlanCost (stage 3 of
chapter 12's pipeline).


The single-row IndexScan numbers reuse chapter 12 §12.6's own worked
EXPLAIN example verbatim (`startup=12.00  cost=24.01` for a 3-level tree
with rows_fetched=1) so passing them is evidence the formula matches the
chapter's own arithmetic, not just numbers we picked ourselves.
"""


from quilldb.plan.cost import CPU_PER_ROW, RANDOM_PAGE_COST, SEQ_PAGE_COST, assign_cost
from quilldb.plan.planner import AccessPath
from quilldb.plan.predicates import Predicate
from quilldb.plan.statistics import IndexStats, TableStats
from quilldb.sql.binder import BoundLiteral

_TABLE_STATS = TableStats(row_count=10_000, page_count=200, height=3)
_STATS = IndexStats(row_count=10_000, rows_per_prefix=(100, 10, 2), height=3, leaf_pages=100)




def _eq(column: str, value: object = 1) -> Predicate:
    literal = BoundLiteral(value)
    return Predicate(column, "=", literal, literal)




# =====================================================================
# constants: the model must actually be page-oriented
# =====================================================================




def test_random_page_cost_exceeds_seq_page_cost():
    assert RANDOM_PAGE_COST > SEQ_PAGE_COST




# =====================================================================
# SeqScan: flat page_count + per-row CPU, no dependence on rows_fetched
# =====================================================================




def test_seq_scan_cost_is_page_count_plus_cpu_per_row():
    path = AccessPath("seq_scan", None, (), (), rows_fetched=10_000, est_rows=10_000)
    result = assign_cost(path, None, _TABLE_STATS)
    expected = SEQ_PAGE_COST * _TABLE_STATS.page_count + CPU_PER_ROW * _TABLE_STATS.row_count
    assert result.cost is not None
    assert result.cost.total == expected




def test_seq_scan_has_zero_startup_cost():
    path = AccessPath("seq_scan", None, (), (), rows_fetched=10_000, est_rows=10_000)
    result = assign_cost(path, None, _TABLE_STATS)
    assert result.cost is not None
    assert result.cost.startup == 0.0




# =====================================================================
# IndexScan: chapter 12 §12.6's own worked EXPLAIN example
# =====================================================================




def test_index_scan_startup_is_random_page_cost_times_height():
    path = AccessPath("index_scan", None, (_eq("email"),), (), rows_fetched=1, est_rows=1)
    result = assign_cost(path, _STATS, _TABLE_STATS)
    assert result.cost is not None
    assert result.cost.startup == RANDOM_PAGE_COST * _STATS.height == 12.0




def test_index_scan_total_matches_documented_worked_example():
    """chapter 12 §12.6's own EXPLAIN: `startup=12.00  cost=24.01`."""
    path = AccessPath("index_scan", None, (_eq("email"),), (), rows_fetched=1, est_rows=1)
    result = assign_cost(path, _STATS, _TABLE_STATS)
    assert result.cost is not None
    assert round(result.cost.total, 2) == 24.01




# =====================================================================
# IndexScan: rows_fetched drives both the table-lookup term and the
# fraction of leaf pages walked
# =====================================================================




def test_index_scan_cost_grows_with_rows_fetched():
    few = AccessPath("index_scan", None, (_eq("a"),), (), rows_fetched=2, est_rows=2)
    many = AccessPath("index_scan", None, (_eq("a"),), (), rows_fetched=100, est_rows=100)
    cost_few = assign_cost(few, _STATS, _TABLE_STATS).cost
    cost_many = assign_cost(many, _STATS, _TABLE_STATS).cost
    assert cost_few is not None and cost_many is not None
    assert cost_many.total > cost_few.total




def test_index_scan_leaf_pages_touched_is_clamped_to_at_least_one():
    """Even a single-row seek touches a whole leaf page, not a fraction
    of one -- rounding down to 0 would make that page look free.
    """
    path = AccessPath("index_scan", None, (_eq("a"),), (), rows_fetched=1, est_rows=1)
    result = assign_cost(path, _STATS, _TABLE_STATS)
    assert result.cost is not None
    # if leaf pages touched were 0, the SEQ_PAGE_COST term would go negative
    assert result.cost.total >= RANDOM_PAGE_COST * _STATS.height




def test_index_scan_costs_more_than_seq_scan_when_low_selectivity():
    """A boolean-style index fetching half the table should often lose
    to a plain SeqScan -- chapter 12 §12.6 trap #2, the reason SeqScan
    must remain a candidate even when a seek is legal.
    """
    half_table = 5_000
    low_selectivity_stats = IndexStats(row_count=10_000, rows_per_prefix=(half_table,), height=3, leaf_pages=100)
    seq_path = AccessPath("seq_scan", None, (), (), rows_fetched=10_000, est_rows=10_000)
    index_path = AccessPath("index_scan", None, (_eq("flag"),), (), rows_fetched=half_table, est_rows=half_table)


    seq_cost = assign_cost(seq_path, None, _TABLE_STATS).cost
    index_cost = assign_cost(index_path, low_selectivity_stats, _TABLE_STATS).cost
    assert seq_cost is not None and index_cost is not None
    assert index_cost.total > seq_cost.total




def test_index_scan_leaf_page_term_scales_with_leaf_pages_touched():
    """Fetching half an index's rows must walk roughly half its leaf
    pages -- the `SEQ_PAGE_COST * (leaf_pages_touched - 1)` term has to
    actually scale with `stats.leaf_pages`, not just with rows_fetched
    alone, or a wide index with many leaf pages would look identical in
    cost to a narrow one fetching the same row count.
    """
    wide_index = IndexStats(row_count=10_000, rows_per_prefix=(5_000,), height=3, leaf_pages=1_000)
    narrow_index = IndexStats(row_count=10_000, rows_per_prefix=(5_000,), height=3, leaf_pages=10)
    path = AccessPath("index_scan", None, (_eq("a"),), (), rows_fetched=5_000, est_rows=5_000)


    wide_cost = assign_cost(path, wide_index, _TABLE_STATS).cost
    narrow_cost = assign_cost(path, narrow_index, _TABLE_STATS).cost
    assert wide_cost is not None and narrow_cost is not None
    assert wide_cost.total > narrow_cost.total




# =====================================================================
# every other field survives untouched (AccessPath is frozen -- a new
# instance comes back, not a mutated one)
# =====================================================================




def test_rows_fetched_and_est_rows_are_preserved():
    path = AccessPath("index_scan", None, (_eq("a"),), (_eq("c", 3),), rows_fetched=100, est_rows=33)
    result = assign_cost(path, _STATS, _TABLE_STATS)
    assert result.rows_fetched == 100
    assert result.est_rows == 33
    assert result.kind == "index_scan"