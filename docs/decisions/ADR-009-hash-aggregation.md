# ADR-009: Hash aggregation, where SQLite sorts


## Status

Accepted (decided in week 7; recorded in week 8).


## Context

`GROUP BY` needs one accumulator per group, and there are two ways to get them
([chapter 18 §18.6](../theory/exec/18-sorting-and-aggregation.md)). **Sort-based** sorts the input by the
group key so equal keys are adjacent, then makes one pass, emitting a row whenever the key changes. It
holds one group's state at a time and its output arrives in key order. **Hash-based** keeps a map from
group key to accumulator, makes one pass with no sort, and holds every group's state at once.

SQLite is sort-based only. Its `select.c` has no hash-table operator, and the plan shows it as
`USE TEMP B-TREE FOR GROUP BY` when no index already provides the order. That is a code-size choice for a
C library aimed at embedded devices: a second general-purpose data structure for one operator is not worth
its footprint. quilldb does not carry that constraint. Python has a hash table built in.


## Decision

`GROUP BY` and plain aggregates run in `HashAggregate` ([`exec/aggregate.py`](../../src/quilldb/exec/aggregate.py)):
a `dict` from the tuple of group-key values to a list of per-aggregate states. It drains its child in
`open()`, folds each row into its group, then emits one flat row per group, `(group key values...,
finished aggregates...)`.

- **NULL is an ordinary key.** `GROUP BY nullable_col` puts every NULL row in one group, because
  `None == None` and `hash(None)` is well-defined. Nothing special-cases it.
- **No `GROUP BY` means exactly one group, always.** The single group is seeded before the child is pulled,
  so `SELECT COUNT(*) FROM empty` returns one row containing 0. A real `GROUP BY` over no rows returns no
  rows.
- **The output order is not promised.** The planner does not treat `HashAggregate` as preserving any
  order, so an `ORDER BY` above it adds a `Sort`.


## Consequences

- **Memory is proportional to the number of groups, and there is no spill path.** Measured on 50,000 rows:
  `GROUP BY grp` with 20 groups peaked at about **0.8 MB**; `GROUP BY id` with 50,000 groups peaked at
  about **14.6 MB** (`tracemalloc`; both figures include the buffer pool and the fetched result). A
  high-cardinality `GROUP BY` on a big table is bounded by RAM. `Sort` has a documented cap
  (`MAX_SORT_ROWS`, 1,000,000 rows, which raises `SortLimitExceededError`); `HashAggregate` has none.
- **It is a blocking operator.** Nothing is emitted until the whole input has been read, so a `LIMIT` above
  it cannot short-circuit the scan. This is the same limit `Sort` has ([ADR-003](ADR-003-iterator-pipeline-not-bytecode.md)).
- **Cost does not depend on input order.** An index that already delivers rows in group order does not
  help: quilldb has no streaming, sort-free `GROUP BY` for that case, which SQLite does.
- **No sort for the aggregate itself.** With few groups over many rows, which is the common case, this does
  one pass and holds a handful of accumulators.
- **Not measured against a sort-based version.** There is no sort-based `GROUP BY` in quilldb to compare
  with, so the claim that hashing is faster rests on the algorithmic argument (one pass against a sort),
  not on a benchmark here.


## Alternatives considered

- **Sort, then stream (SQLite's approach).** Constant memory per group and output in key order, which can
  satisfy an `ORDER BY` for free. Rejected because it costs a sort when the number of groups is small,
  and because `Sort` here is itself a fully in-memory operator with a row cap, so it would not bound memory
  any better than the hash does.
- **Both, chosen by the planner** (streaming when an index provides the group order, hash otherwise). The
  better engine, and what a fuller implementation would do: the planner already tracks the order an access
  path delivers (`output_order`), so the information exists. Not built; `HashAggregate` is the only
  aggregation operator.
- **Hash with a spill to disk** when the group count is large. Removes the memory limit, but needs
  partitioning and a temp-file path that nothing else in quilldb has.
