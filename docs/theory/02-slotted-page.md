# 02 — The Slotted Page


> **Read at the start of week 1.** Covers roadmap session 6 (`storage/page.py`, `SlottedPage`).
>
> **Time:** ~20 minutes. **Prerequisite:** [chapter 01](01-pages-and-pager.md).


---


## 2.0 The problem: variable-length things in a fixed-size box


You have a 4096-byte page. You want to put rows on it:


```
(1, "ada", 36)          →  about 12 bytes
(2, "grace", 45)        →  about 14 bytes
(3, "a very long name…", 30)  →  about 200 bytes
```


Three requirements, and they fight each other:


1. **They're different sizes.** You can't compute "where is row 5" arithmetically the way you can with pages.
2. **They must be kept in sorted order by key**, because the entire point of week 2 is binary-searching within a page (chapter 05). A page whose rows are in random order is a page you have to scan linearly.
3. **You'll insert into the middle of that order.** Row with key 25 arrives after keys 1–50 are already placed. It belongs between 24 and 26.


**How do you lay out the bytes?** Take a minute on this. It's a genuinely nice puzzle and the answer is used by essentially every database ever built.


---


## 2.1 Wrong answer 1: pack them in order, front to back


```
┌──────────────┬───────────────┬──────────────────────┬──────────┐
│ (1,"ada",36) │ (2,"grace",45)│ (3,"long name...",30)│  free    │
└──────────────┴───────────────┴──────────────────────┴──────────┘
```


Rows are physically in sorted order. Simple, dense, obvious.


**Why it breaks:** insert key 25 into a page holding keys 1–50, and it has to go physically between 24 and 26. Which means **every byte after it moves.** If the page is 90% full, you're shifting ~3,600 bytes to insert a 15-byte row.


Now count what that costs across a realistic insert. You do a `memmove` of 3,600 bytes — in Python, a slice-and-reassign of a 3,600-byte `bytearray`. That's fast in absolute terms, but it happens on *every insert into a non-empty page*, and it means the page's entire contents are dirtied and must be rewritten to disk even though logically you added 15 bytes.


There's a subtler cost that matters more: **you can't hold a stable reference to a row.** Any insert can move any other row. In week 2 you'll have cursors holding positions inside pages, and in week 4 you'll have index entries pointing at rows. If rows move whenever a neighbour is inserted, every one of those references is invalidated by every insert.


**The insight to extract:** the requirement is that rows be *findable* in sorted order. Nothing requires them to be *stored* in sorted order. Those are different things, and separating them is the whole trick.


---


## 2.2 Wrong answer 2: append rows anywhere, keep a sorted list of pointers


Getting warm. Write rows wherever there's room and keep a separate sorted array of "row 1 is at byte 100, row 2 is at byte 340, …"


This is basically right! But where does the pointer array live?


**Attempt A: at a fixed size near the front.** "Reserve 200 bytes for up to 100 pointers." Now you've hardcoded a maximum cell count per page. Put 100 tiny rows on a page and you run out of pointers with 3 KB of space free. Put 3 huge rows on it and you've wasted 194 bytes of pointer array. Either way you've picked a wrong number, because the right number depends on the data.


**Attempt B: at the end, growing backwards, with rows at the front growing forwards.** This works, and it's just the real answer mirrored. Both regions need to grow and neither can know how much space the other will want, so they have to grow toward each other from opposite ends. Which end holds which is a convention.


---


## 2.3 The answer: two regions growing toward each other


> **Put the pointer array right after the header, growing forward. Put the row data at the very end of the page, growing backward. The free space is whatever is left between them. The page is full when they meet.**


```
byte 0                                                        byte 4095
┌──────────┬──────────────────┬───────────────┬───────────────────────┐
│  header  │  cell pointers → │  free space   │ ← cell content        │
│ 8 or 12 B│  2 bytes each,   │               │   variable length,    │
│          │  SORTED by key   │               │   ARBITRARY order     │
└──────────┴──────────────────┴───────────────┴───────────────────────┘
             grows forward →                    ← grows backward
```


This is called a **slotted page**, and the two-line summary is:


- **The pointer array is sorted by key. The data is in whatever order it was written.**
- **Data, once written, never moves.**


Insert key 25 into a page of keys 1–50:


1. Append its ~15 bytes at the low end of the content area. **Nothing else moves.**
2. Insert its 2-byte offset into the correct position in the pointer array — shifting maybe 50 entries × 2 bytes = **100 bytes**.


Compare: 100 bytes shifted instead of 3,600. And the *data* is stable, so cursors and index entries keep working.


`btreeInt.h` describes exactly this, and its wording is worth having:


> "Cell content is stored at the very end of the page and grows toward the beginning of the page."


with the pointer array using "2 bytes per cell. Sorted order." and the content area holding cells in "Arbitrary order interspersed with freeblocks."


### Two independent wins, and it's worth separating them


People usually explain slotted pages as "so inserts are cheap." That's half of it. The two wins are:


1. **Cheap ordered insert.** Reordering 2-byte integers instead of variable-length rows. This is the one everyone mentions.
2. **Stable addresses.** A row's location doesn't change when its neighbours change. This is the one that actually matters more as the system grows, because it's what makes references *from outside the page* possible.


There's also a third, quieter win: a **slot index is a compact identity**. "The 5th cell on page 47" is a tiny, stable, meaningful address — and it's exactly what a cursor stores (chapter 06 §6.3). Without the slot array you'd have to identify a position by byte offset, which is bigger and which changes.


### Why the *pointers* go at the front and the *data* at the back


Slightly arbitrary, but there's a reason, and `btreeInt.h` names it. The system tries


> "to keep free space after the last cell pointer so that new cells can be easily added without having to defragment the page."


The pointer array has to grow *contiguously* — it's an array, you binary-search it, it can't have holes. The content area doesn't; cells can be scattered with gaps between them. So you put the region with the strict contiguity requirement adjacent to the free space, where it can always extend. The region that tolerates gaps takes the far end.


There's a nice corroborating detail in `btree.c`: `allocateSpace()` deliberately "will avoid using the first two bytes past the cell pointer area," because if you're allocating space for a cell you're about to need a pointer slot for it too. **The allocator reserves room for its own bookkeeping in the same operation.** That's the kind of small correctness detail that's obvious in hindsight and a subtle bug if you miss it.


> **Say this out loud:** "A slotted page has a sorted array of 2-byte offsets growing forward from the header and variable-length cell data growing backward from the end of the page. Inserting into the middle of the sort order means appending the data — which never moves — and shifting a few 2-byte offsets. So you get cheap ordered insertion *and* stable addresses for anything referencing a cell from outside the page."


---


## 2.4 "Free space" is three different things


### The problem


You want to insert a 40-byte cell. `free_space()` returns 300. Can you?


### Your first instinct


"300 > 40. Obviously yes."


### Why that's wrong


Not necessarily — because those 300 bytes may not be **contiguous**.


Here's how that happens. Delete a cell from the middle of the content area and you leave a hole:


```
┌────────┬─────────┬──────────┬───────┬────────┬──────────┬───────┐
│ header │ ptrs →  │   gap    │ cellC │ ~hole~ │  cellA   │ cellB │
└────────┴─────────┴──────────┴───────┴────────┴──────────┴───────┘
                    ↑ 200 B            ↑ 100 B
                   unallocated         freed by a delete
```


Total free: 300 bytes. Largest usable run: 200 bytes. A 250-byte cell doesn't fit *anywhere*, despite "300 free."


So free space partitions into three genuinely different things, and SQLite tracks them separately for good reasons:


| Kind | What it is | Usable directly? |
|---|---|---|
| **Unallocated** | The gap between the last pointer and the first cell | ✅ Yes — the easy case |
| **Freeblocks** | Holes ≥ 4 bytes left by deletes, in the content area | ✅ Yes, if one is big enough |
| **Fragments** | Holes of 1, 2, or 3 bytes | ❌ **Never** — too small to track |


### Why freeblocks need 4 bytes minimum


Because SQLite chains the holes into a linked list, and the link lives *in the hole* — exactly the same trick as the freelist in chapter 01 §1.8, one level down. From the format spec:


- First 2 bytes: the offset of the next freeblock, or zero if this is the last.
- Next 2 bytes: the size of this freeblock, including these 4 bytes of header.


So a hole must be ≥ 4 bytes to describe itself. A 3-byte hole cannot hold its own metadata and is therefore **untrackable** — it's dead space until the page is defragmented. That's what a *fragment* is: not a special case someone invented, but the inevitable consequence of the bookkeeping needing 4 bytes.


**This is the same self-describing-free-space idea appearing at two scales** — free pages hold the next free page number, free byte-runs hold the next free byte-run offset. Noticing that a technique recurs at different scales is a strong sign you've understood it rather than memorised it.


Two more rules from the spec, both invariants your validator can check:


- Freeblocks are "always connected in order of increasing offset." A sorted chain makes coalescing adjacent holes possible — you can tell whether the next hole is contiguous with this one.
- "In a well-formed page there is always at least one cell before the first freeblock." (Because a hole before all cells would just be part of the unallocated region.)


### Where the number 60 comes from


The page header spends **one byte** on the count of fragmented bytes, and the spec says that count "may not exceed 60" in a well-formed page.


Why 60 and not 255, when a byte could hold 255?


Work it out: a fragment is at most 3 bytes, and fragments arise between cells. With a minimum cell size of 4 bytes, you can't have very many cells on a page, and therefore can't have very many inter-cell gaps. 60 is a deliberate cap: exceed it and SQLite defragments rather than continuing to leak unusable bytes. `btree.c` implements the threshold slightly conservatively — `pageFindSlot()` stops accepting slots that are 1–3 bytes oversized once the fragment count exceeds 57, leaving headroom before hitting 60.


**The general lesson is more useful than the number:** if you allow unusable dead space to accumulate without bound, you have a slow leak. So you cap it and trigger a cleanup at the cap. That's the same reasoning behind compaction thresholds in LSM-trees, GC triggers in a managed runtime, and `VACUUM` in Postgres. **Bounded waste plus a cleanup trigger, rather than perfect efficiency.**


### Defragmentation


The fix is to sweep everything tight against the end of the page:


> `defragmentPage()` restores the state where "all unused bytes are contained in the unallocated space region, and all cells are packed tightly at the end of the page." — `btree.c`


Cost: rewriting the whole content area and updating every pointer. It's the expensive operation you avoid until you need it. And note it's *safe* to move cells during defragmentation for a specific reason: everything referencing a cell references it by **slot index**, not byte offset, and you update the offsets as you move. §2.3's "data never moves" is really "data never moves *during ordinary insertion*" — the indirection through the slot array is precisely what makes a bulk relocation possible without invalidating anything outside the page.


### What you should actually build


Your `SlottedPage` API in `implementation.md` is `insert_cell`, `get_cell`, `delete_cell`, `free_space()`. Here's the useful distinction, and it's one worth internalising because it applies to every part of this project:


> **The format tells you what bytes are *legal*. It does not tell you which legal bytes you must *produce*.**


A page whose freeblock offset is 0 and whose fragment count is 0 is a perfectly well-formed SQLite page — it just happens to have no holes. So:


**Skip freeblocks entirely at first. On delete, repack the page:** remove the cell, slide the remaining content tight against the end, rewrite the offsets, set freeblock and fragment fields to 0. It's O(page size) per delete instead of O(1), but it's ~15 lines instead of ~80, it can't leak fragments, and `free_space()` always returns a single contiguous number — so the question at the top of this section can't even arise. `sqlite3` reads such a page without complaint, and `integrity_check` passes.


**What you're giving up is one-directional compatibility.** Your writer produces a strict subset of legal pages. Your *reader* only has to understand freeblocks if you want to open a file that `sqlite3` itself wrote — which is a strictly larger goal than the acceptance test you set (sqlite3 reads what quilldb writes) and a reasonable thing to defer. **Write down which direction you support**, because "SQLite-compatible" is ambiguous and the ambiguity is exactly where an interviewer will press:


> *"quilldb writes files the sqlite3 CLI can read and verify. It doesn't yet read arbitrary sqlite3-written files — it would need freeblock parsing, WAL, non-4096 page sizes, and UTF-16 text. The header validation refuses those explicitly rather than misreading them."*


That's a much stronger sentence than "it's SQLite-compatible," because it names the boundary. Then be precise about the delete tradeoff too: *"I repack on delete rather than maintaining a freeblock chain. It's O(page size) per delete against O(1), but it eliminates fragmentation entirely and makes 'is there room?' a single comparison. SQLite maintains freeblocks because at its write volumes the O(1) matters and it can afford the ~60-byte fragment cap plus a defragmentation path. At my scale, deletes aren't hot."*


Add freeblocks only if a benchmark tells you deletes are hot, or when you want to read foreign files. Neither is week 1.


> **Say this out loud:** "Free space on a page isn't one number — it's the unallocated gap, plus freeblocks (holes of 4+ bytes chained through their own contents), plus fragments (1–3 byte holes too small to hold the 4-byte link, so they're untrackable dead space). SQLite caps fragments at 60 bytes per page and defragments past that: bounded waste with a cleanup trigger, rather than chasing perfect packing."


---


## 2.5 The page header: 8 bytes, or 12, and what each one buys


> Not to be confused with the 100-byte **file header** (chapter 01 §1.6) — that's metadata about the whole file, read once at open. This is metadata about *one* page. Every page carries one, at byte 0 — except page 1, where it starts at byte 100 because the file header is in front of it.


The layout, from the format spec:


| Offset | Size | Field | Notes |
|---|---|---|---|
| 0 | 1 | Page type | 2 = interior index, 5 = interior table, 10 = leaf index, 13 = leaf table |
| 1 | 2 | First freeblock offset | **0 if none** |
| 3 | 2 | Cell count | |
| 5 | 2 | Cell content area start | **0 is interpreted as 65536** |
| 7 | 1 | Fragmented free bytes | the 1–3 byte holes from §2.4 |
| 8 | 4 | Right-most child pointer | **interior pages only — absent on leaves** |


**8 bytes on a leaf, 12 on an interior page.** Then the cell pointer array, immediately after.


### Four things to notice


**1. The header's *size* depends on its own first byte.** You read byte 0, decode the type, and only then know whether the cell pointer array begins at offset 8 or offset 12. That's mildly annoying — it means no fixed `PAGE_HEADER_SIZE` constant, and every offset calculation has to consult the page type.


The obvious alternative is a uniform 12 bytes everywhere, wasting 4 bytes on each leaf so that every offset is a constant. Weigh it honestly: leaves are the overwhelming majority of pages in any tree (chapter 05 §5.3 — fanout ~300 means ~99.7% of pages are leaves), so a uniform header costs ~4 bytes per 4096, about **0.1% of the file.** In exchange you'd delete a branch from every page access.


At *your* scale that's a trade worth taking. At SQLite's — billions of deployments, every byte multiplied by the world, and a 0.1% file-size regression that would show up on every phone on earth — it isn't. **This is the right shape of answer when asked why a design looks awkward: name the resource each side is optimising and note that the constant differs by nine orders of magnitude.** The awkwardness is bought, not accidental.


Since you're writing their format, you take their 8/12. Put it behind one function and stop thinking about it:


```python
def header_size(page_type: PageType) -> int:
    return LEAF_HEADER_SIZE if page_type.is_leaf else INTERIOR_HEADER_SIZE
```


**2. "A zero value is interpreted as 65536."** This is a lovely hack. The content-start field is 2 bytes, so it can express 0–65535. But the maximum page size is 65536, and an *empty* page's content area starts at exactly the end of the page — which is 65536, one too big to store. So they reuse 0, which can never legitimately occur (content can't start at offset 0; the header is there). **Encoding an out-of-range value into an impossible in-range one** — the same trick as the page-size field's `1` meaning 65536 (chapter 01 §1.6) and as "page 0 means null" (§1.5). Three appearances in one format is not a coincidence; it's a habit.


You'll never hit this, because you fixed the page size at 4096. But it's a five-second interview answer that shows you read the spec rather than a blog post about it.


**3. Fragmented free bytes gets one byte; the freeblock offset gets two.** Look at that asymmetry and work out why. A freeblock offset must address any position in a page, so it needs 2 bytes. A fragment *count* is bounded — §2.4 showed it can never usefully exceed 60 — so one byte is generous. **The field widths encode the invariants.** When you see a suspiciously narrow field in a format, there's usually a bound you haven't noticed yet.


**4. The page type is one byte, and it must be validated.** `btree.c`'s `decodeFlags` accepts *only* 2, 5, 10, and 13; anything else is corruption. Note what the values encode: two independent facts (leaf vs interior, table vs index) packed into one enumerated byte rather than separate flag bits.


And notice what the enum does *not* cover: **overflow and freelist pages have no type byte at all.** An overflow page is 4 bytes of next-pointer followed by raw content; a freelist trunk page is an array of integers. Neither is self-identifying — they're known only by how you arrived at them.


That's a genuine weakness, and it's worth being able to say so: a corrupt child pointer that lands on an overflow page produces *garbage interpreted as a cell count*, not a clean "expected leaf, got overflow." Adding a type byte to those pages would cost 1 byte and buy cheap self-validation — a real improvement, and unavailable to you, because those bytes are content in the format you're writing. **Knowing what you'd change if you owned the format is a better answer than either defending it or having changed it.** Your defence against this is the validator's reachability invariant instead (chapter 06 §6.6, invariant 10).


---


## 2.6 The full-page condition, and why it's the trigger for everything


The page is full when the two regions meet:


```python
def free_space(self) -> int:
    header_start = FILE_HEADER_SIZE if self.page_id == SCHEMA_ROOT_PAGE else 0
    pointer_array_end = (
        header_start
        + header_size(self.page_type)              # 8 or 12 — §2.5
        + self.cell_count * CELL_POINTER_SIZE
    )
    return self.content_start - pointer_array_end
```


Three inputs, and **each one of them is a bug you will write.** The page type decides 8 versus 12. Page 1 shifts everything by 100. And a new cell of `n` bytes needs `n + CELL_POINTER_SIZE` — **the data and its offset entry.**


Forgetting the `+ 2` is the classic off-by-one: you'll pass the space check, write the cell, and then overwrite the first byte of your own cell content with the last byte of the pointer array. Symptom: the page looks fine until you read the *first* cell, which is corrupt. Test it explicitly — `roadmap.md` week 1 session 6's success criterion is exactly "fill a page to within 10 bytes, verify the 11th insert is refused cleanly."


Forgetting the page-1 offset is worse, because the byte you overwrite is in the **file header** (chapter 01 §1.6). Symptom: the database opens fine until the schema page fills up, then the magic bytes get clobbered and the file becomes unopenable — by you *and* by `sqlite3`. Write a test that fills page 1 to capacity and then re-opens the database.


This single condition is the trigger for the most important control flow in the project:


```
insert_cell() returns "no room"
        └──► the B+tree splits the page          (chapter 06 §6.4)
                └──► which needs a new page       (chapter 01 §1.8, the freelist)
                        └──► and a new signpost in the parent
                                └──► which may itself be full → cascade
                                        └──► and if the root splits,
                                             the tree grows a level
```


Everything hard in week 2 hangs off "the two regions met." Which is why it's worth being certain about `free_space()` in week 1 — a bug here shows up as a B+tree bug in week 2, and you'll debug the wrong file (`roadmap.md` week 1: "every bug for the next seven weeks will *look* like a B-tree bug").


---


## 2.7 Roads not taken


### Fixed-size slots (no variable-length cells at all)


Give every slot the same size, sized for the largest row. Now the slot array is unnecessary — slot *i* is at a computable offset — and there's no fragmentation ever.


**Used by:** simple embedded formats, some in-memory structures, and column stores for fixed-width columns.


**Why not here:** you'd have to pick a maximum row size, and the waste is the difference between that maximum and the average, on every row. A schema with a `TEXT` column has no sensible maximum. This is wrong answer 3 from chapter 01 §1.3, one level down, and it fails the same way.


### Log-structured pages (append-only, never update in place)


Never modify a cell. Append a new version and treat the newest as current. Reads scan backward or consult an in-memory index; periodically compact.


**Used by:** LSM-tree engines (RocksDB, LevelDB, Cassandra) at the file level; your SSD's FTL at the flash level (chapter 00 §0.3).


**Why it's attractive:** every write is sequential, which per chapter 00 §0.5 is 2–200× faster than random. And there's no in-place update, so **torn writes become almost impossible** — you never overwrite live data, so a failed write damages only the thing you were adding.


**Why not here:** reads get slower (you may check several places for the current version), you need compaction (a whole background subsystem with its own scheduling, write-amplification, and tuning problems), and space usage becomes unpredictable. The B+tree's read performance and predictability are what you want, and chapter 05 §5.8 works through the full B-tree-vs-LSM comparison.


### Copy-on-write pages


Never modify a page in place. Copy it, modify the copy, write it to a *new* page number, and update the parent to point at the new location — which means copying the parent too, all the way to the root, ending in one atomic root-pointer swap.


**Used by:** LMDB, BoltDB, ZFS, btrfs.


**Why it's beautiful:** you get atomicity almost for free. There's no journal at all — the old tree remains completely intact and readable until the single root swap makes the new one live. That also gives you **free MVCC**: a reader holding the old root sees a perfectly consistent snapshot, and readers never block writers.


**Why not here:** every write to a leaf dirties the whole path to the root (~4 pages instead of 1), the file grows on every write until you reclaim old versions (which needs its own free-space management with a notion of "no reader still needs this version"), and it works best with mmap, which conflicts with your journal design (chapter 01 §1.9).


**But notice how much of week 5 it would eliminate**, and be ready to say so: *"Copy-on-write would have given me atomicity without a journal, via a single root-pointer swap, plus snapshot isolation for free. I chose in-place update with an undo journal because it's the design SQLite uses and I wanted the fsync-ordering and crash-recovery problem specifically — that's the transferable skill. The COW version is arguably less code and it's what I'd pick if the goal were the product rather than the learning."* That answer is better than a defence, because it's an accurate assessment of a real tradeoff.


### Row-major vs column-major


Everything here assumes a **row store**: all of a row's columns are adjacent, so fetching one row is one read. A **column store** puts all values of one column together instead.


**Why column stores exist:** `SELECT AVG(age) FROM users` in a row store reads every byte of every row to use 4 bytes of each. In a column store it reads only the age column — often 50× less I/O — and because the values are homogeneous they compress far better (run-length, dictionary, delta encoding) and vectorise cleanly on modern CPUs.


**Why not here:** every single-row operation becomes N reads instead of one, so `INSERT` and point lookups get much worse. Row stores serve OLTP (many small transactions, whole rows); column stores serve OLAP (few huge scans, few columns). SQLite, Postgres, MySQL are row stores; ClickHouse, DuckDB, BigQuery, Parquet are column stores.


Worth knowing as a one-liner because it's an extremely common interview question, and the answer is not "column stores are newer and better" — it's **"which axis does your workload read along?"**


---


## 2.8 What you're building


`roadmap.md` week 1 session 6, `storage/page.py`:


```python
class SlottedPage:
    def insert_cell(self, index: int, data: bytes) -> None: ...
    def get_cell(self, index: int) -> bytes: ...
    def delete_cell(self, index: int) -> None: ...
    def free_space(self) -> int: ...
```


Note the API takes a **slot index**, not a key. The page doesn't know what a key is — that's the B+tree's job in week 2. This page knows only "an ordered sequence of byte strings." Keeping it that dumb is what lets the same class serve table leaves, table interiors, index leaves and index interiors without modification.


**Invariants worth asserting** (these become validator checks in chapter 06 §6.6):


1. Every cell offset lies within `[header_start + header_size + 2*cell_count, PAGE_SIZE)`, where `header_start` is 100 on page 1 and 0 elsewhere. *(A pointer into either header or past the page end is corruption — and per `roadmap.md` §7, it must raise a typed error, never an `IndexError`.)*
2. No two cells overlap.
3. `free_space()` is non-negative, always.
4. `insert_cell` then `get_cell` at the same index round-trips exactly.
5. Inserting a cell needing more than `free_space()` raises cleanly and **leaves the page unmodified.** *(Half-applied mutation is the bug that becomes unfixable in week 5.)*
6. After `delete_cell(i)`, the cell formerly at `i+1` is now at `i`, and the count decreased by one.


Invariant 5 is the one to be careful about. "Check space, then mutate" must be genuinely atomic in the sense that a rejected insert leaves no trace — because in week 5, a partially-mutated page that was never journalled is unrecoverable damage.


---


## 2.9 Check yourself


1. Why can't rows be stored in sorted order physically?
2. Name the two independent benefits of the slot array, and say which matters more as the system grows.
3. Why does the pointer array go next to the free space rather than the content area?
4. `free_space()` says 300 and your cell needs 250. Why might it still not fit?
5. Why is a freeblock's minimum size 4 bytes? What is a "fragment" and why is it unusable?
6. Where does the number 60 come from, and what's the general principle it illustrates?
7. Why is it safe to move cells during defragmentation, when §2.3 said data never moves?
8. A leaf's page header is 8 bytes and an interior page's is 12. What would a uniform 12 cost, and at what scale does that trade flip?
9. Why does the fragment count get one byte while the freeblock offset gets two?
10. Why does SQLite interpret a content-start value of 0 as 65536? Name the two other places the same trick appears.
11. Overflow and freelist pages have no type byte. What corruption does that let through, and what defends against it instead?
12. A new cell of `n` bytes needs how much free space, and what are the *three* ways `free_space()` can be wrong?
13. Copy-on-write pages would remove most of week 5. Why aren't you doing it?
14. `SELECT AVG(age) FROM users` — why is a column store 50× faster, and what does it give up?


---


## 2.10 Sources


- [`btreeInt.h`](https://github.com/sqlite/sqlite/blob/master/src/btreeInt.h) — the page-layout diagram; "Cell content is stored at the very end of the page and grows toward the beginning of the page"; pointer array "2 bytes per cell. Sorted order."; content "Arbitrary order interspersed with freeblocks"; keeping "free space after the last cell pointer so that new cells can be easily added without having to defragment the page"; "Unused space within the cell content area is collected into a linked list of freeblocks"; "A group of 3 or fewer free bytes is called a fragment."
- [`btree.c`](https://github.com/sqlite/sqlite/blob/master/src/btree.c) — `allocateSpace()` avoiding the two bytes past the pointer area; `pageFindSlot()`'s fragment threshold of 57; `defragmentPage()`'s post-condition; `decodeFlags` accepting only page types 2, 5, 10, 13.
- [SQLite Database File Format](https://www.sqlite.org/fileformat2.html) — the 8-vs-12-byte header table; freeblock 2+2-byte structure and increasing-offset ordering; "at least one cell before the first freeblock"; fragment count "may not exceed 60"; content-start 0 meaning 65536; total-free-space definition; the defragmented state.


---


**Next:** [03 — Encoding: varints and records](03-encoding-varints-and-records.md) — why a small number shouldn't cost 8 bytes, and how to read column 5 without decoding columns 1 through 4.



