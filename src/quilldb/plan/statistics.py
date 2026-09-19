"""Stage 2 of the cost-based pipeline (chapter 12 §12.5-§12.6): turning
`quill_stat1` prefix averages into row-count estimates for one AccessPath.


quill_stat1 stores exactly what SQLite's sqlite_stat1 stores (§12.5): for a
K-column index, K+1 space-separated integers -- total rows in the index,
then the average number of rows sharing the same value for the first 1,
2, ... K-1 columns. `10000 100 10 2` for an index on (a,b,c) means 10,000
rows total, ~100 rows per distinct `a`, ~10 rows per distinct (a,b), ~2
rows per distinct (a,b,c).


Two invariants this module has to hold, straight from §12.6:


1. STATISTICS ARE DERIVED, NOT LIVE. Nothing here updates a row count on
   INSERT/DELETE -- quill_stat1 is only refreshed by re-running ANALYZE.
   So an estimate can be stale, and that's an accepted tradeoff (bad
   estimates choose a slower plan, never a wrong one), not a bug to fix
   in this module.
2. MISSING STATISTICS MUST NEVER BLOCK PLANNING. A table that has never
   been ANALYZEd still needs an AccessPath with *some* row estimate, or
   choose_access_path() (not yet built) would have nothing to compare.
   default_table_stats()/default_index_stats() supply the documented
   fallback so estimate_row_counts() always has numbers to work with.
"""


from dataclasses import dataclass


from quilldb.catalog.schema import IndexSchema
from quilldb.errors import CorruptDatabaseError
from quilldb.plan.planner import AccessPath


# SQLite's own fallback for an unanalyzed table (optoverview.html): assume
# 1,000,000 rows so an unindexed scan looks expensive relative to any seek,
# without ever claiming to know the real size.
_DEFAULT_TABLE_ROWS = 1_000_000


# Absent a real distinct-value count for a column, assume every 10 rows
# share one value -- arbitrary but documented, and only a fallback: real
# ANALYZE output always overrides it.
_DEFAULT_ROWS_PER_VALUE = 10




@dataclass(frozen=True)
class TableStats:
    """`quill_stat1`'s row for a table with `idx IS NULL`: just a row count."""


    row_count: int




@dataclass(frozen=True)
class IndexStats:
    """One index's decoded `quill_stat1` row: the K+1 integers from the
    `stat` column, unpacked into names stage 2 can index into directly.


    `row_count` is the first integer (rows in the index -- always equal to
    the table's row_count, since every table row has exactly one entry per
    index). `rows_per_prefix` holds the remaining K integers, one per
    prefix length from 1 to K: position 0 is the average rows sharing the
    same value of the first indexed column alone, position 1 is the
    average sharing the same first-two-columns value, ..., position K-1 is
    the average sharing the same full-key value.


    Concretely, for `10000 100 10 2` on index (a,b,c) (K=3, so 4 integers):
    row_count=10000, rows_per_prefix=(100, 10, 2) -- position 0 is
    rows/distinct-a, position 1 is rows/distinct-(a,b), position 2 is
    rows/distinct-(a,b,c). `len(rows_per_prefix) == len(index.columns)`.
    """


    row_count: int
    rows_per_prefix: tuple[int, ...]




def default_table_stats() -> TableStats:
    """The documented fallback when a table has no quill_stat1 row at all."""
    return TableStats(_DEFAULT_TABLE_ROWS)




def default_index_stats(index: IndexSchema, table_stats: TableStats) -> IndexStats:
    """The documented fallback when an index has no quill_stat1 row: assume
    every prefix length is equally (un)selective at `_DEFAULT_ROWS_PER_VALUE`
    rows per value. Flat and deliberately uninformative -- an unanalyzed
    index should look mediocre at every prefix length, not be guessed into
    looking artificially good or bad at a particular one. Real ANALYZE
    output always overrides this.
    """
    return IndexStats(table_stats.row_count, (_DEFAULT_ROWS_PER_VALUE,) * len(index.columns))




def parse_stat1(stat: str, index: IndexSchema) -> IndexStats:
    """Decode one `quill_stat1.stat` string, e.g. "10000 100 10 2", into an
    IndexStats. Raises CorruptDatabaseError if the integer count doesn't
    match `len(index.columns) + 1` -- the K+1 rule from §12.5's format
    spec, so a hand-edited or truncated row is caught here rather than
    silently mis-indexed later.
    """
    parts = stat.split()
    expected = len(index.columns) + 1
    if len(parts) != expected:
        raise CorruptDatabaseError(
            f"quill_stat1 row for index {index.name!r} has {len(parts)} integers, expected {expected}"
        )
    try:
        numbers = [int(part) for part in parts]
    except ValueError:
        raise CorruptDatabaseError(f"quill_stat1 row for index {index.name!r} is not all integers: {stat!r}") from None
    return IndexStats(numbers[0], tuple(numbers[1:]))




def estimate_row_counts(path: AccessPath, stats: IndexStats | None, table_stats: TableStats) -> AccessPath:
    """TODO(human): fill in `rows_fetched` and `est_rows` (chapter 12 §12.6
    stage 2).


    Args:
        path: an AccessPath from enumerate_access_paths(), with
            `rows_fetched`/`est_rows` still at their 0 default.
        stats: this path's index statistics, or None for a seq_scan (a
            seq_scan has no index to look up -- use `table_stats` alone).
        table_stats: the table's own row count, always available (falls
            back to default_table_stats() if unanalyzed).


    Returns:
        A NEW AccessPath (it's frozen) with `rows_fetched` and `est_rows`
        set. Every other field is unchanged from `path`.


    What each number means, from §12.6:


      - `rows_fetched`: rows actually read off the index/table BEFORE
        residual filtering. For a seq_scan this is the whole table:
        `table_stats.row_count`. For an index_scan, look up
        `stats.rows_per_prefix[len(path.seek_terms) - 1]` -- the average
        rows sharing a value for AS MANY columns as `seek_terms` covers.
        (`len(seek_terms)` equalities/inequalities on N distinct columns
        means N columns' worth of prefix -- e.g. two seek_terms from
        `a=1 AND b=2` means prefix length 2, so index into
        rows_per_prefix[1].) Clamp to at least 1: an index matching zero
        real rows must not cost zero, or it would look free (§12.6's
        "must be clamped to at least 1" warning).
      - `est_rows`: rows that would actually survive AFTER `path.residual`
        is applied as a Filter on top. There's no statistic for a
        residual predicate's own selectivity in quill_stat1 (it only
        covers index prefixes) -- so the documented, deliberately simple
        choice is a flat 1/3 selectivity per residual predicate:
        `est_rows = max(1, rows_fetched * (1/3) ** len(path.residual))`.
        This is knowingly imprecise (§12.6 says as much about
        correlation), but it must never make est_rows EXCEED
        rows_fetched -- residual filtering only removes rows, never adds
        them.


    Two column counts that are easy to confuse: `len(path.seek_terms)` may
    be GREATER than the number of distinct columns consumed, because a
    sandwich (`b>2 AND b<5`) contributes two seek_terms for one column.
    Count distinct `seek_terms` columns, not len(seek_terms), when indexing
    into `rows_per_prefix`.
    """
    # get the distinct columns in seek_terms of AccessPath
    if path.kind == "index_scan":
        columns = {predicate.column for predicate in path.seek_terms}
        if stats:
            rows_fetched = max(1, stats.rows_per_prefix[len(columns) - 1])
        else:
            rows_fetched = 1
    else:
        rows_fetched = table_stats.row_count
   
    value = rows_fetched * (1 / 3) ** len(path.residual)
    est_rows = min(rows_fetched, max(1, int(value)))
       
    return AccessPath(
        kind = path.kind,
        index = path.index,
        seek_terms=path.seek_terms,
        residual=path.residual,
        rows_fetched=rows_fetched,
        est_rows=est_rows
    )