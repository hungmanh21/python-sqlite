# quilldb — A SQL Database Engine in Pure Python


## 8-Week Roadmap, Optimized for a Portfolio Project


> Previous version archived at `roadmap.original.md`. This revision re-cuts the scope around one
> question: **what maximizes hiring signal per hour for an entry-level backend role?**
>
> **New to database internals? Read `guide.md` first.** It explains every concept here with
> analogies, breaks each week into ~7 two-hour sessions, and has a glossary. This file is the terse
> planning reference; `guide.md` is the teaching version. `references.md` has the annotated reading
> list, and **`docs/theory/` has the actual theory** — the physics, the design space, and why SQLite
> chose what it chose, with primary sources. Chapters 00–06 (foundations, weeks 1–2) are written.


---


## 0. The One-Sentence Pitch


> **quilldb is a relational database engine written from scratch in pure Python: a paged storage
> layer with an LRU buffer pool, B+tree indexes, a SQL parser and iterator-based query executor,
> crash-safe transactions with an undo journal, and multi-threaded connections with deadlock
> detection. 1,400 tests including crash-injection recovery proofs and differential testing
> against SQLite.**


Every clause of that sentence is a deliverable in this plan. If a task doesn't strengthen a clause
in that sentence or the README, it's cut.


### Target end state


```python
import quilldb


db = quilldb.connect("app.db")
db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT, age INTEGER)")
db.execute("CREATE INDEX idx_email ON users (email)")


with db.transaction():
    for i in range(10_000):
        db.execute("INSERT INTO users VALUES (?, ?, ?)", (i, f"u{i}@x.com", i % 80))


print(db.execute("SELECT email FROM users WHERE email = ?", ("u42@x.com",)).fetchone())
print(db.execute("EXPLAIN SELECT age, COUNT(*) FROM users GROUP BY age").explain())


# and from 8 threads at once, safely
with ThreadPoolExecutor(8) as pool:
    pool.map(do_work, [quilldb.connect("app.db") for _ in range(8)])
```


---


## 1. What Changed From v1, And Why


### 1.1 Narrowed: SQLite's format exactly, but only one direction


**quilldb writes the real SQLite on-disk format, byte for byte.** The `"SQLite format 3\000"` magic,
SQLite's 100-byte header layout, page 1 doubling as the `sqlite_schema` root, 8-or-12-byte page
headers, page types 2/5/10/13, 9-byte varints, trunk-page freelists, SQLite's exact overflow
threshold arithmetic. The acceptance test is one line:


```bash
sqlite3 yourfile.db "PRAGMA integrity_check"   # must print: ok
```


**What v1 wanted and this doesn't:** full *bidirectional* interop — reading arbitrary files that
`sqlite3` wrote, and surviving `sqlite3` writing to a file mid-flight. That's the expensive half,
and it's what actually cost 50–60 hours: freeblock parsing, the rollback-journal byte format and
hot-journal detection, WAL, auto-vacuum pointer maps, non-4096 page sizes, UTF-16 text encodings,
schema formats 1–3, and exact type affinity. All cut. `FileHeader.check_supported()` **refuses**
files using those features rather than misreading them, which is the honest engineering answer and
also about 15 lines.


**Why matching the bytes is cheap when interop isn't.** Look at what week 1 builds anyway: slotted
pages, varints, a serial-type record codec, overflow chains, a freelist, a 100-byte header. The
*structures* were always on the plan. Writing SQLite's version of them instead of your own is mostly
a matter of which constants you type. The genuinely extra work is small and enumerable:


| Extra work | Cost |
|---|---|
| Freelist trunk pages instead of a one-level list (§ chapter 01 §1.8) | ~2h |
| Page 1's header at offset 100, and its 100 fewer usable bytes | ~1h |
| Header size 8 vs 12 by page type, threaded through offset arithmetic | ~1h |
| Exact X/M/K overflow thresholds instead of `USABLE // 4` | ~1h |
| Four cell formats rather than two | ~2h |
| `sqlite_schema` rows holding real `CREATE TABLE` text (week 3) | ~2h |
| Header fields you don't use but must write correctly (cookies, fractions, encoding) | ~1h |


**~10 hours, not 50.** The deadline survives.


**What you gain, and it's the actual reason to do this:** `PRAGMA integrity_check` becomes a free
adversarial validator. It walks every b-tree, checks cell ordering and page types, follows every
overflow chain, cross-checks the freelist, and confirms every page is accounted for exactly once —
in C, written by people who have been finding these bugs since 2000. Run it after every test.
That's worth more than the interop demo it replaces, and chapter 06 §6.6 makes the full argument.


**What we still lose:** the alternating-writer demo — `sqlite3` and quilldb taking turns writing the
same file. That needs journal-format compatibility and the reverse read direction. It goes on the
"future work" list in the README, which is where ambitious-but-uncut scope belongs.


> **Format fidelity is not feature completeness, and the distinction matters when you describe this
> project.** quilldb still doesn't do three-way rebalancing, page merging, WAL, or `VACUUM`. None of
> those change the bytes — a two-way-split tree is a *valid* SQLite tree, just less dense. So they're
> **implementation gaps**, not divergences from the spec. "Not yet implemented" and "deliberately
> different from the format" are very different claims, and only the first one is true here.


> Keep behavioural differential testing too. Run the same SQL through `sqlite3` and through quilldb
> and compare result sets. That's a *second*, independent oracle — `integrity_check` tells you the
> file is structurally sound, differential testing tells you the answers are right. You want both.


### 1.2 Dropped: SQLite-style bytecode VM → iterator pipeline


A Volcano-style operator tree (`SeqScan → Filter → Project`, each with `next()`) is about a third
the code of a register machine plus compiler, is far easier to debug, gives you `EXPLAIN` for free
as a tree-print, and is how Postgres and most real systems actually execute. Strictly more relevant
to a backend interview than VDBE trivia.


### 1.3 Dropped outright


| Cut | Why |
|---|---|
| WAL, checkpointing, WAL index, shared-memory coordination | 25h+. The undo journal already earns you the full "atomic commit + crash recovery" story. WAL adds a second durability mechanism, not a second talking point. |
| Multi-process locking via `fcntl` | You asked for threads. Threads cost a fraction as much and cover the goal. |
| Pointer maps, auto-vacuum, incremental vacuum | Pure implementation cost, zero interview value. Nobody will ask. The header's `largest_root_page` / `incremental_vacuum` fields must be written as 0, and a file with them set is **refused** — walking it while ignoring pointer maps would read them as b-tree pages. |
| Attached databases, temporary databases | Same. |
| `WITHOUT ROWID`, `AUTOINCREMENT` | Compatibility trivia. |
| Configurable page sizes, reserved-bytes-per-page | Fix 4096 and 0 as constants — both are still *written* to the header, and a file declaring anything else is refused rather than misread. |
| UTF-16 / text-encoding variants | UTF-8 only, and the header says so (offset 56 = 1). A file declaring 2 or 3 is refused. |
| Full B-tree merge / redistribution / root contraction | ~10h for correctness you cannot demo. Delete frees empty pages to the freelist, so space is reused and it isn't a leak. Under-full pages are documented as a known limitation — and note this is an *implementation gap, not a format difference*: the files are valid, just less dense. An honest limitation section is *itself* a credibility signal. |
| Exact SQLite type affinity, exact error codes | Simple declared-type coercion, own exception hierarchy. |
| `mmap` optimization | Premature. |


### 1.4 Added, because v1 was missing high-ROI items


| Added | Why it earns its hours |
|---|---|
| **LRU buffer pool from week 1** | v1 deliberately delayed this. That's the wrong call: retrofitting a cache touches every call site in the pager and B-tree. And "bounded buffer pool with pin counts, dirty tracking, and LRU eviction" is a top-3 interview talking point that costs ~90 lines. Build it once, up front. |
| **`EXPLAIN`** | ~2h. Nothing makes a database look more real in a demo. |
| **Deadlock detection** (wait-for graph, cycle detect) | ~50 lines on top of the lock manager. Turns "I used a lock" into a genuine concurrency-control conversation. |
| **Compiled-plan cache** | Keyed by SQL string. ~1h, and it's what "prepared statements" actually means internally. |
| **In-memory databases** (`connect(":memory:")`) | ~1h once the pager is abstract. Makes the whole test suite fast, which compounds over 8 weeks. |
| **CI + README + demo GIF** | v1 treated presentation as a week-8 afterthought. For a *GitHub* project this is 30% of the total ROI. A stranger decides in 40 seconds from the README whether you're worth talking to. |
| **Vertical slice by week 3** | See below. This is the biggest structural change. |


### 1.5 Restructured: vertical slices, not horizontal layers


v1 was strictly bottom-up — file format, then records, then leaf pages, then interior pages, then
writes. Nothing is runnable until week 8. On a hard deadline that's the wrong risk profile: any
slip anywhere means you show up with no working database at all.


This plan gets **`CREATE TABLE` / `INSERT` / `SELECT` running end-to-end at week 3**, then deepens.
By week 3 you have something demoable, and every week after that improves a working system rather
than approaching a first runnable state. If life happens in week 6, you still have a real project.


---


## 2. Locked Decisions


| Area | Decision |
|---|---|
| Package name | `quilldb` (cosmetic — rename the `src/` dir anytime) |
| Language | Pure Python 3.11+, stdlib only at runtime |
| File format | Own format, SQLite-inspired. Documented in `docs/file-format.md` |
| Page size | Fixed 4096 bytes |
| Text encoding | UTF-8 only |
| Storage | Slotted pages, B+tree, rowid-keyed tables, secondary index B+trees |
| Buffer pool | Bounded LRU with pin counts and dirty writeback, from day 1 |
| Executor | Volcano iterator pipeline |
| Parser | Handwritten tokenizer + recursive-descent |
| Planner | Rule-based index selection |
| Durability | Undo (rollback) journal, fsync-ordered, hot-journal recovery |
| Concurrency | Multiple connections, one process, threads. Table-level 2PL, single writer, deadlock detection, busy timeout |
| Public API | DB-API 2.0-inspired |
| Testing | pytest, Hypothesis, differential vs `sqlite3`, crash injection, threaded stress |
| Typing | Full annotations, `mypy --strict` in CI |


### Allowed runtime dependencies


Python standard library only: `struct`, `os`, `io`, `threading`, `dataclasses`, `enum`, `typing`,
`memoryview`, `collections`, `contextlib`.


### Prohibited in the engine


Python's `sqlite3`, any SQL parser library, parser generators, third-party B-tree or storage
libraries, ORMs, serialization frameworks (`pickle`, `msgpack`) in place of the record codec.


### Allowed for development and testing


`pytest`, `pytest-cov`, Hypothesis, `sqlite3` **as a behavioral oracle only**, ruff, mypy,
`cProfile`/`py-spy`.


---


## 3. Architecture


```
        quilldb.connect(...)              api/       DB-API-ish surface
                 │
        SQL text │
                 ▼
    tokenizer → parser → AST             sql/       handwritten, recursive descent
                 │
                 ▼
         binder + catalog lookup         catalog/   schema stored in a B+tree
                 │
                 ▼
          rule-based planner             plan/      index selection, EXPLAIN
                 │
                 ▼
    operator tree: SeqScan/IndexScan/    exec/      Volcano iterators, next()
    Filter/Project/NestedLoopJoin/
    HashAgg/Sort/Limit/Insert/Update/Delete
                 │
                 ▼
      TableCursor / IndexCursor          btree/     B+tree: search, scan, insert, delete, split
                 │
                 ▼
       transaction + undo journal        txn/       atomicity, rollback, recovery, lock manager
                 │
                 ▼
      buffer pool (LRU, pinned)          storage/   bounded cache, dirty writeback
                 │
                 ▼
      pager → slotted pages → file       storage/   4096B pages, freelist, overflow chains
```


Two rules that keep this from turning into mud:


1. **Every mutation goes through a transaction object**, from week 1 — even before the journal
   exists. Week 5 then replaces the journal internals without touching the B-tree.
2. **Layers only call downward.** The B-tree never knows about SQL; the pager never knows about
   B-trees.


---


## 4. The Eight Weeks


~14 hours per week. Each week ends with something runnable, tested, and committed.


Each week below has four parts: **why it exists** (what breaks without it), **role in SQLite** (the
real counterpart, so you're learning a system rather than inventing one), **what you build**, and a
concrete **success state** you can check off.


### Component map


You're writing SQLite's own on-disk format, so every component has a SQLite counterpart *and* reads
the same bytes. Knowing the names is worth real interview points — it's the difference between
"I made a page cache" and "this is what SQLite calls the pcache, bounded by `PRAGMA cache_size`."


| Week | You build | SQLite counterpart |
|---|---|---|
| 1 | `Pager`, `BufferPool`, record codec | `pager.c`, `pcache.c`/`pcache1.c`, `util.c` varints, `vdbeaux.c` serial types |
| 2 | B+tree, cursors, splits | `btree.c` — `BtCursor`, `balance()`, `balance_nonroot()`, `balance_deeper()` |
| 3 | Tokenizer, parser, catalog, executor | `tokenize.c`, `parse.y` (Lemon), `sqlite_schema`, `vdbe.c` |
| 4 | Index B+trees, planner, `EXPLAIN` | `btree.c` again, `where.c`, `EXPLAIN QUERY PLAN` |
| 5 | Undo journal, recovery | `pager.c` — `<db>-journal`, hot-journal detection |
| 6 | Lock manager, threaded connections | `pager.c` + VFS `xLock`, the SHARED/RESERVED/PENDING/EXCLUSIVE ladder |
| 7 | Joins, aggregation, sort | `where.c` join ordering, `vdbesort.c`, `AggStep`/`AggFinal` |
| 8 | README, docs, benchmarks, CI | `testing.html`, the SQLite docs site, `shell.c` |


---


### Week 1 — Pager, Buffer Pool, Record Codec


**Goal:** a byte-level storage foundation with a bounded page cache, and tuples that round-trip
to disk.


**Why this week exists.** Everything above this layer assumes "give me page 47" always works. If the
pager is subtly wrong, every bug for the next seven weeks will *look* like a B-tree bug — and you'll
debug the wrong file. The record codec defines what a row physically *is*: it's the boundary between
bytes and data, and it's the one component you cannot refactor later without regenerating every test
fixture you own.


The buffer pool goes in now, not later, because it's the seam between logical page numbers and
physical I/O. Retrofitting it means touching every call site in the pager *and* the B-tree.


**Role in SQLite.** The pager is the abstraction that hides the file from everything above it —
SQLite's `btree.c` never calls `read()`, it asks the pager for numbered pages. That single
indirection is what later lets the pager add journaling (your week 5) and locking (week 6) without
the B-tree noticing. SQLite's page cache is `pcache.c`/`pcache1.c`, bounded by `PRAGMA cache_size`.
Varints are `sqlite3PutVarint`/`sqlite3GetVarint` in `util.c`; serial-type encoding lives in
`vdbeaux.c`. The 100-byte file header is simply the first 100 bytes of page 1.


| Task | Hours |
|---|---|
| Repo skeleton, `pyproject.toml`, ruff + mypy + pytest config, CI green on day 1 | 1.5 |
| `errors.py` exception hierarchy | 0.5 |
| 100-byte SQLite file header: all 22 fields, `to_bytes`/`from_bytes`, plus `check_supported()` deciding refuse-vs-tolerate per field | 2 |
| `Pager`: open/create, `read_page`, `write_page`, `allocate_page`, `free_page`, freelist as **trunk pages** batching ~120 leaf page numbers | 3.5 |
| `SlottedPage`: 8/12-byte header by type, page-1's offset-100 case, cell pointer array, cell content area, insert/delete/get cell, repack | 3.5 |
| `BufferPool`: LRU, `pin`/`unpin`, dirty flags, `flush_all`, eviction refuses pinned pages | 2 |
| Varint codec: unsigned, 1–9 bytes with the ninth-byte special case, + Hypothesis round-trip tests | 1 |
| Record codec: serial-type header + body, NULL/INT/REAL/TEXT/BLOB, two's-complement width buckets | 2 |
| CLI `quilldb inspect file.db` | 0.5 |


**Page layout** (not yours to design — this is SQLite's, from fileformat2.html):


```
byte  0  page type       (1)   2 = interior index, 5 = interior table,
                               10 = leaf index,   13 = leaf table
byte  1  first freeblock (2)   big-endian u16, 0 = none
byte  3  cell count      (2)   big-endian u16
byte  5  content start   (2)   offset where cell content begins; 0 means 65536
byte  7  fragment bytes  (1)   count of 1-3 byte holes, never above 60
byte  8  right child     (4)   INTERIOR PAGES ONLY — absent on leaves
         cell pointers   (2 × cell count, ascending key order)
         ... free space ...
         ... cell content, growing down from end of page ...
```


**8 bytes on a leaf, 12 on an interior page.** The header size depends on the type byte you just
read, so there is no single `PAGE_HEADER_SIZE`; put it behind `header_size(page_type)` and never
inline it. See chapter 02 §2.5 for what each field buys and why the asymmetry is worth its branch.


**The one special case, and it will bite you:** page 1 carries the 100-byte file header in front of
its page header, so page 1's page header starts at **byte 100** and page 1 has **100 fewer usable
bytes** than every other page. Put that in one function too:


```python
def page_header_offset(page_id: int) -> int:
    return FILE_HEADER_SIZE if page_id == SCHEMA_ROOT_PAGE else 0
```


Get the free-space arithmetic wrong on page 1 and you overwrite the magic bytes, making the file
unopenable by you *and* by `sqlite3`. Write a test that fills page 1 and then reopens the database.


**Why SQLite accepted that cost** — worth an ADR, and the answer is better than "they saved a page":
there is no "where does the catalog live" field in the header at all, because the schema b-tree is
*always* rooted at page 1. A hardcoded constant cannot be corrupt, stale, or unjournalled. The
cheapest way to guarantee a field is correct is not to have the field. Chapter 01 §1.6 works through
it, including the two concrete costs — `hdrOffset = pgno==1 ? 100 : 0` in `btree.c`, and an exception
carved into the format's own minimum-fanout invariant.


**Success state.**


- **`sqlite3 demo.db "PRAGMA integrity_check"` prints `ok` on a database quilldb created.** Get this
  green in session 3, on a one-page empty file, before writing any more storage code — it is the
  cheapest and strictest test in the project and everything after it gets checked for free.
- `quilldb inspect demo.db` prints correct header fields for a file you created — cross-check them
  against `sqlite3 demo.db ".dbinfo"`, which prints the same fields from the same bytes
- Hypothesis: any tuple of NULL/int/float/str/bytes round-trips tuple → page → disk → tuple
  identically, including boundary values (every varint length including the 8→9 byte transition at
  2⁵⁶, ±2^63, empty string, empty blob, multi-byte UTF-8)
- A buffer pool with capacity 8 handles a 200-page workload: it evicts, never loses a dirty page,
  and refuses to evict a pinned one
- Truncated files, bad magic, and illegal page sizes raise *your* typed errors — never a bare
  `struct.error` or `IndexError`. A file declaring WAL mode, UTF-16, non-zero reserved space, or
  auto-vacuum is **refused** with a clear message rather than misread
- A page can be allocated, freed, and reallocated; the freed page is reused rather than the file
  growing, and freeing 200 pages writes ~2 trunk pages rather than 200 pages
- Filling page 1 to capacity and reopening the database still works — the file header survives
- `mypy --strict` and `ruff` clean; CI green


---


### Week 2 — B+Tree: Search, Scan, Insert, Split


**Goal:** the data structure the whole project rests on, plus a validator that proves it's correct.


**Why this week exists.** The B+tree is the entire reason a database beats a flat file: O(log n)
point lookup *and* ordered range scan from one structure, with everything living on disk in
page-sized units so you only pay for the pages you touch. Splits are where it gets hard — a leaf
split touches three pages and can cascade all the way to the root, growing the tree's height. That
makes it also where corruption enters.


Build the validator *before* you need it. Split bugs are invisible at the point of failure: you
write a plausible-looking page, and find out 200 inserts later when a scan silently skips a range.
The validator converts that into an immediate, precise failure.


**Role in SQLite.** `btree.c`, one of the two hardest files in the project. Every table *and* every
index in a SQLite database is a B-tree in the same file — and `sqlite_schema` (formerly
`sqlite_master`) is itself a table B-tree rooted at page 1, which is why SQLite needs no separate
catalog format. Table B-trees are keyed by 64-bit rowid with payload only in the leaves, making them
B+trees. Cursors are `BtCursor` with a path stack, same as yours. The split machinery is `balance()`
dispatching to `balance_nonroot()`, `balance_deeper()` (height growth), and `balance_quick()` (the
append-optimized fast path).


One deliberate divergence: SQLite rebalances across *three* sibling pages, not two, to reduce split
frequency and improve page fill. You're doing the simple two-way split. That's a good "what would
you do differently with more time" answer — and knowing the tradeoff exists is the point.


| Task | Hours |
|---|---|
| Leaf and interior cell formats; overflow chains for oversized payloads | 2 |
| `BTree.search(key)`, binary search within page, descent | 2 |
| `TableCursor`: `first`, `last`, `seek`, `next`, `prev`, `rowid`, `record` — cursor holds a path stack | 2 |
| `insert`: sorted placement, page repack | 1.5 |
| Leaf split, separator promotion | 2 |
| Recursive interior split + root split (height growth) | 2.5 |
| `validate_btree()`: key order, child key ranges, page types, reachability, no duplicate page ownership, no overflow cycles, uniform height, cell bounds | 2 |


**Keep the validator as a first-class feature, exactly as v1 said.** It's cheap, it will save you
many hours of debugging in weeks 4–6, and it's a real signal: it says you think about invariants,
not just code paths.


**Success state.**


- 100k random-order inserts produce a tree the validator reports clean
- Full scan returns keys in sorted order; `scan() == sorted(set(keys))` as a property test over
  random insert multisets
- Point lookups match a Python `dict` oracle — for present keys *and* absent ones
- Tree height grows as rows accumulate, and is uniform across every leaf (the validator checks this)
- A 10KB value round-trips through an overflow chain
- Closing and reopening the file mid-run changes nothing about subsequent reads
- Sorted-ascending, sorted-descending, and random insertion orders all produce valid trees
- Corrupt fixtures (cell pointer out of bounds, overflow cycle, unsorted keys) raise typed errors
  rather than hanging or recursing forever


---


### Week 3 — Vertical Slice: SQL That Actually Runs ⭐


**Goal:** the milestone that de-risks the whole project. `import quilldb` and use it.


**Why this week exists.** Until SQL runs, you have a data structure, not a database — nothing to show
anyone, no user-facing surface, and no evidence your layering is right. Building the thinnest
possible path from SQL text down to a B-tree cursor *now* proves the seams work while they're still
cheap to move. After this week, every remaining week improves a working system instead of approaching
a first runnable state. That's the difference between a project that survives a bad fortnight and one
that doesn't.


**Role in SQLite.** The frontend pipeline is `tokenize.c` → a parser generated by the **Lemon**
parser generator from `parse.y` → code generation spread across `select.c`, `insert.c`, `update.c`,
`delete.c`, `where.c` → VDBE bytecode → the `vdbe.c` interpreter. A `sqlite3_stmt` — what the C API
calls a prepared statement — *is* a compiled VDBE program.


One divergence worth an ADR, and one thing the format decides for you:


1. **SQLite generates its parser; you're handwriting one.** Lemon gives them a maintainable grammar
   for the full SQL surface. You get better learning and no build step, at the cost of a narrower
   grammar. Both are correct for their context — and note this is purely an implementation choice,
   invisible in the file.
2. **The catalog is `sqlite_schema`, and its shape is not yours to pick.** It's an ordinary table
   b-tree rooted at page 1, holding one row per schema object with exactly five columns:


   ```
   type      TEXT     "table" | "index" | "view" | "trigger"
   name      TEXT     the object's name
   tbl_name  TEXT     the table it belongs to (same as name, for tables)
   rootpage  INTEGER  page number of this object's b-tree root; 0 for views/triggers
   sql       TEXT     the original CREATE statement, verbatim
   ```


   So you store the **raw `CREATE TABLE` text** and re-parse it on open — SQLite's choice, and now
   yours, because `sqlite3` reads that column to learn your schema. Storing structured metadata
   instead would be simpler and open faster, and would make your file unreadable by the tool your
   acceptance test depends on.


   **Understand why SQLite chose it, because it's a good answer and it's now your answer too:** the
   text is the source of truth, so a *newer* library can reinterpret an *older* schema — new keywords,
   new defaults, new constraint semantics — without a migration. Structured metadata freezes your
   interpretation at write time. **The more awkward representation is the one that survives version
   skew**, which is the same argument as `user_version` and the read/write version split in the file
   header (chapter 01 §1.6). Costs you a re-parse per open, which is microseconds.


   One consequence to get right: `rootpage` is why chapter 06 §6.4 insists a table's root page number
   never changes. If it moved on every root split, every split would have to rewrite a catalog row.


| Task | Hours |
|---|---|
| Tokenizer: keywords, identifiers, string/numeric literals, operators, `?` params, comments | 1.5 |
| AST dataclasses | 1 |
| Recursive-descent parser: `CREATE TABLE`, `INSERT`, `SELECT` (single table, `WHERE`) | 3 |
| Expression parsing with precedence climbing: comparisons, `AND`/`OR`/`NOT`, arithmetic, `IS NULL`, `LIKE` | 2 |
| Catalog: `sqlite_schema` rows (type, name, tbl_name, rootpage, sql) in the b-tree rooted at page 1, re-parsed on open | 2.5 |
| Binder: resolve names → column indexes, type-check, raise on unknown table/column | 1.5 |
| Operators `SeqScan`, `Filter`, `Project`, `Insert`; expression evaluator | 2 |
| Public API: `connect`, `Connection.execute`, `Cursor.fetchone/fetchall/fetchmany`, `description`, param binding, `:memory:` | 1.5 |


**Success state.** This runs:


```python
db = quilldb.connect("demo.db")
db.execute("CREATE TABLE users (id INTEGER, name TEXT, age INTEGER)")
db.execute("INSERT INTO users VALUES (?, ?, ?)", (1, "ada", 36))
assert db.execute("SELECT name FROM users WHERE age > 30").fetchall() == [("ada",)]
```


Plus:


- `CREATE TABLE` survives a close and reopen — the catalog is genuinely durable, not in-memory
- Unknown table or column raises a typed error at bind time, before any execution
- `?` parameters bind positionally and are type-checked against declared column types
- `connect(":memory:")` works and the test suite uses it
- `fetchone` / `fetchall` / `fetchmany` and `.description` behave the way a DB-API user expects
- The differential harness is green on ~20 hand-written SQL scripts run against both engines
- A `SELECT` on an empty table returns `[]` rather than raising


**Start differential testing here.** A ~40-line harness that runs a SQL script against both
`sqlite3` and quilldb and diffs the result sets. From this week on, every feature you add gets
tested against a real database for free.


> This is the week that matters most. Protect it. If week 2 runs long, cut the B-tree validator's
> nice-to-haves and interior-split polish rather than slipping week 3.


---


### Week 4 — Mutation, Indexes, and a Planner


**Goal:** a database you can actually keep data in, that gets *fast* when you add an index.


**Why this week exists.** Two distinct wins. `DELETE`/`UPDATE` turn the engine from an append-only
log into a database you can keep real data in. Indexes give you the single most legible performance
story in the project — and index selection is the most *transferable* skill here, because choosing
and reasoning about indexes is what backend engineers do with real databases every week. An
interviewer who does nothing else with databases still does this.


It's also your first encounter with *derived* data that must stay consistent with a base table. Every
insert, update, and delete now has to maintain N index trees atomically. That's a genuine correctness
problem, and it's why week 5 comes next.


**Role in SQLite.** Index B-trees use the *same* `btree.c` code, keyed by
`(indexed columns…, rowid)` with no separate payload. So an index lookup gets you a rowid, and then
a *second* B-tree search fetches the actual row — which is precisely why covering indexes are ~2×
faster: one search instead of two. The planner is `where.c`, roughly 10k lines, and it's
**cost-based**, consuming statistics that `ANALYZE` writes into `sqlite_stat1`. Yours is rule-based,
which is the right call at this scale and an honest thing to say.


SQLite's index-usability rule is documented in `optoverview.html` §2 and is exactly what you should
implement: leading columns need `=`, `IN`, or `IS`; the right-most used column may take inequalities;
**no gaps allowed**. There's a worked table of which `WHERE` clauses can use `ex1(a,b,c,d,…)` — steal
its logic directly.


| Task | Hours |
|---|---|
| `DELETE` + `UPDATE` parsing, `Delete`/`Update` operators | 2 |
| B+tree delete: remove cell, repack, free emptied pages to the freelist (no merge — documented) | 2 |
| Index B+trees: key = `(indexed values..., rowid)`, lexicographic comparison | 2.5 |
| `CREATE INDEX`, `CREATE UNIQUE INDEX`, uniqueness violation errors | 1.5 |
| Index maintenance on insert/update/delete | 1.5 |
| `IndexScan` operator: equality and range seeks | 1.5 |
| Rule-based planner: choose IndexScan when a `WHERE` conjunct is an equality or range on an indexed leading column, else SeqScan | 2 |
| `EXPLAIN`: pretty-print the operator tree with estimated cost | 1 |


**Success state.**


```
>>> print(db.execute("EXPLAIN SELECT * FROM users WHERE email = 'a@b.c'").explain())
Project  [id, email, age]
└─ IndexScan  idx_email  (email = 'a@b.c')   est_rows=1  pages_read=3
```


- `EXPLAIN` prints the operator tree; an index-eligible `WHERE` picks `IndexScan`, an ineligible one
  (leading column absent, or `OR`-connected) correctly falls back to `SeqScan`
- Benchmark: indexed equality lookup does ~3 page reads against ~2,400 for the scan.
  **This number goes in the README** — a concrete before/after is the most persuasive artifact you
  will produce all project
- A `UNIQUE` violation raises *before* any page is written — no half-applied insert
- Indexes stay consistent through insert/update/delete, verified by comparing a full index scan
  against a full table scan plus filter
- Deleted pages return to the freelist and are reused: `DELETE` then `INSERT` of the same volume
  leaves the file the same size
- The validator is clean on table trees *and* index trees
- Differential tests still green — index presence must never change query *results*, only speed
  (a good property test: run every query with and without the index, assert identical output)


---


### Week 5 — Transactions and Crash Recovery ⭐⭐


**Goal:** the strongest engineering signal in the project. This is where you separate yourself from
every "I built a toy database" repo on GitHub.


**Why this week exists.** This is the difference between a database and a file format. Consider what
week 2 built: a B+tree split writes three or more pages. Crash between them and you don't lose a
row — you lose the *tree*. The file becomes unreadable, not merely incomplete. Atomicity isn't a
feature layered on top of your B-tree; it's what makes the B-tree safe to use at all.


Week 4 made this worse, not better: one `INSERT` now writes a table page plus N index pages, and any
partial application leaves indexes disagreeing with their table. There is no way to fix that after
the fact. It has to be atomic.


The crash matrix matters because it *proves* the property instead of asserting it. "I implemented a
journal" is a claim. "I crash at every write boundary and verify all-or-nothing at each one" is
evidence — and evidence is what interviews reward.


**Role in SQLite.** Also `pager.c`. The rollback journal is a sidecar file `<db>-journal` containing
the **original** content of every page about to be modified, plus a header recording the original
database size (so recovery can truncate a file that grew). The central insight, straight from
`atomiccommit.html` §3:


> **The commit point is deleting the journal.** Not writing the pages — *deleting the journal.*


That works because "does this file exist?" is a question the OS answers atomically, and it's the one
atomic primitive SQLite can rely on. Before that instant, recovery rolls the database back; after it,
the new state is durable. Hot-journal detection on open is what makes recovery automatic rather than
something the user has to invoke.


Two details worth copying exactly: the journal's page count starts at **zero** and is only written
after the body is synced, so a torn journal rolls back nothing; and each journaled page carries a
32-bit checksum. `PRAGMA synchronous=FULL` does two fsyncs on the journal, `NORMAL` does one — and
the checksums are precisely what makes `NORMAL` survivable.


| Task | Hours |
|---|---|
| `Transaction`: dirty page set, page versioning, `commit`/`rollback` | 2 |
| Undo journal: header, original-page records, per-record checksum | 2.5 |
| Correct fsync ordering: write journal → fsync journal → write pages → fsync db → delete journal | 1.5 |
| `BEGIN` / `COMMIT` / `ROLLBACK` + `with db.transaction():` + autocommit | 1.5 |
| Hot-journal detection and replay on open | 2 |
| **Fault injection framework**: a file wrapper that raises `SimulatedCrash` at write #N or fsync #N | 2 |
| **Exhaustive crash matrix**: for every fault point in every scenario — crash, reopen, assert the DB is *entirely* pre-commit or *entirely* post-commit, and the validator is clean | 2.5 |


The crash matrix is the highest-value four hours in this roadmap. The test is a loop:


```python
@pytest.mark.parametrize("crash_at", range(1, MAX_WRITES))
def test_atomic_at_every_crash_point(tmp_path, crash_at):
    setup_known_state(tmp_path)
    with pytest.raises(SimulatedCrash):
        run_transaction(tmp_path, fail_at_write=crash_at)
    db = quilldb.connect(tmp_path / "db")        # triggers recovery
    assert read_state(db) in (STATE_BEFORE, STATE_AFTER)   # never in between
    assert validate_all_btrees(db).is_valid
```


A few hundred generated test cases proving atomicity holds at *every* physical write boundary. Very
few candidate projects have this, and it's the kind of thing an interviewer will want to dig into
for ten minutes — which is exactly what you want.


**Success state.**


- The crash matrix is green: parametrized over every write *and* fsync boundary, each reopen yields
  *entirely* the pre-transaction state or *entirely* the post-transaction state, never a mixture,
  with a clean validator every time
- **Compound failure**: crashing *during recovery* from a previous crash still recovers correctly
  (SQLite tests this explicitly — it's the nastiest case and a great thing to have)
- `ROLLBACK` restores byte-identical file content: hash the file before `BEGIN`, hash after
  `ROLLBACK`, assert equal
- The journal is deleted on successful commit and absent afterward
- A journal with one flipped byte is caught by its checksum and refused, not replayed into the
  database
- A journal whose header page-count is still zero rolls back nothing, harmlessly
- Indexes and tables never disagree after any crash point — verify with the week-4 index-vs-scan
  consistency check inside the crash loop
- `docs/durability.md` names the commit point precisely and states the non-guarantees: a lying
  `fsync`, torn sectors on non-atomic hardware, and whatever you decide about directory fsync


---


### Week 6 — Multi-Threaded Concurrency ⭐


**Goal:** your stated end goal — several threads using the database at once, safely.


**Why this week exists.** It's the feature you actually asked for, and it's the area backend
interviews probe hardest after "can you write SQL." Concurrency is also where correctness turns
subtle in ways single-threaded code never is: two threads splitting the same page, a reader
traversing a tree while a writer rebalances it, a cursor holding a pinned page while eviction wants
to reclaim it. Every one of those is a real bug you'll have to reason about rather than test your way
out of. After the crash matrix, this is the strongest signal in the project.


**Role in SQLite.** SQLite locks at the **whole-file** level in rollback-journal mode, via the ladder
UNLOCKED → SHARED → RESERVED → PENDING → EXCLUSIVE (documented in `lockingv3.html`, implemented in
`pager.c` plus the VFS `xLock`). One writer at a time, and in rollback mode a writer blocks all
readers. `PENDING` exists for one specific reason worth understanding: it lets existing readers drain
while blocking *new* ones, which prevents writer starvation. Contention is handled by
`sqlite3_busy_timeout` retrying, and SQLite **avoids** deadlock through strict lock ordering rather
than detecting cycles.


Your divergence here is genuinely in your favor, and you should say so out loud: table-level locks
are **finer-grained than real SQLite's rollback-journal mode**. But say *why* you can afford it —
you're single-process with shared memory, so coordination is cheap, whereas SQLite coordinates
across processes through the filesystem, where fine-grained locking would be prohibitively
expensive. Demonstrating that you understand the constraint behind someone else's coarser choice is
worth far more than claiming you beat them.


Similarly, you're adding deadlock *detection* where SQLite does deadlock *avoidance*. Know both, and
know that detection buys concurrency at the cost of aborted transactions.


| Task | Hours |
|---|---|
| Thread-safe buffer pool: per-page latches, pin counts under lock, safe eviction | 2.5 |
| `Database` (shared: file, pool, lock manager) vs `Connection` (per-thread: txn state, cursors) | 2 |
| `LockManager`: shared/exclusive table locks, condition-variable waiting, single writer | 2.5 |
| Two-phase locking: acquire during execution, release at commit/rollback | 1.5 |
| **Deadlock detection**: wait-for graph + cycle detection, abort the youngest victim with `DeadlockError` | 2 |
| `busy_timeout`, lock wait timeouts, retry helper | 1 |
| **Threaded stress test**: N threads × random transactions; assert no lost updates, invariants hold, no deadlock hangs, validator clean | 2.5 |


**Be precise about your isolation level and say it out loud in the docs.** Table-level 2PL with a
single writer gives you serializable-for-writers, read-committed-or-better for readers, depending
on how you release read locks. Pick one, document it, and know why. Interviewers respect "I
implemented read-committed and here's exactly why it isn't repeatable-read" *far* more than a vague
claim of ACID.


**Success state.**


```python
# 8 threads, 10k transactions, transfers between accounts
# invariant: sum(balances) is unchanged, always
```


- The transfer stress test passes in CI, repeatedly, without flaking
- No lost updates: the final state equals *some* serial execution of the same operations
- A deliberately constructed deadlock is detected, one victim aborts with `DeadlockError`, and the
  other transaction commits — the process never hangs
- `busy_timeout` expires and returns an error rather than blocking forever
- Readers observe consistent state per your documented isolation level, and
  `docs/concurrency.md` **names that level and lists the anomalies it permits**
- A cursor's pinned pages are never evicted out from under it, under contention
- The validator is clean after the full stress run
- `benchmarks/concurrent.py` produces a throughput-vs-thread-count table for the README


On that last point: be honest that the GIL caps write throughput. The interesting results are
*correctness under contention* and reader behavior — presenting those clearly beats pretending
Python scales writes linearly, and an interviewer will trust everything else you say more for it.


---


### Week 7 — Joins, Aggregation, Ordering


**Goal:** make it feel like a database rather than a key-value store with SQL syntax. This is what
makes the demo land with non-specialists.


**Why this week exists.** Legibility. "Can it do a JOIN?" is the first question a non-specialist
asks, and joins are the reason relational databases exist at all rather than key-value stores —
they're what lets you normalize data and recombine it at query time instead of duplicating it.


This week is also a *test of your week-3 design*. If the iterator abstraction is right, a join is
just an operator with two children and aggregation is an operator that drains its child before
emitting. If adding a join turns out to be painful, that's real information: it means the executor
interface leaked something it shouldn't have. Either outcome is useful.


**Role in SQLite.** SQLite uses **nested loop joins exclusively** — no hash join, no sort-merge, by
deliberate design. Instead it builds "automatic query-time indexes," which `optoverview.html` §14.1
describes as "almost the same thing as a hash join," just using the existing B-tree rather than
adding a hash table, specifically to avoid growing the library for embedded targets. That's a
wonderful example of an engineering constraint driving an algorithmic choice, and it's worth citing.


Join *ordering* is chosen by a polynomial-time graph algorithm in `where.c` — fast enough to plan
50-way joins in microseconds. The docs' `node`/`edge` example shows one order beating another by
roughly **2000×**, which is the clearest possible illustration of why ordering matters. Sorting is
`vdbesort.c`, an external merge sort that spills to disk; yours is in-memory with a documented
limit. Aggregation runs through `AggStep`/`AggFinal` opcodes over either sorted or hashed grouping.


| Task | Hours |
|---|---|
| Parse `JOIN ... ON`, comma joins, table aliases, qualified column names | 2 |
| `NestedLoopJoin` + `IndexNestedLoopJoin` when the inner side has a usable index | 2.5 |
| `HashAggregate`: `COUNT`, `SUM`, `AVG`, `MIN`, `MAX`, with and without `GROUP BY` | 2.5 |
| `Sort` (in-memory, documented row limit), `ORDER BY` multi-key ASC/DESC | 1.5 |
| `Limit` / `OFFSET`, pushed into the operator tree | 1 |
| `HAVING`, `DISTINCT` | 1.5 |
| Planner: join ordering heuristic (smaller table outer), index-join selection, `EXPLAIN` for all of it | 1.5 |
| Compiled-plan cache keyed by SQL text | 1 |


**Success state.** This works, and `EXPLAIN` shows a sensible plan:


```sql
SELECT u.age, COUNT(*), AVG(o.total)
FROM users u JOIN orders o ON o.user_id = u.id
WHERE u.age > 25
GROUP BY u.age
HAVING COUNT(*) > 5
ORDER BY 2 DESC
LIMIT 10;
```


Plus:


- `EXPLAIN` shows `IndexNestedLoopJoin` when the inner relation has a usable index, plain
  `NestedLoopJoin` when it doesn't
- The join-order heuristic demonstrably picks the cheaper order on a deliberately asymmetric pair of
  tables — and you can show the page-read counts for both orders
- Differential tests green on the `NULL` cases specifically: `COUNT(*)` vs `COUNT(col)` with NULLs
  present, `AVG` skipping NULLs, `NULL` sort position in `ORDER BY`, `NULL` as a `GROUP BY` key,
  aggregates over zero rows (`SUM` → NULL but `COUNT` → 0)
- `Sort` documents its in-memory row limit and raises a clear error past it rather than exhausting
  memory
- `LIMIT` short-circuits: a `LIMIT 1` over a million-row table doesn't scan a million rows, and you
  can prove it with the page-read counter
- Multi-key `ORDER BY` with mixed `ASC`/`DESC` is correct


`NULL` semantics are where your bugs will be. Three-valued logic deserves twenty minutes of
deliberate thought before you implement comparison — `NULL = NULL` is not true, and that propagates
further than you'd expect.


---


### Week 8 — Presentation (the ROI multiplier)


**Goal:** convert 100 hours of engineering into something a stranger values in 40 seconds. Do not
skimp on this week. A great project with a bad README reads as a mediocre project.


**Why this week exists.** Because 100 hours of engineering nobody can evaluate is worth less than 80
hours a stranger understands immediately. This isn't polish — it's the conversion step, and for your
actual goal it's the highest-ROI week in the plan. A recruiter spends 40 seconds on your GitHub. A
hiring manager spends three minutes. Neither will read `btree.py`.


**Role in SQLite.** Worth internalizing, because it's the same lesson at industrial scale:
**SQLite's documentation and test suite are as responsible for its ubiquity as its code is.** As of
3.42.0 the project ships 590× more test code than library code, across four independent harnesses.
Its docs site is something engineers cite as a reference work. And `testing.html` exists purely to
explain, in public, how the project earns trust.


Nobody chooses an embedded database because its B-tree is elegant — they choose it because the
project makes its reliability *legible*. That's exactly the function your README, docs, ADRs, and
test-count table perform at small scale. `shell.c` (the `sqlite3` CLI) plays the same role your
`quilldb shell` does: it's how people find out the thing is real.


| Task | Hours |
|---|---|
| **README**: pitch, install, 20-line usage example, architecture diagram, feature table, benchmark table, explicit "not implemented" section, how to run tests | 3 |
| **Demo GIF/asciinema** embedded at the top of the README | 1.5 |
| `docs/architecture.md`, `docs/file-format.md`, `docs/durability.md`, `docs/concurrency.md` | 2.5 |
| `docs/decisions/` — 6–8 short ADRs (why undo journal not WAL, why iterators not bytecode, why no page merge, why SQLite's format exactly but only one direction, isolation level choice) | 1.5 |
| Benchmark harness + results table: point lookup, scan, insert throughput (sorted vs random), index vs no index, buffer-pool hit rate, B+tree height vs row count, threads vs throughput | 2 |
| CI matrix: pytest + coverage + mypy strict + ruff on 3.11/3.12/3.13; badges | 1 |
| Polish CLI: `inspect`, `pages`, `btree --root N`, `validate`, `bench`, `shell` (REPL) | 1.5 |
| 5-minute demo script, rehearsed out loud twice | 1 |


**README structure that works** (order matters — most people read only the first screen):


1. One-sentence pitch + badges
2. The GIF
3. `pip install -e .` + the 15-line usage example
4. "What's inside" — the architecture diagram
5. Feature table with ✅/❌
6. Benchmarks table
7. **"Not implemented / known limitations"** — counterintuitively one of the highest-signal
   sections in the whole repo. It proves you know the difference between a subset and a product,
   and it preempts the question "did you realize you didn't handle X?"
8. Testing story: counts by category, especially the crash matrix
9. Design decisions → link the ADRs


The `shell` REPL is worth the 45 minutes on its own. "Let me just show you" beats any explanation.


**Success state.**


- A stranger can clone, `pip install -e .`, paste the 15-line example, and have it work first try —
  test this on a clean checkout, in a fresh virtualenv
- The README leads with the GIF; the benchmark table has real measured numbers; the limitations
  section is specific rather than apologetic
- CI badge green across 3.11 / 3.12 / 3.13, with coverage reported
- 6–8 ADRs, each one a decision you can defend out loud without rereading it
- `quilldb shell` gives a working REPL you can drive in front of someone
- `docs/architecture.md` has a diagram matching the real module layout
- The test-count-by-category table is in the README, with the crash matrix called out
- You have delivered the 5-minute demo twice, out loud, from memory


---


## 5. Descope Ladder


You will fall behind somewhere; everyone does. Cut in this order, top first:


1. `HAVING`, `DISTINCT`, `OFFSET` (week 7)
2. `IndexNestedLoopJoin` — plain nested loop is fine, note it in the README
3. Deadlock *detection* → deadlock *avoidance* via timeout + retry (document the tradeoff)
4. Multi-column indexes → single-column only
5. Aggregation without `GROUP BY` only
6. `UPDATE` → implemented as delete+insert
7. Overflow pages → hard row-size limit with a clear error


**Never cut, in priority order:** the week-3 vertical slice, the week-5 crash matrix, the week-6
threaded stress test, the week-8 README. Those four are the project. Everything else is texture.


If you're a full week behind at week 6: skip week 7 entirely and go straight to presentation. A
polished database with no joins beats an unpolished one with joins, every time.


---


## 6. Repository Layout


Create each directory when its week begins, not before.


```
quilldb/
├── README.md                  ← 30% of the ROI lives here
├── pyproject.toml
├── .github/workflows/ci.yml
├── src/quilldb/
│   ├── __init__.py            connect()
│   ├── errors.py
│   ├── constants.py
│   ├── codec/                 varint.py, record.py, ints.py
│   ├── storage/               header.py, pager.py, page.py, bufferpool.py,
│   │                          overflow.py, freelist.py
│   ├── btree/                 btree.py, cells.py, cursor.py, index.py, validate.py
│   ├── txn/                   transaction.py, journal.py, recovery.py, locks.py
│   ├── catalog/               schema.py, catalog.py
│   ├── sql/                   tokens.py, tokenizer.py, ast.py, parser.py, binder.py
│   ├── plan/                  planner.py, explain.py
│   ├── exec/                  operators.py, expressions.py, aggregate.py, join.py
│   ├── api/                   connection.py, cursor.py
│   └── cli.py
├── tests/
│   ├── unit/  integration/  differential/  property/
│   ├── fault_injection/       ← the crown jewel
│   ├── concurrency/
│   └── corruption/
├── benchmarks/
├── docs/
│   ├── architecture.md  file-format.md  durability.md  concurrency.md
│   └── decisions/             ADR-001..008
└── examples/                  todo_app.py, kv_store.py
```


---


## 7. Test Strategy


| Category | What it covers | When |
|---|---|---|
| **Unit** | varints, records, slotted pages, cell parsing, comparison, split selection, expression eval | continuous |
| **Property** (Hypothesis) | codec round-trips, B+tree vs `dict`/`sorted()` oracle, random insert/delete sequences preserve invariants | W1–W4 |
| **Differential** vs `sqlite3` | same SQL to both engines, diff result sets. `NULL` semantics, type coercion, aggregate edge cases, ordering | W3 onward |
| **Integration** | open/create/reopen, full CRUD, index correctness, catalog persistence | W3 onward |
| **Corruption** | fixture corpus: bad magic, truncated page, bad page type, cell pointer out of bounds, overlapping cells, overflow cycle, unsorted keys, child out of range, inconsistent height → each must raise a *typed* error, never hang or crash the interpreter | W2, W8 |
| **Fault injection** | crash at every write/fsync boundary → atomicity holds, recovery clean | W5 |
| **Concurrency** | N-thread stress with invariant checks, lock-manager unit tests, deadlock scenarios, busy-timeout behavior | W6 |


Target ~80% coverage. Don't chase 100% — spend the marginal hour on the crash matrix instead.


One habit worth adopting from day 1: **never handle malformed external input with `assert`.** Every
untrusted byte gets a typed exception from `errors.py`. It's a small discipline that reads as
professional maturity.


---


## 8. Definition of Done


- [ ] `pip install -e .` then `import quilldb` works
- [ ] `CREATE TABLE`, `CREATE INDEX`, `INSERT`, `SELECT`, `UPDATE`, `DELETE`, `BEGIN`/`COMMIT`/`ROLLBACK`
- [ ] `WHERE`, `ORDER BY`, `LIMIT`, `GROUP BY`, aggregates, single `JOIN`
- [ ] Rule-based index selection, visible via `EXPLAIN`
- [ ] B+tree with splits, multi-level, cursors, overflow, structural validator
- [ ] Bounded LRU buffer pool with pin counts and dirty writeback
- [ ] Atomic commit + rollback + hot-journal recovery, proven by a crash matrix at every write boundary
- [ ] Multiple connections across threads, single writer, deadlock detection, busy timeout
- [ ] Threaded stress test proving invariants under contention, in CI
- [ ] CI green: pytest, coverage, `mypy --strict`, ruff on 3.11–3.13
- [ ] README with GIF, benchmarks, and an honest limitations section
- [ ] Architecture docs + ADRs
- [ ] A rehearsed 5-minute demo


---


## 9. Interview Assets


### Résumé line


> **quilldb** — Relational database engine in pure Python (~7k LOC). Paged storage with an LRU
> buffer pool, B+tree tables and secondary indexes, handwritten SQL parser, iterator-based query
> executor with rule-based index selection, ACID transactions via an undo journal with crash
> recovery verified by fault injection at every write boundary, and multi-threaded connections with
> two-phase locking and deadlock detection. 1,400 tests including differential testing against
> SQLite.


### The five stories you'll be asked for, and where each comes from


| Behavioral prompt | Your answer |
|---|---|
| "Hardest bug you've fixed" | A B+tree split that produced a valid-looking page but corrupted the tree two levels up. Explain how the structural validator turned an invisible corruption into a precise failure — and that you built the validator *before* you needed it. |
| "Tell me about a time you had to guarantee correctness" | The crash matrix. Walk through fsync ordering and the commit point. |
| "How do you handle concurrency" | The lock manager: shared/exclusive, 2PL, the wait-for graph, why you abort the youngest victim, why the GIL means the interesting metric is correctness under contention rather than raw throughput. |
| "Tell me about a tradeoff you made" | Writing SQLite's exact on-disk format but only in one direction — quilldb's files are readable and verifiable by the `sqlite3` CLI, but it doesn't read arbitrary SQLite files. Naming that boundary buys a free reference validator for ~10 hours instead of ~55 for full interop. Or skipping page merging: an implementation gap that produces valid-but-less-dense files, documented rather than hidden. |
| "How do you test something with no reference implementation" | Three oracles: a Python `dict`/`sorted()` model for the B+tree, `sqlite3` for SQL semantics, and invariant validators for structure. |


### Technical depth you'll be able to discuss cold


B+tree vs B-tree and why databases pick B+tree; why 4KB pages; slotted page layout and internal
fragmentation; buffer pool eviction and why LRU is wrong for sequential scans (and what real
systems do instead); WAL vs undo journal and why you chose undo; what `fsync` actually guarantees
and what it doesn't; 2PL vs MVCC; deadlock detection vs prevention; isolation levels and the
anomalies each permits; Volcano iterators vs vectorized execution; how an index turns O(n) into
O(log n) in page reads, not just comparisons.


That list is the real deliverable. The repo is the evidence for it.


---


## 10. Weekly Discipline


- **Commit daily**, with real messages. A GitHub contribution graph showing eight weeks of steady
  work is itself a signal.
- **CI green before every merge.** Never commit on red — the habit is what's being demonstrated.
- **One ADR per significant decision, written when you make it.** Ten minutes each, and it makes
  week 8 nearly free.
- **Keep a `NOTES.md` bug journal.** Symptom → hypothesis → root cause → fix. This is where your
  behavioral-interview stories come from; you will not remember them in November.
- **Demo to yourself every Sunday.** Two minutes, out loud. It surfaces "wait, that's broken" while
  there's still time.


---


## 11. Deferred (README "Future Work")


Cut for time, not because they lack value. Listing them shows you know the shape of the full
problem:


WAL with checkpointing and concurrent readers · MVCC snapshot isolation · **reading arbitrary
SQLite-written files** (freeblock parsing, WAL, UTF-16, non-4096 page sizes, auto-vacuum pointer
maps) · **rollback-journal format compatibility**, and with it the alternating-writer demo ·
multi-process locking via `fcntl` · cost-based optimizer with table statistics · hash join and merge
join · external merge sort for larger-than-memory `ORDER BY` · B+tree three-way rebalancing, page
merging and redistribution · `VACUUM` · subqueries and CTEs · `mmap` I/O · vectorized execution


Note the shape of that list: **everything on it is a feature, not a format change.** quilldb's files
are already real SQLite files. What's deferred is reading *other people's* SQLite files, and a set of
algorithms that would make quilldb's files denser and faster without changing what they are.


---


## 12. Summary of the Bet


v1 spent 120 hours on one deep, narrow, hard-to-evaluate claim: full bidirectional byte-exact SQLite
interoperability, with no way to run a query.


This plan spends the same 120 hours on a **complete, working, demoable database** with genuine depth
in the three areas backend interviews actually probe: **storage structures, durability, and
concurrency** — plus the presentation layer that makes a stranger care. And it keeps the half of v1's
premise that was actually cheap: **the on-disk format is SQLite's, byte for byte, so `sqlite3` can
open and verify what quilldb writes.**


That turns out not to be a party trick. It's a free reference implementation to test against on every
run, which is the single strongest quality lever available to a project this size. You keep the
interesting internals, you keep the format, you drop the expensive direction of the interop — and you
gain something you can hand someone and say *"try it, then open it in sqlite3."*





