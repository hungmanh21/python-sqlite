# NOTES.md — bug journal

Every bug that cost more than ~20 minutes, in the format `docs/implementation/README.md` asks
for: **symptom → what I assumed → what it actually was → fix**. Newest week last.

The entries worth re-reading before an interview are marked ★ — not the hardest ones, the ones
where the *assumption* was the interesting part.

---

## Week 2 — the table B+tree

### B2-1 ★ A "balanced" split that wasn't
- **Symptom:** a leaf split completed, then the very next insert into the same half split again
  immediately. Occupancy was nowhere near the ~50% a halving split should give.
- **Assumed:** cells are roughly uniform, so splitting at `len(cells) // 2` splits the *bytes*
  in half too.
- **Actually:** cell size legally varies by two orders of magnitude, up to `max_local_payload`.
  A count-balanced split can hand one half almost every byte on the page. Worse, `_split_leaf`
  was splitting the *existing* cells and then inserting the pending row into whichever half it
  landed in — so the split decision was blind to the one cell size that most determines whether
  the result actually fits.
- **Fix:** choose the split point closest to half by *serialized footprint* (`len(cell) + 2`,
  matching `PageBody.fits()`), and combine the pending row into the sorted list **before**
  splitting. Also separated two questions that had been sharing one flag: "is this leaf
  positionally its parent's rightmost child" (governs how the parent gets patched) vs. "is the
  pending row this leaf's new maximum" (the narrower condition the peel-one-cell fast path
  needs).

---

## Week 3 — SQL, the executor, the public API

### B3-1 ★ A failed query that reported success
- **Symptom:** a row raising `TypeMismatchError` mid-scan, caught by the caller, then further
  `fetchone()` calls returned `None` — indistinguishable from a query that had simply run out.
- **Assumed:** the operator chain self-closing on error was enough cleanup; the Cursor above it
  didn't need to know.
- **Actually:** `fetchone()` had no try/except, so `Cursor._operator` still pointed at an
  already-self-closed chain. Any caller with retry logic or a loop written before the failing
  row would believe the query succeeded and silently process a truncated result set.
- **Fix:** a failed fetch marks the cursor closed, matching what already happened to the
  resources underneath it, so the next call raises "cursor is closed" instead of lying.
  *Lesson: `None` is a terrible way to signal two different things.*

### B3-2 An orphaned page that only sqlite3 could see
- **Symptom:** hitting the documented 53-table ceiling raised `PageFullError` as designed, and
  every surviving table stayed readable — but `sqlite3 integrity_check` reported
  `Page 55 is never used`.
- **Assumed:** a leaked page is "wasteful, not corrupt" — an old code comment said exactly that.
- **Actually:** wrong by this project's own acceptance bar. `create_table()` allocated the new
  root page *before* appending the `sqlite_schema` row, so the failure path left it allocated
  and unreferenced.
- **Fix:** check page 1's free space *between* `allocate_page()` and the first write through the
  BufferPool, and `free_page()` on the failure path. The ordering is load-bearing:
  `allocate_page()` writes via the pager directly, so at the check the pool holds no cached copy
  and the reclaim survives. Initializing the root first would let the pool flush a dirty
  `LEAF_TABLE` copy over the freelist trunk — turning a leaked page into a *corrupt freelist*
  whose type byte reads as a next-trunk pointer.

---

## Week 4 — mutation, indexes, the planner

### B4-1 A test that was testing the wrong thing
- **Symptom:** `test_overflow_spill_always_fills_overflow_pages_exactly` failed. Looked like a
  real overflow-encoding bug in the engine.
- **Assumed:** `local == MIN_LOCAL_PAYLOAD` proves the M fallback branch fired.
- **Actually:** it doesn't. `K == M` whenever `(P - M) % (U - 4) == 0` — e.g. `P = 8673` gives
  `K = 489 = M`, and the spill is a clean `2 × 4092`. The engine was right; the test's branch
  predicate was wrong.
- **Fix:** discriminate on `k > x`, which is what actually distinguishes the two branches.
  *Lesson: a failing test is a hypothesis, not a verdict.*

### B4-2 ★ `wrong # of entries in index` — the one that started everything
- **Symptom:** `integrity_check` reported `wrong # of entries in index` after index inserts that
  caused a leaf split. Structure looked perfect; every key was findable.
- **Assumed:** a split is a split. `_split_leaf` promoted a **copy** of the separator into the
  parent and kept the original in the left half — the correct B+tree behaviour, ported from the
  table b-tree.
- **Actually:** an index b-tree is a **true b-tree**, not a B+tree (§11.6): an interior cell
  carries a full record and **is a live entry**. Copying the separator therefore creates a second
  live copy of one row, so the index ends up with N+1 entries for N rows. The split had stopped
  conserving entry count.
- **Fix:** `_split_leaf` now *consumes* its separator (`promoted_cell = left_cells.pop()`).
  The universal rule underneath: **a split consumes its separator whenever the separator is
  itself an entry.** Overflow chains transfer verbatim — verified `local_payload_size` is
  identical for `LEAF_INDEX` and `INTERIOR_INDEX` at every size 0..300k.

### B4-3 The index ceiling at 12,128 rows
- **Symptom:** inserting into an index past ~12,100 rows raised `PageFullError`. quilldb could
  *store* 100k rows but not *index* them.
- **Assumed:** the index b-tree grew like the table b-tree, which had `_promote_separator`.
- **Actually:** it never had a cascade. The only code path that added a level handled a **leaf**
  root, so the tree could go one level deep and then never again. Capacity was exactly
  `children_per_interior × entries_per_leaf`, regardless of table size.
- **Fix:** ported `_promote_separator` to index pages. The separator travels as its raw
  `(total_len, local, overflow)` rather than a decoded key, because an index divider *is* an
  entry and has to be re-encoded byte-for-byte with its overflow chain following it. Ceiling
  after: 200,000+ rows, depth 3, `integrity_check: ok`.

### B4-4 ★ `database disk image is malformed` — a zero-cell page, built on purpose
- **Symptom:** once the cascade let index trees exceed two levels, sqlite3 refused the file
  outright: `database disk image is malformed (11)`. Note this is a **hard parse error**, not an
  `integrity_check` finding — the usual acceptance test couldn't even run to say *where*. Found
  it by walking the tree by hand: page 68, `INTERIOR_INDEX`, zero cells.
- **Assumed:** `split_interior_cells`' rightmost peel was correct because it mirrored the leaf
  peel, and because the table b-tree had shipped on it for two weeks.
- **Actually:** an interior split **consumes** its separator. With `m = len(cells) - 1`, the
  separator is the last cell, so `cells[m+1:]` is empty *every time* — not an edge case, an
  arithmetic guarantee. The old docstring even stated it as intended behaviour. The table tree
  hid it because append-heavy inserts refill that page on the very next insert, so the illegal
  state never survives to a flush: `table/ascending → EMPTY_RIGHT=2` (both refilled) vs.
  `index/ascending → EMPTY_RIGHT=10` (left on disk).
- **Fix:** stop one cell short (`m = len(cells) - 2`) and clamp the balanced path to
  `[1, len(cells) - 2]`; minimum splittable size rises from 2 cells to 3. Occupancy is
  unaffected. *Lesson: the bug was in the docstring too, so nothing that compares code against
  its stated contract could ever have caught it — only comparing the contract against the file
  format did.*

### B4-5 An empty non-root leaf is equally illegal
- **Symptom:** follow-up to B4-4 — needed to know whether the rule was about interior pages
  specifically or about all pages.
- **Assumed:** week 4's `_collapse_empty_leaf` scope cut was fine. Its comment read, roughly,
  "leave the page linked and empty — wasteful but harmless."
- **Actually:** took a known-good file, hand-emptied one leaf, fed it to sqlite3: same
  `malformed`. **No page may ever hold zero cells**, leaf or interior. The only exemption is an
  empty index's root. And the table b-tree's escape hatch — drop the parent cell that pointed at
  the emptied child — is unavailable here, because that cell *is a row*.
- **Fix:** `_collapse_empty_leaf` became `_rebalance(path, level)`, restoring a zero-cell page at
  any level by **MERGE** (divider descends into the sibling, page freed, costs the parent a cell)
  or **ROTATE** (divider descends into the page itself, parent's key refilled by borrowing the
  sibling's edge entry, costs the parent nothing). Merge while the parent can afford it; rotate
  when it's down to its last cell. A root left without cells is a level the tree outgrew — pull
  its only child up *into the root's own page*, since `sqlite_schema` records that number.

### B4-6 ★ Freeing a page that something still pointed at
- **Symptom:** `InvalidPageTypeError` during delete rebalancing — something read a page that had
  already been freed and reallocated.
- **Assumed:** to recurse into the sibling, build the path as `(sibling_id, sibling_slot)`. The
  slot describes the sibling, so it goes in the sibling's tuple. Obvious.
- **Actually:** backwards. This codebase's convention is `path[i] = (page_id, slot_chosen_within
  that page)` — a child's index lives in its **parent's** tuple, `path[i][1]`. So the parent
  entry still described the *original* child, and the rebalance unlinked the wrong page while a
  live parent cell still pointed at the one it freed.
- **Fix:** `[*path[:level - 1], (parent_page_id, sibling_slot), (sibling_id, 0)]`.
  *Lesson: an implicit convention costs exactly this much the first time you write new code
  against it. This one is now stated in `_rebalance`'s docstring.*

### B4-7 Rotation ping-pong
- **Symptom:** `RecursionError` during delete rebalancing.
- **Assumed:** a rotation always makes progress — the empty page gains a cell, so it's fixed.
- **Actually:** if the sibling held exactly one cell, borrowing it empties the *sibling*. The
  sibling then rebalances, finds its nearest non-empty neighbour is the page that just took its
  cell, and borrows it back. Forever.
- **Fix:** `can_rotate = len(sibling.cells) >= 2`. A sibling with one cell can't spare it, so
  that case falls through to merge — which always fits, precisely *because* that sibling is
  nearly empty. *Lesson: the correct version was **smaller** than the buggy one, one comparison
  replacing a whole recursion branch. When a termination condition makes code shrink, it's
  usually the real constraint rather than a patch.*

### B4-8 Two test failures that were fixture bugs
- **Symptom:** `test_insert_splits_a_leaf_and_promotes_into_an_existing_parent` and
  `test_delete_empty_non_root_leaf_frees_the_page` both failed after B4-2.
- **Assumed:** the new consume-the-separator logic had broken them.
- **Actually:** both fixtures hand-built the key `["a"], 1` into *both* the leaf and the parent's
  interior cell — the old copy-up convention, i.e. a malformed tree under the corrected rule.
  They were asserting the bug.
- **Fix:** gave each leaf a strictly smaller key (`["A"], -2` and `["A"], 0`). *Lesson: when a
  behaviour change breaks tests, check whether the tests encoded the old behaviour as a
  fixture — those don't show up as assertion edits.*

### B4-9 ★ A planner crash on `WHERE age >= -1`
- **Symptom:** `AssertionError: seek_term 'age' has a non-literal value: BoundUnaryOp`. Only
  fires when the column is indexed *and* the term is sargable — so it survived the whole test
  suite. Broader than first reported: `+1` and `2 + 0` crashed too.
- **Assumed:** `Predicate.value` is always a `BoundLiteral` by the time it reaches `IndexScan` —
  the assert in `operators.py` said so in its docstring.
- **Actually:** a straight contract mismatch between two modules. `plan/predicates.py`'s
  sargability test is `_is_column_free`, which *deliberately* admits any expression computable
  without reading a row — unary and binary ops included. SQL has no negative literal; `-1` is an
  operator applied to `1`. Real sqlite3 plans `age >= -1` as `SEARCH t USING INDEX ix_age
  (age>?)`, so refusing the index would have been the wrong repair.
- **Fix:** `_literal_value` → `_seek_value`, which *evaluates* the column-free expression once at
  open time. Bonus: `evaluate()` already raises a typed `ColumnNotFoundError` if a column ever
  leaks through, so deleting the bare `assert` also closed an `errors.py` classification gap
  rather than widening one.

### B4-10 ★ The index changed the answer
- **Symptom:** none. Nothing crashed, nothing failed, `integrity_check` said `ok`. Found only by
  running the same query against an indexed and an unindexed copy of identical data:
  `WHERE age > NULL` returned **every row** through the index and **no rows** through a scan.
  Real sqlite3 returns none.
- **Assumed:** a seek bound is a seek bound — once `_seek_value` hands back a value, the b-tree
  can order it like any other.
- **Actually:** `compare_keys` orders NULL below every other value, which is correct for
  *storing* NULLs in an index and exactly wrong for a comparison *bound*. `age > NULL` therefore
  seeks "everything above the lowest possible key" and walks the whole index. In SQL, `=`, `<`,
  `>` against NULL evaluate to NULL — never true — so the conjunct is unsatisfiable and the
  answer is always the empty set. Same root cause as B4-9, the `operators.py`/`predicates.py`
  contract mismatch; B4-9 was the half that failed loudly.
- **Fix:** an unsatisfiability guard in `IndexScan.open()` — a folded bound of NULL on any
  operator except `IS` returns an exhausted iterator. `IS` is excluded because it is the one
  comparison that *does* match NULLs; including it would have broken `WHERE age IS NULL`, which
  is a legitimate seek. *Lesson: an index may only change a query's speed, never its results —
  and the test that enforces that is "run it with and without the index and diff", which is the
  only reason this was ever found.*

---

## Week 5 — atomic commit and crash recovery

### B5-1 ★ `Transaction.commit()` leaked a pin on the header page, once per commit
- **Symptom:** nothing failed — found by writing a throwaway script that committed the same
  transaction object's descendants three times in a row and watched
  `pool._cache[SCHEMA_ROOT_PAGE].pin_count` climb 1 → 2 → 3 instead of returning to 0.
- **Assumed:** `BufferPool.get_page_for_write()` was interchangeable with the
  `pinned_for_write()` context manager it backs — both "give me a mutable page," so either call
  site should be fine.
- **Actually:** they're not symmetric. `pinned_for_write()` unpins on `__exit__`;
  `get_page_for_write()` does not, because its normal callers are already mid-mutation and pin
  again themselves for the write proper. `commit()`'s header-stamp line called
  `get_page_for_write()` directly, stamped the header, and returned — no second pin was coming,
  so the first one was never released. Harmless-looking on one commit (page 1 stays cached
  anyway); fatal under the no-steal invariant this same session added, since a pin count that
  only ever grows eventually makes the page permanently ineligible for eviction.
- **Fix:** `with self._pool.pinned_for_write(SCHEMA_ROOT_PAGE) as page: page[:FILE_HEADER_SIZE] =
  self._pager.header_bytes()`. *Lesson: two APIs that produce the same mutable view are not the
  same API if only one of them promises to clean up after itself — the leak was invisible
  precisely because both looked correct in isolation.*

### B5-2 `Connection.close()` had no plan for an open transaction
- **Symptom:** two different failures depending on what the abandoned transaction had done: an
  `AssertionError` crash inside `close()` if it had written anything, or — if it had written
  nothing — a silent stray `t.db-journal` file that then blocked the *next* `BEGIN` with
  `FileExistsError`, on a connection that otherwise looked perfectly healthy.
- **Assumed:** `close()` only ever runs after a matched `COMMIT`/`ROLLBACK` pair, so an in-flight
  explicit transaction wasn't a state it needed to reason about.
- **Actually:** nothing stopped a caller from opening `BEGIN` and just calling `close()` (or
  letting the object go out of scope) without either — an ordinary client mistake, not a crash.
  `close()`'s `pool.flush_all()` then tried to write pages that were dirtied but never
  journalled or barriered, tripping the very assertion this week added to forbid unbarriered
  writes. A transaction that never wrote anything skipped that crash but still left its journal
  file on disk, since nothing had called `journal.delete()`.
- **Fix:** `close()` now checks for an open transaction first and rolls it back before touching
  anything else — an uncommitted transaction never happened, so `close()` undoes it rather than
  flushing it. *Lesson: "the caller will always clean up first" is a claim about callers, not
  about the type system — it needs an explicit check exactly where it's cheapest to add one.*

---

## Week 6 — locks, threads, and deadlock detection

### B6-1 ★ Two bugs wearing one flaky test — a clock quirk hiding a real missed-wakeup
- **Symptom:** `test_waiters_are_fifo_so_nobody_starves` failed intermittently (roughly half the
  runs), with a queued reader apparently granted while a writer was still ahead of it in line —
  but the four other session-1 tests (including `test_exclusive_excludes`, which blocks the exact
  same way) never once failed across hundreds of runs.
- **Assumed (round 1):** a blocked `LockManager.acquire()` reader "unexpectedly granted" meant the
  compatibility/FIFO check in `acquire()` had a real ordering bug — the natural read, since that's
  the only code that decides who gets granted.
- **Actually (round 1):** it wasn't the FIFO check. Isolated with a bare `threading.Condition` and
  zero `LockManager` code: one thread blocked in `cond.wait(timeout=5.0)`, a second thread only
  doing `with cond: pass` in a tight loop (no `notify()` anywhere) — and the waiter woke up
  **early**, reporting a timeout after ~3.4 real seconds. Heavy contention on a `Condition`'s
  underlying lock from other threads can make `wait(timeout=...)` return before the requested time
  elapses on this environment (WSL2's virtualized clock under load is the leading suspect, not
  proven). The FIFO test was the only one of the five hammering the *same* condition from six
  threads at once — enough concurrent lock traffic to trigger it; the two-thread
  `test_exclusive_excludes` never generated enough contention to.
- **Fix (round 1):** hardened `acquire()`'s wait loop to never trust `wait()`'s return value as the
  timeout signal by itself — it now always re-derives "did I actually time out" from
  `deadline - time.monotonic()` at the top of the next loop iteration, so a spurious early wakeup
  just costs one extra loop instead of a false `LockTimeoutError`. Then rewrote the test to stop
  racing a short (0.1s) `acquire()` timeout against wall-clock threading, using `timeout=None` on
  the blocking calls instead (a `None` timeout can't expire early — it's purely notify-driven) and
  proving order by polling `LockManager`'s own internal `waiters`/`holders` state, the way other
  tests in this repo already reach into `pager._header`.
- **Assumed (round 2):** with the clock dependency gone, the test would be clean.
- **Actually (round 2):** a *different*, genuine bug immediately surfaced, because `timeout=None`
  removed the thing that had been silently working around it: readers 3 through 7 queued correctly
  behind the writer, the writer was correctly granted first — and then only reader 3 ever woke up.
  Readers 4-7 hung forever. `acquire()` only called `entry.condition.notify_all()` from
  `release_all()`. When N transactions queue behind each other with no `release_all()` in between,
  the one `notify_all()` that wakes the queue lets only the front waiter (whoever's `grantable()`
  is now `True`) through; nobody notifies the *next* waiter that removing that front entry changed
  what's grantable for them. They go back to sleep waiting for a notification that never comes.
  This was real and present the whole time — the original test's *short timeouts* had been quietly
  masking it, since each reader's own timeout loop re-polled `grantable()` on its own schedule
  regardless of whether anyone notified it.
- **Fix (round 2):** every successful grant inside `acquire()` — not just `release_all()` — now
  calls `entry.condition.notify_all()` before returning, so leaving the front of the queue always
  gives the next waiter a chance to recheck. *Lesson: a flaky concurrency test can be two bugs deep.
  The first fix (stop trusting a wobbly clock) was necessary but made the test's own masking effect
  disappear too, which is what exposed the second, real bug. Don't stop investigating just because
  the first plausible cause checks out — especially not for concurrency code, where a "fix" that
  only changes the odds of triggering a bug (rather than removing the mechanism) can look identical
  to a real fix for a long time.*

### Week 6 review — found after the suite was green

B6-2 through B6-9 all came out of a review pass over the finished week, with every test already
passing. Each has a repro in `src/tests/concurrency/test_week6_regressions.py`, and each of those
tests was confirmed to **fail** against the pre-fix source before being trusted to pass after it.

### B6-2 ★ A rolled-back row that `integrity_check` called `ok`
- **Symptom:** connection `c2` ran `BEGIN` on a 3-page file; `c1` committed 200 inserts, growing it
  to 30 pages; `c2` inserted one row, then `ROLLBACK`. After reopening, `sqlite3` still saw the
  rolled-back row — and `PRAGMA integrity_check` said `ok`, because the file was structurally
  perfect. It just contained a row that was never committed.
- **Assumed:** `Transaction.__init__` is "when the transaction starts", so it's the right moment to
  snapshot `_page_count_before` — true all through week 5, with one connection.
- **Actually:** with concurrency there are two different moments: when a transaction is *created*
  and when it becomes *the writer*. A deferred `BEGIN` (or an autocommit statement queued on
  `"__writer__"`) can sit between them while other writers commit. Every page they commit lands
  above the stale snapshot, so `will_modify()` classified pages 4–30 as "allocated by this
  transaction" — never journalled, because rollback's `truncate()` supposedly erases them for free.
  Rollback then had no original bytes to restore.
- **Fix:** `Transaction._acquire_writer()` re-snapshots `page_count` the first time the transaction
  holds `"__writer__"` — the moment `page_count` stops moving. `journal.begin()` now records that same
  snapshot rather than the live count, so recovery truncates to the same point rollback does.
  *Lesson: `integrity_check` is an acceptance test for the **format**, not for **atomicity**. A
  transaction bug that produces a valid file is invisible to it; only asserting the actual contents
  catches it.*

### B6-3 An application error that locked the whole database
- **Symptom:** inside `with conn.transaction():`, a `DELETE`, a `SELECT` with one `fetchone()`, then
  the app raised `KeyError`. The caller got `ValueError: page 5 is still pinned` instead, and every
  other connection's next write timed out on `"__writer__"` — permanently.
- **Assumed:** session 5's narrowed `pool.clear(self._journalled)` (§37.5) was safe to raise on a
  pinned page, because 2PL guarantees nobody else pins a page this transaction wrote.
- **Actually:** *nobody else*, true — but this transaction's own open cursor can. `execute("ROLLBACK")`
  closes the open cursor first; `transaction()` and `close()` roll back directly and didn't. The
  raise came after `journal.delete()` and before `release_all()`: a half-finished rollback holding
  every lock it had.
- **Fix:** `Connection._end_txn()` (shared by commit and rollback) closes the open cursor first,
  every time. *Lesson: "nothing else can hold X" arguments need "…including me" checked
  separately.*

### B6-4 Closing one connection closed them all
- **Symptom:** `c1.close()` then `c2.execute(...)` → `ValueError: truncate of closed file`. The
  transfer stress test had even documented it and worked around it ("deliberately never closed").
- **Assumed:** `Connection.close()` keeping its week-3 body (`pool.flush_all(); pager.close()`) was
  fine after session 3 moved the pager and pool into `Database`.
- **Actually:** after session 3 those objects belong to the `Database`, shared by every connection.
  One connection closing them closed the file for all of them — and `flush_all()` with another
  connection mid-write would have tripped the write-barrier assertion on its pre-barrier pages.
- **Fix:** `Database` ref-counts open connections; `Connection.close()` rolls back, closes its
  cursor, and reports in. The last one out flushes and closes the file, so every single-connection
  caller behaves exactly as before.

### B6-5 ★ A deadlock resolved by whichever timer fired first
- **Symptom:** txn 2 blocks on `A` (held by 1). Then txn 1 blocks on `B` (held by 2), closing the
  cycle. Across 5 runs: 3 times txn 2 got `DeadlockError` 0.15–1s late; 2 times txn 1 — the one that
  should survive — got `LockTimeoutError` on a genuine cycle, which the week-6 checklist forbids.
- **Assumed:** session 2's "not me, so wait" rule was complete: the real victim's own `acquire()`
  walks the same graph from itself and raises for itself.
- **Actually:** only if it's awake to walk it. The victim ran detection when *it* blocked — before
  the cycle existed — and has been asleep in `wait()` ever since. Nothing in a deadlock ever releases
  a lock the victim waits on (that's what deadlock means), so no `notify_all()` comes. It only
  re-checks at its own timeout, and by then it's a race between two timers. The `test_deadlock.py`
  tests never caught it because every one of them blocks the *youngest* transaction last.
- **Fix:** the detector records the victim in `LockManager._doomed` and wakes the victim's resource
  via `_wake()`, which drops the detector's own condition before taking the victim's (holding two
  entry conditions at once is a lock-order inversion between two concurrent detectors). The victim's
  loop checks `_doomed` first thing. `_detect_deadlock` now runs under `_latch` with a
  `tuple(entry.holders)` snapshot, so the walk sees one consistent graph instead of iterating
  another resource's dict while its owner resizes it.

### B6-6 A timed-out queue head that nobody noticed leaving
- **Symptom:** reader 1 holds `SHARED`; writer 2 queues for `EXCLUSIVE` and times out; reader 3,
  queued behind 2 by FIFO, is now compatible with everything held — and slept until its own timeout.
- **Assumed:** B6-1's fix covered wakeups: every *grant* notifies the queue.
- **Actually:** B6-1 covered one of three ways out of the queue. A waiter leaving on
  `LockTimeoutError` or `DeadlockError` changes the queue head just as much, and notified no one.
- **Fix:** the `notify_all()` moved into `acquire()`'s `finally`, covering every exit. *Lesson: the
  same missed-wakeup twice; B6-1's fix was written at the event ("a grant") instead of the state
  change ("the queue head moved").*

### B6-7 Other connections could see a `CREATE TABLE` before it committed
- **Symptom:** `c1: BEGIN; CREATE TABLE u`, then `c2: EXPLAIN SELECT * FROM u` succeeded, planning a
  table that `c1` was about to roll back. (A plain `SELECT` hid it by blocking on `u`'s table lock.)
- **Assumed:** the schema-cookie check (§37.4) was enough — each connection reloads when the cookie
  moves.
- **Actually:** the cookie moves in memory the moment DDL runs, not when it commits, and
  `create_table()` edits the one shared `Catalog` directly. Worse, the reload itself read page 1
  (the `sqlite_schema` root) with no lock at all, while the DDL writer could be mid-way through
  changing it. Page 1 and the catalog were the one piece of shared state with no resource in the
  lock manager.
- **Fix:** a `"__schema__"` resource, first in the lock order (docs/concurrency.md, "Lock order").
  Every statement takes it before binding — `SHARED`, or `EXCLUSIVE` for DDL. DDL rollback reloads
  the catalog *before* releasing its locks, which meant splitting release out of
  `Transaction.commit()`/`rollback()` (`release=False` + `release_locks()`); as a side effect,
  `_end_txn` now unwires the pager/pool hook while still holding `"__writer__"`, which removes the
  session-5 unwiring race structurally instead of guarding against it.

### B6-8 `ANALYZE` wrote outside every transaction
- **Symptom:** `c1: BEGIN; INSERT ...`, then `c2: ANALYZE` ran straight through instead of waiting
  for the writer. Reading the path it took: its `quill_stat1` writes go through `get_page_for_write`
  while `pool._txn` is **`c1`'s** transaction, so they're journalled into `c1`'s journal and `c1`'s
  `ROLLBACK` would silently erase `c2`'s statistics; with no writer at all, they're written with no
  journal. (The regression test pins the observable half: `ANALYZE` must block.)
- **Assumed:** `ANALYZE` is bookkeeping, not a "real" write — it had never been routed through
  `_run_mutation`.
- **Actually:** any write through the pool is a real write.
- **Fix:** `ANALYZE` runs through `_run_mutation` like every other writer: `EXCLUSIVE` on
  `quill_stat1` (via `StatisticsCatalog.table_name`) plus `SHARED` on each table it measures.

### B6-9 `BEGIN IMMEDIATE` reserved a word SQLite doesn't
- **Symptom:** `CREATE TABLE flags (immediate INTEGER)` stopped parsing once `BEGIN IMMEDIATE`
  added `IMMEDIATE` to the keyword table.
- **Fix:** `_begin()` matches an identifier whose lexeme is `immediate` rather than a dedicated
  token, so `immediate` stays a legal column or table name, as it is in SQLite.

### Hardening from the same review (no observed failure)
- `Pager.reload_header()` and `restore_page()` still used the buffered `seek()`+`read()/write()`
  after session 4 moved everything else to `pread`/`pwrite`. Verified in isolation that a buffered
  read after an `os.pwrite` to the same offset returns the **old** bytes; it didn't reproduce through
  the engine only because the buffered write in `restore_page` happens to invalidate the buffer
  first. Both now follow the same `pread`/`pwrite`-or-`_io_lock` split as `read_page`/`write_page`,
  and `truncate()` takes `_io_lock` (the `:memory:` path shares one `BytesIO` position with readers).
- Stale `TODO(human)` blocks on already-implemented code were rewritten as plain comments
  (`locks.py`, `transaction.py`, `database.py`, `connection.py`, `bufferpool.py`, the transfer stress
  test), and the unused `Database._writer_txn` was removed.

---

## Week 7 — query processing (session 0 retrofit)

### The roadmap's question, answered: `open(outer_row)`
Week 3's iterator abstraction (`Operator.open()`/`next()`/`close()`) held up almost exactly as
written. Adding the one thing a join needs from it — an `IndexScan` re-opened once per outer row,
seeking with that row's join value — required changing `open()`'s signature
(`open(self, outer: Row = ())`) and threading `outer` through `Filter`/`Project` to their child.
`next()` needed no change at all: a `NestedLoopJoin` still just calls `child.next()` in a loop.
So the interface leaked exactly one parameter, not a redesign — `_seek_value` (exec/operators.py)
now evaluates a seek term against `outer` instead of always `()`, which is also what makes an
index-nested-loop join "free": the inner `IndexScan`'s seek bound is an expression over the
*outer* row's columns, bound against the outer row's own layout (never the combined one — that
distinction is week7-query-processing.md §40's `resolve_layout`, session 1's job, not this one's).

### B7-1 ★ An unfiltered index scan would have looked almost free
- **Symptom:** none observed yet — caught while implementing session 0.2 (an index-order access
  path for `ORDER BY indexed_col` with no `WHERE`), before it ever reached a real query.
- **Assumed:** `estimate_row_counts` (`plan/statistics.py`) only ever sees an `index_scan` path
  with at least one seek term, because `_match_index_prefix` returns `None` otherwise — so
  `rows_per_prefix[len(columns) - 1]` was written assuming `columns` is never empty.
- **Actually:** session 0.2 adds exactly the case that assumption excluded: a full index-order
  scan with `seek_terms == ()`. `len(columns) - 1` is then `-1`, and Python's negative-index
  wraparound silently returns `rows_per_prefix[-1]` — the average row count for the FULLY
  specified key, the smallest number in the array. An unfiltered scan of the whole index would
  have been costed as if it touched almost nothing, making it look free next to a seq_scan
  instead of costing the same as one.
- **Fix:** `estimate_row_counts` now falls back to `table_stats.row_count` (same as a seq_scan)
  whenever `path.seek_terms` is empty, index or not. *Lesson: a helper's "N-1" indexing math is
  only as safe as its caller's promise that N is never 0 — and a new caller is exactly how that
  promise gets broken without either side's code changing.*

### B7-2 ★ A LEFT JOIN's own ON condition leaked into the final WHERE filter
- **Symptom:** `SELECT u.name, o.total FROM users u LEFT JOIN orders o ON u.id = o.user_id` over a
  user with no orders returned nothing for that user, instead of `(name, NULL)` — caught by a smoke
  test before any pytest test existed for it.
- **Assumed:** `enumerate_join_plans` (`plan/search.py`) pools every WHERE- and ON-conjunct into one
  list and tracks which ones get consumed by some join step's access path or match expression;
  whatever's left over becomes the final top-level residual `Filter`, applied after the whole join
  — which is correct for a WHERE conjunct, but a LEFT JOIN's own `ON` conjunct was never being
  marked "consumed" by that bookkeeping in the non-reorderable (has-a-LEFT-JOIN) branch, because
  that branch deliberately uses `joins[position-1].on` directly rather than routing ON through the
  same cost-based classification WHERE conjuncts go through.
- **Actually:** an unconsumed conjunct falls straight into the final residual `Filter` — so
  `u.id = o.user_id` got re-evaluated a SECOND time, after `NestedLoopJoin` had already NULL-extended
  the unmatched row. `id = NULL` is `NULL`, not `TRUE`, and `where_passes` rejects `NULL` — exactly
  chapter 17 §17.8's trap, self-inflicted by the planner rather than by a user's WHERE clause.
- **Fix:** every conjunct sourced from a join's own `ON` is pre-marked "consumed" before the
  per-step loop runs, in the has-a-LEFT-JOIN branch only (an all-INNER chain still lets ON conjuncts
  compete for cost-based placement same as WHERE, which is safe with no LEFT edge in the chain).
  *Lesson: "whatever's left over must be a real residual" is only true if every consumer marks its
  conjuncts used, including the one consumer (`joins[k].on`, used verbatim) that doesn't go through
  the shared classification path.*

### B7-3 An ANALYZE'd empty indexed table divided by its own zero row count
- **Symptom:** `ZeroDivisionError` from `assign_cost` (`plan/cost.py`) on `SELECT * FROM t WHERE
  id = 5` against a real, `ANALYZE`'d, genuinely empty table with an index on `id` — no join
  involved; found while testing a LEFT JOIN over an empty inner table, which hits the identical
  single-table code path once the inner side is planned.
- **Assumed:** `stats.row_count` (an index's real, ANALYZE'd row count) is always positive by the
  time `leaf_pages_touched = ceil(stats.leaf_pages * path.rows_fetched / stats.row_count)` runs,
  because every existing test built its fixture tables with at least one row before ANALYZE.
- **Actually:** a table can be ANALYZE'd with zero rows (nothing stops it), giving `row_count == 0`
  and dividing by it. A "fraction of zero leaf pages" was never a case the formula's author had
  reason to consider before a query planned an index scan over a table that turned out empty.
- **Fix:** `row_count == 0` short-circuits to `leaf_pages_touched = 1` — the same floor the general
  formula already clamps to, not a different cost shape. *Lesson: a `stats.field` that's usually a
  divisor is a divide-by-zero waiting for the one fixture nobody happened to write yet.*

## Week 8 — presentation

### B8-1 ★ (fixed) A table delete can leave a zero-cell interior root that `sqlite3` calls malformed
- **Symptom:** found while building `quilldb validate`. A table whose root has exactly two leaves, with
  every row of one leaf deleted, ends up with a root interior page of **zero cells** and one child
  (`quilldb btree FILE --root 3` shows `page 3 INTERIOR_TABLE cells=0`). Real SQLite then refuses the file
  outright: `database disk image is malformed`, a hard parse error, not an `integrity_check` finding.
  Reproduce: 100 rows of `(id, 'x' * 60)`, then `DELETE FROM t WHERE id <= 59`.
- **Assumed:** the docs and `test_delete_from_one_of_two_root_children_leaves_a_single_child_root` call this
  state "tolerated, not collapsed ... less dense, still valid" (`BTree.delete`'s docstring; chapter 10
  §10.3; ADR-005). That holds for a single-child page with a live cell, not for an interior page with none.
- **Actually:** it is the same illegal state as B4-4 and B4-5, which the **index** tree already handles
  (`IndexBTree._rebalance` pulls the lone child up into the root). The **table** tree never got the same
  treatment, and its validator (`validate_btree`) has no zero-cell check, so nothing in quilldb noticed.
  `validate_index_btree` does have it.
- **Fixed.** `BTree.delete` now calls `_collapse_single_child` whenever an interior page is left with one
  child: the root pulls the child up into its own page; any other page is merged into an adjacent sibling,
  or a child is rotated over when the sibling is full, recursing if the grandparent is left with one child.
  A first attempt that simply pointed the grandparent at the child gave `uneven heights [1, 2]`: bypassing
  a page breaks the uniform-depth rule, so a real merge/rotate is needed. `validate_btree` now rejects
  zero-cell interior pages and non-root empty leaves. Tests: the two rewritten/new unit tests in
  `test_btree.py` (root collapse, non-root merge, rotate from a full sibling on either side) and
  `differential/test_delete_integrity.py`, which opens the result with real `sqlite3` (the reproduction plus
  six randomized delete sequences).