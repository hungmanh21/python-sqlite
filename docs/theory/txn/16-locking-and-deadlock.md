# 16 — Locking, 2PL, and Deadlock


> **Read during week 6, before you write `txn/locks.py`.**
>
> **Time:** ~50 minutes. **Prerequisite:** [chapter 15](15-isolation-and-anomalies.md) — you need to know
> what you're guaranteeing before you build the machinery that guarantees it.


---


## 16.0 The problem: one mutex is correct, and useless


Chapter 15 established the goal: serializability. Here is a complete, correct implementation:


```python
class Database:
    def __init__(self):
        self._lock = threading.Lock()


    def transaction(self):
        self._lock.acquire()        # release at commit
```


One global mutex. Transactions run strictly one at a time, so every execution *is* a serial execution.
Every anomaly in chapter 15 is impossible. No deadlock is possible either, because there's only one lock
to hold.


**This is not a straw man.** It is genuinely correct, it is ten lines, and for many embedded workloads it
is the right answer. SQLite in rollback-journal mode is not far from it.


The problem is throughput: a read-only query that touches one table blocks every other transaction in the
system. So the real question of this chapter is not "how do I get correctness" — you just did — but:


> **How much concurrency can I add back without losing the property?**


Every mechanism below is an answer to that, and each buys concurrency by taking on a new problem. That
framing is worth holding onto, because it explains why the machinery gets complicated: **complexity here
is purchased, and the thing it buys is concurrency.**


---


## 16.1 Readers don't conflict with readers


The first and largest win. Two transactions reading the same table cannot interfere — neither changes
anything. So distinguish two lock modes:


|  | Other holds **Shared** | Other holds **Exclusive** |
|---|---|---|
| Want **Shared** (read) | ✅ compatible | ❌ wait |
| Want **Exclusive** (write) | ❌ wait | ❌ wait |


Three of four cells conflict, but the one that doesn't is the common case: most workloads are
read-dominated, so allowing unlimited concurrent readers recovers most of the available parallelism for
one small table.


**The second win is granularity.** Lock *tables* rather than the whole database, and two transactions
working on unrelated tables never meet. Combined with shared/exclusive, that's most of the practical
benefit of a lock manager, from two ideas.


**Where it stops:** row-level locking is the next step and it's a much bigger one, because — per chapter
15 §15.1 — as soon as you lock individual rows, phantoms come back and you need gap or predicate locks to
banish them again. **Table granularity is the sweet spot where strong isolation is still cheap**, and
that's the reason to stop there rather than an apology for stopping there.


---


## 16.2 Two-phase locking: the second phase is the half that matters


2PL is usually stated as: **a transaction acquires all the locks it needs (growing phase), and only then
begins releasing them (shrinking phase). Once it has released any lock, it may not acquire another.**


Everyone remembers the acquiring. The rule that does the work is the second half — *once you release
anything, you may acquire nothing more* — and here is why, because the counterexample is short:


```
        Transaction A                        Transaction B
  ──────────────────────────────────   ──────────────────────────────────
  lock(accounts, X)
  read+write accounts
  unlock(accounts)              ← released early
                                       lock(accounts, X)
                                       read accounts   (sees A's change)
                                       lock(audit, X); write audit
                                       COMMIT
  lock(audit, X)                ← acquiring AFTER releasing
  write audit
  COMMIT
```


Now look for a serial order that explains this. B read A's write to `accounts`, so A must come first. But
A wrote `audit` after B did, so B's audit row is older — meaning B came first. **Both orderings are
contradicted, so the schedule is not equivalent to any serial execution.** The interleaving is only
possible because A released `accounts` and then acquired `audit`.


The rule prevents exactly this. If A holds `accounts` until it has `audit`, B cannot slot in between.


### Strict 2PL, and the second reason to hold until commit


**Strict 2PL** says: hold *every* lock until commit or rollback. The shrinking phase becomes a single
instant at the end.


Basic 2PL gives serializability. Strict 2PL adds something else, and it's the reason essentially every
real system uses it:


> **Without it, one abort can force other transactions to abort — a cascade.**


If A releases a lock after writing, B reads that value, and *then* A rolls back, B has read data that
never existed and must be aborted too. And whatever read B's output must abort as well. Holding write
locks to commit means no one can ever see uncommitted data, so **no abort ever cascades**.


Note this is the same property week 5 already gave you from a different direction: a writer's changes
live in its own memory until commit, so there is nothing to read. Strict 2PL and the journal are two
mechanisms enforcing one invariant — **uncommitted state is invisible** — which is why they compose
cleanly.


**For quilldb, hold both shared and exclusive locks until commit.** It's simpler (release happens in
exactly one place), it's what makes the serializability claim in chapter 15 §15.6 true including
phantoms, and "I use strict 2PL" is a precise statement you can defend.


> **Say this out loud:** "Two-phase locking has a growing phase and a shrinking phase, and the load-bearing
> rule is that once you release a lock you may not acquire another — otherwise two transactions can
> interleave so that each appears to precede the other. Strict 2PL holds everything to commit, which
> additionally means nobody ever reads uncommitted data, so an abort never cascades into other
> transactions."


---


## 16.3 Locks and latches are different things


This distinction is missing from most tutorials and it will make your week-6 code much clearer. You need
**both**, and confusing them produces either deadlocks or corruption.


| | **Lock** | **Latch** |
|---|---|---|
| Protects | logical data — a table, a row | a physical structure — one page, the pool's own map |
| Held for | the transaction's duration | a few instructions |
| Granted to | a transaction | a thread |
| Modes | shared / exclusive, extensible | mutex, or read-write |
| Deadlock | **possible** — needs detection | **impossible by construction** — strictly ordered, never held across a wait |
| Held during I/O | yes | **no, never** |
| In quilldb | `LockManager` | `threading.Lock` on the pool and on frames |


**The example that makes it concrete.** Two threads both want page 47 in the buffer pool. That's not a
transaction-level conflict — they might be in the same transaction — it's a data race on the pool's
internal dictionary. You need a mutex around the pool for the microseconds it takes to look up a frame.
That's a latch, and putting it in the lock manager would be absurd: it doesn't belong to a transaction,
it isn't shared/exclusive in the logical sense, and it must never be held while you wait for anything.


**The rule that keeps latches deadlock-free:** acquire them in a fixed global order and never hold one
while blocking. If you `read_page()` (which does I/O, which can block) while holding the pool latch, you
have serialized the entire buffer pool on disk latency — and if that I/O path can itself want the latch,
you've deadlocked on a structure that "can't deadlock."


**Your week-4 pin counts are part of this story too.** A pinned page must not be evicted; the pin is a
long-lived *reservation* (closer to a lock), while the mutex protecting the pin count is a latch. Two
mechanisms, two lifetimes, one data structure.


---


## 16.4 Deadlock: three strategies, and why you'd pick each


```
Transaction A: holds(users), wants(orders)
Transaction B: holds(orders), wants(users)
```


Both wait forever. Nothing is corrupt; nothing progresses. There are exactly three families of response.


### Prevention — make the cycle structurally impossible


Impose a **global lock order** (always lock tables alphabetically), or **acquire everything up front**
(conservative 2PL), or **never wait** (if a lock is held, abort immediately).


**Cheapest and most restrictive.** A global order is free at runtime and needs zero bookkeeping — and it's
what disciplined application code does. Its weakness: it requires knowing your lock set in advance, which
a query planner deciding access paths at runtime often doesn't.


### Avoidance — refuse requests that *could* lead to a cycle


Use timestamps: under **wait-die**, an older transaction waits for a younger one and a younger one dies
rather than wait for an older. Under **wound-wait**, the older one preempts the younger. Either way, the
wait relation only ever points one way along the timestamp order, so a cycle can't form.


**Cheap, no graph needed, and it aborts transactions that were never actually deadlocked.** That's the
tradeoff: false positives in exchange for no detection machinery.


### Detection — let them happen, then break them


Maintain a **wait-for graph**: an edge A → B means A is waiting for a lock B holds. A deadlock is exactly
a cycle. Run a cycle check when a transaction starts waiting (or periodically), pick a victim, abort it,
release its locks.


```
     A ──waits for──> B
     ↑                │
     └───waits for────┘        cycle => deadlock
```


**Most permissive — nothing aborts unless it genuinely must — and the most machinery.** You need the graph,
you need to maintain it under concurrent mutation, and you need a victim policy.


**Victim selection matters more than it looks.** Abort the *youngest* (least work lost, and it guarantees
progress because the oldest transaction can never be the perpetual victim), or the one holding fewest
locks (releases least), or the one with least work done. Choosing the oldest is the trap: under repeated
contention the same long transaction is aborted forever and never completes — **starvation dressed up as
deadlock handling.**


### And the fourth thing people actually do: timeouts


Wait 5 seconds; if you don't get the lock, give up. **Why it's tempting:** two lines, no graph.
**Why it's genuinely worse:** a timeout cannot distinguish "deadlocked" from "slow." Set it low and you
abort healthy transactions under load; set it high and real deadlocks hang for that long. And you learn
nothing about *why* — no cycle to report, no victim policy, no diagnostics.


**Build detection, keep a timeout as a backstop.** The timeout catches waits your graph doesn't model
(latch bugs, I/O stalls); the graph handles the case it's designed for and gives you a real error message.


> **The interview framing:** "SQLite does prevention through a fixed lock order and fails fast with
> `SQLITE_BUSY`. I built detection with a wait-for graph and abort the youngest victim, because detection
> allows more concurrency — nothing aborts unless there's a genuine cycle — at the cost of maintaining
> the graph and being prepared to abort. Detection also gives me a real `DeadlockError` naming the cycle,
> which is much better to debug than a timeout."


---


## 16.5 SQLite's five lock states, and why one of them exists purely for fairness


SQLite locks the **whole database file** in rollback-journal mode, through five states:


| State | Permits | Blocks | Entered when |
|---|---|---|---|
| **UNLOCKED** | nothing; the cache is *"suspect and subject to verification against the database file"* | nothing | default; after commit releases |
| **SHARED** | reading. *"Any number of processes can hold SHARED locks at the same time, hence there can be many simultaneous readers."* | all writing | **first SELECT** — not `BEGIN` |
| **RESERVED** | intent to write while still only reading — *"planning on writing… at some point in the future,"* *"but that it is currently just reading."* Coexists with readers | a **second writer**: *"Only a single RESERVED lock may be active at one time."* Not new readers | **first INSERT/UPDATE/DELETE** |
| **PENDING** | existing readers to drain — *"existing SHARED locks are allowed to continue"* | **new** readers: *"No new SHARED locks are permitted against the database if a PENDING lock is active."* | on the way to EXCLUSIVE |
| **EXCLUSIVE** | writing the database file | everything | at COMMIT — **or earlier, on a cache spill** |


The write path is `SHARED → RESERVED → [journal] → PENDING → EXCLUSIVE → [write] → commit → UNLOCKED`.


Two details worth noticing before the interesting one. `BEGIN` **takes no locks at all** — locks are
acquired lazily by the first statement that needs them, which is why `BEGIN IMMEDIATE` exists for when you
want the write intent declared up front. And the pager *"only tracks four of the five locking states,"*
because PENDING is *"always just a temporary stepping stone on the path to an EXCLUSIVE lock"* — it never
persists long enough to be a state anyone rests in.


### PENDING exists to solve writer starvation, and the doc explains it beautifully


Why have a state between RESERVED and EXCLUSIVE at all? Because of this:


> "In SQLite version 2, if many processes are reading from the database, it might be the case that there
> is never a time when there are no active readers. And if there is always at least one read lock on the
> database, no process would ever be able to make changes to the database."


Readers are compatible with each other, so under sustained read load the count of readers may never reach
zero — and the writer, needing exclusivity, **waits forever while every individual reader makes progress.**
Nothing is deadlocked. Nothing is buggy. The writer simply never runs.


> "This situation is called **writer starvation**. SQLite version 3 seeks to avoid writer starvation
> through the use of the PENDING lock. The PENDING lock allows existing readers to continue but prevents
> new readers from connecting to the database," so *"all SHARED locks will eventually clear and the writer
> will be given a chance to make its changes."*


**PENDING is a one-way turnstile.** Existing readers finish; new readers queue. The set of readers can now
only shrink, so it reaches zero in bounded time.


**This is the generalizable idea, and it's worth more than the SQLite fact:** a compatible-with-itself
lock mode can starve an incompatible one indefinitely, and the fix is a *draining* state that blocks new
arrivals without evicting current holders. The identical pattern appears in every fair read-write lock
implementation, in connection-pool draining, and in rolling deploys. **Starvation is a liveness bug that no
correctness test will ever catch** — your serializability tests all pass while the writer never runs — and
recognizing that class of bug is a genuinely senior skill.


### `sqlite3_busy_timeout` is a retry budget, not a queue


SQLite doesn't queue waiters. It fails fast: *"If the SHARED lock cannot be obtained, fail immediately and
return `SQLITE_BUSY`."* A busy handler makes the *client* retry. Measured against a holder that releases
after ~2.0 seconds:


```
busy_timeout =    0 ms  ->  FAILED after 0.000 s   (SQLITE_BUSY)
busy_timeout =  200 ms  ->  FAILED after 0.201 s   (SQLITE_BUSY)
busy_timeout = 5000 ms  ->  SUCCEEDED after blocking 1.83 s
```


The `0.201` and `1.83` are the proof: the timeout is *"keep retrying for up to N milliseconds,"* and
success depends entirely on the lock clearing inside that budget. It does not make waiting fair, it does
not guarantee FIFO, and it still returns `SQLITE_BUSY` when the budget runs out. **An application using
SQLite concurrently must handle `SQLITE_BUSY` regardless of the timeout** — which is the practical fact
most people learn the hard way.


> ⚠️ **Citation warning, and this one is easy to get wrong in an interview.** It is accurate to say
> SQLite achieves deadlock freedom by strict lock ordering and fail-fast rather than by detection — the
> fixed `PENDING`-then-`EXCLUSIVE` escalation and the immediate `SQLITE_BUSY` are both documented. But
> **`lockingv3.html` never uses the word "deadlock."** There is no passage to quote. Present that framing
> as your own analysis of the mechanism, not as something SQLite says.


---


## 16.6 The escalation is observable, and it's the clearest way to see why WAL exists


Chapter 15 quoted the rollback-journal reader/writer relationship: *"all readers must be (temporarily)
expelled."* You can watch it happen. Two connections, and the only difference between the tests is
whether the writer's dirty pages fit in its cache:


```
### TEST 1 — writer holds RESERVED (small write, no cache spill)
  B: BEGIN IMMEDIATE  ->  SQLITE_BUSY: database is locked      ← second writer refused
  B: UPDATE           ->  SQLITE_BUSY: database is locked
  B: SELECT           ->  ('v1',)                              ← READS STILL WORK (pre-image)
  A: SELECT           ->  ('A',)                               ← A sees its own uncommitted write


### TEST 2 — same writer, but PRAGMA cache_size=10 forces a spill
  journal size mid-transaction: 50,728 bytes
  B: SELECT           ->  SQLITE_BUSY: database is locked      ← THE WRITER NOW BLOCKS READERS
```


**Nothing changed about the SQL.** In test 1 the writer holds RESERVED, which by design coexists with
readers, so B reads happily. In test 2 the writer's dirty pages overflowed its cache, so it had to write
them into the database file mid-transaction — which requires EXCLUSIVE — which expels readers. The state
machine in §16.5 is not an abstraction; it's directly visible from another connection.


**And it explains a class of production complaint**: "my reads intermittently get `SQLITE_BUSY` even though
the writer hadn't committed yet." The threshold is the cache size, so the behaviour depends on transaction
size, which is why it feels random.


For contrast, the same experiment in WAL mode:


```
journal_mode = wal;  WAL file 4,120,032 bytes mid-transaction
  B: SELECT during the active write txn  ->  ('v1',)  SUCCEEDED   ← never expelled
  B: BEGIN IMMEDIATE                      ->  SQLITE_BUSY         ← still only ONE writer
```


A reader sails through a 4 MB in-flight write that would have hard-blocked it in rollback mode, because the
writer is appending to the WAL rather than modifying the file the reader is reading. **That single result
is the entire argument for WAL**, and the second line is the limit of it: WAL removes the reader/writer
conflict, not the writer/writer one.


---


## 16.7 What quilldb builds


```python
class LockMode(Enum):
    SHARED = 1
    EXCLUSIVE = 2




class LockManager:
    """Table-level shared/exclusive locks, strict 2PL, deadlock detection."""


    def acquire(self, txn_id: int, resource: str, mode: LockMode,
                timeout: float | None = None) -> None:
        """Block until granted.


        Raises:
            DeadlockError: this transaction was chosen as the victim.
            LockTimeoutError: the backstop timeout expired.
        """


    def release_all(self, txn_id: int) -> None:
        """Called at commit/rollback. NEVER mid-transaction — that's what makes
        it strict 2PL (§16.2). If you find yourself wanting a release_one(),
        stop and re-read that section."""


    def _detect_deadlock(self, waiting_txn: int) -> int | None:
        """Cycle-check the wait-for graph from `waiting_txn`. Returns the
        victim's id, or None if there's no cycle. Called when a transaction is
        about to block — that's the only moment a new edge appears, so it's the
        only moment a new cycle can form."""
```


**Four implementation notes that will save you time:**


1. **Check for deadlock at the moment of blocking, not on a timer.** A cycle can only appear when a new
   wait-for edge is added, and edges are only added when someone blocks. A background scanner is strictly
   worse: same work, later detection.
2. **Use one `threading.Condition` per resource, not one global.** With a global condition, every release
   wakes every waiter — the thundering herd — and they all re-check and go back to sleep. Per-resource
   conditions wake only the threads that might now proceed.
3. **Guard against spurious wakeups.** `Condition.wait()` can return without the predicate being true.
   Always re-check in a `while` loop, never an `if`. This is the single most common threading bug in code
   like this and it manifests as a rare, unreproducible corruption.
4. **The lock manager's own state needs a latch** (§16.3) — and it must not be held while a thread waits.
   `Condition` handles this for you by releasing the underlying lock during `wait()`, which is precisely
   why you use a `Condition` rather than a `Lock` plus an `Event`.


### `Database` vs `Connection`, and why the split is the design


```python
class Database:      # ONE per file, shared by all threads
    pager, buffer_pool, lock_manager, catalog


class Connection:    # ONE per thread, never shared
    current_transaction, open_cursors, autocommit_state
```


**Getting this boundary right is most of week 6.** Anything shared needs a latch; anything per-thread
doesn't. The bugs come from state that looks per-connection but is actually shared — the buffer pool being
the obvious one, the catalog cache being the one people miss (two threads, one `CREATE INDEX`, and a
stale schema in the other thread's cache).


Document the threading contract explicitly: *a `Connection` is not thread-safe and must not be shared; a
`Database` is thread-safe and is shared.* That's the same contract as most database drivers, and stating
it is what makes the library usable.


### The GIL, and being honest about it


Python's global interpreter lock means your threads don't execute bytecode in parallel. So:


- **Write throughput will not scale with thread count.** Say so, with the measurement.
- **What the benchmark actually demonstrates** is correctness under contention, plus concurrent-reader
  behaviour — and the GIL is released during I/O, so real overlap does happen on disk-bound work.
- **The lock manager is not pointless under a GIL.** The GIL protects the *interpreter*, guaranteeing
  nothing about your logical invariants: a thread can be suspended between reading a balance and writing
  it, which is exactly chapter 15 §15.0. **The GIL prevents data races, not race conditions.** That
  sentence is worth having ready, because the "doesn't the GIL make this unnecessary?" question is
  extremely likely and the answer distinguishes people who understand concurrency from people who
  understand threads.


Presenting the GIL ceiling honestly makes an interviewer trust everything else you say. Hiding it and
being caught does the opposite.


---


## 16.8 Roads not taken


### Multi-process locking with `fcntl`


What SQLite actually does: advisory byte-range locks on the database file, so *separate processes*
coordinate. **Why SQLite must:** it's a library, there is no server, and two unrelated programs may open
the same file.


**Why not here, and it's a scope decision rather than a difficulty one:** threads were the stated goal, and
`fcntl` brings problems that are genuinely nasty rather than merely laborious — locks are per-process not
per-thread, they're silently broken over NFS, and a crashed process releases its locks with no chance to
clean up the state they were protecting. **Notice that multi-process is *harder* than multi-thread, not
more advanced**, which is the opposite of how it's usually assumed to rank.


### Hierarchical locking with intention modes


The natural next step from table locks: lock a table in **intention-shared** (IS) or **intention-exclusive**
(IX) mode to signal "I will lock rows beneath this," then lock individual rows. A transaction wanting the
whole table exclusively checks the table-level mode alone rather than enumerating every row lock.


**Used by:** InnoDB, SQL Server, DB2 — this is how real row-level locking is made practical.
**Why it's the right thing to name:** it's the specific mechanism that makes fine-grained locking scale,
and "I'd add intention locks so a table-level request doesn't have to inspect every row lock" is a much
more concrete answer than "I'd add row-level locking."


### Lock-free / optimistic structures


Compare-and-swap on page headers, RCU-style readers, or chapter 15 §15.7's optimistic concurrency control.
**Why not:** in Python, atomic primitives are not exposed usefully and the GIL makes the wins illusory —
plus lock-free code is where correctness arguments become genuinely hard to check. **Right answer for the
wrong language.**


### Partitioning instead of locking


Shard the data so each thread owns a disjoint subset and no lock is needed at all. **Used by:** Redis
Cluster, VoltDB, Kafka partitions, and every "one writer thread per shard" design. **Why it's worth
naming:** it's the answer that scales furthest, and it works by *removing the contention* rather than
managing it — the same move SQLite makes with its single writer, generalized. **Its limit:** a transaction
spanning two partitions needs distributed commit, and you've traded a lock manager for two-phase commit.


---


## 16.9 What you're building


Roadmap week 6, 14 hours. The deliverable that proves it:


```python
def test_no_lost_updates_under_contention(db):
    """8 threads, 10k transfers, random accounts.
    The invariant is the SUM, not any individual transfer (chapter 15 §15.8)."""
    before = sum_balances(db)
    run_transfers(db, threads=8, count=10_000, seed=42)
    assert sum_balances(db) == before
    assert validate_all_btrees(db).is_valid




def test_a_deliberate_deadlock_is_detected_and_broken(db):
    """A locks users then wants orders; B locks orders then wants users.
    Exactly one must raise DeadlockError; the other MUST commit.
    The process must never hang — wrap it in a timeout so a failure fails
    rather than blocking CI forever."""
    results = run_crossed_transactions(db, timeout=10)
    assert sum(isinstance(r, DeadlockError) for r in results) == 1
    assert sum(r is None for r in results) == 1




def test_a_pinned_page_is_never_evicted_under_contention(db):
    """A cursor holds a pinned page while other threads hammer the pool."""
```


The deadlock test is the one to write carefully: it must assert both that a victim was chosen **and that
the other transaction succeeded**. A "deadlock handler" that aborts both transactions passes a weaker test
and is useless.


---


## 16.10 Check yourself


1. One global mutex gives serializability in ten lines. What's wrong with it, and what does that make
   this chapter's real question?
2. Fill in the shared/exclusive compatibility matrix. Which cell recovers most of the concurrency?
3. Why does row-level locking bring back a problem that table-level locking doesn't have?
4. State 2PL. Which half is load-bearing, and why? Construct the counterexample.
5. What does *strict* 2PL add beyond serializability?
6. Why does holding write locks to commit mean no abort ever cascades?
7. Give four differences between a lock and a latch. Which can deadlock, and why not the other?
8. Why must a latch never be held during I/O?
9. Name the three deadlock strategies with one advantage and one cost each.
10. Why is aborting the *oldest* transaction a bad victim policy?
11. Why is a timeout a poor substitute for detection — and why keep one anyway?
12. What is writer starvation? Why is it invisible to every correctness test?
13. Exactly what does PENDING do, and why does that bound the wait?
14. Where else does the "block new arrivals, let existing ones drain" pattern appear?
15. Does `busy_timeout=5000` mean your query waits 5 seconds and then succeeds? What do the numbers show?
16. Is it accurate to say SQLite avoids deadlock rather than detecting it? Is it quotable?
17. Two connections, a small write and a big write. Why does a reader succeed in one case and get
    `SQLITE_BUSY` in the other, with identical SQL?
18. What single measured result is the whole argument for WAL, and what does WAL *not* fix?
19. Why check for deadlock when a transaction blocks rather than on a timer?
20. Why one condition variable per resource rather than one global?
21. "Doesn't the GIL make the lock manager unnecessary?" Answer it.
22. Why is multi-process `fcntl` locking harder than multi-threaded locking, rather than more advanced?
23. What do intention locks buy, and for whom?


---


## 16.11 Sources


- [File Locking And Concurrency In SQLite Version 3](https://www.sqlite.org/lockingv3.html) — the five
  lock states and their compatibility; *"Any number of processes can hold SHARED locks at the same
  time"*; RESERVED as *"planning on writing… at some point in the future"* and *"Only a single RESERVED
  lock may be active at one time"*; *"No new SHARED locks are permitted against the database if a PENDING
  lock is active"*; PENDING as *"always just a temporary stepping stone"*; the pager tracking only four of
  five states; *"If the SHARED lock cannot be obtained, fail immediately and return SQLITE_BUSY"*; and the
  writer-starvation passage quoted in §16.5. **This page never uses the word "deadlock."**
- [Isolation In SQLite](https://www.sqlite.org/isolation.html) — *"all readers must be (temporarily)
  expelled."*
- [Write-Ahead Logging](https://www.sqlite.org/wal.html) — readers not blocking writers, and §9's *"This
  is mostly true."*
- Gray & Reuter, **Transaction Processing: Concepts and Techniques** — 2PL, strict 2PL, hierarchical
  locking with intention modes, wait-die and wound-wait.
- **The measurements in §16.5 and §16.6** were taken with two live connections against `sqlite3` 3.37.2 /
  Python 3.50.4: the `busy_timeout` timings, the RESERVED-versus-EXCLUSIVE reader behaviour (with
  `PRAGMA cache_size=10` to force the spill), and the WAL comparison. One thing **not** verified: the
  exact backoff schedule inside `sqlite3_busy_timeout` — the behaviour is measured, the implementation
  unread.


---


**Next:** [17 — Joins](../exec/17-joins.md) — why a join is a nested loop before it's anything else, and
why SQLite ships exactly one join algorithm on purpose.