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


**Four rules that are not negotiable:**


1. **Journal the page's ORIGINAL content, before its first modification.** Journalling the modified page
   makes every crash test pass while making rollback restore the broken state. Chapter 14 §14.6.
2. **No database page may be written before `commit_barrier()` returns.** This single ordering constraint
   is what the whole design rests on.
3. **fsync the database before deleting the journal** — in commit *and* in recovery.
4. **No-steal, with no exceptions.** A dirty page is never evicted while a transaction is open; the pool
   grows instead. This is what keeps rule 2 absolute and the journal single-segment — see session 0 §0.1.


---


## Week 5 file map


```
src/quilldb/
├── txn/                          NEW
│   ├── journal.py                Journal — write, sync, replay, validate
│   ├── transaction.py            Transaction — journalled set, commit, rollback
│   └── recovery.py               recover_if_needed()
├── storage/
│   ├── bufferpool.py             + get_page_for_write / pinned_for_write  (the journal hook)
│   │                             + allocate_page / free_page  (moved up from Pager)
│   │                             + no-steal: never evict a dirty page mid-txn
│   ├── pager.py                  + truncate, restore_page, reload_header, header_bytes
│   │                             + write-barrier assertion in write_page
│   │                             - allocate_page / free_page  (MOVED to BufferPool)
│   └── header.py                 unchanged
├── btree/{btree,index,cursor}.py call sites: get_page → get_page_for_write where dirty
├── catalog/catalog.py            same, + pager.allocate_page → pool.allocate_page
├── plan/analyze.py               same
├── storage/overflow.py           same
├── api/connection.py             + BEGIN/COMMIT/ROLLBACK, autocommit, transaction()
│                                 + recover_if_needed() inside connect()
├── constants.py                  + SECTOR_SIZE, JOURNAL_MAGIC, SyncMode
└── errors.py                     + TransactionError, SimulatedCrash, JournalCorruptError


src/tests/
└── fault_injection/              NEW — the crown jewel
    ├── conftest.py               FaultyFile fixtures
    ├── test_crash_matrix.py
    └── test_crash_during_recovery.py
```


**Two thirds of the week-5 diff is in files that already exist.** That is not incidental — see session 0.


---


## Session 0 — reconcile the storage layer first ⚠️


> **Do this before session 1, and land it green against the existing suite.** It contains no
> transactions, no journal, and no new behaviour. It is the refactor that gives the journal exactly one
> place to live.


**Why this session exists.** Week 5 does not add a layer on top of weeks 1–4. It retroactively imposes an
invariant *on* them: *no page is modified without its original reaching the journal first.* That is a
claim about code spread over seven files that never had any reason to signal write intent. The roadmap
already made this argument once — §1.4 builds the buffer pool in week 1 rather than week 6 because
"retrofitting a cache touches every call site in the pager and B-tree." Journalling is the same retrofit
one layer up, and it is the same size.


Three things in the tree today are structurally incompatible with the journal, and all three are
load-bearing:


1. **Write intent is declared too late.** The established idiom is `get_page()` → mutate in place →
   `unpin(page_id, dirty=True)`. The journal needs to know *before* the mutation; the pool is told
   *after* it. There are 55 `get_page`/`pinned` call sites, ~30 of which end up dirty.
2. **`allocate_page` and `free_page` write straight to the file**, bypassing the pool entirely —
   `free_page`'s own docstring says *"⚠️ WRITES THROUGH THE POOL'S BACK."* Both call `Pager.write_page`,
   which is precisely where the barrier assertion goes, so every allocation inside a transaction would
   trip it.
3. **The file header is only ever written in `Pager.close()`.** `page_count`, `freelist_trunk`,
   `freelist_count`, `change_counter` and `schema_cookie` live in an in-memory `FileHeader` that no
   page-oriented journal can see.


### 0.1 The decision to make before writing any code: what happens when the pool fills


The obvious answer — "spill mid-transaction, but only after `commit_barrier()`, so the journal is already
valid" — does not work, and it is worth knowing why before you build around it. (An earlier draft of this
plan recommended exactly that.) Once the barrier writes `nRec = k`, the journal is sealed. Any
page dirtied *after* that point has nowhere to be journalled that `replay()` will honour — so if it then
reaches the database file, a rollback leaves it there. That is a mixed state, which is the one thing the
crash matrix exists to forbid. SQLite's answer is a **new journal segment** per spill, with a fresh nonce
(chapter 13 §13.5); that is a real feature, not a detail.


**quilldb takes the other branch: full no-steal, and the journal stays single-segment.** A dirty page is
never evicted and never written to the database before the barrier — not even once. When the pool is full
of dirty pages, it **grows** rather than spilling:


| | Bounded dirty set (raise when full) | **Grow the dirty set (chosen)** | Multi-segment journal |
|---|---|---|---|
| Journal format | single segment | **single segment** | N headers, N nonces |
| `commit_barrier()` called | once | **once** | once per spill |
| Max transaction | 128 pages | **available RAM** | unbounded |
| Extra work | none | **~5 lines** | ~3 hours |
| Roadmap's 10,000-row demo | ✗ breaks | **✓** | ✓ |


The bounded option is out because the roadmap's headline snippet commits 10,000 rows in one transaction,
which is several hundred pages against a 128-page pool. Growing costs about five lines and caps a
transaction at RAM — roughly 1.2 MB of `bytearray` for that demo.


**This is the ARIES conversation, and it is worth being able to have.** Chapter 13 §13.10: *"Your design
forbids stealing … which is exactly the constraint ARIES was invented to remove."* Say it as: *"I forbid
steal, so a transaction's dirty set has to fit in memory. ARIES buys that constraint back with LSNs on
every page and a redo phase at recovery — which is a lot of machinery to avoid a limit I can state in one
sentence."* Record the limit in `docs/durability.md`; only clean pages are evictable under LRU, so reads
stay bounded and it is the write set alone that grows.


### 0.2 The six tasks


**Task 1 — write intent on the pool, with an inert hook.**
`get_page_for_write` goes on `BufferPool`, not `Pager`. §30's snippet has it on the Pager calling
`self._pool` — that inverts the layering, since `bufferpool.py` imports `Pager` and wraps it, and
CLAUDE.md forbids upward dependencies.


```python
# storage/bufferpool.py
    def get_page_for_write(self, page_id: int) -> bytearray:
        """The ONLY way to obtain a page you intend to modify.


        Journals the original first, then pins and marks dirty immediately —
        so there is no code path to a mutable page that skipped journalling,
        because this function IS the path.
        """
        if self._txn is not None:
            self._txn.will_modify(page_id)      # inert until session 3
        page = self.get_page(page_id)
        self._cache[page_id].dirty = True       # dirty at acquisition, not at unpin
        return page


    @contextmanager
    def pinned_for_write(self, page_id: int) -> Generator[bytearray]:
        """get_page_for_write, yield, unpin — including when the body raises."""
```


`self._txn` starts as `None` and nothing sets it this session. The refactor lands and the suite stays
green *before* any transaction semantics exist, which is the whole point of doing it separately.


**Task 2 — convert the call sites.** Mechanical, and there are ~30:


```python
raw = self.pool.get_page(page_id)              raw = self.pool.get_page_for_write(page_id)
try:      ...mutate...                    →    try:      ...mutate...
finally:  self.pool.unpin(page_id, True)       finally:  self.pool.unpin(page_id)
```


Sites that compute dirtiness conditionally — [`index.py`](../../src/quilldb/btree/index.py)'s
`dirty = False` … `dirty=dirty` pattern is the common one — cannot be rewritten mechanically, and the
rule that makes them easy is:


> **Over-declaring write intent is safe; under-declaring is corruption.** Journalling a page you end up
> not modifying costs one wasted page image, and rollback assigns identical bytes back over it. Missing
> one loses data. When a branch *might* write, declare the write.


Leave genuinely read-only paths — `validate.py`, the scan side of `cursor.py` — on plain `get_page`.
Journalling every page a sequential scan touches would be a real performance bug.


**Task 3 — move allocation onto the pool.** `allocate_page` and `free_page` need the pool, and `Pager`
cannot see it, so they move up. Call sites change `self.pager.allocate_page()` →
`self.pool.allocate_page()` (~10 of them).


```python
# storage/bufferpool.py
    def allocate_page(self) -> int:
        """Reuse a freed page, or grow the file. Contents are undefined.


        A page taken off the freelist is journalled (it existed before the
        transaction). A page that grows the file is NOT — it gets a zeroed
        entry in the cache and never touches disk until commit, so rollback's
        truncate erases it for free.
        """


    def free_page(self, page_id: int) -> None:
        """Return a page to the freelist, THROUGH the pool.


        Retires the 'writes through the pool's back' warning permanently: the
        freelist trunk is now an ordinary journalled page write like any other.
        """
```


This also fixes the stale-cache hazard that `free_page`'s current docstring and three comments in
`catalog.py` work around by hand. Delete those workarounds as you go.


**Task 4 — make the header a page write.** Page 1's bytes 0–99 are the file header, and page 1 is a
perfectly ordinary journalled page — so the header becomes transactional the moment it is written
*through the pool* instead of direct to the file at `close()`.


```python
# storage/pager.py
    def header_bytes(self) -> bytes:        """The live in-memory header, serialized."""
    def reload_header(self) -> None:        """Re-read bytes 0..99 from the file. Chapter 14 §14.3 step 10."""
```


Commit stamps it in before the barrier, so page 1's original is journalled along with everything else:


```python
    # txn/transaction.py, first line of commit() — BEFORE commit_barrier()
    self._pool.get_page_for_write(SCHEMA_ROOT_PAGE)[:FILE_HEADER_SIZE] = self._pager.header_bytes()
```


Two bugs this closes, neither of which the rest of the plan addresses. A committed transaction that grew
the file previously lost its `page_count` unless `close()` ran — and since `write_page` bounds-checks
against `page_count`, recovery restoring page 30 into a file whose header claims 29 raises
`PageOutOfRangeError`. And **rollback must restore the in-memory header too**, not merely truncate:
`freelist_trunk`, `freelist_count`, `change_counter` and `schema_cookie` all still hold the rolled-back
transaction's values otherwise. `reload_header()` is what §30's `rollback()` calls after replay.


In session 0 this is just plumbing: add the two methods, have `close()` route the header through
`get_page_for_write(1)` + `flush_all()` rather than seeking to byte 0 itself, and confirm the suite is
still green.


**Task 5 — the Pager additions recovery needs.**


```python
    def truncate(self, page_count: int) -> None:
        """Shrink the file to `page_count` pages and set the header to match.
        Rollback and recovery both need this; nothing today exposes it."""


    def restore_page(self, page_id: int, data: bytes) -> None:
        """Write a page during replay ONLY.


        Bounds-checks against the JOURNAL's recorded page count, not the live
        header's — recovery restores pages BEFORE it truncates (chapter 14
        §14.5 bug 2), so a page above the current count is expected here and
        must not raise PageOutOfRangeError."""
```


And the barrier assertion, which is the one piece of §30 that lands exactly as written:


```python
    def write_page(self, page_id: int, data: bytes | bytearray) -> None:
        assert self._txn is None or self._txn.barrier_passed, (
            "database write before the journal was made valid — this is the one "
            "ordering bug that corrupts data unrecoverably (chapter 13 §13.11)"
        )
```


**Task 6 — decide what `:memory:` does.** [`connect()`](../../src/quilldb/api/connection.py) supports
`":memory:"` and the test suite leans on it, but `Pager.memory()` sets `_path = None` and the journal
path is defined as *db\_path + "-journal"*. Cheapest correct answer: **`Journal` takes a file object, not
a path**, and an in-memory database journals to an `io.BytesIO`. Every fsync becomes a no-op, `delete()`
drops the buffer, and `BEGIN`/`ROLLBACK` get real semantics on `:memory:` for free. Recovery is
meaningless there — process death takes the database with it — so `recover_if_needed()` returns `False`
immediately when `_path is None`. This also makes the whole of session 1 testable without touching disk.


### 0.3 Done when


- [ ] `ruff check .`, `mypy`, and the full existing suite are green, with **no behaviour change**
- [ ] `git grep -n "pool.get_page(" src/quilldb` — every surviving hit is a genuinely read-only path
- [ ] `git grep -n "unpin(.*dirty=True\|pinned(.*dirty=True"` returns nothing
- [ ] `git grep -n "pager.allocate_page\|pager.free_page"` returns nothing outside `bufferpool.py`
- [ ] `Pager.write_page` is called from exactly two places: `BufferPool._evict_one` and
      `BufferPool.flush_page`
- [ ] `sqlite3 file.db "PRAGMA integrity_check"` still says `ok` after the differential suite
- [ ] `BufferPool` has a `_txn` attribute that is always `None` and a growth path for dirty pages


**Budget: one session, possibly two.** The call-site conversion is the long pole. The session table below
assumes it lands in one.


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


ONE SEGMENT ONLY, deliberately. Real SQLite journals hold many headers, one
per mid-transaction spill (chapter 13 §13.5). quilldb forbids spilling
outright — see session 0 §0.1 — so there is exactly one header at offset 0 and
`commit_barrier()` is called exactly once per transaction. If you ever relax
no-steal, this is the first thing that has to change, and it changes the format.
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
    def __init__(self, db_path: Path | None, sync_mode: SyncMode = SyncMode.FULL) -> None:
        """The journal lives at db_path with '-journal' appended.


        db_path is None for an in-memory database (Pager.memory() sets
        _path = None): the journal is an io.BytesIO, every fsync is a no-op,
        and delete() drops the buffer. Rollback still works exactly as it does
        on disk — only recovery is meaningless, since the database dies with
        the process. Session 0 §0.2 task 6.
        """


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


    def __init__(self, pager: Pager, pool: BufferPool, journal: Journal) -> None:
        self._journalled: set[int] = set()
        self._page_count_before = pager.page_count
        self._active = True
        self.barrier_passed = False     # read by the write_page assertion


    def will_modify(self, page_id: int) -> None:
        """Call BEFORE the first modification of this page.


        If page_id isn't in self._journalled: read its CURRENT content from
        disk, write it to the journal, and add it to the set.


        Read it with pager.read_page(), NOT through the pool: this is an
        internal read, and routing it through get_page() would pin a page
        nobody unpins and inflate the `pages_read` benchmark counter that
        chapter 19 reports.


        A page allocated fresh in this transaction (page_id > page_count_before)
        does NOT need journalling — it didn't exist before, so rollback's
        truncate erases it. Skipping those is a real saving on insert-heavy
        transactions, and it's a nice thing to be able to explain.
        """


    def commit(self) -> None:
        """stamp the header into page 1 -> journal.commit_barrier() ->
        pool.flush_all() -> pager.sync() -> journal.delete().
        In that order, no exceptions.


        The header stamp comes FIRST and goes through get_page_for_write, so
        page 1's original is journalled like any other page. Skip it and a
        committed transaction that grew the file loses its page_count unless
        close() happens to run — see session 0 §0.2 task 4.
        """


    def rollback(self) -> None:
        """journal.replay() -> pager.truncate(_page_count_before) ->
        pager.sync() -> journal.delete() -> pool.clear() ->
        pager.reload_header().


        Restore BEFORE truncate, fsync BEFORE delete (chapter 14 §14.3).


        The last two steps are the ones people drop. The buffer pool now
        describes a file that changed underneath it, and the in-memory
        FileHeader still holds this transaction's freelist_trunk,
        freelist_count, change_counter and schema_cookie — truncating fixes
        page_count and nothing else.
        """
```


### The structural defence against the worst bug


Rule 1 of the contract — journal the original, before modification — is a rule about *call ordering*,
and rules about call ordering get broken. Make it impossible instead:


**Session 0 already built the mechanism** — `BufferPool.get_page_for_write()` is the only route to a
mutable page, and every dirtying call site goes through it. All this session adds is the object on the
other end of the hook:


```python
# storage/bufferpool.py — the hook session 0 left inert
        if self._txn is not None:
            self._txn.will_modify(page_id)     # now actually journals


# storage/pager.py — the assertion, which needs no session-0 caveat
    def write_page(self, page_id: int, data: bytes | bytearray) -> None:
        assert self._txn is None or self._txn.barrier_passed, (
            "database write before the journal was made valid — this is the one "
            "ordering bug that corrupts data unrecoverably (chapter 13 §13.11)"
        )
        ...
```


**That assertion is worth more than a test.** It fires the first time you get the order wrong, in
whichever test happened to exercise it, with a message that says what's wrong. Leave it in — it costs a
branch on a path already doing I/O. It is also why session 0 had to move `allocate_page` and `free_page`
off the Pager: both wrote through `write_page` directly, so both would trip this on every allocation.


### Buffer-pool consequence (no-steal)


```python
# storage/bufferpool.py — in the eviction candidate loop
    if candidate.dirty and self._txn is not None:
        continue    # cannot evict: its original is journalled but the new content
                    # isn't committed. ARIES calls being ALLOWED to do this "steal";
                    # you forbid it. Chapter 13 §13.10.
```


And the failure mode that follows: if every unpinned entry is dirty, there is no victim. Per session 0
§0.1 the pool **grows** rather than spilling — no database page may be written before the barrier, and
that includes an eviction. Clean pages stay evictable under LRU, so only the write set grows.


Write two tests: one that runs a transaction several times larger than `capacity` and commits correctly,
and one that asserts `pager.write_page` was not called at all before `commit_barrier()` — a spy on the
pager is the easiest way, and it catches an eviction that slipped through much more directly than a
corrupted file would.


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


    Returns False immediately for an in-memory database (pager._path is None):
    nothing survives the process, so there is nothing to recover.


    Replay writes through pager.restore_page(), NOT write_page — a page above
    the live header's page_count is expected here, because restoring precedes
    truncating (chapter 14 §14.5 bug 2). Finish with pager.reload_header():
    recovery just changed page_count and the freelist head underneath the
    header object that open() built (§14.3 step 10).
    """
```


### Where it goes in `connect()`


Ordering matters more than it looks. `connect()` currently runs
`Pager.open()` → `BufferPool(pager)` → `Catalog(...)` → `catalog.load()`, and `catalog.load()` reads
page 1 **through the pool**. Recovery has to complete before anything populates the cache, or the catalog
caches a schema read out of a file that is about to be rolled back underneath it:


```python
    pager = Pager.open(path) if path.exists() else Pager.create(path)
    recover_if_needed(path, pager)      # <-- HERE: after open, before the pool exists
    pool = BufferPool(pager)
    catalog = Catalog(pager, pool)
    catalog.load()
```


Putting it before `BufferPool` is construction makes the constraint structural rather than a comment:
there is no cache to invalidate because there is no cache yet.


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
    ("analyze_refresh",      ...),      # quill_stat1 is replaced atomically, never half-published
    ("multi_page_txn",       ...),      # several times the pool's capacity: forces the pool to grow,
                                        # and proves nothing reached the db before the barrier
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
    assert statistics_are_complete_or_absent(db)     # stale is safe; structurally partial is not
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


**The five assertions inside the loop are the deliverable, not the loop.** Anyone can crash a program;
what makes this evidence is that after every crash you check independent properties — the state is
unmixed, the trees are structurally valid, the indexes agree with their tables, planner statistics are
complete-or-absent, and an independently-written C implementation agrees the file is sound.


**Keep it fast enough to actually run.** The full product is thousands of cases. Practical shape: the
write matrix for one cheap scenario runs on every commit; the full product and the
crash-during-recovery test are marked slow and run in CI. A test suite you skip proves nothing.


**Also assert the negative:** with no fault injected, every scenario commits and leaves no journal. It's
embarrassingly easy to write a matrix that passes because the transaction never did anything.


---


## Week 5 sessions


| #   | 2 hours on                                                                             | Done when                                                 |
| --- | -------------------------------------------------------------------------------------- | --------------------------------------------------------- |
| **0** | **Session 0 above: write-intent API, move allocate/free, header as a page write, Pager additions** | **the existing suite is green with no behaviour change** |
| 1   | `journal.py`: header, records, `journal_checksum`, `begin` / `record_original`         | the magic-and-nRec-withheld test is green                 |
| 2   | `commit_barrier`, `replay`, and all four "replays nothing" cases                       | a hand-built corrupt journal is correctly ignored         |
| 3   | `Transaction`, wire up the inert hook, the write-barrier assertion, no-steal            | the byte-identical rollback hash test is green            |
| 4   | `BEGIN` / `COMMIT` / `ROLLBACK`, autocommit, `db.transaction()`                        | exception inside the context manager rolls back           |
| 5   | `recovery.py` + hot-journal detection wired into `connect()`                           | kill a process mid-txn, reopen, data is pre-transaction   |
| 6   | `FaultyFile`, and the write matrix for one scenario                                    | one scenario × every write boundary is green              |
| 7   | Full matrix, sync matrix, crash-during-recovery, `docs/durability.md`                  | all six scenarios green; the doc names the non-guarantees |


**This is eight sessions, not seven.** Session 0 is the price of the retrofit, and it is the one session
that cannot be cut — sessions 3 through 7 all assume `get_page_for_write` is the only route to a mutable
page. Take the extra two hours out of session 7's optional scenarios if the week is tight.


**Session 3 is still the one to slow down on**, but session 0 has already done its hardest part. What
remains is verifying the invariant actually holds now that something depends on it: grep for direct
`bytearray` mutation of pooled pages, and check that the write-barrier assertion fires if you
deliberately reorder `commit()`.


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
- [ ] Crash matrix green over every **write** boundary, all six scenarios, faulting both files
- [ ] Crash matrix green over every **fsync** boundary
- [ ] **Crash during recovery** green — the nastiest case, and the one that catches fsync-before-unlink
- [ ] After every crash point: unmixed state, valid trees, indexes agree with tables, statistics are
      complete-or-absent, and `integrity_check` is `ok`
- [ ] A transaction several times larger than the buffer pool commits correctly — the pool grew, and
      `pager.write_page` was **not called once** before `commit_barrier()`
- [ ] Pages allocated within the transaction are **not** journalled — and rollback still truncates them away
- [ ] `free_page` goes through the pool: freeing a page inside a transaction is journalled, and a
      rollback puts it back on the freelist it came from
- [ ] After `ROLLBACK`, the in-memory `FileHeader` matches the file — `freelist_trunk`, `freelist_count`,
      `change_counter` and `schema_cookie`, not just `page_count`
- [ ] A committed transaction that grew the file survives a crash **without** a clean `close()` —
      the header was stamped into page 1 at commit, not at close
- [ ] `BEGIN` / `ROLLBACK` work on `connect(":memory:")` and touch no filesystem path
- [ ] `PRAGMA synchronous = OFF | NORMAL | FULL` changes the fsync count; assert the counts
- [ ] `docs/durability.md` names the commit point precisely **and** lists the non-guarantees: lying
      `fsync`, torn sectors, no directory fsync after unlink, and what the matrix cannot simulate
- [ ] `NOTES.md` entries for every bug over 20 minutes — this week produces the best interview stories


**If the week runs short, cut in this order:** `synchronous` levels, then the sync matrix, then the
largest optional workload scenarios. **Never cut** session 0, the byte-identical rollback test, the write
matrix, or crash-during-recovery. The last three are the week's entire value, and session 0 is what makes
them mean anything — a crash matrix over a journal that some call site can bypass is a matrix that proves
nothing.