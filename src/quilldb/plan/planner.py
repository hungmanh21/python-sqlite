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
    """


    kind: Literal["seq_scan", "index_scan"]
    index: IndexSchema | None
    seek_terms: tuple[Predicate, ...]
    residual: tuple[Predicate, ...]
    rows_fetched: int = 0
    est_rows: int = 0
    cost: PlanCost | None = None




def enumerate_access_paths(
    table: TableSchema,
    indexes: list[IndexSchema],
    predicates: list[Predicate],
) -> list[AccessPath]:
    """Every legal way to answer a WHERE clause against `table`.


    SeqScan is unconditional -- chapter 12 §12.6 trap #2: it must remain a
    candidate even when an index applies, because a low-selectivity index
    can still lose on cost once stage 3 exists to say so. Every index in
    `indexes` is then checked against the leading-column rule (§12.3); an
    index that can't form a seek at all is simply omitted, not added with
    an empty seek.
    """
    paths = [AccessPath("seq_scan", None, (), tuple(predicates))]


    by_column: dict[str, list[Predicate]] = {}
    for predicate in predicates:
        by_column.setdefault(predicate.column, []).append(predicate)


    for index in indexes:
        path = _match_index_prefix(index, by_column, predicates)
        if path is not None:
            paths.append(path)


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