# Week 4 — Stage 5 checklist: IndexScan, ANALYZE, EXPLAIN


← [Week 4 spec](week-4-mutation-and-indexes.md)


Stages 1-4 of the cost-based pipeline (`plan/predicates.py`, `plan/planner.py`,
`plan/statistics.py`, `plan/cost.py`, `plan/search.py`) are done and tested:
`enumerate_access_paths -> estimate_row_counts -> assign_cost ->
choose_access_path` correctly picks the cheapest legal AccessPath for one
table. Nothing outside `plan/` calls any of it yet. This is the remaining
work to make the planner real: `build_operator()` still hard-codes the
Week-3 plan, and `ANALYZE`/`EXPLAIN` parse but never bind or execute.


Full design rationale lives in `/usr2/hmanh/.claude/plans/optimized-stargazing-dijkstra.md`
(the plan-mode writeup). This file is the SEQUENCED, one-step-at-a-time
checklist derived from it — each step is independently completable, testable,
and reviewable before starting the next. Work top to bottom; check a step
done before moving on.


## Step 1 — `IndexScan` operator (isolated, no wiring)


**What:** A new `IndexScan` class in `exec/operators.py`, matching
`SeqScan`'s shape (`open`/`next`/`close`/`explain`). Given an `AccessPath`
(`kind="index_scan"`) plus the table it reads from, it seeks the index for
matching rowids (`IndexBTree.seek_eq`/`seek_range`, chosen by whether
`seek_terms` are all equalities or end in an inequality sandwich) and, for
each rowid, does a `TableCursor.seek(rowid)` point lookup to fetch the real
row. NOT wired into `build_operator()` yet — tested standalone against a
hand-built `AccessPath`, the same way `test_operators.py` already tests
`SeqScan`/`Filter`/`Project` in isolation before `build_operator` enters the
picture.


**Why isolated first:** the seek-shape logic (single seek_eq vs seek_range,
extracting literal values out of `Predicate.value`) is the one piece with
real judgment calls. Isolating it means you can get IndexScan correct and
reviewed before it has to also interact with the planner's cost/candidate
selection.


**Files:** `exec/operators.py` (new class), `tests/unit/exec/test_index_scan.py`
(new — row production in index order, pin discipline on exhaustion/abandonment/
exception, matching the existing SeqScan test pattern in `test_operators.py`).


**Done when:** `IndexScan` correctly streams rows for a hand-built AccessPath
against equality seeks, sandwich seeks, and gap/residual cases, with zero
leaked pins, verified by tests modeled on the existing SeqScan suite.


## Step 2 — Wire `IndexScan` into `build_operator()`


**What:** `build_operator()`'s SELECT branch calls the full pipeline:
`catalog.indexes_for(table.name)` -> sargable predicates
(`extract_conjuncts` + `classify_predicate` over `statement.where`) ->
`enumerate_access_paths` -> cost every candidate with
`default_table_stats()`/`default_index_stats()` (ANALYZE doesn't exist yet,
so this step ALWAYS uses the documented fallback stats — that's fine,
`choose_access_path` still works, it just can't yet prefer a real selective
index over the flat default) -> `choose_access_path` -> build `SeqScan` or
`IndexScan` as the source. `Filter` is built from `path.residual` only, not
the raw WHERE clause, since seek_terms are already satisfied by construction.


**Why before ANALYZE:** this is the trap-#3-critical step — "the planner
must never change results" — and it's testable without ANALYZE existing at
all, using only the default stats. Isolating it from ANALYZE means a bug here
can't be confused with a bug in tree-measurement arithmetic later.


**Files:** `exec/operators.py` (`build_operator` SELECT branch),
`tests/unit/exec/test_operators.py` (extend the existing `build_operator`
section), plus the trap-#3 property test: for a handful of WHERE clauses
against an indexed table, run with and without the index (e.g. by not
creating it) and assert identical row sets.


**Done when:** `SELECT ... WHERE indexed_col = ?` produces an `IndexScan` in
`build_operator()`'s output and the SAME rows a `SeqScan` plan would, for
every existing operator test plus new index-specific ones.


## Step 3 — ANALYZE: measure a real table B-tree


**What:** `plan/analyze.py`, new file. A single function
`measure_table(pager, pool, root) -> TableStats` that walks a real
table B-tree (DFS from root, same shape as `btree/validate.py`'s
`validate_btree` walk but without the invariant re-checking — see the
plan-mode writeup's design note #3) and returns row_count (leaf cell count),
page_count (total pages visited), and height (depth to first leaf reached).
No `quill_stat1` persistence yet, no IndexBTree measurement yet — just prove
the walker against a real on-disk tree.


**Why split from index measurement:** table measurement is pure counting (no
new arithmetic); index measurement needs the `rows_per_prefix` run-length
logic, which is real algorithmic work. Splitting means the mechanical piece
(scaffolded, no TODO) lands and gets tested before the harder piece.


**Files:** `plan/analyze.py` (new), `tests/unit/plan/test_analyze.py` (new
— build a real multi-page table via inserts, measure it, assert against
known page/row counts).


**Done when:** `measure_table` returns correct row_count/page_count/height
for a hand-built multi-level table B-tree, verified against counts you can
independently check (e.g. via existing test fixtures that already build
multi-page trees).


## Step 4 — ANALYZE: measure a real index B-tree (TODO(human))


**What:** `measure_index(pager, pool, root, n_key_columns) -> IndexStats` in
`plan/analyze.py`. Same page-walk shape as Step 3, PLUS the one genuinely new
piece: walking every leaf key in index order and computing `rows_per_prefix`
— for each prefix length 1..K, the average number of consecutive keys
sharing that prefix. This is the TODO(human) step: scaffolding provides the
page-walk (reusing Step 3's pattern) and leaf-key iteration; you implement
the run-length -> average conversion.


**Why its own step:** this is the one place besides the original planner
stages where you're asked to derive a nontrivial number from raw data,
matching this project's "hand-write the algorithmic core" pattern from
stages 1-4.


**Files:** `plan/analyze.py` (extend), `tests/unit/plan/test_analyze.py`
(extend — pin against a hand-built index where the expected prefix averages
are known by construction, same spirit as chapter 12 §12.5's `10000 100 10 2`
example but derived from real inserted rows instead of a literal string).


**Done when:** `measure_index` on a real multi-level index B-tree produces
`rows_per_prefix` matching hand-counted expectations.


## Step 5 — `quill_stat1` persistence + `StatisticsCatalog`


**What:** `encode_stat1_row(stats) -> str` (mirrors `parse_stat1`'s decode,
in `plan/statistics.py`), and a `StatisticsCatalog` class in
`plan/analyze.py` that owns a `quill_stat1` table (created the same way
`Catalog.create_table` creates any table — see design decision #4 in the
plan-mode writeup) and exposes `analyze(target: str | None)` (runs
`measure_table`/`measure_index` over one or all tables/indexes and persists
rows), `table_stats(name)`, `index_stats(name)` (load from the table, or
fall back to `default_table_stats`/`default_index_stats` if absent).


**Why after measurement:** persistence is mechanical once the numbers exist;
splitting it out means Steps 3-4's measurement logic gets verified in
isolation first, without also debugging record-encoding round-trips.


**Files:** `plan/analyze.py` (extend), `tests/unit/plan/test_analyze.py`
(extend — round-trip encode/decode, `StatisticsCatalog.analyze()` end-to-end
against a real on-disk database, `table_stats`/`index_stats` fallback
behavior when unanalyzed).


**Done when:** `ANALYZE` (invoked directly through `StatisticsCatalog`, not
yet through SQL) persists real numbers that survive a reload.


## Step 6 — Bind and execute `ANALYZE`, connect stats into the planner


**What:** `sql/binder.py` gains `BoundAnalyze` (passthrough, like
`BoundCreateTable` — target validity checked at execute time, not bind
time). `Connection` gains a `StatisticsCatalog` member, constructed and
loaded alongside `Catalog` in `connect()`. `Connection.execute()` dispatches
`BoundAnalyze` to `stats_catalog.analyze(target)`. `build_operator()`'s
Step-2 wiring is updated to look up real stats via `StatisticsCatalog`
before falling back to defaults.


**Why after Step 5:** this is where the whole pipeline first becomes
observable end-to-end: run `ANALYZE`, then run a `SELECT`, and see the
planner actually prefer a real selective index over the flat default —
chapter 12's own suggested test ("with real statistics, y_idx at ~10
rows/value beats x_idx at ~5,000").


**Files:** `sql/binder.py`, `api/connection.py`, `exec/operators.py`
(`build_operator`'s stats lookup), tests extending whichever suite covers
`Connection.execute()` end-to-end.


**Done when:** `ANALYZE; SELECT ... WHERE selective_col = ?` picks an
IndexScan and `ANALYZE; SELECT ... WHERE low_selectivity_col = ?` picks a
SeqScan, both driven by real measured statistics, not the flat default.


## Step 7 — `EXPLAIN` and `EXPLAIN ANALYZE`


**What:** `sql/binder.py` gains `BoundExplain` (binds the inner `Select` via
`bind_select`). `Operator.explain()` gains cost/row annotations for
`IndexScan` specifically (chapter 12's own format:
`IndexScan ix_email (email = 'a@b.c') est_rows=1 startup=12.00 cost=24.01`).
`Connection.execute()` dispatches `BoundExplain`: builds the operator via
`build_operator`, for plain `EXPLAIN` returns `operator.explain()` as a
one-column result without running anything; for `EXPLAIN ANALYZE`, drains
the plan first (discarding rows, timing it) then appends actual row count
vs `est_rows`.


**Why last:** EXPLAIN is a read-only reporting layer over everything built
in Steps 1-6 — it has nothing to verify on its own except that it prints
what the earlier steps already produced.


**Files:** `sql/binder.py`, `exec/operators.py` (explain() annotations),
`api/connection.py`, tests extending the `Connection.execute()` suite.


**Done when:** `EXPLAIN SELECT ...` and `EXPLAIN ANALYZE SELECT ...` produce
chapter-12-shaped output against a real database, matching the demo chapter
12 itself shows.


---


**Verification, every step:** `python -m pytest src/tests/unit/plan/
src/tests/unit/exec/ -q`, full suite with the one known pre-existing deselect,
`ruff check` + `mypy` on touched files — same discipline as stages 1-4.