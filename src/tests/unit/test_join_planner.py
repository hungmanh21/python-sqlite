"""Join ordering tests (week7-query-processing.md §43): the equijoin NDV
estimator and index_ndv() at the unit level. enumerate_join_plans/
choose_join_plan proven end to end through quilldb.connect() lands
alongside the executor wiring that makes them runnable.
"""

from quilldb.plan.statistics import IndexStats, estimate_equijoin_rows, index_ndv

# =====================================================================
# index_ndv: NDV(leading column) = row_count / rows_per_prefix[0]
# =====================================================================


def test_index_ndv_divides_row_count_by_rows_per_value() -> None:
    stats = IndexStats(row_count=1000, rows_per_prefix=(10,))
    assert index_ndv(stats) == 100


def test_index_ndv_is_clamped_to_at_least_one() -> None:
    # rows_per_prefix larger than row_count shouldn't be possible from a
    # real ANALYZE, but the clamp is what keeps a defaulted/degenerate
    # stats row from producing an NDV of 0 (division to a value < 1).
    stats = IndexStats(row_count=1, rows_per_prefix=(10,))
    assert index_ndv(stats) == 1


# =====================================================================
# estimate_equijoin_rows: Selinger's `|R| x |S| / max(NDV(R.a), NDV(S.b))`
# =====================================================================


def test_both_ndv_known_uses_selingers_formula() -> None:
    # |R|=1000, |S|=2000, NDV(R.a)=100, NDV(S.b)=50 -> 1000*2000/100 = 20000
    assert estimate_equijoin_rows(1000, 2000, left_ndv=100, right_ndv=50) == 20_000


def test_both_ndv_known_is_symmetric_in_which_side_has_the_larger_ndv() -> None:
    assert estimate_equijoin_rows(1000, 2000, left_ndv=50, right_ndv=100) == 20_000


def test_both_ndv_known_result_is_clamped_to_at_least_one() -> None:
    assert estimate_equijoin_rows(1, 1, left_ndv=1_000_000, right_ndv=1_000_000) == 1


def test_one_ndv_unknown_assumes_the_missing_side_via_rows_per_value() -> None:
    # The unknown side's NDV is ASSUMED as rows/_DEFAULT_ROWS_PER_VALUE(10),
    # not dropped from the max() -- so which side is missing matters,
    # because the two sides have different row counts:
    #   right unknown: assumed NDV(S) = 2000/10 = 200 > NDV(R)=100
    #                  -> 1000*2000/200 = 10_000
    assert estimate_equijoin_rows(1000, 2000, left_ndv=100, right_ndv=None) == 10_000
    #   left unknown:  assumed NDV(R) = 1000/10 = 100 == NDV(S)=100
    #                  -> 1000*2000/100 = 20_000
    assert estimate_equijoin_rows(1000, 2000, left_ndv=None, right_ndv=100) == 20_000


def test_both_ndv_unknown_assumes_both_sides_via_rows_per_value() -> None:
    # Neither side's NDV is known -- both assumed: NDV(R)=1000/10=100,
    # NDV(S)=2000/10=200, same max()/formula as the both-known case.
    assert estimate_equijoin_rows(1000, 2000, left_ndv=None, right_ndv=None) == 10_000


def test_both_ndv_unknown_scales_linearly_not_quadratically_with_table_size() -> None:
    # A flat constant divisor would make this rows^2/10 (quadratic in
    # table size); assuming an NDV that itself scales with rows keeps the
    # estimate proportionate instead -- 10x bigger tables, 10x bigger
    # estimate, not 100x.
    small = estimate_equijoin_rows(100_000, 100_000, left_ndv=None, right_ndv=None)
    large = estimate_equijoin_rows(1_000_000, 1_000_000, left_ndv=None, right_ndv=None)
    assert large == small * 10
