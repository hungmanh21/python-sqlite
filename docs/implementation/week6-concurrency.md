# quilldb — Implementation Plan: Week 6, Multi-Threaded Concurrency


← [Index](README.md)  ·  Prev: [Week 5 — Transactions](week5-transactions.md)  ·  Next: [Week 7 — Query Processing](week7-query-processing.md)


---


# Week 6 Spec — Locks, Threads, and Deadlock Detection ⭐


> **Read [chapter 15](../theory/txn/15-isolation-anomalies.md) before you write any code this week**
> and [chapter 16](../theory/txn/16-locking-and-deadlock.md) before `locks.py`. Week 6 is the other week
> where the first hour's decisions are structural: the `Database`/`Connection` boundary and "release locks
> only at commit" are both things you cannot retrofit cleanly.


This is the feature you actually asked for — several threads using the database at once, safely — and it's
the area backend interviews probe hardest after "can you write SQL."


**Write `docs/concurrency.md` in session 1, before the lock manager.** Not as documentation discipline: as
a design tool. Naming your isolation level forces you to decide when read locks are released, and
discovering in session 6 that you release them early — which silently downgrades you from serializable to
read-committed — is a bad time to find out.


---


## Week 6 contract


```python
db = quilldb.Database("app.db")         # ONE per file, thread-safe, shared


with ThreadPoolExecutor(8) as pool:     # a Connection per thread, NOT shared
    pool.map(worker, [db.connect() for _ in range(8)])
```


**`connect()` does not change its return type.** `quilldb.connect(path)` still hands back a
`Connection` — now one bound to a private `Database` that it owns and closes. Making it return a
`Database` would break `__init__.py`'s `__all__`, every test in `src/tests/unit/test_connection.py`,
and the differential harness, in exchange for nothing: `Database` is a new name, not a replacement one.


```python
conn = quilldb.connect("app.db")        # unchanged: a Connection, on a private Database
```


Supported:


- one `Database` shared by all threads; one `Connection` per thread
- table-level shared/exclusive locks, **strict 2PL** — everything released at commit/rollback
- one writer at a time overall
- deadlock **detection** via a wait-for graph, aborting the youngest victim with `DeadlockError`
- `busy_timeout` as a backstop, raising `LockTimeoutError`
- `BEGIN IMMEDIATE` — declare write intent up front


Not supported, rejected with a typed error:


- sharing one `Connection` across threads (detect it and raise; don't let it corrupt silently)
- multi-**process** access — no `fcntl`, no advisory file locks. A second process opening the same file is
  undefined; document it as a known limitation
- row-level locks, intention locks, lock escalation
- WAL, so readers and writers on the same table do block each other


**The stated isolation level is serializable** (chapter 15 §15.6), and the two things that make that true
are worth restating because they're the two things easiest to break: locks are at **table** granularity
(so phantoms can't occur) and they're held **until commit** (so nothing reads uncommitted data).


---


## Week 6 file map


```
src/quilldb/
├── txn/
│   ├── locks.py            NEW — LockMode, LockManager, wait-for graph
│   ├── transaction.py      + acquire locks during execution, release at commit
│   └── journal.py          + ownership moves to Database — one -journal per file (§37.3)
├── api/
│   ├── database.py         NEW — Database: pager, pool, lock manager, catalog, txn ids
│   └── connection.py       Connection: per-thread txn state, cursors, _check_thread
├── storage/
│   ├── pager.py            + thread-safe page I/O, ONE allocate_page (§37.1, §37.3)
│   └── bufferpool.py       + latching, pins under lock, safe eviction, narrowed clear()
├── catalog/catalog.py      + re-load() when the schema cookie moved (§37.4)
├── sql/
│   ├── tokenizer.py        + IMMEDIATE keyword
│   ├── parser.py           + BEGIN IMMEDIATE
│   ├── ast.py              + Begin.immediate
│   └── binder.py           + BoundBegin.immediate
└── errors.py               + DeadlockError, LockTimeoutError, ThreadingError


src/tests/concurrency/      NEW
├── test_lock_manager.py
├── test_deadlock.py
├── test_transfer_stress.py     ← the deliverable
├── test_pager_thread_safety.py
└── test_pool_thread_safety.py


benchmarks/concurrent.py        throughput vs thread count, for the README
docs/concurrency.md             the isolation level and what it permits
pyproject.toml                  + pytest-timeout (dev group), testpaths fix
```


The four storage/catalog files are the ones the roadmap does not list, and §37 is why they are here.
Week 5 left `pager`, `bufferpool`, `journal`, and `catalog` wired as though one thread existed —
because one did.


---


## 34. Week 6 additions to `errors.py`


```python
class DeadlockError(QuillDBError):
    """This transaction was chosen as the victim to break a lock cycle.


    Carries the cycle so the message is diagnosable — this is the entire
    advantage of detection over a timeout, so don't throw it away:


        DeadlockError: transaction 7 aborted to break cycle
                       txn 7 waits for 'orders' (held by txn 4)
                       txn 4 waits for 'users'  (held by txn 7)


    The transaction is rolled back before this is raised. The caller may retry.
    """


    def __init__(self, victim: int, cycle: list[tuple[int, str, int]]) -> None: ...




class LockTimeoutError(QuillDBError):
    """The busy timeout expired. NOT a deadlock — the backstop fired.


    If you see this in tests where you expect DeadlockError, your detection has
    a gap. Keep the two distinct; collapsing them hides real bugs.
    """




class ThreadingError(QuillDBError):
    """A Connection was used from a thread other than its owner."""
```


Keeping `DeadlockError` and `LockTimeoutError` separate is a deliberate design choice with a payoff. A
`LockTimeoutError` appearing in a test that constructs a genuine cycle means your cycle detection missed
it — and if both raised the same type, you would never notice.


---


## 35. `txn/locks.py`


### The stub


```python
class LockMode(Enum):
    SHARED = 1
    EXCLUSIVE = 2




COMPATIBLE = {
    (LockMode.SHARED,    LockMode.SHARED):    True,
    (LockMode.SHARED,    LockMode.EXCLUSIVE): False,
    (LockMode.EXCLUSIVE, LockMode.SHARED):    False,
    (LockMode.EXCLUSIVE, LockMode.EXCLUSIVE): False,
}




@dataclass
class LockEntry:
    holders: dict[int, LockMode]       # txn_id -> mode
    waiters: list[tuple[int, LockMode]]  # FIFO, so nobody starves
    condition: threading.Condition     # ONE PER RESOURCE (chapter 16 §16.7)




class LockManager:
    def __init__(self, default_timeout: float = 5.0) -> None:
        self._locks: dict[str, LockEntry] = {}
        self._latch = threading.Lock()      # guards _locks itself — a LATCH, not a lock
        self._waiting_for: dict[int, str] = {}   # the wait-for graph, one edge per txn


    def acquire(self, txn_id: int, resource: str, mode: LockMode,
                timeout: float | None = None) -> None:
        """Block until granted.


        Sketch:
          1. If already held by txn_id in a sufficient mode, return (reentrant).
          2. If compatible with all current holders AND no waiters ahead, grant.
          3. Otherwise: record the wait-for edge, run _detect_deadlock, and if a
             cycle exists raise DeadlockError for the victim. Else wait on the
             condition IN A WHILE LOOP.


        Raises:
            DeadlockError, LockTimeoutError.
        """


    def release_all(self, txn_id: int) -> None:
        """Drop every lock held by this transaction and notify each resource's
        waiters. There is deliberately no release_one() — see §16.2."""


    def _detect_deadlock(self, waiting_txn: int) -> int | None:
        """Follow wait-for edges from `waiting_txn`. If the walk returns to
        `waiting_txn`, that's a cycle; return the victim (the YOUNGEST txn in
        the cycle — highest id, since ids increase). Otherwise None.


        Called only at the moment a transaction blocks, because that's the only
        moment a new edge appears (§16.7).
        """
```


### The upgrade problem, which the roadmap doesn't mention and which will bite you


A transaction reads a table (SHARED), then writes it (needs EXCLUSIVE). That's a **lock upgrade**, and
it's the classic deadlock generator:


```
A holds SHARED(users), wants EXCLUSIVE(users)
B holds SHARED(users), wants EXCLUSIVE(users)
```


Neither can be granted because the other's SHARED is in the way. **Two transactions that never touched a
second resource are now deadlocked** — and if you only build cycle detection over distinct resources, you
may not even model this.


Three options, and pick one deliberately:


| Option | How | Cost |
|---|---|---|
| Detect it | treat an upgrade request as a wait-for edge to every other SHARED holder | your graph must handle multiple edges per txn |
| Prevent it | acquire EXCLUSIVE up front if the statement might write | less concurrency; needs the planner to say |
| Sidestep it | single writer: take one global write lock before any EXCLUSIVE table lock | trivially correct, serializes all writers |


**Recommendation: sidestep it.** You already committed to a single writer (chapter 15 §15.6). Make that
explicit as a `"__writer__"` resource acquired EXCLUSIVE before any table write. Then no two transactions
ever contend for an upgrade, because only one of them is a writer at all. It's three lines, it makes the
single-writer guarantee *structural* rather than emergent, and it removes an entire class of deadlock.


Note honestly what it costs: two transactions writing *different* tables now serialize. Say so in
`docs/concurrency.md`.


### The tests


```python
def test_shared_locks_coexist(lm):
    lm.acquire(1, "users", LockMode.SHARED)
    lm.acquire(2, "users", LockMode.SHARED)      # must not block




def test_exclusive_excludes(lm):
    lm.acquire(1, "users", LockMode.EXCLUSIVE)
    with pytest.raises(LockTimeoutError):
        lm.acquire(2, "users", LockMode.SHARED, timeout=0.1)




def test_release_all_wakes_waiters(lm):
    lm.acquire(1, "users", LockMode.EXCLUSIVE)
    granted = threading.Event()
    threading.Thread(target=lambda: (lm.acquire(2, "users", LockMode.SHARED),
                                     granted.set()), daemon=True).start()
    assert not granted.wait(0.1)
    lm.release_all(1)
    assert granted.wait(2.0)          # generous: this asserts progress, not latency




def test_waiters_are_fifo_so_nobody_starves(lm):
    """A stream of readers must not indefinitely postpone a waiting writer —
    chapter 16 §16.5's writer starvation, in your own lock manager."""




def test_deadlock_is_detected_and_one_side_commits(db):
    """The important assertion is the SECOND one."""
    results = run_crossed_transactions(db, timeout=10)
    assert sum(isinstance(r, DeadlockError) for r in results) == 1
    assert sum(r is None for r in results) == 1        # the other MUST succeed




def test_deadlock_error_names_the_cycle(db):
    err = provoke_deadlock(db)
    assert len(err.cycle) == 2
    assert {c[1] for c in err.cycle} == {"users", "orders"}
```


`test_waiters_are_fifo_so_nobody_starves` is the one people skip, and it's testing the failure mode from
chapter 16 §16.5 — a liveness bug that every correctness test passes straight through. FIFO waiter queues
are how you get it right: a waiting EXCLUSIVE request blocks *later* SHARED requests, exactly as PENDING
does for SQLite.


---


## 36. `api/database.py` and `api/connection.py` — the split that is the design


```python
class Database:
    """ONE per file. Thread-safe. Shared by every thread.


    Owns everything global: the pager, the buffer pool, the lock manager, the
    catalog cache, and the transaction-id counter.
    """
    def connect(self) -> "Connection":
        """A new Connection bound to the calling thread."""




class Connection:
    """ONE per thread. NOT thread-safe. Never share one.


    Owns: the current transaction, open cursors, autocommit state, and the id
    of the thread that created it.
    """
    def __init__(self, db: Database) -> None:
        self._owner_thread = threading.get_ident()


    def _check_thread(self) -> None:
        """Called at the top of every public method.


        Raises:
            ThreadingError: used from a different thread.
        """
        if threading.get_ident() != self._owner_thread:
            raise ThreadingError(
                f"Connection created in thread {self._owner_thread} used from "
                f"{threading.get_ident()}. Create one Connection per thread."
            )
```


**`_check_thread()` costs one comparison and it is the highest-value ten lines in the week.** Sharing a
`Connection` across threads produces interleaved cursor state and half-applied transactions —
non-deterministic corruption that looks like a B-tree bug. Turning it into an immediate, clearly-worded
exception converts your worst debugging session into a five-second fix, for the user *and* for you.


### The shared state that people miss


Obvious shared state gets latched because it's obvious. Here is the list that doesn't:


| State | Why it's shared | What goes wrong |
|---|---|---|
| buffer pool frames + its map | one pool per file | torn frames, lost pins, double eviction |
| the 100-byte file header in memory | one copy | two threads allocate the same page number |
| **freelist head** | lives in the header | **two transactions get the same "free" page** |
| **catalog cache** | schemas parsed once | thread B runs against a schema that `CREATE INDEX` changed |
| transaction-id counter | must be unique | two transactions with one id, so `release_all` frees the wrong locks |


The freelist one is worth dwelling on: `allocate_page()` reads `header.freelist_trunk`, decides, and
writes it back. Two threads doing that concurrently both get page 9, both initialize it, and one tree
silently adopts another tree's page. `integrity_check` will catch it — as "page 9 is referenced twice" —
which is a good demonstration of why you keep running it.


⚠️ There are currently **two** `allocate_page()` implementations, on `Pager` and on `BufferPool`, both
writing `header.freelist_trunk` directly. Latching one leaves the race live in the other — see §37.3,
which resolves it before this table's first row means anything.


The catalog cache is the one that produces the weirdest bug reports. Bump the schema cookie on every DDL
statement and have each `Connection` re-read the catalog when it changes.

Be precise about what week 3 actually left you, though: `create_table()` and `create_index()` *write* the
cookie, and **nothing anywhere reads it.** The read side — one `Catalog` on the `Database`, a
`_last_seen_cookie` per `Connection`, a re-`load()` when it moved — is new code this week, not decoration
being switched on. §37.4 sizes it.


---


## 37. Thread-safe storage — the pager first, then the pool


Week 5 left five pieces of storage state that are correct for one thread and wrong for two. All five
become reachable the moment a `Database` is shared, and none of them are in the roadmap as written.
Do them before the stress test, not after: a stress test that runs on top of these bugs does not fail
loudly, it fails one run in fifteen and looks like a B-tree bug.


### 37.1 `Pager` is the least thread-safe object in the tree


`read_page()` and `write_page()` are `self._file.seek(offset)` followed by a read or a write, on ONE
shared file handle. Two threads interleaving between the seek and the read means thread A receives the
page thread B asked for.


The GIL does not save you. `seek` and `read` are two separate calls and the interpreter may switch
between them; the GIL prevents data races, not race conditions (chapter 16 §16.7).


```python
# storage/pager.py
    def read_page(self, page_id: int) -> bytearray:
        """Stateless, offset-carrying I/O: os.pread performs the seek and the
        read as one syscall, so there is no window between them to interleave
        in — and no shared cursor for a second thread to move."""
        return bytearray(os.pread(self._fd, PAGE_SIZE, (page_id - 1) * PAGE_SIZE))
```


⚠️ `Pager.memory()` is backed by `io.BytesIO`, which has no `pread`. The in-memory path needs a plain
`threading.Lock` around seek-then-read instead, so this is two implementations, not one. Do not let
`:memory:` go untested here — most of the unit suite runs on it.


### 37.2 `pool._txn` / `pager._txn` is ONE slot


`Connection._begin()` does `self.pager._txn = txn; self.pool._txn = txn`. That single hook drives both
`will_modify()`'s journalling and `write_page()`'s barrier assertion. With N connections on one shared
pool it is a global variable holding per-transaction state.


It is sound — but only because `__writer__` is exclusive, so at most one writer exists at a time. The
integrity of the journal is therefore a *consequence of the lock manager*, which is worth saying out
loud rather than rediscovering at 2am. Two rules follow:


- **A read-only transaction must never touch the hook.** §38's "autocommit still gets a transaction" is
  about *locks*, not journalling. A `SELECT`'s implicit transaction takes SHARED and releases at commit;
  it never writes, so it must not construct a `Journal` and must not assign `pool._txn`. If it does, it
  clobbers a concurrent writer's hook and the barrier assertion starts passing when it should fire —
  the assertion that is your last line of defence against the one unrecoverable ordering bug.
- **Rename it while you are there.** `Database._writer_txn`, set and cleared under the writer lock, says
  what it is. `pool._txn` reads like per-connection state and is not.


### 37.3 There are TWO `allocate_page()` implementations


`Pager.allocate_page()` and `BufferPool.allocate_page()` both mutate `pager._header.freelist_trunk`
directly. Latch one and you ship the race in the other — and the DoD test below only exercises whichever
one the operators happen to call.


**Pick one before you latch anything.** `BufferPool.allocate_page()` is the one the tree actually uses
(it has to seed a cache entry for the new page), so make `Pager.allocate_page()` private or delete it.
The header fields involved — `freelist_trunk`, `freelist_count`, `page_count` — then have exactly one
writer and one latch covers all three.


`Journal` has the same shape of problem in miniature: it is constructed per-`Connection` from the
database path, so N connections name one `-journal` file. Also sound only under `__writer__`. Move its
ownership to `Database` and the soundness stops being a coincidence.


### 37.4 The schema cookie is written but never read


The roadmap says you built the cookie in week 3 "for exactly this purpose." Half true:
`create_table()` and `create_index()` call `pager.bump_schema_cookie()`, and **nothing in the tree ever
reads it.** `Catalog` caches `_tables`/`_indexes` in memory, and there is one `Catalog` per `connect()`
today, so no invalidation has ever been needed.


Week 6 builds the whole read side: one `Catalog` owned by the `Database`, a `_last_seen_cookie` per
`Connection`, and a re-`load()` at the top of `execute()` when it moved. That is roughly forty lines and
it is a session-3 concern (it is a `Database`-ownership question), not a freebie — which also means
"cut the catalog-cache invalidation" is a bigger cut than it sounds like.


### 37.5 Rollback's `pool.clear()` is a global operation on shared state


`Transaction.rollback()` ends with `pool.clear()`, which drops every entry "dirty or clean, pinned or
not". That is correct and necessary for week 5's single thread, because replay rewrote the file
underneath the cache. With concurrent readers it detaches frames another thread is holding a live
`bytearray` reference to: that thread keeps reading a buffer the pool has forgotten about, and the next
`get_page()` hands out a second copy of the same page.


Table-level 2PL does not cover this. Locks are per table; pins are per page, and *every* transaction
touches page 1.


| Option | How | Cost |
|---|---|---|
| Wait it out | rollback blocks until every pin count reaches 0 | a reader that never finishes blocks rollback forever, and no lock orders this wait |
| Lock everyone out | the writer also takes EXCLUSIVE on a `"__schema__"` resource across the rollback | correct, but serializes rollback against all readers, including on tables it never touched |
| **Narrow the blast radius** | `clear()` takes the set of pages replay actually restored and drops only those | needs `Transaction._journalled`, which already exists |


**Recommendation: narrow it.** `Transaction` already tracks exactly which pages it journalled, and those
plus page 1 are the only pages replay changed — every other cached page is still a faithful copy of the
file. `clear(pages: set[int] | None = None)`, with `None` keeping today's wholesale behaviour for
recovery, is a small change and it is the only one of the three that does not trade a correctness bug
for a liveness bug.


Pages above `page_count_before` are the exception worth naming: `truncate()` erased them, so they must
leave the cache too, pinned or not. A reader cannot legitimately hold one — those pages did not exist
when it started — so `discard()`ing them is safe, and asserting their pin count is 0 is a good tripwire.


### 37.6 And then the pool itself


```python
class BufferPool:
    def __init__(self, ...):
        self._latch = threading.Lock()        # guards the frame map and LRU order


    def pin(self, page_id: int) -> bytearray:
        """Under the latch: find or allocate a frame, increment its pin count.


        ⚠️ THE LATCH MUST NOT BE HELD DURING I/O (chapter 16 §16.3). If a page
        must be read from disk:
            - insert a placeholder frame, mark it 'loading', pin it
            - RELEASE the latch
            - do the read
            - re-acquire, mark it ready, notify anyone who waited on it
        Holding the latch across a read serializes the whole pool on disk
        latency and turns an 8-thread benchmark into a 1-thread one.
        """


    def _choose_victim(self) -> int | None:
        """Under the latch. Skip frames that are pinned OR dirty-in-open-txn
        (no-steal, week 5 §30). Return None if nothing is evictable — the
        caller must then grow or raise, never evict something unsafe."""
```


### The tests


```python
def test_concurrent_reads_never_return_the_wrong_page(db):
    """§37.1, and the cheapest test of the week. 8 threads each read a
    DIFFERENT known page in a tight loop; every read must return the page it
    asked for. Fails reliably on seek-then-read, passes on pread. Run it
    against :memory: too — that path takes the latch, not pread."""


def test_a_pinned_page_is_never_evicted_under_contention(db):
    """A cursor holds a pinned page while 7 threads thrash the pool. The pinned
    page's contents must be unchanged and its frame must be the same frame."""


def test_concurrent_allocate_never_returns_the_same_page_twice(db):
    """The freelist race. 8 threads x 200 allocations; assert 1600 DISTINCT
    page numbers. This test is short, and it is the one that catches the bug
    that would otherwise show up as mysterious tree corruption a week later.

    Assert first that there is only ONE allocate_page() left to test (§37.3):
    latching the pool's copy while the pager's copy survives means this passes
    and the database still corrupts."""
    got = run_concurrent_allocations(db, threads=8, per_thread=200)
    assert len(set(got)) == len(got) == 1600


def test_rollback_does_not_drop_a_readers_pinned_page(db):
    """§37.5. Thread A holds a pinned page of table `t1` while thread B rolls
    back a transaction that only touched `t2`. A's page must still be in the
    pool afterwards — same frame, same contents."""


def test_ddl_in_one_thread_is_visible_to_another(db):
    """Thread A creates an index; thread B must not keep planning against a
    stale catalog. Checks the schema cookie path — which is NEW code, not a
    wiring job: nothing read the cookie before this week (§37.4)."""
```


---


## 38. Wiring 2PL into execution


```python
# txn/transaction.py
    def lock_for_read(self, table: str) -> None:
        self._db.lock_manager.acquire(self.id, table, LockMode.SHARED, self._timeout)


    def lock_for_write(self, table: str) -> None:
        # Single writer: one global lock first, which removes lock upgrades
        # entirely (§35). Always this order — it IS the global lock ordering
        # that makes the remaining deadlock cases table-vs-table.
        self._db.lock_manager.acquire(self.id, "__writer__", LockMode.EXCLUSIVE, self._timeout)
        self._db.lock_manager.acquire(self.id, table, LockMode.EXCLUSIVE, self._timeout)
```


**Where the calls go: in the operators' `open()`, not in the pager.** An operator knows which table it is
about to touch and knows it before reading anything; the pager only sees page numbers, by which point the
lock should already be held. `SeqScan.open()` and `IndexScan.open()` take SHARED; `InsertOp`, `DeleteOp`,
`UpdateOp` take EXCLUSIVE. An index scan must lock the **table**, not the index — the index is derived
data belonging to that table, and locking them separately invents a second lock order for no benefit.


**Autocommit still gets a transaction — for the locks, not for the journal.** A bare `SELECT` opens an
implicit transaction, takes SHARED, runs, commits, releases. Skipping the lock for "just a read" is how
you get a read that observes a writer's half-applied page.


But a read-only transaction must NOT construct a `Journal` or assign `pool._txn` (§37.2): there is
nothing to undo, and that hook has one slot which belongs to the writer. Make read-only the default and
promote to a journalling transaction on the first `lock_for_write()`.


```python
def test_locks_are_held_until_commit_not_released_early(db):
    """The test that proves strict 2PL, and the one that catches an accidental
    downgrade to read-committed."""
    c = db.connect()
    c.execute("BEGIN")
    c.execute("SELECT * FROM users").fetchall()     # takes SHARED
    other = db.connect()
    other.busy_timeout = 0.2
    with pytest.raises((LockTimeoutError, DeadlockError)):
        other.execute("UPDATE users SET age=1")     # must be blocked
    c.execute("COMMIT")
    other.busy_timeout = 5.0
    other.execute("UPDATE users SET age=1")         # now fine
```


The timeout is a **connection attribute, not an `execute()` keyword.** `execute(sql, parameters)` is the
DB-API shape week 3 committed to and `parameters` is positional #2, so a `timeout=` keyword there would
be both a wart and a hazard. `sqlite3` spells it `busy_timeout` for the same reason.


---


## 39. The stress test and the benchmark


```python
# tests/concurrency/test_transfer_stress.py


def sum_balances(db) -> int:
    """A Python-side fold, NOT `SELECT SUM(balance)`. Aggregates are week 7 —
    there is no SUM or COUNT in the tokenizer yet, and writing this helper as
    SQL turns the week's headline deliverable into a week-7 dependency.

    Call it between phases, or take __writer__, so the fold sees one snapshot.
    """
    conn = db.connect()
    try:
        return sum(row[0] for row in conn.execute("SELECT balance FROM accounts"))
    finally:
        conn.close()


@pytest.mark.slow
@pytest.mark.timeout(120)
def test_sum_of_balances_never_changes(tmp_path):
    """THE deliverable. 8 threads, 10k transfers between random accounts.


    The invariant is not that every transfer succeeded — some will hit
    DeadlockError and that's correct behaviour. The invariant is that money is
    neither created nor destroyed (chapter 15 §15.0).
    """
    db = setup_accounts(tmp_path, accounts=50, each=1000)
    before = sum_balances(db)


    def worker(seed):
        rng = random.Random(seed)
        for _ in range(1250):
            src, dst = rng.sample(range(50), 2)
            for attempt in range(5):                 # retry on deadlock: that's the contract
                try:
                    transfer(db.connect(), src, dst, rng.randint(1, 10))
                    break
                except DeadlockError:
                    continue


    with ThreadPoolExecutor(8) as pool:
        list(pool.map(worker, range(8)))


    assert sum_balances(db) == before
    assert validate_all_btrees(db).is_valid
    assert min(all_balances(db)) >= 0                # no account went negative
    db.close()
    assert sqlite3_integrity_check(tmp_path / "t.db") == "ok"


@pytest.mark.slow
@pytest.mark.timeout(600)
@pytest.mark.parametrize("run", range(20))
def test_stress_is_not_flaky(tmp_path, run):
    """A concurrency test that passed once has told you almost nothing."""
    test_sum_of_balances_never_changes(tmp_path / f"run{run}")
```


**Seed every RNG.** A concurrency failure you cannot reproduce costs an entire session. Seeded workers plus
a recorded thread schedule is the difference between "it failed in CI once" and a fixable bug.


**Add a hang guard.** Every concurrency test should run under a timeout that *fails* rather than blocks —
`pytest-timeout`, or a watchdog thread that dumps all stack traces and aborts. The failure mode you're
testing for includes "the process never finishes," and a test suite that hangs in CI is worse than one that
fails.



**The hang guard needs a dependency and a marker, neither of which exists yet.** `pytest-timeout` is
not in `pyproject.toml` — add it to the `dev` group. And `addopts = "-m 'not slow'"` is already in
force, so an unmarked 20×-parametrized stress test would run on every bare `pytest`; mark both stress
tests `slow` and run them deliberately. While you are in that file, `testpaths = ["tests"]` has never
matched this repo's `src/tests/` layout (pytest falls back to recursive discovery and warns) — adding
`src/tests/concurrency/` is the moment to fix it to `["src/tests"]`.


```python
# benchmarks/concurrent.py — for the README
#   threads | txn/sec | note
#   1       |  ...    |
#   2       |  ...    |
#   4       |  ...    |
#   8       |  ...    | write throughput is GIL- and single-writer-bound
#
# Report reads and writes separately. Writes will not scale — say so, explain
# that it's the single-writer design plus the GIL, and show that READ
# throughput does scale because SHARED locks are compatible. Being honest about
# the ceiling makes the rest of the numbers credible (chapter 16 §16.7).
```


---


## Week 6 sessions


| # | 2 hours on | Done when |
|---|---|---|
| 1 | `docs/concurrency.md`, errors, `LockManager` skeleton: compatibility, acquire/release, per-resource conditions | shared coexist, exclusive excludes, release wakes waiters |
| 2 | Wait-for graph, cycle detection, youngest-victim policy, `__writer__` lock | a crossed pair produces exactly one victim and one commit |
| 3 | `Database`/`Connection` split, `_check_thread`, txn-id counter, `Catalog` + `Journal` ownership moved to `Database`, the schema-cookie read path (§37.4) | cross-thread `Connection` raises `ThreadingError`; DDL in one thread is visible in another |
| 4 | **Storage safety: `pread`/`pwrite` + the `BytesIO` latch (§37.1), one surviving `allocate_page` (§37.3)**, then pool latching, pins under lock, no-steal eviction | 8 threads reading 8 pages never cross; 8×200 allocations yield 1600 distinct pages |
| 5 | 2PL wiring in operator `open()`, read-only vs journalling transactions (§37.2), narrowed `clear()` (§37.5), `BEGIN IMMEDIATE` | locks-held-until-commit is green; rollback doesn't drop a reader's pinned page |
| 6 | `busy_timeout`, FIFO waiters, the transfer stress test | sum of balances holds over 10k transfers |
| 7 | 20× flake hunt, `benchmarks/concurrent.py`, finish `docs/concurrency.md` | stress test green 20 runs in a row; the table is in the README |


**Session 4 grew.** It used to be "latch the pool"; it is now "make storage thread-safe, of which the
pool is the last third." Sessions 3 and 5 each absorbed one §37 item too. That is the honest cost of the
five things week 5 left single-threaded, and it is better paid here than during session 7's flake hunt.


**Session 7 is not padding.** A concurrency bug that appears one run in fifteen will appear during a demo.
Budget the whole session for running the stress test repeatedly and fixing what falls out.


---


## Week 6 definition of done


- [ ] `docs/concurrency.md` names the isolation level **and lists the anomalies it permits**, including
      the same-connection caveat
- [ ] Shared locks coexist; exclusive excludes both modes
- [ ] Locks are released **only** at commit/rollback — no `release_one()` exists in the codebase
- [ ] A read lock held to commit blocks another connection's write (strict 2PL, proven by test)
- [ ] Waiters are FIFO, so a stream of readers cannot postpone a waiting writer indefinitely
- [ ] A deliberate deadlock is detected: exactly one `DeadlockError`, **and the other transaction commits**
- [ ] `DeadlockError` names the cycle — both transactions and both resources
- [ ] `LockTimeoutError` and `DeadlockError` are distinct, and no genuine cycle produces the former
- [ ] The single-writer `__writer__` lock removes lock upgrades entirely
- [ ] `Connection` used from the wrong thread raises `ThreadingError` immediately
- [ ] `quilldb.connect()` still returns a `Connection`; week 5's tests and the differential harness pass
      unchanged
- [ ] Page I/O is atomic per call: `os.pread`/`os.pwrite` on the disk path, a latch on the `:memory:`
      `BytesIO` path — and both are tested
- [ ] 8 threads reading 8 known pages in a loop never receive a page they did not ask for
- [ ] Exactly ONE `allocate_page()` implementation remains; the other is deleted or made private
- [ ] 8×200 concurrent `allocate_page()` calls return 1600 distinct pages (the freelist race)
- [ ] A read-only transaction never constructs a `Journal` and never assigns the writer hook
- [ ] `Journal` and `Catalog` are owned by `Database`, not constructed per-`Connection`
- [ ] Rollback does not evict a concurrent reader's pinned page from a table it never touched
- [ ] A pinned page is never evicted under pool contention
- [ ] The buffer-pool latch is **never** held across disk I/O
- [ ] DDL in one thread invalidates the other threads' catalog cache via the schema cookie — and note the
      cookie's **read side is new code this week**, not existing wiring
- [ ] Transfer stress test: 8 threads × 10k transfers, sum unchanged, no negative balances, trees valid
- [ ] `sum_balances` is a Python-side fold, not `SELECT SUM(...)` — aggregates are week 7
- [ ] `integrity_check` is `ok` after the full stress run
- [ ] The stress test passes **20 consecutive runs** with no flakes
- [ ] Every concurrency test runs under a hang guard that fails rather than blocking CI: `pytest-timeout`
      in the dev group, stress tests marked `slow`
- [ ] `benchmarks/concurrent.py` reports read and write throughput separately, with the GIL and
      single-writer ceilings stated plainly
- [ ] The multi-process limitation is documented as out of scope, not left implied
- [ ] `NOTES.md` entries for every bug over 20 minutes


**If the week runs short, cut in this order:** the benchmark table, then `BEGIN IMMEDIATE`, then the
catalog-cache invalidation (single-threaded DDL is a documentable limitation — but see §37.4: that is
now a real ~40-line cut rather than a one-liner, so decide it at the start of session 3 instead of
discovering it in session 7). **Never cut** §37.1's page-I/O fix, the transfer stress test, the deadlock
test's "the other side commits" assertion, or the 20× flake hunt.


Nothing in §37.1–37.5 is optional. They are the difference between a stress test that proves something
and a stress test that passes because eight threads happened not to interleave.