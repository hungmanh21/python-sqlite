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
`CHECK`, `INSERT OR REPLACE` / `ON CONFLICT`, and — in the planner — skip-scan, `OR`
decomposition and multi-index intersection.

Feature gaps and format fidelity are different claims. Missing features are gaps; anything that
touches on-disk bytes has to match the real format exactly, because a real SQLite binary reads
it back.

## Documentation

| Doc | Answers |
|---|---|
| `roadmap.md` | *What* to build, week by week |
| `guide.md` | What this thing *is*, in plain language |
| `docs/theory/` | *Why* it's shaped that way — mirrors `src/quilldb/` package for package |
| `docs/implementation/` | Exact contracts and the tests that define "done" |
| `docs/decisions/` | ADRs for deliberate departures from the obvious design |
