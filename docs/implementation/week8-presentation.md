# quilldb — Implementation Plan: Week 8, Presentation


← [Index](README.md)  ·  Prev: [Week 7 — Query Processing](week-7-query-processing.md)


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


| #   | Deliverable                                                                              | Hours |
| --- | ---------------------------------------------------------------------------------------- | ----- |
| 45  | README — pitch, install, usage, architecture, features, benchmarks, limitations, testing | 3     |
| 46  | Demo GIF / asciinema at the top of the README                                            | 1.5   |
| 47  | `docs/architecture.md`, `file-format.md`, `durability.md`, `concurrency.md`              | 2.5   |
| 48  | 6–8 ADRs in `docs/decisions/`                                                            | 1.5   |
| 49  | Benchmark harness and results table                                                      | 2     |
| 50  | CI: pytest + coverage + mypy strict + ruff on 3.11/3.12/3.13, badges                     | 1     |
| 51  | CLI: `inspect`, `pages`, `btree`, `validate`, `bench`, `shell`                           | 1.5   |
| 52  | 5-minute demo script, rehearsed out loud twice                                           | 1     |


---


## 45. The README


**Order matters more than content, because most readers stop after the first screen.**


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


**Query processing.** Nested loop joins only, max 3 tables. Cost-based planning uses `quill_stat1`
prefix averages rather than histograms, so skew and correlated columns can produce bad estimates.
Join search is exhaustive only within the three-table limit. `ORDER BY` sorts in memory and raises
past N rows.


**Concurrency.** Threads in one process. No multi-process locking; a second process opening the same
file is undefined.


**SQL.** No subqueries, CTEs, window functions, `RIGHT`/`FULL JOIN`, `ALTER TABLE`, foreign keys,
triggers, or views.
```


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
7. EXPLAIN ANALYZE the query              -> actual pages_read=4     ← THE MOMENT
8. !sqlite3 demo.db "PRAGMA integrity_check"   -> ok                 ← THE OTHER MOMENT
```


Steps 6–8 are the whole point. **Step 6 proves selection is cost-based rather than "first applicable
index wins"; step 7 compares the estimate with execution; step 8 shows an independent C implementation
validating your bytes.** A viewer who watches nothing else has seen the claims that matter.


Rehearse it. A GIF with a typo and a backspace signals carelessness about the thing you chose to put at the
top of the page.


---


## 47. The four docs


Each answers one question a reader will have, and each is 1–2 pages. **Write them from the theory chapters
you already have** — that's what they're for.


| Doc               | The question                     | Source                                                                                    |
| ----------------- | -------------------------------- | ----------------------------------------------------------------------------------------- |
| `architecture.md` | how do the pieces fit?           | a diagram matching the real module layout, plus the path of one query end to end          |
| `file-format.md`  | what's on disk?                  | chapters 01–03; the 100-byte header, page layout, record encoding, what's refused and why |
| `durability.md`   | why won't a crash corrupt it?    | chapter 13 §13.4's ordering and §13.11, **plus the non-guarantees**                       |
| `concurrency.md`  | what's guaranteed under threads? | chapter 15 §15.6 — the isolation level **and what it permits**                            |


**`durability.md` must name the commit point precisely and list what it does not guarantee**: a lying
`fsync`, torn sectors on non-atomic hardware, no directory fsync after the journal is unlinked, and what the
crash matrix cannot simulate (chapter 14 §14.4). This is the doc a senior engineer will read most closely,
and the non-guarantees section is what tells them you're trustworthy.


**`architecture.md`'s diagram must match the code.** A diagram showing modules you renamed is worse than no
diagram. Generate the module list from the filesystem if you're worried.


---


## 48. The ADRs


Six to eight, one page each: **Context → Decision → Consequences → Alternatives considered**. These are
where your interview answers come from, so write each one the week you made the decision, not now.


| ADR | Records                                                                  | From                                                                                |
| --- | ------------------------------------------------------------------------ | ----------------------------------------------------------------------------------- |
| 001 | Buffer pool caches raw pages, not parsed objects                         | *(already written)*                                                                 |
| 002 | SQLite's on-disk format exactly, but one direction only                  | roadmap §1.1                                                                        |
| 003 | Iterator pipeline, not a bytecode VM                                     | chapter 09                                                                          |
| 004 | Undo journal, not WAL                                                    | chapter 13 §13.3, §13.10                                                            |
| 005 | No sibling merging on delete                                             | chapter 10 §10.6                                                                    |
| 006 | Table-level 2PL with deadlock detection, not SQLite's file-level ladder  | chapters 15 §15.6, 16 §16.4                                                         |
| 007 | SQLite-style cost model, but exhaustive three-table search instead of N3 | chapter 12 §12.5 (how quilldb scopes the architecture), §12.7 (why not System R DP) |
| 008 | Repack pages on delete instead of maintaining freeblocks                 | chapter 02 §2.4, chapter 10 §10.1                                                   |


**The test for each ADR: can you defend it out loud, without rereading it?** If not, the "alternatives
considered" section is too thin — that's the part that makes a decision defensible, because it shows what
you were choosing *between*.


---


## 49. Benchmarks


Per chapter 19: **page reads are the headline, time is context.**


```python
# benchmarks/run_all.py — emits a paste-able markdown table
BENCHMARKS = [
    "point_lookup_index_vs_scan",      # ~4 vs ~2,417 page reads          ← THE number
    "btree_height_vs_rows",            # 1k->2, 100k->3, 10M->4
    "insert_sequential_vs_random",     # rightmost-split case, chapter 05 §5.7
    "insert_cost_per_index",           # the write-amplification tax     ← publish it
    "buffer_pool_hit_rate_vs_size",    # the working-set knee, chapter 04
    "covering_index_vs_not",           # ~2x, chapter 11 §11.5
    "join_order_cheap_vs_expensive",   # both orders' page reads         ← publish it
    "limit_1_short_circuits",          # <10 page reads over 1M rows
    "throughput_vs_threads",           # reads scale, writes don't       ← publish it
]
```


**Emit markdown, not printed lines.** You'll regenerate these several times during the week, and
hand-copying is how a README ends up with numbers that contradict the code.


**Check every number against the arithmetic before publishing** (chapter 19 §19.9). A scan reporting 4,800
reads where row-size arithmetic predicts 2,400 is a bug, not a benchmark.


**Include the two unflattering rows.** The per-index insert tax and the flat write-throughput curve make
every other row credible (chapter 19 §19.6).


And the test-count table, generated rather than typed:


```bash
pytest --collect-only -q | tail -1        # then break it down by directory
```


The **crash-injection** row is the one an interviewer stops on. Make sure you can explain how those cases
are generated.


---


## 50. CI


```yaml
# .github/workflows/ci.yml
strategy:
  matrix:
    python-version: ["3.11", "3.12", "3.13"]
steps:
  - run: pip install -e ".[dev]"
  - run: ruff check src/
  - run: mypy --strict src/quilldb
  - run: pytest src/tests -m "not slow" --cov=quilldb --cov-report=term
  - run: pytest src/tests -m slow                    # crash matrix, concurrency stress
  - run: python examples/readme_example.py           # the README example cannot rot
  - run: |                                           # the format claim, enforced
      python -c "import quilldb; ..."                # build a db
      sqlite3 /tmp/ci.db "PRAGMA integrity_check" | grep -qx ok
```


**Those last two steps are the ones worth arguing for.** Running the README example in CI means your
front-page code is guaranteed to work. Running `integrity_check` in CI means your central format claim is
*enforced* rather than asserted — the badge is then evidence, and that's a genuinely unusual thing to have.


Badges: CI status, coverage, Python versions, license. Put them on line 2.


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
       EXPLAIN ANALYZE -> 4 actual page reads versus 2417 for the scan.
       "Same query, 600× fewer page reads. You can check the arithmetic:
        100k rows of ~100 bytes is about 2,400 pages."
2:15  sqlite3 the same file: PRAGMA integrity_check -> ok
       "That's their C implementation validating my bytes."
2:45  The crash matrix. "340 cases. Every write and fsync boundary, and after
       each one the database is entirely pre-commit or entirely post-commit."
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


| #   | 2 hours on                                                                   | Done when                                            |
| --- | ---------------------------------------------------------------------------- | ---------------------------------------------------- |
| 1   | Benchmark harness + all eight benchmarks, numbers checked against arithmetic | a paste-able markdown table exists                   |
| 2   | README: pitch, install, example, features, benchmarks                        | the example runs on a clean checkout in a fresh venv |
| 3   | README: limitations, testing table, architecture diagram                     | limitations section is specific, not apologetic      |
| 4   | The four `docs/*.md`, drawn from the theory chapters                         | `durability.md` lists the non-guarantees             |
| 5   | ADRs 002–008                                                                 | each defensible out loud without rereading           |
| 6   | CLI polish + `shell` REPL                                                    | you can drive it in front of someone                 |
| 7   | GIF, CI matrix, badges, demo rehearsal ×2                                    | GIF has no typos; CI green on all three versions     |


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
- [ ] Limitations section is specific — format direction, no merging, no WAL, planner, concurrency, SQL
- [ ] Four `docs/*.md` written; `durability.md` names the commit point **and** the non-guarantees;
      `concurrency.md` names the isolation level **and** what it permits
- [ ] `architecture.md`'s diagram matches the actual module layout
- [ ] 6–8 ADRs, each with a real "alternatives considered" section
- [ ] CI green on 3.11 / 3.12 / 3.13 with `mypy --strict` and `ruff`; badges on line 2
- [ ] `quilldb shell` is a working REPL; `quilldb validate` runs both validators
- [ ] The 5-minute demo delivered twice, out loud, from memory
- [ ] `docs/theory/` linked from the README — twenty chapters of design rationale is a differentiator, and
      an unlinked directory is an invisible one


**The last box is easy to forget and it's most of why the theory exists.** Any reader can see your code.
The reasoning behind it is the part they can only get from you — or from a directory you remembered to link.