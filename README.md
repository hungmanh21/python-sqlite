# quilldb

A relational database engine written from scratch in pure Python — no ORM, no `sqlite3` module,
no parser generator — that writes **the real SQLite on-disk file format, byte for byte**.

Not a format *inspired by* SQLite. The actual `"SQLite format 3\000"` header, page layout,
varints, record encoding and type codes, copied from
[the format spec](https://sqlite.org/fileformat2.html) rather than invented. The acceptance test
is therefore not one quilldb can grade itself on:

```console
$ sqlite3 mydata.db "PRAGMA integrity_check"
ok
```

Real `sqlite3` opens a file quilldb wrote, walks every b-tree, counts every index entry against
its table, and agrees. `src/tests/differential/` goes further: it runs the same SQL through both
engines and diffs the results.

## The benchmark

Same query, same data, with and without an index. **Page reads**, not seconds — page reads are
what the access path determines, they don't depend on this laptop's thermal state, and nobody
has to wonder whether a warm cache was being measured.

100,000 rows, 4096-byte pages, cold buffer pool (a fresh connection per run):

| Access path | Page reads | Rows examined | Time |
|---|---:|---:|---:|
| `SeqScan` + `Filter` | 2,622 | 100,000 | 958 ms |
| `IndexScan ix_email` | **7** | **1** | **0.7 ms** |

**375× fewer page reads.** Check the arithmetic: 100,000 rows × ~100 bytes ÷ 4096 ≈ 2,441 pages,
against 2,622 measured — the difference is page headers and cell pointers. The 7 is two b-tree
descents, not one: the index is three levels deep at this size and so is the table, because an
index lookup finds a *rowid* and then still has to go get the row.

A page read here is exactly a buffer-pool **miss**. The OS page cache is not dropped (that needs
root), so the milliseconds reflect a warm OS cache and are context, not the claim.

And the half that's easy to leave out — every index is a tax on every write:

| Indexes | ms per `INSERT` | |
|---|---:|---|
| 0 | 0.1495 | 1.00× |
| 1 | 0.3238 | 2.17× |
| 3 | 0.6252 | 4.18× |

```console
$ python benchmarks/index_lookup.py
```

## Concurrency

Table-level strict two-phase locking, one writer at a time, deadlocks detected (not prevented) via
a wait-for graph — see [`docs/concurrency.md`](docs/concurrency.md) for the isolation level and
what it permits. Read and write throughput are reported separately because they hit different
ceilings:

| Threads | Read txn/sec | Write txn/sec |
|---:|---:|---:|
| 1 | 4,983 | 45.5 |
| 2 | 4,455 | 49.6 |
| 4 | 3,928 | 46.8 |
| 8 | 3,321 | 48.3 |

**Neither column climbs, for two different reasons.** Writes are flat *structurally*: every write
transaction takes a global `EXCLUSIVE` lock before touching any table, so exactly one commit is
ever in flight, and each commit pays for ~4 real `fsync()` calls — more threads submitting writes
can't raise that ceiling. Reads never queue behind each other (`SHARED` locks coexist), but the
read column doesn't climb either, because quilldb is pure Python: CPython's GIL runs one thread's
bytecode at a time, and a read here is pure CPU work with nothing to release the GIL for, so
threads add scheduling overhead instead of parallelism. The lock manager isn't the read ceiling —
the interpreter is.

```console
$ python benchmarks/concurrent.py
```

## What works

```sql
CREATE TABLE users (id INTEGER, email TEXT, age INTEGER);
INSERT INTO users VALUES (1, 'ada@example.com', 36);
SELECT name, age + 1 FROM users WHERE age > ? AND name LIKE 'a%';
UPDATE users SET age = age + 1 WHERE id = 4;
DELETE FROM users WHERE age < 18;
CREATE UNIQUE INDEX ix_email ON users (email);
ANALYZE;
EXPLAIN SELECT * FROM users WHERE email = ?;
```

A hand-written tokenizer and recursive-descent parser, a binder that resolves names against a
self-describing catalog (page 1 is both the file header's home *and* the root of the
`sqlite_schema` table), a **cost-based planner** that picks between a sequential scan and an
index using statistics `ANALYZE` measures off the real b-trees, and pull-based iterator
operators where nothing materialises a full result set.

```console
$ python -c "
import quilldb
db = quilldb.connect('mydata.db')
db.execute('CREATE TABLE users (id INTEGER, email TEXT)')
db.execute('CREATE INDEX ix ON users (email)')
db.execute('INSERT INTO users VALUES (?, ?)', (1, 'ada@example.com'))
print(db.execute('EXPLAIN SELECT id FROM users WHERE email = ?', ('ada@example.com',)).fetchall()[0][0])
"
Project
└─ IndexScan ix (email = 'ada@example.com') est_rows=10 startup=12.00 cost=132.10
```

## Architecture

Each layer is a client of the one below it, with no upward dependencies:

```
api/         connect() / Connection / Cursor — the DB-API-shaped surface
exec/        pull-based operators: SeqScan, IndexScan, Filter, Project, Insert, ...
plan/        access-path selection, the cost model, ANALYZE, EXPLAIN
catalog/     sqlite_schema, name resolution
sql/         tokenizer -> recursive-descent parser -> AST
btree/       table b-trees (keyed on rowid) and index b-trees (a TRUE b-tree)
codec/       varints, the manifest-then-body record format
storage/     pager, slotted pages, pin-counted LRU buffer pool, overflow chains
```

One distinction worth pulling out, because it is the single most expensive thing to get wrong
here: a **table** b-tree is a B+tree — interior cells are `[child][rowid]`, pure routing, and
every row lives in a leaf. An **index** b-tree is a *true* b-tree — an interior cell carries a
full record and **is a live entry**. A leaf split must therefore *consume* its separator rather
than copy it, scans must emit interior entries, and deleting one is the classic
delete-from-an-internal-node problem. Get it wrong and quilldb stays perfectly consistent with
itself while `sqlite3` reports `wrong # of entries in index`.

## Running it

```console
$ pytest                                # full suite (slow tests excluded)
$ pytest -m slow                        # includes 100k-row cases
$ pytest src/tests/differential/        # diff against real sqlite3
$ ruff check . && mypy                  # mypy is strict over src/
$ quilldb inspect mydata.db             # dump a file header
```

## Not implemented

Named, not hidden: WAL, `VACUUM`, `DROP TABLE` / `DROP INDEX` / `ALTER TABLE`, partial and
expression indexes, `COLLATE`, `DESC` indexes, `WITHOUT ROWID`, `AUTOINCREMENT`, foreign keys,
`CHECK`, `INSERT OR REPLACE` / `ON CONFLICT`, `GROUP BY` / `HAVING` / `DISTINCT` combined with a
`JOIN` (`sql/binder.py`'s `bind_join_select` rejects it explicitly rather than silently returning
unaggregated rows), and — in the planner — skip-scan, `OR` decomposition and multi-index
intersection.

Feature gaps and format fidelity are different claims. Missing features are gaps; anything that
touches on-disk bytes has to match the real format exactly, because a real SQLite binary reads
it back.

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

The join-order search (`plan/search.py`'s `enumerate_join_plans`) enumerates every legal order
exhaustively rather than pruning, which is only "obviously cheap" (at most `3! = 6` orders) because
this project's tests never join more than three tables — the SQL grammar itself has no such limit,
so a longer chain still parses and runs, just without the same search-is-basically-free guarantee
or test coverage past three.

## Documentation

| Doc | Answers |
|---|---|
| `roadmap.md` | *What* to build, week by week |
| `guide.md` | What this thing *is*, in plain language |
| `docs/theory/` | *Why* it's shaped that way — mirrors `src/quilldb/` package for package |
| `docs/implementation/` | Exact contracts and the tests that define "done" |
| `docs/decisions/` | ADRs for deliberate departures from the obvious design |
