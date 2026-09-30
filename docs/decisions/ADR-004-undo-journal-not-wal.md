# ADR-004: Atomic commit with an undo (rollback) journal, not a write-ahead log


## Status


Accepted (decided in week 5; recorded in week 8).


## Context


A crash in the middle of a commit must not leave a database that is half old and half new: a torn b-tree
loses the tree, not a row. Two families of solution exist. An **undo journal** copies each page's
original contents to a separate file before overwriting it, so recovery can roll a partial commit back.
A **write-ahead log** appends the new page images to a log and only later copies them into the database,
so recovery can replay them. SQLite supports both; the undo journal is its default.

The project also had to fit a single buffer pool shared by concurrent transactions (weeks 5 and 6), which
constrains what the pool may evict.


## Decision


quilldb uses SQLite's rollback-journal design: the commit point is **deleting the journal file**, the
fsync order is journal body, directory, header, data pages, delete journal, and a journal is born invalid
(magic and record count both withheld) until its contents are synced. Pages are journalled whole, once,
before their first modification (`will_modify`). The buffer pool is **no-steal**: a dirty page belonging to
an open transaction is never evicted.


## Consequences


- **The commit protocol is small enough to crash-test exhaustively.** The crash matrix
  (`src/tests/fault_injection/`, 83 tests, 65 of them marked slow) injects a crash at every write and sync
  boundary of a commit, and a second crash during recovery itself, and asserts the database is either
  entirely pre-transaction or entirely post-transaction.
- **Torn pages are handled by construction.** The journal stores whole original pages, not deltas, so
  rolling back overwrites a torn page completely and never depends on its prior contents. This came from
  the simpler representation, not from extra work.
- **Writes are slow and serial.** A commit pays roughly four real `fsync` calls, and there is one writer
  at a time. Measured: about 60 to 73 write transactions per second regardless of thread count
  (`python -m quilldb.bench concurrent`). A WAL commit would do zero or one.
- **Readers cannot run alongside a writer** the way they can under WAL.
- **No-steal limits the pool.** A transaction that dirties more pages than the pool holds makes the pool
  grow past its capacity for the length of the transaction instead of spilling. This is the constraint
  ARIES-style logging exists to remove.
- **`integrity_check` does not test atomicity.** It validates the format. A rollback bug that produced a
  structurally valid file with a row that should not exist (`NOTES.md` B6-2) passed it; only asserting
  the actual contents catches that.


## Alternatives considered


- **WAL.** Faster commits (zero fsyncs at `synchronous=NORMAL`, one at `FULL`, against four) and
  concurrent readers. It needs a wal-index in shared memory, a checkpointer with its own reader rules and
  a checkpoint policy, estimated at 25 or more hours for a second durability mechanism rather than a
  second thing to talk about. It would also have replaced the crash matrix with plumbing.
- **Shadow paging / copy-on-write** (LMDB, ZFS). Deletes most of this design: no journal and no recovery
  code. The costs are that every leaf write dirties the whole path to the root, the file grows until old
  versions are reclaimed, and reclamation needs a notion of which readers still need which version.
- **ARIES (redo and undo, steal / no-force).** The industrial design; it decouples the pool from
  transaction boundaries. Far more machinery than a single-writer engine needs.
- **Group commit.** Amortises the fsync across transactions, but with one writer there is rarely a second
  transaction to batch with.
