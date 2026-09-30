# quilldb — Implementation Plan: Week 8, Presentation


← [Index](README.md)  ·  Prev: [Week 7 — Query Processing](week7-query-processing.md)


---


# Week 8 Spec — The ROI Multiplier


> **Read [chapter 19](../theory/benchmarks/19-measuring-it.md)** before writing benchmarks. It's short, and
> it's the difference between numbers that survive questioning and numbers that don't.


**Do not treat this week as optional polish.** A recruiter spends ~40 seconds on your GitHub. A hiring
manager spends three minutes. **Neither will read `btree.py`.** Seven weeks of engineering that a stranger
cannot evaluate in 40 seconds is worth less than five weeks they can.


The framing that makes this week easy to take seriously: SQLite ships roughly **590× more test code than
library code** and maintains a public page explaining how it's tested. Nobody picks an embedded database
because its B-tree is elegant — they pick it because the project makes its reliability *legible*. Your
README, ADRs, benchmark table and test-count table do that same job at small scale.


---


## Week 8 deliverables


| #   | Deliverable                                                                              | Hours | Starting point (as of the week 7 merge)                                                        |
| --- | ---------------------------------------------------------------------------------------- | ----- | ---------------------------------------------------------------------------------------------- |
| 45  | README — pitch, install, usage, architecture, features, benchmarks, limitations, testing | 3     | **Exists** (~190 lines): restructure into the order below, add what's missing                  |
| 46  | Demo GIF / asciinema at the top of the README                                            | 1.5   | Nothing                                                                                        |
| 47  | `docs/architecture.md`, `file-format.md`, `durability.md`, `concurrency.md`              | 2.5   | `durability.md` and `concurrency.md` **exist** (review only); the other two are new            |
| 48  | 6–8 ADRs in `docs/decisions/`                                                            | 1.5   | ADR-001 only; most of the rest can be extracted from `docs/design_decisions.md` and `NOTES.md` |
| 49  | Benchmark harness and results table                                                      | 2     | 2 of 9 exist as loose scripts in `benchmarks/`; they move into the package (session 0)         |
| 50  | CI: pytest + coverage + mypy strict + ruff on 3.12/3.13, badges                          | 1     | No `.github/`; `pyproject.toml` needs fixes first (session 0)                                  |
| 51  | CLI: `inspect`, `pages`, `btree`, `validate`, `bench`, `shell`                           | 1.5   | Only `inspect` exists                                                                          |
| 52  | 5-minute demo script, rehearsed out loud twice                                           | 1     | A draft exists below; its numbers are placeholders until session 1                             |


---


## Session 0: reconcile the plan with the code


This spec was drafted before weeks 4–7 landed, so a few of its assumptions were checked against the repo
at the week 7 merge. The mismatches are small, but each one would break something on camera or in CI, so
they get fixed **first**, before any presentation work depends on them.


**Decisions already made:**


- **Python 3.12+, not 3.11.** `pyproject.toml` says `requires-python = ">=3.12"` and the code uses
  PEP 695 syntax (`def f[T](...)`, `type X = ...`) in `btree/split.py`, `exec/expressions.py`,
  `exec/aggregate.py`, `sql/ast.py` and `sql/binder.py`. A 3.11 job would fail on import. The CI matrix is
  **3.12 / 3.13**, and the README badge says so.
- **`mypy` checks the library, not the tests.** `mypy --strict src/quilldb` has one error today; the ~500
  in `src/tests/` are missing annotations in test code, and are deliberately not fixed. The README says
  "strict mypy over the library".
- **The benchmark harness lives in the package** (`src/quilldb/bench/`), so an installed `quilldb bench`
  can import it. As a side effect, `concurrent.py` stops shadowing the stdlib's `concurrent` package
  when run as a script — a hazard the current file's docstring works around.


**Code changes this week depends on (each is small).** All five are done (✅) as of the Session 0 commits;
the harness move only relocated the two existing scripts and added `python -m quilldb.bench` — the
`run_all` table generator is still Session 1's job. Two notes on what actually landed:


- The `SeqScan` annotation reversed a deliberate earlier test ("annotate `IndexScan` only"), and `est_rows`
  keeps `IndexScan`'s meaning — the estimate *after* the residual filter — not the rows the scan emits.
- `pyproject.toml` lists the dev tools twice, as a `dev` extra (what `pip install -e ".[dev]"` and CI use)
  and as a `[dependency-groups]` entry (what `uv sync` uses). They must be kept in step by hand.


| Change | Why the plan needs it |
| --- | --- |
| ✅ `EXPLAIN ANALYZE` reports `pages_read` (and `rows_examined`) next to `actual_rows` / `elapsed` — `api/connection.py` around line 590 | Demo step 7 is "THE MOMENT" and today it prints only `actual_rows=… elapsed=…`. The counters already exist (`pool.misses`, `pool.rows_examined`). |
| ✅ `EXPLAIN` shows `est_rows` / `cost` on the `SeqScan` line too, not just `IndexScan` | Demo step 4 promises "SeqScan, est_rows and cost". Today it prints a bare `SeqScan users`. The planner already computes the sequential cost to compare against. |
| ✅ `pyproject.toml`: rename `sqlite-scratch` → `quilldb`, real description, move `hypothesis`/`pytest` out of runtime `dependencies`, add `pytest-cov`, add a `dev` extra, set mypy `files = ["src/quilldb"]`, `requires-python` stays `>=3.12` | `pip install -e ".[dev]"` finds no `dev` extra today (dev tools are in `[dependency-groups]`, which is uv's mechanism); `pytest-cov` isn't installed anywhere; and a stranger's `pip install -e .` shouldn't pull in test tools. |
| ✅ Fix `storage/bufferpool.py:35` (imports `PAGE_SIZE` from `storage.pager`, which doesn't re-export it — import it from `quilldb.constants`) | The only `mypy --strict src/quilldb` error; CI would fail on it. |
| ✅ Move `benchmarks/*.py` into `src/quilldb/bench/`, expose `python -m quilldb.bench` | See the decision above; the README's `python benchmarks/…` lines change with it. |


**Docs that were stale at the week 7 merge** (fixed alongside this rewrite; listed so they stay fixed):
`docs/design_decisions.md`'s Status column still said "Planned" for weeks 4–7 features;
`docs/theory/README.md` linked `../design-decisions.md` (hyphen) but the file uses an underscore;
ADR-001 cited a pre-reorganisation theory path and called the page-1 offset problem "unsolved".


---


## 45. The README


**Order matters more than content, because most readers stop after the first screen.**


**A README already exists** (written during weeks 4–7). Its content is good — the measured benchmark, the
concurrency write-up, the "Deviations from SQLite" table — but it opens with prose instead of the pitch,
GIF and install, and it's missing badges, an install section, a feature table, a test-count table, and
links to `docs/theory/`, `durability.md` and the ADRs. This item is **restructure and fill gaps**, not
write from scratch. Keep the existing "Deviations from SQLite" table; it's exactly the kind of specific,
unapologetic section this chapter is asking for.


**Status: restructured ✅.** `README.md` now follows this order: pitch, install and a self-contained example
(`examples/readme_example.py`, run and checked on this checkout), architecture, features table, benchmarks
(fresh numbers from `python -m quilldb.bench`), a reconciled "Not implemented" (every SQL entry probed
against the real parser: `IN`, `BETWEEN`, `CASE`, `UNION`, scalar functions, `COUNT(DISTINCT)`, `NOT NULL`
and `PRIMARY KEY` all fail to parse and are listed), the deviations table, test counts (1,664 = 1,264 unit +
287 differential + 83 fault-injection + 30 concurrency), and design decisions. **Still open, each waiting on
another item:** the demo GIF (§46), the CI and coverage badges and the CI step that runs
`examples/readme_example.py` (§50), a `LICENSE` file (there is none, so no licence badge), and more ADR
links (§48; only ADR-001 exists).


```markdown
# quilldb
> A SQL database engine written from scratch in pure Python.
[badges: CI · coverage · python versions · license]


![demo](docs/demo.gif)                      ← ABOVE the fold. Non-negotiable.


## Install & use
    pip install -e .
[the 15-line example — must work on a clean checkout, first try]


## What's inside
[architecture diagram]


## Features                                  ✅ / ❌ table
## Benchmarks                                measured numbers, per chapter 19
## Not implemented / known limitations       ← one of the highest-signal sections
## Testing                                   counts by category
## Design decisions                          links to the ADRs
```


### The one-sentence pitch


Every clause must be a thing you actually built:


> **quilldb is a relational database engine written from scratch in pure Python: a paged storage layer
> writing SQLite's on-disk format, an LRU buffer pool, B+tree tables and indexes, a hand-written SQL parser
> and iterator-based query executor with a statistics-driven cost-based optimizer, crash-safe transactions with an undo
> journal, and multi-threaded connections with table-level 2PL and deadlock detection — validated by
> `PRAGMA integrity_check` and a crash-injection matrix.**


Read it back against the repo and delete any clause you can't demonstrate in 30 seconds.


### The usage example


**Test it on a clean checkout in a fresh virtualenv.** This is the single most common way a good project
loses a reader: the example doesn't run because it depends on a file you had lying around. Make it
self-contained, put it in `examples/readme_example.py`, and **have CI execute it** so it can never rot.


### The limitations section, which is the counterintuitive one


Be specific and unapologetic. Specific limitations read as engineering judgement; vague ones read as
ignorance.


```markdown
## Not implemented


**On-disk format.** quilldb *writes* SQLite's format and `PRAGMA integrity_check` passes. It does not
*read* arbitrary sqlite3-written files — no freeblock parsing, no WAL, no auto-vacuum pointer maps, no
page sizes other than 4096, no UTF-16. `FileHeader.check_supported()` refuses those explicitly rather
than misreading them.


**B-tree.** Two-way splits, no sibling merging. SQLite rebalances below ⅓ page occupancy
(`nFree*3 <= usableSize*2` in `balance()`); quilldb frees pages only when they empty, so a delete-heavy
workload leaves the file larger than optimal until the space is reused. Still a valid tree — occupancy is
not a format constraint.


**No WAL.** Undo journal only. WAL would allow readers to run concurrently with a writer; it needs a
shared-memory index over the log and a checkpointer.


**Query processing.** Nested loop joins only. Cost-based planning uses `quill_stat1` prefix averages
rather than histograms, so skew and correlated columns can produce bad estimates. Join-order search is
exhaustive (every legal left-deep order), which is only cheap because the tests never join more than three
tables (3! = 6 orders) — the grammar has no such limit, so a longer chain still runs, just without the
same search-cost guarantee. `ORDER BY` sorts in memory and raises `SortLimitExceededError` past
`MAX_SORT_ROWS` (1,000,000).


**Concurrency.** Threads in one process. No multi-process locking; a second process opening the same
file is undefined.


**SQL.** No subqueries, CTEs, window functions, `RIGHT`/`FULL JOIN`, `DROP`/`ALTER TABLE`, foreign keys,
triggers, or views.
```


> **Before publishing, reconcile this block with the README's existing "Not implemented" list** and check
> each SQL entry against `sql/parser.py`. The two lists should be one list. In particular, `GROUP BY` /
> `HAVING` / `DISTINCT` / `OFFSET` **are** implemented now (week 7), but combining `GROUP BY`, `HAVING` or
> `DISTINCT` with a `JOIN` is rejected explicitly by the binder — that specific gap belongs here by name.
> Already confirmed against the code: the format bullet (`check_supported()` refuses non-4096 pages,
> UTF-16, WAL, reserved space, auto-vacuum, and old schema formats) and the sort limit. The SQL bullet is
> not yet checked.


**Why this section works:** it proves you know the difference between a subset and a product, it pre-empts
"did you realize you didn't handle X," and every entry demonstrates knowledge of the thing you skipped.
Notice how much more each line says than "not implemented" would.


---


## 46. The demo GIF


`asciinema rec` + `agg` to convert, or `terminalizer`. **Under 30 seconds**, no typing mistakes, readable
font size.


**The shot list:**


```
1. quilldb shell demo.db
2. CREATE TABLE users (...); a few INSERTs
3. SELECT ... WHERE active=1 AND email=?  -> rows come back
4. EXPLAIN the same query                 -> SeqScan, est_rows and cost
5. CREATE INDEX idx_active; CREATE INDEX idx_email; ANALYZE
6. EXPLAIN again                          -> chooses selective idx_email, not first idx_active
7. EXPLAIN ANALYZE the query              -> actual pages_read=<N>   ← THE MOMENT
8. !sqlite3 demo.db "PRAGMA integrity_check"   -> ok                 ← THE OTHER MOMENT
```


**Verified against the engine at the week 7 merge:** step 6 works — on a 5,000-row table with `idx_active`
and `idx_email`, `ANALYZE` makes the planner pick `idx_email`. Step 8 works with the system `sqlite3`
(3.45.1). One thing a viewer will notice: `sqlite3 demo.db .tables` also lists `quill_stat1`, because
quilldb's statistics table deliberately isn't named `sqlite_stat1` (see `docs/design_decisions.md`).
Have the sentence ready.


**Two practical constraints on a 30-second recording:**


- The scan-vs-index gap only looks dramatic on a big table (the README's 2,622 vs 7 is at 100k rows), but
  100k inserts and two `CREATE INDEX`es on 100k rows won't fit in 30 seconds. Either start the recording
  from a pre-built table (a small `examples/build_demo_db.py`) and index a smaller column, or cut the wait
  in post — but say which you did if asked.
- `!` is not SQL. The `shell` REPL must implement it as "run the rest of the line as a shell command",
  which is why it appears in the CLI section below.


Steps 6–8 are the whole point. **Step 6 proves selection is cost-based rather than "first applicable
index wins"; step 7 compares the estimate with execution; step 8 shows an independent C implementation
validating your bytes.** A viewer who watches nothing else has seen the claims that matter.


Rehearse it. A GIF with a typo and a backspace signals carelessness about the thing you chose to put at the
top of the page.


---


## 47. The four docs


Each answers one question a reader will have, and each is 1–2 pages. **Write them from the theory chapters
you already have** — that's what they're for.


| Doc               | The question                     | Source                                                                                    | State                                        |
| ----------------- | -------------------------------- | ----------------------------------------------------------------------------------------- | -------------------------------------------- |
| `architecture.md` | how do the pieces fit?           | a diagram matching the real module layout, plus the path of one query end to end          | **New**                                      |
| `file-format.md`  | what's on disk?                  | chapters 01–03; the 100-byte header, page layout, record encoding, what's refused and why | **New** — `design_decisions.md`'s storage and encoding tables are most of the raw material |
| `durability.md`   | why won't a crash corrupt it?    | chapter 13 §13.4's ordering and §13.11, **plus the non-guarantees**                       | **Exists** — review, don't rewrite           |
| `concurrency.md`  | what's guaranteed under threads? | chapter 15 §15.6 — the isolation level **and what it permits**                            | **Exists** — review, don't rewrite           |


**Reviewing the two existing docs.** Both already name what they must (`durability.md`: the commit point
and eight non-guarantees; `concurrency.md`: serializable, and a "What it permits" section). Three things
to check rather than rewrite:


- `durability.md` cites `journal.py:251` by line number, which drifts. Prefer the function name.
- `concurrency.md` opens "Written before `txn/locks.py`" — true when written, but a reader arriving in
  week 8 should see present tense. The "Confirmed (sessions 1–7)" checklist at the bottom already does the
  real work; lead with it.
- Both should be linked from the README (today only `concurrency.md` is).


**`durability.md` must name the commit point precisely and list what it does not guarantee**: a lying
`fsync`, torn sectors on non-atomic hardware, no directory fsync after the journal is unlinked, and what the
crash matrix cannot simulate (chapter 14 §14.4). This is the doc a senior engineer will read most closely,
and the non-guarantees section is what tells them you're trustworthy.


**`architecture.md`'s diagram must match the code.** A diagram showing modules you renamed is worse than no
diagram. Generate the module list from the filesystem if you're worried.


---


## 48. The ADRs


Six to eight, one page each: **Context → Decision → Consequences → Alternatives considered**. These are
where your interview answers come from. The ideal is to write each one the week you made the decision; that
didn't happen, so this week is a **harvest**, not a first draft: `docs/design_decisions.md` already argues
most of these in prose (its "Different" rows), and `NOTES.md` has the bugs that tested them. Restructure
that material into the four headings, and add the alternatives section where the prose only argued one side.


| ADR | Records                                                                            | From                                                                                |
| --- | ---------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| 001 | Buffer pool caches raw pages, not parsed objects                                   | *(already written — refresh its stale references, see Session 0)*                   |
| 002 | SQLite's on-disk format exactly, but one direction only                            | roadmap §1.1; `design_decisions.md` header                                          |
| 003 | Iterator pipeline, not a bytecode VM                                               | chapter 09; `design_decisions.md` "Iterator pipeline"                               |
| 004 | Undo journal, not WAL                                                              | chapter 13 §13.3, §13.10                                                            |
| 005 | No sibling merging on delete                                                       | chapter 10 §10.6                                                                    |
| 006 | Table-level 2PL with deadlock detection, not SQLite's file-level ladder            | chapters 15 §15.6, 16 §16.4; `docs/concurrency.md`                                  |
| 007 | SQLite-style cost model, with exhaustive left-deep join search instead of N3       | chapter 12 §12.5 (how quilldb scopes the architecture), §12.7 (why not System R DP) |
| 008 | Repack pages on delete instead of maintaining freeblocks                           | chapter 02 §2.4, chapter 10 §10.1                                                   |


ADR 007's title changed from "three-table search": the search enumerates every legal order with no hard
cap, and it is only *cheap* at three tables. Say so in the ADR rather than claiming a limit the code
doesn't enforce.


**Better ADR candidates than 005 or 008, if you have to choose.** Two decisions that have no ADR yet and
are more distinctive than the ones above: **hash aggregation** (quilldb deliberately diverges from SQLite,
which has no hash operator, because Python's `dict` removes the constraint that made SQLite's choice right
— `design_decisions.md` argues this in full) and **`quill_stat1` instead of `sqlite_stat1`** (same bytes,
different name, so real SQLite never plans with quilldb's statistics). Swap them in for whichever pair you
can defend least well out loud.


**The test for each ADR: can you defend it out loud, without rereading it?** If not, the "alternatives
considered" section is too thin — that's the part that makes a decision defensible, because it shows what
you were choosing *between*.


---


## 49. Benchmarks


Per chapter 19: **page reads are the headline, time is context.**


```python
# src/quilldb/bench/run_all.py — emits a paste-able markdown table
BENCHMARKS = [
    "point_lookup_index_vs_scan",      # EXISTS (index_lookup.py): 2,622 vs 7 page reads, 375×   ← THE number
    "btree_height_vs_rows",            # EXISTS (btree_height.py): 1k->2, 100k->3 measured; 10M->4 computed
    "insert_sequential_vs_random",     # EXISTS (inserts.py, via BTree): 1 vs 12,259 reads       ← publish it
    "insert_cost_per_index",           # EXISTS (index_lookup.py): ~1× / ~2× / ~4× (2.17/4.18 in README; 1.99/3.93 on rerun) ← publish it
    "buffer_pool_hit_rate_vs_size",    # EXISTS (hit_rate.py): 70.7% at 8 pages -> 99.5% at 512
    "covering_index_vs_not",           # EXISTS (covering_index.py): 7 vs 3 reads (2.3x) -- index-ONLY, quilldb has no covering path
    "join_order_cheap_vs_expensive",   # EXISTS (join_order.py): 388 vs 7,703 reads (20x)         ← publish it
    "limit_1_short_circuits",          # EXISTS (limit_short_circuit.py): 4 reads vs 1,990 over 100k rows
    "throughput_vs_threads",           # EXISTS (concurrent.py): neither reads nor writes scale   ← publish it
]
```


That's **nine** benchmarks, three of which already exist as scripts and just need moving into the package
and reformatting as tables. Most of the rest need only what the engine already exposes: `db.pages_read`
(buffer-pool misses), `db.pages_cached` (hits), `db.rows_examined` and `db.reset_counters()`. Three of them
depend on something to settle first:


- **`insert_sequential_vs_random`** ✅ done via the `BTree` API (`bench/inserts.py`), because the SQL
  surface can't choose a rowid (`sql/parser.py` has no `PRIMARY KEY`). The table is labelled "B+tree
  insert", not "INSERT". 20,000 rows through a 64-page pool: sequential 1 read, random 12,259; a cold search
  of every key reads 363 pages sequentially, 16,580 randomly.
- **`buffer_pool_hit_rate_vs_size`** ✅ done. `connect(path, pool_capacity=…)` now reaches the pool, and
  `bench/hit_rate.py` sweeps it. Writing it exposed a bug: `allocate_page` never evicted, so capacity was
  enforced on reads only (fixed; see the ADR-001 update). Two results to publish honestly:
  - The Zipf-skewed sweep is a gentle curve, not a cliff: 70.7% at 8 pages, 88.2% at 64, 93.7% at 128,
    99.5% at 512. State the skew (exponent 1.2, hot ids scattered); uniform draws give a sharp knee at
    about 512 pages instead.
  - **Chapter 04 ("What you should do") says "hit rate went from 99% to near zero after a scan". Measured, it does not:**
    100% before, **67.5%** on the first pass over a 20-id hot set right after a full-table scan, 100% once
    the hot set is reloaded. The floor is high because every lookup re-hits the catalog and index-root
    pages within the same query. Either soften the chapter to "the scan evicts the hot set (about 3 reads
    per lookup instead of 0 on the first pass)" or drop the number, and report reads, per chapter 19.
    *(Done: chapter 04's "Say this out loud" now states the measured 100% / 67.5% / 100%.)*
- **`btree_height_vs_rows`** ✅ done. Built at 1k / 10k / 100k rows (heights 2 / 2 / 3, measured 53.6 rows
  per leaf and 312 children per interior page); 1M / 10M / 100M are computed from that fanout and labelled
  "computed" in the output (heights 3 / 4 / 4). Building 1M+ rows takes too long to be worth it.
- **`limit_1_short_circuits`** ✅ done, at 100,000 rows rather than 1M: `LIMIT 1` reads 4 pages against 1,990
  for the full scan. `ORDER BY grp LIMIT 1` reads all 1,990, because a sort blocks the short-circuit; keep
  that row, it is the one people expect to be cheap.
- **`join_order_cheap_vs_expensive`** ✅ done, with two caveats to state in the write-up. (1) A `LEFT JOIN`
  is used to pin the written order, because an INNER join is always reordered to the cheapest; the data
  guarantees every left row matches so all statements return the same rows. (2) The effect is in page
  reads (388 vs 7,703), not rows examined (about 100,000 either way), and it needs the inner table bigger
  than the pool. The planner does not use an index on the join column at these sizes: its cost model is in
  pages and prices a scan of the inner table below the seeks.
- **`covering_index_vs_not`** ✅ done, but **not as the plan described it**: quilldb has no index-only path
  (`IndexScan` always looks up the table row), so the benchmark compares the engine's real cost with a
  direct `IndexBTree.seek_range` walk (7 vs 3 reads at 1 row, 13 vs 4 at 200 rows). Present it as "what a
  covering index would save", never as a feature quilldb has. Possible follow-up: implement it.


**`run_all` ✅ done** (`python -m quilldb.bench [names…] [--list]`): each benchmark's own table under a
heading, with a one-line caveat, in a fenced block, ready to paste. Full run: a few minutes. **Two lessons
from writing it:** (1) `index_lookup` originally inserted one row per autocommitted statement, which cost an
`fsync` each (~15 ms). Setup took ~54 minutes and the write-side table read 1.00× / 1.03× / 1.11×, hiding the
index tax completely. Batched into one transaction it runs in under 2 minutes and shows ~1× / 2× / 4×. State
in the write-up that the tax is measured inside a transaction. (2) Every other benchmark batches for the
same reason.

**The throughput row has changed meaning since this plan was drafted.** The original expected shape was
"reads scale, writes don't". The measurement (`concurrent.py`) says reads *fall* slightly as threads are
added (4,983 → 3,321 txn/s from 1 to 8) because CPython's GIL runs one thread's bytecode at a time, and a
read here is pure CPU. Writes are flat for a different, structural reason: one `__writer__` lock, one
commit in flight, ~4 real `fsync`s each. Publish that — "neither scales, for two different reasons" is a
better answer than the one originally expected, because it shows you found the GIL ceiling rather than
assuming the lock manager was the limit.


**Emit markdown, not printed lines.** You'll regenerate these several times during the week, and
hand-copying is how a README ends up with numbers that contradict the code.


**Check every number against the arithmetic before publishing** (chapter 19 §19.9). A scan reporting 4,800
reads where row-size arithmetic predicts 2,400 is a bug, not a benchmark.


**Include the two unflattering rows.** The per-index insert tax and the flat write-throughput curve make
every other row credible (chapter 19 §19.6).


And the test-count table, generated rather than typed:


```bash
pytest -m "" --collect-only -q | tail -1       # -m "" overrides addopts' "not slow", so slow tests count
pytest -m "" --collect-only -q src/tests/unit  # then break it down by directory
```


At the week 7 merge that gives **1,657** tests in total (1,586 in the default run, 71 more under `slow`):


| Category                | Tests | Where                                                                    |
| ----------------------- | ----: | ------------------------------------------------------------------------ |
| Unit                    | 1,257 | `src/tests/unit/` (5 are `slow`)                                         |
| Differential vs sqlite3 |   287 | `src/tests/differential/`, including the generated NULL matrix           |
| **Crash injection**     |  **83** | `src/tests/fault_injection/` (18 in the default run, 65 `slow`)        |
| Concurrency             |    30 | `src/tests/concurrency/` (1 `slow`: the 8-thread transfer stress test)   |


Regenerate these; don't copy them. Two things about the table as it stands:


- **Chapter 19's example table is illustrative, not a target.** Its "340 crash cases" and "23 corruption
  tests" are made-up round numbers. Publish the categories that exist, with the numbers the collector
  prints. There is no dedicated corruption-handling directory, and Hypothesis property tests are spread
  across 14 unit files rather than living in one place — a "Property (Hypothesis)" row needs a marker
  (`@pytest.mark.property`) or a grep-based count, and should be added only if you'll maintain it.
- **Collection is slow (~45 s)**, and the reason is worth knowing: the crash matrix *measures* each
  scenario's real write and fsync counts at collection time to decide how many cases to generate
  (`_measure_real_write_count`, `_measure_real_sync_count`). The script that builds the table should call
  pytest once and cache the result, not re-collect per category.


The **crash-injection** row is the one an interviewer stops on. The cases are generated, not hand-listed:
six mutation scenarios (`single_insert`, `insert_causing_split`, `delete_freeing_page`,
`update_with_indexes`, `analyze_refresh`, `multi_page_txn`) × every real write boundary and every real
fsync boundary, plus a nested crash *inside* recovery. Be ready to say what it cannot show: per
`durability.md`, this harness only ever observes the pre-commit state after a crash, because nothing is
written to the database file before the commit barrier and `Journal.delete()` makes no `.write()` call to
fault. The demo script and README must not claim more than that.


---


## 50. CI


**Status: written ✅, not yet run on GitHub.** `.github/workflows/ci.yml` has two jobs: `test` (3.12 and
3.13: `ruff check .`, `mypy --strict src/quilldb`, the fast suite with coverage, the README example, and
`python -m quilldb.bench --list`) and `slow` (3.13 only, `pytest -m slow`). Every command was run locally
and passes (the fast suite: 1,593 tests, 96% coverage, about 6 minutes), but the workflow itself has not
run, and the slow suite was not run in this pass. Deviations from the sketch below: the example writes to a
temp directory and **asserts** `integrity_check == "ok"` with the standard library's `sqlite3`, instead of a
separate `sqlite3` CLI step; `ruff check .` covers `examples/` too; coverage is printed in the log and there
is no coverage badge (it needs a third-party service). The README's CI badge points at
`hungmanh21/python-sqlite`, so it stays grey until the first run.


```yaml
# .github/workflows/ci.yml
strategy:
  matrix:
    python-version: ["3.12", "3.13"]                 # not 3.11: the code uses PEP 695 syntax
steps:
  - run: pip install -e ".[dev]"                     # needs the `dev` extra from Session 0
  - run: ruff check src/
  - run: mypy --strict src/quilldb                   # the library only; tests are deliberately unchecked
  - run: pytest src/tests -m "not slow" --cov=quilldb --cov-report=term
  - run: pytest src/tests -m slow                    # crash matrix, concurrency stress
  - run: |                                           # the README example cannot rot,
      python examples/readme_example.py              # and the format claim is enforced
      sqlite3 readme_example.db "PRAGMA integrity_check" | grep -qx ok
```


Two implementation notes:


- **One example, two guarantees.** `readme_example.py` should write `readme_example.db` in the current
  directory (deleting any previous one first), so the last two steps share a file and there's no separate
  "build a database" script to drift from the README's. The old draft's `python -c "import quilldb; ..."`
  placeholder is gone for that reason.
- **Time budget.** The slow tests are minutes, not seconds — the transfer stress test alone is 131–171 s
  per run (see `docs/concurrency.md`), and the crash matrix's `multi_page_txn` scenario re-runs a
  35,000-insert setup per crash point. Running them on both Python versions doubles that; running `-m slow`
  on 3.13 only is a reasonable trade if the CI bill or wall time matters.


**Those last two steps are the ones worth arguing for.** Running the README example in CI means your
front-page code is guaranteed to work. Running `integrity_check` in CI means your central format claim is
*enforced* rather than asserted — the badge is then evidence, and that's a genuinely unusual thing to have.


Badges: CI status, coverage, Python versions, license. Put them on line 2. A *coverage* badge needs
somewhere to publish the number (Codecov, or a generated-badge gist); if that's more setup than it's worth,
print coverage in the CI log and keep the other three badges — an honest three beats a broken four.

The mypy claim in the README should read "strict mypy over the library", not "strict mypy" — `src/tests/`
has ~500 missing-annotation errors that CI doesn't check, and a reader who runs plain `mypy` will find them.


---


## 51. The CLI


```
quilldb shell app.db                REPL — the single highest-value 45 minutes this week
quilldb inspect app.db              header fields, page count, freelist, schema
quilldb pages app.db --range 1-10   page types, cell counts, free space
quilldb btree app.db --root 2       render the tree structure
quilldb validate app.db             your validator + shell out to sqlite3 integrity_check
quilldb bench app.db                the benchmark table
```


**`shell` earns its time on its own: "let me just show you" beats any explanation.** It's also what makes
the GIF possible.


`validate` running *both* your validator and `sqlite3 integrity_check` is a nice touch — one command that
demonstrates the whole format-fidelity bet.


**Design notes, from what exists:**


- **Today only `inspect` exists**, and it deliberately reads the 100-byte header directly instead of going
  through `Pager`, so it works on files quilldb can't open. `pages`, `btree` and `validate` should keep
  that property where they can: a diagnostic tool that refuses to run on the broken file you wanted to
  diagnose is worse than none.
- **`validate`'s first half** is `btree/validate.py`, which today is used by tests, not the running engine.
  It needs a small driver: walk the catalog, run the validator over every table and index root. The second
  half shells out to `sqlite3`; check `shutil.which("sqlite3")` first and print "sqlite3 not found — ran
  quilldb's validator only" rather than failing, since the binary is a dependency of the *check*, not of
  quilldb.
- **`shell`** needs: statements terminated by `;` (so multi-line input works), `EXPLAIN` output printed
  as-is, `.tables`-style conveniences only if cheap, and **`!command`** running the rest of the line in the
  system shell — that is what the GIF's step 8 uses.
- **`bench`** calls `quilldb.bench` (the harness moves into the package in Session 0). It takes no
  `app.db` argument — each benchmark builds its own database in a temp directory — so drop the argument
  from the usage line above when you implement it.


---


## 52. The demo script


Five minutes, rehearsed **out loud, twice, from memory.** Reading it aloud is not optional; you will
discover which sentences you can't actually say.


```
0:00  "It's a SQL database engine in pure Python. It writes SQLite's real
       on-disk format, so the sqlite3 command-line tool can read and verify
       the files it produces."
0:30  shell: CREATE TABLE, INSERT, SELECT
1:00  EXPLAIN -> SeqScan with estimated rows and cost
1:30  CREATE indexes on active and email; ANALYZE; EXPLAIN -> selective
       email index, not the first applicable low-selectivity index.
       EXPLAIN ANALYZE -> 7 actual page reads versus 2,622 for the scan.   [figures from the README's
       "Same query, 375× fewer page reads. You can check the arithmetic:     measured run; re-take them
        100k rows of ~100 bytes is about 2,400 pages."                       after the bench is moved]
2:15  sqlite3 the same file: PRAGMA integrity_check -> ok
       "That's their C implementation validating my bytes."
2:45  The crash matrix. "83 generated cases across six mutation shapes: every
       write and fsync boundary, plus a second crash during recovery itself.
       After each one the database is back to its pre-transaction state, every
       b-tree validates, and sqlite3 says ok. What it can't show: torn sectors,
       a lost page cache, a lying disk. Those need block-level fault injection."
3:30  The 8-thread transfer stress test. "Sum of balances never changes. One
       side of a deadlock aborts, the other commits."
4:15  Limitations, unprompted. "No WAL, nested loop joins only, stat1-style
       averages rather than histograms. Here's where estimates fail."
4:45  "The write-up of why SQLite made each of these choices is in docs/theory."
```


**Volunteering the limitations at 4:15 is the strongest 30 seconds in the demo.** It converts you from
someone showing off a project into someone assessing one, which is what the job is.


---


## Week 8 sessions


| #   | 2 hours on                                                                              | Done when                                                                |
| --- | --------------------------------------------------------------------------------------- | ------------------------------------------------------------------------ |
| 0   | Session 0 above (about 1–2 h, taken from the time the existing README and docs save)   | `EXPLAIN ANALYZE` shows `pages_read`; `pip install -e ".[dev]"` works; `mypy --strict src/quilldb` is clean; the harness is in `src/quilldb/bench/` |
| 1   | Benchmark harness + the remaining six benchmarks (three already exist), checked against arithmetic | a paste-able markdown table of all nine exists                           |
| 2   | README: restructure the existing one into pitch, GIF slot, install, example, features   | the example runs on a clean checkout in a fresh venv                     |
| 3   | README: reconcile limitations, generate the testing table, add architecture diagram      | limitations section is specific, not apologetic, and matches the parser  |
| 4   | `architecture.md` + `file-format.md` (new); review the two existing docs                | `durability.md` lists the non-guarantees; all four are linked from README |
| 5   | ADRs 002–008, harvested from `design_decisions.md` and `NOTES.md`                       | each defensible out loud without rereading                               |
| 6   | CLI: `pages`, `btree`, `validate`, `bench`, and the `shell` REPL                        | you can drive it in front of someone                                     |
| 7   | GIF, CI matrix, badges, demo rehearsal ×2                                               | GIF has no typos; CI green on both Python versions                       |


---


## Week 8 definition of done


- [ ] A stranger can clone, `pip install -e .`, paste the README example, and have it work **first try** —
      tested on a clean checkout in a fresh virtualenv
- [ ] CI runs the README example, so it cannot rot
- [ ] CI runs `sqlite3 ... PRAGMA integrity_check` on a quilldb-written file — the format claim is enforced
- [ ] The GIF is above the fold, under 30 seconds, no typos, and includes both the `EXPLAIN` before/after
      and the `integrity_check`
- [ ] Benchmark table has measured numbers, states cache state and run count, and every figure agrees with
      the arithmetic
- [ ] The table includes the two unflattering rows: per-index insert cost, and flat write throughput
- [ ] Test-count table is generated from a real collection, with the crash-injection row called out
- [ ] Limitations section is specific — format direction, no merging, no WAL, planner, concurrency, SQL —
      and is a single list, reconciled with the README's existing "Not implemented"
- [ ] Four `docs/*.md` written; `durability.md` names the commit point **and** the non-guarantees;
      `concurrency.md` names the isolation level **and** what it permits
- [ ] `architecture.md`'s diagram matches the actual module layout
- [ ] 6–8 ADRs, each with a real "alternatives considered" section
- [ ] CI green on 3.12 / 3.13 with `mypy --strict src/quilldb` and `ruff`; badges on line 2
- [ ] `EXPLAIN ANALYZE` reports `pages_read`, so the demo's central moment is real output, not a mock-up
- [ ] No stale docs: `design_decisions.md` statuses match the code, and every link from the README and
      `docs/theory/README.md` resolves
- [ ] `quilldb shell` is a working REPL; `quilldb validate` runs both validators
- [ ] The 5-minute demo delivered twice, out loud, from memory
- [ ] `docs/theory/` linked from the README — twenty chapters of design rationale is a differentiator, and
      an unlinked directory is an invisible one


**The last box is easy to forget and it's most of why the theory exists.** Any reader can see your code.
The reasoning behind it is the part they can only get from you — or from a directory you remembered to link.