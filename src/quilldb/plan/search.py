"""Stage 4 of the cost-based pipeline (chapter 12 §12.6): picking the
winning AccessPath out of everything stage 1-3 produced -- and, since week
7 (§43 "Join ordering"), the multi-table extension of the same idea:
picking the winning left-deep JOIN order out of every legal one.


`choose_access_path` picks the minimum-cost path for ONE table.
`enumerate_join_plans`/`choose_join_plan` below do the same job one level
up: enumerate every legal order the tables could be joined in, cost each
one with the same stage 1-3 pipeline applied per table, and pick the
cheapest.


Session 7 ("interesting orders") folds ORDER BY into both rankings: an
optional `order_by` parameter on `choose_access_path`/`choose_join_plan`,
defaulting to `()` -- which keeps every pre-session-7 call site ranking on
`cost.total` alone, byte-for-byte the old behavior, matching every other
`outer: Row = ()`-shaped extension already in this codebase. See
`total_cost_with_ordering`/`sort_cost` below for the actual comparison.
"""


import itertools
import math
from dataclasses import dataclass
from typing import Literal, Protocol

from quilldb.catalog.catalog import Catalog
from quilldb.catalog.schema import IndexSchema, TableSchema
from quilldb.plan.cost import CPU_PER_ROW, assign_cost
from quilldb.plan.planner import AccessPath, PlanCost, SortKey, enumerate_access_paths
from quilldb.plan.predicates import (
    Predicate,
    classify_predicate,
    extract_conjuncts,
    referenced_tables,
)
from quilldb.plan.statistics import (
    IndexStats,
    TableStats,
    estimate_equijoin_rows,
    estimate_row_counts,
    index_ndv,
)
from quilldb.sql.binder import BoundBinaryOp, BoundColumn, BoundExpression, BoundJoin, TableScope


class StatsSource(Protocol):
    """The only two StatisticsCatalog methods join planning needs.

    A Protocol rather than importing StatisticsCatalog itself (plan/analyze.py)
    -- so build_operator() can pass a lightweight stand-in when no real
    StatisticsCatalog is available, exactly like SchemaSource lets
    sql/binder.py bind without a live catalog (see that Protocol's own
    docstring for the same reasoning).
    """

    def table_stats(self, name: str) -> TableStats: ...
    def index_stats(self, index: IndexSchema) -> IndexStats: ...


def choose_access_path(candidates: list[AccessPath], order_by: tuple[SortKey, ...] = ()) -> AccessPath:
    """Pick the minimum-cost.total candidate -- or, when `order_by` is given
    and some candidate might actually satisfy it, the minimum
    total-cost-including-any-Sort-on-top candidate (session 7).


    Call this AFTER every candidate has been through estimate_row_counts()
    and assign_cost() -- it reads `path.cost.total`, which stage 3 fills
    in and stages 1-2 leave at `None`.


    Args:
        candidates: every legal AccessPath for one table, as produced by
            enumerate_access_paths() and then costed by assign_cost().
            Never empty in practice -- enumerate_access_paths() always
            includes a seq_scan (chapter 12 §12.6 trap #2) -- but don't
            assume that here; an empty list is a caller bug, not a case
            to silently paper over.
        order_by: this query's ORDER BY, as SortKeys, or `()` for none.
            Deliberately checked against every candidate's `output_order`
            BEFORE ever calling total_cost_with_ordering/sort_cost: when
            nothing here has a nonempty `output_order` at all (the common
            case -- no index leads with an unconstrained walk over this
            table), no candidate could possibly avoid a Sort, so ranking
            on `cost.total` alone is already the right answer and there's
            nothing to gain -- or risk -- by invoking sort_cost.


    Returns:
        The candidate with the lowest (ordering-aware, if applicable)
        cost. Ties are legal (a seq_scan and a full-key index seek can
        cost the same on a tiny table) and any tiebreak is fine -- the
        two candidates are interchangeable by definition once their costs
        are equal.


    This is deliberately the simplest possible reduction over `cost.total`
    -- chapter 12 §12.6 is explicit that the *hard* part of stage 4 was
    already done by stage 3 assigning honest costs. A boolean-style index
    matching half the table should lose to a seq_scan here not because of
    a special case in this function, but because assign_cost() already
    priced it worse (chapter 12 §12.6 trap #2). If this function needs an
    if/elif per AccessPath.kind to get the right answer, that's a sign a
    cost is wrong upstream, not that this function needs to get smarter.
    """
    if order_by and any(path.output_order for path in candidates):
        return min(candidates, key=lambda path: total_cost_with_ordering(path, order_by))
    return min(candidates, key=_cost_total)




def _cost_total(path: AccessPath) -> float:
    assert path.cost is not None, "choose_access_path requires every candidate to be costed by assign_cost() first"
    return path.cost.total


@dataclass(frozen=True)
class PlanCandidate:
    """One executable left-deep join order: a table_ordinal per position,
    the AccessPath chosen for each (already run through estimate_row_counts
    and assign_cost -- see _cheapest_path), the join type joining that
    position onto everything before it, and whatever match condition beyond
    that AccessPath's own seek isn't already guaranteed.


    `order[0]`/`join_types[0]`/`match_expressions[0]` describe the driving
    table: there's nothing to join it TO, so `join_types[0]` is the inert
    value `"inner"` and `match_expressions[0]` is always None. Every
    BoundColumn inside `match_expressions`/`access_paths[i].residual` is
    still in (table_ordinal, index) form -- exec/operators.py's
    _build_join_operator resolves it to this candidate's own flat layout
    with sql/binder.py's resolve_layout, exactly the pass that function
    exists for.


    `output_order` (session 7) is exactly `access_paths[0].output_order`:
    "the output order comes from the OUTERMOST table's access path" in a
    left-deep nested loop (§43) -- position 0 is the only table whose rows
    reach the final output un-interleaved with anything else, so it's the
    only one whose own sort order survives the join. A later position's
    output_order, however good, says nothing about the JOINED stream's
    order and is deliberately not consulted here.
    """


    order: tuple[int, ...]
    access_paths: tuple[AccessPath, ...]
    join_types: tuple[Literal["inner", "left"], ...]  # aligned with `order`
    match_expressions: tuple[BoundExpression | None, ...]  # aligned with `order`; [0] is None
    residual: BoundExpression | None  # evaluated on the FINAL combined row, after every join
    est_rows: int
    cost: PlanCost
    output_order: tuple[SortKey, ...] = ()


Costed = AccessPath | PlanCandidate
"""The two stage-4 candidate shapes sort_cost/total_cost_with_ordering
compare -- a plain Union rather than a Protocol: both already declare
`output_order`/`est_rows` with identical types, and a Protocol's
structural match rejects a frozen dataclass's read-only attributes against
a plain (settable-by-default) Protocol member, for no benefit here since
there are exactly two concrete implementers, not an open set (contrast
StatsSource, which really does stand in for either a live StatisticsCatalog
or build_operator's `stats=None` fallback).
"""


def total_cost_with_ordering(candidate: Costed, order_by: tuple[SortKey, ...]) -> float:
    """THE comparison key whenever there's an ORDER BY (week7-query-processing.md
    §43): never rank candidates on `candidate.cost.total` alone once a Sort
    might be needed on top of one of them but not another -- see this
    module's own docstring and sort_cost's for why ranking on cost.total
    alone is the "interesting orders" trap.
    """
    assert candidate.cost is not None, "total_cost_with_ordering requires every candidate to be costed first"
    return candidate.cost.total + sort_cost(candidate, order_by)


def sort_cost(candidate: Costed, order_by: tuple[SortKey, ...]) -> float:
    """0.0 if `candidate.output_order` already satisfies `order_by`, else
    the cost of the Sort this candidate would need on top
    (week7-query-processing.md §43).


    `order_by` is this query's ORDER BY, one SortKey per key, in the
    query's own written order. `candidate.output_order` is what
    `candidate`'s own access path already guarantees, forward from
    position 0 -- empty when nothing about this candidate is known to be
    sorted at all (plan/planner.py's AccessPath docstring explains exactly
    which shapes populate it and which don't).


    "Satisfied" means `order_by` is a prefix of `candidate.output_order`,
    compared key by key with plain `==` (SortKey is a frozen dataclass, so
    `SortKey(expr, descending) == SortKey(expr, descending)` is exactly
    the check) -- both the expression AND the direction must match at
    every position `order_by` cares about; positions past `len(order_by)`
    in `output_order` don't matter (an index on (a, b) with `ORDER BY a`
    is already satisfied, whatever `b` does). If `order_by` is longer than
    `candidate.output_order`, or any key disagrees, it's not satisfied.


    When not satisfied, return a cost > 0.0 for the Sort this candidate
    would need -- in the same page-read-equivalent units plan/cost.py's
    other costs use (SEQ_PAGE_COST/RANDOM_PAGE_COST/CPU_PER_ROW), so it's
    comparable to `candidate.cost.total`. `candidate.est_rows` is what
    exec/sort.py's own Sort would actually have to sort; the roadmap has
    no prescribed formula for what that costs (an in-memory sort has no
    page reads to count), so pick something that scales with `est_rows`
    and document the reasoning -- the two things that must be true no
    matter which formula you pick: it's 0.0 exactly when satisfied (never
    a small positive number that rounding could still lose to a genuinely
    cheaper unsorted candidate), and it's large enough, for a real Sort
    over a nontrivial number of rows, to flip a ranking that cost.total
    alone would get wrong -- test_sort_cost_is_inside_the_join_comparison
    (test_join_planner.py, once you write it) is what actually proves that.
    """
    if candidate.output_order[: len(order_by)] == order_by:
        return 0.0

    # An in-memory comparison sort is ~n*log2(n) comparisons; price each one
    # at CPU_PER_ROW, the same per-row CPU unit assign_cost() already uses,
    # so this stays comparable to cost.total. Clamped to at least 2 rows so
    # log2 never collapses to 0 (which would silently reintroduce the "small
    # positive number that rounding could still lose" bug this function's
    # own docstring warns against) -- a real Sort is never actually free just
    # because a row estimate happens to be 0 or 1.
    rows = max(2, candidate.est_rows)
    return CPU_PER_ROW * rows * math.log2(rows)


def enumerate_join_plans(
    scopes: list[TableScope],
    joins: list[BoundJoin],
    where: BoundExpression | None,
    catalog: Catalog,
    stats: StatsSource,
) -> list[PlanCandidate]:
    """Enumerate every legal left-deep join order (week7-query-processing.md
    §43, "Join ordering").


    Reordering is scoped to a chain of pure INNER joins: with a LEFT JOIN
    anywhere, this returns exactly the one candidate in the FROM clause's
    own order, and every join's `on` is used exactly as written -- pushing
    a WHERE conjunct across a LEFT JOIN's nullable side is §17.8's trap in
    reverse (§43's pushdown table), and the safe way not to hit it here is
    to never attempt that pushdown at all rather than get the preserved-
    vs-nullable-side bookkeeping subtly wrong. A query that mixes an INNER
    and a LEFT JOIN plans in written order: always legal, just not always
    cheapest. Reordering across LEFT JOIN edges when it's provably safe is
    real join-order search's next refinement, not attempted here.


    For a pure INNER chain, WHERE and every join's ON are interchangeable
    (both must hold for a row to survive), so they're pooled into one set
    of conjuncts and redistributed per position by cost -- an equijoin
    conjunct becomes a seek candidate for whichever side of it ends up
    placed later in a given order, exactly like a WHERE predicate becomes a
    seek candidate for a single table today.


    Returns EVERY legal order, not just the cheapest-looking one -- chapter
    12 §12.7's "keep the cheapest per subset" pruning is what loses
    interesting orders (§43), and at the <=3 tables this grammar produces
    (comma joins and JOIN chains both flatten into one FROM list), N! is at
    most 6: nothing is gained by pruning it.
    """
    all_inner = all(join.join_type == "INNER" for join in joins)
    orders = (
        list(itertools.permutations(range(len(scopes))))
        if all_inner
        else [tuple(range(len(scopes)))]
    )

    conjuncts: list[BoundExpression] = list(extract_conjuncts(where))
    on_conjuncts: set[int] = set()  # id() of every conjunct sourced from a join's own ON
    for join in joins:
        if join.on is not None:
            for conjunct in extract_conjuncts(join.on):
                conjuncts.append(conjunct)
                on_conjuncts.add(id(conjunct))

    # In the has-a-LEFT-JOIN branch, every ON conjunct is applied verbatim as
    # its OWN join step's match expression (joins[position-1].on, used as-is
    # in _build_candidate) -- never redistributed by cost, and never allowed
    # to fall through to the final top-level residual Filter. Pre-marking
    # them "used" is what keeps them out of that residual: applied there
    # instead, a LEFT JOIN's ON condition would run AFTER NULL-extension and
    # reject every NULL-extended row, exactly the §17.8/§43 trap this
    # function's docstring says not to risk.
    preconsumed = on_conjuncts if not all_inner else set()

    return [
        _build_candidate(order, scopes, joins, conjuncts, catalog, stats, all_inner, preconsumed)
        for order in orders
    ]


def choose_join_plan(candidates: list[PlanCandidate], order_by: tuple[SortKey, ...] = ()) -> PlanCandidate:
    """Pick the minimum total-cost join order -- or, when `order_by` is
    given and some candidate's driving table might actually satisfy it,
    the minimum cost-including-any-Sort-on-top (session 7, "interesting
    orders"). Same `order_by`/guard reasoning as choose_access_path's own
    docstring -- `order_by=()` (every pre-session-7 caller) ranks on
    `cost.total` alone, unchanged.
    """
    if order_by and any(candidate.output_order for candidate in candidates):
        return min(candidates, key=lambda candidate: total_cost_with_ordering(candidate, order_by))
    return min(candidates, key=lambda candidate: candidate.cost.total)


def _build_candidate(
    order: tuple[int, ...],
    scopes: list[TableScope],
    joins: list[BoundJoin],
    conjuncts: list[BoundExpression],
    catalog: Catalog,
    stats: StatsSource,
    all_inner: bool,
    preconsumed: set[int],
) -> PlanCandidate:
    used: set[int] = set(preconsumed)  # id() of conjuncts already spoken for
    access_paths: list[AccessPath] = []
    join_types: list[Literal["inner", "left"]] = ["inner"]
    match_expressions: list[BoundExpression | None] = [None]
    available: set[int] = set()

    # A WHERE conjunct naming only a LEFT JOIN's nullable (inner) side must
    # never become that table's own access-path filter: pushed there, it
    # runs BEFORE NULL-extension and so never gets a chance to reject the
    # NULL row it's supposed to (week7-query-processing.md §43's pushdown
    # table, the "stays above the join" cell -- §17.8's trap in reverse).
    # Only meaningful when `not all_inner`, since reordering never happens
    # with a LEFT JOIN in the chain.
    nullable_tables = {
        order[position] for position in range(1, len(order)) if joins[position - 1].join_type == "LEFT"
    }

    total_cost = 0.0
    startup = 0.0
    est_rows = 1

    for position, table_ordinal in enumerate(order):
        table = scopes[table_ordinal].table
        table_stats = stats.table_stats(table.name)

        step_predicates: list[Predicate]
        newly_used: set[int]
        if table_ordinal in nullable_tables:
            step_predicates, newly_used = [], set()
        else:
            step_predicates, newly_used = _predicates_for_step(
                conjuncts, table_ordinal, available, used, allow_cross_table=all_inner
            )
        used |= newly_used
        path = _cheapest_path(table, catalog, step_predicates, stats, table_stats, table_ordinal)
        assert path.cost is not None, "_cheapest_path returns paths already costed by assign_cost"
        access_paths.append(path)

        cross_table_residual = [
            predicate.source
            for predicate in path.residual
            if referenced_tables(predicate.source) != {table_ordinal}
        ]

        if position == 0:
            startup = path.cost.startup
            total_cost = path.cost.total
            est_rows = path.est_rows
        else:
            step_join_type: Literal["inner", "left"]
            if all_inner:
                step_match = _and_all(cross_table_residual)
                step_join_type = "inner"
            else:
                on = joins[position - 1].on
                step_match = _and_all(([on] if on is not None else []) + cross_table_residual)
                step_join_type = "left" if joins[position - 1].join_type == "LEFT" else "inner"
            join_types.append(step_join_type)
            match_expressions.append(step_match)

            total_cost += est_rows * path.cost.total
            est_rows = _joined_row_estimate(
                est_rows, table, table_stats.row_count, step_predicates, scopes, catalog, stats
            )

        available.add(table_ordinal)

    residual = _and_all([conjunct for conjunct in conjuncts if id(conjunct) not in used])

    return PlanCandidate(
        order=order,
        access_paths=tuple(access_paths),
        join_types=tuple(join_types),
        match_expressions=tuple(match_expressions),
        residual=residual,
        est_rows=est_rows,
        cost=PlanCost(startup=startup, total=total_cost),
        output_order=access_paths[0].output_order,  # the driving table's, and only the driving table's
    )


def _predicates_for_step(
    conjuncts: list[BoundExpression],
    table_ordinal: int,
    available: set[int],
    used: set[int],
    allow_cross_table: bool,
) -> tuple[list[Predicate], set[int]]:
    """Conjuncts usable once `table_ordinal` is being placed: mention this
    table, mention nothing placed LATER than it, and haven't already been
    consumed by an earlier step.


    `allow_cross_table=False` additionally restricts to conjuncts that
    mention ONLY this table -- the has-a-LEFT-JOIN branch's safety rule
    (see enumerate_join_plans): no WHERE/ON conjunct crosses a table
    boundary through this path, so nothing here can accidentally become a
    LEFT JOIN's ON when it should have stayed a plain WHERE, or vice versa.
    """
    predicates: list[Predicate] = []
    newly_used: set[int] = set()
    allowed = available | {table_ordinal}
    for conjunct in conjuncts:
        if id(conjunct) in used:
            continue
        tables = referenced_tables(conjunct)
        if table_ordinal not in tables:
            continue
        if not allow_cross_table and tables != {table_ordinal}:
            continue
        if not tables <= allowed:
            continue
        predicate = classify_predicate(conjunct, table_ordinal)
        if predicate is not None:
            predicates.append(predicate)
            newly_used.add(id(conjunct))
    return predicates, newly_used


def _cheapest_path(
    table: TableSchema,
    catalog: Catalog,
    predicates: list[Predicate],
    stats: StatsSource,
    table_stats: TableStats,
    table_ordinal: int,
) -> AccessPath:
    """The single-table stage 1-4 pipeline (planner.py/statistics.py/cost.py/
    this module's own choose_access_path), applied to one join step exactly
    as build_operator applies it to a bare single-table SELECT. A `predicates`
    entry whose `.value` references an earlier-joined table costs and estimates
    no differently than a literal-valued one -- assign_cost/estimate_row_counts
    never look at what `.value` evaluates to, only at which column and operator
    it pairs with -- which is what makes a chosen index_scan here into a
    correlated/parameterized seek for free once exec/join.py's NestedLoopJoin
    re-opens it per outer row.


    `table_ordinal` is only for tagging each candidate's own `output_order`
    (planner.py's SortKey) with the right table-scope, so a driving-table
    (position 0) candidate's advertised order can be `==`-compared against
    an ORDER BY expression bound the same way (_join_expression). Ranking
    itself stays plain `choose_access_path(costed)`, order-UNaware, at
    every position: only PlanCandidate.output_order (position 0's own path)
    ever feeds sort_cost, so there is nothing for a later position to gain
    -- and something to get subtly wrong -- by ranking here as if its own
    order mattered to the join as a whole (see PlanCandidate's docstring).
    """
    indexes = list(catalog.indexes_for(table.name))
    candidates = enumerate_access_paths(table, indexes, predicates, table_ordinal)
    costed = []
    for candidate in candidates:
        index_stats = stats.index_stats(candidate.index) if candidate.index is not None else None
        candidate = estimate_row_counts(candidate, index_stats, table_stats)
        candidate = assign_cost(candidate, index_stats, table_stats)
        costed.append(candidate)
    return choose_access_path(costed)


def _joined_row_estimate(
    outer_rows: int,
    inner_table: TableSchema,
    inner_row_count: int,
    step_predicates: list[Predicate],
    scopes: list[TableScope],
    catalog: Catalog,
    stats: StatsSource,
) -> int:
    """The join-so-far's new estimated row count after adding `inner_table`.


    An equijoin predicate among `step_predicates` (`.operator == "="`, and
    `.value` is itself a BoundColumn -- an actual column-to-column join key,
    not a literal) uses Selinger's selectivity via estimate_equijoin_rows().
    Anything else -- a cross join, or a step whose only predicates are
    non-equality or literal-valued -- has no join key relating it to what's
    already joined, so the two sides are treated as independent: the
    estimate is their plain product, same as a real cross join's actual
    cardinality.
    """
    equijoin_value: BoundColumn | None = None
    equijoin_column = ""
    for predicate in step_predicates:
        if predicate.operator == "=" and isinstance(predicate.value, BoundColumn):
            equijoin_value = predicate.value
            equijoin_column = predicate.column
            break
    if equijoin_value is None:
        return max(1, outer_rows * inner_row_count)

    inner_ndv = _leading_ndv(inner_table, equijoin_column, catalog, stats)
    outer_table = scopes[equijoin_value.table_ordinal].table
    outer_ndv = _leading_ndv(outer_table, equijoin_value.name, catalog, stats)
    return estimate_equijoin_rows(outer_rows, inner_row_count, outer_ndv, inner_ndv)


def _leading_ndv(table: TableSchema, column: str, catalog: Catalog, stats: StatsSource) -> int | None:
    """This table's NDV for `column`, or None if `column` doesn't LEAD any
    index on `table` -- the "unknown" case estimate_equijoin_rows's own
    fallback handles. A defaulted (never-ANALYZEd) index still counts as
    "known": index_ndv() derives a number from it either way, real stats
    or the documented default (week7-query-processing.md §43's own note
    on what "known" means here).
    """
    folded = column.casefold()
    for index in catalog.indexes_for(table.name):
        if index.columns[0].casefold() == folded:
            return index_ndv(stats.index_stats(index))
    return None


def _and_all(expressions: list[BoundExpression]) -> BoundExpression | None:
    if not expressions:
        return None
    result = expressions[0]
    for expression in expressions[1:]:
        result = BoundBinaryOp(result, "AND", expression)
    return result