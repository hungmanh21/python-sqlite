# quilldb — Reference Material, Mapped to the Roadmap


A companion to `roadmap.md`. For each week: what to read *before* you start coding, which
sections specifically, roughly how long it takes, and which reference implementation to consult
when you get stuck.


---


## 0. How To Use This


> **Read `docs/theory/` first for weeks 1–3.** Chapters 00–09 there already contain the *content* of
> most of the week 1–3 reading below — storage, B+tree mechanics, the SQL frontend, catalog and
> binding, and iterator execution — worked through from first principles with the primary sources
> quoted and cited. That's ~6.5 hours and it comes *out of* the budget below rather than adding to
> it. Use the entries below when you want the primary text itself, or when a theory chapter points
> you at one.


**Reading budget: ~13 hours total across 8 weeks — about 10% of build time.** Most weeks have 60–90
minutes of targeted reading, done the day you *start* that week's work, not before. Reading further
ahead than one week is procrastination dressed as diligence; you won't retain B+tree split
mechanics in week 1 and you'll re-read them in week 2 anyway.


Two weeks are exceptions, and both are worth the overrun. **Week 5 (~2 h)** is the highest-leverage
reading in the project. **Week 4 (~2 h 15 min)** is the second, because the planner is the one component
where you're copying an *architecture* rather than a data structure, and the architecture only exists in
prose — there's no `fileformat2.html` for a cost model.


Every entry below is marked:


- ⭐ **Essential** — read it, you'll be slower without it
- ○ **Useful** — read if you're stuck or curious
- ◇ **Depth** — for interview conversations, not for implementation


And each says *which sections*. Almost nothing here should be read cover to cover.


---


## 1. The Core Shelf


Six things you'll return to repeatedly. If you acquire nothing else, acquire these.


| Resource                                                                      | What it's for                                                                                                                                                                                                                                                                 | Cost |
| ----------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---- |
| ⭐ [SQLite Database File Format](https://www.sqlite.org/fileformat2.html)      | The canonical spec for varints, serial types, record format, page layout. **This is not background reading — it's the specification quilldb implements.** Every constant in `constants.py` comes from here, and `PRAGMA integrity_check` is the enforcement.                  | Free |
| ⭐ [Atomic Commit In SQLite](https://www.sqlite.org/atomiccommit.html)         | Your entire week 5, written by the people who got it right. Non-negotiable.                                                                                                                                                                                                   | Free |
| ⭐ **Database Internals** — Alex Petrov (O'Reilly, 2019)                       | Part I only (ch 1–8). The best book-length treatment of storage engines, page layout, and B-tree implementation. Skip Part II entirely — it's distributed systems, irrelevant here.                                                                                           | ~$40 |
| ⭐ [Crafting Interpreters](https://craftinginterpreters.com/) — Robert Nystrom | Free online. Chapters 4–6 are the best writing on handwritten tokenizers and recursive-descent parsers that exists. Your week 3.                                                                                                                                              | Free |
| ⭐ [CMU 15-445 Intro to Database Systems](https://15445.courses.cs.cmu.edu/)   | Lecture slides + notes. Fall 2026 is taught by Jignesh Patel; **Andy Pavlo's archived semesters have full public YouTube lecture playlists** — those are the famous ones. Archived course sites are linked from the homepage. Watch ~8 of 26 lectures, listed per week below. | Free |
| ⭐ [SQLite: How It Is Tested](https://www.sqlite.org/testing.html)             | Your testing philosophy. Also the source of the best stat in your README: as of 3.42.0, SQLite ships **590× more test code than library code**.                                                                                                                               | Free |


---


## 2. Week-By-Week


### Week 1 — Pager, Buffer Pool, Record Codec


Read in this order. The fly.io post is a gentle on-ramp; the spec is dense and makes far more sense
after it.


- ⭐ [SQLite Internals: Pages & B-trees](https://fly.io/blog/sqlite-internals-btree/) — Ben Johnson.
  **~20 min, read this first.** Walks a single `INSERT` from SQL down to bytes: varints, serial type
  codes (`TEXT = n*2+13`, `BLOB = n*2+12`, float = `0x07`), the cell pointer array, records packed
  backward from the end of the page. Exactly your week 1, at exactly the right altitude.
- ⭐ [SQLite File Format](https://www.sqlite.org/fileformat2.html) — sections **1.2** (database
  header, all 100 bytes), **1.5/1.6** (B-tree page header and cell pointer array), **2.1** (record
  format and serial types), and the varint description in **1.5**. **~60 min.** Skip: WAL format,
  pointer maps, `sqlite_stat*`, the schema table details.
- ⭐ **Petrov, Database Internals ch 3** ("File Formats") — slotted pages, cell layout, why the
  pointer array grows one way and cell content the other. The best treatment of slotted page design
  I know of. ~45 min.
- ⭐ **CMU 15-445: "Database Storage" (2 lectures) + "Buffer Pools"** — the *slides* are enough,
  ~40 min. Buffer pool: pin counts, dirty flags, eviction policy, why LRU is wrong for sequential
  scans.
- ○ **CMU 15-445 Project #1** is literally "build a buffer pool manager." The public writeup is a
  free, precise spec for the component you're building this week. Worth reading even though you
  won't submit it.
- ○ Python: [`struct`](https://docs.python.org/3/library/struct.html),
  [`memoryview`](https://docs.python.org/3/library/stdtypes.html#memoryview),
  [`os.fsync`](https://docs.python.org/3/library/os.html#os.fsync). Know that `struct.pack('>I', …)`
  is your friend and that slicing a `memoryview` doesn't copy.


**Gotcha worth knowing up front:** decide *now* whether your varints are big-endian
SQLite-style (7 bits per byte, high bit = continue, max 9 bytes) or LEB128 little-endian
(protobuf-style). They're incompatible and mixing them up between encoder and decoder is a
genuinely nasty afternoon. Write it in the format spec before you write code.


---


### Week 2 — B+Tree


This is the week where a good walkthrough saves you the most time. Splits — especially propagating
a split into a full parent — are where everyone gets stuck.


- ⭐ [Let's Build a Simple Database](https://cstack.github.io/db_tutorial/) — cstack. **Parts 7–14.**
  A step-by-step B-tree-with-splits build in C, with the SQLite page format. Parts **10** (splitting
  a leaf), **13** (updating the parent after a split), and **14** (splitting internal nodes) are
  precisely your hard problem this week. The C translates to Python essentially 1:1. ~90 min for
  those parts. Note the project is no longer maintained, which doesn't matter — the content is
  timeless.
- ⭐ **Petrov, Database Internals ch 2 + ch 4.** Ch 2 is B-tree fundamentals; **ch 4 ("Implementing
  B-Trees") is the money chapter** — page splits, merges, rebalancing, right-most pointers, sibling
  links. ~90 min.
- ⭐ [chidb](https://chi.cs.uchicago.edu/chidb/) — **Assignment 1: B-Trees, 8 steps.** A University
  of Chicago teaching RDBMS, SQLite-derived with simplifying assumptions. Its assignment spec walks
  from "open the file and load a B-tree node" through cell manipulation, search, insertion without
  splitting, insertion *with* splitting, and finally index B-trees. **This is very nearly a
  drop-in task list for your weeks 2 and 4** — the closest structural match to this project that
  I've found anywhere. Read the assignment page and the file-format page.
- ○ [Build Your Own Database From Scratch](https://build-your-own.org/database/) — James Smith.
  **Part I (ch 4–8) is free to read online.** Go. Ch 5 (B+Tree node and insertion), ch 6 (deletion
  and testing), ch 8 (free list). The copy-on-write framing differs from yours but the node
  mechanics are directly useful, and ch 6's *testing* discussion is good.
- ○ [SQLite `btree.c`](https://github.com/sqlite/sqlite/blob/master/src/btree.c) — the file opens
  with an extended comment block documenting the page format. Ground truth, dense, public domain.
  Consult when a detail is ambiguous; don't try to read it straight through.
  *(Note: sqlite.org's own source browser has bot protection — use the GitHub mirror.)*
- ○ **CMU 15-445: "B+Tree Indexes"** (usually 2 lectures) — slides, ~30 min.
- ◇ **Goetz Graefe, "Modern B-Tree Techniques"** (2011, ~200pp) — the definitive modern survey.
  Skim the taxonomy for interview vocabulary; do not read it now.


**Where the bugs will be:** propagating a split when the parent is *also* full, and the right-most
child pointer. Your structural validator exists to catch exactly these. Write it before you need it.


---


### Week 3 — SQL Frontend and Iterator Executor


- ⭐ [Crafting Interpreters](https://craftinginterpreters.com/) — **ch 4 (Scanning), 5 (Representing
  Code), 6 (Parsing Expressions).** ~2h and worth every minute. Java in the text, but you're taking
  the structure, not the code. Ch 5's discussion of AST representation will save you from a bad
  node design.
- ⭐ [Simple but Powerful Pratt Parsing](https://matklad.github.io/2020/04/13/simple-but-powerful-pratt-parsing.html)
  — matklad. **~40 min.** Use this for expression precedence *instead of* Crafting Interpreters'
  recursive-cascade approach — it's markedly cleaner for SQL, which has many precedence levels
  (`OR` < `AND` < `NOT` < comparison < `+ -` < `* /` < unary). The key reframing is "binding power"
  rather than precedence, with asymmetric left/right powers giving you associativity for free.
  Code at `matklad/minipratt`.
- ⭐ [SQLite SQL syntax diagrams](https://www.sqlite.org/lang.html) — railroad diagrams per
  statement. Your grammar reference; pick the subset you support and note the rest as unsupported.
- ⭐ [toydb](https://github.com/erikgrinaker/toydb) — **read the architecture guide.** Rust, ~7.3k
  stars, by an engineer who has since worked on CockroachDB and Neon. Explicitly educational:
  "simple and understandable, and also functional and correct," with performance a stated non-goal.
  Iterator-based executor with `EXPLAIN`, plus a SQL parser. Its architecture doc is a guided tour
  of the whole pipeline and is the single best model for how to *document* a project like yours.
- ○ [chidb](https://chi.cs.uchicago.edu/chidb/) **Assignment 3 (Code Generation)** — schema loading,
  simple `SELECT`, `INSERT`, `CREATE TABLE`. Note chidb targets a bytecode DBM rather than
  iterators, so take the SQL→plan mapping and ignore the instruction emission.
- ◇ **Graefe, "Volcano — An Extensible and Parallel Query Evaluation System"** (1994) — the paper
  that named the iterator model. First ~6 pages is all you need. Cite it in your ADR.


**Protect this week.** It's the one that turns the project from an artifact into a thing you can
hand someone.


---


### Week 4 — Indexes and the Planner


**~2 h 15 min, the longest reading week after week 5 — budget for it.** Week 4 is where the two planner
philosophies meet, so read them as a *pair* rather than picking a side: `optoverview` §2 gives you the
**rules** (which plans are legal), `queryplanner-ng` gives you the **costs** (which legal plan wins).
quilldb implements both, in that order, and the trade-off note at the end of this section is the answer
to "why not just use rules?"


- ⭐ [SQLite Query Optimizer Overview](https://www.sqlite.org/optoverview.html) — sections **2**
  (WHERE clause analysis), **8** (choosing between multiple indexes), **9** (covering indexes),
  **10** (ORDER BY optimizations). **~50 min.** §2 is effectively a spec for the **legality stage** —
  stage 1 of four — and it states precisely when an index is usable: leading columns need `=`/`IN`/`IS`,
  the right-most used column may take inequalities, **no gaps allowed**. There's a clean worked table of
  which WHERE clauses can use `ex1(a,b,c,d,…)` and why. Steal that table's logic directly; it's the
  source of the 12-case test in week 4. Then read **§8** immediately after, because it's where the docs
  admit that legality alone doesn't decide — that's the seam the cost model fills.
  Skip: subquery flattening (§11), co-routines (§12), skip-scan (§6), outer-join strength reduction
  (§16) — all out of scope.
- ⭐ [The Next-Generation Query Planner](https://www.sqlite.org/queryplanner-ng.html) — **~20 min, and
  this is the citation that matters.** The page that actually says SQLite's planner is cost-based: plan
  search is *"finding a minimum-cost path through the graph"*, *"the 'cost' here is logarithmic"*, and
  since 3.8.0 the search keeps the N best paths at each step (N-nearest-neighbours, "N3").
  **`arch.html` never says "cost-based"** — it calls the planner *"an AI that strives to select the best
  algorithm"*, which is evocative and useless as a citation. If you cite one page for the architecture
  you copied, cite this one. See chapter 12 §12.5.
- ⭐ **Selinger et al., "Access Path Selection in a Relational Database Management System"** (1979) —
  the founding cost-based optimizer paper, and **core reading now, not depth**. Read §§1–5, ~40 min.
  Three things in it are directly yours, and it's worth being able to name them:
  - separating **candidate access paths** from **choosing** among them — your
    `enumerate_access_paths` / `choose_access_path` split is this paper's structure;
  - **selectivity factors**. Selinger's default for `column = value` with no index is **1/10** and for
    `BETWEEN` is **1/4** — which are quilldb's `DEFAULT_EQUALITY_SELECTIVITY` and
    `DEFAULT_RANGE_SELECTIVITY`, and SQLite's own defaults, unchanged in 45 years. (His default for a
    one-sided `>` is 1/3, where SQLite uses 1/4 — a small honest divergence worth knowing.)
  - the **join estimate**. Selinger's selectivity for `column1 = column2` is
    `1/MAX(ICARD1, ICARD2)` — which is *literally* week 7's
    `|R| × |S| / max(NDV(R.a), NDV(S.b))`. You are not inventing that formula; you're citing it.


  What you deliberately *don't* take is the dynamic-programming join enumeration — see chapter 12 §12.7.
- ⭐ [SQLite File Format](https://www.sqlite.org/fileformat2.html) **§2.6.4** — the `sqlite_stat1`
  encoding that `quill_stat1` mirrors: K+1 integers per index, the first is the row count, the N-th is
  the average number of rows sharing the first N-1 columns, and the last is 1 for a unique index. ~10
  min, and read it *before* writing `ANALYZE` rather than after.
- ⭐ [Use The Index, Luke](https://use-the-index-luke.com/) — Markus Winand. Free web version. The
  practical mental model for leading columns, range constraints, and covering indexes. Read the
  "Anatomy of an Index" and "The Where Clause" chapters. ~45 min.
- ○ [SQLite Query Planner](https://www.sqlite.org/queryplanner.html) — the same material from the
  user's perspective, with `EXPLAIN QUERY PLAN` output. Good model for what your `EXPLAIN` should
  print.
- ○ [SQLite File Format](https://www.sqlite.org/fileformat2.html) §1.6 — index B-tree cell format,
  for how index keys are laid out.


#### The trade-off, so you can argue it either way


You're building cost-based. You should still be able to defend the rule-based choice, because it's a
real engineering position and an interviewer may push on it.


|                 | Rule-based                                                 | Cost-based                                        |
| --------------- | ---------------------------------------------------------- | ------------------------------------------------- |
| Decides using   | the *shape* of the query                                   | the shape **and** the data                        |
| Needs           | nothing                                                    | `ANALYZE`, plus statistics fresh enough to trust  |
| Typical failure | picks a legal-but-terrible index, *consistently*           | good on average, occasionally very bad on skew    |
| Debugging       | you can predict the plan by reading the query              | you must inspect `EXPLAIN` **and** the statistics |
| Build cost here | ~3 h                                                       | ~7 h, and it never stops being tunable            |
| Cannot do       | separate two same-shaped indexes 500× apart in selectivity | promise you the same plan tomorrow                |


**They are stages, not rivals** — this is the framing week 4 is built around, and the single most
useful thing to have straight. Rules answer *which plans are legal*; costs answer *which legal plan
wins*. No cost can make an illegal seek correct, and no rule can separate two indexes with identical
shapes and wildly different selectivity (chapter 12 §12.1, failure 3). Anyone who presents these as
competing philosophies has skipped a step.


The genuinely defensible rule-based position, worth saying out loud: **predictability is a feature.** A
plan that is always mediocre is easier to operate than one that is usually excellent and occasionally
catastrophic — which is exactly why production databases ship plan hints and plan freezing (chapter 12
§12.7). And statistics are *derived state*: one more thing that goes stale, one more thing to
invalidate, one more reason a query got slow overnight with no deploy. quilldb pays those costs
deliberately — that's what the `ANALYZE`-invalidates-the-plan-cache wiring in week 7 is *for* — and the
honest summary is that cost-based trades worst-case predictability for average-case speed.


---


### Week 5 — Transactions and Crash Recovery ⭐⭐


**Spend two hours here.** This is the highest-value reading in the project, and the resources are
unusually good.


- ⭐⭐ [Atomic Commit In SQLite](https://www.sqlite.org/atomiccommit.html) — **read it twice.** This
  document *is* your week 5 spec. What matters most:
  - **§2 Hardware Assumptions.** Sector writes are linear, not atomic. The OS reorders writes. File
    size may grow before content lands. This is the honest list of what you're defending against,
    and it's where your `docs/durability.md` "what I don't guarantee" section comes from.
  - **§3, the 12-step commit sequence.** The load-bearing ordering is:
    write journal (3.5) → **fsync journal** (3.7) → write db pages (3.9) → **fsync db** (3.10) →
    **delete journal = the commit point** (3.11). Note §3.7 normally does *two* fsyncs: sync the
    journal body, then write the page count into the header, then sync the header. The page count
    starts at **zero** so a torn journal rolls back nothing.
  - **§4 Rollback.** The five-part test for a *hot* journal — exists, non-empty, no RESERVED lock,
    well-formed header, no missing super-journal. Recovery: take EXCLUSIVE, copy originals back,
    truncate to the journal header's original size, verify per-page checksums, sync, delete.
  - **§6.2 Garbage in journal files.** Two defenses: the zero-initialized page count, and a 32-bit
    checksum per journal page. Implement both — the checksum is ~5 lines and it's a real answer to
    "how do you know the journal isn't corrupt?"
  - **§8 Testing.** How they crash-test: a modified VFS simulating incomplete sector writes, garbage
    pages, and reordered writes at varying points. This is your fault-injection design.
  - **§9 Things That Can Go Wrong.** Broken locking, lying `fsync`, partial deletion, renamed hot
    journals. Excellent material for the limitations section and for interviews.
- ⭐ [Files are hard](https://danluu.com/file-consistency/) — Dan Luu. **~30 min**, and it's why
  you'll take `fsync` seriously. Walks the incremental hardening of a single overwrite: undo log →
  fsync the log → **checksum in the log** → **fsync the parent directory** → final fsync. Also
  notes macOS needs `fcntl(F_FULLFSYNC)` and that some drives simply ignore flush commands "to look
  better in benchmarks." The closing point matters for your README: silent corruption is inevitable,
  so the real question is whether your format loses one record or the whole database.
  - Don't skip the **Update** section: the most common reader suggestion was "just write to a temp
    file and rename." Luu rebuts it — rename is atomic in normal operation but *not across crashes*.
    You'll be tempted by the same shortcut.
- ⭐ [SQLite: How It Is Tested](https://www.sqlite.org/testing.html) — the **anomaly testing**
  section. Their I/O-error pattern is exactly your crash matrix: a rigged layer injects an error
  after N operations, run in a loop with N increasing, twice (fail-once and fail-always), then
  `PRAGMA integrity_check` to confirm no corruption. Also read the **compound failures** idea — an
  error *while recovering from a prior crash*. That's a nasty test case and a great thing to have.
- ⭐ **Petrov, Database Internals ch 5** ("Transaction Processing and Recovery") — WAL vs
  shadow-paging vs undo/redo, the taxonomy you need to justify your choice. ~45 min.
- ○ [Ensuring data reaches disk](https://lwn.net/Articles/457667/) — LWN. What `write`, `fsync`,
  `fdatasync`, and `sync_file_range` each actually guarantee.
- ◇ **Pillai et al., "All File Systems Are Not Created Equal"** (OSDI '14) — the paper behind Dan
  Luu's piece. Found crash-consistency bugs in LevelDB, HDFS, Zookeeper, and git. The two most
  common developer errors: assuming ordering between syscalls, and assuming syscalls are atomic.
  Notably, "sqlite devs understood the issues deeply but still had bugs."
- ◇ **Mohan et al., "ARIES"** (1992) — canonical redo+undo WAL recovery. You're building undo-only,
  so read the abstract and intro so you can explain the difference. Don't read all 70 pages.
- ◇ **Rebello et al., "Can Applications Recover from fsync Failures?"** (USENIX ATC '20) — what
  happens when `fsync` returns an error. The answer is worse than you'd hope.


---


### Week 6 — Multi-Threaded Concurrency


- ⭐ **CMU 15-445: "Two-Phase Locking" + "Deadlock Detection & Prevention"** — slides, ~40 min.
  Covers exactly your week: S/X lock compatibility matrix, 2PL vs strict 2PL, the **wait-for graph**
  and cycle detection, and prevention alternatives (wait-die, wound-wait). Also the lecture on
  "Concurrency Control Theory" for serializability.
- ⭐ [A Critique of ANSI SQL Isolation Levels](https://arxiv.org/abs/cs/0701157) — Berenson,
  Bernstein, Gray, Melton, O'Neil, O'Neil (SIGMOD '95; also MSR-TR-95-51). **~1h, and it's the
  single most valuable interview-prep reading in this list.** The argument: SQL-92 defines isolation
  via three anomalies (dirty read, non-repeatable read, phantom) and those definitions *fail* to
  characterize several real isolation levels, including what standard locking implementations
  actually give you. It also gives the formal treatment of **snapshot isolation**.
  **Why it matters to you:** after reading it you can say "I implemented X, which permits anomaly Y,
  and here's why that was the right tradeoff" — instead of vaguely claiming ACID. Interviewers
  notice the difference immediately.
- ⭐ [SQLite File Locking And Concurrency](https://www.sqlite.org/lockingv3.html) — the
  UNLOCKED → SHARED → RESERVED → PENDING → EXCLUSIVE ladder. Note *why* PENDING exists: it lets
  existing readers finish while blocking new ones, preventing writer starvation. That state machine
  maps cleanly onto `threading.Condition` and is a better design than the one you'd invent.
- ⭐ Python [`threading`](https://docs.python.org/3/library/threading.html) — specifically
  `Condition` (`wait_for`, `notify_all`), `RLock`, and lock timeouts. Most of your lock manager is
  a `Condition` plus a dict of lock states.
- ○ **David Beazley, "Understanding the Python GIL"** — the well-known PyCon 2010 talk, slides and
  video on `dabeaz.com`. *(I couldn't verify a stable URL for this one — search the title.)* You
  need this for the honest version of your throughput story: why write throughput won't scale with
  threads, and why the interesting result is correctness under contention plus reader behavior.
- ◇ **Gray et al., "Granularity of Locks and Degrees of Consistency in a Shared Data Base"** (1976)
  — origin of multi-granularity locking (intention locks) and "degrees of consistency," the ancestor
  of isolation levels. Read if you want to explain why table-level locking is a coherent choice
  rather than a shortcut.
- ◇ [toydb](https://github.com/erikgrinaker/toydb) — for contrast, it uses **MVCC snapshot
  isolation** rather than locking. Reading its transaction module tells you what you'd build if you
  went the MVCC route, which is a likely follow-up question.


---


### Week 7 — Joins, Aggregation, Ordering


- ⭐ **CMU 15-445: "Sorting & Aggregations" + "Joins"** — slides, ~40 min. Nested loop, block
  nested loop, index nested loop, sort-merge, hash join; and hash vs sort-based aggregation.
- ⭐ [SQLite optoverview](https://www.sqlite.org/optoverview.html) **§7 (Joins)** — SQLite's joins
  are *all* nested loops, which is exactly your plan, so this is directly applicable. The worked
  `node`/`edge` example showing one join order beating another by **~2000×** is a great README or
  interview illustration of why join ordering matters.
- ○ [toydb SQL examples](https://github.com/erikgrinaker/toydb) — its `EXPLAIN` output showing hash
  joins, aggregation, projection, and ordering. Good format reference for your own `EXPLAIN`.
- ◇ **Graefe, "Query Evaluation Techniques for Large Databases"** (1993, ~100pp) — the definitive
  survey. Read the join and sorting sections only. This is where you get the vocabulary to discuss
  what you *didn't* build (hash join, sort-merge, external sort) intelligently.
- ○ **Note on `NULL` semantics:** this is where your differential tests against `sqlite3` will earn
  their keep. `NULL` in comparisons, in aggregates (`COUNT(*)` vs `COUNT(col)`), in `GROUP BY`
  keys, and in `ORDER BY` sort position are all easy to get subtly wrong. Three-valued logic is
  worth 20 minutes of deliberate thought before you implement `WHERE`.


---


### Week 8 — Presentation


- ⭐ **Hellerstein, Stonebraker & Hamilton, "Architecture of a Database System"** (Foundations and
  Trends in Databases, 2007, 119pp) —
  [PDF at Berkeley](https://dsf.berkeley.edu/papers/fntdb07-architecture.pdf).
  **Read this in week 8, not week 1.** It's the classic "how a whole RDBMS fits together" survey:
  process models, storage, query processing, transactions, shared components. By week 8 every
  section will map onto something you built or deliberately cut, which makes it a completely
  different reading experience than it would have been in August. It will sharpen how you *talk*
  about the project more than anything else on this list.
  *(My automated fetch couldn't extract text from the PDF — the file is there and its metadata
  confirms a 119-page Foundations-and-Trends monograph, but open it yourself to confirm.)*
- ⭐ [toydb](https://github.com/erikgrinaker/toydb) — **use its README and architecture doc as your
  template.** Clear scope statement, explicit non-goals ("performance, scalability, and availability
  are non-goals"), architecture diagram, guided code tour, SQL reference, and — notably — *honest
  benchmarks*, including "Write performance is atrocious" with the reason (fsync, no write
  batching). That honesty reads as confidence, not weakness. Copy the posture.
- ⭐ [SQLite: How It Is Tested](https://www.sqlite.org/testing.html) — source material for your
  testing section. The 590× test-to-code ratio, four independent harnesses, and their taxonomy
  (anomaly testing, fuzzing, malformed-database testing, boundary values, mutation testing) give you
  a vocabulary for describing your own suite. Their line on defensive code vs coverage is worth
  quoting: MC/DC testing "works well for building code that is robust during normal use, whereas
  fuzz testing is good for building code that is robust against malicious attack."
- ○ [SQL Logic Test](https://www.sqlite.org/sqllogictest/) — the framework SQLite uses to run 7.2M
  queries against itself *and* Postgres, MySQL, SQL Server, and Oracle, comparing answers. This is
  your differential testing idea, industrialized. Cite it as prior art for your harness.
- ○ [chidb testing docs](https://chi.cs.uchicago.edu/chidb/) — how a teaching DB structures its test
  suite.


---


## 3. Reference Implementations


Read code when a doc leaves you unsure. Ranked by usefulness *to this specific project*.


| Project                                                                 | Lang | Steal                                                                                                                                                                                                                   | Rating                      |
| ----------------------------------------------------------------------- | ---- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------- |
| [chidb](https://chi.cs.uchicago.edu/chidb/)                             | C    | Assignment specs 1–4 as incremental task lists. SQLite-derived format with deliberate simplifications — the same tradeoff you're making. Has a shell with `EXPLAIN`.                                                    | ⭐⭐ closest structural match |
| [cstack/db_tutorial](https://cstack.github.io/db_tutorial/)             | C    | Parts 7–14: B-tree splits, step by step, with working code at each step.                                                                                                                                                | ⭐⭐ best for week 2          |
| [toydb](https://github.com/erikgrinaker/toydb)                          | Rust | Architecture doc, iterator executor, `EXPLAIN` format, README posture, goldenscript testing approach.                                                                                                                   | ⭐⭐ best documented          |
| [build-your-own.org/database](https://build-your-own.org/database/)     | Go   | Part I free: B+tree node/insert/delete, free list. Ch 6's testing discussion.                                                                                                                                           | ⭐                           |
| [SQLite](https://github.com/sqlite/sqlite) `src/btree.c`                | C    | Ground truth on page format and cell handling. `pager.c` for journal mechanics — but it's ~10k lines of edge cases you've cut.                                                                                          | ⭐ reference only            |
| [BoltDB](https://github.com/boltdb/bolt)                                | Go   | ~4k LOC, single-file B+tree with mmap and copy-on-write. Unusually readable end-to-end in an afternoon.                                                                                                                 | ⭐                           |
| CMU **BusTub** (linked from 15-445)                                     | C++  | Buffer pool manager (wk 1) and lock manager (wk 6) reference designs.                                                                                                                                                   | ○                           |
| **SimpleDB** — accompanies Sciore, *Database Design and Implementation* | Java | A complete teaching RDBMS: file/log/buffer manager, records, metadata, parser, planner, JDBC. Scope match is very close to yours. *(Couldn't verify a canonical download URL — it ships with the book; search for it.)* | ○                           |


**How to read someone else's database:** find the page/node struct first, then the insert path, then
the split. Everything else is bookkeeping. Don't start at `main()`.


---


## 4. Deliberately Skipped


Saying why you're *not* reading something is as useful as a reading list. Each of these is a good
resource that would cost you hours for no return on this project:


| Skip                                                                 | Why                                                                                                                                                                                                                                        |
| -------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Petrov, Database Internals **Part II**                               | Distributed systems. Half the book, zero relevance.                                                                                                                                                                                        |
| [SQLite WAL docs](https://www.sqlite.org/wal.html)                   | You cut WAL. Read only if you resurrect it.                                                                                                                                                                                                |
| SQLite `pager.c`, `wal.c`                                            | Thousands of lines handling cases you explicitly descoped.                                                                                                                                                                                 |
| ARIES in full                                                        | You're undo-only. Abstract + intro is enough to compare.                                                                                                                                                                                   |
| All 26 CMU 15-445 lectures                                           | ~35 hours. Watch the 8 named above.                                                                                                                                                                                                        |
| **Designing Data-Intensive Applications**                            | Excellent book, wrong book. Only ch 3 is relevant and it's a survey — Petrov covers the same ground with the implementation detail you actually need.                                                                                      |
| The SQL standard                                                     | Enormous, expensive, and SQLite's syntax diagrams are a better spec for your purposes.                                                                                                                                                     |
| Graefe, Modern B-Tree Techniques (in full)                           | 200 pages. Skim for vocabulary only.                                                                                                                                                                                                       |
| SQLite's `sqlite_stat4` sampling and `where.c`'s `LogEst` arithmetic | You build `stat1` and plain float costs. Read fileformat2 §2.6.4 for the `stat1` encoding you *do* implement; skip `stat4` histograms and the fixed-point logarithm tricks — they're an optimization for a planning budget you don't have. |
| SQLite's `where.c` / `whereexpr.c` / `wherecode.c`                   | ~13,000 lines. You're copying the four-stage *architecture*, not the implementation. `optoverview` §2/§8 and `queryplanner-ng` describe it in ~30 pages.                                                                                   |


---


## 5. When You're Stuck


| Symptom                                             | Go here                                                                                                                                                                                      |
| --------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| "What byte goes where?"                             | fileformat2.html §1.2, §1.5, §2.1                                                                                                                                                            |
| "My varint decodes wrong"                           | fly.io post, then fileformat2 §1.5. Check endianness and the continue-bit convention.                                                                                                        |
| "Split works but the tree is corrupt two levels up" | cstack parts 13–14; then trust your validator over your intuition                                                                                                                            |
| "How do I parse expression precedence?"             | matklad Pratt parsing                                                                                                                                                                        |
| "Which index should the planner pick?"              | optoverview §2 for which are *legal*, §8 for choosing between them, then queryplanner-ng for the cost model. If the answer feels arbitrary, you're missing statistics — check `ANALYZE` ran. |
| "What order do I fsync in?"                         | atomiccommit.html §3.7–3.11. This is the answer, verbatim.                                                                                                                                   |
| "Is my journal safe against a torn write?"          | atomiccommit.html §6.2 — zero-init page count + per-page checksum                                                                                                                            |
| "What isolation level did I actually build?"        | Berenson et al. Work out which anomalies you permit.                                                                                                                                         |
| "How do I test something with no reference?"        | testing.html anomaly-testing section; SQL Logic Test for the differential idea                                                                                                               |
| "Is my design reasonable?"                          | Compare against chidb's assignment structure and toydb's architecture doc                                                                                                                    |


---


## 6. Verification Note


I fetched and confirmed content for: the fly.io B-tree post, build-your-own.org/database,
cstack's db_tutorial, chidb, `atomiccommit.html`, matklad's Pratt parsing post, `testing.html`,
`optoverview.html`, CMU 15-445, danluu's file-consistency piece, toydb, and the Berenson isolation
paper. The section details and figures quoted above come from those fetches.


Three caveats:
- **Hellerstein PDF** — the URL serves a 119-page Foundations-and-Trends monograph, but automated
  text extraction failed. Confirm by opening it.
- **Beazley's GIL talk** and **Sciore's SimpleDB** — cited by name because I couldn't verify a
  stable URL. Search the titles.
- Books (Petrov, Sciore) and older papers (Graefe, Selinger, Gray, ARIES, Pillai) are cited from
  knowledge by title/author/year rather than by link — those are stable enough to search.
- **Selinger's specific selectivity constants** in the week-4 entry (1/10 for `=`, 1/4 for `BETWEEN`,
  1/3 for a one-sided inequality, `1/MAX(ICARD1, ICARD2)` for `col1 = col2`) are quoted from knowledge,
  not from a fetched copy of the 1979 paper. The *correspondence* to SQLite's and quilldb's defaults is
  the interesting claim and it's worth 10 minutes to verify against the paper's §5 before you say it in
  an interview — it's a great line if right and an easy thing to be corrected on if not.