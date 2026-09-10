# quilldb — Implementation Plan: Week 5, Transactions and Crash Recovery


← [Index](README.md)  ·  Prev: [Week 4 — Mutation and Indexes](week-4-mutation-and-indexes.md)  ·  Next: [Week 6 — Concurrency](week-6-concurrency.md)


---


# Week 5 Spec — Atomic Commit, Rollback, and the Crash Matrix ⭐⭐


> **Read [chapter 13](../theory/txn/13-atomic-commit-and-the-journal.md) before you write any code this
> week**, and [chapter 14](../theory/txn/14-crash-recovery.md) before the crash matrix. Week 5 is one of
> two weeks where a mistake made in the first hour cannot be refactored out later — the fsync ordering
> and the "journal the original, not the modified, page" rule are both decisions you cannot retrofit.


**This is the highest-signal week in the project.** Not because transactions are hard — the journal is
maybe 150 lines — but because the *crash matrix* converts a claim into evidence, and almost nobody's
side project has one.


**The strategic point, and it should shape how you spend the week:** a journal is a claim. A
parametrized test that crashes at every physical write boundary and asserts the database is entirely
pre-commit or entirely post-commit is a proof. Budget accordingly — if you must cut, cut features, never
the matrix.


---


## Week 5 contract


```python
db.execute("BEGIN")
db.execute("INSERT INTO users VALUES (1, 'ada', 36)")
db.execute("ROLLBACK")          # the row is gone, file is byte-identical


with db.transaction():          # commits on clean exit, rolls back on exception
    for i in range(10_000):
        db.execute("INSERT INTO users VALUES (?, ?, ?)", (i, f"u{i}", i % 80))


db.execute("INSERT INTO users VALUES (2, 'grace', 45)")   # autocommit: implicit txn
```


Supported:


- `BEGIN` / `COMMIT` / `ROLLBACK` as statements, plus a `db.transaction()` context manager
- **autocommit**: any statement outside an explicit transaction gets its own implicit one
- automatic recovery of a hot journal inside `connect()`
- `PRAGMA synchronous = OFF | NORMAL | FULL` (default `FULL`)


Not supported, rejected with a typed error:


- nested transactions, `SAVEPOINT`, `RELEASE`, `BEGIN IMMEDIATE` / `EXCLUSIVE` (week 6 adds locking modes)
- WAL mode, `journal_mode` other than the default
- reading arbitrary SQLite `-journal` files — quilldb writes its own format (chapter 13 §13.11)


**Three rules that are not negotiable:**


1. **Journal the page's ORIGINAL content, before its first modification.** Journalling the modified page
   makes every crash test pass while making rollback restore the broken state. Chapter 14 §14.6.
2. **No database page may be written before `commit_barrier()` returns.** This single ordering constraint
   is what the whole design rests on.
3. **fsync the database before deleting the journal** — in commit *and* in recovery.


---


## Week 5 file map


```
src/quilldb/
├── txn/                          NEW
│   ├── journal.py                Journal — write, sync, replay, validate
│   ├── transaction.py            Transaction — dirty set, commit, rollback
│   └── recovery.py               recover_if_needed()
├── storage/
│   ├── pager.py                  + journal hook, + write barrier assertion
│   └── bufferpool.py             + "don't evict a dirty page of an open txn"
├── api/connection.py             + BEGIN/COMMIT/ROLLBACK, autocommit, transaction()
├── constants.py                  + SECTOR_SIZE, JOURNAL_MAGIC, SyncMode
└── errors.py                     + TransactionError, SimulatedCrash, JournalCorruptError


src/tests/
└── fault_injection/              NEW — the crown jewel
    ├── conftest.py               FaultyFile fixtures
    ├── test_crash_matrix.py
    └── test_crash_during_recovery.py
```


---


## 28. Week 5 additions to `errors.py` and `constants.py`


```python
class TransactionError(QuillDBError):
    """COMMIT with no transaction open, BEGIN inside one, etc."""




class JournalCorruptError(QuillDBError):
    """The journal is structurally unreadable — not merely incomplete.


    NOTE: an incomplete journal is NORMAL and is not this error. A missing
    magic or nRec == 0 means "nothing to replay," which is a successful
    outcome. Reserve this for a journal whose header parses but whose
    self-described geometry is impossible (page size 0, absurd nRec).
    """




class SimulatedCrash(BaseException):
    """Injected by FaultyFile. Inherits BaseException, NOT Exception.


    This matters: `except Exception` blocks in your own commit path must not
    swallow it, or your fault injection will silently test nothing.
    """
```


```python
SECTOR_SIZE = 512               # journal header is padded to this (chapter 13 §13.5)
JOURNAL_MAGIC = b"quilldbj"     # 8 bytes, deliberately not SQLite's d9d505f920a163d7




class SyncMode(IntEnum):
    OFF = 0
    NORMAL = 1
    FULL = 2
```


The `SimulatedCrash(BaseException)` choice is a real trap avoided. If it inherits `Exception` and any
layer of your commit path has a broad `except Exception:`, your crash tests will pass because the crash
was caught and handled — and you will believe you have proven something you haven't.


---


## 29. `txn/journal.py`


### The stub


```python
"""The rollback journal: original page images, so a transaction can be undone.


Format (chapter 13 §13.5 — SQLite's layout, quilldb's magic):


    HEADER, padded to SECTOR_SIZE:
      0   8  magic            JOURNAL_MAGIC, but written as ZEROS initially
      8   4  nRec             record count, written as ZERO initially
      12  4  checksum nonce   random per journal
      16  4  page_count_before  truncate back to this on rollback
      20  4  sector size
      24  4  page size


    RECORDS, immediately after the header, no padding:
      [page_id u32][original page image, PAGE_SIZE bytes][checksum u32]


The magic and nRec are withheld until the body is synced, so a torn journal is
rejected two independent ways. See §13.6 — this is the single most important
property of the format.
"""




def journal_checksum(page: bytes, nonce: int) -> int:
    """SQLite's sampled additive sum (chapter 13 §13.7).


    Not a CRC: it reads every 200th byte from the end, 20 bytes out of 4096.
    Designed to catch torn writes cheaply, not adversarial corruption.
    """
    cksum = nonce
    i = PAGE_SIZE - 200
    while i > 0:
        cksum += page[i]
        i -= 200
    return cksum & 0xFFFFFFFF




class Journal:
    def __init__(self, db_path: Path, sync_mode: SyncMode = SyncMode.FULL) -> None:
        """The journal lives at db_path with '-journal' appended."""


    def begin(self, page_count_before: int) -> None:
        """Create the file and write the header with magic and nRec BOTH ZERO.


        Raises:
            FileExistsError: a journal already exists — that's a hot journal or
                a bug, and either way you must not overwrite it.
        """


    def record_original(self, page_id: int, data: bytes) -> None:
        """Append one record. Data must be the page as it was BEFORE any
        modification in this transaction.


        Must be idempotent per page_id within a transaction: recording the same
        page twice wastes I/O. The caller (Transaction) tracks that.
        """


    def commit_barrier(self) -> None:
        """Make the journal valid. Nothing may touch the database before this.


            fsync(journal)                    # body durable
            fsync(directory)                  # the journal's NAME durable  (§13.4)
            write magic + nRec, 12 bytes @ 0  # one write, inside one sector
            fsync(journal)                    # NOW it is valid


        At SyncMode.NORMAL, skip the SECOND journal fsync — the per-record
        checksums cover for it (§13.8). At OFF, skip all three.
        """


    def replay(self, pager: Pager) -> int:
        """Roll back. Returns the number of pages restored.


        Stops at the first record whose checksum fails — that's the boundary
        between what reached disk and what didn't, not an error (§14.3).
        Returns 0 if the magic is absent or nRec is 0.
        """


    def delete(self) -> None:
        """Unlink. THE COMMIT POINT. The caller must have fsynced the database
        already; assert that if you can."""
```


### The tests


```python
def test_header_is_padded_to_sector_size(journal):
    journal.begin(page_count_before=29)
    assert first_record_offset(journal) == SECTOR_SIZE




def test_magic_and_nrec_are_zero_until_the_barrier(journal):
    journal.begin(29)
    journal.record_original(7, b"\xab" * PAGE_SIZE)
    raw = journal.path.read_bytes()
    assert raw[0:12] == b"\x00" * 12          # BOTH withheld — §13.6
    journal.commit_barrier()
    raw = journal.path.read_bytes()
    assert raw[0:8] == JOURNAL_MAGIC
    assert int.from_bytes(raw[8:12], "big") == 1




def test_journal_without_magic_replays_nothing(tmp_path, pager):
    j = make_journal_with_records(tmp_path, count=3, write_magic=False)
    assert j.replay(pager) == 0




def test_journal_with_zero_nrec_replays_nothing(tmp_path, pager):
    j = make_journal_with_records(tmp_path, count=3, nrec=0)
    assert j.replay(pager) == 0




def test_replay_stops_at_a_bad_checksum(tmp_path, pager):
    j = make_journal_with_records(tmp_path, count=5)
    corrupt_byte_in_record(j, record=3)
    assert j.replay(pager) == 2               # records 1 and 2 only




@pytest.mark.parametrize("nonce", [0, 1, 0x7B6057E4, 0xFFFFFFFF])
def test_checksum_is_deterministic_and_nonce_dependent(nonce):
    page = bytes(range(256)) * 16
    assert journal_checksum(page, nonce) == journal_checksum(page, nonce)
    assert journal_checksum(page, nonce) != journal_checksum(page, nonce + 1)




def test_checksum_only_samples_every_200th_byte():
    """Documents the tradeoff rather than pretending it's a real checksum."""
    a = bytearray(PAGE_SIZE)
    b = bytearray(PAGE_SIZE)
    b[97] = 0xFF                              # 97 is NOT a sampled offset
    assert journal_checksum(bytes(a), 0) == journal_checksum(bytes(b), 0)
    b2 = bytearray(PAGE_SIZE); b2[96] = 0xFF  # 96 IS sampled
    assert journal_checksum(bytes(a), 0) != journal_checksum(bytes(b2), 0)
```


That last test is unusual and worth keeping: it asserts a **known weakness**. A test that pins down what
your checksum deliberately does *not* detect is documentation that can't go stale, and it stops a future
you from "fixing" the sampling without understanding why it samples.


---


## 30. `txn/transaction.py`


### The stub


```python
class Transaction:
    """One transaction's worth of state.


    Owns: the set of pages already journalled, and the original page_count so
    rollback can truncate.
    """


    def __init__(self, pager: Pager, journal: Journal) -> None:
        self._journalled: set[int] = set()
        self._page_count_before = pager.page_count
        self._active = True


    def will_modify(self, page_id: int) -> None:
        """Call BEFORE the first modification of this page.


        If page_id isn't in self._journalled: read its CURRENT content from
        disk, write it to the journal, and add it to the set.


        A page allocated fresh in this transaction (page_id > page_count_before)
        does NOT need journalling — it didn't exist before, so rollback's
        truncate erases it. Skipping those is a real saving on insert-heavy
        transactions, and it's a nice thing to be able to explain.
        """


    def commit(self) -> None:
        """journal.commit_barrier() -> write dirty pages -> fsync db ->
        journal.delete(). In that order, no exceptions."""


    def rollback(self) -> None:
        """journal.replay() -> truncate to _page_count_before -> fsync ->
        journal.delete() -> discard the buffer pool."""
```


### The structural defence against the worst bug


Rule 1 of the contract — journal the original, before modification — is a rule about *call ordering*,
and rules about call ordering get broken. Make it impossible instead:


```python
# storage/pager.py
    def get_page_for_write(self, page_id: int) -> bytearray:
        """The ONLY way to obtain a mutable page.


        Journals the original first, then returns the buffer. There is no code
        path to a writable page that skipped journalling, because this function
        is the path.
        """
        if self._txn is not None:
            self._txn.will_modify(page_id)
        return self._pool.pin_for_write(page_id)


    def write_page(self, page_id: int, data: bytes | bytearray) -> None:
        assert self._txn is None or self._txn.barrier_passed, (
            "database write before the journal was made valid — this is the one "
            "ordering bug that corrupts data unrecoverably (chapter 13 §13.11)"
        )
        ...
```


**That assertion is worth more than a test.** It fires the first time you get the order wrong, in
whichever test happened to exercise it, with a message that says what's wrong. Leave it in — it costs a
branch on a path already doing I/O.


### Buffer-pool consequence (no-steal)


```python
# storage/bufferpool.py — in the eviction candidate loop
    if frame.dirty and self._txn_open:
        continue    # cannot evict: its original is journalled but the new content
                    # isn't committed. ARIES calls being ALLOWED to do this "steal";
                    # you forbid it. Chapter 13 §13.10.
```


And the failure mode to guard: if every frame is dirty and unevictable, the pool must **spill by
committing nothing** — it can't. So either grow the pool or raise. SQLite handles this by escalating to
EXCLUSIVE and writing dirty pages into the database mid-transaction (which is why chapter 14 §14.3's
transcript found uncommitted data in the file). **You may do the same, but only after
`commit_barrier()`** — journal first, then spill is safe. Write a test that fills the pool inside one
transaction.


---


## 31. `txn/recovery.py`


```python
def recover_if_needed(db_path: Path, pager: Pager) -> bool:
    """Roll back a hot journal if present. Called from connect(), before the
    first read. Returns True if it recovered.


    Steps in chapter 14 §14.3. The two orderings that matter:
      - restore pages BEFORE truncating (a page above the truncation point may
        still need restoring)
      - fsync the database BEFORE deleting the journal (or a crash here leaves
        NOTHING recoverable — the worst bug in week 5)


    Must be idempotent: after a crash mid-recovery, the next open re-runs it,
    which is safe because every journal record is an assignment (§14.2).
    """
```


### The tests, in the order to write them


```python
def test_rollback_restores_a_byte_identical_file(tmp_path):
    """WRITE THIS ONE FIRST. It's the only test that validates the journal's
    CONTENTS rather than the recovery mechanics — it's what catches
    'I journalled the modified page' (chapter 14 §14.6)."""
    db = connect(tmp_path / "t.db"); populate(db, 500); db.close()
    before = hashlib.sha256((tmp_path / "t.db").read_bytes()).digest()


    db = connect(tmp_path / "t.db")
    db.execute("BEGIN")
    for i in range(500, 1500):
        db.execute("INSERT INTO t VALUES (?, ?)", (i, f"v{i}"))
    db.execute("ROLLBACK")
    db.close()


    assert hashlib.sha256((tmp_path / "t.db").read_bytes()).digest() == before




def test_recovery_truncates_a_grown_file(tmp_path):
    """The transaction must be big enough to allocate pages."""
    ...
    assert page_count_after_recovery == page_count_before_txn




def test_recovery_is_idempotent(tmp_path, pager):
    crash_mid_transaction(tmp_path)
    assert recover_if_needed(tmp_path / "t.db", pager) is True
    assert recover_if_needed(tmp_path / "t.db", pager) is False    # nothing left to do
```


---


## 32. `FaultyFile` — the fault injector


```python
class FaultyFile:
    """Wraps a file object and fails deterministically.


    Counts writes and syncs from 1. Raises SimulatedCrash when the count hits
    the configured trigger, AFTER doing nothing — the write does not happen.
    """


    def __init__(self, real_file, *, fail_at_write: int | None = None,
                 fail_at_sync: int | None = None) -> None:
        self._real = real_file
        self._writes = 0
        self._syncs = 0
        self.fail_at_write = fail_at_write
        self.fail_at_sync = fail_at_sync


    def write(self, data: bytes) -> int:
        self._writes += 1
        if self._writes == self.fail_at_write:
            raise SimulatedCrash(f"write #{self._writes}")
        return self._real.write(data)


    def flush_and_sync(self) -> None:
        self._syncs += 1
        if self._syncs == self.fail_at_sync:
            raise SimulatedCrash(f"sync #{self._syncs}")
        self._real.flush()
        os.fsync(self._real.fileno())
```


**Two design notes that make the difference between a useful injector and a decorative one:**


1. **Fail *before* performing the operation, not after.** "Crash after the write succeeded" is a much
   weaker test — the interesting states are the ones where a write you believed happened didn't.
2. **Count writes and syncs separately, and inject into *both* files.** The journal and the database are
   different file objects; a crash matrix that only faults the database never tests journal-write
   failures. Parametrize over which file to fault.


A third, which will save you an hour: expose the final counts. `MAX_WRITES` for the matrix has to come
from somewhere, and the honest source is "run the transaction once with no fault and see how many writes
it did." Hardcoding a guess means either missing boundaries or running hundreds of no-op cases.


---


## 33. The crash matrix


```python
# tests/fault_injection/test_crash_matrix.py


SCENARIOS = [
    ("single_insert",      lambda db: db.execute("INSERT INTO t VALUES (9999, 'x')")),
    ("insert_causing_split", ...),      # must allocate a page — exercises truncate-on-rollback
    ("delete_freeing_page",  ...),      # exercises the freelist transactionally
    ("update_with_indexes",  ...),      # table page + N index pages: the week-4 consistency risk
    ("multi_page_txn",       ...),      # bigger than the buffer pool: forces a mid-txn spill
]




@pytest.mark.parametrize("scenario", [s[0] for s in SCENARIOS])
@pytest.mark.parametrize("target", ["db", "journal"])
@pytest.mark.parametrize("crash_at", range(1, MAX_WRITES + 1))
def test_atomic_at_every_write_boundary(tmp_path, scenario, target, crash_at):
    setup_known_state(tmp_path)
    with pytest.raises(SimulatedCrash):
        run_scenario(tmp_path, scenario, fail_at_write=crash_at, target=target)


    db = quilldb.connect(tmp_path / "t.db")          # triggers recovery
    state = read_state(db)
    assert state in (STATE_BEFORE, STATE_AFTER), "found a MIXTURE — atomicity violated"
    assert validate_all_btrees(db).is_valid
    assert index_matches_table(db)                   # the week-4 property, after a crash
    db.close()
    assert sqlite3_integrity_check(tmp_path / "t.db") == "ok"
    assert not (tmp_path / "t.db-journal").exists()




@pytest.mark.parametrize("sync_at", range(1, MAX_SYNCS + 1))
def test_atomic_at_every_sync_boundary(tmp_path, sync_at):
    ...   # same body, fail_at_sync=sync_at




@pytest.mark.slow
@pytest.mark.parametrize("first", [1, 3, 5, 7])          # sample, don't exhaust
@pytest.mark.parametrize("second", range(1, MAX_RECOVERY_WRITES + 1))
def test_crash_during_recovery(tmp_path, first, second):
    """Chapter 14 §14.5 — the case that finds the two worst ordering bugs."""
    crash_mid_transaction(tmp_path, at=first)
    with pytest.raises(SimulatedCrash):
        connect_with_faulty_file(tmp_path, fail_at_write=second)
    db = quilldb.connect(tmp_path / "t.db")
    assert read_state(db) in (STATE_BEFORE, STATE_AFTER)
    assert validate_all_btrees(db).is_valid
```


**The four assertions inside the loop are the deliverable, not the loop.** Anyone can crash a program;
what makes this evidence is that after every crash you check *four independent properties* — the state is
unmixed, the trees are structurally valid, the indexes agree with their tables, and an
independently-written C implementation agrees the file is sound.


**Keep it fast enough to actually run.** The full product is thousands of cases. Practical shape: the
write matrix for one cheap scenario runs on every commit; the full product and the
crash-during-recovery test are marked slow and run in CI. A test suite you skip proves nothing.


**Also assert the negative:** with no fault injected, every scenario commits and leaves no journal. It's
embarrassingly easy to write a matrix that passes because the transaction never did anything.


---


## Week 5 sessions


| # | 2 hours on | Done when |
|---|---|---|
| 1 | `journal.py`: header, records, `journal_checksum`, `begin` / `record_original` | the magic-and-nRec-withheld test is green |
| 2 | `commit_barrier`, `replay`, and all four "replays nothing" cases | a hand-built corrupt journal is correctly ignored |
| 3 | `Transaction`, `get_page_for_write`, the write-barrier assertion, no-steal in the pool | the byte-identical rollback hash test is green |
| 4 | `BEGIN` / `COMMIT` / `ROLLBACK`, autocommit, `db.transaction()` | exception inside the context manager rolls back |
| 5 | `recovery.py` + hot-journal detection wired into `connect()` | kill a process mid-txn, reopen, data is pre-transaction |
| 6 | `FaultyFile`, and the write matrix for one scenario | one scenario × every write boundary is green |
| 7 | Full matrix, sync matrix, crash-during-recovery, `docs/durability.md` | all five scenarios green; the doc names the non-guarantees |


**Session 3 is the one to slow down on.** If `get_page_for_write` isn't the only route to a mutable page,
every later session builds on a foundation that can silently skip journalling. Grep for direct
`bytearray` mutation of pooled pages before moving on.


---


## Week 5 definition of done


- [ ] `ROLLBACK` leaves the file **byte-identical** — verified by hash, on a transaction that grew the file
- [ ] The journal's magic and `nRec` are both zero until `commit_barrier()`; one 12-byte write flips them
- [ ] A journal with no magic, `nRec == 0`, or zero length replays nothing and is deleted
- [ ] A single flipped byte in a record is caught and replay stops there
- [ ] `commit_barrier` does journal-fsync → directory-fsync → header write → journal-fsync, in that order
- [ ] No database page is ever written before the barrier — enforced by an assertion, not just a test
- [ ] The journal is deleted on commit and absent afterwards
- [ ] Recovery restores pages, **then** truncates, **then** fsyncs, **then** deletes the journal
- [ ] Recovery happens automatically inside `connect()`; no user-callable repair step exists
- [ ] Recovery is idempotent — running it twice is safe, and calling it after a mid-recovery crash works
- [ ] Crash matrix green over every **write** boundary, all five scenarios, faulting both files
- [ ] Crash matrix green over every **fsync** boundary
- [ ] **Crash during recovery** green — the nastiest case, and the one that catches fsync-before-unlink
- [ ] After every crash point: unmixed state, valid trees, indexes agree with tables, `integrity_check` `ok`
- [ ] A transaction larger than the buffer pool works (mid-transaction spill after the barrier)
- [ ] Pages allocated within the transaction are **not** journalled — and rollback still truncates them away
- [ ] `PRAGMA synchronous = OFF | NORMAL | FULL` changes the fsync count; assert the counts
- [ ] `docs/durability.md` names the commit point precisely **and** lists the non-guarantees: lying
      `fsync`, torn sectors, no directory fsync after unlink, and what the matrix cannot simulate
- [ ] `NOTES.md` entries for every bug over 20 minutes — this week produces the best interview stories


**If the week runs short, cut in this order:** `synchronous` levels, then the sync matrix, then scenarios
4 and 5. **Never cut** the byte-identical rollback test, the write matrix, or crash-during-recovery.
Those three are the week's entire value.