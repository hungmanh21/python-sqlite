"""Stage 1 of the cost-based pipeline (chapter 12 §12.6): which access paths
are LEGAL for one table, given its sargable predicates and its indexes.


Legality only. SeqScan is always legal, and every index either offers a
seek or it doesn't -- no cost estimate is needed to decide that. Stage 2
(row estimates from quill_stat1) and stage 3 (page-oriented costs) haven't
been built yet, so AccessPath below carries only what stage 1 can
determine on its own, and choose_access_path() doesn't exist yet either:
with nothing to compare costs on, "cheapest" isn't a decidable question
until the next session.
"""


from dataclasses import dataclass
from typing import Literal

from quilldb.catalog.schema import IndexSchema, TableSchema
from quilldb.plan.predicates import Predicate
from quilldb.sql.binder import BoundColumn, BoundExpression


@dataclass(frozen=True)
class SortKey:
    """One ORDER BY-shaped requirement or guarantee: an expression plus a
    direction (week7-query-processing.md session 7, "interesting orders").

    Doubles as both sides of the comparison plan/search.py's sort_cost has
    to make:
      - what a query's ORDER BY actually asks for (built from the bound
        SELECT's own order_by, one SortKey per key, in the query's own
        write order), and
      - what an AccessPath/PlanCandidate's own traversal already guarantees
        (AccessPath.output_order below), one entry per index column past
        any equality prefix, ALL sharing that path's single `reverse` flag
        -- a B+tree walks forward or backward as a whole, never some
        columns one way and others the other (chapter 18 SS18.3).

    `expression` is unresolved table-scope (BoundColumn.table_ordinal),
    exactly the form _join_expression/_expression already produce for a
    bare column reference -- so comparing a requested key against a
    guaranteed one is plain `==`, no translation needed either direction.
    """

    expression: BoundExpression
    descending: bool


@dataclass(frozen=True)
class PlanCost:
    """A path's page-read-equivalent cost (chapter 12 §12.6 stage 3).


    `startup`: cost paid before the first row comes out (an IndexScan's
    root-to-leaf descent; 0 for a SeqScan, which starts producing rows
    from the first page it reads).
    `total`: startup plus every further page read and per-row CPU cost
    to exhaust the whole path. `total` is what choose_access_path()
    (not yet built) ranks candidates on; `startup` only matters once a
    LIMIT or an ORDER BY makes "first row fast" worth something on its
    own.
    """


    startup: float
    total: float



@dataclass(frozen=True)
class AccessPath:
    """One legal way to read `table`, before any cost is attached.


    `seek_terms` is the ordered prefix of predicates the index will
    actually use to position a seek -- in index-column order, which is not
    necessarily the order they appeared in the WHERE clause. `residual` is
    every predicate this path can't consume: it still has to become a
    Filter above the scan, or rows come out wrong (chapter 12 §12.6 trap
    #1 -- dropping a term silently instead of re-checking it).


    `rows_fetched` and `est_rows` are 0 until stage 2
    (plan/statistics.py's estimate_row_counts) fills them in -- stage 1
    only decides legality, so it has nothing to estimate from yet.
    `cost` is None until stage 3 (plan/cost.py's assign_cost) fills it
    in, for the same reason.

    `output_order`/`reverse` (session 7, "interesting orders") are the
    physical-ordering half of this path, set only by `_index_order_path`
    below -- a full, unconstrained walk of an index is the only shape
    stage 1 currently knows is genuinely still sorted end to end. An
    equality-consumed prefix ALSO leaves its trailing columns sorted in
    principle, but `_match_index_prefix` doesn't advertise that yet
    (documented gap, same spirit as exec/sort.py's missing top-K heap):
    every seeking AccessPath here has `output_order == ()`, so it never
    wins a sort-avoidance comparison even where a real database could.
    `reverse` tells IndexScan.open() which of IndexBTree's two full-walk
    methods to call (`scan()`/`seek_eq([])` vs `scan_reverse()`) -- it is
    meaningless (and always False) whenever `output_order` is empty.
    """


    kind: Literal["seq_scan", "index_scan"]
    index: IndexSchema | None
    seek_terms: tuple[Predicate, ...]
    residual: tuple[Predicate, ...]
    rows_fetched: int = 0
    est_rows: int = 0
    cost: PlanCost | None = None
    output_order: tuple[SortKey, ...] = ()
    reverse: bool = False




def enumerate_access_paths(
    table: TableSchema,
    indexes: list[IndexSchema],
    predicates: list[Predicate],
    table_ordinal: int = 0,
) -> list[AccessPath]:
    """Every legal way to answer a WHERE clause against `table`.


    SeqScan is unconditional -- chapter 12 §12.6 trap #2: it must remain a
    candidate even when an index applies, because a low-selectivity index
    can still lose on cost once stage 3 exists to say so. Every index in
    `indexes` is then checked against the leading-column rule (§12.3); an
    index that can't form a seek at all is simply omitted, not added with
    an empty seek.


    `table_ordinal` (default 0, session 7) is only ever non-zero for a
    join's own per-table planning (plan/search.py's `_cheapest_path`): it's
    threaded into every SortKey this table's candidates advertise, so an
    ORDER BY expression bound against that same table-scope (`_join_expression`'s
    output) can be compared to it with plain `==` -- no separate
    translation step, matching BoundColumn.table_ordinal's own reasoning.
    """
    paths = [AccessPath("seq_scan", None, (), tuple(predicates))]


    by_column: dict[str, list[Predicate]] = {}
    for predicate in predicates:
        by_column.setdefault(predicate.column, []).append(predicate)


    for index in indexes:
        path = _match_index_prefix(index, by_column, predicates)
        if path is not None:
            paths.append(path)
        else:
            # No predicate touches this index's leading column, so
            # _match_index_prefix has nothing to seek with -- but the
            # index's own sort order is still a legal (if usually
            # expensive) way to answer the query, and it's the ONLY shape
            # that can satisfy an ORDER BY without a Sort on top
            # (week7-query-processing.md session 0.2/session 7). Offer
            # both traversal directions -- a B+tree walks backwards for
            # free (chapter 18 SS18.3) -- so cost comparison, not omission,
            # is what rules either out when there's no ORDER BY/LIMIT to
            # make it worthwhile.
            paths.append(_index_order_path(index, predicates, table, table_ordinal, reverse=False))
            paths.append(_index_order_path(index, predicates, table, table_ordinal, reverse=True))


    return paths




_EQUALITY_OPERATORS = ("=", "IS")




def _match_index_prefix(
    index: IndexSchema,
    by_column: dict[str, list[Predicate]],
    predicates: list[Predicate],
) -> AccessPath | None:
    """Try to seek `index` using the leading-column rule (chapter 12 §12.3).


    Walks `index.columns` left to right:


      - A column with one or more `=`/`IS` predicates is fully consumed and
        the walk continues -- equality collapses that column to a single
        point, so the next column is still sorted within it.
      - A column with only inequalities (`<`/`<=`/`>`/`>=`) consumes up to
        two of them (a "sandwich" between two bounds), then the walk stops
        -- a range leaves many sub-blocks, one per distinct value, so no
        later column has a single contiguous region to seek.
      - A column with no predicate at all stops the walk without consuming
        anything. This is the gap rule: `WHERE a=1 AND c=3` against
        `(a,b,c)` seeks on `a` alone, because `b` has nothing on it.


    Args:
        index: the index being tested, e.g. columns=("a", "b", "c") for
            `CREATE INDEX abc ON t(a,b,c)`, checked in physical sort order.
        by_column: this query's sargable predicates, grouped by column
            name. A column absent from this dict has no predicate at all.
        predicates: the same predicates as `by_column`, flattened, so
            `residual` can be computed as "everything not consumed."


    Returns:
        None if the index's first column has no predicate (no seek at
        all, so this index must not become a candidate). Otherwise an
        AccessPath with the consumed predicates as `seek_terms`
        (index-column order) and everything else as `residual` --
        residual predicates still have to run as a Filter, or rows come
        out wrong (chapter 12 §12.6 trap #1).
    """
    seek_terms: list[Predicate] = []


    for column in index.columns:
        column_predicates = by_column.get(column, [])
        if not column_predicates:
            break  # gap: nothing constrains this column, seek can't reach further


        equalities = [p for p in column_predicates if p.operator in _EQUALITY_OPERATORS]
        inequalities = [p for p in column_predicates if p.operator not in _EQUALITY_OPERATORS]


        seek_terms.extend(equalities)
        if not inequalities:
            continue  # equality pins this column to a point; the next column still helps


        seek_terms.extend(inequalities[:2])  # sandwich: at most two bounding inequalities
        break  # nothing right of an inequality column can contribute to the seek


    if not seek_terms:
        return None


    consumed_ids = {id(p) for p in seek_terms}
    residual = tuple(p for p in predicates if id(p) not in consumed_ids)


    return AccessPath(
        kind="index_scan",
        index=index,
        seek_terms=tuple(seek_terms),
        residual=residual,
    )




def _index_order_path(
    index: IndexSchema,
    predicates: list[Predicate],
    table: TableSchema,
    table_ordinal: int,
    *,
    reverse: bool,
) -> AccessPath:
    """A full walk of `index` start to end, in its own sort order (forward)
    or the reverse of it (backward) -- no seek at all
    (week7-query-processing.md session 0.2/session 7).


    Shares the "index_scan" kind with a seeking path (empty `seek_terms`
    is what tells them apart), which is deliberate: IndexScan.open()
    already treats an empty `seek_terms` as "every entry, in `reverse`'s
    direction" (a forward walk was the only direction before session 7;
    `reverse` picks between IndexBTree's `scan()`/`seek_eq([])` and
    `scan_reverse()`), so the executor needs no new AccessPath.kind at all
    -- only `reverse`. Every predicate stays in `residual`, since a path
    with nothing to seek on filters nothing on its own.


    `output_order` is every one of `index.columns`, in index order, each
    tagged `reverse` -- this is the ONE shape stage 1 currently knows is
    still sorted end to end (see AccessPath's own docstring for the gap
    this leaves on a seeking path). BoundColumn's `index`/`data_type` come
    from `table.column_index`/`table.columns`, not from the Predicate
    machinery -- there may be no predicate on these columns at all (that's
    the whole point: nothing constrained this index, so nothing but its
    own order makes it worth considering).
    """
    output_order = tuple(
        SortKey(BoundColumn(table.column_index(name), name, table.columns[table.column_index(name)].data_type, table_ordinal), reverse)
        for name in index.columns
    )
    return AccessPath(
        kind="index_scan",
        index=index,
        seek_terms=(),
        residual=tuple(predicates),
        output_order=output_order,
        reverse=reverse,
    )


@dataclass(frozen=True)
class PlanShape:
    """The reusable GEOMETRY of a winning AccessPath -- which index (or
    none, for seq_scan) and which direction -- with no bound VALUE
    anywhere in it (plan/cache.py, week7-query-processing.md §43's plan
    cache).


    Deliberately NOT the AccessPath itself: an AccessPath's `seek_terms`/
    `residual` are Predicate objects whose `.value` is THIS bind's own
    BoundExpression tree -- for `WHERE id = ?`, literally a BoundLiteral
    holding this call's parameter value. Caching and reusing THAT for a
    later call with a different `?` would answer every later call with the
    first call's value -- exactly the trap the plan cache's own docstring
    warns never to fall into. `rebuild_access_path` below re-derives a
    fresh AccessPath from a PlanShape plus THIS call's own fresh
    predicates, so every value in it is this execution's.
    """

    kind: Literal["seq_scan", "index_scan"]
    index_name: str | None
    reverse: bool


def access_path_shape(path: AccessPath) -> PlanShape:
    """The PlanShape a chosen AccessPath reduces to -- what plan/cache.py
    actually stores."""
    return PlanShape(path.kind, path.index.name if path.index is not None else None, path.reverse)


def rebuild_access_path(
    shape: PlanShape,
    table: TableSchema,
    indexes: list[IndexSchema],
    predicates: list[Predicate],
    table_ordinal: int = 0,
) -> AccessPath:
    """Re-derive the AccessPath a cached PlanShape names, against THIS
    call's own fresh `predicates` -- skips re-enumerating and re-costing
    every OTHER candidate (that's the whole saving a cache hit buys), but
    reuses the exact same column-matching logic enumerate_access_paths
    would have run for the winning index, so the rebuilt seek_terms/
    residual split is identical to a fresh enumeration+choose, just
    without paying to cost the candidates that would have lost anyway.


    For a fixed SQL text (plan/cache.py's cache key), WHICH columns carry
    an equality/inequality predicate is fixed too -- only the VALUES those
    predicates hold differ call to call -- so `_match_index_prefix`
    against the SAME index always reaches the SAME seek_terms/residual
    split; nothing here depends on `shape` beyond which index and
    direction to use.
    """
    if shape.kind == "seq_scan":
        return AccessPath("seq_scan", None, (), tuple(predicates))

    index = next(i for i in indexes if i.name == shape.index_name)
    by_column: dict[str, list[Predicate]] = {}
    for predicate in predicates:
        by_column.setdefault(predicate.column, []).append(predicate)

    path = _match_index_prefix(index, by_column, predicates)
    if path is not None:
        return path
    return _index_order_path(index, predicates, table, table_ordinal, reverse=shape.reverse)