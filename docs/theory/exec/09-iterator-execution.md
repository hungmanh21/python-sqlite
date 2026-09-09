# 09 — Iterator Execution


> **Read in week 3 before writing `exec/expressions.py` and `exec/operators.py`.**
>
> **Time:** ~45 minutes. **Prerequisite:** [chapter 08](../catalog/08-the-catalog-and-binding.md) for bound expressions, and [chapter 06](../btree/06-b-tree-mechanics.md) §6.3 for the storage cursor that powers a scan.


---


## 9.0 The problem: turn meaning into rows without loading the table


After parsing and binding, this query:


```sql
SELECT name FROM users WHERE age > 30;
```


has enough information to execute:


```text
table root: page 4
name: column slot 1
age:  column slot 2
predicate: slot 2 > 30
```


The obvious implementation is one function:


```python
def execute_select(statement):
    rows = read_every_row(statement.table)
    rows = [row for row in rows if predicate(row)]
    rows = [project(row) for row in rows]
    return rows
```


It gives the right answer. It also loads the entire table, bakes every relational operation into one
function, and offers no clean place for Week 4's index scan or Week 7's joins.


You need a design that is:


- **streaming** — memory depends on operator state, not table size
- **composable** — filtering and projection work over any source
- **extensible** — an index scan can replace a table scan without rewriting its consumers
- **observable** — `EXPLAIN` can print what will run
- **resource-safe** — cursors release page pins on exhaustion, error, or abandonment


The iterator model gets all five from one interface.


---


## 9.1 The pull pipeline


Represent the query as an operator tree:


```text
Project(name)
└─ Filter(age > 30)
   └─ SeqScan(users)
```


Every operator supports:


```python
open()
next() -> row | None
close()
```


The user asks the root for one row. The request flows downward; data flows upward:


```text
Cursor.fetchone()
    asks Project.next()
        asks Filter.next()
            asks SeqScan.next() → (1, "ada", 36)
        tests 36 > 30 → TRUE
    returns ("ada",)
```


The next `fetchone()` repeats from the operators' current positions. Nothing asks for "all rows."


This family is called the **iterator model**, the **Volcano model**, or the **open/next/close model**.
The names differ slightly across texts, but the central contract is the same: each `next()` produces
at most one tuple and preserves enough state to resume later.


> **Say this out loud:** "Execution is a pull pipeline. The consumer asks the root operator for one
> row; each operator pulls from its child only as needed, transforms or rejects that row, and returns
> upward. That gives bounded memory and lets access paths be swapped under the same Filter and
> Project nodes."


---


## 9.2 Wrong answer 1: materialize after every operation


```python
scan_rows = list(scan(table))
filtered = [row for row in scan_rows if predicate(row)]
projected = [project(row) for row in filtered]
```


For 100 test rows this is pleasant. For 10 million 100-byte rows, the first list holds roughly a
gigabyte of payload before Python object overhead. The filter may retain only ten rows, but memory is
spent before that selectivity helps.


Streaming lets the filter discard a row immediately:


```text
read row → test → discard
read row → test → discard
read row → test → project → return one result
```


At any instant the pipeline holds one row plus operator state. A query over data larger than RAM is
therefore ordinary rather than exceptional.


### Latency matters too


Materialization delays the *first* result until the full scan finishes. A pull pipeline can return
the first matching row immediately. This matters for interactive clients and for `LIMIT`, which can
stop asking after enough rows and avoid reading the rest of the table entirely.


`LIMIT` is a Week 7 feature, but the Week 3 interface already makes its short-circuit behavior
possible.


---


## 9.3 Wrong answer 2: make every operator know every child type


```python
class Filter:
    def next(self):
        if isinstance(self.child, SeqScan): ...
        elif isinstance(self.child, IndexScan): ...
```


This defeats composition. Adding `IndexScan`, `Join`, or `Sort` requires editing every consumer.


The operator contract exists to erase those differences:


```python
row = self.child.next()
```


`Filter` cares only that its child yields rows. It does not know whether they came from a table
cursor, an index, a join, or an in-memory values list.


This is the database version of programming to an interface. The important payoff arrives in Week
4: the planner replaces `SeqScan(users)` with `IndexScan(idx_age)` while leaving `Filter` and
`Project` unchanged.


```text
Project(name)                  Project(name)
└─ Filter(age > 30)       →        └─ Filter(age > 30)
   └─ SeqScan(users)                  └─ IndexScan(idx_age, 30..)
```


The filter may become partly redundant after an index chooses candidates, but retaining it as a
residual check is correct and keeps planning simple.


---


## 9.4 What `open`, `next`, and `close` each mean


### `open`: acquire and initialize


`SeqScan.open()` creates a `TableCursor`, descends to the first leaf, and pins its path. `Filter.open()`
opens its child. `Project.open()` does the same.


Why not do this in `__init__`? Constructors should assemble a plan without touching data. This makes
it possible to:


- build and print an `EXPLAIN` tree without executing
- fail during binding before acquiring storage resources
- reopen a reusable operator deliberately
- test plan construction separately from execution


### `next`: advance once


`next()` has a precise state transition: return one row and position after it, or return `None` when
no rows remain. For a filter it may call its child many times before returning once:


```python
def next(self):
    while (row := self.child.next()) is not None:
        if where_passes(evaluate(self.predicate, row)):
            return row
    return None
```


The loop is not eager. It does exactly enough work to satisfy one request.


### `close`: release, even on the unhappy path


`SeqScan.close()` closes its `TableCursor`, unpinning the entire root-to-leaf path. Parent operators
close children. `close()` must be idempotent because cleanup paths overlap:


- normal exhaustion closes the pipeline
- explicit `Cursor.close()` closes it
- `Connection.close()` closes an abandoned cursor
- exception handling closes it in `finally`


If the second close raises, resource cleanup becomes fragile. "Already closed" is a valid state, not
an exceptional one.


### The generator temptation


Python generators appear to provide the whole model:


```python
def filter_rows(child):
    for row in child:
        if predicate(row):
            yield row
```


They are excellent for pure pipelines. The difficulty is deterministic ownership of page pins. A
generator abandoned without exhaustion is finalized at an implementation-dependent time unless the
caller explicitly closes it. The explicit operator lifecycle makes resource ownership visible and
maps directly to a DB cursor's lifecycle.


You can still implement small internal helpers as generators. The public execution boundary should
have an explicit close operation.


> **Say this out loud:** "Constructors build plans; `open` acquires state, `next` performs one unit of
> demand-driven work, and `close` releases resources idempotently. I kept lifecycle explicit because
> a scan owns pinned buffer-pool pages, so relying on generator finalization would make correctness
> depend on garbage collection timing."


---


## 9.5 The leaf of the plan: `SeqScan`


`SeqScan` adapts the storage-level `TableCursor` to the executor's row interface.


The two cursors solve different problems:


| Cursor | Knows about | Returns |
|---|---|---|
| `TableCursor` | B+tree pages, rowids, overflow chains, pins | encoded record bytes |
| API `Cursor` | result columns, fetch methods, lifecycle | result tuples |


`SeqScan` sits between them:


```text
TableCursor.record() → bytes
decode_record()      → tuple[Value, ...]
SeqScan.next()       → executor Row
```


This boundary keeps record encoding out of `Filter` and page navigation out of the API.


### The advance-order bug


At a valid storage cursor position, `SeqScan.next()` must:


1. read the current record
2. advance the cursor for the next call
3. return the record it already read


Advance first and the smallest row is skipped. Return first and forget to advance and the same row
is returned forever. This is a tiny state machine; write empty, one-row, and two-row tests.


### Why scans naturally have backpressure


If the application calls `fetchone()` once and pauses, no more pages are read. Demand controls work.
This property is **backpressure** in its simplest form: the producer cannot outrun the consumer
because production occurs only in response to a pull.


---


## 9.6 `Filter` and SQL's three-valued logic


Python booleans have two values. SQL predicates have three:


```text
TRUE, FALSE, UNKNOWN
```


quilldb represents `UNKNOWN` as `None`, the same representation used for SQL `NULL`.


Why is a third value necessary?


```sql
age > 30
```


If `age` is `NULL`, the database does not know whether the comparison is true or false. `NULL` is not
a number and not a special number smaller than every other. The result is unknown.


### Comparisons with NULL


```text
NULL = NULL  → NULL
NULL != 5    → NULL
NULL > 30    → NULL
```


This surprises people expecting `NULL = NULL` to be true. `NULL` means "missing/unknown," not "the
same singleton object." Two unknown ages are not thereby known equal.


To ask whether a value is missing, SQL has `IS NULL`:


```text
NULL IS NULL      → TRUE
5 IS NULL         → FALSE
NULL IS NOT NULL  → FALSE
```


`IS NULL` never returns unknown, which is why chapter 07 gives it a distinct AST node rather than
turning it into equality with a NULL literal.


### AND and OR are not Python `and` and `or`


The defining cases:


```text
FALSE AND NULL → FALSE   # already false regardless of the unknown
TRUE  AND NULL → NULL
TRUE  OR  NULL → TRUE    # already true regardless of the unknown
FALSE OR  NULL → NULL
NOT NULL       → NULL
```


These follow from asking whether the unknown input could change the result. If not, the known value
wins. If yes, the result stays unknown.


### WHERE keeps only TRUE


`WHERE` discards both false and unknown:


```sql
SELECT * FROM users WHERE age > 30;
```


Rows with `age = NULL` do not appear. They did not satisfy the predicate; the engine could not prove
it true.


This rule is why `Filter` should call one named helper such as `where_passes()`. Scattering Python
truthiness across operators creates inconsistencies — especially because `None`, `0`, `b""`, and
`""` all have Python truth behavior that is not automatically SQL predicate behavior.


> **Say this out loud:** "SQL uses three-valued logic because comparisons involving unknown data are
> themselves unknown. `IS NULL` is a separate operator that always returns true or false, and WHERE
> keeps only true, discarding both false and unknown. I implemented the truth tables explicitly
> instead of inheriting Python's `and`, `or`, and truthiness."


---


## 9.7 Expression evaluation after binding


A bound column is an integer slot:


```python
BoundColumn(index=2, name="age", data_type=INTEGER)
```


Evaluation is then a recursive tree walk:


```text
BoundBinaryOp(
    left=BoundColumn(2),
    operator=">",
    right=BoundLiteral(30),
)
```


```python
left = evaluate(expr.left, row)    # row[2]
right = evaluate(expr.right, row)  # 30
return sql_compare(">", left, right)
```


The evaluator should not know table names, root pages, tokens, or SQL source text. Those facts belong
to earlier phases.


### NULL propagation


For Week 3 arithmetic and ordinary comparison:


```text
NULL + 1 → NULL
7 * NULL → NULL
NULL < 4 → NULL
```


Check for NULL before Python performs the operation. Otherwise errors like `None + 1` leak as
`TypeError`, exposing the host language instead of SQL semantics.


### Division by zero


SQLite returns NULL for division or remainder by zero. Python raises `ZeroDivisionError`. This is a
good example of why an evaluator is a semantic boundary rather than a thin call to Python operators.


Pick the SQLite behavior, test it, and keep the difference localized.


### LIKE without regex surprises


Week 3 `LIKE` needs two metacharacters:


- `%` matches zero or more characters
- `_` matches exactly one character


A dynamic-programming matcher over `(value_index, pattern_index)` is small and avoids translating
regex metacharacters incorrectly. A pattern such as `a.c%` contains a literal dot in SQL; feeding it
unescaped to a regular expression would make the dot match any character.


SQLite's default ASCII case-folding behavior for LIKE has details quilldb need not reproduce yet.
Document Week 3 LIKE as case-sensitive Unicode code-point matching. Again: a narrower declared
semantic is better than accidental partial compatibility.


---


## 9.8 `Project`: expressions define the output row


Projection is not merely selecting columns:


```sql
SELECT name, age + 1, name LIKE 'a%' FROM users;
```


For each input row, `Project` evaluates each bound expression in order and returns the tuple of
results. That same mechanism handles reordered columns, repeated columns, constants, and arithmetic.


`SELECT *` should already have been expanded by the binder into explicit bound columns. If Project
has a special star branch, every later feature has to remember two projection representations. The
binder is the phase whose job is to turn shorthand into explicit meaning.


### Column descriptions


The API cursor needs names before the first row exists, including for an empty table. Therefore
descriptions come from bound projection expressions, not from inspecting result values.


A plain bound column uses its name. A computed expression can use a stable rendered form for now.
SQL aliases arrive later; do not infer types or names from the first row.


---


## 9.9 `Insert` as an operator, even though it returns no rows


Why model insertion with the same interface as scans?


Because later statements are naturally dataflow:


```sql
INSERT INTO archive SELECT * FROM events WHERE old = 1;
```


That feature is not in Week 3, but a common operator lifecycle gives mutation a place in the same
plan tree and lets execution/reporting share machinery.


Week 3's `Insert` is a one-shot state machine:


```text
before first next(): not executed
first next(): encode row, choose rowid, insert; return None
later next(): return None without inserting again
```


The public `Connection.execute()` drains and closes mutation operators before returning, so the user
does not need to call `fetchone()` to make an insert happen. This is an API decision built on top of
the operator contract.


### Choosing an implicit rowid


With no exposed `INTEGER PRIMARY KEY` yet, table B+tree keys are internal. Seek to the last rowid and
choose `last + 1`, or 1 for an empty tree. This is simple and produces increasing keys, which the
rightmost-split optimization from chapter 05 handles efficiently.


Do not use the number of rows as the next rowid. Deletion in Week 4 would make that reuse an existing
key.


---


## 9.10 Streaming operators and blocking operators


Week 3's operators are all **streaming**:


- SeqScan can return after reading one row
- Filter can return after finding one passing row
- Project can return after transforming one row


Later you will add operations that cannot always produce output immediately:


- `Sort` must see all input before it knows the first row
- hash aggregation must consume a group or the whole input
- a hash join must build a hash table for one side


These are **blocking operators**. They still fit `open/next/close`; `open()` or the first `next()`
performs the blocking work, then later calls emit results. The interface does not promise every
operator uses constant memory. It gives every operator one lifecycle and one compositional surface.


This distinction is useful in query planning: pushing a selective Filter below Sort reduces how much
the blocking operator must hold.


```text
bad:   Filter → Sort → SeqScan    # sort everything, then discard
good:  Sort → Filter → SeqScan    # filter first, sort fewer rows
```


Read trees bottom-up: in the good plan, `SeqScan` feeds `Filter`, which feeds `Sort`.


---


## 9.11 The road not taken


### SQLite's VDBE bytecode


SQLite compiles statements into instructions for the **Virtual Database Engine** (VDBE), then steps
that program. A simplified flavor is:


```text
OpenRead table
Rewind
Column 2
Le 30 jump
Column 1
ResultRow
Next
Halt
```


Why it is good: a compact interpreter centralizes control flow, prepared statements are reusable
programs, and low-level opcodes allow careful performance engineering in C.


Why quilldb does not use it: you would have to design an instruction set, compiler, register model,
and interpreter before one query runs. That is much more code than operator composition and teaches
less about access paths and relational plan shape for this project's goals.


### Push-based execution


Instead of parents pulling, children can push rows into consumers. Push engines can reduce virtual
call overhead and improve instruction/cache behavior. They complicate early termination and require
the consumer protocol to propagate "stop" upward.


The pull model maps directly to `fetchone()` and is easier to debug. At pure-Python scale, page
access, decoding, and expression evaluation dominate before iterator call overhead becomes the main
problem.


### Vectorized execution


Process batches of hundreds or thousands of values per call rather than one row. This amortizes
dispatch and enables CPU-friendly columnar loops; analytical engines often choose it.


quilldb stores row-oriented records and targets an OLTP-shaped teaching workload. A row-at-a-time
iterator is the coherent first design. If profiling later identifies `next()` dispatch as dominant,
batching is an evolution of the operator boundary, not a reason to skip the vertical slice.


### Return every row as a list


Simplest API, worst bound. It prevents true `fetchone`, hides abandoned-scan cleanup, and makes a
large query require memory proportional to its output. `fetchall()` should be a convenience loop
over streaming `fetchone()`, visibly choosing materialization at the caller's request.


---


## 9.12 What you're building


The Week 3 plan path is fixed but real:


```text
SQL text
  → AST
  → bound statement
  → Project [→ Filter] → SeqScan
  → API Cursor.fetchone/fetchmany/fetchall
```


The contracts that future weeks rely on:


| Contract | Future payoff |
|---|---|
| every source is an `Operator` | Week 4 swaps in `IndexScan` |
| expressions consume bound slots | indexes/plans do no name lookup per row |
| root cursor owns deterministic close | Week 6 can reason about locks and pins |
| plan exists before opening | Week 4 `EXPLAIN`, later plan cache |
| rows stream one at a time | `LIMIT`, joins, and large results remain possible |
| NULL semantics live in one evaluator | every later operator gets consistent truth behavior |


One property is more important than all micro-optimizations: **an operator that raises must still
close its child.** Test this with a tiny buffer pool. A leaked pin may not fail the query that leaked
it; it fails the next unrelated query when eviction finds every page pinned. Resource bugs, like
split bugs, often surface far from their cause.


---


## 9.13 Check yourself


1. What flows downward and what flows upward in a pull pipeline?
2. Why does materializing after SeqScan waste memory even when a Filter is highly selective?
3. How can `LIMIT` avoid reading the rest of a table under pull execution?
4. Why must Filter know only `child.next()` rather than the child's concrete type?
5. Why should constructors build plans without opening storage cursors?
6. What exact state transition does `next()` promise?
7. Why must `close()` be idempotent?
8. What resource-lifetime problem makes explicit operators safer than relying only on generators here?
9. In what order does SeqScan read and advance, and what bug appears if you reverse it?
10. Evaluate `FALSE AND NULL`, `TRUE OR NULL`, `NULL = NULL`, and `NULL IS NULL`.
11. Which predicate results does WHERE keep?
12. Why should `SELECT *` be expanded during binding rather than handled specially by Project?
13. Why is the next rowid `last + 1` rather than `row_count + 1`?
14. Name two blocking operators and explain how they still fit open/next/close.
15. Give the best reason for SQLite's VDBE and the best reason quilldb uses iterators.


If 12 of 15 come out fluently, you understand the Week 3 execution model rather than just its code.


---


## 9.14 Sources


- Goetz Graefe, **"Volcano — An Extensible and Parallel Query Evaluation System"** (1994) — the open/next/close model, operator composition, and exchange as a parallelism boundary.
- [SQLite Architecture](https://www.sqlite.org/arch.html) — the VDBE's place beneath code generation and the separation between frontend and storage layers.
- [SQLite Opcode Documentation](https://www.sqlite.org/opcode.html) — generated documentation for the VDBE instruction set; useful for contrasting a bytecode engine with an operator tree.
- [SQLite Expression Language](https://www.sqlite.org/lang_expr.html) — NULL behavior, operators, parameters, and LIKE semantics.
- [PostgreSQL Executor README](https://github.com/postgres/postgres/blob/master/src/backend/executor/README) — plan-tree execution and tuple-at-a-time demand from parent nodes.
- [toydb architecture](https://github.com/erikgrinaker/toydb/blob/master/docs/architecture.md) — a small educational database using iterator execution and explicit plans, close in scale to quilldb.


---


**End of Week 3 theory.** Week 4 adds indexes and planning, where the payoff for all three chapters
arrives: the parser and binder stay unchanged, the planner swaps `IndexScan` under the same operators,
and `EXPLAIN` prints the tree that already exists.