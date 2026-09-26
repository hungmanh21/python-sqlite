# Concurrency in quilldb

Written before `txn/locks.py`, per [chapter 15 §15.8](theory/txn/15-isolation-anomalies.md#158-what-youre-building-this-week):
naming the isolation level first is what forces the decisions that are painful to retrofit — like
"when do read locks get released" — rather than discovering them in session 6.

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
  commit. A `busy_timeout` is a backstop for waits the graph doesn't model, not a substitute for
  detection (chapter 16 §16.4).

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

## Open items (filled in as the sessions land)

- [ ] FIFO waiter ordering confirmed to prevent writer starvation under sustained read load
  (chapter 16 §16.5) — `test_waiters_are_fifo_so_nobody_starves`
- [ ] Deadlock victim policy is "youngest transaction in the cycle" (highest id), confirmed to
  guarantee the other side of a crossed pair commits
- [ ] `busy_timeout` documented as a connection attribute, not an `execute()` keyword
