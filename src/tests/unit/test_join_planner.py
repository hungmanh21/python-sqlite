"""Join ordering tests (week7-query-processing.md §43): the equijoin NDV
estimator and index_ndv() at the unit level, then enumerate_join_plans/
choose_join_plan proven end to end through quilldb.connect() -- the
flagship claim is that the SEARCH picks a cheaper join order than the one
the query happened to be written in, and the only way to prove that
honestly is to measure real page reads for both orders, not just trust the
estimates.
"""

from pathlib import Path

import pytest

import quilldb
from quilldb.plan.planner import AccessPath, PlanCost, SortKey
from quilldb.plan.search import PlanCandidate, choose_join_plan, enumerate_join_plans, sort_cost
from quilldb.plan.statistics import IndexStats, estimate_equijoin_rows, index_ndv
from quilldb.sql.ast import DataType
from quilldb.sql.binder import BoundColumn, BoundJoinSelect, bind
from quilldb.sql.parser import parse

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


# =====================================================================
# enumerate_join_plans / choose_join_plan, end to end
# =====================================================================


def _outstanding_pins(connection: quilldb.Connection) -> int:
    return sum(entry.pin_count for entry in connection.pool._cache.values())


@pytest.mark.slow
def test_cost_search_picks_the_cheaper_join_order(tmp_path: Path) -> None:
    """The flagship claim: given a small table and a large table joined on
    an indexed equality, the chosen plan reads far fewer pages than a full
    scan of the large table alone would.

    Proven by MEASURING real page reads (buffer-pool misses) -- and
    measured COLD, from a fresh Connection per measurement, exactly as
    week7-query-processing.md §43's own test-runnability notes require: a
    WARM pool (the same Connection that just inserted every row) would read
    nothing for either query and prove nothing at all.
    """
    path = tmp_path / "db.sqlite"
    small_n = 5
    big_n = 2_000
    # `big.s_id` is UNIQUE per row (0..big_n-1), so `s_id`'s real,
    # ANALYZE'd rows-per-value average is exactly 1 -- no skew for a
    # flat-average selectivity estimate to get wrong. `small` names 5
    # SPECIFIC values out of that range: a full SeqScan of `big` pays for
    # all 2,000 rows to find those 5 matches, while driving from `small`
    # and seeking `big`'s index touches only those 5 rows' own pages plus
    # the seek itself. A wide `padding` column keeps big_n's PAGE count
    # high without needing tens of thousands of (Python-speed) individual
    # INSERTs to get there.
    padding = "x" * 500
    with quilldb.connect(path) as db:
        db.execute("CREATE TABLE small (id INTEGER)")
        db.execute("CREATE TABLE big (id INTEGER, s_id INTEGER, padding TEXT)")
        db.execute("CREATE INDEX ix_small_id ON small (id)")
        db.execute("CREATE INDEX ix_big_s ON big (s_id)")
        for i in range(small_n):
            db.execute("INSERT INTO small VALUES (?)", (i,))
        for i in range(big_n):
            db.execute("INSERT INTO big VALUES (?, ?, ?)", (i, i, padding))
        db.execute("ANALYZE")

    with quilldb.connect(path) as db:
        rows = db.execute(
            "SELECT small.id, big.id FROM small JOIN big ON small.id = big.s_id"
        ).fetchall()
        chosen_misses = db.pool.misses
    assert len(rows) == small_n

    # A full SeqScan of `big` alone is the FLOOR every "big drives" candidate
    # would have to pay at least once, before adding a single per-row probe
    # into `small` on top -- so the chosen plan reading FEWER pages than
    # that floor is direct evidence it drove with `small` (5 rows, a
    # handful of index-seek pages into `big`) instead of seq-scanning all
    # of `big` to drive.
    with quilldb.connect(path) as db:
        db.execute("SELECT * FROM big").fetchall()
        one_big_scan_misses = db.pool.misses

    assert chosen_misses < one_big_scan_misses


def test_reordering_picks_the_cheaper_table_regardless_of_written_order(tmp_path: Path) -> None:
    """The direct proof that this is REORDERING and not just "the query
    happened to already be written cheap-side-first": `big` is written
    FIRST in the FROM clause here, so a search that never reordered would
    hand back a candidate with `big` driving by construction. It doesn't.
    """
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        db.execute("CREATE TABLE small (id INTEGER)")
        db.execute("CREATE TABLE big (id INTEGER, s_id INTEGER)")
        db.execute("CREATE INDEX ix_small_id ON small (id)")
        db.execute("CREATE INDEX ix_big_s ON big (s_id)")
        for i in range(3):
            db.execute("INSERT INTO small VALUES (?)", (i,))
        for i in range(300):
            db.execute("INSERT INTO big VALUES (?, ?)", (i, i % 3))
        db.execute("ANALYZE")

        statement = bind(parse("SELECT * FROM big JOIN small ON big.s_id = small.id"), db.catalog)
        assert isinstance(statement, BoundJoinSelect)
        candidates = enumerate_join_plans(
            list(statement.scopes), list(statement.joins), statement.where, db.catalog, db.stats
        )
        assert len(candidates) == 2  # both orders of a 2-table all-INNER chain
        chosen = choose_join_plan(candidates)

        small_ordinal = next(scope.ordinal for scope in statement.scopes if scope.table.name == "small")
        assert chosen.order[0] == small_ordinal


def test_left_join_never_reorders_even_when_the_left_side_is_bigger(tmp_path: Path) -> None:
    """A LEFT JOIN's own table order is never touched by the search
    (enumerate_join_plans's documented scope cut) -- proven here by a case
    where reordering would obviously be cheaper (a huge preserved side, a
    tiny nullable side) but the result must still be correct, which it can
    only be by keeping the written order.
    """
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        db.execute("CREATE TABLE big (id INTEGER, s_id INTEGER)")
        db.execute("CREATE TABLE small (id INTEGER)")
        db.execute("CREATE INDEX ix_small_id ON small (id)")

        db.execute("INSERT INTO small VALUES (1)")
        for i in range(50):
            db.execute("INSERT INTO big VALUES (?, ?)", (i, i))
        db.execute("ANALYZE")

        rows = db.execute(
            "SELECT big.id FROM big LEFT JOIN small ON big.s_id = small.id"
        ).fetchall()
        # Every `big` row still appears exactly once, matched or NULL-extended --
        # reordering `big`/`small` would be a different (and wrong) query.
        assert len(rows) == 50


def test_join_leaves_no_pins_behind(tmp_path: Path) -> None:
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        db.execute("CREATE TABLE a (id INTEGER)")
        db.execute("CREATE TABLE b (id INTEGER, a_id INTEGER)")
        db.execute("CREATE INDEX ix_a ON a (id)")
        db.execute("CREATE INDEX ix_b ON b (a_id)")
        for i in range(10):
            db.execute("INSERT INTO a VALUES (?)", (i,))
            db.execute("INSERT INTO b VALUES (?, ?)", (i, i))
        db.execute("ANALYZE")

        db.execute("SELECT * FROM a JOIN b ON a.id = b.a_id").fetchall()
        assert _outstanding_pins(db) == 0


# =====================================================================
# sort_cost / total_cost_with_ordering -- the "interesting orders"
# comparison (week7-query-processing.md §43, session 7's TODO(human))
# =====================================================================


def _path_with_order(output_order: tuple[SortKey, ...], est_rows: int) -> AccessPath:
    return AccessPath(
        "index_scan", None, (), (), est_rows=est_rows, cost=PlanCost(startup=0.0, total=1.0), output_order=output_order
    )


def test_sort_cost_is_zero_when_output_order_already_satisfies_order_by() -> None:
    key = SortKey(BoundColumn(0, "id", DataType.INTEGER), descending=False)
    candidate = _path_with_order((key,), est_rows=1000)
    assert sort_cost(candidate, (key,)) == 0.0


def test_sort_cost_is_zero_when_output_order_is_a_longer_prefix_match() -> None:
    """An index on (a, b) already satisfies `ORDER BY a` regardless of what
    `b` does -- only the positions `order_by` actually names are compared.
    """
    a = SortKey(BoundColumn(0, "a", DataType.INTEGER), descending=False)
    b = SortKey(BoundColumn(1, "b", DataType.INTEGER), descending=False)
    candidate = _path_with_order((a, b), est_rows=1000)
    assert sort_cost(candidate, (a,)) == 0.0


def test_sort_cost_is_positive_when_output_order_is_empty() -> None:
    key = SortKey(BoundColumn(0, "id", DataType.INTEGER), descending=False)
    candidate = _path_with_order((), est_rows=1000)
    assert sort_cost(candidate, (key,)) > 0.0


def test_sort_cost_is_positive_when_direction_disagrees() -> None:
    """Same column, wrong direction: a forward index scan doesn't satisfy
    `ORDER BY id DESC`, so this still needs a real Sort.
    """
    forward = SortKey(BoundColumn(0, "id", DataType.INTEGER), descending=False)
    backward = SortKey(BoundColumn(0, "id", DataType.INTEGER), descending=True)
    candidate = _path_with_order((forward,), est_rows=1000)
    assert sort_cost(candidate, (backward,)) > 0.0


def test_sort_cost_grows_with_est_rows() -> None:
    key = SortKey(BoundColumn(0, "id", DataType.INTEGER), descending=False)
    small = _path_with_order((), est_rows=10)
    large = _path_with_order((), est_rows=100_000)
    assert sort_cost(small, (key,)) < sort_cost(large, (key,))


def _candidate(*, cost_total: float, output_order: tuple[SortKey, ...], est_rows: int) -> PlanCandidate:
    path = _path_with_order((), est_rows=est_rows)
    return PlanCandidate(
        order=(0, 1),
        access_paths=(path, path),
        join_types=("inner", "inner"),
        match_expressions=(None, None),
        residual=None,
        est_rows=est_rows,
        cost=PlanCost(startup=0.0, total=cost_total),
        output_order=output_order,
    )


def test_sort_cost_is_inside_the_join_comparison() -> None:
    """The roadmap's own "interesting orders" regression (week7-query-
    processing.md §43): a candidate that's cheaper by `cost.total` alone
    but needs a Sort on a large result set must lose to a slightly pricier
    candidate whose driving table's own order already satisfies the
    ORDER BY -- exactly the trap this module's own docstring names.
    """
    key = SortKey(BoundColumn(0, "id", DataType.INTEGER), descending=False)
    order_by = (key,)

    cheaper_but_unsorted = _candidate(cost_total=100.0, output_order=(), est_rows=5_000)
    pricier_but_presorted = _candidate(cost_total=110.0, output_order=(key,), est_rows=5_000)

    # Without an ORDER BY, raw cost.total wins -- confirms the "cheaper"
    # label above is actually true, not begging the question.
    assert choose_join_plan([cheaper_but_unsorted, pricier_but_presorted]) is cheaper_but_unsorted

    # With the ORDER BY, the Sort `cheaper_but_unsorted` would need over
    # 5,000 rows costs far more than the 10.0 cost.total gap -- the
    # pre-sorted candidate wins instead.
    assert choose_join_plan([cheaper_but_unsorted, pricier_but_presorted], order_by) is pricier_but_presorted
