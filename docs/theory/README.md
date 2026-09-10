# quilldb — Theory


The fourth doc, and the one that answers *"but why?"*


The other three docs each do a different job:


| Doc | Job | What it doesn't do |
|---|---|---|
| `roadmap.md` | What to build, in what order, with hour budgets | Explain the ideas |
| `guide.md` | Teach the concepts with analogies, split into 2-hour sessions | Go deep enough to defend in an interview |
| `docs/implementation/` | Signatures and tests to code against | Say why the signatures look like that |
| `references.md` | Where to read more | Contain the knowledge — it's a map, not the territory |
| **`docs/theory/` (this)** | **The actual knowledge: the physics, the theory, the design space, and why SQLite picked what it picked** | Tell you what to type |


If `references.md` says *"read atomiccommit.html §3.7–3.11 to learn the fsync order,"* this directory says *"here is the fsync order, here is the failure that each step prevents, here is what SQLite assumes about your hard drive that makes it work, and here is what happens when that assumption is false."*


---


## How these chapters are written


**Assumed background: none.** You know Python. You have never thought about how a file physically reaches a disk, what a "block device" is, or why databases exist. Every term is defined the first time it appears.


**Each section is a question, not a lecture.** The format is deliberate and it repeats:


> **The problem** — a concrete situation you'd actually hit
> **Your first instinct** — the design you'd reach for, stated fairly
> **Why it breaks** — the specific failure, with numbers
> **What real systems do** — the design that survives
> **Why that works** — the mechanism
> **The road not taken** — who chose differently, and the constraint that made them right
> **Say this out loud** — the two-sentence interview version


Read the problem, stop, and actually try to answer it before reading on. The reveal only sticks if you've committed to a wrong answer first. This is why the chapters are ~40% longer than a straight explanation would be — that length *is* the teaching method.


**Everything is sourced.** Claims about SQLite's internals are footnoted to primary sources (the file-format spec, `atomiccommit.html`, `btreeInt.h`, `arch.html`). Where SQLite's documentation is *thinner* than the folklore around it, this is said explicitly — see §0.4 on page size for an example. You should be able to defend every claim here without having read anything else.


---


## Layout


This directory mirrors `src/quilldb/`, so the theory for a package sits in a folder with the
package's name. When you open `src/quilldb/txn/journal.py` and wonder *why the fsync order is
that order*, the answer is in `docs/theory/txn/`.


| Folder | Explains the code in | Chapters |
|---|---|---|
| [`foundations/`](foundations/) | nothing — read it before you write any code | 00 |
| [`storage/`](storage/) | `storage/` — `pager`, `page`, `bufferpool`, `overflow`, `freelist` | 01, 02, 04 |
| [`codec/`](codec/) | `codec/` — `varint`, `record` | 03 |
| [`btree/`](btree/) | `btree/` — `btree`, `cells`, `cursor`, `split`, `index` | 05, 06, 10, 11 |
| [`sql/`](sql/) | `sql/` — `tokenizer`, `ast`, `parser` | 07 |
| [`catalog/`](catalog/) | `catalog/` — `schema`, `catalog` — plus `sql/binder.py` | 08 |
| [`plan/`](plan/) | `plan/` — `planner`, `explain` | 12 |
| [`exec/`](exec/) | `exec/` — `operators`, `expressions`, `join`, `aggregate`, `sort` | 09, 17, 18 |
| [`txn/`](txn/) | `txn/` — `transaction`, `journal`, `recovery`, `locks` | 13, 14, 15, 16 |
| [`benchmarks/`](benchmarks/) | `benchmarks/` | 19 |


**Chapter numbers are global and follow reading order, so folders have gaps.** `btree/` jumps from
06 to 10 because deletion and indexes arrive in week 4, after the SQL chapters. Folder order is
*not* reading order — the table at the bottom of this file is.


Two folders deliberately don't match a package one-to-one. `foundations/` has no package because
it's about the hardware. And chapter 08 lives in `catalog/` although it also covers
`sql/binder.py`, because binding is a thing you do *to* the catalog and splitting them would make
both halves incomprehensible.


---


## Chapters


### Part 0 — The Ground Floor


| # | Chapter | What you'll be able to explain afterwards |
|---|---|---|
| 00 | [Foundations: the machine you're actually writing to](foundations/00-foundations.md) | Why storage is slow, why you can't write one byte, why `write()` doesn't mean "written," what atomicity means at the hardware level, and the three eternal problems every database solves |


**Read chapter 00 before week 1, and don't skip it.** Every design decision in the following seven chapters is a response to something in it. If you read only this chapter you'll still understand *why databases are shaped like databases*, which is most of the value.


### Part 1 — Week 1: Storage


| # | Chapter | What you'll be able to explain afterwards |
|---|---|---|
| 01 | [Pages and the pager](storage/01-pages-and-the-pager.md) | Why fixed-size numbered pages beat every alternative, why the pager indirection is the most valuable line you draw all project, and how the freelist works |
| 02 | [The slotted page](storage/02-the-slotted-page.md) | How variable-length rows fit in a fixed-size box, why two regions grow toward each other, and what fragmentation actually costs |
| 03 | [Encoding: varints and records](codec/03-encoding-varints-and-records.md) | Why numbers aren't stored as 8 bytes, how a manifest-then-body layout lets you read column 5 without decoding columns 1–4, and the endianness trap |
| 04 | [The buffer pool](storage/04-the-buffer-pool.md) | Caching theory from first principles, why LRU is wrong for table scans, what pin counts protect against, and the dirty-page rule that data loss hides behind |


### Part 2 — Week 2: The B+Tree


| # | Chapter | What you'll be able to explain afterwards |
|---|---|---|
| 05 | [Why B-trees](btree/05-why-b-trees.md) | The fanout arithmetic that makes 125 million rows reachable in 4 reads, why not a binary tree, why not a hash table, why the "+" matters, and where LSM-trees beat you |
| 06 | [B-tree mechanics](btree/06-b-tree-mechanics.md) | Cells, cursors, the split cascade, why trees grow upward from the root, overflow chains, and the invariants your validator asserts |


### Part 3 — Week 3: SQL to Rows


| # | Chapter | What you'll be able to explain afterwards |
|---|---|---|
| 07 | [From SQL text to a tree](sql/07-from-sql-text-to-a-tree.md) | Why tokenization and parsing are separate, how an AST preserves meaning, how Pratt binding powers encode precedence, and why quilldb handwrites a parser while SQLite uses Lemon |
| 08 | [The catalog and binding](catalog/08-the-catalog-and-binding.md) | How page 1 bootstraps a self-describing database, why SQLite stores CREATE text, what the schema cookie invalidates, and why names and parameters disappear before execution |
| 09 | [Iterator execution](exec/09-iterator-execution.md) | How open/next/close streams rows through composable operators, how SQL's three-valued NULL logic works, how resources follow cursor lifetime, and why SQLite's bytecode VM is a valid alternative |


### Part 4 — Week 4: Mutation, Indexes, and Planning


| # | Chapter | What you'll be able to explain afterwards |
|---|---|---|
| 10 | [Deletion and space reuse](btree/10-deletion-and-space-reuse.md) | Why deleting a row makes the file bigger before it makes it smaller, what a freeblock is and when it's reclaimable, why SQLite merges siblings and quilldb doesn't, and why databases don't shrink |
| 11 | [Index B-trees](btree/11-index-b-trees.md) | Why an index is the same B-tree with the payload thrown away, why the rowid is glued onto every key, what makes a covering index twice as fast, and what `WITHOUT ROWID` really changes |
| 12 | [The query planner](plan/12-the-query-planner.md) | What "sargable" means and why `WHERE lower(x)='a'` can't use an index, the leading-column-no-gaps rule and why gaps are fatal, and what `sqlite_stat1` buys a cost-based planner that a rule-based one can't have |


### Part 5 — Week 5: Durability


| # | Chapter | What you'll be able to explain afterwards |
|---|---|---|
| 13 | [Atomic commit and the journal](txn/13-atomic-commit-and-the-journal.md) | Why the commit point is *deleting a file*, the exact fsync order and the specific crash each barrier stops, why the journal holds old pages rather than new ones, and what WAL trades away to be faster |
| 14 | [Crash recovery](txn/14-crash-recovery.md) | What makes a journal "hot," why replay must be idempotent, why crashing *during* recovery is the case that finds real bugs, and how to turn a durability claim into evidence |


### Part 6 — Week 6: Concurrency


| # | Chapter | What you'll be able to explain afterwards |
|---|---|---|
| 15 | [Isolation and the anomalies](txn/15-isolation-and-anomalies.md) | The four anomalies as concrete interleavings rather than vocabulary, what serializability actually means, which level forbids which anomaly, and how to name yours honestly |
| 16 | [Locking, 2PL, and deadlock](txn/16-locking-and-deadlock.md) | Why the *second* phase of two-phase locking is the load-bearing half, locks vs. latches, deadlock detection vs. avoidance vs. prevention, and why SQLite's 5-state ladder needs `PENDING` |


### Part 7 — Week 7: Query Processing


| # | Chapter | What you'll be able to explain afterwards |
|---|---|---|
| 17 | [Joins](exec/17-joins.md) | Why a join is a nested loop before it's anything else, what an index turns the inner loop into, when hash and merge joins win, and why SQLite ships only one join algorithm on purpose |
| 18 | [Sorting and aggregation](exec/18-sorting-and-aggregation.md) | How to sort more data than you have RAM, why an index can delete the sort step entirely, sort-based vs. hash-based grouping, and why `LIMIT` changes the algorithm rather than just the output |


### Part 8 — Week 8: Presentation


| # | Chapter | What you'll be able to explain afterwards |
|---|---|---|
| 19 | [Measuring it](benchmarks/19-measuring-it.md) | Why you report page reads and not milliseconds, how warm caches produce numbers that are real but meaningless, and how to state a result an interviewer can't poke a hole in |


---


## Reading order and timing


Do **not** read far ahead. Read chapter 00 now, then one part per week. Reading week-5 theory in
August is procrastination that feels like diligence — you'll have forgotten it by October and
you'll re-read it anyway.


| When | Read | Time |
|---|---|---|
| Before you write any code | 00 | 60–75 min |
| Start of week 1 | 01, 02 | 50 min |
| Week 1, before the codec sessions | 03 | 30 min |
| Week 1, before the buffer pool session | 04 | 35 min |
| Start of week 2 | 05 | 40 min |
| Week 2, before the split sessions | 06 | 50 min |
| Start of week 3 | 07 | 40 min |
| Week 3, before catalog + binding | 08 | 40 min |
| Week 3, before the executor | 09 | 45 min |
| Week 4, before B-tree delete | 10 | 35 min |
| Week 4, before index B-trees | 11 | 45 min |
| Week 4, before the planner | 12 | 45 min |
| **Start of week 5, before any code** | **13** | **55 min** |
| Week 5, before the crash matrix | 14 | 40 min |
| **Start of week 6, before any code** | **15** | **40 min** |
| Week 6, before the lock manager | 16 | 50 min |
| Week 7, before the join operator | 17 | 40 min |
| Week 7, before sort + aggregate | 18 | 40 min |
| Week 8, before writing benchmarks | 19 | 25 min |


Total ≈ 13 hours across eight weeks, which comes *out of* the reading budget in `references.md`
rather than adding to it. These chapters replace most of the external reading; the external
sources remain listed for when you want the primary text.


Chapters 13 and 15 are the two marked **read before you write any code that week**, and the
reason is the same for both: weeks 5 and 6 are the only weeks where a design mistake made in the
first hour cannot be refactored out later. Everywhere else you can start typing and let the theory
catch up.


---


## A note on the "roads not taken" sections


Every chapter has one, and they are not padding. They're the highest-value paragraphs in the directory for your actual goal.


Interviews rarely ask *"how does a B-tree work?"* — that's a fact you either know or don't. They ask *"why did you choose that?"* and *"what would you do differently?"* Those questions are only answerable if you know what the alternatives were and what constraint eliminated each one. A candidate who says "I used a B+tree because that's what databases use" and a candidate who says "I used a B+tree rather than an LSM-tree because my workload is read-heavy with in-place updates, and LSM's write advantage comes from deferring work I'd then have to build compaction to pay off" have the same code and wildly different outcomes.


The roads-not-taken sections are also where your ADRs (`docs/decisions/`) come from. Write the ADR the week you read the chapter, not in week 8.


---


## Relationship to `guide.md`


`guide.md` stays exactly as it is, and you should still use it — it's the session-by-session driver with the binder analogy that carries the whole project. These chapters go *underneath* it. Where `guide.md` says "the page is full when the two regions meet," chapter 02 explains what happens to the bytes that are free but unusable, why SQLite tracks "fragments" separately from "freeblocks," and why the number 60 appears in the spec.


Two places where these chapters deliberately **correct or refine** `guide.md`, so you don't get whiplash:


1. `guide.md` §1.5 says going to disk is "roughly 10,000 times longer" than RAM. That's right for a spinning disk and about 20× too pessimistic for the NVMe SSD you're actually running on. Chapter 00 gives real numbers with ranges. The *conclusion* is unchanged — caching is still the single biggest lever — but if you quote "10,000×" about an SSD in an interview, someone will correct you.
2. `guide.md` §1.3 says varints are "big-endian (most significant bits first, what SQLite does)." True, but incomplete in a way that will bite you: SQLite's 9th byte contributes all 8 bits rather than 7, which is why the maximum is 9 bytes and not 10. Chapter 03 §3.4 derives it; `constants.py` now says `MAX_VARINT_BYTES = 9`.


---


## The format decision these chapters assume


**quilldb writes the real SQLite on-disk format, byte for byte.** Not a format inspired by it — the actual one, with the `"SQLite format 3\000"` magic, SQLite's 100-byte header layout, page 1 doubling as the `sqlite_schema` root, 8-or-12-byte page headers, page types 2/5/10/13, 9-byte varints, trunk-page freelists, and SQLite's overflow threshold formulas.


**The acceptance test is one line:** `sqlite3 yourfile.db "PRAGMA integrity_check"` returns `ok`.


Three things follow from that, and they shape how every chapter is written:


1. **Where the format speaks, there is nothing to decide.** Chapter 03's old "which max varint length should I pick?" is gone; it's 9. What remains is *why* it's 9, which is the part worth knowing.
2. **You get a reference implementation to test against, for free.** This is the real prize, and chapter 06 §6.6 makes the argument: `integrity_check` is an adversarial validator written by people who have been finding B-tree bugs since 2000. It's worth more than the interop.
3. **Format fidelity and feature completeness are different things.** quilldb still doesn't do three-way rebalancing, page merging, WAL, `VACUUM`, or a bytecode VM. None of those change the bytes — a two-way-split tree is a *valid* SQLite tree, just less dense. So those are **implementation gaps**, not divergences, and chapters 01 §1.8, 02 §2.4, and 05 §5.7 are careful about the distinction. It matters when you describe the project: "not yet implemented" and "deliberately different from the spec" are very different claims, and only one of them is true here.


Where a chapter explains a design you would plausibly have done differently — page 1's double duty, the 8-vs-12-byte header, no zigzag varints, two-level freelists — it now explains **SQLite's reasoning and what it cost them**, rather than justifying a departure. That's what §1.6, §2.5, §3.5, and §1.8 are for.