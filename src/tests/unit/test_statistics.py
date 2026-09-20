"""estimate_row_counts tests: turning quill_stat1 prefix averages into
rows_fetched/est_rows on an AccessPath (stage 2 of chapter 12's pipeline).


The composite-index numbers reuse chapter 12 §12.5's own worked example
(`10000 100 10 2` on index (a,b,c)) so passing these tests is evidence the
lookup does what the chapter describes, not just what we guessed.
"""


from quilldb.catalog.schema import IndexSchema
from quilldb.plan.planner import AccessPath
from quilldb.plan.predicates import Predicate
from quilldb.plan.statistics import IndexStats, TableStats, estimate_row_counts
from quilldb.sql.binder import BoundLiteral

_ABC = IndexSchema("abc", "t", ("a", "b", "c"), unique=False, root_page=3, sql="CREATE INDEX abc ON t(a,b,c)")
_STATS = IndexStats(row_count=10_000, rows_per_prefix=(100, 10, 2))
_TABLE_STATS = TableStats(row_count=10_000)




def _eq(column: str, value: object = 1) -> Predicate:
    literal = BoundLiteral(value)
    return Predicate(column, "=", literal, literal)




def _cmp(column: str, operator: str, value: object = 1) -> Predicate:
    literal = BoundLiteral(value)
    return Predicate(column, operator, literal, literal)




# =====================================================================
# seq_scan: rows_fetched is always the whole table, no residual shrinkage
# =====================================================================




def test_seq_scan_fetches_the_whole_table():
    path = AccessPath("seq_scan", None, (), (_eq("c", 3),))
    result = estimate_row_counts(path, None, _TABLE_STATS)
    assert result.rows_fetched == 10_000




# =====================================================================
# index_scan rows_fetched: chapter 12 §12.5's own worked numbers
# =====================================================================




def test_single_column_seek_uses_first_prefix_average():
    path = AccessPath("index_scan", _ABC, (_eq("a"),), ())
    result = estimate_row_counts(path, _STATS, _TABLE_STATS)
    assert result.rows_fetched == 100




def test_two_column_seek_uses_second_prefix_average():
    path = AccessPath("index_scan", _ABC, (_eq("a"), _eq("b")), ())
    result = estimate_row_counts(path, _STATS, _TABLE_STATS)
    assert result.rows_fetched == 10




def test_full_key_seek_uses_third_prefix_average():
    path = AccessPath("index_scan", _ABC, (_eq("a"), _eq("b"), _eq("c")), ())
    result = estimate_row_counts(path, _STATS, _TABLE_STATS)
    assert result.rows_fetched == 2




def test_sandwiched_inequality_counts_as_one_column_not_two():
    """`a=1 AND b>2 AND b<5` has THREE seek_terms but only TWO distinct
    columns (a, b) -- rows_fetched must come from rows_per_prefix[1], not
    rows_per_prefix[2], or a sandwich would look falsely more selective
    than it is.
    """
    path = AccessPath("index_scan", _ABC, (_eq("a"), _cmp("b", ">", 2), _cmp("b", "<", 5)), ())
    result = estimate_row_counts(path, _STATS, _TABLE_STATS)
    assert result.rows_fetched == 10




def test_rows_fetched_is_clamped_to_at_least_one():
    """A prefix average of 0 (or a stats row claiming zero rows for a
    real value) must not make the seek look free.
    """
    stats = IndexStats(row_count=10_000, rows_per_prefix=(0, 0, 0))
    path = AccessPath("index_scan", _ABC, (_eq("a"),), ())
    result = estimate_row_counts(path, stats, _TABLE_STATS)
    assert result.rows_fetched >= 1




# =====================================================================
# est_rows: residual filtering shrinks the estimate, never grows it
# =====================================================================




def test_no_residual_means_est_rows_equals_rows_fetched():
    path = AccessPath("index_scan", _ABC, (_eq("a"), _eq("b")), ())
    result = estimate_row_counts(path, _STATS, _TABLE_STATS)
    assert result.est_rows == result.rows_fetched == 10




def test_residual_predicate_shrinks_est_rows_below_rows_fetched():
    path = AccessPath("index_scan", _ABC, (_eq("a"),), (_eq("c", 3),))
    result = estimate_row_counts(path, _STATS, _TABLE_STATS)
    assert result.rows_fetched == 100
    assert result.est_rows < result.rows_fetched




def test_est_rows_never_exceeds_rows_fetched():
    """Residual filtering only removes rows -- however many residual
    predicates pile up, est_rows must not cross back above rows_fetched.
    """
    residual = (_eq("c", 3), _eq("d", 4), _eq("e", 5))
    path = AccessPath("index_scan", _ABC, (_eq("a"),), residual)
    result = estimate_row_counts(path, _STATS, _TABLE_STATS)
    assert result.est_rows <= result.rows_fetched




def test_est_rows_is_clamped_to_at_least_one():
    residual = tuple(_eq(f"col{i}", i) for i in range(10))
    path = AccessPath("index_scan", _ABC, (_eq("a"),), residual)
    result = estimate_row_counts(path, _STATS, _TABLE_STATS)
    assert result.est_rows >= 1




def test_est_rows_stays_zero_when_rows_fetched_is_zero():
    """A genuinely empty, ANALYZEd table (row_count=0) must not have its
    residual filtering *invent* a row: the floor-of-1 clamp exists to keep
    a nonzero fetch from looking free, not to override a real zero.
    """
    path = AccessPath("seq_scan", None, (), (_eq("c", 3),))
    result = estimate_row_counts(path, None, TableStats(row_count=0))
    assert result.rows_fetched == 0
    assert result.est_rows == 0




# =====================================================================
# every other field survives untouched (AccessPath is frozen -- a new
# instance comes back, not a mutated one)
# =====================================================================




def test_seek_terms_and_residual_are_preserved():
    seek_terms = (_eq("a"),)
    residual = (_eq("c", 3),)
    path = AccessPath("index_scan", _ABC, seek_terms, residual)
    result = estimate_row_counts(path, _STATS, _TABLE_STATS)
    assert result.seek_terms == seek_terms
    assert result.residual == residual
    assert result.index is _ABC
    assert result.kind == "index_scan"