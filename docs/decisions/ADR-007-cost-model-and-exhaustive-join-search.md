# ADR-007: A page-cost model with SQLite-style statistics, and exhaustive left-deep join search


## Status


Accepted (statistics and access-path choice decided in week 4, join ordering in week 7; recorded in
week 8).


## Context


Once an index exists, something has to decide whether to use it. "If the column has an index, use it" is
wrong: an index lookup finds a rowid and then still has to fetch the row, so for a large enough fraction
of the table a sequential scan reads fewer pages. Deciding needs an estimate of how many rows match and a
cost for each candidate access path. Joins add a second decision, the order of the tables, which changes
how many times the inner side is re-read.


## Decision


- **Access paths are costed in pages.** The planner enumerates candidate paths (sequential scan, and each
  usable index seek), estimates rows from statistics, prices each path (`startup` and `cost`, shown by
  `EXPLAIN`), and picks the cheapest. An `IndexScan` is priced as if every row needs a random table
  lookup, because quilldb has no covering-index path.
- **Statistics come from `ANALYZE`**, stored in `quill_stat1` in SQLite's `sqlite_stat1` encoding (the row
  count, then the average number of rows per key prefix). Prefix averages, no histograms.
- **Join order is chosen by exhaustive search** over every legal left-deep order. There is no cap in the
  code. It is only *cheap* because the tests join at most three tables (3! = 6 orders); a longer chain
  still parses and runs, without that guarantee or test coverage. A `LEFT JOIN` anywhere in the chain
  disables reordering: the query plans in written order.
- **Joins are nested loops only.** SQLite has no hash join either; it argues for reusing a b-tree it has
  already hardened instead of carrying a second data structure.


## Consequences


- **The planner earns its keep on join order.** With 20 sensors and 5,000 wide readings rows, an INNER
  join is planned with the big table outer and reads **388** pages. The same join written with the small
  table outer, which a `LEFT JOIN` pins because it is never reordered, reads **7,703**, about 20 times
  more, though both examine about 100,000 rows (`python -m quilldb.bench join_order`). Rows examined
  cannot tell the orders apart; page reads can.
- **Pages, not rows, drive the choice, and that has a visible edge.** At small sizes the planner prices a
  scan of the inner table below the index seeks, so it does not use an index on the join column even when
  one exists; the benchmark had to avoid indexes to isolate the ordering effect. A page-counting model is
  right about I/O and blind to CPU work per row.
- **A plan that looks free can be a trap.** An unfiltered index scan would have looked almost free
  without the per-row lookup term (`NOTES.md` B7-1), which is why the term is there.
- **Statistics are estimates.** Skew and correlated columns can produce bad row estimates, and nothing
  re-checks them at runtime.
- **The `quill_stat1` name is deliberate.** The bytes match SQLite's, but under `sqlite_stat1` a real
  `sqlite3` would read quilldb's numbers and plan with them; a different name keeps the two planners
  independent. The cost: it is an ordinary user-visible table and appears in `.tables`.
- **Exhaustive search is `n!`.** Fine at three tables, unbounded past that.


## Alternatives considered


- **System R dynamic programming.** The classic answer (PostgreSQL for small joins, DB2): optimal
  left-deep order under the model, with a memo of the best plan per subset. Enumerating at most six orders
  directly is smaller and easier to inspect than building the memo.
- **SQLite's N3 polynomial heuristic.** Chosen by SQLite because it must handle much larger joins with a
  fixed planning budget; unnecessary at three tables.
- **Cascades / Volcano-style rule optimisers** (SQL Server, CockroachDB, DuckDB). Extensible, but a
  framework whose memo, rule scheduling and property enforcement cost more than a bounded set of scan,
  join and sort alternatives.
- **Genetic / randomised search** (Postgres GEQO). For when exhaustive search is impossible; it is
  non-deterministic, which complicates reproducible `EXPLAIN` output and tests.
- **Runtime-adaptive plans.** Attack the real weakness, that estimates are guesses that compound across
  joins, but need a re-plannable executor, which fights the pull-based model (ADR-003).
- **Hints (`INDEXED BY`).** A decision frozen at a moment when the data looked a certain way.
- **Hash join or a transient index for unindexed equi-joins.** Not built. SQLite's automatic transient
  index is the mechanism it substitutes for a hash join, and is the next step on the descope list.
