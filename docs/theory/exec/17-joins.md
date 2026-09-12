# 17 — Joins


> **Read during week 7, before `exec/join.py`.**
>
> **Time:** ~40 minutes. **Prerequisites:** [chapter 09](09-iterator-execution.md) (operators),
> [chapter 11](../btree/11-index-b-trees.md) (what an index seek costs),
> [chapter 12](../plan/12-the-query-planner.md) (access paths).


---


## 17.0 The problem: two tables, one result


```sql
SELECT c.name, o.total
FROM customers c JOIN orders o ON o.cust_id = c.id
WHERE c.region = 'APAC';
```


1,000 customers, 100,000 orders. Chapter 09 gave you operators that pull rows from *one* table. **What
does a join operator actually do?**


Start from the definition, because SQL's is unusually clean:


> **A join is a filtered cross product.** Conceptually: form every pair of one customer and one order, then
> keep the pairs where `o.cust_id = c.id`.


That definition is *exactly* correct and it is a catastrophic implementation. 1,000 × 100,000 =
**100,000,000 pairs** generated to keep maybe 100,000. And that's two tables; three tables cross-multiply
again.


So every join algorithm is an answer to one question: **how do you produce only the surviving pairs
without generating the doomed ones?** Every technique below is a different way to avoid enumerating a
product you already know is mostly false.


---


## 17.1 Nested loops: the answer that's always available


```python
for outer_row in customers:              # the OUTER relation
    for inner_row in orders:             # the INNER relation
        if inner_row.cust_id == outer_row.id:
            yield combine(outer_row, inner_row)
```


Six lines. It works for **any** join predicate — equality, inequality, `LIKE`, a user-defined function,
anything you can evaluate — because it just evaluates the predicate on every pair. That universality is
why every database has it and why it's the fallback when nothing better applies.


### Counting the real cost, which is not comparisons


The comparison count is |R| × |S| = 100 million, and that's how the textbook presents it. **The number
that actually matters is page reads**, and it's worse than it looks:


```
outer: read every page of customers once            =  P_R
inner: read every page of orders ONCE PER OUTER ROW  =  |R| × P_S
```


With `customers` at 10 pages and `orders` at 2,000 pages: 10 + 1,000 × 2,000 = **2,000,010 page reads.**
The inner table is re-scanned a thousand times.


**Two things immediately follow, and they're the two levers.**


**1. The buffer pool changes everything.** If `orders` fits in the pool, it's read from disk once and the
other 999 scans are memory hits. So the *effective* cost depends on the ratio of the inner relation to
your cache — which is why chapter 04's buffer pool is load-bearing here and why "which table is the inner
one" is a real decision.


**2. Block nested loops recover most of the loss for free.** Instead of one outer row at a time, buffer *B*
outer rows and check them all during one pass of the inner:


```
cost = P_R + (|R| / B) × P_S
```


At B = 100 that's 20,000 page reads instead of 2,000,000 — a 100× improvement from a `for` loop over a
buffer. **Notice it doesn't change the comparison count at all.** Same 100 million comparisons, 1% of the
I/O. That's the clearest possible illustration of chapter 00's lesson: in a storage engine, the cost model
is I/O, and an "optimization" that reduces CPU while leaving I/O alone usually does nothing.


SQLite doesn't do block nested loops, for a reason that's about to become clear.


---


## 17.2 Index nested loops: turning the inner scan into a seek


This is the transformation that matters, and it's why chapter 11 came first.


```python
for outer_row in customers:                          # still a scan
    for inner_row in idx_orders_cust_id.seek_eq(outer_row.id):   # a SEEK, not a scan
        yield combine(outer_row, inner_row)
```


The inner loop no longer looks at every order. It descends an index and reads only the matching ones.


```
cost = P_R + |R| × (index descent + matching rows)
     = 10 + 1,000 × ~4
     ≈ 4,010 page reads
```


**From 2,000,010 to about 4,010, by adding an index.** Same algorithm — still two nested loops — with the
inner relation's access path swapped from `SeqScan` to `IndexScan`. And you already built both: this join
is *composition*, not new machinery, which is the payoff of the iterator model in chapter 09.


This is also the sharpest example of why an index matters more inside a join than anywhere else. Outside a
join, a missing index costs you one table scan. Inside a join, it costs you **one table scan per outer
row.** The same missing index is a thousand times more expensive.


> **Say this out loud:** "A join is semantically a filtered cross product, and nested loops evaluate it
> directly, which works for any predicate. The cost that matters is that the inner relation gets re-read
> once per outer row. An index on the join column turns the inner scan into a seek, which took my test
> query from about two million page reads to four thousand — same algorithm, different access path. That's
> why index nested loop join is the workhorse."


---


## 17.3 SQLite has exactly one join algorithm, and that's a decision


Not an omission — a documented, reasoned choice. From the optimizer overview, §7, in full:


> **"SQLite implements joins as nested loops."**


That's the whole sentence. And it's still true in current versions: searching the current documentation
finds no "hash join," no "merge join," and no "sort-merge" anywhere.


You can see it in the generated bytecode, where the indentation is SQLite's own and marks loop nesting:


```
4     Rewind         0     16      <- OUTER loop over cust
5       Column         0     2   1     r[1] = cust.region
6       Ne             2     15  1     if r[1] != 'r3' goto 15
8       SeekGE         2     15  3   <- INNER loop, index on orders(cust_id)
9         IdxGT          2     15  3
13        ResultRow      4     2
14      Next           2     9       <- inner back-edge
15    Next           0     5         <- outer back-edge
16    Halt
```


Two back-edges, one nested inside the other. No build phase, no sorter, no merge. It is a nested loop in
the most literal sense the machine allows.


### The documentation has a section called "Hash Joins" that exists to explain its absence


This is the best-argued design decision in the whole optimizer document, and it's worth quoting at length
because the reasoning is transferable:


> "An automatic index is almost the same thing as a hash join. The only difference is that a B-Tree is used
> instead of a hash table. … SQLite constructs a transient index instead of a hash table in this instance
> because **it already has a robust and high performance B-Tree implementation at hand, whereas a
> hash-table would need to be added.** Adding a separate hash table implementation to handle this one case
> would increase the size of the library (which is designed for use on low-memory embedded devices) for
> minimal performance gain. SQLite might be enhanced with a hash-table implementation someday, but for now
> it seems better to continue using automatic indexes in cases where client/server database engines might
> use a hash join."


So when SQLite meets a join with no usable index, it doesn't fall back to a naive nested loop — it
**builds an index on the fly** and then does an index nested loop. Observable in the plan:


```
sqlite> EXPLAIN QUERY PLAN SELECT ... FROM cust c, inv i, ord o WHERE ...;
|--SCAN c
|--SCAN i
`--SEARCH o USING AUTOMATIC COVERING INDEX (ts=? AND amt=?)
```


`AUTOMATIC COVERING INDEX` is a transient B-tree built for this query and discarded afterwards. **It is
structurally a hash join with a B-tree instead of a hash table** — build phase, probe phase, same shape —
which means SQLite gets ~90% of the benefit for **zero new code**.


> **This is the argument to internalize, and it applies far beyond databases.** The reason not to add the
> hash table isn't that hash joins are bad; it's that a *second* data structure means a second
> implementation to test, tune, fuzz, and carry in a library that ships on billions of devices — to
> outperform a structure they've already hardened. **Reusing an existing well-tested mechanism at 90%
> efficiency beats adding a new one at 100%,** when the thing you're economizing on is complexity rather
> than cycles. That is a mature engineering judgement, and being able to make it is worth more in an
> interview than knowing what a hash join is.


### The Bloom filter (3.38.0) does not change this answer


Recent SQLite added a Bloom filter optimization, and it's easy to mistake for a new join algorithm. It
isn't. The identical query on the identical file, across two versions:


```
### 3.37.2 (pre-3.38)              ### 3.50.4
|--SCAN f                          SCAN f
|--SEARCH d1 USING INTEGER PK      BLOOM FILTER ON d1 (id=?)
`--SEARCH d2 USING INTEGER PK      BLOOM FILTER ON d2 (id=?)
                                   SEARCH d1 USING INTEGER PK
                                   SEARCH d2 USING INTEGER PK
```


**The plan shape is unchanged** — `SCAN f` outer, `SEARCH` inner. The filter is an extra cheap pre-test
*inside the existing nested loop*: before doing the B-tree seek, check a bitmap that can say "definitely
not present," and skip the seek. `where.c` states the conditions, and the fourth one is instructive:


```
**   (1)  The SEARCH happens more than N times where N is the number of rows in the table
**   (2)  Some searches are expected to find zero rows
**   (3)  Bloom-filter processing is not disabled.
**   (4)  The size of the table being searched is known by ANALYZE.
```


Condition 4 again — statistics unlocking an optimization rather than merely ranking one (chapter 12 §12.4).
And condition 2 is the essence of a Bloom filter: it only pays off when you expect to find *nothing*,
because that's the only case where a probabilistic "definitely absent" saves real work.


---


## 17.4 Hash join: build a lookup structure once


If the join is an **equality**, you don't need an index — you can build a hash table on the fly:


```python
# BUILD phase: the smaller relation
table = defaultdict(list)
for row in customers:
    table[row.id].append(row)


# PROBE phase: the larger relation, one pass
for order in orders:
    for cust in table.get(order.cust_id, ()):
        yield combine(cust, order)
```


```
cost = P_R + P_S = 10 + 2,000 = 2,010 page reads
```


Each relation is read **exactly once**. That's asymptotically better than index nested loops: O(|R| + |S|)
against O(|R| × log |S|), with no index required and no B-tree descents.


**Three hard requirements, and each is a reason it isn't universal:**


1. **Equality only.** A hash table answers "which rows have exactly this key." It cannot answer
   `o.total > c.credit_limit`. Nested loops can. **Every algorithm faster than nested loops buys its speed
   by restricting the predicate** — that's the pattern, and it's worth stating in exactly those words.
2. **Memory.** The build side must fit. When it doesn't, you need **grace hash join**: hash both relations
   into *k* partitions on disk such that matching keys land in the same partition, then join partition by
   partition. Correct, and now you have temp files, spill policy, and partition skew to manage.
3. **A good hash function and a skew story.** One key holding 40% of the rows makes one bucket a linear
   scan, and your O(|R| + |S|) quietly becomes O(|R| × |S|) for that bucket.


**Why it wins where it wins:** large table joined to large table with no useful index. That's an analytics
shape, and it's exactly the workload SQLite isn't for.


---


## 17.5 Sort-merge join: exploit order you may already have


Sort both relations on the join key, then walk them together like merging two sorted lists:


```
customers (by id):  1    3    7    9
orders (by cust_id):1 1  3    7 7 7   9
                    └────┴────┴──────┴──  advance whichever side is behind
```


```
cost = sort(R) + sort(S) + P_R + P_S
```


The merge itself is one linear pass. **The whole question is whether you have to pay for the sort** — and
often you don't:


- **The inputs may already be sorted.** An index scan on the join column produces rows in key order for
  free (chapter 11). Two index scans on the two join columns feed a merge join with **zero** sorting.
- **The output is sorted**, which may satisfy a downstream `ORDER BY` or `GROUP BY` and delete *that*
  sort too. This is the "interesting orders" idea from System R (chapter 12 §12.7): a plan that costs more
  in isolation can win because it produces order somebody else needed.
- **It streams**, so it handles relations far larger than memory without partitioning — unlike hash join,
  whose spill story is a whole subsystem.


Merge join is the classic choice for large-to-large equi-joins in a system that already has a good
external sorter — which chapter 18 is about.


---


## 17.6 Which algorithm when


| Algorithm | Page reads | Requires | Wins when |
|---|---|---|---|
| Nested loop | P_R + \|R\| × P_S | nothing | inner is tiny or cached; **any** predicate |
| Block nested loop | P_R + (\|R\|/B) × P_S | buffer space | no index, moderate sizes |
| **Index nested loop** | P_R + \|R\| × ~4 | index on inner join key | **selective outer side — the OLTP case** |
| Hash join | P_R + P_S | equality + memory | large ⋈ large, no index |
| Sort-merge | sort + P_R + P_S | sortable/sorted inputs | inputs already sorted, or output must be |


**Read the "Requires" column top to bottom** — it's a ladder of restrictions. Nested loops need nothing and
cost the most. Each faster algorithm demands something more: an index, or equality, or memory, or order.
There is no algorithm that is both fastest and unconditional, which is why real engines keep several and
why the planner exists.


---


## 17.7 Join *order* matters more than join *algorithm*


Three tables, and the algorithm is fixed as index nested loop. There are still 12 left-deep orderings, and
they are not close:


```
customers (1k, filtered to 10 by region) ⋈ orders (100k) ⋈ items (1M)


Order A: customers → orders → items
    10 outer rows × ~4 reads    +  ~100 matching orders × ~4  ≈  450 reads


Order B: items → orders → customers
    1,000,000 outer rows × ~4 reads                          ≈  4,000,000 reads
```


Nearly four orders of magnitude, same algorithm, same indexes. **The outer relation's cardinality
multiplies everything downstream**, so the single most important decision is what goes outermost — and the
rule is *put the most selective relation first*, because a `WHERE` that cuts 1,000 rows to 10 cuts the
entire rest of the plan by 100×.


SQLite reorders your `FROM` clause to do this. Verified — the query listed `ord` before `inv`, and the
plan chose otherwise:


```
### 3-table join, as written: FROM ord o, cust c, inv i ...
|--SCAN i                                        <- planner put `inv` outermost
|--SEARCH o USING INTEGER PRIMARY KEY (rowid=?)
`--SEARCH c USING INTEGER PRIMARY KEY (rowid=?)
```


And after `ANALYZE` it reordered again, to `SCAN c → SEARCH o → SEARCH i`, because the statistics changed
which relation looked most selective. **`FROM` clause order is a hint you cannot rely on and should not
try to control** — which is a genuinely useful practical fact.


This is why join ordering, not algorithm selection, is what the hard part of a query optimizer is about
(chapter 12 §12.7): the search space is factorial in the number of tables, and the payoff for getting it
right is measured in orders of magnitude.


---


## 17.8 Outer joins: the NULL-extended row, and why it constrains everything


An `INNER JOIN` drops unmatched rows. A `LEFT JOIN` must emit them, padded with NULLs:


```python
for outer_row in customers:
    matched = False
    for inner_row in index.seek_eq(outer_row.id):
        matched = True
        yield combine(outer_row, inner_row)
    if not matched:
        yield combine(outer_row, NULL_ROW)     # NULL extension
```


One boolean. That's the entire implementation difference, and it's a good thing to notice: outer joins are
cheap to *execute* and expensive to *optimize*, because of the following.


**Outer joins break the algebraic properties the planner depends on.**


- **Not commutative.** `A LEFT JOIN B` ≠ `B LEFT JOIN A`. So the planner cannot freely choose which side is
  outer — the SQL has *fixed* it. An inner join gives the planner two choices; a left join gives it one.
- **Not freely associative.** `(A LJ B) LJ C` and `A LJ (B LJ C)` can differ. So a left join acts as a
  **barrier** in the reordering search space.


Which produces the practical consequence: **a `LEFT JOIN` costs you optimizer freedom, not just rows.** If
you wrote a left join where an inner join would do, you've removed plans the planner could otherwise have
chosen. That's a better answer to "why is my query with five left joins slow" than anything about row
counts.


### The `WHERE` versus `ON` trap


The most common real-world outer-join bug, and it follows directly from the code above:


```sql
-- Intent: every customer, with their APAC orders (or NULLs)
SELECT * FROM customers c LEFT JOIN orders o ON o.cust_id = c.id
WHERE o.region = 'APAC';        -- ← silently an INNER JOIN
```


The NULL-extended rows have `o.region = NULL`. `NULL = 'APAC'` evaluates to `NULL`, not `TRUE`, and
chapter 09's rule is that `WHERE` keeps only rows that are exactly `TRUE` — so every NULL-extended row is
discarded and the outer join has been undone. Moving the predicate into the `ON` clause fixes it:


```sql
... LEFT JOIN orders o ON o.cust_id = c.id AND o.region = 'APAC';
```


**This is worth being able to explain cold.** It's a genuine production bug, it's a favourite interview
question, and the explanation ties together three-valued logic, `WHERE`-versus-`ON` evaluation order, and
NULL extension — three things you'd otherwise be asserting separately.


---


## 17.9 What you're building


Roadmap week 7. One operator:


```python
class NestedLoopJoin(Operator):
    def __init__(self, outer: Operator, inner: Operator,
                 predicate: Expression, join_type: Literal["inner", "left"]) -> None: ...


    def next(self) -> Row | None:
        """Advance the inner; when it's exhausted, advance the outer and re-open
        the inner. For 'left', emit a NULL-extended row when an outer row
        produced no matches.
        """
```


**The design point worth noticing:** the inner operator gets **re-opened** once per outer row. If that
inner operator is an `IndexScan` parameterized by the current outer row's join value, you have index
nested loop join *without writing a second operator* — the join is generic, and the access path decides
the cost. That's chapter 09's iterator model earning its keep, and it's the reason the join is 40 lines
rather than 200.


**Four things to get right:**


1. **`open()`/`close()` discipline on the inner.** It's opened and closed |R| times. A leaked cursor or an
   unreleased pin per outer row is a resource leak that only shows up on large joins.
2. **Correlated parameters.** The inner `IndexScan` needs the *current* outer row's value each time it's
   re-opened. Pass it explicitly rather than reaching into shared mutable state.
3. **`ON` versus `WHERE`.** `ON` is evaluated inside the join and participates in NULL extension; `WHERE`
   is a `Filter` above the join. Getting this backwards produces exactly §17.8's bug in your own engine.
4. **Three-valued logic in the join predicate.** A predicate evaluating to `NULL` is not a match — same
   rule as `WHERE` (chapter 09).


**The test that matters most** is differential: run every join query through `sqlite3` and through quilldb
and compare result sets, including empty results, all-NULL columns, duplicate join keys on both sides, and
a left join with no matches at all. Join semantics have many corners and an independent oracle checks all
of them at once.


---


## 17.10 Roads not taken


### Hash join and sort-merge join


§17.4 and §17.5 have the mechanisms. **Why not built:** both need machinery you don't have — hash join
needs a spill strategy to be honest about memory, sort-merge needs the external sorter from chapter 18 —
and neither helps the workloads you'll demo, which are small and indexed. **The answer to give:** *"I
implemented index nested loop, which is what SQLite does and what suits an OLTP workload. Hash join would
win on large unindexed equi-joins — it reads each side once instead of probing per outer row — and
sort-merge wins when the inputs are already sorted or the output needs to be. I didn't build them because
the same argument SQLite makes applies: I already have a B-tree, so a transient index gets me most of the
hash-join benefit with no new data structure."*


### Semi-joins and anti-joins


`WHERE EXISTS (...)` doesn't need every match — it needs *one*. A semi-join stops the inner loop at the
first hit; an anti-join (`NOT EXISTS`) emits the outer row only if there were none. **Why it matters:**
it's a one-line change to the loop (`break` after the first match) for a potentially large win, and
recognizing `IN`/`EXISTS` as semi-joins rather than subqueries is how a planner makes them fast. Cheap
enough to be a genuine "if I had one more hour" item.


### Worst-case optimal joins


For cyclic joins (the classic triangle query, `R(a,b) ⋈ S(b,c) ⋈ T(c,a)`), any binary join plan can be
asymptotically worse than optimal, because an intermediate result may be larger than the final answer.
Multi-way algorithms like Leapfrog Triejoin beat every pairwise plan on these. **Used by:** graph and
datalog engines. **Why it's worth naming:** it's the one case where "join two at a time, in the best order"
is provably the wrong *shape*, not just badly ordered — and knowing a limit of the standard model is
better than knowing one more variant inside it.


### Vectorized and columnar execution


Process a batch of 1,000 rows per operator call instead of one, over columnar arrays. **Used by:** DuckDB,
ClickHouse, Snowflake. **Why:** amortizes per-call overhead and lets the CPU use SIMD — often 10–100× on
analytic scans. **Why not here:** it's a rewrite of chapter 09's row-at-a-time model, and the bottleneck in
a pure-Python engine is interpreter overhead and I/O, not vector width. Worth naming as *the* modern answer
to "how would you make execution faster."


---


## 17.11 Check yourself


1. A join is semantically a filtered cross product. Why does no engine implement it that way?
2. Write nested loop join in six lines. What class of predicate does it support?
3. Why is |R| × |S| the wrong cost measure? What's the right one?
4. Block nested loops cut I/O 100× and change the comparison count by nothing. What does that tell you
   about optimizing a storage engine?
5. What exactly does an index change about nested loop join, and why is a missing index worse inside a join
   than outside one?
6. How many join algorithms does SQLite implement? Quote the sentence.
7. What is an `AUTOMATIC COVERING INDEX`, and why is it "almost the same thing as a hash join"?
8. Reconstruct SQLite's argument for not adding a hash table. What resource is it economizing?
9. Does the 3.38 Bloom filter make SQLite's joins something other than nested loops? What did the plan
   comparison show?
10. Why does a Bloom filter only pay off when searches are expected to find nothing?
11. Hash join is O(|R| + |S|) and beats index nested loop asymptotically. Name its three requirements.
12. When is a sort-merge join's sort free? Name two ways.
13. Fill in the "Requires" column of the algorithm table. What pattern do you see going down it?
14. Two orderings of the same three-table join differ by ~4 orders of magnitude. Why? What's the rule?
15. Can you rely on `FROM` clause order? What did `ANALYZE` do to the chosen order?
16. What is the entire implementation difference between an inner and a left join?
17. Why is a `LEFT JOIN` more expensive for the *optimizer* than an inner join?
18. `LEFT JOIN orders o ON ... WHERE o.region = 'APAC'` silently becomes an inner join. Explain, using
    three-valued logic.
19. Why does the generic `NestedLoopJoin` operator give you index nested loop join for free?
20. What is a semi-join, and what's the one-line change that implements it?


---


## 17.12 Sources


- [The SQLite Query Optimizer Overview](https://www.sqlite.org/optoverview.html) — §7 *"SQLite implements
  joins as nested loops."*; §14.1 "Hash Joins", including *"An automatic index is almost the same thing as
  a hash join… because it already has a robust and high performance B-Tree implementation at hand, whereas
  a hash-table would need to be added"* and *"SQLite might be enhanced with a hash-table implementation
  someday."*
- [`where.c`](https://github.com/sqlite/sqlite/blob/master/src/where.c) — the four Bloom-filter
  preconditions, and `sqlite3WhereCodeOneLoopStart()` generating code to *"test the Bloom filter and skip
  the subsequent B-Tree seek."*
- [SQLite release notes](https://www.sqlite.org/changes.html) — 3.38.0 (2022-02-22), *"Use a Bloom filter
  to speed up large analytic queries."*
- Selinger et al., **"Access Path Selection in a Relational Database Management System"** (SIGMOD 1979) —
  join ordering, left-deep plans, and interesting orders.
- Ngo, Porat, Ré & Rudra on worst-case optimal joins, for §17.10.
- **The measurements** were taken on `sqlite3` 3.37.2 (CLI) and 3.50.4 (via Python): the VDBE bytecode in
  §17.3, the `AUTOMATIC COVERING INDEX` plan, the Bloom-filter A/B on the *same database file* across the
  two versions, and the `FROM`-reordering results in §17.7. Note the Bloom filter requires `ANALYZE` — the
  first attempts showed no filter until statistics existed.


---


**Next:** [18 — Sorting and aggregation](18-sorting-and-aggregation.md) — how to sort more data than you
have memory, and why an index can delete the sort entirely.