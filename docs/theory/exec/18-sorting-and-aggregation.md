# 18 — Sorting and Aggregation


> **Read during week 7, before `exec/sort.py` and `exec/aggregate.py`.**
>
> **Time:** ~40 minutes. **Prerequisites:** [chapter 09](09-iterator-execution.md) (operators),
> [chapter 11](../btree/11-index-b-trees.md) (an index is sorted).


---


## 18.0 The problem: `sorted()` doesn't fit in memory


```sql
SELECT * FROM events ORDER BY created_at;
```


10 million rows, ~800 bytes each: **8 GB**. Your machine has 2 GB free.


```python
rows = list(cursor)      # MemoryError
rows.sort()
```


Every language's built-in sort assumes the data is in memory. A database cannot. So: **how do you sort
more data than you can hold?**


There's a second problem hiding behind it, and it's the one that produces the bigger win. `ORDER BY` also
breaks something structural about chapter 09's design: every operator so far was **streaming** — `next()`
did a bounded amount of work and returned one row. A sort can't emit its first row until it has seen its
last. It's a **pipeline breaker**, and it changes the memory profile of the whole query.


---


## 18.1 External merge sort


The answer is two phases, and it's one of those algorithms that feels obvious once seen and is hard to
invent.


**Phase 1 — run generation.** Read as much as fits. Sort it in memory with whatever sort your language
has. Write it out as a **run**: a sorted file. Repeat until the input is consumed.


```
8 GB input, 500 MB of memory  ->  16 sorted runs of 500 MB each
```


**Phase 2 — k-way merge.** Open all 16 runs. Read the first record of each. Repeatedly emit the smallest
and advance that run, using a heap of size k to find the smallest in O(log k):


```
run 1:  3  9 14 ...        heap: (1,run3) (3,run1) (7,run2)
run 2:  7 11 22 ...   -->  emit 1, pull run3's next
run 3:  1  5 30 ...
```


**Memory: k input buffers plus the heap** — kilobytes, not gigabytes. Output is fully sorted and streams,
so a downstream operator can start consuming before the merge finishes.


### The cost, which is the reason this is a solved problem


Both phases read and write the data once, so **two passes over the data** — provided you can merge all
runs at once. If there are more runs than you can hold buffers for, you merge in multiple rounds:


```
passes = 1 + ceil( log_k( N / M ) )
```


Plug in real numbers: N = 8 GB, M = 500 MB, k = 100. `log_100(16)` < 1, so **two passes.** With
N = 1 TB and M = 1 GB, k = 100: `log_100(1024)` ≈ 1.5 → three passes.


> **Sorting a terabyte with a gigabyte of RAM takes three passes over the data.** That's the fact worth
> remembering, because it converts "we can't sort that, it doesn't fit" into a bandwidth calculation. And
> notice which parameter matters: raising *k* (the merge fan-in) reduces passes logarithmically, while
> raising *M* only reduces the run count linearly. **Fan-in is the lever**, which is why merge sort and not
> quicksort is the external algorithm — quicksort's partitioning has no fan-in to raise.


---


## 18.2 SQLite's sorter, and what a real implementation adds


`vdbesort.c` implements exactly the above, with vocabulary worth knowing:


> "The VdbeSorter object implements a **multi-threaded external merge sort** algorithm that is efficient
> even if the number of elements being sorted exceeds the available memory."


**It doesn't touch disk unless it has to:**


> "Records passed to the sorter via calls to Write() are initially held unsorted in main memory. Assuming
> the amount of memory used never exceeds a threshold, when Rewind() is called the set of records is sorted
> using an **in-memory merge sort**. In this case, **no temporary files are required**."


**A run is called a PMA, and they have levels:**


> "If the amount of space used to store records in main memory exceeds the threshold, then the set of
> records currently in memory are sorted and written to a temporary file in **"Packed Memory Array" (PMA)**
> format. A PMA created at this point is known as a **"level-0 PMA"**. Higher levels of PMAs may be created
> by merging existing PMAs together — for example merging two or more level-0 PMAs together creates a
> level-1 PMA."


**And the threshold is a number you already know:**


> the threshold "is set to the value returned by `PRAGMA main.page_size` multiplied by that returned by
> `PRAGMA main.cache_size`, in bytes."


So the sorter's memory budget *is* the buffer-pool budget. That's a nice piece of design economy: one
knob, `cache_size`, governs both caching and sorting, so a user tuning memory doesn't have to discover a
second setting. (`SQLITE_CONFIG_PMASZ` can override it, capped at 512 MiB — `SQLITE_MAX_PMASZ` is `1<<29`
— *"to prevent various integer overflows."*)


**Two refinements over the textbook version:**


- **Lazy final merge.** *"these PMAs are merged incrementally as keys are retrieved from the sorter by the
  VDBE"* — the merge is driven by the consumer pulling rows, so a query with a `LIMIT` never finishes
  merging. Chapter 09's pull model paying off inside the sorter.
- **Hierarchical merging for locality.** Past `SORTER_MAX_MERGE_COUNT` runs, *"a hierarchy of
  incremental-merges is used… This done is to improve locality."* Merging 500 runs at once means 500
  interleaved read streams and terrible locality; merging in tiers of 16 keeps each pass sequential. **The
  extra pass costs less than the seeks it avoids** — chapter 00 §0.5's sequential-versus-random ratio,
  showing up as an algorithm choice.


### Watching it spill


Measured: 400,000 rows sorted with `cache_size` deliberately shrunk to force spilling, syscalls counted
inside the sort:


```
=== PRAGMA temp_store = 1 (FILE) ===
  openat(AT_FDCWD, "/var/tmp/etilqs_c71dd7115516e178", O_RDWR|O_CREAT|O_EXCL|...) = 4
  pwrite64 calls during the sort: 587


=== PRAGMA temp_store = 2 (MEMORY) ===
  openat calls: 0
  pwrite64 calls: 0
```


**One** temp file, 587 writes — confirming *"all PMAs generated are appended to a single temporary file"* in
single-threaded mode. `temp_store=MEMORY` eliminates the file entirely, trading the memory ceiling back.


The `etilqs_` prefix is `sqlite` spelled backwards. It's deliberately obscure so that a support engineer
grepping a filesystem full of temp files can identify SQLite's — and it's a small reminder that operability
details end up in the code.


Also worth knowing: *"The temporary file is not opened and the information is not truly written to disk
until the page cache is full."* Small sorts never touch the filesystem at all, which is why the experiment
above had to shrink the cache to observe anything.


---


## 18.3 The best sort is no sort


Everything above is what you do when you have no choice. Very often you do have a choice, because
**an index is already sorted** (chapter 11) — so scanning it *is* a sort, at zero cost.


Verified, on a table with an index on `cid`:


```
SELECT * FROM ord ORDER BY amt          -- amt is NOT indexed
  |--SCAN ord
  `--USE TEMP B-TREE FOR ORDER BY               <- the sorter runs


SELECT * FROM ord ORDER BY cid          -- cid IS indexed
  `--SCAN ord USING INDEX ord_cid               <- no sort step at all


SELECT * FROM ord ORDER BY oid          -- oid is INTEGER PRIMARY KEY = rowid
  `--SCAN ord                                   <- no sort, no index needed


SELECT * FROM ord ORDER BY cid DESC     -- indexed, descending
  `--SCAN ord USING INDEX ord_cid               <- STILL no sort
```


Four results, and each teaches something different:


1. **Unindexed column → the sorter runs.** `USE TEMP B-TREE FOR ORDER BY` is the plan telling you a
   pipeline breaker just appeared.
2. **Indexed column → the sort vanishes.** The plan line disappears entirely; there's nothing to do.
3. **`ORDER BY rowid` needs no index at all**, because a table b-tree *is* stored in rowid order. Free
   ordering along one axis, which is what "clustered" means (chapter 11 §11.7).
4. **`DESC` is also free** — walk the same index backwards. A B+tree's leaf ordering is symmetric, so
   descending costs nothing. This is worth knowing because people add a second `DESC` index believing they
   need one; you only do for *mixed* orderings like `ORDER BY a ASC, b DESC`, where no single-direction
   traversal produces the order.


The documentation states the intent plainly: *"SQLite strives to implement ORDER BY clauses using a
preexisting index."* Otherwise it builds a *"transient index whose data is the row data and whose key is the
ORDER BY terms"* — note that even the fallback is a b-tree, the same reuse-what-you-have argument as
chapter 17 §17.3's automatic index.


> **Say this out loud:** "The cheapest sort is the one you don't do. An index is a sorted structure, so if
> the `ORDER BY` matches an index prefix the planner scans the index and skips sorting entirely — including
> `DESC`, which just walks the leaves backwards. `ORDER BY rowid` is free on a rowid table because the
> table itself is stored in that order. When nothing matches, you fall back to an external merge sort,
> which is two passes over the data as long as you can merge all the runs at once."


---


## 18.4 `LIMIT` changes the algorithm, not just the output


```sql
SELECT * FROM events ORDER BY score DESC LIMIT 10;
```


Ten million rows in, ten rows out. Sorting all 8 GB to discard 9,999,990 rows is absurd — and the fix is a
different algorithm, not a truncated one.


**Top-K with a bounded heap:** keep a min-heap of size 10. For each row, if it beats the heap's minimum,
replace it. At the end, the heap holds the top 10.


| | Full sort | Top-K heap |
|---|---|---|
| Time | O(N log N) | **O(N log K)** |
| Memory | O(N) — spills to disk | **O(K)** — 10 rows |
| Temp files | yes | **none** |
| Streams? | no | still consumes all input, but never spills |


With K = 10 and N = 10 million, `log K` ≈ 3.3 against `log N` ≈ 23 — and far more importantly, **the disk
disappears from the calculation entirely.** No runs, no merge, no temp file.


**This is why `LIMIT` is not a post-filter**, and it's a genuinely common misconception. `LIMIT` must be
pushed *into* the sort as a parameter. If your `Sort` operator sorts everything and a `Limit` above it takes
10, you've done all the work you were trying to avoid. Note that SQLite's sorter is documented as being for
`ORDER BY` *"that cannot be satisfied using indexes and **without LIMIT clauses**"* — the `LIMIT` case takes
a different path precisely because the algorithm differs.


**And when the `ORDER BY` matches an index, `LIMIT` becomes nearly free:** scan the index, take 10 rows,
stop. Ten rows read out of ten million. `ORDER BY indexed_col LIMIT 10` is one of the cheapest useful
queries in a database — and the same query without the index is one of the most expensive. **A 6-order-of-
magnitude difference decided by one index**, which is the single most persuasive benchmark you can put in a
README.


---


## 18.5 Aggregation: three functions and a shape


`COUNT`, `SUM`, `AVG`, `MIN`, `MAX`. The naive implementation collects all values and then computes — which
is a memory leak with a result attached. Every real system uses the same interface instead:


```python
class Aggregate(Protocol):
    def init(self) -> State: ...                       # empty accumulator
    def step(self, state: State, value: Value) -> State: ...   # fold in one row
    def final(self, state: State) -> Value: ...        # produce the answer
```


SQLite's VDBE calls these `AggStep` and `AggFinal`, and it's the same interface exposed to user-defined
aggregates. **Why this shape and not something simpler:**


1. **Constant memory per group.** `SUM` keeps one number regardless of input size.
2. **It streams.** One pass, no materialization, so it composes with everything upstream.
3. **`final` is separate for a reason** — see `AVG`.
4. **It's a fold**, which is why the same interface parallelizes: add a `merge(state, state)` and you can
   aggregate partitions independently and combine. That's exactly the map-reduce shape, and it's the reason
   this interface appears in Spark, in SQL engines, and in `functools.reduce`.


### The traps, all four of which are real bugs people ship


**`AVG` must keep sum and count, not a running average.** `final` divides. A running average
(`avg += (x - avg)/n`) accumulates floating-point error and is harder to get right — the separate `final`
step exists so you don't have to.


**`SUM` of no rows is `NULL`. `COUNT` of no rows is `0`.** Both are correct per the SQL standard, and the
asymmetry is deliberate: counting nothing is meaningfully zero, while summing nothing has no value. Get
this wrong and `SELECT SUM(x) FROM t WHERE false` returns `0`, which quietly corrupts downstream
arithmetic.


**`COUNT(*)` counts rows; `COUNT(col)` counts non-NULL values of `col`.** Different answers on the same
table, and the most common SQL misunderstanding there is.


**`MIN`/`MAX` can skip the scan entirely.** With an index on `x`, `SELECT MAX(x) FROM t` is one descent to
the rightmost leaf entry. SQLite implements this. It's the cheapest optimization in this chapter and it's
worth noticing *why* it's available: `MIN`/`MAX` are the only aggregates whose answer is a *position* in a
sorted order rather than a computation over all values.


---


## 18.6 `GROUP BY`: sort or hash, and SQLite picks one


```sql
SELECT region, COUNT(*), AVG(total) FROM orders GROUP BY region;
```


Now you need one accumulator *per group*, and there are exactly two strategies.


### Sort-based


Sort by the group key. Now identical keys are adjacent, so make one pass and emit a result whenever the key
changes:


```
after sorting by region:
  APAC  ┐
  APAC  ├─ accumulate
  APAC  ┘
  EMEA  ← key changed: emit the APAC row, reset the accumulator
```


**Memory: one group's state.** Cost: the sort. Output arrives in group-key order, which may satisfy a
downstream `ORDER BY` for free.


### Hash-based


A dict from group key to accumulator. One pass, no sort:


```python
groups = {}
for row in rows:
    st = groups.setdefault(row.region, agg.init())
    groups[row.region] = agg.step(st, row.total)
```


**Memory: every group at once.** Cost: one pass. Output in arbitrary order. Faster when the number of
*groups* is small even if the number of *rows* is huge — grouping a billion rows into 5 regions needs 5
accumulators and no sort at all.


### SQLite has no hash aggregation


Verified two ways. In `select.c` the strategy is a single binary flag:


```c
int groupBySort;    /* Rows come from source in GROUP BY order */
...
if( sqlite3WhereIsOrdered(pWInfo)==pGroupBy->nExpr ){
  /* The optimizer is able to deliver rows in group by order so
  ** we do not have to sort. ... */
  groupBySort = 0;
}else{
  /* Rows are coming out in undetermined order.  We have to push
  ** each row into a sorting index, terminate the first loop,
  ** then loop over the sorting index in order to get the output
  ** in sorted order */
  ExplainQueryPlan2(..., "USE TEMP B-TREE FOR %s", "GROUP BY");
  groupBySort = 1;
```


**Two paths: sorted already, or sort it.** And searching all 337 KB of `select.c` for "hash" turns up only
identifier-resolution hash tables for column-name collision checks — nothing aggregation-related.


Observable:


```
SELECT cid, count(*) FROM ord GROUP BY cid        -- no usable index
  |--SCAN ord
  `--USE TEMP B-TREE FOR GROUP BY


SELECT cid, count(*) FROM ord GROUP BY cid        -- with an index on ord(cid)
  `--SCAN ord USING COVERING INDEX ord_cid        <- the sort step is GONE
```


Combined with chapter 17's "nested loops only," this gives a striking summary: **SQLite has essentially no
hash-table-based query operator.** No hash join, no hash aggregation, no hash distinct. Everything is
b-trees and sorting.


**Which is a coherent architecture, not a set of omissions.** One data structure, hardened over 25 years,
serving tables, indexes, transient indexes, sorters, and grouping. The cost is that analytic workloads —
large joins, high-cardinality grouping — are where SQLite is weakest, and that's exactly the workload it
declines to target. **Being able to state a system's architectural bet in one sentence, along with the
workload it therefore serves badly, is the most useful form of "do you know how this works."**


---


## 18.7 `DISTINCT` is `GROUP BY` with the aggregate removed


Same problem — collapse duplicate keys — so the same mechanism, from literally the same code path. Note the
shared format string in the snippet above; it emits `"USE TEMP B-TREE FOR DISTINCT"` when the caller is
`DISTINCT`:


```
SELECT DISTINCT cid FROM ord
  |--SCAN ord
  `--USE TEMP B-TREE FOR DISTINCT
```


And it falls away the same way when an index provides the order. **`SELECT DISTINCT x` and
`SELECT x GROUP BY x` are the same query**, and a good engine plans them identically — a nice thing to check
in your own implementation, because if they don't produce the same plan, one of them is missing an
optimization.


---


## 18.8 What you're building


Roadmap week 7:


```python
class Sort(Operator):
    """A PIPELINE BREAKER. open() consumes the entire child before the first
    next() can return.


    The roadmap's baseline is IN-MEMORY with a documented row limit that raises
    a clear error rather than exhausting memory. That's an honest scope choice —
    a documented limit is a limitation; an OOM kill is a bug.


    The external merge sort of §18.1 is the stretch goal. If you build it, give
    the spill threshold a deliberately tiny test setting — an external sorter
    that never spills during testing is an external sorter you haven't tested.
    """
    def __init__(self, child, keys: list[tuple[Expression, bool]],   # (expr, descending)
                 limit: int | None = None) -> None: ...
        # `limit` here, NOT a Limit operator above — §18.4




class HashAggregate(Operator):
    """quilldb CAN use a dict, unlike SQLite — you have one for free in Python.


    Say this deliberately: SQLite avoids hash aggregation because adding a hash
    table to a C library aiming at embedded devices costs code size for modest
    gain. In Python a dict is free, so the tradeoff genuinely differs. That's a
    better answer than either copying them blindly or diverging silently.
    """




class Limit(Operator):
    """Still needed for LIMIT without ORDER BY. Must stop pulling from its child
    once satisfied — that's the whole point of the pull model (chapter 09)."""
```


**Four things to get right:**


1. **`ORDER BY` with mixed direction** (`a ASC, b DESC`) needs a comparator, not a single reverse flag. Get
   the sign per key.
2. **NULL ordering.** Chapter 03: NULLs sort first, and SQLite puts them first ascending. Assert it —
   it's a differential-test discrepancy waiting to happen.
3. **Push `LIMIT` into `Sort`.** Then implement top-K, and measure the difference; it's a good README
   number.
4. **Check whether the planner can skip the sort.** If the `ORDER BY` matches an index prefix you're
   already scanning, emit no `Sort` at all — and make `EXPLAIN` show that, since a *missing* operator is
   the most convincing evidence of an optimization.


**Differential testing is the right oracle here.** Aggregate semantics have many corners — `SUM` of no
rows, `COUNT(col)` with NULLs, `AVG` of integers, `MIN` of mixed types, `GROUP BY` on a NULL key, `DISTINCT`
with NULLs — and comparing against `sqlite3` checks all of them without you having to enumerate the
standard.


---


## 18.9 Roads not taken


### Approximate aggregation


`COUNT(DISTINCT user_id)` over a billion rows needs a billion-entry set. **HyperLogLog** answers it to
within ~2% using about 1.5 KB, by tracking the maximum number of leading zeros in hashed values.
**Used by:** Redis `PFCOUNT`, BigQuery's `APPROX_COUNT_DISTINCT`, Presto. **Why it's worth naming:** it's
the clearest example of trading *exactness* for resources, a dimension most engineers never consider
available. When someone asks how you'd scale an analytics query, "does it have to be exact?" is often the
best question.


### Window functions


`ROW_NUMBER() OVER (PARTITION BY region ORDER BY total DESC)`. Like aggregation but emits a value **per
row** rather than per group, over a sliding frame. **Why they're more work than they look:** the frame can
be `ROWS BETWEEN 2 PRECEDING AND CURRENT ROW`, so you need a buffer of rows, not an accumulator — and the
whole thing must be re-partitioned and re-sorted. SQLite has them (since 3.25). Out of scope, and the right
"future work" entry because they'd reuse your sorter.


### Pre-aggregation and materialized views


Maintain the answer as the data changes, so the query is a lookup. **Used by:** every warehouse, and every
`counts` table anyone has ever hand-rolled. **Why not:** it's chapter 11's derived-data problem again —
something must keep it consistent, and now the something is a summary table rather than an index.
**Notice that indexes, materialized views, and caches are the same idea** with different refresh
strategies; recognizing that is more useful than any one of them.


### Vectorized aggregation


Aggregate a batch of 1,000 values per call over columnar arrays. **Used by:** DuckDB, ClickHouse. **Why
not:** chapter 17 §17.10 — it's a rewrite of the execution model, and in Python the win is bounded by
interpreter overhead. Still the correct answer to "how would you make aggregation 50× faster."


---


## 18.10 Check yourself


1. Why can't a database use its language's built-in sort?
2. What is a pipeline breaker, and what does `ORDER BY` change about a query's memory profile?
3. Describe both phases of external merge sort. What's in memory during phase 2?
4. How many passes to sort 1 TB with 1 GB of RAM and fan-in 100? Which parameter would you raise first, and
   why?
5. Why is merge sort the external algorithm rather than quicksort?
6. What is a PMA, and what makes a level-1 PMA?
7. What determines SQLite's sorter memory budget, and why is it a nice design choice?
8. Why does SQLite merge in a hierarchy past a threshold rather than merging all runs at once?
9. `ORDER BY cid DESC` on an index over `(cid)` requires no sort. Why? When *would* you need a second
   index?
10. Why is `ORDER BY rowid` free on a rowid table?
11. `ORDER BY score DESC LIMIT 10` over 10M rows: what algorithm, what time, what memory?
12. Why must `LIMIT` be pushed into the sort rather than applied above it?
13. Why do aggregates use init/step/final rather than collect-then-compute? Give three reasons.
14. Why must `AVG` keep a sum and a count?
15. `SUM` of zero rows and `COUNT` of zero rows return different things. What, and why is that right?
16. `COUNT(*)` vs `COUNT(col)` — when do they differ?
17. Why can `MAX(x)` skip the scan, and why is that available for `MIN`/`MAX` but not `SUM`?
18. Compare sort-based and hash-based grouping on memory, cost, and output order. When does hash win big?
19. Does SQLite have hash aggregation? What are the two code paths, and what did grepping `select.c` show?
20. State SQLite's architectural bet in one sentence, and name the workload it therefore serves badly.
21. Why are `SELECT DISTINCT x` and `SELECT x GROUP BY x` the same query?
22. quilldb uses a dict for grouping where SQLite doesn't. Why is that defensible rather than a divergence?
23. Why should your external sorter have a deliberately tiny spill threshold in tests?


---


## 18.11 Sources


- [`vdbesort.c`](https://github.com/sqlite/sqlite/blob/master/src/vdbesort.c) — the header comment quoted
  throughout §18.2: "multi-threaded external merge sort," in-memory sort when under threshold, PMA
  definition and levels, the `page_size × cache_size` threshold, incremental merging driven by the VDBE,
  the locality rationale for hierarchical merges, `SORTER_MAX_MERGE_COUNT`, and single-threaded mode
  appending all PMAs to one file. Also the restriction to `ORDER BY` "without LIMIT clauses."
- [`select.c`](https://github.com/sqlite/sqlite/blob/master/src/select.c) — the `groupBySort` flag with
  both branches quoted in §18.6, and the shared `"USE TEMP B-TREE FOR %s"` explain string.
- [Temporary Files Used By SQLite](https://www.sqlite.org/tempfiles.html) — *"SQLite strives to implement
  ORDER BY clauses using a preexisting index"*; the transient-index fallback; and *"The temporary file is
  not opened and the information is not truly written to disk until the page cache is full."*
- [`main.c`](https://github.com/sqlite/sqlite/blob/master/src/main.c) — `SQLITE_CONFIG_PMASZ` and the
  512 MiB `SQLITE_MAX_PMASZ` cap.
- Flajolet, Fusy, Gandouet & Meunier, **"HyperLogLog"** (2007) — for §18.9.
- **Measurements** on `sqlite3` 3.37.2 / Python 3.50.4: the `EXPLAIN QUERY PLAN` outputs in §18.3, §18.6 and
  §18.7 are verbatim; the `temp_store` syscall counts in §18.2 were taken by tracing a 400,000-row sort
  with `cache_size` shrunk to force spilling. Not verified: the internal PMA byte layout, level-1+
  promotion, and multi-threaded sorting.


---


**Next:** [19 — Measuring it](../benchmarks/19-measuring-it.md) — why you report page reads instead of
milliseconds, and how to state a result nobody can poke a hole in.