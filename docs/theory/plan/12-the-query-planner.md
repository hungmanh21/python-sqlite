# 12 — The Query Planner


> **Read during week 4, before you write `plan/planner.py`.** Covers access-path selection and
> `EXPLAIN`.
>
> **Time:** ~60 minutes. **Prerequisites:** [chapter 09](../exec/09-iterator-execution.md) (operators),
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


Before unpacking them, separate the three questions a planner must answer:


1. **Can this condition be translated into an index lookup at all?**
2. **If it is a composite index, can the lookup start at the columns supplied by the query?**
3. **If several indexes are usable, which one will do the least work?**


The naive rule asks none of these. It only notices that a column name occurs both in the query and in
an index definition.


### Failure 1: the column is in a composite index, but in the wrong position


```sql
CREATE INDEX abc ON t(a, b, c);
SELECT * FROM t WHERE c = 3;
```


It is tempting to say "`c` is indexed," but that is imprecise. The index is sorted by the complete
tuple `(a, b, c)`: first by `a`, then by `b` among rows with the same `a`, then by `c` among rows with
the same `(a,b)`.


Imagine these index entries:


```
(a=1, b=1, c=3)   ← match
(a=1, b=1, c=8)
(a=1, b=2, c=3)   ← match
(a=2, b=1, c=3)   ← match
(a=2, b=1, c=9)
```


The `c=3` rows are scattered among many different `(a,b)` groups. There is no single place in the
B-tree where "the `c=3` block" begins, because no such block exists. To make a normal seek key, the
planner would first need values for `a` and `b`, and the query supplies neither.


There are two importantly different outcomes:


- It is **correct but usually not useful** to scan the entire index and check `c` on every entry. That
  skips no work compared with a table scan (though a covering index can change the exact cost).
- It is **incorrect** to pretend that `c` is the first search key and jump to one part of the tree.
  That can miss matching rows under other `(a,b)` prefixes and return an incomplete answer.


So "a column occurs somewhere in an index" does not mean "the database can seek by that column." This
is the **leading-column rule**, explained in §12.3. §12.4 then shows skip-scan, a statistics-dependent
exception that handles some missing *leading* columns by performing many smaller seeks.


### Failure 2: the query asks about a transformed value, but the index stores the original value


```sql
CREATE INDEX email_idx ON users(email);
SELECT * FROM users WHERE lower(email) = 'ada@x.com';
```


The index contains keys such as `Ada@X.com`, sorted as original `email` values. The query is not asking
"is `email` equal to this value?" It is asking "after running `lower` on `email`, is the result equal
to this value?"


An ordinary index on `email` has not stored those lowercase results, and their order need not match
the order of the original strings. The engine therefore cannot navigate directly to all matching
rows. It normally has to read rows, compute `lower(email)` for each one, and test the result.


The column name appearing inside a function does not make the condition index-searchable. To support
this lookup, use an expression index on `lower(email)` (if the database supports it), store a
normalized email in a separate indexed column, or use a suitable case-insensitive collation and query
shape.


This is a **sargability** failure: the condition is not shaped like a search argument for the index
that actually exists. §12.2 explains it in detail.


### Failure 3: both indexes are valid, but one is much more selective


```sql
CREATE INDEX x_idx ON t(x);
CREATE INDEX y_idx ON t(y);
SELECT * FROM t WHERE x = 1 AND y = 5;
```


Suppose the table has 10,000 rows:


- `x` has only 2 distinct values. On average, `x=1` finds about 5,000 candidate rows.
- `y` has 1,000 distinct values. On average, `y=5` finds about 10 candidate rows.


Both plans are logically correct:


```
seek x_idx for x=1 → read about 5,000 rows → filter y=5
seek y_idx for y=5 → read about    10 rows → filter x=1
```


The first plan may inspect roughly 500 times as many rows. A "take the first matching index" rule can
choose it merely because `x=1` appeared first in the SQL or `x_idx` appeared first in a schema list.
That is arbitrary: SQL condition order is not a cost estimate.


Unlike failures 1 and 2, this is not about whether an index *can* be used. Both can. It is about which
usable index is cheaper. The useful measure is **selectivity**: a highly selective condition keeps a
small fraction of the table. A cost-based planner uses statistics about the data to estimate that
fraction. §12.5 explains this choice and shows that changing only SQLite's statistics can flip the
selected index.


### Keep the three failures separate


| Failure                               | Question the planner failed to ask                                | Result                                                        |
| ------------------------------------- | ----------------------------------------------------------------- | ------------------------------------------------------------- |
| `c=3` with index `(a,b,c)`            | Can this column position form a usable index prefix?              | A normal seek is unavailable; a fake one can miss rows.       |
| `lower(email)=?` with index `(email)` | Does the existing index store and order the value being compared? | The ordinary index cannot seek for this expression.           |
| `x=1 AND y=5` with two usable indexes | Which valid access path is estimated to read fewer rows/pages?    | Either answer is correct, but one can be dramatically slower. |


That gives the map for the rest of the chapter: **§12.2 covers sargability (failure 2), §12.3 covers
the leading-column rule (failure 1), §12.4 covers skip-scan as a special exception to failure 1, and
§12.5 covers selectivity and cost-based choice (failure 3).**


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


| Not sargable                        | Why                                         | Sargable rewrite                                        |
| ----------------------------------- | ------------------------------------------- | ------------------------------------------------------- |
| `WHERE lower(email) = ?`            | index sorted by `email`, not `lower(email)` | index on `lower(email)`, or store a normalized column   |
| `WHERE age + 0 = 30`                | the index stores `age`, not `age + 0`       | `WHERE age = 30`                                        |
| `WHERE substr(name,1,3) = 'ada'`    | prefix *function*, not prefix *comparison*  | `WHERE name LIKE 'ada%'`                                |
| `WHERE strftime('%Y', ts) = '2026'` | scrambles order                             | `WHERE ts >= '2026-01-01' AND ts < '2027-01-01'`        |
| `WHERE a = 1 OR b = 2`              | see below                                   | two index scans unioned, or an index on the right thing |


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


| Query                        | Plan                             | What happened                                 |
| ---------------------------- | -------------------------------- | --------------------------------------------- |
| `WHERE a=1`                  | `SEARCH … (a=?)`                 | prefix of 1                                   |
| `WHERE a=1 AND b=2`          | `SEARCH … (a=? AND b=?)`         | prefix of 2                                   |
| `WHERE a=1 AND b=2 AND c=3`  | `SEARCH … (a=? AND b=? AND c=?)` | whole index                                   |
| `WHERE a IN (1,2,3) AND b=2` | `SEARCH … (a=? AND b=?)`         | `IN` counts as equality                       |
| `WHERE a IS NULL AND b=2`    | `SEARCH … (a=? AND b=?)`         | so does `IS`                                  |
| `WHERE a=1 AND b>2 AND b<5`  | `SEARCH … (a=? AND b>? AND b<?)` | two sandwiching inequalities                  |
| `WHERE a=1 AND b>2 AND c=3`  | `SEARCH … (a=? AND b>?)`         | **`c` dropped** — it's right of an inequality |
| `WHERE a=1 AND c=3`          | `SEARCH … (a=?)`                 | **`c` dropped** — gap at `b`                  |
| `WHERE c=3`                  | `SCAN t`                         | no usable prefix at all                       |


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


Section 12.3 ended on the phone-book case that looked hopeless: `WHERE b=2` against an index on
`(a, b, c)`, with no constraint on `a`. The `b=2` entries are scattered — one small cluster inside
*every* distinct `a` value — so there's no contiguous block to seek to, and the conclusion was "read
everything." That conclusion is only *usually* right. Here is the same query, on the same file, twice
— the only difference being whether `ANALYZE` has run:


```
-- no sqlite_stat1 table
sqlite> EXPLAIN QUERY PLAN SELECT * FROM t WHERE b=2;
`--SCAN t


-- after ANALYZE
sqlite> EXPLAIN QUERY PLAN SELECT * FROM t WHERE b=2;
`--SEARCH t USING INDEX abc (ANY(a) AND b=?)
```


### The mechanism

`ANY(a)` is the **skip-scan**. The reasoning: `a` has only 100 distinct values across 10,000 rows (so
~100 rows per value). Instead of giving up, enumerate the distinct values of `a` — which the index
already lets you find cheaply, since they're each the start of a contiguous block — and do a proper
two-column seek within each one:


```
for each distinct value v of a:          -- 100 iterations, found by seeking past each block
    seek (a = v AND b = 2)               -- ~3 reads each
```


That's ~300 page reads instead of a 2,400-page scan. The leading-column rule from §12.3 isn't
violated — SQLite **synthesizes the missing leading equality** by iterating over every value it could
take, rather than requiring the query to supply one.

> **ELI5.** Back to the phone book sorted by `(last_name, first_name)`, and you're looking for anyone
> named "Anh" with no last name given. Reading every page is slow. But suppose you happen to know this
> town only has 100 distinct last names. Then you can flip to the start of each of the 100 last-name
> sections (100 quick flips) and, once you're inside a section, jump straight to "Anh" the normal fast
> way, because *within* a last name the book is sorted by first name too. 100 quick flips plus 100 fast
> in-section jumps beats reading the whole book — but only because there happen to be just 100
> sections. If this were a big city with 10,000 last names, flipping to each one would cost more than
> just reading straight through, and you'd be worse off than the plain scan.


### Why it needs `ANALYZE` — the deepest point in the chapter

The skip-scan only wins if `a` has *few* distinct values. With 10,000 distinct values of `a` instead of
100, the same plan becomes 10,000 seeks — vastly worse than the scan it was supposed to beat. So
whether this plan is even a good idea depends entirely on a number the planner doesn't get for free:
**how many distinct values does `a` have?**

That number lives in `sqlite_stat1` — it's literally the `100` in the stored string `10000 100 10 2`
(one row per column: total rows, then distinct-value counts for `a`, `a,b`, and `a,b,c` respectively).
Without that row, the planner has no basis for the estimate, which is why the documentation says a
skip-scan is never chosen on a database that has not been analyzed.

> Statistics are usually described as helping the planner *rank* plans it already knows about — pick
> the cheaper of two candidates. Here they do something stronger: they make a plan **available** that
> was not in the candidate set at all. Without stats the planner cannot even *propose* the skip-scan,
> because proposing it requires an estimate to justify it — you can't compare "100 seeks" against "one
> scan" without knowing it's 100 and not 10,000. **Statistics don't just re-order the menu; they add
> dishes to it.**
>
> ELI5 version of the same point: it's not that the librarian is bad at choosing a strategy without
> knowing the last-name count — it's that *without that count, the flip-through-each-section idea isn't
> a strategy they can even consider.* Its usefulness is conditional on a fact they don't have yet, so
> the idea itself doesn't exist for them until they have it.


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


**quilldb defers skip-scan.** It is here because it is the cleanest demonstration of statistics making
a candidate available, and because "an index on `(a,b,c)` is useless for `WHERE b=?`" is the kind of
confident simplification that a well-read interviewer will correct. A planner can be genuinely
cost-based without implementing every access path SQLite knows.

> ELI5 version of the same point: this section is here to teach a *concept* — that statistics can make
> a whole new plan possible, not just re-rank plans you already had — and skip-scan happens to be the
> clearest example of that concept in the real SQLite docs. Building a toy engine to prove you
> understand combustion doesn't require also bolting on a turbocharger; the lesson doesn't need the
> extra part. The second reason is more defensive: it's on record that skip-scan was understood (its
> statistics-gating, its ~18-duplicate threshold, its leading-gap-only behavior) and deliberately not
> built, rather than missed. "We didn't implement it" and "we didn't know about it" read very
> differently to a reviewer.


---


## 12.5 The cost-based pipeline, and proof that SQLite uses it


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
ANALYZE sqlite_schema;  -- reload the edited statistics into this connection
```


```
EXPLAIN QUERY PLAN SELECT * FROM c WHERE x=1 AND y=5;
`--SEARCH c USING INDEX cx (x=?)            ← flips, on numbers alone
```


Identical data, identical query, identical indexes, identical SQLite. **Only the estimates changed, and
the decision changed with them.** That is what "cost-based" means, demonstrated rather than asserted —
and controlled statistics injection is a useful diagnostic technique on a test copy. Directly editing
production statistics is risky because every query sharing them may be replanned.

> ELI5: picture two filing cabinets for finding a person — one sorted by eye color (only 2 possible
> values, so "brown eyes" still leaves thousands of matches), one sorted by exact street address (1,000
> possible values, so it narrows you to ~10 people). Asked to find "the brown-eyed person on Elm St,"
> you obviously check the street-address cabinet first — it's the one statistics say is useful. The
> experiment then secretly swaps the labels on the cabinets without moving a single folder, and SQLite
> starts checking the *other* cabinet first. Nothing about the actual people changed — only the label
> describing how useful each cabinet is — and the choice flipped anyway. That's the whole proof in one
> picture.


### What's actually stored


```
CREATE TABLE sqlite_stat1(tbl, idx, stat)
```


Three columns, all typeless. For a K-column index the `stat` string holds **K+1** space-separated
integers. Decoding `10000 100 10 2` for the index on `(a,b,c)`, against data where `a` had 100 distinct
values, `(a,b)` had 1,000, and `(a,b,c)` had ~5,000:


| Position | Value | Meaning                             | Check                        |
| -------- | ----- | ----------------------------------- | ---------------------------- |
| 1        | 10000 | rows in the index                   | 10,000 rows ✓                |
| 2        | 100   | avg rows sharing the same `(a)`     | 10000 / 100 distinct = 100 ✓ |
| 3        | 10    | avg rows sharing the same `(a,b)`   | 10000 / 1000 = 10 ✓          |
| 4        | 2     | avg rows sharing the same `(a,b,c)` | 10000 / ~5000 ≈ 2 ✓          |


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

> ELI5: these are "edge cases" because each one quietly breaks a different assumption hiding inside
> the one-sentence format spec quoted just above. `noidx` breaks "there's always a real index with K
> key columns to count" — this row describes the bare table, not an index, so the K+1 formula
> degenerates to a single number. `withidx` breaks "the trailing integer is always *measured*" — for a
> UNIQUE index there can never be a duplicate key by definition, so SQLite doesn't measure that last
> number, it just asserts `1`. `w` breaks "an index name and a table name are always different
> strings" — a WITHOUT ROWID table has no separate rowid B-tree for its index to sit next to; the
> table's own B-tree *is* the index, so `idx` and `tbl` collapse to the same name. Each row is the
> smallest example that forces the implicit assumption into view.


Notice there is **no entry for the trailing rowid** — a 3-column index yields 4 integers, not 5.
Statistics describe the *declared* key columns; the rowid's contribution is known to always make the
key unique, so measuring it would be measuring a constant.

> ELI5: `sqlite_stat1` is a cheat-sheet, not a photograph. For an index on `(a,b,c)` it stores just 4
> numbers — total rows, then "on average, how many rows share the same `a`," then same for `(a,b)`,
> then `(a,b,c)` — like summarizing a school roster as "10,000 students; ~100 share a first name; ~10
> share a first+last name; ~2 share first+last+middle." No rowid entry exists because adding the rowid
> to any key always drops the group size to exactly 1 — that's always true by definition, so writing it
> down would tell you nothing you didn't already know.


Also notice how *little* `stat1` is. Four integers per index. No histograms, no value distributions,
and no correlation beyond the declared index prefixes. That is enough to distinguish "10 rows per
value" from "5,000," and cheap enough that `ANALYZE` is fast. SQLite builds compiled with
`SQLITE_ENABLE_STAT4` may additionally store sampled key distributions in `sqlite_stat4`; quilldb
deliberately stops at the simpler `stat1` model.


### How the cost gets used


SQLite represents a candidate way to access one table as a `WhereLoop`. A loop records prerequisites
(which outer-table values must already exist), one-time setup cost, run cost, estimated output rows,
and useful ordering. `WherePath` candidates combine loops into join orders.


Costs in `where.c` are stored as `LogEst` — **logarithmic** estimates, which is why they're small
integers and why adding them corresponds to multiplying underlying quantities. The planner keeps a
candidate set with `rSetup`, `rRun`, and `nOut`, then prunes dominated choices:


```c
LogEst rSetup,         /* One-time setup cost */
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

> ELI5: `LogEst` stores costs as "how many zeroes," not the raw number — like writing "5" to mean
> 100,000 instead of writing out all six digits. The payoff: when two costs *multiply* (which is what
> happens when a join nests loops — "for each of these 100 rows, do that other thing 5,000 times" =
> 500,000), their logarithms just *add*. Small integers, cheap addition, same ranking as if you'd
> multiplied the big numbers. And "N Nearest Neighbors" is the search strategy on top of those costs:
> instead of committing to whichever single next step looks cheapest right now (which can walk you into
> a dead end — a cheap-looking first move that traps you in an expensive overall plan), it keeps the
> top N candidate partial plans alive at each step, the way a chess player keeps a few good lines open
> a few moves ahead instead of committing to the first plausible move.


> ⚠️ **Citation trap.** `arch.html` is the natural place to look and it does **not** say "cost-based" —
> it calls the planner *"an AI that strives to select the best algorithm from these millions of
> choices."* Evocative, unhelpful as a citation. The page you want is `queryplanner-ng.html`.


### Rules and costs are different stages, not competing philosophies


Even a cost-based planner needs rules. Sargability and the leading-column rule answer **which plans are
legal**. The cost model answers **which legal plan should win**.


For `WHERE x=1 AND y=5`, candidate generation might produce:


```
SeqScan(t) + Filter(x=1 AND y=5)
IndexScan(cx, x=1) + Filter(y=5)
IndexScan(cy, y=5) + Filter(x=1)
```


It must not produce `IndexScan(abc, c=3)` as an ordinary seek when the leading columns are missing.
No low cost can make an illegal seek correct. Conversely, generating `IndexScan(cx)` does not mean it
should win: if `x=1` fetches half the table, one sequential pass may be cheaper than thousands of
rowid lookups.


That gives the pipeline worth remembering — an assembly line where each stage narrows what the next
stage even sees: first throw out anything *illegal*, then guess row counts for what's left, then turn
those counts into a cost, then search for the cheapest surviving path:


```text
SQL predicates
    ↓ legality rules
candidate access paths
    ↓ cardinality estimates from statistics
estimated rows at each operator
    ↓ I/O + CPU cost model
costed candidates
    ↓ plan search
cheapest complete legal plan
```


Calling a planner cost-based requires this separation. Choosing the longest index prefix and printing
a number beside it is still a rule-based choice.


### How SQLite actually builds that candidate list

Everything above described candidate generation from the outside: "some process produces
`SeqScan(t) + Filter(...)`, `IndexScan(cx, x=1) + Filter(...)`, etc." The mechanism inside `where.c`
that actually produces those lines is worth naming, because it explains a structural choice quilldb
deliberately didn't copy.

Each candidate is a `WhereLoop` — one access path for one table. `whereLoopAddBtree()` runs once per
table in the FROM list and, for every index on that table (plus an implicit loop for a plain rowid
scan), calls `whereLoopAddBtreeIndex()`. That function walks the index's columns left to right — the
same leading-column walk as §12.3 — but with two differences from "just return the longest legal
prefix":

1. **Every prefix length becomes its own candidate**, not only the longest one. Matching column `a`
   alone is one `WhereLoop`; matching `a` and `b` is a second, independent `WhereLoop`; and so on.
   SQLite doesn't assume longer is always cheaper — a two-column equality prefix can lose once join
   order enters the picture, because a shorter prefix can leave a later column of the *same* index free
   to serve as an ordering key for a downstream join, something a single-table view of the index can't
   see.
2. **Pruning happens during generation, not after.** Each new `WhereLoop` is handed to
   `whereLoopInsert()` immediately, which runs the exact dominance check already quoted above in "How
   the cost gets used": `if( rRun<=p->rRun && (prereq & p->prereq)==prereq )`. A candidate whose
   run-cost is no better than one already kept, under the same or looser prerequisites, is discarded on
   the spot. There is no separate "now that we have every candidate, go cost and prune them" phase —
   costing and pruning are interleaved with generation, one column-extension at a time.

A third path is worth naming even though it's out of scope for quilldb: `whereLoopAddOr()` builds a
specialized loop for `WHERE a=1 OR b=2`, treating it as a union of two independent index seeks rather
than one scan — the mechanism behind §12.2's "OR is special" observation.

> ELI5: imagine planning a road trip through a fixed sequence of cities. SQLite doesn't just work out
> "the longest chain of cities I can commit to, in order" and stop there — it writes down *every*
> partial chain ("just city 1," "city 1 then city 2," "city 1 then city 2 then city 3"...) as its own
> candidate route, and throws away a candidate the moment it's worse in every way than one already on
> the list, without waiting until every route is written down first. Generating and comparing happen in
> the same breath, not as two separate passes.


### How quilldb scopes the same architecture


quilldb implements the complete pipeline, but over a deliberately bounded search space:


- Candidate generation (`plan/planner.py`'s `enumerate_access_paths` and `_match_index_prefix`)
  returns exactly one `AccessPath` per index — the single longest legal prefix — not one candidate per
  prefix length the way `whereLoopAddBtreeIndex` does, and generation is a separate stage from costing:
  every path is built first, then `plan/cost.py` prices all of them, then `search.py`'s
  `choose_access_path` takes a plain `min()` over `cost.total`. There is no `whereLoopInsert`-style
  pruning interleaved into generation. This is a legitimate simplification, not a missing feature: with
  no join reordering benefit to a shorter prefix (single-table access paths only, at this stage) and a
  candidate set bounded by "number of indexes on one table," generate-then-cost never grows large enough
  for interleaved pruning to matter — the same reasoning chapter 12.6 gives for exhaustive join search
  being fine at three tables instead of needing SQLite's N³ heuristic.
- SQLite-style `ANALYZE` prefix averages in `quill_stat1`, without `stat4` histograms.
- SeqScan, normal IndexScan, covering IndexScan, nested loop, index nested loop, and sort/no-sort
  alternatives. Skip-scan, OR decomposition, index intersection, and automatic indexes stay deferred.
- Page-read-equivalent costs using B+tree height, estimated leaf pages, table rowid lookups, and a small
  CPU-per-row term. SQLite's exact `LogEst` arithmetic is not copied.
- Exhaustive legal left-deep join search for at most three tables. SQLite needs N3 because it supports
  far larger joins; quilldb has at most `3! = 6` orders, so exhaustive search is simpler and optimal
  under its own cost model.


This is a genuine cost-based optimizer, not a claim of feature parity with SQLite.


The interview version:


> *"My optimizer follows SQLite's candidate–cardinality–cost–search architecture. `ANALYZE` persists
> average rows per index prefix, the planner generates every legal scan path, and a page-oriented cost
> model chooses among them. For joins I exhaustively search legal left-deep orders because the language
> caps joins at three tables; SQLite's N3 heuristic solves a scale problem I intentionally don't have.
> The main estimation limitation is the same one visible in `stat1`: averages cannot model skew or
> correlation, so stale or coarse statistics can make the plan slower, but never change its answer."*


---


## 12.6 What you're building


### Statistics


`ANALYZE` creates `quill_stat1(tbl, idx, stat)`, using the same K+1 integer encoding described above.
Statistics are derived data: writes do not update them row by row, so they become stale until the next
`ANALYZE`. That is deliberate. Updating distinct counts transactionally on every insert would make
ordinary writes more expensive for estimates that do not need to be exact.


Missing or malformed statistics use documented defaults. Planning must never fail just because the
database has not been analyzed.


The trade is explicit:


- `ANALYZE` performs extra read work now so later queries can avoid much more work.
- Statistics stay small and cheap to load, but averages miss skew and correlations.
- Statistics can become stale, so plan quality may degrade as data changes.
- Re-running `ANALYZE` may legitimately change plans, so the compiled-plan cache must be invalidated.
- None of these conditions may affect correctness. Bad statistics choose a slow legal plan, never a
  different answer.


```python
@dataclass(frozen=True)
class PlanCost:
    startup: float
    total: float




@dataclass(frozen=True)
class AccessPath:
    kind: Literal["seq_scan", "index_scan"]
    index: IndexSchema | None
    seek_terms: list[Predicate]      # the prefix of terms the index will seek on
    residual: list[Predicate]        # everything else — becomes a Filter above
    rows_fetched: int                # rows read from the tree, BEFORE residual filtering
    est_rows: int                    # rows emitted, AFTER residual filtering
    cost: PlanCost
    covering: bool
    output_order: tuple[str, ...]    # index columns in physical ascending order




def enumerate_access_paths(table: TableSchema, indexes: list[IndexSchema],
                           predicates: list[Predicate], required_columns: set[str],
                           stats: StatisticsCatalog) -> list[AccessPath]: ...




def choose_access_path(candidates: list[AccessPath]) -> AccessPath: ...
```


### Stage 1: enumerate every legal path


Always emit SeqScan. Then, for each index, walk its columns left to right accumulating sargable
equalities; allow inequalities on the first column where you stop; stop at the first gap. Everything
not consumed goes to `residual`. An applicable index is a candidate, not an automatic winner.


### Stage 2: estimate rows


For index `(a,b,c)` with statistics `10000 100 10 2`:


```
a=?                 → rows_fetched ≈ 100
a=? AND b=?         → rows_fetched ≈ 10
a=? AND b=? AND c=? → rows_fetched ≈ 2
```


`rows_fetched` controls scan cost. Residual predicates may reduce `est_rows` passed to a parent join,
aggregate, or sort, but they cannot retroactively reduce the rows already fetched.


For an equijoin, the small optimizer uses the standard uniform estimate:


```
|R ⋈ S| ≈ |R| × |S| / max(NDV(R.key), NDV(S.key))
```


This is useful and knowingly imperfect. If 90% of rows have `status='open'`, an average for all status
values will badly underestimate that literal. Predicates represented only by separate indexes may also
be highly correlated. `stat1` cannot see either fact unless the relevant joint prefix is represented by
one composite index.


### Stage 3: assign costs


Use transparent page-read-equivalent costs rather than copying SQLite's internal constants. Two
constants and one convention:


```
SEQ_PAGE_COST    = 1.0     -- a page reached by walking forward
RANDOM_PAGE_COST = 4.0     -- a page reached by a fresh root-to-leaf descent
height                     -- root→leaf pages, LEAF INCLUSIVE
```


```
SeqScan   = SEQ_PAGE_COST × table_pages + CPU_PER_ROW × table_rows


IndexScan = RANDOM_PAGE_COST × index_height           -- the seek
          + SEQ_PAGE_COST × (estimated_leaf_pages - 1) -- further leaves, walked forward
          + rows_fetched × (RANDOM_PAGE_COST × table_height)   -- zero when covering
          + CPU_PER_ROW × rows_fetched
```


`table_height` is why this works, and it has to be *stored* — it lives on `TableStats` next to
`row_count` and `page_count`. It's paid once per fetched row, so it's the term that makes a
low-selectivity index lose. Two details that are easy to get wrong and both distort the headline
benchmark: because `height` includes the leaf, the descent already paid for the first leaf page
(hence `- 1`), and `rows_fetched` must be clamped to at least 1, or an index matching nothing costs
zero and beats everything.


`RANDOM_PAGE_COST` must exceed `SEQ_PAGE_COST` or the model is not actually page-oriented — it would be
counting page *touches* while claiming to distinguish random from sequential access. The 4:1 ratio is
conventional (it's PostgreSQL's default) and is yours to calibrate.


The model captures the important tradeoff: a non-covering index may perform one table-tree lookup per
candidate. That makes a low-selectivity index slower than one sequential scan even though the index is
perfectly legal. A covering index can win because it removes those table lookups.


The numbers are estimates, not promises. Keep the constants centralized, calibrate them against measured
page counters, and document them. Do not tune one magic multiplier until a preferred fixture wins.


### Worked example: from SQL to cost

Tie stages 1-3 together against one concrete query, using the same `t(a,b,c)` / index `abc` this
chapter has used throughout.

```sql
CREATE INDEX abc ON t(a, b, c);
SELECT * FROM t WHERE a = 1 AND b = 2 AND c > 3;
```

Stats, decoded from `quill_stat1`:

```
table_stats:  row_count=10000  page_count=100  height=3
abc stats:    "10000 100 10 2" → row_count=10000  rows_per_prefix=(100, 10, 2)  height=3  leaf_pages=100
```

**Stage 1 — legal paths.** The `WHERE` clause splits on `AND` into three sargable predicates: `a=1`,
`b=2`, `c>3`. Walking `abc` left to right: `a` has an equality (fully consumed, keep going — equality
pins a single point, so `b` is still contiguous within it); `b` has an equality (same); `c` has only an
inequality (consumed as the sandwich bound, then the walk stops — nothing is right of it anyway, since
`c` is the last column). No gap anywhere, so the entire `WHERE` clause got absorbed into the seek:

```
seq_scan:            seek_terms=()                 residual=(a=1, b=2, c>3)
index_scan (abc):    seek_terms=(a=1, b=2, c>3)     residual=()
```

**Stage 2 — row estimates.**

```
seq_scan:   rows_fetched = 10000                  (the whole table; nothing was sought)
            est_rows     = 10000 * (1/3)^3 ≈ 370    (3 residual predicates, 1/3 selectivity each)

index_scan: rows_fetched = rows_per_prefix[3-1] = rows_per_prefix[2] = 2   (3 distinct columns touched)
            est_rows     = 2                         (nothing left to filter further)
```

One honest caveat: that `2` technically means "rows sharing one *exact* `(a,b,c)` value," but `c>3` is a
range, not an equality. Row-count lookup only counts how many distinct *columns* got touched by the
seek, not whether each one was pinned by `=` or merely bounded by `<`/`>` — `quill_stat1`'s flat K+1
integers have no separate notion of range selectivity to draw on. This is a documented imprecision, not
a bug: it can only make the estimate worse, never silently wrong.

**Stage 3 — costs.**

```
SeqScan:    1.0 * 100  +  0.01 * 10000  =  100 + 100  =  200.0             (startup=0.0)

IndexScan:  leaf_pages_touched = ceil(100 * 2 / 10000) = ceil(0.02) = 1
            startup = 4.0 * 3 = 12.0
            cost    = 1.0*(1-1) + 2*(4.0*3) + 0.01*2 = 0 + 24 + 0.02 = 24.02
            total   = 12.0 + 24.02 = 36.02
```

`choose_access_path` picks `min(200.0, 36.02)` — the index scan, correctly: two selective equalities
plus a tight bound on the third column make this a highly targeted seek.

**What each cost component is, and why it's there.**

SeqScan's two terms:

- `SEQ_PAGE_COST × page_count` — the actual I/O: every page read exactly once, in physical order, no
  jumping around. That's what "sequential" buys: the cheap rate.
- `CPU_PER_ROW × row_count` — a seq scan has zero `seek_terms`, so every row, matching or not, still has
  to be evaluated against the `WHERE` clause by `Filter`. Kept small relative to page cost, modeling
  "I/O dominates until row counts get very large."

IndexScan's four terms, in the order they're paid:

- `RANDOM_PAGE_COST × height` (the seek, charged as `startup`) — descending from the index root to the
  first matching leaf, one random page read per B+tree level.
- `SEQ_PAGE_COST × (leaf_pages_touched - 1)` (further leaves) — leaf pages are linked, so matches
  beyond the first leaf page are walked forward sequentially, not re-descended to from the root. The
  `- 1` exists because `height` already paid for reaching that first leaf page during the descent.
- `rows_fetched × (RANDOM_PAGE_COST × table_height)` (table lookups — usually the dominant term) —
  quilldb's index entries store the indexed columns plus the rowid, not the full row, so every matched
  row needs its own separate root-to-leaf descent into the *table's* B-tree. This is exactly what a
  covering index would remove; quilldb doesn't model `covering` yet, so every index scan is priced as if
  it always needs the table lookup — a deliberately pessimistic simplification, never a falsely
  optimistic one.
- `CPU_PER_ROW × rows_fetched` — the same per-row evaluation cost as SeqScan's, but charged only against
  the rows the seek actually touched — the direct payoff of a selective seek.

Why `RANDOM_PAGE_COST` must exceed `SEQ_PAGE_COST` at all: it's the one assumption everything else
depends on. If random and sequential reads cost the same, an index scan's repeated table lookups
wouldn't look any worse than a scan's forward read, and the model would lose the ability to tell "seek"
apart from "scan."

**How the seek actually finds a contiguous run, and where `leaf_pages_touched` comes from.** It's
tempting to read this plan as "seek on `a,b`, then filter `c` once you reach the leaf" — but that's not
quite it. `a`, `b`, and `c` are all part of the *same* index key, and the leaf level is sorted
lexicographically on `(a,b,c)`: first by `a`, then by `b` within each `a`, then by `c` within each
`(a,b)`. Every row with `a=1 AND b=2` already sits physically next to every other such row, sorted by
`c` ascending:

```
(a=1, b=2, c=1) → rowid 401
(a=1, b=2, c=2) → rowid 402
(a=1, b=2, c=3) → rowid 403
(a=1, b=2, c=4) → rowid 404   ← first row matching c>3
(a=1, b=2, c=9) → rowid 405   ← still matching
(a=1, b=3, c=1) → rowid 406   ← b changed: group is over
```

The seek descends using **all three bounds combined into one composite key** — "find the first entry
≥ `(1,2,3+ε)`" — landing directly on the first genuinely matching row in one root-to-leaf trip. From
there the cursor walks forward through the linked leaf pages, and the only thing it checks to know when
to stop is whether `a` or `b` changed; it never re-checks `c>3` row by row, because `c` only increases
within the group once the walk has started past `c=3`. A range on the *last* column of a composite index
still gets this treatment — it narrows where the walk starts and stops, it doesn't become a separate
filter pass (this is the mechanism behind §12.3's sandwich case).

`leaf_pages_touched`, though, is never computed by actually walking the tree — costing happens *before*
any I/O, which is the entire point of statistics-based estimation. It's inferred from density alone:

```
leaf_pages_touched = ceil(stats.leaf_pages * rows_fetched / stats.row_count)
                    = ceil(100 * 2 / 10000) = ceil(0.02) = 1
```

The assumption: rows are spread roughly evenly across the index's leaf pages, so a contiguous run of
just 2 rows out of a 10,000-row, 100-leaf-page index should occupy about `100 * (2/10000) = 0.02` of a
page — "essentially nothing," rounded up to the minimum possible touch, 1 page. That's also why the
`- 1` in the cost formula makes sense concretely here: `height` already paid for descending to that
first matching leaf page, and the estimate says the whole 2-row run still fits on that same page — so
zero additional sequential leaf reads get charged.

> ELI5: the index's leaf level is one long sorted list, glued page to page. Looking for `a=1, b=2,
> c>3` is like looking for "everyone in a phone book sorted by last name, then first name, then
> birthday, whose last name is 'Al...' and who was born after March" — you don't scan the whole book,
> you flip straight to the "Al" section (the seek), and you're already looking at a small, contiguous
> slice of pages, not names scattered across the whole book. The planner doesn't count that slice by
> actually flipping to it, though — it estimates the slice's size from how thick the whole book is and
> what fraction of names it expects to match.


### Stage 4: search

As of week 5, only the single-table half of this section is built. `plan/search.py`'s
`choose_access_path` is literally `min(candidates, key=cost.total)` over one table's `AccessPath` list —
there's no `Join` operator, no `Sort` operator, and no `ORDER BY` in the AST yet to search over. The
join-order and `ORDER BY` material below is the target shape this stage grows into at week 7 (§12.7's
"at most three tables" roadmap note), not what runs today. `choose_access_path` doesn't get rewritten
when joins land — it becomes the per-table building block a join-order search calls repeatedly.

For one table, choose the minimum-cost access path. For up to three inner-joined tables, enumerate every
legal left-deep order and every usable parameterized inner index path. Cost a nested loop as:


```
outer_cost + outer_estimated_rows × inner_lookup_cost
```


Concretely: `orders(order_id, customer_id, total)` and `customers(customer_id, region)`, joined on
`customer_id`, `WHERE region='US'` matching ~50 of 10,000 customers.

- **outer=`customers`, inner=`orders`:** outer cost is cheap — an index seek on `region` fetching ~50
  rows. For each of those 50 rows, one cheap seek into `orders` by that specific customer's
  `customer_id` (~12 cost each, *parameterized* by the outer row's value). Total ≈
  `outer_cost + 50 × 12`.
- **outer=`orders`, inner=`customers`:** `region` lives on the *other* table, so `orders` has nothing of
  its own to filter on — the outer side scans the whole table, maybe 100,000 rows, then seeks into
  `customers` once per row. Total ≈ `outer_cost_huge + 100,000 × 12`.

Same tables, same indexes, same query — only the join order changed, and the cost differs by orders of
magnitude. That's why stage 4 for joins is a genuine search over *combinations*, not "cost each table's
best standalone path and glue them together": the inner table's path here is parameterized by the outer
row's value, which is why it can be far cheaper than anything that table's own stages 1-3 would produce
querying it alone.

LEFT JOIN restricts reordering because it is not commutative. For `ORDER BY`, compare a cheaper unordered
path plus Sort against a potentially more expensive ordered index path. Avoiding Sort is a candidate
property, not a command to ignore total cost.


Concretely: the ranking key is `candidate.cost.total + sort_cost(candidate, order_by)`, not
`candidate.cost.total`. And because quilldb keeps *all* six orders rather than pruning to the cheapest
plan per relation set, it gets this for free — pruning is what makes **interesting orders** a problem,
because the plan you discard for being 5% pricier may have been the one that avoided the sort. System R
handles this by keeping one extra plan per useful ordering; quilldb handles it by being small enough not
to prune. See §12.7.

> ELI5: imagine ranking delivery routes purely by gas cost and throwing away every route but the
> cheapest. If the cheapest route happens to end somewhere inconvenient — say, the driver still has to
> circle back before clocking out — you've thrown away a route that cost 5% more in gas but needed no
> circling back at all, and only find out it would've been better after the fact. "Interesting orders"
> is the same trap one level up: pruning by cost alone can discard a slightly pricier plan that was
> already sorted the way a later step needed, forcing an expensive Sort you didn't have to pay for.
> quilldb sidesteps the trap by never pruning early — with only six join orders total, it just keeps
> everything until the final ranking, so nothing useful ever gets thrown away too soon.


**Three traps:**


1. **`residual` must never be dropped.** Consuming `a=1` from `WHERE a=1 AND c=3` and forgetting to
   filter on `c` returns wrong rows — and it's the kind of bug that only shows up in data where the
   index prefix isn't already selective enough to hide it. Your differential tests catch this; make
   sure they include multi-conjunct predicates.
2. **`SeqScan` must remain a candidate even when an index is usable.** A boolean index that fetches half
   the table should often lose. Separately assert that `WHERE c=3`, `WHERE lower(x)=?`, and
   `WHERE a=1 OR b=2` cannot generate an ordinary index seek.
3. **Don't let the planner change results, ever.** The roadmap's week-4 property test is the guard:
   run every query with and without each index and assert identical output. A planner is an
   *optimization*, and an optimization that changes answers is just a bug with a nice name.


Two additional tests prove the architecture rather than only its output:


- With real statistics, `y_idx` at ~10 rows/value beats `x_idx` at ~5,000.
- Change only the stored statistics and assert that the selected index flips while results do not.


Cost-based tests are deterministic when the fixture statistics are explicit. Do not make assertions
against whatever distribution happened to be inserted by an unrelated test.


`EXPLAIN` prints the operator tree. Note that quilldb's `EXPLAIN` corresponds to SQLite's `EXPLAIN
QUERY PLAN`, not its `EXPLAIN` — the latter dumps VDBE bytecode, which you don't have (chapter 09).
Worth one line in the README so nobody expects bytecode.


```
Project  [id, email, age]
└─ IndexScan  ix_email  (email = 'a@b.c')   est_rows=1  startup=12.00  cost=24.01
```


Plain `EXPLAIN` does not execute, so it can print only estimates. `EXPLAIN ANALYZE` executes the chosen
plan once and adds `actual_rows` and the buffer pool's measured `pages_read`. Keeping those interfaces
separate prevents an estimate from being presented as evidence.


---


## 12.7 Roads not taken


### System R dynamic programming (the classic answer)


For join ordering, enumerate every subset of tables bottom-up, keeping the cheapest plan for each
subset plus the cheapest "interesting order" — then build up. Guarantees the optimal left-deep order
under the cost model.


**Used by:** PostgreSQL (for small joins), DB2, and essentially every textbook. **Why not here:**
quilldb's week-7 joins contain at most three tables, so directly enumerating at most six left-deep
orders is smaller and easier to inspect than building a subset memo. Worth knowing as *the* canonical
answer, and worth knowing that SQLite deliberately chose a polynomial heuristic (N3) instead because
it must handle much larger joins with a fixed planning budget.


### Cascades / Volcano optimizer


Express optimization as rules that transform plan fragments, applied by a search engine with
memoization. **Used by:** SQL Server, CockroachDB, Calcite, DuckDB. **Why it's attractive:**
extensible — new optimizations are new rules, not new special cases in a growing function. **Why not:**
it's a framework whose memo groups, transformation scheduling, and property enforcement cost more than
quilldb's bounded set of scan, nested-loop, and sort alternatives.


### Genetic / randomized join ordering


Postgres's GEQO, used past a threshold number of tables: random plans, mutate, keep the fit ones.
**Why it exists:** when exhaustive search is impossible, a good-enough plan found quickly beats an
optimal plan found never. **The interesting property:** it's non-deterministic — the same query can get
different plans on different runs unless carefully seeded, complicating reproducible `EXPLAIN` output
and regression tests.


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
14. What can `sqlite_stat1` averages represent, and what skew/correlation information do they lose?
    What can an optional `stat4` build add?
15. Name the four stages of quilldb's cost-based pipeline. Which stages use rules, statistics, costs,
    and search respectively?
16. Your planner consumes `a=1` and forgets the residual `c=3`. What's the symptom, and which test
    catches it?
17. Why must SeqScan remain a candidate even when a legal index path exists?
18. Why does quilldb enumerate join orders exhaustively while SQLite uses N3?
19. What belongs in plain `EXPLAIN`, and what requires `EXPLAIN ANALYZE`?


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
- [The ANALYZE command](https://www.sqlite.org/lang_analyze.html) — how statistics are collected and
  reloaded, plus the optional `sqlite_stat4` table when SQLite is compiled with STAT4 support.
- [`where.c`](https://github.com/sqlite/sqlite/blob/master/src/where.c) — `LogEst rRun` / `nOut` and
  the dominance-pruning comparison. The planner spans `where.c`, `whereexpr.c` and `wherecode.c`,
  ~13,000 lines together.
- **Measurements in §12.3, §12.4 and §12.5** are verbatim `EXPLAIN QUERY PLAN` output from `sqlite3`
  3.37.2 (CLI) and 3.50.4 (via Python), on 10,000-row tables with known value distributions. The
  statistics-injection experiment in §12.5 is reproducible with the shown SQL. Two things I could
  *not* verify: skip-scan's exact "~18 duplicates" threshold (documented, not bisected), and the
  optional `unordered` / `sz=NNN` / `noskipscan` tokens in `stat` — `ANALYZE` never emitted them.


---


**Next:** [13 — Atomic commit and the journal](../txn/13-atomic-commit-and-the-journal.md) — why the
commit point is *deleting a file*, and the exact order of writes that makes a crash survivable.