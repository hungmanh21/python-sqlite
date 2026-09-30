# Architecture: how the pieces fit

quilldb is a stack of layers. Each is a package under `src/quilldb/`, and each is a client of the layer
below. That is why the bottom layers can be tested with nothing above them. There are two deliberate
exceptions, both checked against the imports: `storage/bufferpool.py` names `Transaction` for type hints
only (a `TYPE_CHECKING` import, nothing at runtime), and `catalog/` uses `sql/`'s parser, because the
catalog stores the original `CREATE` text and re-parses it on open, while the binder in `sql/` in turn
reads the catalog. The parser itself never touches the catalog.

```
                 quilldb.connect(path)
                          │
  api/         Connection · Cursor · Database          the public surface
                          │
  sql/         tokenizer → parser → binder             text → AST → bound tree
  catalog/     Catalog (reads sqlite_schema)           names → tables, columns, indexes
  plan/        predicates · planner · cost · search    pick an access path and join order
  exec/        operators · expressions · join · sort   pull-based iterator tree
                          │
  txn/         Transaction · LockManager · Journal     atomicity, isolation, recovery
                          │
  btree/       BTree · IndexBTree · cursor · validate  table and index trees
  codec/       varint · record                         rows ↔ bytes
                          │
  storage/     BufferPool · Pager · page · header      4096-byte pages on disk
                          │
                    app.db  +  app.db-journal
```

Alongside the stack: [`errors.py`](../src/quilldb/errors.py) (the exception hierarchy),
[`constants.py`](../src/quilldb/constants.py) (format constants), and the tools that sit on top:
[`cli.py`](../src/quilldb/cli.py), [`shell.py`](../src/quilldb/shell.py), and
[`bench/`](../src/quilldb/bench/).

## The layers, bottom to top

| Layer | Owns | Does *not* know about |
|---|---|---|
| **`storage/`** | Fixed-size numbered pages ([`pager.py`](../src/quilldb/storage/pager.py), including the freelist), the 100-byte header, slotted-page parse/serialize, overflow chains, and a pin-counted LRU cache of **raw page bytes** ([`bufferpool.py`](../src/quilldb/storage/bufferpool.py)) | What a page means. It hands out bytes. |
| **`codec/`** | SQLite's varint and record encoding | Pages |
| **`btree/`** | Search, insert, delete, split, cursors and validators for table trees (keyed by rowid) and index trees (keyed by column values, rowid appended) | SQL, types beyond the record codec |
| **`txn/`** | The rollback journal, table-level shared/exclusive locks with deadlock detection, crash recovery, and the `Transaction` that ties them together | What a statement does |
| **`sql/`** | Tokenizer, hand-written recursive-descent parser, and the binder that resolves names against the catalog and checks types | Storage. It produces trees, not rows. |
| **`catalog/`** | `sqlite_schema` (page 1) as an in-memory `Catalog` of tables, columns and indexes | |
| **`plan/`** | Predicate analysis, access-path enumeration, page-count cost model, statistics from `ANALYZE`, exhaustive join-order search, a plan cache | Executing anything |
| **`exec/`** | The operators (`SeqScan`, `IndexScan`, `Filter`, `Project`, `Sort`, `Limit`, joins, aggregates, `Insert`/`Update`/`Delete`), and expression evaluation with three-valued NULL logic | |
| **`api/`** | `connect`, `Connection.execute`, `Cursor`, and the `Database` that connections share | |

Three design decisions shape the seams. Each has an ADR:

- **The buffer pool caches raw bytes, and callers decode** ([ADR-001](decisions/ADR-001-bufferpool-raw-pages.md)).
  The B-tree layer parses a page when it needs one. That mirrors SQLite's own pcache/btree split.
- **Operators are an iterator pipeline, not bytecode** ([ADR-003](decisions/ADR-003-iterator-pipeline-not-bytecode.md)).
  Each operator has `open`, `next`, `close`, and nothing materializes a full result set.
- **Format compatibility goes one direction** ([ADR-002](decisions/ADR-002-sqlite-format-one-direction.md)).
  See [file-format.md](file-format.md) for what that means on disk.

## `Database` and `Connection`

A [`Database`](../src/quilldb/api/database.py) is what all connections to one file share: the `Pager`,
the `BufferPool`, the `Catalog`, the statistics catalog, the plan cache, and the `LockManager`. Opening
one runs crash recovery first. A `Connection` is one client's view of it, with its own transaction state
and at most one open cursor.

The pool is therefore per-database, not per-connection: `quilldb.connect(path, pool_capacity=N)` sets
the capacity for the database, and every connection to it shares those pages. A connection is tied to the thread
that created it; concurrent work uses separate connections, one per thread
([concurrency.md](concurrency.md)).

## One query, end to end

Take `SELECT name FROM users WHERE email = 'u77@x.com'`, with an index on `email`.

**1. `Connection.execute(sql)`** ([`api/connection.py`](../src/quilldb/api/connection.py)) closes any
cursor still open on this connection, then parses the text. `BEGIN`, `COMMIT` and `ROLLBACK` are handled
right here and never reach the binder.

**2. Locks.** Every other statement takes the `__schema__` lock before it reads the catalog: shared for
a query, exclusive for DDL. If another connection's committed DDL bumped the header's schema cookie, the
shared `Catalog` is reloaded first.

**3. `parse` → `bind`.** The parser ([`sql/parser.py`](../src/quilldb/sql/parser.py)) yields an AST with
unresolved names. The binder ([`sql/binder.py`](../src/quilldb/sql/binder.py)) turns it into a `Bound*`
tree: it resolves `users` and `email` against the catalog, coerces literals to declared types, and rejects
what cannot be right (unknown columns, type mismatches, a bare column beside an aggregate).

**4. Plan.** `build_operator` ([`exec/operators.py`](../src/quilldb/exec/operators.py)) asks the planner
for the cheapest access path. `plan/planner.py` enumerates them (a full scan, plus one per usable index),
`plan/cost.py` prices each **in pages**, and `plan/statistics.py` supplies row estimates from `ANALYZE`'s
`quill_stat1` table, or defaults when there is none. For joins, `plan/search.py` searches join orders
exhaustively. A single-table plan is cached by SQL text and schema cookie
([ADR-007](decisions/ADR-007-cost-model-and-exhaustive-join-search.md)).

**5. Operators.** The result is a tree; here it is a `Project` over an `IndexScan`. Real output, on a
2,000-row table, from `EXPLAIN ANALYZE`:

```text
Project
└─ IndexScan ix_email (email = 'u77@x.com') est_rows=10 startup=12.00 cost=132.10
actual_rows=1 elapsed=0.000387s pages_read=4 pages_cached=1 rows_examined=1
```

`pages_read` counts buffer-pool **misses**, which are the reads that cost I/O. `est_rows=10` next to
`actual_rows=1` is the planner working from defaults, since `ANALYZE` was not run.

**6. Rows are pulled, not pushed.** `execute()` does not drain a `SELECT`. It returns a `Cursor` over the
open operator, and `fetchone()` calls `next()` down the tree. `Project.next()` asks `IndexScan.next()`,
which seeks the index B-tree (`IndexBTree.seek_range`), gets a rowid, then searches the table B-tree
(`BTree.search`) for the row.

**7. Pages.** Each B-tree step calls `BufferPool.get_page(n)`. A hit returns the cached bytes. A miss asks
the `Pager` to read page *n* from the file at offset `(n - 1) * 4096`, evicting the least-recently-used
unpinned page if the pool is full. The tree parses the bytes into a `PageBody`, follows the cell it wants,
and unpins the page. Nothing above the pool ever sees a file offset.

**8. Cleanup.** Draining or closing the cursor closes the operators, releasing pins and, in autocommit,
ending the statement's transaction and its locks.

### A write takes the same path plus a transaction

`INSERT`, `UPDATE`, `DELETE` and DDL are wrapped in a [`Transaction`](../src/quilldb/txn/transaction.py),
implicit unless `BEGIN` was used. The first time a statement is about to modify a page that already
existed, the **original page image** is recorded in the journal, and a table lock is taken (table locks
are two-phase: held until commit or rollback). Dirty pages stay in the pool: it never writes a
transaction's dirty page out early ("no-steal"), so a big enough transaction grows the pool past its
capacity for as long as it runs. `COMMIT` then runs, in order:

1. Sync the journal body, then stamp its magic and record count and sync again. Only now is the journal
   *valid*.
2. Flush the dirty pages and sync the database file.
3. Delete the journal. **That deletion is the commit point.**

A crash before step 3 leaves a hot journal, and the next `Database` open rolls the file back to the
pre-transaction state. The full argument, and what is *not* guaranteed, is in
[durability.md](durability.md).

## Errors say who is at fault

Every exception in [`errors.py`](../src/quilldb/errors.py) falls on one side of a single line. Anything
deriving from `CorruptDatabaseError` means **the file on disk is bad**. Everything else means the caller
gave bad input, or is internal control flow (`PageFullError` triggers a split; `DuplicateRowIDError` is
caught by callers that need it). Keeping that split is how a shell can print `Error: ...` and carry on for
bad SQL, while `quilldb validate` reports a bad file as a file problem.

## Where to go next

| To learn | Read |
|---|---|
| What is on disk | [file-format.md](file-format.md) |
| Why a crash cannot corrupt it | [durability.md](durability.md) |
| What is guaranteed under threads | [concurrency.md](concurrency.md) |
| Why each design is shaped that way | [docs/theory/](theory/README.md), which mirrors `src/quilldb/` package for package |
| The decisions that departed from the obvious | [docs/decisions/](decisions/) |
| What the engine costs, measured | the README's benchmark section, or `python -m quilldb.bench` |
