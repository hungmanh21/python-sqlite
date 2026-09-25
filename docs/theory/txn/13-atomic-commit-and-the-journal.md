# 13 — Atomic Commit and the Journal


> **Read at the START of week 5, before you write any code.** Week 5 is one of only two weeks where a
> design mistake made in the first hour cannot be refactored out later.
>
> **Time:** ~55 minutes. **Prerequisites:** [chapter 00 §0.6](../foundations/00-foundations.md) (what
> `write()` does and doesn't promise), [chapter 06 §6.4](../btree/06-b-tree-mechanics.md) (the split
> cascade).


---


## 13.0 The problem: a crash doesn't lose a row, it loses the tree


Week 2 built page splitting. Look at what one root split physically does:


```
1. write the new left leaf            page 7
2. write the new right leaf           page 8
3. write the root, now an interior    page 2  ── pointing at 7 and 8
```


Three page writes. Now lose power after step 2.


Page 2 is still the *old* leaf. Pages 7 and 8 contain half the rows each. Nothing points at them, so
the rows they hold are gone — that's bad but comprehensible, it's data loss.


Now lose power after step 3 but before step 1 completes (the OS is free to reorder — chapter 00 §0.6).
Page 2 is now an interior page whose children are pages 7 and 8. Page 7 contains **whatever was there
before** — an old freelist trunk, a stale leaf, zeros. You descend the tree, read page 7's first byte
as a page type, and get `0x00`. Not a valid page type.


> **You didn't lose a row. You lost the ability to read the file at all.** Every row in the database is
> now unreachable because one pointer points at garbage.


That's the difference between a database and a file format, and it's why week 5 is not an optional
polish week. Week 4 made it strictly worse: one `INSERT` now writes a table page *plus* a page in every
index, and a partial application leaves indexes claiming rows that don't exist.


**So: how do you make N page writes happen all-or-nothing, on hardware that gives you no such
guarantee?** This is the central question of the week and the single best interview topic in the
project. Think about it before reading on — most first answers are a variation of one idea, and it
doesn't work.


---


## 13.1 Your first instinct: write them in a careful order


"Write the children before the parent. Then a crash mid-way leaves an old-but-consistent tree — the
new leaves are orphaned, wasting space, but the tree still reads."


This is genuinely good thinking, and it's the right instinct: *ordering* is a real tool, and you'll use
it in §13.4. But as a complete solution it fails three ways, and each failure is worth knowing
separately.


**1. Ordering doesn't survive the OS.** From chapter 00 §0.6: `write()` copies into the page cache and
returns. The kernel writes back whenever it likes, in whatever order it likes. Your careful sequence is
a sequence of *`write()` calls*, not of disk writes. Without an explicit barrier the disk may well
receive step 3 first.


**2. Some operations have no safe order.** A split cascade updates a leaf, its parent, its
grandparent, and the root. Deleting a row updates a table page and three index pages. There is no
ordering of "table page updated, index page not yet" that is consistent — the index claims a row the
table no longer has. **Consistency isn't a property of one page; it's a property of the set**, and no
sequencing of individual writes gives you a property of the set.


**3. A single page write isn't even atomic.** Chapter 00 §0.3: the disk's atomic unit is a sector, 512
or 4096 bytes. A 4096-byte page write can be *torn* — half new, half old. So "write the child fully
before the parent" isn't available; you can't write anything "fully" in one indivisible step.


> The instinct to salvage: **ordering plus barriers can give you all-or-nothing, but only if there is
> one single step that flips the whole thing from "old" to "new."** Ordering alone gives you a gradient.
> You need a cliff.


---


## 13.2 The one atomic thing you actually have


So what *is* atomic on this machine? Chapter 00 said: a single sector write, probably, if the drive
doesn't lie. That's 512 bytes. Not enough to hold a page, let alone a transaction.


But there's another candidate, and it's in a different layer of the system entirely: **the filesystem's
own metadata operations.** Does the file `mydb-journal` exist? That question has exactly two answers,
and a journaling filesystem is built to guarantee that `unlink()` either happened or didn't — because
its *own* integrity depends on that.


This is the pivot the whole design turns on:


> **Don't try to make your data writes atomic. Find one bit that already flips atomically, and make
> that bit mean "committed."**


SQLite chooses the *existence of a file*. And its documentation names the moment exactly:


> "After the database changes are all safely on the mass storage device, the rollback journal file is
> deleted. **This is the instant where the transaction commits.** If a power failure or system crash
> occurs prior to this point, then recovery processes to be described later make it appear as if no
> changes were ever made to the database file."


Read that twice, because it inverts the intuitive model. **Writing your data is not committing.** All
the page writes happen *before* the commit point. The commit is a separate, tiny, atomic act performed
afterward — and until it happens, all that carefully written data is officially deniable.


**This is a transferable design pattern**, and naming it is worth more than the SQLite fact:


| System | The atomic bit | Everything before it is |
|---|---|---|
| SQLite rollback journal | journal file is deleted | deniable — will be undone |
| Git | the ref file is updated (one small atomic rename) | unreferenced objects, invisible |
| LMDB / COW b-trees | the root pointer is swapped | a whole new tree nobody can see |
| Blue-green deploy | the load balancer's pointer moves | a fully built, unreferenced environment |
| `os.replace()` on a temp file | the rename | a temp file nobody reads |


All five say the same thing: **do all the expensive, non-atomic work somewhere deniable, then flip one
pointer.** Once you see the shape you'll use it in code that has nothing to do with databases.


---


## 13.3 Undo or redo: which version of the page goes in the journal?


You're going to keep a sidecar file. What goes in it — the pages as they *were*, or as they *will be*?


Here's the problem both strategies are answering, stated once so the rest of this section isn't just a
list of trade-offs. A transaction crashes somewhere in the middle. On restart you have a set of pages
that partially reflect the transaction and partially don't — some old, some new, in a mix that depends
on exactly which writes reached disk before power died. Recovery has to force every page into **one**
consistent state: either **all pre-transaction** (the transaction never happened) or **all
post-transaction** (it fully happened). You cannot tell which one a page *should* be just by looking at
it — you need a separate record of the *other* version, the one that currently isn't on disk. That
record is the journal, and there are exactly two things it can hold:


- **The version about to be overwritten** (undo) — so that if the transaction turns out to be
  incomplete, you can put it back and pretend nothing happened.
- **The version about to replace it** (redo) — so that if the transaction turns out to be complete, you
  can (re)apply it even though the database file never received the final bytes.


That's the motivation for *both* existing as ideas: undo answers "how do I make an unfinished
transaction disappear," redo answers "how do I make a finished transaction stick even though the disk
copy is incomplete." Every crash-recovery scheme is built from one of these two primitives, or — in
ARIES's case, §13.10 — both at once. Naming which one a system uses tells you immediately what its
recovery routine does on startup: undo means "erase the losers," redo means "finish the winners."


Both work, and the choice determines everything else about your design.


| | **Undo** (rollback journal) | **Redo** (write-ahead log) |
|---|---|---|
| Journal holds | the **original** page content | the **new** page content |
| Database file during a txn | modified in place | untouched |
| To commit | delete the journal | write a commit record to the log |
| To roll back | copy the journal back over the db | do nothing — the db was never touched |
| After a crash | **undo**: restore old pages | **redo**: replay new pages onto the db |
| Reader sees | the db file, which is *being* modified | the db file plus a log it must consult |
| SQLite calls it | `journal_mode=DELETE` (default) | `journal_mode=WAL` |


### Trade-offs at a glance

Five axes, and undo and redo split them almost evenly — which is *why* this is a real design decision
and not an obviously-correct one:

| Axis | Winner | Why |
|---|---|---|
| **Implementation complexity** | **Undo** | database file is always the truth; no log-lookup, no wal-index, no checkpointer |
| **Commit latency / write cost** | **Redo** | commit is a sequential append + one sync; the expensive random writes into the db file are deferred to a batched checkpoint |
| **Reader/writer concurrency** | **Redo** | the db file isn't touched mid-transaction, so readers never have to be locked out of it (§13.9's PENDING/EXCLUSIVE dance is a rollback-journal-only problem) |
| **Disk footprint during a transaction** | **Undo** | the journal covers exactly the pages this one transaction dirtied, and is deleted the instant it commits; a WAL file accumulates across *many* transactions until a checkpoint runs |
| **Torn-page recovery** | **Undo** | stores whole original pages, so a torn page is just overwritten wholesale on rollback; a redo log storing deltas needs a special case — PostgreSQL's `full_page_writes` — because a delta can't repair an unknown starting state (§13.10) |

Notice undo wins on *simplicity* and *bounded resource use per transaction*, redo wins on *throughput*
and *concurrency*. Neither list is longer than the other — which is exactly why production databases
that need maximum concurrency (Postgres, MySQL/InnoDB) accept redo's complexity, while SQLite ships
undo as the default and makes WAL an opt-in for people who specifically need concurrent readers.


**Why undo is the simpler thing to build, and therefore the right week-5 choice.** With an undo
journal, the database file is *always the live database*. A reader opens it and reads pages. There is no
second place data might be hiding, no log to search, no mapping from page number to "wherever the
latest version of that page currently lives." Every read path you wrote in weeks 1–4 keeps working
unchanged.


With a redo log, the newest version of page 47 might be in the log rather than the file, so every single
page read has to first ask "is there a newer copy in the log?" That question needs an index over the
log, kept in shared memory, coordinated between processes — which is exactly why SQLite's WAL mode
needs a `-shm` file and a whole extra subsystem.


**And why redo is faster, which is why WAL exists.** Commit with a redo log means appending to a file
and syncing it — sequential writes, and the expensive random writes into the database file are deferred
to a *checkpoint* that batches them. Commit with an undo journal means writing the old pages, syncing,
then writing the new pages into their scattered homes, then syncing again. Undo pays random-write cost
on every commit; redo defers and batches it.


There's a third consequence that matters more than speed, and it's the real reason WAL is popular:
**with a redo log, the database file isn't being modified during a write, so readers don't need to be
locked out.** That's chapter 15's topic, and it's the single biggest practical difference between the
two modes.


> **Say this out loud:** "A rollback journal is undo logging: it stores the *old* page contents, the
> database is modified in place, and commit means deleting the journal. A WAL is redo logging: it stores
> the *new* contents, the database file is untouched during the transaction, and commit means writing a
> frame to the log. Undo keeps every read path simple because the database file is always the truth;
> redo makes commits sequential and lets readers run concurrently with a writer, at the cost of needing
> an index over the log and a checkpoint process."


**So which one does quilldb build, and why?** The rollback (undo) journal — for exactly the
"simpler to build" reason above, not because redo is wrong. Undo keeps the database file as the single
source of truth for every reader at every moment, so none of the read-path code from weeks 1–4 has to
change: a page read is still just "go get page N." Redo would require every page read to first ask "is
there a newer version of this page sitting in a log?", plus a shared index over that log so concurrent
readers and writers agree on what "newer" means — that's real infrastructure (SQLite's `-shm` file and
wal-index), not a small addition. §13.10 has the full cost/benefit case, including WAL's fsync-count
numbers, once you've seen the whole commit sequence in §13.4 to compare it against.


---


## 13.4 The sequence, and the specific crash each step prevents


Here is the whole commit, as SQLite's atomic-commit documentation numbers it. **The right-hand column is
the part to learn** — every step exists to stop one specific failure, and if you can name the failure
you can reconstruct the sequence from first principles instead of memorizing it.


| § | Action | I/O | The failure it prevents |
|---|---|---|---|
| 3.2 | Acquire SHARED | — | reading a torn mix of pre- and post-change pages |
| 3.3 | Read the pages you need | reads | — |
| 3.4 | Acquire RESERVED | — | a second concurrent writer |
| 3.5 | Create the journal; write the original pages and a header recording the **original db size** | write | being unable to undo — and being unable to undo *file growth* |
| 3.6 | Modify the pages in memory | — | — |
| 3.7 | **fsync the journal**, write `nRec`, **fsync again** | 2 fsyncs | committing on top of a journal that isn't fully on disk |
| 3.8 | PENDING → EXCLUSIVE | — | new readers arriving mid-write; writer starvation |
| 3.9 | Write the changed pages into the database | writes | — |
| 3.10 | **fsync the database** | fsync | the journal being deleted while the data is still in the page cache |
| 3.11 | **Delete the journal** | unlink | ← **THIS IS THE COMMIT POINT** |
| 3.12 | Release EXCLUSIVE and PENDING | — | — |


### The lock states in that table, decoded

Steps 3.2, 3.4, and 3.8 name four lock states without defining them — here's just enough to follow this
chapter. (The full compatibility matrix, why PENDING specifically prevents writer starvation, and what
happens when locks contend are chapter 16's job — [16 — Locking and deadlock](16-locking-and-deadlock.md).)

Think of the lock as a single value stamped on the whole database file, escalating one step at a time as
a transaction gets more serious about writing:

| State | Means | Who else can be doing what |
|---|---|---|
| **UNLOCKED** | not touching the file | anyone, anything |
| **SHARED** | reading | any number of other **SHARED** readers, simultaneously |
| **RESERVED** | "I intend to write eventually, but I'm still only reading so far" | existing **SHARED** readers keep reading; no *other* writer may also take **RESERVED** |
| **PENDING** | "I'm about to write — existing readers, please finish up" | current **SHARED** holders may finish; no *new* **SHARED** locks are granted |
| **EXCLUSIVE** | actually writing the database file | nobody — the only state that locks out everyone else |

Read §13.4's four lock-related steps again with this table next to them:

- **SHARED** (3.2) is taken just to read the pages a statement needs, so a concurrent writer can't
  change them underneath you mid-read.
- **RESERVED** (3.4) is taken the *moment you know you'll eventually write* — before you've written a
  single byte — specifically so a second would-be writer is turned away immediately, rather than doing
  work (building its own journal) it would only have to discard. Notice **RESERVED** deliberately still
  allows concurrent readers: you can build the journal and hold the original pages in memory without
  blocking anyone who only wants to read.
- **PENDING** (3.8, first half) is a one-way turnstile: existing readers drain, no new ones are admitted.
  Without this step a writer jumping straight from RESERVED to EXCLUSIVE could wait forever if readers
  kept arriving faster than the current ones finished.
- Only once the last reader has left does the lock become **EXCLUSIVE** (3.8, second half) — and only
  under EXCLUSIVE is it safe to actually overwrite pages in the database file (3.9). A reader can never
  observe a half-written page, because no reader can hold *any* lock while EXCLUSIVE is held.

The lock drops back to **UNLOCKED** at 3.12, right after the journal deletion that is the actual commit
(3.11) — so for a brief moment the transaction is *committed but still holding its locks*, while cleanup
finishes.

Two of those deserve individual attention.


**Step 3.5's "original database size" is easy to overlook and it's load-bearing.** If the transaction
grew the file — a split allocated page 30 in a 29-page database — then rolling back must *shrink* it
again. Restoring old page contents isn't enough, because page 30 has no old content; it didn't exist.
The header records the original page count and recovery truncates back to it. Verified: a journal whose
header said 28 pages produced a database of exactly 28 × 4096 = 114,688 bytes after recovery.


**Step 3.10 is the one people drop, and dropping it is invisible in testing.** You deleted the journal
because the data was "written" — but if that data was still in the OS page cache when the power failed,
you have now destroyed the only copy of the old pages while never having stored the new ones. The
database is corrupt and unrecoverable. Notice that this failure requires a *real* power cut to
reproduce: killing the process leaves the page cache intact and the OS finishes the writes. **A test
suite that only kills processes cannot detect a missing fsync**, which is why §14.4 is about what your
crash matrix does and doesn't prove.


### The measured trace, and a surprise


Here is one real `synchronous=FULL`, `journal_mode=DELETE` commit of two dirty pages, traced at the
syscall level with file descriptors resolved:


```
OPEN      db-journal
WRITE     db-journal  n= 512 off=0        <- header sector: magic ZEROED, nRec=0
WRITE     db-journal  n=   4 off=512      <- page number
WRITE     db-journal  n=4096 off=516      <- the original page image
WRITE     db-journal  n=   4 off=4612     <- checksum
WRITE     db-journal  n=   4 off=4616     <- next page number   (record stride = 4104)
WRITE     db-journal  n=4096 off=4620
WRITE     db-journal  n=   4 off=8716
*FSYNC*   db-journal                      <- §3.7 flush #1: the journal BODY
OPEN      .           [O_DIRECTORY]
*FSYNC*   .                               <- fsync of the DIRECTORY
WRITE     db-journal  n=  12 off=0        <- magic(8) + nRec(4), in ONE write
*FSYNC*   db-journal                      <- §3.7 flush #2: the journal HEADER
WRITE     db          n=4096 off=0        <- §3.9
WRITE     db          n=4096 off=4096
*FSYNC*   db                              <- §3.10
!UNLINK!  db-journal                      <== §3.11 COMMIT POINT
```


> ⚠️ **The numbered walkthrough implies three fsyncs. A real commit does four.** The extra one is an
> `fsync()` on the **containing directory**, issued right after the first journal flush. §3.x never
> mentions it; it appears only in §9.5: *"SQLite tries to prevent this by opening and syncing the
> directory containing the rollback journal at the same time it syncs the journal file itself."*
>
> **Why it's needed, and it's a genuinely non-obvious failure:** `fsync(journal_fd)` makes the journal's
> *contents* durable. It says nothing about the **directory entry** that gives the file its name. Lose
> power and you can end up with a journal whose bytes are all on disk and whose filename isn't — so
> recovery never finds it, never rolls back, and the half-written database is silently accepted as
> correct. Syncing the directory makes the *name* durable.
>
> Do not write "three fsyncs" without qualifying it, and do not confuse this directory sync (at journal
> **creation**) with a directory sync after deletion. The latter does **not** happen by default — see
> §13.8.


**The general lesson is bigger than SQLite:** durability applies to *names* as well as *contents*.
Anyone who has written the create-temp-file-then-`os.replace()` pattern and not synced the directory has
the same latent bug.


---


## 13.5 The journal file, decoded


You're writing your own journal format (that's the scope decision), but you should be able to read
SQLite's, because it's short, it's well designed, and every field earns its place. Here is a real hot
journal produced by killing a process mid-transaction:


```
$ od -A d -t x1 -N 64 hot.db-journal
0000000  d9 d5 05 f9 20 a1 63 d7  00 00 00 07  7b 60 57 e4
0000016  00 00 00 1c  00 00 02 00  00 00 10 00  00 00 00 00
0000032  00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
```


| Offset | Size | Field | This file | What it's for |
|---|---|---|---|---|
| 0 | 8 | magic | `d9d505f920a163d7` | identifies a journal — **and its absence marks an unsynced segment** (§13.6) |
| 8 | 4 | `nRec` | 7 | how many page records follow. **Starts at zero** (§13.6) |
| 12 | 4 | checksum nonce | `0x7b6057e4` | random per segment; seeds every record's checksum (§13.7) |
| 16 | 4 | db size in pages | 28 | truncate back to this on rollback (§13.4) |
| 20 | 4 | sector size | 512 | the alignment the header is padded to |
| 24 | 4 | page size | 4096 | so recovery doesn't need the database file to parse the journal |


28 bytes of fields, then zero padding out to the sector size. Then the records:


```
offset  512..515  = page number : 3
offset  516..4611 = page image  : 4096 bytes
offset 4612..4615 = checksum    : 0x7b605c19
offset 4616..4619 = NEXT page number : 2      => record stride = 4 + 4096 + 4 = 4104
```


So: **`[page number][the page's original 4096 bytes][checksum]`**, repeated, no padding between records.


### Why the header is padded to the sector size


Because of chapter 00 §0.3. The header is what recovery reads first and trusts most; a torn write that
half-updates it would be the worst possible corruption. Padding it to a whole sector means the header
occupies sectors nothing else uses, so a partial write elsewhere can never scribble on it — and the
`nRec` update in §13.4 rewrites only bytes 0–11, comfortably inside one sector, which is what makes
that update effectively atomic.


**Notice the format stores the sector size and page size *in the journal*.** Recovery therefore needs
nothing but the journal to parse the journal. If it had to read the page size out of the database file's
header, recovery would depend on the database file being intact — which is precisely the assumption you
cannot make when recovering.


### One journal file holds *many* headers


The intuitive model — one header, then all the records — is wrong, and the correction matters for
anyone writing a parser. Every sync point starts a **new segment**: a fresh sector-aligned header plus
its own records. A single 119 KB hot journal, fully parsed:


```
seg 1  hdr@     0  nRec=7  nonce=0x7b6057e4  dbSize=28  pages=[3,2,4,5,1,6,7]
seg 2  hdr@ 29696  nRec=6  nonce=0xdee7c870             pages=[8,9,10,11,12,13]
seg 3  hdr@ 55296  nRec=6  nonce=0x6024dfee             pages=[14,...,19]
seg 4  hdr@ 80896  nRec=6  nonce=0x8faab4a0             pages=[20,...,25]
TOTAL: 25 records, 25 checksums verified, 0 mismatches
```


Four headers, **four different random nonces**, in one file. Segments appear when the transaction is
bigger than the page cache and has to spill dirty pages to disk mid-transaction — so a long transaction
syncs repeatedly rather than once at the end.


A fresh nonce per segment is a deliberate choice worth understanding: it means a stale record left over
from an *earlier* transaction that happened to occupy the same bytes will fail the checksum, because it
was computed against a different nonce. Without the nonce, an old record with a valid self-consistent
checksum could be mistaken for a current one.


---


## 13.6 The chicken-and-egg problem, and the two-guard answer


Here's a problem the design so far hasn't solved. The journal exists to make the database recoverable.
**What makes the journal itself recoverable?** You crash while writing the journal — 4 of 7 page records
are on disk. Recovery finds a journal file, believes it, and replays 4 stale pages over a database that
was never modified. **You have just corrupted a database that was perfectly fine.**


You cannot solve this with another journal. So:


> **A journal must be born invalid and become valid in one atomic step.**


SQLite does this twice over, which is the part worth copying.


**Guard 1: `nRec` starts at zero.** From the documentation:


> "SQLite records the number of pages in the rollback journal in the header of the rollback journal.
> **This number is initially zero.** So during an attempt to rollback an incomplete (and possibly
> corrupt) rollback journal, the process doing the rollback will see that the journal contains zero
> pages and will thus make no changes to the database."


**Guard 2, which the documentation doesn't emphasize: the 8-byte magic is withheld too.** The header is
initially written with **twelve zero bytes** — magic *and* `nRec`. Both are filled in together, in a
single 12-byte write, *after* the body has been fsynced. That's the `n=12 off=0` line in §13.4's trace,
and it comes after the first fsync, not before.


So a torn journal is rejected two independent ways: no magic means "this isn't a journal segment at
all," and `nRec = 0` means "this journal describes zero pages, replay nothing."


### Caught in the wild


The hot journal from §13.5 had a fifth segment slot that was never synced. Its bytes:


```
$ od -A d -t x1 -j 106496 -N 32 hot.db-journal
0106496  00 00 00 00 00 00 00 00  00 00 00 00  93 20 8e e4
         ^^^^^^^^^^^^^^^^^^^^^^^  ^^^^^^^^^^^
         magic = ZEROS            nRec = 0        nonce IS written
0106512  00 00 00 1c  00 00 02 00  00 00 10 00  ...
         dbSize=28    sector=512   pagesize=4096   <- these ARE filled in


trailing bytes after this header: 12,824
  record 0  page 26
  record 1  page 27
  record 2  page 28
```


**Three complete page records, physically present on disk, correctly ignored by recovery.** The
magic-scan found only four segments and stopped. This is exactly the scenario the guards exist for,
observed in a real crash rather than argued about.


Note which fields *are* written up front: nonce, db size, sector size, page size. Only the two that
constitute the "this is valid" signal are held back. **That's the design principle — write everything
except the commit token, then write the commit token.** It's the same shape as §13.2, one level down:
the journal's validity has its own atomic bit, and 12 bytes inside one sector is close enough to atomic
to rely on.


> ⚠️ Two documented exemptions where `nRec` is instead set to `0xFFFFFFFF` up front, meaning "as many
> records as you find": no-sync mode, and filesystems reporting `SQLITE_IOCAP_SAFE_APPEND` (where an
> append is guaranteed not to corrupt existing bytes). `pager.c` calls the assumption "dangerous." I
> never observed either path on ext4 — every header I produced had a real count.


---


## 13.7 The checksum, which is not a checksum


Each record carries a 32-bit trailer. Here is what it actually is, from `pager.c`:


```c
** This is not a real checksum. It is really just the sum of the
** random initial value (pPager->cksumInit) and every 200th byte
** of the page data, starting with byte offset (pPager->pageSize%200).
** Each byte is interpreted as an 8-bit unsigned integer.
```


```python
def journal_checksum(page: bytes, nonce: int, page_size: int = 4096) -> int:
    cksum = nonce
    i = page_size - 200
    while i > 0:
        cksum += page[i]
        i -= 200
    return cksum & 0xFFFFFFFF
```


For a 4096-byte page that samples **20 bytes** — offsets 96, 296, 496, … 3896 — out of 4096. It reads
0.5% of the page. It is an additive sum, not a CRC, and it would not detect a byte swap or a
compensating pair of changes.


**It's also correct for its purpose**, and the reasoning is the most instructive thing in this chapter:


> "If journal corruption occurs due to a power failure, the most likely scenario is that one end or the
> other of the record will be changed. It is much less likely that the two ends of the journal record
> will be correct and the middle be corrupt. Thus, this 'checksum' scheme, though fast and simple,
> catches the mostly likely kind of corruption."


**The threat model is a truncated or partially-written record, not an adversary and not bit rot.** Under
that model, sampling both ends is nearly as good as hashing everything, and it costs 20 byte-loads
instead of 4096 on a path that runs for every page of every transaction.


> This is what "engineering" means as distinct from "computing." A CRC32 is strictly better at detecting
> corruption and strictly worse at this job, because this job is *"cheaply reject torn writes on the
> hot path."* Being able to explain a design that looks weak by naming the threat model it was built
> for is a much stronger interview move than observing that it isn't a real checksum.


Also note the nonce is *added*, not mixed. That's how a record from a previous transaction fails
validation even if its own bytes are internally consistent — §13.5's point.


**Verification worth mentioning:** reimplementing exactly the code above in Python reproduced **all 25**
stored checksums across four segments with four different nonces, bit-for-bit. When you can
re-derive a format's values yourself, you know you've read it correctly — that's a much stronger
position than having quoted a spec.


---


## 13.8 `PRAGMA synchronous`: which fsync you're allowed to skip


fsync is the expensive part of a commit. `synchronous` chooses how many you do. Measured — `fsync()`
calls strictly between `BEGIN` and `COMMIT`, one small transaction, `journal_mode=DELETE`:


| `synchronous` | fsyncs | Which ones |
|---|---|---|
| `OFF` | **0** | none at all |
| `NORMAL` | 3 | journal body, directory, database |
| `FULL` (default) | **4** | journal body, directory, **journal header**, database |
| `EXTRA` | 5 | all of FULL, plus the directory again **after** the unlink |


Look at what `NORMAL` drops: **the second journal fsync — the one after `nRec` is written.** So at
`NORMAL`, `nRec` and the magic hit the page cache but aren't forced to disk before the database pages
are written.


**And now §13.7 pays off.** At `NORMAL` you can end up with a journal whose header says "7 records"
while only 5 record bodies actually reached the disk. The `nRec` guard has been weakened — so the
**per-record checksums** become the thing that catches it. Replay stops at the first record that doesn't
verify. That's why the documentation can call `NORMAL` reasonable rather than reckless, and it's a
lovely example of two mechanisms covering for each other: the checksum is cheap insurance that makes it
safe to skip an expensive barrier.


**`EXTRA` is the only mode that fsyncs the directory after deleting the journal**, and this is the
detail most likely to be written up wrongly. §3.11 says nothing about it. The mechanism is explicit in
`pager.c`:


```c
u8 extraSync;    /* sync directory after journal delete */
...
/* Set Pager.extraSync if "PRAGMA synchronous=EXTRA" is requested ... If this
** flag is set, SQLite syncs the directory to disk immediately after deleting
** a journal file in "PRAGMA journal_mode=DELETE" mode. */
```


**What that means for the default:** at `FULL`, a transaction that commits immediately before a power
cut can be *rolled back* on reboot — because the `unlink` was in the page cache and never made durable.
The database will **not** be corrupt; you simply lose the last transaction. So the default trades a
sliver of **durability** (the D in ACID) to save an fsync, while never trading **consistency**. The
official durability matrix says exactly this: `FULL` is "maybe not durable," `EXTRA` is ACID.


> **This distinction is worth being precise about**, because "is SQLite ACID?" is a question people
> answer too confidently in both directions. The honest answer: atomicity and consistency hold at
> `FULL`; full durability needs `EXTRA`. And at `NORMAL`, the docs say you may lose *consistency* on a
> rollback journal — which is a categorically bigger deal and the reason `FULL` is the default.


---


## 13.9 `journal_mode`: where the commit point moves


The commit point is "delete the journal" only in the default mode. Two alternatives move it, and both
were measured:


| mode | The commit point is | Post-commit journal | fsyncs at FULL |
|---|---|---|---|
| `DELETE` | `unlink(journal)` | gone | **4** |
| `TRUNCATE` | `ftruncate(journal, 0)` | 0 bytes, still exists | **5** |
| `PERSIST` | zeroing the journal header | full size, header zeroed | **5** |


`PERSIST` is the neatest of the three conceptually: it makes the commit point *the erasure of the magic
bytes* — §13.6's guard 1 run in reverse. A journal whose magic is zeroed is not a journal, so there is
nothing to recover. Verified:


```
$ od -A d -t x1 -N 16 PERSIST_FULL.db-journal
0000000  00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
```


> ⚠️ **`TRUNCATE` is not cheaper, and the folklore says it is.** It costs **five** fsyncs against
> `DELETE`'s four. The documented rationale is about avoiding a *directory* modification — "truncating
> a file is much faster than deleting the file since the containing directory does not need to be
> changed" — which is a claim about metadata cost, **not** about fsync count. Anyone who assumes
> "TRUNCATE does less I/O" is wrong on this axis. Whether it's faster overall depends on your
> filesystem, which is the honest answer.


---


## 13.10 Roads not taken


### WAL (write-ahead logging)


§13.3 has the mechanism. Here's why it isn't week 5, in cost terms: the log itself is easy, and
everything around it isn't. You need a **wal-index** mapping page numbers to their newest frame,
living in a shared-memory `-shm` file so multiple processes can consult it; a **checkpoint** process
that moves frames back into the database with its own concurrency rules about which readers still need
which frames; and a policy for when checkpointing happens and what to do when it can't. That's 25+
hours, and it buys a *second* durability mechanism rather than a second thing to talk about.


The measured payoff is real, though, and worth quoting: at `synchronous=NORMAL` a WAL commit does
**zero** fsyncs, and at `FULL` it does **one** (against `DELETE`'s four). That's the number that
explains WAL's popularity in one line.


**The right thing to say:** *"I implemented the rollback journal because it's the design where the
atomic-commit problem is most visible — the commit point is a single `unlink` and I can crash-test every
boundary around it. WAL would be faster and would let readers run alongside a writer, which is the
bigger win in practice, but it needs a shared-memory index over the log and a checkpointer, and it
would have replaced my crash matrix with plumbing."*


### Shadow paging / copy-on-write


Never overwrite a page. Write a modified copy to a new page number, and copy the parent to point at it,
all the way to the root — then swap one root pointer. **Used by:** LMDB, BoltDB, ZFS, btrfs.


**Why it's tempting: it deletes this entire chapter.** No journal, no fsync ordering beyond one barrier,
no recovery code, and readers holding the old root get a consistent snapshot for free. Chapter 02 §2.7
already flagged it, and it's the strongest "what would you do differently" answer available to you —
*provided* you also name the costs: every leaf write dirties the whole root path, the file grows on
every write until old versions are reclaimed, and reclamation needs its own notion of "no reader still
needs this version."


### ARIES (redo + undo together)


The industrial design, in PostgreSQL, InnoDB, DB2, SQL Server. Log **both** old and new values, stamp
every page with the log sequence number of the last change that touched it, and allow **steal**
(evicting a dirty page belonging to an uncommitted transaction) and **no-force** (committing without
flushing the data pages). Recovery runs three phases: analysis, redo everything, then undo the losers.


**Why:** it decouples the buffer pool from transaction boundaries entirely — which is what lets many
concurrent transactions interleave their dirty pages in one buffer pool. Your design forbids stealing:
a dirty page belonging to an open transaction cannot be evicted, which is a real constraint on your
buffer pool (chapter 04 §4.5) and is exactly the constraint ARIES was invented to remove. Knowing
*which* constraint the fancy algorithm buys you back is the useful form of this knowledge.


### Full-page writes (torn-page protection)


Here's a subtlety worth carrying. PostgreSQL's WAL records are normally *deltas* — but the first time a
page is modified after a checkpoint, the WAL includes the **entire page image**. That's
`full_page_writes`, and it exists because of chapter 00 §0.3: if the page was torn during the crash, a
delta can't repair it, because deltas assume a known starting state. A full image doesn't.


**Your journal is immune to this by construction**, and it's worth noticing why: it stores whole
original pages, not deltas. So rollback overwrites a torn page completely and never depends on its prior
contents. **You got torn-page protection for free by choosing the less clever representation** — a good
example of simplicity buying a property you didn't set out to buy.


### Group commit


Under concurrent load, batch several transactions' commits into one fsync. **Used by:** essentially every
server database, because fsync is the bottleneck and its cost is nearly independent of how much data you
sync. **Why not here:** you're single-writer by design (chapter 16), so there is rarely a second
transaction to batch with. Worth naming because it's the standard answer to "how would you make commits
faster," and the answer is *amortize the barrier*, not *make the barrier cheaper*.


---


## 13.11 What you're building


quilldb writes **its own journal format using SQLite's algorithm** — the fsync order, the commit point,
the original-database-size field, the two guards from §13.6, and per-record checksums. Copy the field
*layout* from §13.5 too; it's well designed and there's nothing to gain from inventing a different one.
Use your own magic bytes so nobody can mistake a quilldb journal for a SQLite one.


```python
class Journal:
    MAGIC = b"quilldbj"          # 8 bytes, your own


    def open_for_write(self, page_count_before: int) -> None:
        """Create the journal. Writes the header with magic and nRec BOTH ZERO,
        the real page_count_before, and pads to SECTOR_SIZE."""


    def record_original(self, page_id: int, data: bytes) -> None:
        """Append [page_id u32][data][checksum u32] — the page as it was BEFORE
        modification. Idempotent: recording the same page twice in one
        transaction must write it once, or rollback order stops mattering...
        and it must not matter anyway (§14.2)."""


    def commit_barrier(self) -> None:
        """§13.7's two flushes and the directory sync:
             fsync(journal)                 # body is durable
             fsync(dirfd)                   # the journal's NAME is durable
             write magic + nRec at offset 0 # 12 bytes, one write, one sector
             fsync(journal)                 # the journal is now VALID
        Only after this may a single database page be written."""
```


```python
class Transaction:
    """Owns the dirty page set for one transaction.


    Before a page is modified for the first time, its ORIGINAL content goes to
    the journal. Track which pages you've already journalled — journalling a
    page twice is wasted I/O, and journalling the *modified* version is a
    catastrophic bug that turns rollback into "restore the broken state."
    """
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
```


**The four things to get right, in the order they'll bite:**


1. **Journal the page before the first modification, not after.** Obvious when stated; easy to get wrong
   when the buffer pool hands out a mutable `bytearray` and the mutation happens somewhere else. The
   cleanest defence is structural: make "get me a writable page" the function that journals, so there is
   no path to a mutable page that skipped it.
2. **Never write a database page before `commit_barrier()` returns.** This is the one ordering
   constraint the whole design rests on. Consider asserting it in debug builds — a flag on the pager that
   refuses writes while a transaction is open and unbarriered.
3. **`rollback()` must restore *byte-identical* content.** The test is a hash: hash the file before
   `BEGIN`, hash after `ROLLBACK`, assert equal. That single assertion is worth a dozen narrower ones.
4. **Fully zero freed pages, still** (weeks 1 and 4). Rollback can *un*-free a page, so the freelist is
   now being modified transactionally too.


---


## 13.12 Check yourself


1. A root split writes three pages. Describe the specific crash that makes the database unreadable
   rather than merely incomplete, and why it's worse than losing rows.
2. Why can't careful write ordering alone give you atomicity? Give all three reasons.
3. What is the one atomic primitive SQLite relies on, and why is it available when a 4096-byte atomic
   write isn't?
4. "Writing your data is not committing." Explain, and name two non-database systems with the same
   shape.
5. Undo vs redo: which does a rollback journal use, and what does that make simple that WAL makes hard?
6. Why does a WAL let readers run concurrently with a writer when a rollback journal can't?
7. What does the journal header's "original database size" field protect against? Give the concrete
   scenario.
8. Why must the database file be fsynced *before* the journal is deleted? Why won't killing a process
   ever detect the absence of that fsync?
9. A real `FULL` commit does four fsyncs, not three. What's the extra one, and what failure does it
   prevent?
10. Why is the journal header padded to the sector size?
11. Why does the journal store the page size and sector size, when the database file already has them?
12. One journal file, four headers, four different nonces. Why more than one header, and what does a
    fresh nonce per segment protect against?
13. A journal must be "born invalid." Name both guards, and say which 12 bytes make it valid.
14. You found three complete page records in a journal that recovery ignored. Why was that correct?
15. The record checksum reads 20 bytes out of 4096 and isn't a CRC. Defend it — what's the threat model?
16. `synchronous=NORMAL` drops one specific fsync. Which one, and which mechanism covers for it?
17. Is SQLite ACID at the default settings? Answer precisely.
18. Does `journal_mode=TRUNCATE` do less I/O than `DELETE`? Justify with numbers.
19. Your journal stores whole original pages rather than deltas. What property does that give you for
    free, and what does PostgreSQL have to do because its WAL stores deltas?
20. What does ARIES's "steal" policy allow that your design forbids, and where does that constraint show
    up in your code?


---


## 13.13 Sources


- [Atomic Commit In SQLite](https://www.sqlite.org/atomiccommit.html) — the §3.x commit sequence; §3.11
  "the rollback journal file is deleted. This is the instant where the transaction commits."; §3.7 "On
  most platforms two separate flush (or fsync()) operations are required."; §6.2 on `nRec` beginning at
  zero; §9.5 on syncing the directory containing the journal; §4.4 on truncating back to the recorded
  size.
- [`pager.c`](https://github.com/sqlite/sqlite/blob/master/src/pager.c) — the journal header field list
  above `writeJournalHdr()`; `pager_cksum()` and its "This is not a real checksum" comment with the
  torn-record threat model; `syncJournal()`'s ordering pseudo-code and "This ensures that all data has
  really hit the disk before nRec is updated"; the `extraSync` flag, "sync directory after journal
  delete," and its `synchronous=EXTRA` gate.
- [`PRAGMA synchronous`](https://www.sqlite.org/pragma.html#pragma_synchronous) and
  [`PRAGMA journal_mode`](https://www.sqlite.org/pragma.html#pragma_journal_mode) — the four sync levels
  and their durability matrix; `TRUNCATE`'s directory-modification rationale; `PERSIST` zeroing the
  header.
- [Write-Ahead Logging](https://www.sqlite.org/wal.html) — for the §13.3 and §13.10 contrast.
- **Measurements throughout** were taken on `sqlite3` 3.37.2 (CLI) and 3.50.4 (via Python) on ext4 with
  a 512-byte reported sector size: the syscall trace in §13.4, the decoded journal in §13.5, the
  unsynced fifth segment in §13.6, and the fsync-count table in §13.8. The checksum in §13.7 was
  reimplemented from the C source and reproduced all 25 stored checksums across four nonces. One thing
  **not** verified: a genuine mid-sector torn write — the torn-segment evidence came from killing a
  process, which exercises the same recovery path but does not prove behaviour under a partial physical
  sector write.


---


**Next:** [14 — Crash recovery](14-crash-recovery.md) — what makes a journal "hot," why replay must be
idempotent, and how to turn a durability claim into evidence.