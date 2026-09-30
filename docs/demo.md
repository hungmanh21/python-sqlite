# The demo

Two artifacts: a **30-second GIF** for the top of the README, and a **five-minute talk**. Both start from
the same database. Every number below was measured on this repo (Python 3.13, 4096-byte pages, real
`sqlite3` 3.50.4 as the checker); re-check them if the engine changes.

## Setup (before recording)

```console
$ python examples/build_demo_db.py demo.db     # 5,000 users, no indexes, about 3 s
$ quilldb shell demo.db
```

Half the users are `active`, so an index on `active` is nearly useless. Every `email` is distinct, so an
index on `email` is decisive. That gap is what the demo shows the planner working out.

## The GIF (under 30 seconds)

Type these at the `quilldb>` prompt. Every output shown is real.

```sql
SELECT id, email FROM users WHERE active = 1 AND email = 'user4001@example.com';
```
```text
id   | email
-----+---------------------
4001 | user4001@example.com
(1 row)
```

```sql
EXPLAIN SELECT id, email FROM users WHERE active = 1 AND email = 'user4001@example.com';
```
```text
Project
└─ Filter
   └─ SeqScan users est_rows=111111 startup=0.00 cost=20000.00
```

```sql
CREATE INDEX idx_active ON users (active);
CREATE INDEX idx_email ON users (email);
ANALYZE;
EXPLAIN SELECT id, email FROM users WHERE active = 1 AND email = 'user4001@example.com';
```
```text
Project
└─ Filter
   └─ IndexScan idx_email (email = 'user4001@example.com') est_rows=1 startup=8.00 cost=16.01
```

It picked `idx_email`, not the first index that applies. That is the cost model choosing, not "first
applicable index wins".

**Then leave the shell and re-enter it before `EXPLAIN ANALYZE`** (`.quit`, then `quilldb shell demo.db`).
The buffer pool is warm after building the indexes, and `pages_read` counts only cache *misses*, so on
the warm pool the same query reports `pages_read=1 pages_cached=4`, which flatters the index. A fresh
shell is a cold pool, and that is the number worth showing:

```sql
EXPLAIN ANALYZE SELECT id, email FROM users WHERE active = 1 AND email = 'user4001@example.com';
```
```text
Project
└─ Filter
   └─ IndexScan idx_email (email = 'user4001@example.com') est_rows=1 startup=8.00 cost=16.01
actual_rows=1 elapsed=0.000394s pages_read=4 pages_cached=1 rows_examined=1
```

For contrast, a query with no usable index (fresh shell again, or accept the warm-pool caveat aloud):

```sql
EXPLAIN ANALYZE SELECT id, email FROM users WHERE name = 'User number 4001';
```
```text
actual_rows=1 elapsed=0.022314s pages_read=58 pages_cached=2 rows_examined=5000
```

**4 page reads with the index, 58 without: about 14×, on 5,000 rows.** The gap grows with the table. The
README's benchmark has 7 reads against 2,622 at 100,000 rows.

Finally, the independent check:

```text
!sqlite3 demo.db "PRAGMA integrity_check"
ok
```

The real C SQLite is validating quilldb's bytes.

**Two things a viewer may notice, have the sentence ready:**

- `!sqlite3 demo.db .tables` also lists `quill_stat1`. The statistics table is deliberately not named
  `sqlite_stat1`, so real SQLite does not read quilldb's statistics and plan with them (`docs/design_decisions.md`).
- `est_rows=111111` on the first plan is a default for a table with no statistics. `est_rows=1` after
  `ANALYZE` is the real estimate.

**Recording notes.** `asciinema rec demo.cast`, then `agg demo.cast demo.gif`, at a readable font size.
Building the indexes on 5,000 rows plus the shell start-up takes a few seconds of the 30. Trim the wait in
post-production and say so if asked. Cut on a typo instead of using backspace.

## The five-minute talk

Rehearse it **out loud, twice, from memory.**

| Time | Say / do | Backing fact |
|---|---|---|
| 0:00 | "It's a SQL database engine in pure Python. It writes SQLite's real on-disk format, so the `sqlite3` command-line tool can read and verify the files it produces." | `quilldb validate` runs both validators |
| 0:30 | In the shell: create a table, insert, select. | `examples/build_demo_db.py` builds it in advance |
| 1:00 | `EXPLAIN` shows a `SeqScan` with an estimated row count and cost. | est. cost 20000.00 |
| 1:30 | Create both indexes and run `ANALYZE`. `EXPLAIN` now picks `idx_email`. `EXPLAIN ANALYZE` on a fresh shell: **4 page reads against 58** for the scan. "At 100,000 rows it's 7 against 2,622, 375×. You can check the arithmetic: 100k rows of about 100 bytes is roughly 2,400 pages." | README benchmark; `python -m quilldb.bench index_lookup` |
| 2:15 | `!sqlite3 demo.db "PRAGMA integrity_check"` gives `ok`. "That's their C implementation validating my bytes." Then `quilldb validate demo.db`: "both halves agree." | |
| 2:45 | **The crash matrix.** "83 fault-injection tests across six mutation shapes: every write and fsync boundary, plus a second crash during recovery itself. After each one the database is back to its pre-transaction state, every b-tree validates, and `sqlite3` says ok. What it can't show: torn sectors, a lost page cache, a lying disk. Those need block-level fault injection." | [durability.md](durability.md) |
| 3:30 | **The transfer stress test.** "Eight threads, ten thousand transfers each. The sum of balances never changes. One side of a deadlock aborts, the other commits." | [concurrency.md](concurrency.md) |
| 4:15 | **Limitations, unprompted.** "No WAL, so readers can't run alongside a writer. Nested-loop joins only. The planner uses per-index average selectivity, not histograms, so skewed columns give bad estimates. Here's where they fail." | README, *Not implemented* |
| 4:45 | "Why each choice was made is in `docs/theory`, and the decisions that departed from the obvious design are ADRs." | `docs/decisions/` |

Volunteering the limitations at 4:15 is the strongest thirty seconds. It turns "look what I built" into
"here is what it does and doesn't do", and that is the job.

### A real bug to tell, if there's time

`quilldb validate` exists because building it found a bug the test suite could not: deleting most of a
table's rows left an interior page with zero cells. quilldb's own validator had no check for that, and the
tests asserted the bad state as intended behaviour. Real SQLite calls the file *malformed*. The fix is in
`BTree.delete`, and the lesson is in [file-format.md](file-format.md): a quilldb-side validator cannot see
what only the reference implementation rejects (NOTES.md B8-1).

### Questions to expect

| Question | Answer |
|---|---|
| Why 4 page reads for one row? | Two levels of the `email` index (an interior root, then a leaf) plus two levels of the table tree (`quilldb btree demo.db --root 3` shows the interior root over its leaves). |
| Why does a warm run say `pages_read=0`? | It counts misses only. A warm pool serves everything from cache. Re-open for a cold number. |
| Why no WAL? | An undo journal is simpler to prove atomic and matches the single-writer design ([ADR-004](decisions/ADR-004-undo-journal-not-wal.md)). The price: readers block behind a writer. |
| What breaks the estimates? | Skewed or correlated columns. The planner works from an average per index prefix, not a histogram. |
| Can it open my SQLite file? | Not promised. It writes the format faithfully; reading arbitrary SQLite files is out of scope ([ADR-002](decisions/ADR-002-sqlite-format-one-direction.md)). |
