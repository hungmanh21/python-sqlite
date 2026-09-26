# Durability: what the crash matrix proves, and what it doesn't

"I implemented a journal" is a claim. This page is the evidence, and — just as importantly — the
honest limits of that evidence. Full background: [`docs/theory/txn/13-atomic-commit-and-the-journal.md`](theory/txn/13-atomic-commit-and-the-journal.md)
and [`docs/theory/txn/14-crash-recovery.md`](theory/txn/14-crash-recovery.md).

## The commit point, precisely

A transaction is committed the instant `Journal.delete()` (`src/quilldb/txn/journal.py:251`) returns
— its own docstring says so: `"""Unlink. THE COMMIT POINT."""`. Not `commit_barrier()`, even though
that's where the journal becomes *valid* (magic + `nRec` stamped, §13.4 step 3.7) and where the new
page data is physically written to the database file (`flush_all()` + `pager.sync()`, step 3.9–3.10).

That ordering is deliberate, not incidental: between `commit_barrier()` completing and `delete()`
running, the database file already holds the *new* data, but a hot journal is still on disk — and
`recover_if_needed()` (`src/quilldb/txn/recovery.py`) treats any hot journal as evidence of an
interrupted commit and **rolls it back**, undoing the new writes. So a crash in that window doesn't
partially commit; it fully un-commits a transaction whose data was already on disk. This is what
`test_recovery_restores_pre_transaction_state` (`src/tests/unit/test_recovery.py`) checks directly,
and it's why "the database file has the new bytes" and "the transaction is committed" are different
claims here.

## What's proven

`src/tests/fault_injection/` parametrizes over every real write boundary, every real fsync boundary,
and a second crash landing inside recovery itself, across six scenarios:

| Scenario | What it exercises |
|---|---|
| `single_insert` | the baseline path — runs on every commit |
| `insert_causing_split` | truncate-on-rollback of a page allocated *during* the transaction |
| `delete_freeing_page` | the freelist, transactionally (`Pager.free_page`) |
| `update_with_indexes` | table page + index page(s) kept in sync or rolled back together |
| `analyze_refresh` | `quill_stat1` replaced atomically, never half-published |
| `multi_page_txn` | 35,000 inserts in one transaction — pushes `BufferPool`'s cache past its
  default 128-page capacity, proving no-steal holds under real pressure, not just in the easy case |

Boundary counts are **measured, not guessed** (`_measure_real_write_count`,
`_measure_real_sync_count`): a `db`-target write count and a `journal`-target write count for the same
scenario are never equal — `Journal.record_original()` makes three `.write()` calls per journalled
page against one for `Pager.flush_all()` — so a single shared constant either misses real boundaries
on one side or generates unreachable ones on the other. `multi_page_txn`'s crash points are sampled (8
per target) rather than exhausted, because every crash point re-runs the full 35,000-insert setup
(no-steal means nothing reaches disk before the barrier regardless of where `crash_at` falls) —
exhausting all ~140 would mean hours, not minutes.

After every crash point, five independent things are checked
(`_assert_recovered_state_is_consistent`): the visible row state is unmixed, every b-tree is
structurally valid, no index disagrees with its table, `sqlite3 ... "PRAGMA integrity_check"` says
`ok`, and no journal is left behind. `test_crash_during_recovery` adds a second, nested crash — one
mid-transaction, one mid-recovery — because idempotent replay (§14.2) is a claim until something
actually interrupts it twice.

**One structural fact worth stating plainly:** this specific harness can only ever observe
STATE_BEFORE after a crash, never STATE_AFTER. Two things combine to guarantee it — `Pager.write_page`
asserts no database page is written before `commit_barrier()` returns, and `Journal.delete()` makes no
`.write()` call at all (just `unlink()`), so a write-only fault injector has nothing to fault at the
one point that would produce STATE_AFTER. That's a limitation of *this test harness's fault model*, not
evidence that STATE_AFTER is unreachable in reality — it plainly is reachable (any crash after a real,
un-faulted `delete()` returns is exactly STATE_AFTER) — this harness just never manages to land a fault
there.

## Non-guarantees

Volunteering these is stronger than being asked about them.

- **A lying `fsync`.** If the drive acknowledges a sync it never performed, every guarantee above is
  void. No test here can detect it — that needs hardware you trust, not a better test.
- **Torn sectors.** The undo-journal design is immune for *its own* pages (whole pages are stored, so a
  torn restore is just overwritten wholesale — §13.10), but this only covers page-sized writes. A torn
  write to the journal *header itself*, mid-sector, is a real gap the header's sector-sized padding
  (§13.5) argues about but this test suite cannot reproduce — that needs a block-level fault injector,
  not a process-level one.
- **A lost page cache.** Killing a process (what every test here does) leaves the OS page cache intact
  and the kernel finishes pending writes on its own. A real power cut does not. Nothing here simulates
  that; it needs a VM snapshot or an actual power cut.
- **No directory fsync after `unlink()`.** `Journal.delete()` (`journal.py:251`) calls `self.path.unlink()`
  directly, with no following `os.fsync()` on the parent directory. `commit_barrier()` *does* fsync the
  directory once, after journal creation (§13.4 step 3.5) — but the corresponding fsync after deletion,
  which would make the journal's *absence* durable as promptly as its presence was, is not implemented.
- **`PRAGMA synchronous` is not wired up.** `SyncMode` (`constants.py`) and `Journal`'s three sync
  levels (OFF / NORMAL / FULL) exist and are what the sync-boundary matrix actually parametrizes over
  — but there is no `PRAGMA synchronous` statement yet to select a level from SQL. Every transaction in
  this codebase runs at `SyncMode.FULL` today; the matrix tests that level only.
- **Journal-content correctness is a separate claim from journal-mechanics correctness.** Every test
  above verifies that rollback restores *what the journal says*. None of them can verify the journal
  said the *right thing* — a bug that journals the modified page instead of the original would pass
  every crash test here, because rollback would faithfully restore the already-broken state. The guard
  against that class of bug is the byte-identical rollback hash test (`test_transaction.py`,
  hash-before-`BEGIN` vs. hash-after-`ROLLBACK`), not this matrix.
- **Single-process, single-writer.** `recover_if_needed()`'s own docstring says so directly: the
  two-processes-race-to-recover and stale-lock hazards (§14.2) don't apply yet because there's no
  locking (chapter 16, week 6) to race over. A hot journal today only ever comes from *this* process
  having crashed, not a concurrent one.
- **Max transaction size is bounded by RAM, not by the buffer pool's configured capacity.** No-steal
  forbids evicting a dirty, uncommitted page early, so growing the pool past capacity (what
  `multi_page_txn` exercises) is correct behavior, not a leak — but it also means a transaction's
  entire dirty set has to fit in memory. There is no spill-to-disk path, and none is planned this week;
  ARIES-style logging is the real answer to that limit, and adopting it is a deliberate non-goal for
  week 5 (§0.1).

## The honest one-sentence version

*"The crash matrix proves atomicity holds at every write and fsync boundary this code controls, across
six real mutation shapes, including a second crash during recovery itself. It does not simulate torn
sectors, a lost page cache, or a lying disk — those need block-level fault injection or a real power
cut. What it does catch is every ordering bug in the commit and recovery sequence, which is where the
bugs actually were."*
