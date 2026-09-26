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
