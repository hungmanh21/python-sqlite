# quilldb

> A relational database engine written from scratch in pure Python — that writes **the real SQLite
> on-disk file format, byte for byte**.

![python](https://img.shields.io/badge/python-3.12%20%7C%203.13-blue)
![dependencies](https://img.shields.io/badge/runtime%20dependencies-0-brightgreen)
[![CI](https://github.com/hungmanh21/python-sqlite/actions/workflows/ci.yml/badge.svg)](https://github.com/hungmanh21/python-sqlite/actions/workflows/ci.yml)
<!-- No coverage badge: it needs a third-party service. CI prints coverage in its log. -->

quilldb is a paged storage layer, an LRU buffer pool, B+tree tables and indexes, a hand-written SQL
parser and iterator-based executor with a statistics-driven cost-based optimizer, crash-safe
transactions with an undo journal, and multi-threaded connections with table-level two-phase locking
and deadlock detection — validated by `PRAGMA integrity_check` and a crash-injection matrix. No ORM, no
`sqlite3` module in the engine, no parser generator.

<!-- TODO: docs/demo.gif goes here, above the fold (week 8 §46). -->

Not a format *inspired by* SQLite. The actual `"SQLite format 3\000"` header, page layout, varints,
record encoding and type codes, copied from [the format spec](https://sqlite.org/fileformat2.html)
rather than invented. The acceptance test is therefore not one quilldb can grade itself on:

```console
$ sqlite3 mydata.db "PRAGMA integrity_check"
ok
```

Real `sqlite3` opens a file quilldb wrote, walks every b-tree, counts every index entry against its
table, and agrees. `src/tests/differential/` goes further: it runs the same SQL through both engines
and diffs the results.

## Install & use

Python 3.12 or newer. There are no runtime dependencies.

```console
$ pip install -e .          # or: uv sync
$ python examples/readme_example.py
```

```python
import pathlib, sqlite3, tempfile
import quilldb

path = pathlib.Path(tempfile.mkdtemp()) / "demo.db"
db = quilldb.connect(path)
db.execute("CREATE TABLE users (id INTEGER, email TEXT, age INTEGER)")
db.execute("CREATE INDEX ix_email ON users (email)")
db.execute("BEGIN")
for i in range(1, 1001):
    db.execute("INSERT INTO users VALUES (?, ?, ?)", (i, f"user{i}@example.com", 20 + i % 40))
db.execute("COMMIT")
db.execute("ANALYZE")

print(db.execute("EXPLAIN SELECT id FROM users WHERE email = ?", ("user7@example.com",)).fetchall()[0][0])
print(db.execute("SELECT age, COUNT(*) FROM users GROUP BY age ORDER BY age LIMIT 3").fetchall())
db.close()

# The file is a real SQLite database: the standard library's sqlite3 checks it.
# CI runs this script, so a corrupt file fails the build.
result = sqlite3.connect(path).execute("PRAGMA integrity_check").fetchone()[0]
print(result)
assert result == "ok", result
```

```text
Project
└─ IndexScan ix_email (email = 'user7@example.com') est_rows=1 startup=8.00 cost=16.01
[(20, 25), (21, 25), (22, 25)]
ok
```

`EXPLAIN` shows the plan the cost-based planner chose; `EXPLAIN ANALYZE` runs it and adds
`actual_rows`, `pages_read`, `pages_cached` and `rows_examined`.

## What's inside

Each layer is a client of the one below it, with no upward dependencies:

```
api/         connect() / Connection / Cursor — the DB-API-shaped surface
exec/        pull-based operators: SeqScan, IndexScan, Filter, Project, Join, Sort, Aggregate, ...
plan/        access-path selection, the cost model, join-order search, ANALYZE, EXPLAIN
catalog/     sqlite_schema, name resolution
sql/         tokenizer -> recursive-descent parser -> AST -> binder
txn/         undo journal, crash recovery, lock manager, deadlock detection
btree/       table b-trees (keyed on rowid) and index b-trees (a TRUE b-tree)
codec/       varints, the manifest-then-body record format
storage/     pager, slotted pages, pin-counted LRU buffer pool, overflow chains
```

One distinction worth pulling out, because it is the single most expensive thing to get wrong here:
a **table** b-tree is a B+tree — interior cells are `[child][rowid]`, pure routing, and every row
lives in a leaf. An **index** b-tree is a *true* b-tree — an interior cell carries a full record and
**is a live entry**. A leaf split must therefore *consume* its separator rather than copy it, scans
must emit interior entries, and deleting one is the classic delete-from-an-internal-node problem. Get
it wrong and quilldb stays perfectly consistent with itself while `sqlite3` reports
`wrong # of entries in index`.

## Features

| | |
|---|---|
| ✅ | `CREATE TABLE` (`INTEGER`, `REAL`, `TEXT`, `BLOB`), `CREATE [UNIQUE] INDEX` (multi-column) |
| ✅ | `INSERT`, `UPDATE`, `DELETE`, with `?` parameters |
| ✅ | `SELECT` with `WHERE`, `LIKE`, `IS [NOT] NULL`, `ORDER BY [DESC]`, `LIMIT` / `OFFSET`, `DISTINCT` |
| ✅ | `GROUP BY`, `HAVING`, `COUNT` / `SUM` / `AVG` / `MIN` / `MAX` |
| ✅ | `INNER` and `LEFT JOIN`, comma joins, nested-loop with exhaustive join-order search |
| ✅ | Cost-based planner: sequential scan vs index, using statistics `ANALYZE` measures off the b-trees |
| ✅ | `EXPLAIN` and `EXPLAIN ANALYZE` (with page-read counters) |
| ✅ | Transactions: `BEGIN` / `COMMIT` / `ROLLBACK`, atomic and durable via an undo journal, crash recovery |
| ✅ | Threads: one `Connection` per thread, table-level strict 2PL, deadlock detection |
| ✅ | Overflow pages for large values; freelist; real SQLite file header |
| ❌ | See [Not implemented](#not-implemented) |

## Benchmarks

Per [chapter 19](docs/theory/benchmarks/19-measuring-it.md): **page reads are the headline, time is
context.** Page reads are what the access path determines; they don't depend on this laptop's thermal
state, and nobody has to wonder whether a warm cache was being measured. A page read here is exactly a
buffer-pool **miss**, on a cold pool (a fresh connection per row). The OS page cache is not dropped
(that needs root), so milliseconds reflect a warm OS cache.

```console
$ python -m quilldb.bench            # all of them, as markdown (a few minutes)
$ python -m quilldb.bench --list
```

### An index, and what it costs

100,000 rows, 4096-byte pages:

| Access path | Page reads | Rows examined | Time |
|---|---:|---:|---:|
| `SeqScan` + `Filter` | 2,622 | 100,000 | 939 ms |
| `IndexScan ix_email` | **7** | **1** | **1.1 ms** |

**375× fewer page reads.** Check the arithmetic: 100,000 rows × ~100 bytes ÷ 4096 ≈ 2,441 pages,
against 2,622 measured — the difference is page headers and cell pointers. The 7 is two b-tree
descents, not one: the index is three levels deep at this size and so is the table, because an index
lookup finds a *rowid* and then still has to go get the row.

The half that's easy to leave out — every index is a tax on every write (inside one transaction, so
this is index maintenance, not one `fsync` per row):

| Indexes | ms per `INSERT` | |
|---:|---:|---|
| 0 | 0.161 | 1.00× |
| 1 | 0.321 | 1.99× |
| 3 | 0.633 | 3.93× |

### More measurements

Unflattering rows are published on purpose.

| Question | Result |
|---|---|
| How deep is a table b-tree? | Height **2** at 1,000 rows, **3** at 100,000 (measured); **4** at 10,000,000 (*computed* from the measured 53.6 rows per leaf and 312 children per interior page — not built) |
| Sequential vs random inserts, 20,000 rows, 64-page pool | **1** page read vs **12,259**. Cold search of every key: 363 vs 16,580. Measures `BTree.insert`, not the SQL `INSERT` (the parser cannot choose a rowid) |
| Buffer pool size vs hit rate, Zipf-skewed lookups | 70.7% at 8 pages, 88.2% at 64, 93.7% at 128, 99.5% at 512 — a gentle curve, not a cliff. Uniform lookups give a sharp knee near 512 |
| What does a big scan do to a warm pool? | Hit rate on a 20-id hot set: 100% before, **67.5%** on the first pass after a full-table scan, 100% once reloaded |
| `LIMIT 1` over 100,000 rows | **4** page reads. `ORDER BY … LIMIT 1` reads **1,990** — the whole table — because a sort has to see every row |
| Join order, 20 × 5,000 rows | Big table outer: **388** page reads. Small table outer: **7,703** (20×), though both examine ~100,000 rows. INNER joins are reordered to the cheap side automatically; a `LEFT JOIN` pins the written order |
| What would a covering index save? | quilldb has **no** index-only path (an `IndexScan` always fetches the row). Walking the index alone reads 3 pages where the engine reads 7 (2.3×) for one row |

### Concurrency

Table-level strict two-phase locking, one writer at a time, deadlocks detected (not prevented) via a
wait-for graph — see [`docs/concurrency.md`](docs/concurrency.md) for the isolation level and what it
permits. Read and write throughput are reported separately because they hit different ceilings:

| Threads | Read txn/sec | Write txn/sec |
|---:|---:|---:|
| 1 | 5,236 | 73.2 |
| 2 | 4,859 | 68.3 |
| 4 | 4,060 | 65.8 |
| 8 | 3,645 | 60.3 |

**Neither column climbs, for two different reasons.** Writes are flat *structurally*: every write
transaction takes a global `EXCLUSIVE` lock before touching any table, so exactly one commit is ever in
flight, and each commit pays for ~4 real `fsync()` calls — more threads submitting writes can't raise
that ceiling. Reads never queue behind each other (`SHARED` locks coexist), but the read column
doesn't climb either, because quilldb is pure Python: CPython's GIL runs one thread's bytecode at a
time, and a read here is pure CPU work with nothing to release the GIL for, so threads add scheduling
overhead instead of parallelism. The lock manager isn't the read ceiling — the interpreter is.

## Not implemented

Named, not hidden — specific, because specific limits read as engineering judgement.

**On-disk format.** quilldb *writes* SQLite's format and `PRAGMA integrity_check` passes. It does not
*read* arbitrary sqlite3-written files: no freeblock parsing, no WAL, no auto-vacuum pointer maps, no
page sizes other than 4096, no UTF-16. `FileHeader.check_supported()` refuses those explicitly rather
than misreading them.

**B-tree.** Two-way splits, no sibling merging for tables. Pages are freed when they empty, so a
delete-heavy workload leaves the file larger than optimal until the space is reused. Still a valid
tree — occupancy is not a format constraint. `VACUUM` and `auto_vacuum` are not implemented.

**Transactions.** Undo journal only, no WAL — so readers cannot run concurrently with a writer.

**Query processing.** Nested-loop joins only. Cost-based planning uses `quill_stat1` prefix averages
rather than histograms, so skew and correlated columns can produce bad estimates. Join-order search is
exhaustive (every legal left-deep order), which is only cheap because the tests never join more than
three tables (3! = 6 orders) — the grammar has no such limit, so a longer chain still runs, just
without the same search-cost guarantee. There is no covering-index (index-only) scan. `ORDER BY` sorts
in memory and raises `SortLimitExceededError` past 1,000,000 rows. `GROUP BY` / `HAVING` / `DISTINCT`
combined with a `JOIN` is rejected by the binder rather than silently returning unaggregated rows.
The planner has no skip-scan, `OR` decomposition or multi-index intersection.

**Concurrency.** Threads in one process. No multi-process locking; a second process opening the same
file is undefined.

**SQL.** No `IN`, `BETWEEN`, subqueries, CTEs, `UNION`, `CASE`, scalar functions (`UPPER`, `LENGTH`, …),
`COUNT(DISTINCT …)`, `RIGHT` / `FULL JOIN`, `DROP` / `ALTER TABLE`, views, triggers, foreign keys,
`CHECK`, `NOT NULL` / `PRIMARY KEY` / `DEFAULT` column constraints, `AUTOINCREMENT`,
`INSERT OR REPLACE` / `ON CONFLICT`, `WITHOUT ROWID`, `COLLATE`, or `DESC` / partial / expression
indexes. Every rowid is assigned automatically.

Feature gaps and format fidelity are different claims. Missing features are gaps; anything that
touches on-disk bytes has to match the real format exactly, because a real SQLite binary reads it
back.

## Deviations from SQLite

Not gaps — implemented, tested, and intentionally stricter than the reference, each in the same
direction (refuse rather than silently coerce or guess):

| quilldb | Real SQLite | Why |
|---|---|---|
| `INSERT INTO t VALUES ('42')` into an `INTEGER` column raises `TypeMismatchError` | Casts `'42'` to `42` (type affinity) | Coercing text into a number silently turns garbage input into a plausible-looking value instead of an error (`sql/binder.py`'s `_coerce_to_declared_type`) |
| `1 + 'a'` raises `TypeMismatchError` | `1` (text casts to `0`) | Same reasoning, for expressions instead of storage (`exec/expressions.py`) |
| `WHERE '1'` treats all text as false | Treats `'1'` as true | Implementing SQLite's numeric-string affinity for `WHERE` but not elsewhere would be an inconsistent half-measure (`exec/expressions.py`) |
| `SELECT id, COUNT(*) FROM t` (no `GROUP BY`) raises `AggregateError` | Returns `COUNT(*)` alongside an arbitrary row's `id` | A bare column with no `GROUP BY` key to be functionally determined by is ambiguous the moment the table has more than one row — silently picking one row's value is how a query returns a plausible wrong answer (`sql/binder.py`'s `bind_aggregate_select`, week7-query-processing.md §40) |
| An `ORDER BY` sorting more than 1,000,000 rows raises `SortLimitExceededError` | Spills to a temp file and keeps sorting | `exec/sort.py`'s `Sort` holds every row in memory (`MAX_SORT_ROWS`); refusing past a documented cap with an actionable message ("add an index on the `ORDER BY` column") beats risking an OOM kill |

A differential test that disagrees with sqlite3 on one of these rows is expected behavior, not a
bug — that's what distinguishes a documented deviation from an undocumented one.

## Testing

1,664 tests, run with `pytest` (the 71 slow ones are excluded by default; `pytest -m slow` adds them):

| Suite | Tests | What it does |
|---|---:|---|
| `src/tests/unit/` | 1,264 | Each layer in isolation, from varints up to the planner |
| `src/tests/differential/` | 287 | The same SQL through quilldb and real `sqlite3`, results diffed |
| `src/tests/fault_injection/` | 83 | Crashes injected at every write point of a commit and of recovery (65 of these are slow) |
| `src/tests/concurrency/` | 30 | Multi-threaded transfers, deadlocks, lock behaviour |

```console
$ pytest                                # full suite, slow tests excluded
$ pytest -m slow                        # includes 100k-row cases and the full crash matrix
$ pytest src/tests/differential/        # diff against real sqlite3
$ ruff check . && mypy                  # mypy is strict over src/quilldb
$ quilldb inspect mydata.db             # dump a file's header
```

## Design decisions

- [`docs/design_decisions.md`](docs/design_decisions.md) — one page: what's the same as SQLite, what's
  different on purpose, what was cut, and why.
- [`docs/decisions/`](docs/decisions/) — one ADR per deliberate departure from the obvious design, each
  with context, decision, consequences and the alternatives that were rejected:

  | ADR | Decision |
  |---|---|
  | [001](docs/decisions/ADR-001-bufferpool-raw-pages.md) | The buffer pool caches raw page bytes, not parsed pages |
  | [002](docs/decisions/ADR-002-sqlite-format-one-direction.md) | SQLite's exact on-disk format, in one direction only |
  | [003](docs/decisions/ADR-003-iterator-pipeline-not-bytecode.md) | A pull-based iterator pipeline, not a bytecode VM |
  | [004](docs/decisions/ADR-004-undo-journal-not-wal.md) | An undo journal, not a write-ahead log |
  | [005](docs/decisions/ADR-005-no-sibling-merging.md) | Free empty pages, but never merge underfull siblings |
  | [006](docs/decisions/ADR-006-table-level-2pl-with-deadlock-detection.md) | Table-level strict 2PL with deadlock detection |
  | [007](docs/decisions/ADR-007-cost-model-and-exhaustive-join-search.md) | A page-cost model and exhaustive left-deep join search |
  | [008](docs/decisions/ADR-008-repack-pages-on-delete.md) | Repack a page on delete instead of keeping freeblocks |
  | [009](docs/decisions/ADR-009-hash-aggregation.md) | Hash aggregation, where SQLite sorts |

## Documentation

| Doc | Answers |
|---|---|
| `roadmap.md` | *What* to build, week by week |
| `guide.md` | What this thing *is*, in plain language |
| `docs/theory/` | *Why* it's shaped that way — mirrors `src/quilldb/` package for package |
| `docs/implementation/` | Exact contracts and the tests that define "done" |
| [`docs/architecture.md`](docs/architecture.md) | How the layers fit, and one query traced end to end |
| [`docs/file-format.md`](docs/file-format.md) | What is on disk, and what is refused |
| [`docs/durability.md`](docs/durability.md) | Why a crash cannot corrupt it, and what is not guaranteed |
| [`docs/concurrency.md`](docs/concurrency.md) | The isolation level, and what it permits |
| [`docs/demo.md`](docs/demo.md) | The demo: shot list with real output, and the five-minute talk |
| `docs/decisions/` | ADRs for deliberate departures from the obvious design |
