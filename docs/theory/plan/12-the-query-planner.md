# 12 — The Query Planner


> **Read during week 4, before you write `plan/planner.py`.** Covers access-path selection and
> `EXPLAIN`.
>
> **Time:** ~45 minutes. **Prerequisites:** [chapter 09](../exec/09-iterator-execution.md) (operators),
> [chapter 11](../btree/11-index-b-trees.md) (what an index can do).


---


## 12.0 The problem: you built the index. Who decides to use it?


Week 4 gives you two ways to answer `SELECT * FROM users WHERE email = ?`:


```
SeqScan(users) → Filter(email = ?)          2,400 page reads
IndexScan(ix_email, email = ?)                   7 page reads
```


Same rows out, 340× apart. Nothing in the SQL says which to use — the user wrote *what* they want, not
*how*. Something has to choose, and that something is the **planner**.


This is the component that makes SQL a declarative language rather than a verbose one. And it's the
part of a database engineers interact with most and understand least: every "why is my query slow"
question is a question about the planner's choice.


**How would you choose?** Take a minute. The naive rule is one line long, and there are at least three
distinct ways it's wrong.


---


## 12.1 Your first instinct: "if a WHERE column has an index, use it"


```python
for conjunct in where_clause:
    if isinstance(conjunct, Equality) and has_index(conjunct.column):
        return IndexScan(index_for(conjunct.column), conjunct)
return SeqScan(table)
```


Reasonable, and it's roughly what you'll build. But here are three queries it gets wrong, each for a
different reason.


**1.** `WHERE c = 3`, with an index on `(a, b, c)`. There *is* an index on `c`. Using it as a seek is
impossible, and if you try, you'll return wrong rows.


**2.** `WHERE lower(email) = 'ada@x.com'`, with an index on `email`. There is an index on the column
mentioned. It cannot be used at all.


**3.** `WHERE x = 1 AND y = 5`, with indexes on both `x` and `y`, where `x` has 2 distinct values and
`y` has 1,000. Both are "an equality on an indexed column." One reads 5,000 rows and the other reads
10. Your rule picks whichever it saw first.


Three failures, three separate concepts: **sargability** (2), the **leading-column rule** (1), and
**selectivity** (3). Take them in that order.


---


## 12.2 Sargability: the predicate has to be shaped right


The word is ugly — a contraction of "Search ARGument able" from 1970s IBM — but the idea is exact:


> **A predicate is sargable if it can be turned into a seek on an index.** Which requires the indexed
> column to appear *bare* on one side of a comparison, with everything else evaluable without looking
> at the row.


SQLite's optimizer documentation lists the shapes that qualify, and the list is worth having in full
because it's more permissive than people assume:


> "To be usable by an index a term must usually be of one of the following forms:
> `column = expression` / `column IS expression` / `column > expression` / `column >= expression` /
> `column < expression` / `column <= expression` / `expression = column` / `expression IS column` /
> `expression > column` / … / `column IN (expression-list)` / `column IN (subquery)` /
> `column IS NULL` / `column LIKE pattern` / `column GLOB pattern`"


Note what's included: `IN` a list *or a subquery*, `IS NULL`, and even `LIKE`/`GLOB` (only for prefix
patterns, and only under conditions — a trailing `%` is fine, a leading one is not). Note that the
column may be on either side. And note the operative word `column` — bare.


### Why `lower(email) = 'x'` is hopeless, and it's not an implementation gap


This is the one to understand mechanically rather than memorize, because the reasoning transfers.


The index on `email` is a tree sorted by `email`. Sorted order is the *only* thing that makes a seek
possible: you can discard half the tree at each step because you know which side your target is on.


Now ask: does knowing the sort order of `email` tell you anything about the sort order of
`lower(email)`? No. `'Zoe@x.com'` sorts before `'ada@x.com'` in `BINARY` order (uppercase `Z` is byte
0x5A, lowercase `a` is 0x61), but `'zoe@x.com'` sorts *after* `'ada@x.com'`. The function scrambles the
order. **An index sorted by `f(x)` is a different index from one sorted by `x`,** for any `f` that
isn't monotonic — and even for a monotonic one, the engine would need to *know* it's monotonic to
exploit it.


So the only way to seek is to scan every row, compute `lower(email)`, and compare. Which is a
`SeqScan` with a `Filter`. The index is not being ignored out of laziness; it genuinely contains no
usable information for this question.


The same argument kills a family of predicates that people write all the time:


| Not sargable | Why | Sargable rewrite |
|---|---|---|
| `WHERE lower(email) = ?` | index sorted by `email`, not `lower(email)` | index on `lower(email)`, or store a normalized column |
| `WHERE age + 0 = 30` | the index stores `age`, not `age + 0` | `WHERE age = 30` |
| `WHERE substr(name,1,3) = 'ada'` | prefix *function*, not prefix *comparison* | `WHERE name LIKE 'ada%'` |
| `WHERE strftime('%Y', ts) = '2026'` | scrambles order | `WHERE ts >= '2026-01-01' AND ts < '2027-01-01'` |
| `WHERE a = 1 OR b = 2` | see below | two index scans unioned, or an index on the right thing |


That table is one of the most practically useful things in this directory. **Recognizing non-sargable
predicates is the single highest-value database skill for an application backend engineer**, because
it's the actual cause of most slow queries in production, and the fix is usually a rewrite rather than
a new index. It's also exactly the kind of thing interviewers ask, because it distinguishes people who
have debugged a real slow query from people who have read about indexes.


### `OR` is special, and worth knowing why


`WHERE a = 1 OR b = 2` can't be a single seek: the qualifying rows are in two disjoint regions of two
different indexes. The doc is blunt about the naive case — *"The index is not usable because the WHERE
clause terms are connected by OR instead of AND."*


Whereas `AND` is *free* to discard terms. If you use `a = 1` as a seek and ignore `b = 2`, you get a
superset of the answer, and a `Filter` on top removes the rest. **`AND` lets you use a subset of the
predicates and re-check the rest; `OR` doesn't, because dropping a branch of an `OR` loses rows.**
That asymmetry is the whole reason `AND` is planner-friendly, and it's the cleanest one-sentence answer
to "why is my `OR` query slow."


(SQLite does have an `OR` optimization that runs one index scan per branch and unions the rowids. It's
real, it's in §12.8's roads not taken, and it's out of scope for week 4.)


> **Say this out loud:** "A predicate is sargable if the indexed column appears bare on one side of a
> comparison, so the index's sort order can be used to skip rows. `lower(email) = ?` isn't sargable
> because an index sorted by `email` says nothing about the order of `lower(email)` — you'd need an
> index on the expression itself. And `AND` is planner-friendly because you can seek on one term and
> re-check the others, which you can't do with `OR` without losing rows."


---


## 12.3 The leading-column rule: you can only constrain a prefix


### The intuition first


A phone book is sorted by `(last_name, first_name)`. Three questions:


- *"Find Nguyen, Anh."* Easy — one lookup.
- *"Find every Nguyen."* Easy — they're all together, in one contiguous block.
- *"Find every person named Anh."* **You read the entire book.** The Anhs are scattered — one under
  Nguyen, one under Tran, one under Pham. There is no block to find.


A composite index is exactly this. An index on `(a, b, c)` is sorted by the *tuple* `(a, b, c)`, which
means it is sorted by `a` first, and only sorted by `b` *within* a fixed value of `a`. So `b` alone
gives you no contiguous region, for the same reason first names don't.


### The rules


SQLite's documentation states three, and they follow from that one picture:


> "The initial columns of the index must be used with the `=` or `IN` or `IS` operators. **The
> right-most column that is used can employ inequalities.** For the right-most column of an index that
> is used, there can be up to two inequalities that must sandwich the allowed values of the column
> between two extremes."


> "It is not necessary for every column of an index to appear in a WHERE clause term in order for that
> index to be used. **However, there cannot be gaps in the columns of the index that are used.**"


> "index columns will not normally be used (for indexing purposes) if they are to the right of a column
> that is constrained only by inequalities. *(See the skip-scan optimization below for the exception.)*"


Summarised: **equalities on a prefix, then at most one column with inequalities, then stop.**


### Verified, one query at a time


`t(a,b,c,d,e)`, `CREATE INDEX abc ON t(a,b,c)`, 10,000 rows, `SELECT *` in every case:


| Query | Plan | What happened |
|---|---|---|
| `WHERE a=1` | `SEARCH … (a=?)` | prefix of 1 |
| `WHERE a=1 AND b=2` | `SEARCH … (a=? AND b=?)` | prefix of 2 |
| `WHERE a=1 AND b=2 AND c=3` | `SEARCH … (a=? AND b=? AND c=?)` | whole index |
| `WHERE a IN (1,2,3) AND b=2` | `SEARCH … (a=? AND b=?)` | `IN` counts as equality |
| `WHERE a IS NULL AND b=2` | `SEARCH … (a=? AND b=?)` | so does `IS` |
| `WHERE a=1 AND b>2 AND b<5` | `SEARCH … (a=? AND b>? AND b<?)` | two sandwiching inequalities |
| `WHERE a=1 AND b>2 AND c=3` | `SEARCH … (a=? AND b>?)` | **`c` dropped** — it's right of an inequality |
| `WHERE a=1 AND c=3` | `SEARCH … (a=?)` | **`c` dropped** — gap at `b` |
| `WHERE c=3` | `SCAN t` | no usable prefix at all |


Every rule confirmed, including the two "dropped" cases. Note that dropping a term is not wrong — it's
a *smaller seek plus a filter*. `WHERE a=1 AND c=3` seeks the `a=1` block and then checks `c` on each
row it finds.


### Why a gap is fatal, concretely


Lay out the index entries for `(a, b, c)` and look at where `c = 3` lives:


```
a=1  b=1  c=3   ← match
a=1  b=1  c=7
a=1  b=2  c=3   ← match
a=1  b=2  c=9
a=1  b=3  c=3   ← match
a=2  b=1  c=3   ← match
```


With `a = 1` fixed, the matching `c` values are **not adjacent** — they're one per `b` value, spread
through the whole `a = 1` block. There's no range to seek. So the best available move is exactly what
SQLite did: seek the `a=1` block, then examine every entry in it.


And `WHERE b = 2` with no constraint on `a`? The `b=2` entries are spread through the *entire* index,
one region per distinct `a`. Which is the phone-book "find every Anh" — read everything.


...unless you know something extra. Which is the next section.


---


## 12.4 The skip-scan: when statistics unlock a plan that didn't exist


Here is the same query, on the same file, twice — the only difference being whether `ANALYZE` has run:


```
-- no sqlite_stat1 table
sqlite> EXPLAIN QUERY PLAN SELECT * FROM t WHERE b=2;
`--SCAN t


-- after ANALYZE
sqlite> EXPLAIN QUERY PLAN SELECT * FROM t WHERE b=2;
`--SEARCH t USING INDEX abc (ANY(a) AND b=?)
```


`ANY(a)` is the **skip-scan**. The reasoning: `a` has only 100 distinct values across 10,000 rows. So
instead of giving up, enumerate the distinct values of `a` and do a proper two-column seek within each:


```
for each distinct value v of a:          -- 100 iterations, found by seeking past each block
    seek (a = v AND b = 2)               -- ~3 reads each
```


That's ~300 page reads instead of a 2,400-page scan. The gap rule isn't violated — SQLite
**synthesizes the missing leading equality** by iterating over it.


And it only works if you know `a` has few distinct values. With 10,000 distinct values of `a` this plan
would be 10,000 seeks and vastly worse than the scan. **That number lives in `sqlite_stat1`** — it's
literally the `100` in the stored string `10000 100 10 2` — which is why the documentation says a
skip-scan is never chosen on a database that has not been analyzed.


> **This is the deepest point in the chapter, so it's worth stating on its own.** Statistics are
> usually described as helping the planner *rank* plans. Here they do something stronger: they make a
> plan **available** that was not in the candidate set before. Without stats the planner cannot even
> propose the skip-scan, because proposing it requires an estimate to justify it. **Statistics don't
> just re-order the menu; they add dishes to it.**


A second observation from the same experiment, which surprised me:


```
-- no stats
WHERE a>1 AND b=2   →  SEARCH t USING INDEX abc (a>?)          -- range on a, b unused
-- after ANALYZE
WHERE a>1 AND b=2   →  SEARCH t USING INDEX abc (ANY(a) AND b=?)  -- skip-scan chosen instead
```


The planner *abandoned* a legitimate range scan in favour of 100 small seeks, because the cost model
judged that constraining `b` too was worth more than the contiguity of the range. That is a genuinely
non-obvious call, and it's the sort of thing only a cost model makes.


> ⚠️ **Two honesty notes.** The "~18 or more duplicates" profitability threshold for skip-scan is from
> the documentation; I confirmed that skip-scan is statistics-gated but did not bisect the exact
> number. And `WHERE a=1 AND c=3` did **not** become a skip-scan over the middle column even with
> statistics present — it stayed at `(a=?)`. Skip-scan filling a *leading* gap is what I observed;
> don't generalize it to interior gaps.


**quilldb does not implement skip-scan**, and shouldn't. It's here because it's the cleanest available
demonstration of what statistics are *for*, and because "an index on `(a,b,c)` is useless for
`WHERE b=?`" is the kind of confident simplification that a well-read interviewer will correct.


---


## 12.5 Rule-based vs cost-based, and proof that SQLite is the latter


Failure 3 from §12.1: two indexes, both offering an equality seek, 500× apart in how much they read.
A rule can't choose between them, because a rule only knows *shapes* and these have the same shape. You
need to know something about the **data**.


### Proving it, by lying to the planner


`c(x, y)` with indexes `cx(x)` and `cy(y)`, 10,000 rows, `x = i % 2` (2 distinct values) and
`y = i % 1000` (1,000 distinct values). After `ANALYZE`:


```
sqlite_stat1:  cy | 10000 10        -- y: ~10 rows per value.  Selective.
               cx | 10000 5000      -- x: ~5000 rows per value. Useless.


EXPLAIN QUERY PLAN SELECT * FROM c WHERE x=1 AND y=5;
`--SEARCH c USING INDEX cy (y=?)            ← picks the selective one
```


Now overwrite the statistics with lies, changing nothing else:


```sql
UPDATE sqlite_stat1 SET stat='10000 1'    WHERE idx='cx';
UPDATE sqlite_stat1 SET stat='10000 9000' WHERE idx='cy';
```


```
EXPLAIN QUERY PLAN SELECT * FROM c WHERE x=1 AND y=5;
`--SEARCH c USING INDEX cx (x=?)            ← flips, on numbers alone
```


Identical data, identical query, identical indexes, identical SQLite. **Only the estimates changed, and
the decision changed with them.** That is what "cost-based" means, demonstrated rather than asserted —
and it's a nice trick to know, because deliberately corrupting statistics is a legitimate way to
diagnose a planner in production.


### What's actually stored


```
CREATE TABLE sqlite_stat1(tbl, idx, stat)
```


Three columns, all typeless. For a K-column index the `stat` string holds **K+1** space-separated
integers. Decoding `10000 100 10 2` for the index on `(a,b,c)`, against data where `a` had 100 distinct
values, `(a,b)` had 1,000, and `(a,b,c)` had ~5,000:


| Position | Value | Meaning | Check |
|---|---|---|---|
| 1 | 10000 | rows in the index | 10,000 rows ✓ |
| 2 | 100 | avg rows sharing the same `(a)` | 10000 / 100 distinct = 100 ✓ |
| 3 | 10 | avg rows sharing the same `(a,b)` | 10000 / 1000 = 10 ✓ |
| 4 | 2 | avg rows sharing the same `(a,b,c)` | 10000 / ~5000 ≈ 2 ✓ |


Which matches the format spec exactly:


> "The first integer in this list is the approximate number of rows in the index… The N-th integer (for
> N>1) is the estimated average number of rows in the index which have the same value for the first N-1
> columns. **For a K-column index, there will be K+1 integers in the stat column. If the index is
> unique, then the last integer will be 1.**"


Three edge cases, all confirmed:


```
noidx    | NULL | 500        -- idx IS NULL: stat is one integer, the table's row count
withidx  | wp   | 500 1      -- UNIQUE index: last integer is 1, exactly as documented
w        | w    | 500 1      -- WITHOUT ROWID: idx equals tbl, since the table IS the index
```


Notice there is **no entry for the trailing rowid** — a 3-column index yields 4 integers, not 5.
Statistics describe the *declared* key columns; the rowid's contribution is known to always make the
key unique, so measuring it would be measuring a constant.


Also notice how *little* this is. Four integers per index. No histograms, no value distributions, no
correlation between columns. SQLite deliberately stores an average and nothing else — enough to
distinguish "10 rows per value" from "5,000," which is the decision that actually matters, and cheap
enough that `ANALYZE` is fast and the numbers fit in the schema.


### How the cost gets used


Costs in `where.c` are stored as `LogEst` — **logarithmic** estimates, which is why they're small
integers and why adding them multiplies the underlying quantities. The planner keeps a candidate set of
loops with `rRun` (cost to run) and `nOut` (estimated rows out) and prunes dominated ones:


```c
LogEst rRun,           /* Run-cost of the new entry */
LogEst nOut            /* Number of outputs for the new entry */
...
if( rRun<=p->rRun && (prereq & p->prereq)==prereq ){
```


And the search itself, from SQLite's own write-up of the design:


> "The problem of finding the best query plan is equivalent to finding a **minimum-cost path** through
> the graph." · "The 'cost' here is logarithmic."


Since version 3.8.0 the search is the "next generation query planner," using **N Nearest Neighbors**:
*"the algorithm keeps track of the N best paths at each step for some small integer N."* The previous
planner kept only one — a pure greedy walk — which is fast but can be led astray by a locally cheap
first join.


> ⚠️ **Citation trap.** `arch.html` is the natural place to look and it does **not** say "cost-based" —
> it calls the planner *"an AI that strives to select the best algorithm from these millions of
> choices."* Evocative, unhelpful as a citation. The page you want is `queryplanner-ng.html`.


### Why quilldb is rule-based, and how to say so


You are building a **rule-based** planner: apply the leading-column rule, pick an `IndexScan` if a
sargable term matches an index prefix, else `SeqScan`. No statistics, no cost model.


That's the right call for three reasons, and they're worth having in this order:


1. **Statistics are a second consistency problem.** `sqlite_stat1` is *derived data that goes stale* —
   the same category of problem as an index disagreeing with its table (chapter 11 §11.9), except that
   stale statistics don't corrupt anything, they just quietly produce bad plans. That's arguably worse
   to debug. You already have one derived-data problem in week 4; a second one is not the best use of
   two hours.
2. **A rule-based planner is deterministic, and therefore testable.** `EXPLAIN` output is an exact
   assertion. Cost-based plans flip on data distribution, which makes tests either fragile or vacuous.
3. **The leading-column rule captures most of the available win.** The difference between `SeqScan` and
   `IndexScan` is 340×. The difference between the *right* index and the *wrong* index is maybe 500×
   in a constructed case and usually much less. You are buying the first factor, which is the one that
   turns an unusable query into a fast one.


The interview version, which concedes the gap rather than hiding it:


> *"My planner is rule-based: I check whether a `WHERE` conjunct is sargable against an index prefix and
> emit an `IndexScan` if so. SQLite is cost-based — it keeps average-rows-per-value figures in
> `sqlite_stat1` from `ANALYZE` and searches for a minimum-cost join order with logarithmic cost
> estimates. What I give up is choosing between two applicable indexes, and I can show you the case
> where that costs 500× — I just decided that maintaining derived statistics was a second staleness
> problem I didn't want alongside index maintenance. The thing I found most interesting is that
> statistics don't only rank plans, they enable plans: SQLite's skip-scan literally cannot be proposed
> without them."*


---


## 12.6 What you're building


```python
@dataclass
class AccessPath:
    kind: Literal["seq_scan", "index_scan"]
    index: Index | None
    seek_terms: list[Predicate]      # the prefix of terms the index will seek on
    residual: list[Predicate]        # everything else — becomes a Filter above
    est_rows: int                    # for EXPLAIN, even if crudely guessed




def choose_access_path(table: TableSchema, predicates: list[Predicate]) -> AccessPath: ...
```


The `seek_terms` / `residual` split **is** the algorithm. For each candidate index, walk its columns
left to right accumulating sargable equality terms; allow inequalities on the first column where you
stop; stop at the first column with no term. Everything not consumed goes to `residual`. Score
candidates by how many columns they consumed, longest wins, ties broken deterministically (by index
name, so tests don't flake).


**Three traps:**


1. **`residual` must never be dropped.** Consuming `a=1` from `WHERE a=1 AND c=3` and forgetting to
   filter on `c` returns wrong rows — and it's the kind of bug that only shows up in data where the
   index prefix isn't already selective enough to hide it. Your differential tests catch this; make
   sure they include multi-conjunct predicates.
2. **`SeqScan` must remain reachable.** A planner that always finds an index is a planner with a bug.
   Assert the negative cases: `WHERE c=3`, `WHERE lower(x)=?`, `WHERE a=1 OR b=2` all produce
   `SeqScan`.
3. **Don't let the planner change results, ever.** The roadmap's week-4 property test is the guard:
   run every query with and without each index and assert identical output. A planner is an
   *optimization*, and an optimization that changes answers is just a bug with a nice name.


`EXPLAIN` prints the operator tree. Note that quilldb's `EXPLAIN` corresponds to SQLite's `EXPLAIN
QUERY PLAN`, not its `EXPLAIN` — the latter dumps VDBE bytecode, which you don't have (chapter 09).
Worth one line in the README so nobody expects bytecode.


```
Project  [id, email, age]
└─ IndexScan  ix_email  (email = 'a@b.c')   est_rows=1  pages_read=3
```


**Print `pages_read` from the actual counter, not an estimate.** A number measured by the buffer pool is
evidence; an estimate is a claim. That one field turns `EXPLAIN` from a debugging aid into the
before/after artifact the README needs (chapter 19).


---


## 12.7 Roads not taken


### System R dynamic programming (the classic answer)


For join ordering, enumerate every subset of tables bottom-up, keeping the cheapest plan for each
subset plus the cheapest "interesting order" — then build up. Guarantees the optimal left-deep order
under the cost model.


**Used by:** PostgreSQL (for small joins), DB2, and essentially every textbook. **Why not:** it's
O(2ⁿ) in the number of tables, which is fine at 5 and impossible at 20 — and quilldb's week 7 joins
are two or three tables where the search space is trivial. Worth knowing as *the* canonical answer,
and worth knowing that SQLite deliberately chose a polynomial heuristic (N3) instead, because it must
run on a phone with a fixed memory budget.


### Cascades / Volcano optimizer


Express optimization as rules that transform plan fragments, applied by a search engine with
memoization. **Used by:** SQL Server, CockroachDB, Calcite, DuckDB. **Why it's attractive:**
extensible — new optimizations are new rules, not new special cases in a growing function. **Why not:**
it's a framework, and frameworks cost more than the two optimizations you currently have.


### Genetic / randomized join ordering


Postgres's GEQO, used past a threshold number of tables: random plans, mutate, keep the fit ones.
**Why it exists:** when exhaustive search is impossible, a good-enough plan found quickly beats an
optimal plan found never. **The interesting property:** it's non-deterministic — the same query can get
different plans on different runs, which is exactly the testability objection from §12.5 in production
form.


### Runtime adaptivity


Start executing, notice the estimate was wrong, change plan mid-flight. **Used by:** Spark's adaptive
query execution, SQL Server's adaptive joins, Oracle. **Why it's the future-facing answer:** it attacks
the actual weak point, which is that estimates are guesses that compound multiplicatively across
joins. **Why not here:** it requires the executor to be re-plannable mid-stream, which fights the
simple pull-based iterator model of chapter 09 — and it's the kind of thing worth mentioning as *"if I
kept going, the honest next step is that my estimates are unvalidated, and I'd rather measure than
estimate harder."*


### Hints


Let the user write `INDEXED BY ix` and skip the decision. SQLite supports this and its documentation
actively discourages it. **Why it's a bad default:** a hint is a decision frozen at a moment when the
data looked a certain way, and it will still be there after the data changes. Useful as an escape
hatch, terrible as a strategy.


---


## 12.8 Check yourself


1. Same query, two plans, 340× apart. What decides, and why isn't the choice in the SQL?
2. Define sargable in one sentence.
3. Why can't an index on `email` serve `WHERE lower(email) = ?` — and why is that not a fixable
   implementation gap?
4. Rewrite these to be sargable: `WHERE age + 0 = 30`, `WHERE strftime('%Y', ts) = '2026'`,
   `WHERE substr(name,1,3) = 'ada'`.
5. Why is `AND` friendly to a planner while `OR` isn't? Answer in terms of what happens when you drop
   a term.
6. Use the phone book to explain why an index on `(a,b,c)` can't seek on `b` alone.
7. State the leading-column rule, including what happens after the first inequality.
8. `WHERE a=1 AND c=3` on an index over `(a,b,c)`. What does SQLite seek on, what happens to `c`, and
   why is that still correct?
9. Draw the index entries and show concretely why a gap prevents a seek.
10. What is a skip-scan, when is it profitable, and why can't it be used on an un-analyzed database?
11. What's the difference between statistics *ranking* plans and statistics *enabling* plans?
12. How would you prove a planner is cost-based rather than rule-based, using only SQL?
13. `sqlite_stat1` holds `10000 100 10 2` for an index on `(a,b,c)`. What is each number? Why are there
    four and not five?
14. Why does SQLite store only averages rather than histograms?
15. Give three reasons quilldb's planner is rule-based, and name what it gives up.
16. Your planner consumes `a=1` and forgets the residual `c=3`. What's the symptom, and which test
    catches it?
17. Why should `EXPLAIN` print measured `pages_read` rather than an estimate?


---


## 12.9 Sources


- [The SQLite Query Optimizer Overview](https://www.sqlite.org/optoverview.html) §2 — the full list of
  index-usable term forms; "The initial columns of the index must be used with the = or IN or IS
  operators. The right-most column that is used can employ inequalities."; "there cannot be gaps in the
  columns of the index that are used."; the `OR`-connected-terms exclusion; the worked `ex1(a,b,c,…,z)`
  examples; the skip-scan section and its statistics requirement.
- [The Next-Generation Query Planner](https://www.sqlite.org/queryplanner-ng.html) — "The problem of
  finding the best query plan is equivalent to finding a minimum-cost path through the graph."; "The
  'cost' here is logarithmic."; NGQP and the N-Nearest-Neighbors (N3) search keeping "the N best paths
  at each step," versus the legacy single-path NN heuristic. **This is the citation for "cost-based" —
  `arch.html` doesn't use the term.**
- [SQLite Database File Format](https://www.sqlite.org/fileformat2.html) §2.6.4 — the `sqlite_stat1`
  schema and the K+1-integers format, including "If the index is unique, then the last integer will be
  1."
- [`where.c`](https://github.com/sqlite/sqlite/blob/master/src/where.c) — `LogEst rRun` / `nOut` and
  the dominance-pruning comparison. The planner spans `where.c`, `whereexpr.c` and `wherecode.c`,
  ~13,000 lines together.
- **Measurements in §12.3, §12.4 and §12.5** are verbatim `EXPLAIN QUERY PLAN` output from `sqlite3`
  3.37.2 (CLI) and 3.50.4 (via Python), on 10,000-row tables with known value distributions. The
  statistics-injection experiment in §12.5 is reproducible in four SQL statements. Two things I could
  *not* verify: skip-scan's exact "~18 duplicates" threshold (documented, not bisected), and the
  optional `unordered` / `sz=NNN` / `noskipscan` tokens in `stat` — `ANALYZE` never emitted them.


---


**Next:** [13 — Atomic commit and the journal](../txn/13-atomic-commit-and-the-journal.md) — why the
commit point is *deleting a file*, and the exact order of writes that makes a crash survivable.