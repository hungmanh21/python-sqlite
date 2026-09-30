# ADR-003: Execute queries with a pull-based iterator pipeline, not a bytecode VM


## Status


Accepted (decided in week 3; recorded in week 8).


## Context


SQLite compiles every statement into bytecode for its VDBE, a register machine. The alternative used by
Postgres and most textbooks is the Volcano model: a tree of operators, each with `open` / `next` /
`close`, where a parent pulls rows from its child one at a time.

The executor also has to satisfy constraints that come from elsewhere in the project: `EXPLAIN` should be
readable, the planner should be able to swap one access path for another without touching the operators
above it, and no operator should have to hold a whole result set in memory.


## Decision


Queries run as a tree of pull-based operators (`exec/operators.py`, `exec/join.py`, `exec/sort.py`,
`exec/aggregate.py`). Each operator sees only "something that yields rows", so `Filter` neither knows nor
cares whether its child is a `SeqScan`, an `IndexScan`, a join or a `Sort`. A `SELECT`'s operator stream
is not drained at the API boundary: `fetchone()` pulls from the same open operator.


## Consequences


- **Streaming for free.** At any moment the pipeline holds one row plus operator state, so a query over
  data larger than memory is ordinary. `Limit` stops pulling once it has its rows, and nothing below it
  runs further. Measured over 100,000 rows: `LIMIT 1` reads **4** pages and `LIMIT 100` reads 5, against
  **1,990** for the full scan (`python -m quilldb.bench limit_short_circuit`).
- **The blocking operators are where it stops.** `Sort` and hash aggregation must consume their whole
  input before yielding a row, so `ORDER BY grp LIMIT 1` also reads all 1,990 pages. The model gives
  short-circuiting only where no blocking operator sits between the `Limit` and the scan.
- **`EXPLAIN` is a tree print.** Each operator renders itself; `EXPLAIN ANALYZE` wraps the same tree and
  adds the row and page counters.
- **The planner's swap is local.** Replacing `SeqScan(users)` with `IndexScan(idx)` leaves `Filter` and
  `Project` unchanged, and the filter is kept as a residual check, which is correct and keeps planning
  simple.
- **Per-row dispatch is paid on every row.** A tree of Python method calls costs more per row than a
  compiled program that amortises dispatch. This was not measured against a bytecode implementation; the
  claim is that it is the price of the model, not a measured slowdown.
- **A cost of being a tree:** re-planning mid-stream is hard, which is why runtime-adaptive execution was
  never a candidate.


## Alternatives considered


- **A bytecode VM (SQLite's VDBE).** It amortises dispatch and is what SQLite does, but it needs a
  compiler from the bound tree to instructions plus the interpreter, roughly three times the code by the
  estimate in `docs/design_decisions.md`, and `EXPLAIN` becomes a disassembly rather than a tree. It is
  also less transferable: the operator-tree model is how Postgres executes.
- **Materialise between operators** (`list(scan)` then filter then project). Simple, and pleasant for 100
  rows, but memory grows with table size and the first result waits for the full scan; it also makes
  `LIMIT` useless as an optimisation. Chapter 09 §9.2.
- **Operators that know their children's types.** Every new operator would edit every consumer. Chapter 09
  §9.3.
