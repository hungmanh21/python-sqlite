# 06 — B-Tree Mechanics


> **Read during week 2, before the split sessions (6 and 7).**
>
> **Time:** ~50 minutes. **Prerequisite:** [chapter 05](05-why-b-trees.md) for the *why*; [chapter 02](../storage/02-the-slotted-page.md) for the page layout this all sits on.
>
> This is the chapter for the hard part. `guide.md` warns that week 2 sessions 6–7 will overflow their two hours; `references.md` says "this is the week where a good walkthrough saves you the most time." Both are right. Read §6.4 and §6.6 before you start coding, not when you're stuck.


---


## 6.0 Two kinds of cell, and why they're different shapes


Chapter 02 gave you a page that stores an ordered sequence of opaque byte strings. Chapter 05 said interior pages navigate and leaves hold data. Cross that with table-vs-index and you get **four** cell formats, which is exactly why the page type byte enumerates four values:


```
TABLE LEAF (0x0D):      [ payload len varint ][ rowid varint ][ payload ][ overflow u32? ]


TABLE INTERIOR (0x05):  [ left child u32 ][ rowid varint ]


INDEX LEAF (0x0A):      [ payload len varint ][ payload ][ overflow u32? ]


INDEX INTERIOR (0x02):  [ left child u32 ][ payload len varint ][ payload ][ overflow u32? ]
```


The trailing overflow page number is present only when the payload actually spilled (§6.5).


Read down that list and three rules fall out, each of which is the answer to a "why is it like that?" question:


**Interior cells begin with a 4-byte child pointer; leaf cells never have one.** That's the navigate-vs-store split made physical.


**Table cells carry a rowid; index cells don't.** A table b-tree is keyed by a 64-bit integer, so the key is a varint sitting next to the payload. An index b-tree's key *is* the payload — an encoded record of the indexed columns followed by the rowid — so there's nothing separate to store. This is why index comparison needs the record ordering from chapter 03 §3.8 while table comparison is just integer comparison.


**Table interior cells have no payload at all.** Not "a small payload" — none.


### Watch the field order in the table leaf cell


Payload length comes **first**, then the rowid. That ordering is easy to get backwards, and getting it backwards produces a file that `sqlite3` will read as corrupt in a confusing way — it'll interpret your rowid as a payload length, which is usually a small number, so it will report a cell that's implausibly short rather than saying anything about rowids.


Why that order? Because the length is what you need to *skip* the cell, and skipping is more common than reading. When SQLite walks a page's cells to compute free space, or to defragment, it needs each cell's size and nothing else. Putting the size first means `cellSizePtr()` reads one varint and stops. **Front-load the field that the cheapest operation needs** — the same principle as the child pointer below, and as chapter 03 §3.7's record header.


### Why the interior cell has no payload


Chapter 05 §5.5: payload in interior cells destroys fanout. An interior cell at ~12 bytes gives fanout ~300; at ~92 bytes it gives ~44, costing two extra page reads on every lookup forever. **The absence of payload is the whole design.**


Notice that index interior cells *do* carry a payload, and must — the separator key is the indexed value, there's nowhere else to put it. That's precisely why index b-trees have a *lower* fanout than table b-trees on the same page size, and why the overflow threshold formula in §6.5 is stricter for index pages (`((U-12)*64/255)-23`, roughly a quarter page) than for table leaves (`U-35`, nearly a whole page). **Two different formulas because two different things are scarce.**


### Why the child pointer comes first


Fixed-width field first, variable-width after. You can read the child pointer with a single `struct.unpack('>I', ...)` at a known offset — no varint decoding needed. Since descent reads *only* the child pointer (once you've decided which cell to follow, you don't care about its key any more), the hot path is a fixed-offset read.


SQLite does the same thing for the same reason. From the format spec, a table interior cell is "4-byte big-endian page number [then] varint integer key," and `findCellPastPtr()` exists specifically to skip "the initial 4-byte child pointer found on interior pages."


**General principle worth extracting: put fixed-width fields before variable-width ones, so the fixed ones are addressable without parsing.** It's the same reasoning as the record header in chapter 03 §3.7 — front-load what you need to navigate.


### Why the leaf cell needs an explicit payload length


Couldn't you infer it from the next cell's offset? No — chapter 02 §2.3 established that cells sit in **arbitrary order** in the content area. The cell physically after cell 3 in memory might be cell 17. The slot array is sorted; the content isn't. So each cell must state its own length.


There's a second reason that matters more: **overflow.** When a payload is too big for the page (§6.5), only part of it is stored locally, and you need to know the *total* size to know how much is in the overflow chain. SQLite's leaf cell stores exactly that — "varint total payload bytes (including overflow)."


### N keys, N+1 children


An interior page with *N* keys has *N+1* children — there's one more gap than there are dividers:


```
        ┌──────────┬──────────┬──────────┐
        │ key: 100 │ key: 500 │ key: 900 │       3 keys
        └──────────┴──────────┴──────────┘
       ↙          ↓          ↓          ↘
   [<=100]   [101..500]  [501..900]   [>900]     4 children
```


Three of those pointers pair naturally with a key. **The last one doesn't** — the "everything greater than the largest key" child has no key of its own. It lives in the page header, at byte 8 in your layout, and it's called the **right-most child pointer**.


> ⚠️ **This is bug number one in week 2, and it's near-universal.** The rightmost pointer is the field people forget, because it's the only child that isn't in the cell array. Symptoms: keys larger than any separator become unreachable; inserts at the high end vanish; a split of the rightmost child corrupts the parent. `references.md` names it explicitly: "Where the bugs will be: propagating a split when the parent is *also* full, and the right-most child pointer."
>
> **Two defences.** First, make it structural: write a single `children(page)` helper that returns *all* N+1 child pointers — the cell ones plus the header one — and never index into cells directly anywhere else. Second, make it a validator invariant: the number of children must always equal cell count + 1 (§6.6).


SQLite's layout matches: 12-byte interior header vs 8-byte leaf, and the extra 4 bytes are exactly "Right child (the Ptr(N) value). Omitted on leaves" (`btreeInt.h`).


---


## 6.1 Search: the descent, and the comparison that will bite you


```python
def search(self, key: int) -> tuple[int, int] | None:
    page_id = self.root
    while True:
        page = self.pool.get_page(page_id)
        if page.type is PageType.LEAF:
            slot = binary_search_leaf(page, key)
            return (page_id, slot) if slot is not None else None
        page_id = child_for_key(page, key)     # descend
```


Two nested searches: **descend** between pages, **binary search** within a page. Chapter 05 §5.3's arithmetic in code.


### The boundary condition, and why it's the split bug in disguise


Given an interior page with separator 500, does key 500 go left or right?


**Both conventions are correct** as long as you're consistent, and inconsistency is the single most common source of silently-lost keys. SQLite's convention, stated in the format spec:


> "Pointers left of key X ⇒ all keys **≤ X**; pointers right of X ⇒ all keys **> X**."


So the left child is **inclusive** of the separator. Key 500 goes **left**.


Pick this convention (matching SQLite means you can check your work against their spec) and write it in `docs/file-format.md` in exactly those words. Then:


- **descent** uses `key <= separator → go left`
- **split** must promote a separator that makes this true — which means promoting the **largest key in the left half**, not the smallest key in the right half.


Those two must agree. Get them one apart and you produce the corruption `guide.md` §2.4 describes: *nothing crashes, every page is structurally valid, and exactly one key per split is unreachable.* You find out 200 inserts later when a scan silently returns 9,997 of 10,000 rows.


> **The reason this bug is so nasty is worth naming: it's a bug where two pieces of code have to agree about a convention, and neither one is wrong on its own.** No local inspection finds it. Only a global invariant check does — which is exactly the argument for §6.6.


### Why binary search within the page, and what it costs


An interior page has ~300 cells. Linear scan is 300 comparisons; binary search is ~8. Both are in RAM, both are microscopic next to the 50 µs page read.


So **why bother?** Two reasons:


1. **In Python, 300 iterations of a loop that decodes a varint each time is genuinely slow** — possibly a meaningful fraction of the page read time. This is one place where interpreter overhead makes an in-memory optimisation actually matter.
2. **It's the same code path as `seek()`**, which needs "the position where this key would be" for range scans — and binary search naturally yields an insertion point on a miss, where linear scan needs extra bookkeeping.


⚠️ Binary search over the *slot array* is what makes this work — you binary-search the sorted 2-byte offsets and dereference to read each key (chapter 02 §2.3). You cannot binary-search the cell content, because it's in arbitrary order. This is the payoff for the slotted page design and it's worth noticing that the two chapters lock together here.


`docs/implementation/week-2-*.md` session 2 has you build this against a **hand-built page** before insert works. Do it that way. Testing search against a fixture you constructed by hand means a failure is unambiguously in search, not in insert.


---


## 6.2 A brief word on testing order, because it saves a day


`roadmap.md` week 2 sessions 3 and 4 say: hand-build a 2-level tree in a fixture, then implement `search` and `TableCursor` against it — *before* `insert` works.


This looks like extra work. It is the opposite.


If you build insert first and then search, then when search fails you have two suspects and no way to isolate them. A hand-built fixture is a **known-good input**: any failure is in the code under test. You'll write maybe 30 lines of fixture and save several hours.


**The general technique** — construct your data structure by hand at the byte level so that readers can be tested independently of writers — is worth having as a habit. It's the same instinct as the `dict`/`sorted()` oracle in `roadmap.md` §7: get a source of truth that doesn't depend on the code you're testing.


---


## 6.3 Cursors: a position you can move


A cursor is a **position in the tree** that can advance. Chapter 05 §5.4 argued this is the capability a hash index lacks and the reason B+trees won.


```python
class TableCursor:
    path: list[tuple[int, int]]   # [(page_id, slot), ...] root → leaf
    def first(self)  -> None: ...
    def last(self)   -> None: ...
    def seek(self, key) -> bool: ...
    def next(self)   -> bool: ...
    def prev(self)   -> bool: ...
```


### Why a path stack rather than just the leaf


Because of what happens when a leaf runs out.


You're at the last cell of a leaf and call `next()`. You need the *next leaf* — which is a different page, and to find it you must go up to the parent and take its next child. If the parent is also exhausted, up again.


So the cursor stores the whole path from root to leaf, with the slot index taken at each level. `next()` becomes:


```
1. slot += 1 in the current leaf.
2. Still in range? Done.
3. Otherwise pop up to the parent, advance ITS slot.
4. If the parent is also exhausted, pop again — repeat.
5. Once you've advanced some ancestor, descend leftmost from there to a leaf.
```


Amortised O(1) per row: most `next()` calls are step 1, and the pops are paid for once per leaf.


### Three things that must be true, and are easy to get wrong


**1. The cursor holds pins on its entire path.** Every page in the path must be pinned (chapter 04 §4.6), or eviction can pull a page out from under it. `roadmap.md` week 6's success criteria include "a cursor's pinned pages are never evicted out from under it, under contention" — but the *pinning* has to be right in week 2 or that test can't pass.


This is also where a pin leak comes from: a cursor abandoned mid-scan (an exception, a `break`, a generator never exhausted) leaves its whole path pinned forever. Make the cursor a context manager, or give it a `close()` and call it in `finally`.


**2. The rightmost child must be part of iteration.** §6.0's warning, again — an interior page's children are cells 0..N-1 *plus* the header pointer. If `next()` iterates only the cells, the last child of every interior page is never visited, so you silently skip a whole subtree. Symptom: a scan returns most of the table. Use the `children()` helper.


**3. What happens if the tree is modified mid-scan?** `guide.md` doesn't raise this and it will occur to you in week 6. If a split happens while a cursor is parked, its saved path may point at slots that have moved. Real systems either keep the cursor's key and re-seek (SQLite saves the cursor position and restores it — `BtCursor` has this machinery), or hold locks that prevent it.


**Decide now, document it, and make it explicit:** the simplest correct rule is *"a cursor is invalidated by any mutation to its tree; using one afterwards raises."* That is a legitimate design (many APIs work this way — including Python's own "dictionary changed size during iteration"), it's one flag to implement, and it turns an undefined behaviour into a clean error. Write it in `docs/architecture.md`. Then in week 6, 2PL makes it automatically true for concurrent transactions, and you'll be glad you named the rule early.


---


## 6.4 Insert and the split cascade


### The easy case (session 5)


1. Descend to the leaf where the key belongs.
2. Binary-search for the insertion slot.
3. `insert_cell(slot, encoded)`.


Done, if there's room. Most inserts are this. Get it fully working — insert at the start, at the end, in the middle, into an empty page — before touching splits.


### The hard case (sessions 6 and 7)


There isn't room. Chapter 02 §2.6: the two regions met.


```
Before:  leaf holds keys 1..50, FULL.  Insert key 25.


1. Allocate a new page from the freelist            (chapter 01 §1.8)
2. Move roughly half the cells — 26..50 — to it
3. Insert key 25 into the left page, which now has room
4. Promote a separator into the parent:
        "keys <= 25 → left page;  keys > 25 → new page"
```


Step 4 is **separator promotion** and it's where the bugs are.


### What makes step 4 hard


**It writes three pages.** Left leaf, new right leaf, parent. All three must be consistent with each other. Any two-out-of-three state is corruption — and note that this is also precisely why week 5 exists: three page writes that must be atomic, on hardware that gives you no atomicity (chapter 00 §0.7).


**The separator must match your descent convention.** §6.1: with "left is inclusive," promote the **largest key in the left half**. Off by one and one key becomes unreachable per split — invisible, cumulative, and discovered much later.


**The parent might be full.** Then the parent splits, promoting a separator to *its* parent. Which might also be full. **The cascade.**


**The root might split.** The root has no parent. So you allocate a new page, put one key and two children in it, and **make it the new root** — updating the header's root page number. Chapter 05 §5.6: this is the only operation that changes the tree's height, and it changes it for every leaf at once.


> ⚠️ **The root's page number changes.** So anything caching "the root of table X" must be invalidated — the catalog (week 3), open cursors, the buffer pool's notion of the tree. It's a small thing that produces a very confusing bug: everything works until the first root split, then lookups go to a page that is no longer the root.
>
> **The standard fix, and it's worth knowing as a technique:** keep the root's page number *fixed* and, on a root split, copy the old root's contents to a *new* page and rewrite the original page as the new root. Then "the root of table X" is a constant forever. SQLite does exactly this — a table's root page number never changes, which is why `sqlite_schema` can record it once. Costs one extra page copy per root split, which happens ~4 times in the life of a table. **Do this.** It's strictly less code than invalidation everywhere.


### Implementation advice that will actually help


**Write the split as a pure function first.** Something like:


```python
def split_cells(cells: list[bytes], keys: list[int], is_rightmost: bool)
        -> tuple[list[bytes], list[bytes], int]:
    """Return (left_cells, right_cells, separator_key). No I/O, no pages."""
```


Now the hardest logic in the project is a pure function you can unit-test exhaustively — every cell count, every size distribution, both the rightmost and middle cases — with no pager, no fixtures, and no page arithmetic. Then the page-level code is bookkeeping around a function you already trust.


This is the single most useful structural suggestion in this chapter. `guide.md` says session 6 will take "the full session and then some"; separating the decision from the mutation is how you make that true rather than an understatement.


**Do the rightmost-split optimisation here.** Chapter 05 §5.7: if the splitting page is the rightmost child of its parent, split at the *last* cell rather than the middle. Five lines, takes sequential-insert occupancy from 50% to ~100%, halves your file size on the most common insert pattern there is. Add it *after* the 50/50 split passes tests, with a test asserting each path is taken when expected.


**Handle the cascade recursively, and think about the recursion's shape.** The natural implementation is: `insert_into(page, cell)` returns either `None` or a `(separator, new_page_id)` to be inserted into the parent. The recursion unwinds naturally up the path stack, and the base case — the root returning a promotion — is where `balance_deeper` happens. Tree height bounds the depth at ~4, so recursion depth is never a concern.


---


## 6.5 Overflow: payloads too big for a page


### The problem


A page is 4096 bytes. Someone inserts a 10 KB `TEXT` value. **Where does it go?**


### The answer


Store what fits on the leaf, then a page number pointing to a continuation page, which holds more data plus a pointer to the next — a linked list.


```
 leaf cell:  [key][total len = 10240][ first ~4000 bytes ][ overflow → p91 ]
                                                                     │
 page 91:    [ next → p92 ][ ~4092 bytes of content ]  ◄──────────────┘
 page 92:    [ next → p93 ][ ~4092 bytes ]
 page 93:    [ next →  0  ][ remaining bytes ]              ← 0 = end (chapter 01 §1.5)
```


Each overflow page spends its first 4 bytes on the next-page number and the rest on content. From the format spec: "The first four bytes of each overflow page are a big-endian integer which is the page number of the next page in the chain, or zero for the final page."


### Why not just allow big pages?


Because the page size is fixed by hardware alignment (chapter 00 §0.4) and by fanout (chapter 05 §5.3). You can't grow the page for one big row without making every page bigger for every row.


### Why not store *all* the payload in overflow whenever it doesn't fit?


Here's where SQLite's design gets interesting, and the reasoning is genuinely worth understanding. The thresholds, from the format spec, with **U** = usable page size and **P** = payload size:


```
X = U - 35                    max payload stored locally, TABLE LEAF pages
X = ((U-12)*64/255) - 23      max payload stored locally, INDEX pages
M = ((U-12)*32/255) - 23      MINIMUM payload that must stay local, always
K = M + ((P - M) % (U - 4))


if P <= X:              store all P locally, no overflow
elif K <= X:            store first K locally, spill P-K
else:                   store first M locally, spill P-M
```


That looks arbitrary. It isn't — there are exactly three ideas in it.


**Idea 1: keep the record header local.** The spec states the goal directly: the thresholds are designed "to make sure enough of the payload is on the b-tree page that the record header can usually be accessed without consulting an overflow page."


Recall chapter 03 §3.7: a record is a header of serial-type codes followed by a body. To know a row's *types* — or to extract one small column — you need the header, which is at the front. If the whole payload spilled, then reading *any* column of a large row would cost an extra page read. By guaranteeing a minimum **M** bytes stay local, the header is almost always local, and `SELECT id FROM big_table` never touches an overflow page.


**That's the payoff for chapter 03's manifest-then-body layout, and it only works because the header is at the front.** The two design decisions are coupled.


**Idea 2: guarantee a minimum fanout.** Also stated: "to give a minimum fanout of 4 for index b-trees." If a single cell could occupy an entire page, an interior page might hold one key and two children — a binary tree, with all of chapter 05 §5.2's problems. So a cell is capped at roughly a quarter of the page, guaranteeing ≥4 children per interior page and therefore a bounded height. `btreeInt.h` gives the same reasoning for why the max embedded payload fraction is 64 out of 255: "to limit the maximum cell size so that at least 4 cells will fit on one page."


Note the numbers 64 and 32 out of 255 — those are the "maximum embedded payload fraction" and "minimum embedded payload fraction" from the file header, which the spec says **must be** 64 and 32. They're *stored* in the header for a format that never allowed them to vary. A vestigial degree of freedom, frozen at its only legal value — which is a very common thing to find in mature formats and a nice bit of colour.


**Idea 3 — the elegant one: make every overflow page exactly full.** This is what the `K = M + ((P - M) % (U - 4))` formula is for, and nothing in the docs spells out the arithmetic, so here it is.


Each overflow page holds `U - 4` bytes of content (the page minus its next-pointer). Suppose you store `K` bytes locally. Then the spilled amount is `P - K`, and:


```
P - K  =  P - M - ((P - M) mod (U - 4))
```


which is **exactly divisible by `(U - 4)`.**


So the spilled data fills a whole number of overflow pages with **zero bytes wasted on the last one.** The formula picks the local amount precisely so that the remainder lands locally rather than in a half-empty final overflow page.


`btree.c` confirms this is the intent: the local/overflow split aims "to minimize the amount of unused space on overflow pages." And the fallback (`else: store M`) handles the case where that ideal `K` would exceed the local maximum `X` — then you store the minimum `M` and accept a partial last page.


**That's a lovely piece of design**: one modulo operation converts "up to 4 KB wasted per large row" into "zero wasted." Being able to derive it — not just quote it — is exactly the kind of thing that makes an interviewer lean in, because it demonstrates you read a formula and worked out what it was *for*.


### What you should build


`roadmap.md` week 2 includes overflow chains; the descope ladder (§5) lists them as the *last* thing to cut, replaced by "a hard row-size limit with a clear error."


**Do implement them, and use the real formulas — this is the one place in the project where "close enough" produces silent corruption.** Think about why. The threshold decides *how many bytes of payload sit in the cell*. A reader doesn't rediscover that number; it recomputes it from the same formula and reads exactly that many bytes before following the overflow pointer. So if your writer keeps 1024 bytes locally where the formula says 1002, `sqlite3` reads 1002 bytes of payload and then interprets your next 22 payload bytes as an overflow page number. **Two implementations of one formula that must agree exactly, with no field recording the answer** — the same class of hazard as §6.1's descent convention, and the reason it can't be approximated.


```python
U = USABLE_SIZE                          # page_size - reserved_space


def max_local(page_type: PageType) -> int:
    """X — the most payload that may live on the page."""
    if page_type is PageType.LEAF_TABLE:
        return U - 35                    # 4061 at U=4096
    return ((U - 12) * 64 // 255) - 23   # 1002 at U=4096, index pages


MIN_LOCAL = ((U - 12) * 32 // 255) - 23  # M — 489 at U=4096


def local_payload_size(page_type: PageType, p: int) -> int:
    x = max_local(page_type)
    if p <= x:
        return p                         # no overflow at all
    k = MIN_LOCAL + ((p - MIN_LOCAL) % (U - 4))
    return k if k <= x else MIN_LOCAL
```


Use **integer** division, matching C's truncation — `//`, not `/`. And note the numbers: a table leaf holds up to 4061 bytes locally, nearly the whole page, while an index page holds only 1002. That asymmetry is idea 2 at work: index interior cells carry their separator key, so capping them at a quarter page is what guarantees fanout ≥ 4.


> ⚠️ **U is a property of the file, not of the page.** Page 1 has 100 fewer *usable bytes available* (chapter 01 §1.6), but `U` in these formulas is still `page_size - reserved_space`. Don't subtract 100 here. Page 1's shortfall affects whether the cell *fits*, which is `free_space()`'s job (chapter 02 §2.6) — not how the payload is split, which is this function's job. Conflating the two gives you cells that are internally correct and won't fit, or fit and are misparsed.


The modulo line is idea 3, and it's one line, so there's no reason not to have it: *"I use the same remainder trick as SQLite, so every overflow page except possibly the last is completely full — the local amount is chosen so the spilled remainder divides evenly by the overflow page capacity."*


### ⚠️ Two hazards


**Cycles.** If page 91 points to 92 and 92 points back to 91, a reader loops forever — and this is exactly what a corrupted or malicious file will contain. Two defences, and you want both: the validator checks for cycles (§6.6), and the reader **caps its hops** at `total_length / (U-4) + 2`. `roadmap.md` §7's corruption suite requires that an overflow cycle "raise typed errors rather than hanging or recursing forever." A hang is worse than a crash: no stack trace, no error, just a process that stops.


**Freeing the chain.** Deleting a row with overflow must return *every* page in the chain to the freelist. Walk it first, collect the page numbers, then free them. Miss this and you have a genuine space leak — pages unreachable and unallocatable. Test it: insert a 100 KB value, delete it, check the file doesn't grow when you insert it again.


---


## 6.6 The validator: why you write it before you need it


This is the most important section in the chapter, and the argument is not the obvious one.


### The problem with split bugs


Suppose your split promotes `27` where it should promote `26`.


- **Nothing crashes.** Every page has a valid header, a valid cell count, valid offsets.
- **Every page is internally consistent.** Read any page in isolation and it looks perfect.
- **Only the *relationship between* pages is wrong.** The parent claims keys ≤ 27 are left; the left page's largest key is 26; key 27 lives on the right page but lookups for it go left.
- **The failure surfaces far from the cause.** You discover it hundreds of operations later, as a scan returning 9,997 of 10,000 rows.


So the bug has **no locality in space** (it's between pages, not in one) and **no locality in time** (found long after it's caused). Those are precisely the two properties that make debugging hard, and they're why ordinary unit tests are weak here — a unit test on the page you just wrote passes.


### The answer: check global invariants after every mutation


```python
def validate_btree(pager, root_page_id) -> ValidationReport: ...
```


Then in tests:


```python
for key in random_keys:
    tree.insert(key, payload)
    assert validate_btree(pager, root).is_valid      # after EVERY insert
```


This converts "silently wrong, discovered 200 inserts later" into **"raised immediately, on the operation that caused it."** You get the failing operation, the failing page, and the violated invariant — which is usually enough to fix the bug without a debugger.


**That's the real argument, and it's worth stating precisely: the validator doesn't find bugs you couldn't find otherwise. It relocates the failure to the cause.** Most of your week-2 debugging time would otherwise be spent bisecting *when* the corruption happened, and that's the part it eliminates.


### The invariants, and what each one catches


| # | Invariant | Catches |
|---|---|---|
| 1 | Keys within a page are **strictly increasing** | A bad insertion slot; a split that mis-partitioned |
| 2 | Every key on page P lies within the range its parent's separators claim | **The off-by-one separator (§6.1).** The most valuable check here |
| 3 | Child count == cell count + 1 (**including the rightmost pointer**) | The forgotten rightmost pointer (§6.0) |
| 4 | Every page is reachable **exactly once** from the root | A page claimed by two parents (double-linked after a split); an orphan (allocated but never linked) |
| 5 | **All leaves at the same depth** | A promotion that went to the wrong level; a botched root split |
| 6 | Every cell offset lies within the page, and no two cells overlap | Page-level corruption (chapter 02 §2.8) |
| 7 | No overflow chain contains a **cycle**; chain length matches the declared payload size | Corrupt or malicious files (§6.5) |
| 8 | Page types are valid, and leaves/interiors appear only where expected | A pointer landing on a freelist or overflow page |
| 9 | Every non-root page is at least ~half full *(optional — see below)* | Split logic that under-fills |
| 10 | Pages in all b-trees ∪ freelist trunks ∪ freelist leaves ∪ overflow pages == every page in the file | **Space leaks** — the real reason to build this |


Invariants **2, 4, and 5** are the ones that catch split bugs specifically. Invariant **10** is the one that catches the leaks §6.5 warns about, and it's the one people don't think of. Note that page 1 needs no special mention in invariant 10: it's the `sqlite_schema` root, so it's reachable as a b-tree page like any other.


### And you get a second, adversarial validator for free


Write yours anyway — the argument above stands, and it's your validator that reports *which invariant* broke. But then add one more line to the same test:


```python
assert integrity_check(db_path) == "ok"     # subprocess to the sqlite3 CLI
```


`PRAGMA integrity_check` walks every b-tree, verifies cell ordering and page-type validity, follows every overflow chain, cross-checks the freelist count against the actual chain length, and confirms that every page is accounted for exactly once. That's roughly invariants 1, 3, 4, 6, 7, 8, and 10 — implemented in C, by people who have been finding these bugs since 2000, tested against a corpus that includes decades of real-world corruption.


**Which is the stronger argument for writing the format exactly rather than one you designed yourself.** It isn't the interop party trick; it's that you get an independent reference implementation to check your work against, on every single test. The two validators fail differently and that's the point: yours says *"invariant 2 violated on page 47"* and is fast enough to run after every insert; theirs says *"row 3 missing from index"* and knows about failure modes you haven't thought of. Run yours in the inner loop and theirs at the end of each test.


⚠️ **Invariant 9 is a trap** if you implement the rightmost-split optimisation (chapter 05 §5.7) — that optimisation deliberately creates a nearly-empty right page, and it deliberately never merges after delete (chapter 01 §1.8's documented limitation). So the honest version is: *"at least half full, except the rightmost page at each level, and except after deletion."* Either encode those exceptions or make invariant 9 a warning rather than an error. **Don't let a legitimate design decision produce a failing validator** — you'll start ignoring it, and then it's worthless.


### Also write `dump_tree()` — before you're stuck


```python
def dump_tree(pager, root, depth=0) -> str:
    """Print the tree as indented text."""
```


`guide.md` §2.6 is right that "80% of 'I'm stuck' is actually 'I can't see what's happening.'" A validator tells you *that* something is wrong and which invariant broke; `dump_tree` shows you *what the tree looks like*. You need both, and you need them before session 6, not during it.


Make the output diffable — one line per page, sorted, with page numbers and key ranges. Then "dump before insert, dump after insert, diff" is your primary debugging tool, and it shows you *exactly* which pages a split touched.


### And it's an interview asset, for a specific reason


`roadmap.md` §9 lists "hardest bug you've fixed" as answerable from exactly this. The version that lands is not "I wrote a validator." It's:


> *"B+tree split bugs have a nasty property: they produce structurally valid pages, so nothing crashes and every page looks correct in isolation — the corruption is in the relationship between pages, and you find out hundreds of operations later as a scan silently returning 9,997 of 10,000 rows. So before writing splits I wrote a validator that walks the whole tree and asserts ten structural invariants, and I ran it after every mutation in tests. That doesn't find bugs I couldn't otherwise find; it relocates the failure to the operation that caused it, which is where all the debugging time actually goes. The one that earned its keep was 'every key on a page lies inside the range its parent claims' — it caught a separator off-by-one on the first split test."*


That answer demonstrates invariant-based thinking, a testing strategy, and self-awareness about where debugging time goes. It's worth more than the code.


---


## 6.7 Roads not taken


### Prefix compression and suffix truncation


Interior separators don't need to be *real* keys — they only need to *separate*. If the left page ends at `"Nguyen, An"` and the right starts at `"Nguyen, Bao"`, the separator only needs to be `"Nguyen, B"`. Anything that divides them works.


**Why it matters:** shorter separators mean more cells per interior page, which means higher fanout, which per chapter 05 §5.3 is in the base of the logarithm. For long text keys this can be a large win — it's the difference between fanout 40 and fanout 200 on a 100-byte key column.


Similarly, **prefix compression** stores each key as "shares *k* bytes with the previous key, then these bytes" — very effective when keys share prefixes, which sorted keys do by construction.


**Why not for you:** your table keys are integer rowids, already tiny. It would matter for the index B+trees in week 4 with long text keys. Graefe's "Modern B-Tree Techniques" covers the family, and it's a good "how would you improve this?" answer for a text-indexed workload.


### B-link trees (Lehman & Yao, 1981)


Add a right-sibling pointer to every node, including interiors, plus a "high key." This makes concurrent operations dramatically easier: a reader that arrives at a node just after it split can detect this and follow the sibling pointer to find its key, instead of needing a lock on the parent.


**Why it matters:** it's how Postgres's B-trees achieve high concurrency (Postgres implements a Lehman & Yao variant), and it's the standard answer to "how do you do fine-grained B-tree locking?"


**Why not for you:** week 6 uses **table-level** locking, so concurrent structural modification never happens — a writer holds an exclusive lock on the whole table. That makes B-link trees unnecessary, which is a genuine benefit of the coarse-locking choice worth mentioning: *"table-level locking means I never have two threads modifying one tree, so I don't need latch coupling or B-link sibling pointers. Finer-grained B-tree concurrency is where I'd go next, and Lehman & Yao's B-link tree is the standard approach."*


### Bulk loading


Building a B+tree by inserting *n* keys one at a time costs *n* × O(log n) with splits throughout. If your input is already sorted, you can instead build the tree **bottom-up**: fill leaves completely and sequentially, then build the level above from their separators, and so on. One pass, sequential writes, **100% page occupancy**, no splits.


**Where you'd use it:** `CREATE INDEX` on a populated table (week 4 session 4). `roadmap.md` says "build the tree by scanning existing rows" — if you scan the table in rowid order and sort by index key, you can bulk-load rather than insert one at a time.


**Worth doing?** Probably not in week 4 — one-at-a-time insertion is simpler and correct. But it's a good "what would you optimise?" answer with a real number attached: sorted bulk load gives 100% occupancy against ~69% for random insertion (chapter 05 §5.7), so the index is ~30% smaller *and* built faster.


### Fractal tree / B^ε-tree


Add a buffer of pending operations to each interior node; flush downward in batches when full. This gets LSM-like write throughput while keeping B-tree read behaviour — a genuine attempt at beating the RUM tradeoff (chapter 05 §5.8) rather than just picking a corner of it. Used by TokuDB/PerconaFT.


**Why not:** substantially more complex, and it's the sort of thing to *name* rather than build. But it's a strong answer to "is the B-tree/LSM tradeoff fundamental?" — the honest answer is "mostly, but B^ε-trees and their cousins get closer to the frontier than either endpoint."


---


## 6.8 What you're building


`roadmap.md` week 2, mapped to the theory:


| Session | Build | Theory |
|---|---|---|
| 1 | All four cell formats, encode/decode | §6.0 — fixed-width first; length before rowid; why table interiors have no payload |
| 2 | Page header parse; binary search within a page | §6.1 — the search convention; why binary over the slot array |
| 3 | `search()` root → leaf, against a **hand-built fixture** | §6.1, §6.2 — isolate readers from writers |
| 4 | `TableCursor` with a path stack; crossing leaves | §6.3 — pins, the rightmost child, invalidation policy |
| 5 | `insert()`, easy case only | §6.4 |
| 6 | **Leaf split + separator promotion** + rightmost optimisation | §6.4, chapter 05 §5.7 — write `split_cells` as a pure function first |
| 7 | **Recursive split + root split** | §6.4 — cascade; keep the root page number fixed |
| 8 | `validate_btree()` + stress test | §6.6 — all ten invariants |


### Two changes to the plan this chapter recommends


1. **Write the validator earlier than session 8.** A skeleton in session 5 — even just invariants 1, 2, 3, and 5 — pays for itself during sessions 6 and 7, which is precisely when you need it. Session 8 then completes it and adds the stress test. `roadmap.md` week 2 already says "Build the validator *before* you need it"; this is what that means concretely.
2. **Write `split_cells()` as a pure function** (§6.4). It turns the hardest logic in the project into something exhaustively unit-testable without any I/O.


### The success criteria, and which ones matter most


From `roadmap.md`, with commentary:


- ✅ 100k random-order inserts, validator clean — **the headline test**
- ✅ **`PRAGMA integrity_check` returns `ok` after each of those runs** — the independent oracle from §6.6, and the one that makes the headline test trustworthy
- ✅ `scan() == sorted(set(keys))` as a property test — the `sorted()` oracle
- ✅ Point lookups match a `dict` oracle, for present **and absent** keys — absent keys catch boundary bugs
- ✅ Tree height grows; uniform across every leaf — invariant 5
- ✅ A 10 KB value round-trips through an overflow chain — §6.5
- ✅ Close and reopen mid-run changes nothing — proves nothing important lives only in memory
- ✅ **Sorted-ascending, sorted-descending, and random insertion orders all produce valid trees** — this is the one people skip, and sorted-ascending is exactly the case chapter 05 §5.7's optimisation touches. Run all three and check occupancy, not just validity
- ✅ Corrupt fixtures raise typed errors rather than hanging — §6.5's cycle cap


---


## 6.9 Check yourself


1. Name the four cell formats and the one field that distinguishes each pair.
2. In a table leaf cell, does the payload length or the rowid come first? Give the reason, and the symptom if you swap them.
3. Why does the interior cell put the child pointer before the key?
4. Index interior cells carry a payload and table interior cells don't. Why, and how does that show up in the overflow threshold formulas?
5. Why does a leaf cell need an explicit payload length when the slot array already gives offsets?
6. An interior page with 300 keys has how many children, and where does the extra one live? What breaks if you forget it?
7. Does key 500 go left or right of a separator 500 in SQLite's convention? Which two pieces of code must agree, and what's the symptom when they don't?
8. Why binary-search a page when 300 linear comparisons cost nothing next to a page read?
9. Why build `search` against a hand-built fixture before `insert` works?
10. Why does a cursor need the whole path rather than just the leaf?
11. What's your policy when the tree is modified mid-scan, and why is it worth deciding in week 2?
12. A leaf split writes three pages. Name them, and say why that fact is also the reason week 5 exists.
13. Why should the root's page number never change, how do you arrange that, and why does `sqlite_schema` need no root-page field of its own?
14. Derive why `K = M + ((P - M) % (U - 4))` makes every overflow page exactly full.
15. What are the *two* stated design goals behind SQLite's overflow thresholds?
16. Why can't the overflow threshold be approximated? What exactly does a reader do differently if your writer keeps 22 bytes too many?
17. Is `U` smaller on page 1? Explain which calculation page 1's missing 100 bytes *does* affect.
18. Why do split bugs have no locality in space or time, and what does the validator actually buy you?
19. Which validator invariant catches a separator off-by-one? Which catches a space leak?
20. What does `PRAGMA integrity_check` give you that your own validator doesn't, and vice versa?
21. Why doesn't table-level locking need B-link trees?


---


## 6.10 Sources


- [SQLite Database File Format](https://www.sqlite.org/fileformat2.html) — the four cell formats; "pointers left of key X ⇒ all keys ≤ X"; interior pages hold K keys and K+1 children with the rightmost pointer at header offset 8; overflow page first-4-bytes-next-pointer; the complete X/M/K threshold formulas and both stated rationales ("minimum fanout of 4" and "the record header can usually be accessed without consulting an overflow page"); max/min embedded payload fractions that "must be" 64 and 32; leaf depth 1, interior depth 1 + max child depth, "all children of an interior page have the same depth in a well-formed database."
- [`btreeInt.h`](https://github.com/sqlite/sqlite/blob/master/src/btreeInt.h) — "The key and data for any entry are combined to form the payload"; "The payload for an entry and the preceding pointer are combined to form a Cell"; "Right child (the Ptr(N) value). Omitted on leaves"; overflow chain "all but the last filled completely"; the max-embedded-fraction-of-64 rationale ("at least 4 cells will fit on one page"); lookup as O(log(M)) page reads.
- [`btree.c`](https://github.com/sqlite/sqlite/blob/master/src/btree.c) — `findCellPastPtr()` and "the initial 4-byte child pointer found on interior pages"; the local/overflow split aiming "to minimize the amount of unused space on overflow pages"; `balance()`, `balance_nonroot()`, `balance_deeper()`, `balance_quick()`; `BtCursor` position save/restore.
- [db_tutorial parts 10, 13, 14](https://cstack.github.io/db_tutorial/) — splitting a leaf, updating the parent, splitting internal nodes. `references.md` is right that this is the best step-by-step walkthrough of exactly this problem; read it if §6.4 leaves you stuck.
- Cited by title/author/year: **Lehman & Yao**, "Efficient Locking for Concurrent Operations on B-Trees" (TODS 1981) — B-link trees. **Graefe**, "Modern B-Tree Techniques" (2011) — prefix compression, suffix truncation, bulk loading. **Bender et al.** on cache-oblivious / B^ε-trees, and PerconaFT's Fractal Tree.


---


Before starting week 2, the two things from these chapters that will change your code most:


1. **Write `split_cells()` as a pure function, and the validator skeleton, before session 6.** (§6.4, §6.6)
2. **Add the rightmost-split optimisation.** Five lines, halves your file size on sequential inserts, and gives you a measured before/after number for the README. (Chapter 05 §5.7)


---


**Next:** [07 — From SQL text to a tree](../sql/07-from-sql-text-to-a-tree.md) — tokenization, ASTs,
recursive-descent statements, and Pratt expression parsing.