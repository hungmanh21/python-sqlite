# quilldb vs. SQLite — What's the Same, What's Different, and Why


One page that answers the question every reader of this repo will eventually ask: *"you said you're
writing SQLite's format — so what did you actually keep, and what did you change on purpose?"*


**The rule that sorts every row below:** quilldb commits to SQLite's on-disk **format**, verified by
`sqlite3 f.db "PRAGMA integrity_check"` returning `ok`, in **one direction only** — quilldb writes files
`sqlite3` can read; it does not need to read arbitrary `sqlite3`-written files. Format fidelity and
algorithmic fidelity are independent axes. A row can be **Same** on bytes and **Different** on algorithm
at once — the journal is the clearest example, and it's in the table.


| Verdict         | Means                                                                                                    |
| --------------- | -------------------------------------------------------------------------------------------------------- |
| 🟰 **Same**      | quilldb does what SQLite does, because the acceptance test requires it or there was no reason to diverge |
| 🔀 **Different** | a deliberate choice, argued below, that does **not** break the format                                    |
| ✂️ **Cut**       | a real SQLite feature, out of scope, refused rather than misread                                         |


**Status** marks whether the row reflects code that exists (**Built**, weeks 1–3) or a design already
written up in `docs/theory/` and specced in `docs/implementation/` but not yet coded (**Planned**,
weeks 4–8). Full reasoning for every row lives in the linked chapter; this page is the index.


---


## Storage and file format


| Area                              | quilldb                                                       | SQLite                                              | Verdict       | Status                   | Why                                                                                                                                                                               |
| --------------------------------- | ------------------------------------------------------------- | --------------------------------------------------- | ------------- | ------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Magic + 100-byte header           | all 22 real fields, real offsets                              | same                                                | 🟰             | Built                    | the acceptance test is byte-exact                                                                                                                                                 |
| Page 1's dual role                | file header + `sqlite_schema` root, b-tree header at byte 100 | same                                                | 🟰             | Built                    | no separate "catalog root" field exists to diverge on — see below                                                                                                                 |
| Page header size                  | 8 bytes leaf / 12 interior, by page type                      | same                                                | 🟰             | Built                    | [storage/01 §1.6](theory/storage/01-pages-and-the-pager.md), [storage/02 §2.5](theory/storage/02-the-slotted-page.md)                                                             |
| Page type bytes                   | 2 / 5 / 10 / 13                                               | same                                                | 🟰             | Built                    | `decodeFlags` accepts only these four                                                                                                                                             |
| Slotted page layout               | pointers forward, cells backward, two regions meeting         | same                                                | 🟰             | Built                    | [storage/02](theory/storage/02-the-slotted-page.md)                                                                                                                               |
| **Freeblocks on delete**          | **repack the page**, zero fragmentation always                | chained freeblocks + fragment counter, capped at 60 | 🔀             | Built                    | see below                                                                                                                                                                         |
| **Freelist structure**            | **one level** (a page is legal, if less efficient)            | two-level trunk/leaf                                | 🔀             | Built, two-level planned | see below                                                                                                                                                                         |
| Freed page zeroing                | full page zeroed                                              | same                                                | 🟰             | Built                    | stale bytes at offset 4–7 make `sqlite3` report "freelist leaf count too big" — verified by experiment                                                                            |
| Overflow thresholds               | exact X/M/K formulas from U                                   | same                                                | 🟰             | Built                    | [btree/06 §6.5](theory/btree/06-b-tree-mechanics.md)                                                                                                                              |
| Page size                         | fixed 4096                                                    | configurable 512–65536                              | ✂️             | Built                    | [ADR-001](decisions/ADR-001-bufferpool-raw-pages.md) assumes it; not a format requirement, a scope cut                                                                            |
| Reserved bytes / page             | fixed 0                                                       | configurable, enables page checksums                | ✂️             | Built                    | no reason to reserve space for a feature not built                                                                                                                                |
| Text encoding                     | UTF-8 only                                                    | UTF-8 / UTF-16 (LE/BE)                              | ✂️             | Built                    | `check_supported()` refuses UTF-16 rather than misreading it                                                                                                                      |
| `auto_vacuum` / pointer maps      | not implemented                                               | opt-in, enables file shrinking                      | ✂️             | Built                    | needs a child→parent back-pointer the base format lacks; every structural change pays a second page write forever — [btree/10 §10.7](theory/btree/10-deletion-and-space-reuse.md) |
| `VACUUM`                          | not implemented                                               | rebuilds the file to reclaim space                  | ✂️             | Built                    | reclamation needs relocating live pages, which needs the back-pointer above                                                                                                       |
| `WITHOUT ROWID` tables            | not implemented                                               | index-organized tables                              | ✂️             | Planned (theory only)    | schema feature, not a format extension — [btree/11 §11.7](theory/btree/11-index-b-trees.md)                                                                                       |
| Reading arbitrary `sqlite3` files | not required                                                  | —                                                   | ✂️ (by design) | Built                    | the acceptance test is one-directional; no freeblock parsing, no journal-format read compat                                                                                       |


### Page 1's dual role — same, and worth knowing *why* it's not a divergence


quilldb doesn't have a "catalog root page" configuration field, and it's tempting to read that as a gap.
It isn't: SQLite doesn't have one either. Page 1 is *hardcoded* as the schema root — a constant that can't
be corrupted, versus a stored field that could point at a bad page. Matching this costs one function,
`page_header_offset()`, and buys the header's simplest possible design. [storage/01 §1.6](theory/storage/01-pages-and-the-pager.md).


### Freeblocks vs. repack-on-delete — different, and it's a real tradeoff, not laziness


SQLite chains deleted cells into a linked list of holes (freeblocks), tracks 1–3 byte remainders
separately as "fragments," and defragments past a 60-byte cap. quilldb repacks the page immediately on
every delete: slide the survivors tight, zero the freeblock and fragment fields.


**Cost:** O(page size) per delete instead of O(1). **Payoff:** `free_space()` is always one contiguous
number, fragments can never accumulate, and `delete_cell` is ~15 lines instead of ~80. A page with no
holes is a perfectly well-formed SQLite page — the fields exist and correctly say "no holes." Right
tradeoff at quilldb's write volume; wrong one at SQLite's. [storage/02 §2.4](theory/storage/02-the-slotted-page.md).


### One-level freelist — a documented gap, and an empirically verified one


A one-level freelist (every freed page linked directly, no trunk/leaf split) is **legal** SQLite —
verified by hand-building one and confirming `sqlite3 ... integrity_check` returns `ok`. The two-level
trunk/leaf design exists purely so freeing and allocating touch ~1/120th as many pages, which is a
performance property, not a correctness one. quilldb currently builds the simpler one-level list; the
two-level version is designed in [storage/01 §1.8](theory/storage/01-pages-and-the-pager.md) as the next
increment, not a rewrite.


---


## Encoding


| Area                  | quilldb                                          | SQLite                        | Verdict | Status  | Why                                                                                                                                 |
| --------------------- | ------------------------------------------------ | ----------------------------- | ------- | ------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| Varint                | 7-bit groups, 9-byte max, 9th byte all 8 bits    | same                          | 🟰       | Built   | [codec/03 §3.4](theory/codec/03-encoding-varints-and-records.md)                                                                    |
| Negative integers     | two's-complement by serial-type width, no zigzag | same                          | 🟰       | Built   | see below                                                                                                                           |
| Record format         | manifest (serial types) + body, same table       | same                          | 🟰       | Built   | [codec/03 §3.5](theory/codec/03-encoding-varints-and-records.md)                                                                    |
| Cross-type sort order | NULL < number < text < blob                      | same                          | 🟰       | Built   | [btree/11 §11.8](theory/btree/11-index-b-trees.md)                                                                                  |
| Collations            | `BINARY` only                                    | `BINARY` / `NOCASE` / `RTRIM` | ✂️       | Planned | covers every test; an index built `BINARY` can't serve a `NOCASE` query anyway — [btree/11 §11.8](theory/btree/11-index-b-trees.md) |


### No zigzag — same as SQLite, and worth knowing why that's the better answer


The obvious fix for "negative varints cost 9 bytes" is zigzag encoding (map signed → unsigned so small
negatives stay small). SQLite doesn't use it for record values, and the reason is sharper than
"convention": a record's serial type *already* declares the value's width, so paying a re-mapping cost to
compress a bare varint would be solving a problem records don't have. Only **bare** rowid varints (never
inside a record) still cost 9 bytes when negative — which is why negative rowids are documented as
pathological rather than fixed. [codec/03 §3.5](theory/codec/03-encoding-varints-and-records.md).


---


## B-tree structure and mutation


| Area                                 | quilldb                                            | SQLite                                                      | Verdict | Status  | Why                                                                                                                            |
| ------------------------------------ | -------------------------------------------------- | ----------------------------------------------------------- | ------- | ------- | ------------------------------------------------------------------------------------------------------------------------------ |
| Cell formats                         | all four: table leaf/interior, index leaf/interior | same                                                        | 🟰       | Built   | [btree/06 §6.0](theory/btree/06-b-tree-mechanics.md)                                                                           |
| Split                                | **two-way only**                                   | three-way rebalance (redistributes across up to 3 siblings) | 🔀       | Built   | see below                                                                                                                      |
| **Delete: merge underfull siblings** | **not implemented**                                | merges below ⅓ occupancy                                    | 🔀       | Planned | see below                                                                                                                      |
| Delete: free empty pages             | yes                                                | yes                                                         | 🟰       | Planned | both do this                                                                                                                   |
| Root collapse on empty               | yes, page number preserved                         | same                                                        | 🟰       | Planned | [btree/10 §10.4](theory/btree/10-deletion-and-space-reuse.md)                                                                  |
| Index key encoding                   | `(indexed cols…, rowid)` as one record             | same                                                        | 🟰       | Planned | rowid is a record **field**, proven by rowids 0/1 costing zero body bytes — [btree/11 §11.4](theory/btree/11-index-b-trees.md) |
| `UNIQUE` index storage               | byte-identical to non-unique                       | same                                                        | 🟰       | Planned | uniqueness is a comparison rule, not a format difference                                                                       |
| Index divider keys (interior)        | full untruncated key                               | same                                                        | 🟰       | Planned | no suffix truncation — [btree/11 §11.6](theory/btree/11-index-b-trees.md)                                                      |
| Buffer pool eviction                 | LRU                                                | LRU-ish, tuned for scans                                    | 🔀       | Built   | [ADR-001](decisions/ADR-001-bufferpool-raw-pages.md), [storage/04](theory/storage/04-the-buffer-pool.md)                       |


### Two-way split, no merge — different, and it's an implementation gap, not a format divergence


This is the sentence to get exactly right, because it's the one an interviewer will test: **none of these
choices produce a file `integrity_check` rejects.** A two-way-split tree, or a tree sitting at 40%
occupancy because nothing merges siblings, is still a *valid* SQLite b-tree — occupancy and split
strategy aren't part of the format, they're properties of the algorithm that produced the bytes. So this
is "not yet implemented," not "diverges from the spec," and only the first is true. [btree/05 §5.7](theory/btree/05-why-b-trees.md), [btree/10 §10.6](theory/btree/10-deletion-and-space-reuse.md).


The concrete SQLite behavior worth knowing before claiming this gap doesn't matter: SQLite's `balance()`
skips rebalancing while free space is ≤ ⅔ of usable space — i.e. it tolerates occupancy down to ⅓, not the
textbook ½ — specifically to avoid split/merge thrashing right at the boundary. quilldb's gap is real
(files can end up larger than optimal after delete-heavy workloads) but bounded by the same kind of logic,
just without the safety valve.


---


## SQL frontend and catalog


| Area                      | quilldb                                                  | SQLite                                             | Verdict | Status | Why                                                                                                                                           |
| ------------------------- | -------------------------------------------------------- | -------------------------------------------------- | ------- | ------ | --------------------------------------------------------------------------------------------------------------------------------------------- |
| Parser                    | hand-written recursive descent                           | Lemon-generated LALR                               | 🔀       | Built  | [sql/07](theory/sql/07-from-sql-text-to-a-tree.md) — hand-written is more legible for an interview, at the cost of Lemon's ambiguity checking |
| `sqlite_schema`           | real 5-column layout at page 1, raw `CREATE` text stored | same                                               | 🟰       | Built  | [catalog/08](theory/catalog/08-the-catalog-and-binding.md)                                                                                    |
| Schema re-parse on open   | yes, from stored SQL text                                | same                                               | 🟰       | Built  | catalog/08                                                                                                                                    |
| `NULL` three-valued logic | full SQL semantics                                       | same                                               | 🟰       | Built  | [exec/09](theory/exec/09-iterator-execution.md)                                                                                               |
| Type system               | declared-type coercion, own error types                  | strict SQLite type affinity rules, own error codes | ✂️       | Built  | simple coercion covers the tested surface; exact affinity rules are compatibility trivia, not a load-bearing decision                         |


---


## Query execution and planning


| Area                                               | quilldb                                                                              | SQLite                                                              | Verdict | Status                                   | Why                                                                                                                       |
| -------------------------------------------------- | ------------------------------------------------------------------------------------ | ------------------------------------------------------------------- | ------- | ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| Execution model                                    | **Volcano iterator operators**                                                       | **bytecode VM (VDBE)**                                              | 🔀       | Built (weeks 1–3), full pipeline Planned | see below                                                                                                                 |
| Query planner                                      | **cost-based**: candidate access paths + cardinality estimates + page-oriented costs | **cost-based**: `sqlite_stat1` + logarithmic costs + NGQP/N3 search | 🟰/🔀     | Planned                                  | same pipeline, smaller search space; [plan/12 §12.5–12.6](theory/plan/12-the-query-planner.md)                            |
| Statistics (`ANALYZE`)                             | persisted `quill_stat1` using SQLite's K+1 prefix-average encoding                   | `sqlite_stat1`, optionally richer `stat4` samples                   | 🟰/✂️     | Planned                                  | same encoding, **different table name** — see below; no histograms — [plan/12 §12.5](theory/plan/12-the-query-planner.md) |
| Plan search                                        | exhaustive legal left-deep plans, maximum 3 joined tables                            | N3 polynomial heuristic for large joins                             | 🔀       | Planned                                  | 3! is six orders, so exhaustive search is simpler and optimal under quilldb's model                                       |
| Skip-scan                                          | not implemented                                                                      | yes, gated on `ANALYZE`                                             | ✂️       | Planned                                  | cost-based planning does not require every SQLite access path                                                             |
| Join algorithm                                     | **nested loop / index nested loop only**                                             | **nested loop / index nested loop only**                            | 🟰       | Planned                                  | see below                                                                                                                 |
| Automatic transient index for unindexed equi-joins | not implemented                                                                      | yes ("almost the same thing as a hash join")                        | ✂️       | Planned                                  | [exec/17 §17.3](theory/exec/17-joins.md) — the exact mechanism SQLite substitutes for a hash join                         |
| Hash join / sort-merge join                        | not implemented                                                                      | **not implemented either**                                          | 🟰       | Planned                                  | neither engine has one — [exec/17 §17.3](theory/exec/17-joins.md)                                                         |
| `ORDER BY` execution                               | **in-memory, documented row limit**                                                  | external merge sort (PMAs, spills to disk)                          | 🔀       | Planned                                  | see below                                                                                                                 |
| `GROUP BY` execution                               | **hash-based** (Python `dict`)                                                       | **sort-based only** — no hash aggregation                           | 🔀       | Planned                                  | see below                                                                                                                 |
| Deviation: bare columns in aggregate queries       | **rejected**                                                                         | allowed (documented extension, arbitrary row returned)              | 🔀       | Planned                                  | stricter than the reference; documented, not accidental — [week-7 spec §40](implementation/week-7-query-processing.md)    |


### `quill_stat1`, not `sqlite_stat1` — same bytes inside, deliberately different name


The one place quilldb keeps SQLite's *encoding* but rejects its *name*, which is worth explaining because
everywhere else the naming is copied exactly — the catalog really is `sqlite_schema`, not `quill_schema`.


The `stat` strings are byte-identical to SQLite's: K+1 integers, row count first, average rows per prefix
after. Only the table name differs, and that's the point. quilldb's files are meant to be opened by the
real `sqlite3` CLI as a reference validator. If these rows lived in `sqlite_stat1`, **real SQLite would
read them and plan with them** — trusting numbers produced by a different `ANALYZE` implementation with a
different definition of "row count" after a crash or a partial write. The failure would be silent and it
would look like a SQLite bug. Under a name SQLite doesn't recognize, the two planners stay independent:
`sqlite3` ignores `quill_stat1` entirely and will create its own `sqlite_stat1` if you run `ANALYZE` there,
which is the correct outcome — two engines, two sets of statistics, no shared mutable state.


The cost, stated plainly: without the `sqlite_` prefix it is an ordinary **user-visible** table. It shows
up in `.tables` and in a `SELECT * FROM quill_stat1`, where SQLite's is hidden as internal. That's a
feature during development — you can read and hand-edit the statistics to test plan flips, which is
exactly what the week-4 "change only the statistics" test does — and a small wart in a demo. Worth it.


### Iterator pipeline, not bytecode — different, chosen for legibility over performance


A Volcano-style operator tree (`open`/`next`/`close`) is roughly a third the code of a register machine
plus a compiler, gives you `EXPLAIN` as a tree-print for free, and is closer to how Postgres executes —
which makes it more transferable to a backend interview than VDBE trivia. The cost is real: bytecode
amortizes per-row dispatch overhead that a tree of Python method calls pays every time. [exec/09](theory/exec/09-iterator-execution.md).


### Same join algorithm family — and it's a documented SQLite decision, not an accident


SQLite's own optimizer docs state plainly: *"SQLite implements joins as nested loops."* There's a section
titled "Hash Joins" whose entire content is the argument for *not* building one — a B-tree it already has
and has hardened for 25 years gets ~90% of a hash join's benefit via a transient index, at the cost of a
second data structure the library would have to test, tune, and carry. quilldb reaches the same nested-loop-only
answer by the same reasoning, minus the transient-index fallback, which is next on the descope ladder if
time allows. [exec/17 §17.3](theory/exec/17-joins.md).


### In-memory sort with a documented limit — a scope cut stated as a limit, not hidden as a bug


SQLite's external merge sort (`vdbesort.c`) handles inputs larger than memory by spilling sorted runs
("PMAs") to a temp file and merging them, in a hierarchy if there are many. Building that is real, useful
work — but it's dwarfed in interview value by the crash matrix and the concurrency stress test, so the
roadmap scopes `Sort` as in-memory with a hard row limit that raises a clear, actionable error rather than
exhausting memory. A documented limit is a limitation; an OOM kill is a bug. [exec/18 §18.1–18.2](theory/exec/18-sorting-and-aggregation.md).


### Hash aggregation — a deliberate, *reasoned* divergence, not a default


This is the one place quilldb argues past SQLite on purpose. SQLite has **no hash-table-based query
operator anywhere** — confirmed by grepping all of `select.c` for "hash" and finding only
identifier-resolution tables. `GROUP BY` is sort-or-index only, for the same code-size reasoning as the
join decision above: a C library aimed at embedded devices doesn't want a second general-purpose data
structure for one operator's benefit. **Python has a hash table for free** — a `dict` — so the tradeoff
that makes SQLite's choice correct doesn't apply to quilldb. Using one here is the right call *because*
the constraint that produced SQLite's choice is absent, which is a stronger statement than either copying
SQLite blindly or diverging without explaining why. [exec/18 §18.6](theory/exec/18-sorting-and-aggregation.md).


---


## Transactions, durability, and concurrency


| Area                            | quilldb                                             | SQLite                                                                      | Verdict | Status  | Why                                                                                                                                     |
| ------------------------------- | --------------------------------------------------- | --------------------------------------------------------------------------- | ------- | ------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| Durability strategy             | **undo (rollback) journal**                         | undo journal **by default**, WAL (redo) opt-in                              | 🟰       | Planned | quilldb builds the default mode only — [txn/13 §13.3](theory/txn/13-atomic-commit-and-the-journal.md)                                   |
| Commit-point mechanism          | **delete a file = commit**                          | same                                                                        | 🟰       | Planned | the one atomic primitive both engines rely on — [txn/13 §13.2](theory/txn/13-atomic-commit-and-the-journal.md)                          |
| fsync ordering                  | journal body → dir → header → data → delete journal | same sequence                                                               | 🟰       | Planned | [txn/13 §13.4](theory/txn/13-atomic-commit-and-the-journal.md)                                                                          |
| Journal validity guards         | magic + `nRec` both withheld until synced           | same                                                                        | 🟰       | Planned | [txn/13 §13.6](theory/txn/13-atomic-commit-and-the-journal.md)                                                                          |
| Per-record checksum             | sampled additive sum (not a CRC), same algorithm    | same                                                                        | 🟰       | Planned | reimplemented and reproduced 25/25 real stored checksums — [txn/13 §13.7](theory/txn/13-atomic-commit-and-the-journal.md)               |
| **Journal file format (bytes)** | **own magic, own layout**                           | real `-journal` byte format                                                 | 🔀       | Planned | see below                                                                                                                               |
| WAL                             | not implemented                                     | opt-in                                                                      | ✂️       | Planned | needs a shared-memory wal-index and a checkpointer — 25+ hours for a second durability mechanism, not a second talking point            |
| Isolation level (target)        | **serializable**                                    | **"serializable"** (their scare quotes)                                     | 🟰       | Planned | both, by different mechanisms — see below                                                                                               |
| Locking mechanism               | **table-level shared/exclusive, strict 2PL**        | **whole-file, 5-state ladder** (UNLOCKED→SHARED→RESERVED→PENDING→EXCLUSIVE) | 🔀       | Planned | see below                                                                                                                               |
| Deadlock handling               | **detection** (wait-for graph, abort youngest)      | **avoidance** (fixed lock order, fail-fast `SQLITE_BUSY`)                   | 🔀       | Planned | [txn/16 §16.4, §16.5](theory/txn/16-locking-and-deadlock.md)                                                                            |
| Concurrency scope               | threads, one process                                | threads or processes, via `fcntl`                                           | ✂️       | Planned | multi-process is *harder* than multi-thread, not more advanced — locks per-process not per-thread, broken over NFS, no cleanup on crash |


### Same algorithm, different bytes — the journal is the clearest example of the whole rule


The commit-point insight, the fsync order, the two born-invalid guards, and the checksum algorithm are all
copied from SQLite exactly, because they're the actual engineering content of week 5. The **byte layout**
(magic bytes, field order) is quilldb's own, because the one-directional acceptance test never requires
`sqlite3` to read a quilldb journal — only to read the *database file* quilldb produces. Where the
one-directional scope removes a requirement, quilldb takes the cheaper path; where it doesn't (the
database file itself), quilldb matches exactly. [txn/13 §13.11](theory/txn/13-atomic-commit-and-the-journal.md).


### Same target, different mechanism — SQLite serializes writers, quilldb locks tables


SQLite's rollback-journal mode reaches "serializable" by removing concurrency: one writer at a time,
enforced by whole-file locks, so no write-write interleaving can exist to be inconsistent. quilldb reaches
the same isolation level with **table-level strict two-phase locking** instead — finer-grained, because a
single process with shared memory can afford a real lock manager where SQLite, coordinating across OS
processes through the filesystem, cannot. The phantom-read anomaly, which normally needs gap locks or
MVCC to eliminate, falls out for free at table granularity: a shared lock on the *whole table* blocks any
insert into the range you read. [txn/15 §15.6](theory/txn/15-isolation-and-anomalies.md), [txn/16 §16.1](theory/txn/16-locking-and-deadlock.md).


### Detection vs. avoidance — a genuine algorithmic choice, argued in both directions


SQLite prevents deadlock structurally: a fixed `PENDING → EXCLUSIVE` escalation order plus immediate
`SQLITE_BUSY` on contention means a cycle can never form. quilldb instead **allows** transactions to wait
and **detects** cycles in a wait-for graph, aborting the youngest transaction in the cycle. Detection
permits strictly more concurrency — nothing aborts unless a real cycle exists — at the cost of maintaining
the graph and being prepared to abort and retry. Both are legitimate; which one you'd pick depends on
whether false-positive aborts (avoidance) or bookkeeping (detection) cost you more. [txn/16 §16.4](theory/txn/16-locking-and-deadlock.md).


---


## Everything cut outright, and the one-line reason


| Feature                                     | Reason                                                                                                                                                  |
| ------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| WAL                                         | needs a wal-index + checkpointer; the undo journal already earns the atomicity/recovery story                                                           |
| `auto_vacuum` / `VACUUM`                    | needs a page back-pointer the base format doesn't carry                                                                                                 |
| Multi-process locking (`fcntl`)             | harder than multi-thread, not more advanced; threads were the stated goal                                                                               |
| `WITHOUT ROWID`                             | schema feature, not needed to hit the format acceptance test                                                                                            |
| Non-4096 page sizes                         | one config to verify instead of a matrix; not required by the format                                                                                    |
| UTF-16                                      | UTF-8 covers every test; declared and refused, not silently misread                                                                                     |
| `stat4` histograms / SQLite N3-scale search | prefix averages and exhaustive search are sufficient for quilldb's three-table limit; skew and correlated columns remain documented estimation limits   |
| Hash join, sort-merge join                  | SQLite doesn't have them either — same reasoning, not a gap relative to the reference                                                                   |
| External (spilling) sort                    | in-memory with a documented, actionable limit; the crash matrix is the higher-value use of the hours                                                    |
| `NOCASE` / `RTRIM` collations               | `BINARY` covers the tested surface; a `BINARY` index can't serve a `NOCASE` query anyway, so adding collations doesn't remove the underlying constraint |


---


## Where the reasoning actually lives


This page is an index. Every "why" above is argued in full — with the empirical verification, the
counterexample, and the "say this out loud" version — in its linked chapter under `docs/theory/`, and the
signatures/tests to build against are in the matching `docs/implementation/week-N-*.md`. The ADRs in
`docs/decisions/` are the fullest record for decisions that have one written; not every row above has an
ADR yet (see [`week-8-presentation.md` §48](implementation/week-8-presentation.md) for the planned list) —
until it does, the chapter is the source of truth.