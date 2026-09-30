# Concurrency in quilldb

The isolation level was named before `txn/locks.py` was written, per
[chapter 15 §15.8](theory/txn/15-isolation-anomalies.md#158-what-youre-building-this-week): deciding
it first is what forces the choices that are painful to retrofit, like "when do read locks get
released". The implementation now exists and the checklist at the bottom records what was confirmed
against it. The design rationale is [ADR-006](decisions/ADR-006-table-level-2pl-with-deadlock-detection.md).

## Isolation level

**Serializable**, via table-level strict two-phase locking with a single writer.

Not "we're SERIALIZABLE" as a claim to take on faith — the rest of this document is the argument for
it, checkable against `txn/locks.py` and `txn/transaction.py`.

## What that means concretely

- **A transaction's reads are repeatable.** A `SHARED` lock on a table is held until commit, so no
  other transaction can acquire `EXCLUSIVE` on it in the meantime. A row read twice in the same
  transaction reads the same value both times, because nothing could have changed it in between.
- **Phantoms cannot occur.** A `SHARED` lock is on the whole table, not on the rows a query happened
  to match. A concurrent `INSERT` needs `EXCLUSIVE` on that same table and must wait, so no row can
  appear in a range another transaction already scanned. This is the one anomaly that usually needs
  gap locks or MVCC to eliminate, and table granularity gets it for free (chapter 15 §15.1).
- **Two transactions writing different tables serialize anyway.** Every write transaction takes the
  global `"__writer__"` lock, `EXCLUSIVE`, before taking any table lock (§35 of the week-6 plan). That
  is strictly more serialization than the isolation level requires — two transactions touching
  disjoint tables could in principle run concurrently — and it is a deliberate trade, not an
  oversight: see "Why a single writer" below.
- **Deadlocks are detected, not prevented.** A wait-for graph is checked whenever a transaction blocks;
  a cycle aborts the youngest transaction in it with `DeadlockError`, and the survivor is guaranteed to
  commit. That holds whichever member of the cycle closes it: if the transaction that completes the
  cycle isn't the youngest, it marks the victim and wakes it (NOTES.md B6-5), rather than leaving the
  victim asleep until its own timeout. A `busy_timeout` is a backstop for waits the graph doesn't model,
  not a substitute for detection (chapter 16 §16.4).
- **A deadlock victim inside `BEGIN` must `ROLLBACK`.** Only the failing statement is abandoned; the
  transaction keeps its locks until the caller rolls it back, and the survivor waits until then. In
  autocommit mode the statement's own transaction is rolled back automatically.
- **Schema changes are serializable too.** Every statement takes `"__schema__"` before it binds —
  `SHARED` normally, `EXCLUSIVE` for `CREATE TABLE`/`CREATE INDEX` — so no connection can bind against,
  plan against, or reload the catalog from an uncommitted DDL (NOTES.md B6-7).

## What it permits

- **No isolation between statements on the same connection.** A connection sees its own uncommitted
  writes immediately — `BEGIN; UPDATE t ...; SELECT ...` on that same connection sees the update. This
  is not a bug; it is the same rule SQLite documents (chapter 15 §15.4), and it is why a cursor must
  never be mutated while it is being iterated on the same connection: collect rowids first, then act.
- **A read-only (autocommit) statement still takes a lock.** `SELECT` outside an explicit `BEGIN` opens
  an implicit transaction that takes `SHARED` and releases at its own commit. Skipping this for "just a
  read" is exactly how a read would observe a writer's half-applied page — see §37.2 of the week-6
  plan for why that transaction must also never touch the journalling hook.
- **Sharing one `Connection` across threads is rejected outright**, not merely unsupported: it raises
  `ThreadingError` rather than corrupting silently. It is not an isolation anomaly, but it is the other
  way this document's guarantees would otherwise be void.
- **Multi-process access is undefined.** There is no `fcntl`, no advisory file locking. A second
  process opening the same file is a documented limitation, not a supported degraded mode.

## Lock order

Every transaction acquires locks in one fixed order, and that order is what keeps the wait-for graph
small enough for the youngest-victim rule to be sound:

```
"__schema__"  →  "__writer__"  →  tables
```

- **`"__schema__"`**: `SHARED` for every statement except `BEGIN`/`COMMIT`/`ROLLBACK`, taken before
  binding; `EXCLUSIVE` for DDL. An explicit transaction holds it to `COMMIT` (strict 2PL), an autocommit
  `SELECT` until its cursor is drained or closed, so DDL waits for open result sets — the same rule
  SQLite enforces with `SQLITE_LOCKED` on a schema change under an active statement.
- **DDL takes `"__schema__"` `EXCLUSIVE` *before* `"__writer__"`.** The other order would deadlock every
  DDL against every concurrent writer: the DDL holding `"__writer__"` waits for readers' `SHARED`
  `"__schema__"`, while any of those readers that wants to write waits for `"__writer__"`.
- **`BEGIN IMMEDIATE`** takes `"__schema__"` `SHARED` and then `"__writer__"`, for the same reason.
- **A transaction's rollback point is taken when it first acquires `"__writer__"`, not at `BEGIN`.**
  Holding `"__writer__"` is what stops `page_count` moving; a snapshot taken any earlier misses pages
  other writers committed in between, and rolling back can't undo writes to them (NOTES.md B6-2).

## Why table granularity

Row-level locking is the natural next step and a materially bigger one: as soon as individual rows are
lockable, phantoms come back, because you can't lock a row that doesn't exist yet — you'd need gap or
predicate locks to get phantom protection back (chapter 15 §15.1, chapter 16 §16.1). Table granularity
is the point where strong isolation is still cheap: two lock modes, one dict entry per table, and
phantom protection falls out of the granularity itself rather than needing separate machinery.

The cost is real and worth stating plainly: two transactions writing disjoint rows of the *same* table
serialize even though they don't logically conflict. That is the price of the sweet spot, not a flaw
in the implementation of it.

## Why a single writer (the `__writer__` lock)

A transaction that reads a table `SHARED` and later needs to write it must upgrade to `EXCLUSIVE`. Two
transactions both holding `SHARED` and both wanting to upgrade deadlock on a *single* resource neither
of them ever shared with a second table — a lock-upgrade deadlock, and the kind of cycle that's easy to
miss if the wait-for graph is only built to think in terms of distinct resources.

quilldb sidesteps the entire class: every write transaction acquires `"__writer__"` `EXCLUSIVE` before
acquiring any table lock. Since at most one transaction is ever a writer at all, no two transactions
ever contend for an upgrade. This is three lines and it makes the single-writer guarantee *structural*
rather than emergent — and it is the same trade SQLite itself makes in rollback-journal mode, just at
finer granularity underneath it (see below).

## Why this differs from SQLite

quilldb's table-level locks are **finer-grained** than SQLite's rollback-journal mode, which locks the
whole database file (five states: `UNLOCKED → SHARED → RESERVED → PENDING → EXCLUSIVE`, chapter 16
§16.5). quilldb can afford that because it is single-process with shared memory — a lock is a mutex and
a dict entry, not a filesystem primitive. SQLite coordinates across separate OS *processes* through
`fcntl` byte-range locks, where per-table locking would mean many more syscalls and a much harder
recovery story when a process dies mid-write holding one of them. Their coarser choice is correct for
their constraint; ours is correct for a narrower one (threads, not processes — chapter 16 §16.8).

The other honest difference: SQLite fails fast (`SQLITE_BUSY`) and calls that a busy-timeout retry
budget, not a queue — it does not detect deadlocks, because with one whole-file lock there's nothing to
detect (chapter 16 §16.5). quilldb's finer granularity makes deadlock *possible* (two tables, two
transactions, crossed order) and that possibility is exactly what buys the extra concurrency, so
quilldb needs the wait-for graph that SQLite doesn't.

## Confirmed (sessions 1–7)

- [x] FIFO waiter ordering prevents writer starvation under sustained read load (chapter 16
  §16.5): a flood of readers queues behind an already-waiting writer rather than jumping it —
  `test_waiters_are_fifo_so_nobody_starves`
- [x] Deadlock victim policy is "youngest transaction in the cycle" (highest id); the other side
  of a crossed pair is guaranteed to commit — `test_deadlock_is_detected_and_one_side_commits`,
  `test_deadlock_error_names_the_cycle`
- [x] `busy_timeout` is a connection attribute (`Connection.busy_timeout`, default 5.0s), not an
  `execute()` keyword — `api/connection.py`
- [x] A transaction upgrading its own SHARED hold to EXCLUSIVE, blocked only by an unrelated
  third-party reader, is not mistaken for a self-deadlock — the wait-for graph walk must exclude
  a resource's own holder from recursing into itself —
  `test_upgrade_blocked_by_another_reader_is_not_a_deadlock`
- [x] Real contention holds the invariant: 8 threads × 10k transfers, sum of balances unchanged,
  no negative balances, b-trees structurally valid, `sqlite3 PRAGMA integrity_check` reports `ok`
  — `test_sum_of_balances_never_changes`
- [x] Week 6 review regressions, each failing before its fix — `test_week6_regressions.py`:
  rollback undoes writes to pages committed after a deferred `BEGIN` (B6-2); a rollback with an open
  cursor releases every lock (B6-3); closing one `Connection` leaves its siblings usable and the last
  close closes the file (B6-4); a cycle closed by the *older* transaction still aborts the youngest,
  promptly (B6-5); a queue head that times out wakes the waiters behind it (B6-6); uncommitted DDL is
  invisible to other connections (B6-7); `ANALYZE` waits for the current writer (B6-8); `immediate` is
  not a reserved word (B6-9)
- [x] 20 consecutive runs of the transfer stress test, no flakes — session 7's flake hunt,
  131s–171s per run, all green
- [x] `src/quilldb/bench/concurrent.py` reports read and write throughput separately: writes are flat
  because the single-writer lock allows exactly one commit in flight regardless of thread count;
  reads don't queue (SHARED coexists) but don't scale either, because CPython's GIL — not the
  lock manager — is the ceiling for CPU-bound work with nothing to release it
