# quilldb — Implementation Plan: Week 7, Query Processing


← [Index](README.md)  ·  Prev: [Week 6 — Concurrency](week-6-concurrency.md)  ·  Next: [Week 8 — Presentation](week-8-presentation.md)


---


# Week 7 Spec — Joins, Aggregation, Ordering


> **Read [chapter 17](../theory/exec/17-joins.md)** before `join.py` and
> **[chapter 18](../theory/exec/18-sorting-and-aggregation.md)** before `sort.py` / `aggregate.py`.


This week makes it feel like a database rather than a key-value store with SQL syntax. "Can it do a JOIN?"
is the first question a non-specialist asks.


**It's also a test of your week-3 design, and the result is information either way.** If the iterator
abstraction is right, a join is an operator with two children and aggregation is an operator that drains
its child — a few dozen lines each, no changes anywhere else. If adding a join turns out to require
touching the pager, the cursor, or the binder, that tells you the executor interface leaked something. Note
which happens; it's a genuinely good thing to be able to discuss.


---


## Week 7 contract


```sql
SELECT u.age, COUNT(*), AVG(o.total)
FROM users u JOIN orders o ON o.user_id = u.id
WHERE u.age > 25
GROUP BY u.age
HAVING COUNT(*) > 5
ORDER BY 2 DESC
LIMIT 10;
```


Supported:


- `JOIN ... ON` and comma joins, `INNER` and `LEFT [OUTER]`, table aliases, qualified column names
- `COUNT(*)`, `COUNT(col)`, `SUM`, `AVG`, `MIN`, `MAX`, with and without `GROUP BY`
- `HAVING`, `DISTINCT`, `ORDER BY` with multiple keys and mixed `ASC`/`DESC`, `LIMIT` / `OFFSET`
- ordinal `ORDER BY 2`, referring to the second output column
- a compiled-plan cache keyed by SQL text


Not supported, rejected with a typed error:


- `RIGHT` / `FULL OUTER JOIN`, `NATURAL JOIN`, `USING (...)`
- correlated subqueries, `EXISTS`, `IN (subquery)`, CTEs, window functions
- `GROUPING SETS` / `ROLLUP`, `FILTER`, `DISTINCT` inside an aggregate (`COUNT(DISTINCT x)`)
- more than **three** tables in one join (document the limit; exhaustive left-deep search is O(n!),
  intentionally bounded to at most six orders)


**Two rules that will produce all of your bugs:**


1. **Three-valued logic.** `NULL = NULL` is not true. A join predicate evaluating to `NULL` is not a match.
   `WHERE` and `ON` and `HAVING` all keep only `TRUE`. Spend twenty minutes on this before implementing
   comparison, not after.
2. **`ON` and `WHERE` are different.** `ON` is evaluated inside the join and participates in NULL
   extension; `WHERE` is a `Filter` above it. Swap them and a `LEFT JOIN` silently becomes an inner join —
   chapter 17 §17.8.


---


## Week 7 file map


```
src/quilldb/
├── exec/
│   ├── join.py          NEW — NestedLoopJoin (inner + left)
│   ├── aggregate.py     NEW — HashAggregate, aggregate functions
│   ├── sort.py          NEW — Sort (in-memory, documented limit), Limit
│   └── operators.py     + Distinct
├── sql/
│   ├── ast.py           + JoinClause, GroupBy, Having, OrderBy, Limit, FunctionCall
│   ├── parser.py        + all of the above, aliases, qualified names
│   └── binder.py        + resolve qualified names, aggregate/group validation
├── plan/
│   ├── planner.py       + join ordering, index-join selection, sort avoidance
│   └── cache.py         NEW — compiled-plan cache
└── errors.py            + AggregateError, SortLimitExceededError
```


---


## 40. `sql/binder.py` — the week's most underestimated file


Joins and aggregates are mostly a *binder* problem, not an executor problem. Three new
responsibilities, and each has a specific error to raise:


```python
    def resolve_column(self, name: QualifiedName, scope: list[TableScope]) -> ColumnRef:
        """`u.id` -> (table u, column 0). Bare `id` -> search all tables in scope.


        Raises:
            AmbiguousColumnError: bare name present in more than one table. This
                is the error people forget, and it must NOT silently pick the
                first match — that's how a query returns plausible wrong data.
            UnknownColumnError
        """


    def validate_aggregates(self, select_list, group_by, having) -> None:
        """Enforce the rule everyone half-knows.


        Every expression in the select list must be either (a) an aggregate, or
        (b) functionally determined by the GROUP BY keys. In practice: it appears
        in GROUP BY.


            SELECT region, name, COUNT(*) FROM t GROUP BY region
                                 ^^^^ ERROR: `name` is neither


        Raises:
            AggregateError, with a message naming the offending column.


        NOTE: real SQLite ALLOWS this and returns an arbitrary row's value.
        You should reject it. Being stricter than your reference is fine —
        document it, because a differential test will find the difference and
        you want it to be a known deviation rather than a surprise.
        """


    def resolve_ordinal(self, order_by, select_list) -> Expression:
        """`ORDER BY 2` means the second output column. 1-based. Out of range is
        an error, not a clamp."""
```


That `validate_aggregates` note is a real decision, so make it deliberately: SQLite permits bare columns in
an aggregate query (a documented extension), most other engines reject it, and your differential tests
*will* hit it. Choose "reject," write it in the README's deviations list, and add a test asserting the
error. A known, documented, tested deviation is a sign of care; an undocumented one is a bug.


---


## 41. `exec/join.py`


```python
class NestedLoopJoin(Operator):
    """Chapter 17. The inner operator is RE-OPENED once per outer row — which
    is what makes this an index nested loop join for free when the inner is an
    IndexScan parameterized by the outer row's join value.
    """


    def __init__(self, outer: Operator, inner: Operator, on: Expression,
                 join_type: Literal["inner", "left"]) -> None: ...


    def next(self) -> Row | None:
        """
        while True:
            if no current outer row: pull one; if none, return None
            pull from inner
            if inner exhausted:
                if left join and this outer row never matched:
                    row = outer + NULLs; advance outer; return row
                advance outer; re-open inner; continue
            if on(combined) is TRUE:      # TRUE, not truthy — NULL is not a match
                mark matched; return combined
        """
```


**Four traps, in the order they'll bite:**


1. **`close()` the inner every time you re-open it.** It's opened |outer| times. A leaked cursor or pin per
   outer row is a resource leak that only appears on large joins — and with week 6's pin counts, an
   unreleased pin makes a page permanently unevictable.
2. **The `matched` flag is per outer row.** Reset it when you advance the outer, not when you open.
3. **Pass the correlated value explicitly.** The inner `IndexScan` needs the *current* outer row's join
   value each re-open. Thread it as a parameter; don't reach into shared mutable state, which breaks the
   moment two joins nest.
4. **NULL is not a match.** `on()` returning `NULL` means skip — and for a `LEFT JOIN`, that outer row is
   still unmatched and must be NULL-extended.


```python
def test_left_join_emits_unmatched_outer_rows(db): ...
def test_null_join_key_never_matches(db):
    """Two rows both with cust_id NULL must NOT join to each other."""
def test_where_on_inner_column_after_left_join_drops_null_rows(db):
    """Documents chapter 17 §17.8's trap as intended behaviour, in YOUR engine."""
def test_inner_cursor_is_closed_once_per_outer_row(db):
    """Assert the pin count returns to its baseline after the join completes."""
```


---


## 42. `exec/aggregate.py`


```python
@dataclass
class AggSpec:
    init: Callable[[], object]
    step: Callable[[object, Value], object]
    final: Callable[[object], Value]




AGGREGATES = {
    "count_star": AggSpec(lambda: 0,          lambda s, _: s + 1,        lambda s: s),
    "count":      AggSpec(lambda: 0,          _count_step,               lambda s: s),
    "sum":        AggSpec(lambda: None,       _sum_step,                 lambda s: s),
    "avg":        AggSpec(lambda: (None, 0),  _avg_step,                 _avg_final),
    "min":        AggSpec(lambda: None,       _min_step,                 lambda s: s),
    "max":        AggSpec(lambda: None,       _max_step,                 lambda s: s),
}
```


**The semantics, all four of which are test cases (chapter 18 §18.5):**


| Case | Result | Why |
|---|---|---|
| `SUM` over zero rows | `NULL` | summing nothing has no value |
| `COUNT` over zero rows | `0` | counting nothing is zero |
| `COUNT(col)` with NULLs | skips them | counts non-NULL values |
| `COUNT(*)` with NULLs | counts rows | doesn't look at values |
| `AVG` of integers | real | `AVG(1,2)` is `1.5`, not `1` |
| `AVG`, `SUM`, `MIN`, `MAX` | skip NULLs | NULL is absence, not zero |


`avg` keeps `(sum, count)` and divides in `final` — never a running average (§18.5).


```python
class HashAggregate(Operator):
    """dict from group key -> per-aggregate state. Drains the child in open().


    quilldb uses a hash here where SQLite sorts, and that's a DEFENSIBLE
    difference rather than a divergence: SQLite avoids adding a hash table to a
    C library targeting embedded devices (chapter 18 §18.6), and Python gives you
    a dict for free. Say it that way.


    Group key must be a HASHABLE tuple of values, and NULL must be a valid key —
    `GROUP BY nullable_col` puts all NULL rows in one group. Python's None
    hashes fine; the trap is if you canonicalize NULL to something else.


    No GROUP BY at all = exactly one group. `SELECT COUNT(*) FROM t` where t is
    empty must return one row containing 0, NOT zero rows. This is the aggregate
    bug people ship most often.
    """
```


That last one deserves the emphasis: **an aggregate query with no `GROUP BY` always returns exactly one
row**, even over an empty table. Implementing it as "iterate groups and emit each" naturally returns *zero*
rows for an empty input, which is wrong and passes every test written against non-empty tables.


---


## 43. `exec/sort.py` and the planner


```python
class Sort(Operator):
    """PIPELINE BREAKER — open() drains the child before the first next().


    In-memory, with MAX_SORT_ROWS documented and enforced:


        raise SortLimitExceededError(
            f"ORDER BY requires sorting {n} rows, over the {MAX_SORT_ROWS} limit. "
            f"quilldb sorts in memory; add an index on the ORDER BY column to "
            f"avoid sorting entirely."
        )


    A documented limit with an actionable message is a LIMITATION. Getting
    OOM-killed is a BUG. The error text is part of the deliverable.


    If `limit` is set, use a bounded heap instead: O(N log K) time, O(K) memory
    (§18.4). Then the limit above doesn't apply, which is worth noting in the
    message.
    """
    def __init__(self, child, keys: list[tuple[Expression, bool]], limit: int | None = None): ...
```


**Ordering is a physical property and therefore part of cost**, not a post-processing rule. Which means
sort avoidance cannot be decided about an access path in isolation — it's decided about a *whole
candidate plan*, because in a left-deep nested loop the output order comes from the **outermost** table's
access path:


```python
    def sort_cost(candidate: PlanCandidate, order_by: list[OrderKey]) -> float:
        """0.0 if `candidate.output_order` already satisfies `order_by`, else the
        cost of the Sort this candidate would need on top.


        Satisfied when output_order starts with the ORDER BY keys, all ascending
        or all descending — a B+tree walks backwards for free (§18.3).


        Mixed ASC/DESC cannot be satisfied by one traversal. Sort.
        ORDER BY rowid on a rowid table needs nothing — the table IS in that order.
        """




    def total_cost_with_ordering(candidate: PlanCandidate,
                                 order_by: list[OrderKey]) -> float:
        """THE comparison key. Never rank candidates on `candidate.cost.total`
        alone when there's an ORDER BY."""
        return candidate.cost.total + sort_cost(candidate, order_by)
```


> ⚠️ **This is the one trap in the week, and it is silent.** The natural thing to write is
> `min(candidates, key=lambda p: p.cost.total)` and then add a `Sort` on top of the winner. That picks
> the cheapest *join*, then pays for a sort that a slightly-pricier ordered candidate would have avoided
> — so the plan is legal, the results are right, and the optimizer is quietly worse than the one you
> think you built. No test fails. This is the classic **"interesting orders"** problem, and it's exactly
> why System R's dynamic programming keeps the cheapest plan per subset *plus* the cheapest plan per
> useful ordering (chapter 12 §12.7). Compare complete alternatives — `cheap scan + sort` versus
> `ordered index scan` — and never prefer sort avoidance as a rule.


Join ordering uses the same candidate → estimate → cost → search pipeline introduced in week 4:


```python
@dataclass(frozen=True)
class PlanCandidate:
    relations: frozenset[TableId]
    root: OperatorSpec
    est_rows: int
    cost: PlanCost                   # the join tree only — NOT including any Sort above it
    output_order: tuple[OrderKey, ...]




def enumerate_join_plans(tables: list[TableRef], predicates,
                         stats: StatisticsCatalog) -> list[PlanCandidate]:
    """Enumerate every legal left-deep order for at most three tables.


    For each outer prefix, consider a full inner scan and every parameterized
    IndexScan whose seek key can be supplied by the outer row. LEFT JOIN edges
    constrain order; inner joins may be reordered freely.


    Return ALL of them. Do not prune to the cheapest plan per relation set:
    that's the pruning step that loses interesting orders, and with at most six
    orders there is nothing to gain by it.
    """
```


For an equijoin `R.a = S.b`, use the standard uniform estimate when both distinct counts are known:


```text
join_rows ≈ |R| × |S| / max(NDV(R.a), NDV(S.b))
```


This is Selinger's 1979 selectivity for `column1 = column2`, `1/MAX(ICARD1, ICARD2)`, unchanged — worth
knowing you're citing it rather than inventing it.


`NDV` is derived, not stored: `quill_stat1` holds average rows per prefix, so
`NDV(R.a) = R.row_count / rows_per_prefix[0]` — which means it is only available when `a` **leads an
index on R**. For an unindexed join key there is no distinct count at all.


```text
TODO(human): define the equijoin fallback when NDV is unknown on one or both sides.


Add the constant(s) to plan/cost.py alongside DEFAULT_EQUALITY_SELECTIVITY, and state the rule for
each of the three cases: both NDVs known, one known, neither known.
```


Then cost nested loops as:


```text
outer total cost + outer estimated rows × parameterized inner lookup cost
```


At three tables there are at most `3! = 6` orders. Exhaustive enumeration is simpler and — because
nothing is pruned — guarantees the minimum under quilldb's model, *including* the sort-avoidance
comparison above. SQLite uses N3 because it must plan much larger joins quickly; copying N3 here would
add machinery without improving the answer, and would reintroduce the interesting-orders problem that
pruning creates.


**Prove the search with estimates and actual counters.** On a deliberately asymmetric pair, show every
candidate's cost and measure both executable orders:


```python
def test_cost_search_picks_the_cheaper_join_order(db):
    small, large = 10, 100_000
    candidates = enumerate_for("SELECT ... FROM small JOIN large ON ...")
    chosen = min(candidates, key=lambda p: total_cost_with_ordering(p, order_by=[]))
    cheap = measure_pages(db, chosen)
    forced = measure_pages(db, most_expensive(candidates))
    assert cheap < forced / 10        # and PRINT both, for the README




def test_sort_cost_is_inside_the_join_comparison(db):
    """The interesting-orders regression. Construct a case where the cheapest
    JOIN order needs a Sort and the second-cheapest does not, with the Sort
    costing more than the gap between them. Ranking on cost.total alone picks
    the first; the correct optimizer picks the second."""
    q = "SELECT ... FROM a JOIN b ON ... ORDER BY b.indexed_col"
    candidates = enumerate_for(q)
    order_by = order_keys(q)


    naive = min(candidates, key=lambda p: p.cost.total)
    correct = min(candidates, key=lambda p: total_cost_with_ordering(p, order_by))
    assert naive is not correct, "fixture doesn't exercise the trap — retune it"


    assert sort_cost(correct, order_by) == 0.0
    assert measure_pages(db, plan_for(q)) <= measure_pages(db, with_sort(naive))
```


That second test is worth the twenty minutes it costs to build the fixture. Without it the trap above is
invisible: every result is correct, every other test passes, and the only symptom is that the optimizer
is mediocre in a way you can't see from the outside.


Also, per the roadmap, a **compiled-plan cache** keyed by SQL text — a dict from the exact statement string
to the bound, planned operator tree. Invalidate it whenever the schema cookie changes. `ANALYZE` also
bumps that cookie: otherwise a cached plan keeps using the decision made from old statistics even after
the statistics table changes.


---


## 44. The tests that carry this week


Unit tests per operator, then the two that find the real bugs:


```python
@given(query=join_aggregate_queries())
def test_differential_against_sqlite3(tmp_path, query):
    """THE test for week 7. Aggregate and join semantics have more corners than
    you can enumerate; sqlite3 knows all of them.


    Generate over: NULLs in join keys, NULLs in GROUP BY keys, empty tables,
    tables with one row, duplicate join keys on BOTH sides, LEFT JOIN with no
    matches, aggregates over zero rows, mixed ASC/DESC, LIMIT 0, OFFSET past
    the end.


    Compare result sets exactly. For unordered queries compare as multisets;
    for ORDER BY compare as sequences.
    """




def test_limit_short_circuits(db):
    """LIMIT 1 over a million rows must not read a million rows' worth of pages.
    Prove it with the page-read counter — the pull model's whole justification
    (chapter 09)."""
    pages = measure_pages(db, "SELECT * FROM million LIMIT 1")
    assert pages < 10
```


The NULL list in that docstring is not padding. Every entry is a case where a plausible implementation
differs from SQL's actual semantics, and each is one generated example away from being caught.


---


## Week 7 sessions


| # | 2 hours on | Done when |
|---|---|---|
| 1 | Parse `JOIN ... ON`, comma joins, aliases, qualified names; `AmbiguousColumnError` | the week's headline query parses |
| 2 | `NestedLoopJoin` inner + left, NULL-extension, cursor discipline | left-join and NULL-key tests green |
| 3 | Aggregate functions + `HashAggregate`, no-`GROUP BY` single-row case | the six-row semantics table is green |
| 4 | `GROUP BY`, `HAVING`, `DISTINCT`, `validate_aggregates` | headline query returns correct rows |
| 5 | `Sort` with multi-key mixed direction, `Limit`/`OFFSET`, top-K heap | mixed ASC/DESC correct; `LIMIT 1` short-circuits |
| 6 | Planner: `sort_cost` folded into the ranking key, exhaustive join search, `EXPLAIN`, plan cache | candidate costs and both page-read numbers recorded; the interesting-orders test is green |
| 7 | Differential test generators over the NULL matrix | green across a few hundred generated queries |


**Session 7 will find bugs in sessions 2–5.** That's the point, and it's why it isn't session 4. Don't
compress it.


---


## Week 7 definition of done


- [ ] The headline query — join + `WHERE` + `GROUP BY` + `HAVING` + `ORDER BY` ordinal + `LIMIT` — works
- [ ] `EXPLAIN` shows an index-driven inner loop when its estimated total cost wins, and a scan-based
      inner loop when the index is unavailable or more expensive
- [ ] The optimizer costs every legal left-deep order, picks the minimum, and records actual page reads for both a cheap and expensive order
- [ ] Candidates are ranked on join cost **plus** the Sort each one would require — the interesting-orders
      regression test proves ranking on `cost.total` alone would pick a different plan
- [ ] `LEFT JOIN` emits NULL-extended rows; a `WHERE` on the inner side visibly drops them (documented)
- [ ] Two NULL join keys do not match each other
- [ ] The inner cursor is closed once per outer row — pin counts return to baseline after a join
- [ ] `SUM` over zero rows is `NULL`; `COUNT` over zero rows is `0`
- [ ] `SELECT COUNT(*)` over an **empty** table returns exactly **one** row containing 0
- [ ] `COUNT(*)` vs `COUNT(col)` differ correctly in the presence of NULLs
- [ ] `AVG` of integers returns a real; `AVG` keeps sum and count, not a running average
- [ ] `GROUP BY` on a nullable column puts all NULL rows in one group
- [ ] Multi-key `ORDER BY` with mixed `ASC`/`DESC` is correct; NULLs sort first
- [ ] An `ORDER BY` matching an index prefix creates a no-Sort candidate; it wins only when its total cost is lower
- [ ] `ORDER BY indexed_col DESC` also avoids the sort
- [ ] `Sort` raises `SortLimitExceededError` with an actionable message rather than exhausting memory
- [ ] `LIMIT 1` over a million rows reads <10 pages — proven with the counter
- [ ] The plan cache is invalidated after both DDL and `ANALYZE` through the schema cookie
- [ ] Differential tests green over a few hundred generated join/aggregate queries including the whole
      NULL matrix
- [ ] Deviations from SQLite (bare columns in aggregate queries, the sort row limit, three-table maximum)
      are listed in the README
- [ ] `NOTES.md` entries for every bug over 20 minutes


**If the week runs short, cut in this order:** the plan cache, then `OFFSET`, then `DISTINCT`, then
`HAVING`. **Never cut** the differential tests or the join-order page-read measurement — the first is what
makes the semantics trustworthy and the second is a README number.