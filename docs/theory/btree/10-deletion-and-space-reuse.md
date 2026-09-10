# 10 — Deletion and Space Reuse


> **Read at the start of week 4, before you write `BTree.delete()`.** Covers roadmap week 4's
> `DELETE`/`UPDATE` operators and B+tree deletion.
>
> **Time:** ~35 minutes. **Prerequisites:** [chapter 02 §2.4](../storage/02-the-slotted-page.md) (the
> three kinds of free space), [chapter 06](06-b-tree-mechanics.md) (cells, splits, the validator).


---


## 10.0 The problem: you delete 95% of your rows and the file doesn't get smaller


Here is a real transcript. 2000 rows, page size 4096, default settings:


```
AFTER 2000 INSERTS:
  rows=2000  page_count=29  freelist_count=0   file = 118,784 bytes


DELETE FROM t WHERE a > 100;          -- 1900 rows gone, 5% of the data left
  rows=100   page_count=29  freelist_count=26  file = 118,784 bytes


DELETE FROM t;                        -- now it's completely empty
  rows=0     page_count=29  freelist_count=26  file = 118,784 bytes


VACUUM;
  rows=0     page_count=3   freelist_count=0   file = 12,288 bytes
```


The file is byte-for-byte the same size with 2000 rows as with zero rows. `integrity_check` says
`ok` at every step. This is not a bug, it is not a quirk of an old version, and every major database
behaves this way.


**Two questions, and take them in order.** First: where did the space *go* — it clearly got tracked
somewhere, because `freelist_count` moved from 0 to 26. Second, and much more interesting: 100 rows
that originally occupied a fraction of one page are now spread across a tree that was built for
2000. **Did SQLite tidy that up, or did it leave the pages half-empty?**


Most people guess wrong on the second one, in a specific and instructive way. Hold your answer.


---


## 10.1 Deleting a cell: the mechanics, and the field that doesn't move


Deleting a row means deleting a cell from a leaf page. From chapter 02 §2.4 you already know the
shape of the answer: the freed bytes become a **freeblock**, a hole of ≥ 4 bytes that stores its own
size and a pointer to the next hole.


Here is that happening. A page with ten 17-byte cells, before and after deleting the middle one:


```
BEFORE (10 rows):
  page 2 header[0:8] = 0d 00 00 00 0a 0f 57 00
    [1:3] first_freeblock = 0        [3:5] ncell = 10   [7] fragments = 0
    freeblock chain = []
    cell pointers = [4080, 4063, 4046, 4029, 4012, 3995, 3978, 3961, 3944, 3927]


AFTER deleting the row whose cell was at offset 4012:
  page 2 header[0:8] = 0d 0f ac 00 09 0f 57 00
                          ^^^^^ 0x0fac = 4012
    [1:3] first_freeblock = 4012     [3:5] ncell = 9    [7] fragments = 0
    freeblock chain (offset, size, next) = [(4012, 17, 0)]
    cell pointers = [4080, 4063, 4046, 4029, 3995, 3978, 3961, 3944, 3927]
```


Three things changed: the cell count dropped, that cell's pointer was removed from the array (the
later pointers shifted down one slot), and the page header's first-freeblock field now points at the
17-byte hole. **The bytes of the deleted row are still physically there.** Nothing overwrote them;
they were simply unlinked and declared free. (Which is worth knowing for a reason beyond mechanics:
"deleted" data persists in a database file until something reuses the space. `VACUUM` is what
actually removes it.)


### Freeblocks chain in offset order, and adjacent holes merge


Delete a second cell that is *not* adjacent to the first, and you get a two-element chain:


```
  freeblock chain = [(3961, 17, 4012), (4012, 17, 0)]
```


Note the ordering: the chain runs in **increasing offset**, which chapter 02 §2.4 flagged as a spec
invariant. Now delete a cell that *is* adjacent to the existing hole at 4012 — its cell was at 4029,
directly above it:


```
  freeblock chain = [(3961, 17, 4012), (4012, 34, 0)]
                                               ^^ 17 + 17, coalesced into one block
```


Two 17-byte holes became one 34-byte hole. **This is why the increasing-offset invariant exists.**
Coalescing requires knowing whether the *next* hole starts exactly where this one ends, and a
sorted chain makes that a single comparison instead of a search. An unsorted chain would still be a
correct freelist and would silently lose the ability to merge — you would accumulate two adjacent
20-byte holes and refuse a 30-byte cell that fits perfectly.


> **The general pattern:** a sort order in a data structure is often not there for searching. It's
> there to make *adjacency* detectable. The same reasoning is why the page-level freelist wants its
> leaves ordered, and why memory allocators keep free lists sorted by address rather than by size.


### The fragment counter moves on *allocation*, not on deletion


Now the detail that contradicts the obvious guess. Chapter 02 §2.4 introduced **fragments**: holes
of 1–3 bytes, too small to hold the 4-byte freeblock header, therefore untrackable dead space,
counted in a single byte at page-header offset 7 and capped at 60.


The natural assumption is that deleting rows creates fragments. It does not. In every delete above,
the fragment counter stayed at `0`. Fragments appear when an **insert** consumes a freeblock and
leaves a remainder too small to be a freeblock of its own:


```
17-byte freeblock present:
  header[0:8] = 0d 0f ac 00 09 0f 57 00   first_freeblock=4012  fragments=0


INSERT a row needing a 15-byte cell — 2 bytes would be left over:
  header[0:8] = 0d 00 00 00 0a 0f 57 02   first_freeblock=0     fragments=2
                                    ^^ freeblock consumed and unlinked;
                                       its 2-byte tail is now dead space


Make another 17-byte hole, INSERT a 16-byte cell — 1 byte left over:
  header[0:8] = 0d 00 00 00 0a 0f 57 03   first_freeblock=0     fragments=3
                                    ^^ the counter accumulates
```


The allocator had a choice — refuse the slightly-too-big freeblock, or take it and abandon the
remainder — and it takes it, because 2 wasted bytes beats leaving a 17-byte hole unused. Fragments
are the **residue of reuse**, not the residue of deletion. Deletion always produces either a
well-formed freeblock or an extension of the unallocated gap.


That reframes the cap. 60 fragmented bytes per page isn't a limit on how messy deletion may get;
it's a budget the *allocator* spends, and hitting the budget triggers a defragmentation.


> **Say this out loud:** "Deleting a cell unlinks its pointer and turns its bytes into a freeblock —
> a hole storing its own size and the offset of the next hole, chained in increasing offset order so
> adjacent holes can coalesce. Fragments, the 1-to-3-byte holes that are too small to describe
> themselves, come from *allocation* rather than deletion: they're the remainder when an insert
> takes a freeblock slightly larger than it needs."


### What quilldb does instead, and why that's still SQLite's format


Chapter 02 §2.4 already made this call: **quilldb repacks the page on delete** rather than
maintaining a freeblock chain. Remove the cell, slide the surviving content tight against the end of
the page, rewrite the offsets, leave first-freeblock and fragment-count at 0.


A page with no holes is a well-formed SQLite page — the fields exist and say "no holes," which is
true. So this costs you nothing in fidelity and buys three things: `free_space()` stays a single
contiguous number, fragments cannot accumulate, and `delete_cell` is about fifteen lines instead of
eighty. What it costs is O(page size) per delete instead of O(1), which matters at SQLite's write
volumes and does not matter at yours.


---


## 10.2 Where the pages went, and why the file can't shrink


Back to the first question. `freelist_count` went 0 → 26, so 26 of the 29 pages are now on the
freelist you built in week 1 (chapter 01 §1.8). 29 − 26 = 3 pages still in use: page 1
(`sqlite_schema`), the table's root, and the index's root.


So the space *is* reclaimed — for the database's own future use. Insert 2000 rows again and the file
stays at 118,784 bytes, because the allocator hands back the freed pages before growing the file.
**"The file didn't shrink" and "the space is wasted" are different claims, and only the first is
true.**


### Why not just truncate the file?


### Your first instinct


"If 26 pages are free, shorten the file by 26 pages."


### Why it breaks


Because of *which* pages are free. Freed pages are scattered through the file — page 9, page 14,
page 22. Truncation only removes bytes from the **end**. To shorten a file by 26 pages you would
need the 26 free pages to be the last 26, which requires **moving** the live pages that are
currently there into the holes lower down.


And moving a page is not a memcpy. A page's identity *is* its number: every parent's child pointer,
every overflow-chain link, every `rootpage` value in `sqlite_schema`, and every freelist entry
refers to it by number. Relocating page 27 to page 9 means finding and rewriting every pointer to
27 — and nothing in the format lets you find those pointers, because b-tree pointers only run
*downward*. There is no back-pointer from a child to its parent.


So the operation is: walk the entire database, rebuild it compactly, swap it in. That is precisely
what `VACUUM` is, which is why `VACUUM` is a separate explicit command that rewrites the whole
file — and why it needs free disk space roughly equal to the database to run.


### What real systems do


They keep a **high-water mark** and reuse below it. The file grows to its peak size and stays
there; deleted space is recycled internally. Shrinking is an occasional, explicit, expensive
maintenance operation:


| Engine | Reuses freed space | Returns space to the OS |
|---|---|---|
| SQLite | freelist, automatic | `VACUUM`, explicit (or `auto_vacuum`, opt-in) |
| PostgreSQL | free space map, automatic | `VACUUM FULL`, explicit and rewrites the table |
| InnoDB | freed extents, automatic | `OPTIMIZE TABLE` / rebuild, explicit |


**The reasoning is the same in all three**, and it's a good thing to be able to state: a database's
size tends to be roughly stable or growing, so optimizing for "give bytes back promptly" would mean
paying relocation costs continuously in exchange for disk space that is cheap and that you will
probably want again shortly. Free space *inside* the file is nearly as good as free space outside
it, and enormously cheaper to arrange.


> **Say this out loud:** "Deleted pages go on a freelist and get reused, but the file doesn't shrink,
> because freed pages are scattered and truncation only removes from the end. Compacting would mean
> relocating live pages, and a page's number is its identity — every parent pointer and root-page
> entry refers to it — so relocation means rewriting the whole file. That's what `VACUUM` is, and
> that's why it's explicit rather than automatic."


---


## 10.3 The real question: does SQLite tidy up half-empty pages?


Now the interesting one. You have a tree of 82 leaf pages at 97.5% occupancy. You delete half the
rows, spread evenly, so every page loses half its cells. What does the tree look like afterwards?


### Your first instinct


Two candidates, and both are popular:


- **"It merges."** B-tree textbooks define a minimum occupancy — usually half — and specify that a
  page dropping below it must borrow from a sibling or merge with one. So: 82 half-full pages become
  41 full pages.
- **"It does nothing until a page is completely empty."** Merging is complicated; just free pages
  when they hit zero cells.


### What actually happens


Neither. Here is the measurement — 3000 rows of ~108 bytes, 82 leaves, 37 cells per page, deleted
with a **uniform stride** so every page loses the same fraction:


| Deletion | Rows left | Occupancy if nothing merged | Actual leaves | To freelist | Actual occupancy |
|---|---|---|---|---|---|
| keep every 2nd | 1500 | 48.8% | **81** (was 82) | 1 | **49.5%** |
| keep every 3rd | 1000 | 32.5% | **34** | 48 | **78.4%** |
| keep every 4th | 750 | 24.4% | 25 | 57 | 80.0% |
| keep every 5th | 600 | 19.5% | 21 | 61 | 76.2% |


Read the first two rows against each other, because that's the whole finding:


- Delete **half** the rows and SQLite does essentially **nothing**. 82 leaves become 81, one page
  goes to the freelist, and the tree sits at **49.5% occupancy indefinitely.** Your file is now twice
  as big as the data needs, and no amount of waiting will fix it.
- Delete **two-thirds** and merging fires hard. 82 leaves collapse to 34, 48 pages go to the
  freelist, and occupancy *rises* to 78.4%.


Occupancy going **up** after a deletion is the signature of genuine merging. If SQLite only freed
empty pages, occupancy could only ever fall.


### Why that threshold


Because it's written down. From `balance()` in `btree.c`:


```c
if( pPage->nOverflow==0 && pPage->nFree*3<=(int)pCur->pBt->usableSize*2 ){
  /* No rebalance required as long as:
  **   (1) There are no overflow cells
  **   (2) The amount of free space on the page is less than 2/3rds of
  **       the total usable space on the page. */
  break;
}
```


Rebalancing is skipped while free space ≤ ⅔ of the usable page — that is, while **occupancy ≥ ⅓**.
For a 4096-byte page: rebalance only once more than 2730 bytes are free. The measured boundary
lands exactly where the arithmetic predicts, 48.8% sailing past and 32.5% triggering.


And when it does fire, `balance_nonroot()` genuinely redistributes rather than merely freeing:


> "Usually a single sibling on either side of the page are used in the balancing… **The number of
> siblings of the page might be increased or decreased by one or two** in an effort to keep pages
> nearly full but not over full."


### Why ⅓ and not ½


The textbook B-tree invariant — every non-root node at least half full — exists to make the height
bound provable. It's a *theoretical* requirement, and enforcing it means rebalancing often, because a
page hovering near half-full triggers work on every delete that crosses the line and again on the
next insert. That's the classic pathology: alternate delete and insert at the boundary and you merge,
split, merge, split forever.


Dropping the bound to ⅓ buys **hysteresis**. There's now a wide dead zone between "just split"
(50%, since a split leaves two half-full pages) and "will merge" (33%), so ordinary churn never
crosses it. You pay for the hysteresis in space: a tree can legitimately sit at 34% occupancy, three
times larger than optimal.


**That is the tradeoff to name out loud, because it recurs everywhere:** eager consolidation gives
you tight space and unbounded worst-case work per operation; lazy consolidation with a generous
threshold gives you bounded amortized work and some permanent slack. Hash table load factors, GC
generation sizing, LSM compaction triggers, and the 60-byte fragment cap from §10.1 are all the same
dial. SQLite turns it toward "predictable operation cost, accept some slack," which is the right
choice for a library that must never surprise you with a long pause.


> ⚠️ **A methodology trap that produced a wrong answer on the first attempt.** The measurement above
> deletes with a uniform stride (`WHERE a % N <> 0`). The first attempt used `WHERE (a % 100) >= k`,
> which deletes *runs of consecutive rowids* — and since rowid order is physical order in a table
> b-tree, that empties whole pages outright. It showed dramatic "merging" even at 45% retention,
> which would have supported a completely wrong threshold. **Deleting a fraction of the rows and
> deleting a fraction of each page are different experiments,** and only the second one measures
> rebalancing. If you benchmark your own delete path, use a stride.


---


## 10.4 Why deletion is harder than insertion


Splitting and merging sound symmetric. They aren't, and the asymmetry is worth understanding because
it's why your week-4 delete is allowed to be simpler than SQLite's.


**A split is local and downward-looking.** The page is full, you have its contents, you divide them
in two, you ask the parent to accept one new separator. All the information you need is in your hand,
and the parent only has to *add* something — which it can always do, or split in turn (chapter 06
§6.4).


**A merge is non-local and needs information the page doesn't have.** To merge you must:


1. Know who your **siblings** are. A page has no sibling pointers — only the parent knows the
   ordering of its children. So you need the parent, which means you need the path you descended
   (chapter 06 §6.3's cursor stack), because there is no child→parent pointer in the format.
2. Know your **position** in the parent, to find the separator between you and your sibling.
3. **Remove** a cell from the parent — the separator that distinguished two pages that are now one.
   Which can drop the parent below *its* threshold, cascading upward.
4. Handle the root specially: when the root ends up with a single child, the tree must lose a level.


Step 3 is the one that makes it genuinely recursive, and step 4 is where you see the mirror image of
"trees grow at the root." **Trees shrink at the root too** — and the transcript proves it. After
deleting every row:


```
  page 2 header = 0d 00 00 00 00 10 00 ...  -> type 13, leaf table,  ncell=0
  page 3 header = 0a 00 00 00 00 10 00 ...  -> type 10, leaf index,  ncell=0
```


Both roots reverted from **interior** pages (types 5 and 2) to **leaf** pages (13 and 10), with zero
cells and `cell_content_start = 4096`. The trees are back to height 1, exactly as they were before
the first insert, and the root page *numbers* never changed — which they must not, since
`sqlite_schema.rootpage` records them.


### The separator-key subtlety, and why table trees dodge it


There's one more asymmetry, and it's the nicest thing in this chapter.


In a table b-tree, an interior cell is `[4-byte left child][rowid varint]` and carries **no payload**
(chapter 06 §6.0). The rowid there is purely a routing value: "keys ≤ this go left." So what happens
when you delete the very row whose rowid appears as a separator upstairs?


**Nothing.** The separator is still a perfectly good boundary. It says "keys ≤ 500 are to the left,"
and that remains true whether or not a row with rowid 500 exists. A separator is a *fence*, not a
*record*, and a fence doesn't stop being a fence when you remove the post next to it. This is the
practical payoff of the "+" in B+tree: because all real data lives in leaves, interior pages hold
only navigational hints, and a stale hint is still correct.


Index b-trees don't get off so lightly. Verified by dumping an interior index page:


```
=== page 3: type byte=2 (interior index)
  cell[0] @4071  [child=6][payload-len=20][payload]
        col0 TEXT 'b000162' | col1 TEXT 'c000162' | col2 int2 = 162
```


The divider key in an interior *index* page is the **full key**, not a truncated one. So an interior
index cell is simultaneously a fence and a real index entry — and deleting that entry means you must
put a replacement fence in its place, promoted from a leaf below. That is the classic
"delete-from-an-internal-node" problem, and it's why index deletion in a real engine is meaningfully
harder than table deletion.


**What this buys you in week 4:** you can implement table deletion as "remove the cell, repack, free
the page if it's empty" and be completely correct. You do not have to touch the parent, because the
separators stay valid. That is not laziness; it's a property of the B+tree you're exploiting, and
saying it that way is much stronger than "I skipped merging."


---


## 10.5 `UPDATE` is mostly `DELETE` plus `INSERT`, and that's not a shortcut


Your instinct with `UPDATE users SET age = 37 WHERE id = 1` is to find the cell and overwrite the
bytes. Sometimes you can. Usually you can't, for a reason that follows directly from chapter 03:
**the record encoding is variable-width.** Changing `age` from 36 to 37 keeps the serial type at 1
and the cell size identical, so an in-place patch works. Changing it from 127 to 128 pushes the
serial type from 1 to 2 and the cell grows by a byte. Changing `'ada'` to `'adalovelace'` grows it by
eight.


A cell that grows may not fit in its slot. So the general path is: delete the old cell, insert the
new one — which may land at a different offset on the page, or trigger a split, or push the row into
an overflow chain.


Three consequences worth having straight before you write the operator:


1. **A rowid change is a move.** `UPDATE t SET rowid = 9 WHERE rowid = 1` doesn't edit a cell; it
   relocates the row to a different position in the tree, possibly a different page.
2. **Every index on a changed column needs maintenance** — delete the old index entry, insert the
   new one. And since an index key ends in the rowid (chapter 11), a rowid change touches *every*
   index, even ones on columns you didn't modify.
3. **You must not leave the row deleted if the insert fails.** A `UNIQUE` violation discovered
   halfway through is the exact reason week 5 exists. Until you have a journal, the defensive move is
   to validate everything you can *before* mutating anything — the roadmap's week-4 success criterion
   "a `UNIQUE` violation raises *before* any page is written" is this rule.


**In-place update as an optimization is worth knowing but not worth building yet.** The condition is
narrow (new cell size ≤ old cell size, no index on any changed column, rowid unchanged), and the
payoff is one page write versus one page write. Note it in the README as future work.


---


## 10.6 What quilldb does


| | SQLite | quilldb week 4 | Is that legal SQLite? |
|---|---|---|---|
| Cell removal | freeblock chain, coalescing, fragments | **repack the page** | ✅ a page with no holes is well-formed |
| Empty page | freed to the freelist | **freed to the freelist** | ✅ same |
| Underfull page | merge siblings below ⅓ occupancy | **nothing** | ✅ occupancy is not a format constraint |
| Root collapse | interior root reverts to leaf | **when it empties, yes** | ✅ same |
| Shrink the file | `VACUUM` | **not implemented** | ✅ absence of a feature |
| `UPDATE` | in-place when it fits, else delete+insert | **always delete+insert** | ✅ same bytes either way |


Every row in that last column says yes, and that's the point this chapter exists to make precisely:
**none of these choices produce a file that differs from a legal SQLite file.** A tree at 40%
occupancy is not a malformed tree. `integrity_check` has no opinion about how full your pages are —
it checks structure, not density. So these are *missing features*, not *format divergences*, and
that distinction is the difference between "my implementation is incomplete in these four documented
ways" and "my files are subtly non-standard," which is a much worse sentence.


The honest version, ready to say:


> *"I free pages when they empty but I don't merge underfull siblings. SQLite merges when a page drops
> below a third full — the gate in `balance()` is `nFree*3 <= usableSize*2` — which means even SQLite
> lets a tree sit at 34% occupancy to avoid merge/split thrashing at the boundary. My tree can sit
> lower than that after a delete-heavy workload, so the file is bigger than optimal until the space is
> reused. It's still a valid tree and `integrity_check` passes; what I'd lose is scan performance,
> because a scan reads pages, not rows."*


That answer demonstrates three things at once: you know the real threshold, you know *why* a
threshold exists at all rather than the textbook ½, and you can state your own gap in terms of its
measurable consequence.


---


## 10.7 Roads not taken


### `auto_vacuum` — and the back-pointer that makes it possible


§10.2 claimed you can't relocate a page because there's no way to find the pointers to it. SQLite has
an opt-in feature that fixes exactly that: with `PRAGMA auto_vacuum` enabled, the file gains
**pointer-map pages**, which store for every page its type and its **parent page number**. That's the
child→parent back-pointer the format otherwise lacks — and with it, moving page 27 to page 9 becomes
possible, because you can look up who points at 27.


The cost is honest and instructive: pointer-map pages consume space and, more importantly, **every
structural change now writes a second page** to keep the map current. You've bought the ability to
shrink by making every split slightly more expensive forever. That's why it's off by default and must
be chosen when the database is created — it changes the file layout, so it can't be toggled later.


This is also why `check_supported()` in your `header.py` refuses a non-zero `largest_root_page`: that
field is how the header signals auto_vacuum, and a file with pointer-map pages has non-b-tree pages
interleaved among the page numbers you'd otherwise walk as tree nodes. Misreading one as a b-tree
page is exactly the silent corruption chapter 01 §1.6 warns about.


> **[Documented, not measured]** Unlike everything else in this chapter, the claim that
> `auto_vacuum=FULL` shrinks the file at commit comes from the pragma documentation; it wasn't tested
> here. Treat it as a citation, not a measurement.


### Tombstones instead of removal (PostgreSQL's MVCC)


PostgreSQL's `DELETE` doesn't remove the row at all. It writes the deleting transaction's id into the
row's `xmax` field, leaving the bytes in place. The row remains visible to transactions that started
earlier, and only becomes reclaimable once no snapshot can still see it — which is what `VACUUM`
determines and cleans.


**Why it's attractive:** deletion becomes a tiny fixed-size write, it never restructures anything,
and — the real prize — you get **snapshot isolation** almost for free, because old versions still
exist to be read. Readers never block writers and never see partial work.


**What it costs:** *bloat.* Dead rows occupy space and are scanned until vacuumed, so a table under
heavy update churn can grow far beyond its live data, and a vacuum that falls behind is one of the
classic Postgres production problems. You've traded a space-and-maintenance problem for a concurrency
win.


Worth being able to compare, because it's the same decision from chapter 15's angle: SQLite's
rollback-journal design gives one writer at a time and reclaims space immediately; MVCC gives many
concurrent readers and defers reclamation. **Neither is reclaiming space "properly" — they're
choosing what to make cheap.**


### Tombstones plus compaction (LSM trees)


RocksDB and friends can't remove anything either, because their files are immutable. A delete writes
a **tombstone** record that shadows the key, and the key isn't actually gone until a compaction merges
the levels containing both. Space reclamation is a background process by construction.


**The instructive failure mode:** a range you deleted can be *slower* to scan than before, because
reads must pass over all the tombstones. Delete-heavy LSM workloads are a known performance trap,
and it's a nice concrete example of "deleting data made things worse" — the same theme as this
chapter's opening transcript, taken to its extreme.


### Eager half-full merging (the textbook B-tree)


Enforce the classic invariant: every non-root node at least half full, merge or borrow immediately
when it isn't. **Used by:** most textbook presentations, and it's what an interviewer who studied
CLRS will expect. **Why not:** §10.3's merge/split thrashing at the boundary, with no hysteresis. The
useful thing here is being able to say the textbook version *is* the naive version, and to name what
the real implementations added.


---


## 10.8 What you're building


Roadmap week 4, two tasks totalling 4 hours:


```python
class BTree:
    def delete(self, key: int) -> bool:
        """Remove the row with this rowid. Returns False if it wasn't there.


        Descend to the leaf, drop the cell, repack the page. If the page is now
        empty and is not the root, free it to the freelist and remove its
        separator from the parent. Do NOT merge underfull pages.
        """
```


**The tests that matter**, beyond the obvious round-trip:


1. **Delete every row one at a time, in random order, validating the tree after each.** This is the
   single highest-value delete test — it walks every code path including the last-cell-on-a-page case,
   and a seeded shuffle makes failures reproducible.
2. **Delete-then-reinsert leaves the file the same size.** `page_count` after inserting N rows,
   deleting all N, and inserting N again should equal `page_count` after the first N. This is what
   proves the freelist is actually being *consumed*, not just appended to — the roadmap's week-4
   criterion.
3. **The root reverts to a leaf when the tree empties**, and its page number is unchanged. Asserting
   the page *type* byte flipped from 5 back to 13 is a one-line check that catches a whole class of
   root-handling bugs.
4. **`integrity_check` is `ok` after a delete-heavy workload**, not just after inserts. Deletion is
   where the free-space bookkeeping gets exercised, so this is where the free adversarial validator
   from chapter 06 §6.6 earns the most.
5. **A freed page is fully zeroed.** You verified in week 1 that leaving stale bytes in a freed page
   makes `sqlite3` report "freelist leaf count too big" — deletion is now a second, much busier
   source of freed pages, so this test protects a known-real bug.


---


## 10.9 Check yourself


1. You delete 95% of the rows. The file size doesn't change. Where did the space go, and how would
   you prove it's actually reusable?
2. Why can't the file be truncated, even when 26 of 29 pages are free?
3. What single missing piece of information makes page relocation impossible, and what does SQLite
   add to make `auto_vacuum` work?
4. Deleting a cell creates a freeblock. Why must the freeblock chain be sorted by offset?
5. When does the fragment counter at page-header offset 7 increase — and why is the intuitive answer
   wrong?
6. At what occupancy does SQLite rebalance, what's the exact condition in `balance()`, and why isn't
   it ½?
7. You delete half the rows, evenly spread. How many pages does SQLite free? Why is that the answer?
8. Name four things a merge needs that a split doesn't.
9. Trees grow at the root. What's the mirror-image fact, and what observable change proves it
   happened?
10. You delete the row whose rowid appears as a separator in an interior page. Why is nothing broken?
11. Why doesn't that argument work for an interior *index* page?
12. Why is `UPDATE age = 128` potentially harder than `UPDATE age = 37`?
13. Why does changing a row's rowid require touching every index, even on unmodified columns?
14. quilldb doesn't merge underfull pages. Why is that an implementation gap rather than a format
    divergence, and what's the measurable consequence?
15. PostgreSQL's `DELETE` leaves the row in place. What does that buy, and what's the failure mode?


---


## 10.10 Sources


- [`btree.c`](https://github.com/sqlite/sqlite/blob/master/src/btree.c) — the `balance()` skip
  condition `pPage->nOverflow==0 && pPage->nFree*3<=(int)pCur->pBt->usableSize*2` with its "less than
  2/3rds of the total usable space" comment; `balance_nonroot()`'s header comment on sibling counts
  being "increased or decreased by one or two in an effort to keep pages nearly full but not over
  full"; `sqlite3BtreeDelete` calling `balance()`.
- [SQLite Database File Format](https://www.sqlite.org/fileformat2.html) — freeblock 2+2-byte
  structure and increasing-offset ordering; the fragment count and its 60-byte cap; pointer-map
  (ptrmap) pages and the largest-root-page header field.
- [`VACUUM`](https://www.sqlite.org/lang_vacuum.html) and
  [`PRAGMA auto_vacuum`](https://www.sqlite.org/pragma.html#pragma_auto_vacuum) — rebuilding to
  reclaim space; auto-vacuum's requirement that it be chosen before tables are created.
- **Measurements in §10.0, §10.1, §10.3 and §10.4** were taken on `sqlite3` 3.37.2 by dumping raw
  pages with a hand-written parser, cross-checked against SQLite's own `dbstat` virtual table (the
  parser's occupancy figure and `dbstat`'s agreed to 76.2%). The `balance()` threshold is the one
  source claim independently confirmed behaviourally rather than only read.


---


**Next:** [11 — Index B-trees](11-index-b-trees.md) — why an index is the same tree with the payload
thrown away, and why the rowid is glued onto the end of every key.