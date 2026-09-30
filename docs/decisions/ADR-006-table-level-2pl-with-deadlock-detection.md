# ADR-006: Table-level strict two-phase locking with deadlock detection


## Status


Accepted (decided in week 6; recorded in week 8).


## Context


SQLite reaches "serializable" by removing concurrency: whole-file locks in a five-state ladder
(UNLOCKED, SHARED, RESERVED, PENDING, EXCLUSIVE), one writer at a time, and a fixed escalation order plus
an immediate `SQLITE_BUSY` on contention so that a deadlock cannot form. It works across OS processes
through the filesystem, which is why it cannot afford a real lock manager.

quilldb's goal was threads in one process, sharing memory. That allows a real lock manager, so the
question was what granularity to lock at and how to handle deadlock.


## Decision


- **Granularity: whole tables**, shared or exclusive, held under **strict two-phase locking** (locks are
  released only at commit or rollback). Readers share a table; a writer excludes everyone on it.
- **A global single-writer lock**, `"__writer__"`, is taken before any table lock by every write
  transaction (see `docs/concurrency.md`, "Why a single writer").
- **Deadlock detection**: transactions may wait, and a wait-for graph is checked for cycles when one would
  form; the youngest transaction in the cycle is aborted with `DeadlockError`.
- **Scope: threads in one process.** No `fcntl`, no multi-process locking.


## Consequences


- **Serializable, and phantoms come free.** A shared lock on the whole table blocks any insert into the
  range a reader saw, which finer-grained schemes need gap locks or MVCC to achieve.
- **Neither reads nor writes scale with threads.** Measured (`python -m quilldb.bench concurrent`): reads
  fall from 5,236 to 3,645 transactions per second from one to eight threads, and writes stay flat around
  60 to 73. The two ceilings differ: writes are flat *structurally* (one commit in flight, each paying
  about four `fsync`s, so table-level locking is not the limit), reads are flat because CPython's GIL runs
  one thread's bytecode at a time. The lock manager is not the read ceiling; the interpreter is.
- **Detection allows strictly more concurrency than avoidance**, since nothing aborts unless a real cycle
  exists, at the price of maintaining the graph and callers that must be ready to retry.
- **Detection is subtle to get right.** The bug journal has two cases: a deadlock resolved by whichever
  timer fired first, because the true victim was asleep and nothing woke it (`NOTES.md` B6-5), and a timed
  out queue head nobody noticed leaving (B6-6). Both were found by review and stress, not by the initial
  tests.
- Table granularity means two writers to different tables still serialise on `"__writer__"`, so the finer
  granularity buys concurrent *readers* of different tables, not concurrent writers.


## Alternatives considered


- **SQLite's file-level ladder with deadlock avoidance.** Simple and structurally deadlock-free, but it
  only makes sense when the lock has to live in the filesystem across processes. It also gives up
  reader/writer concurrency that a shared-memory engine can afford.
- **Row-level locking with intention modes (IS / IX).** The mechanism that makes fine-grained locking
  practical (InnoDB, SQL Server); the natural next step, and the concrete answer to "how would you add
  row locks". Needs a hierarchy of lock modes and a plan for phantoms.
- **Optimistic concurrency, lock-free structures.** In Python the atomic primitives are not usefully
  exposed and the GIL makes the wins illusory, and lock-free code is where correctness arguments are hard
  to check.
- **Partitioning so no lock is needed.** Scales furthest, but a transaction spanning two partitions needs
  distributed commit: a lock manager traded for two-phase commit.
- **Multi-process locking with `fcntl`.** A scope decision, not only a difficulty one: locks are
  per-process not per-thread, broken over NFS, and a crashed process cannot clean up. Multi-process is
  harder than multi-thread, not more advanced.
