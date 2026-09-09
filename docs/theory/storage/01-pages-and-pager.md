# 01 — Pages and the Pager


> **Read at the start of week 1.** Covers roadmap sessions 3, 4, 5 (`storage/header.py`, `storage/pager.py`, freelist).
>
> **Time:** ~30 minutes. **Prerequisite:** [chapter 00](00-foundations.md), especially §0.3 (block devices) and §0.4 (why 4096).


---


## 1.0 The problem: how do you find anything in a file?


A file, to your program, is one thing: a very long array of bytes, numbered from 0. That's it. `seek` to a number, `read` some bytes. There is no structure, no records, no "next," no "third one." Structure is something *you* impose.


So you have 10 million users to store, and a byte array. **Where does user number 4,271,908 go, and how do you find it again?**


Try to answer before reading on. There are several plausible answers and they're all wrong in instructive ways.


---


## 1.1 Wrong answer 1: one file per record


```
users/1.json
users/2.json
users/4271908.json
```


Finding a user is now trivial — the filesystem does it. Tempting!


**Why it breaks:**


- **Filesystems are databases with worse tradeoffs.** A directory lookup is itself a tree search, done by code you can't tune, with metadata you don't control. You've outsourced your index to a system optimised for a completely different access pattern.
- **Per-file overhead is brutal.** Every file consumes an inode and rounds up to a whole filesystem block. Ten million 80-byte users at 4096 bytes minimum each = **40 GB to store 800 MB of data**. That's 50× amplification before you've stored a single index.
- **Ten million `open()` calls.** Each is a syscall, a path resolution, a permission check. Sequential scan becomes a catastrophe.
- **No atomicity across files.** Updating two users is two file writes with no way to make them one operation. Chapter 00 §0.7 was entirely about how hard that is to build; the filesystem gives you nothing here.


**Dead end.** But note the useful thing it reveals: the reason to put everything in one file is that you want *control* over layout, allocation, and atomicity. You're not avoiding the filesystem's features — you're avoiding its *policies*.


---


## 1.2 Wrong answer 2: append text lines


```python
with open("users.jsonl", "a") as f:
    f.write(json.dumps({"id": 1, "name": "ada"}) + "\n")
```


This is what everyone actually reaches for, and it's genuinely fine up to a few thousand rows.


**Why it breaks — and each failure is a week of this project:**


1. **Finding one user means reading the whole file.** There's no way to know where line 4,271,908 starts without scanning 4,271,907 newlines. → *the B+tree, week 2.*
2. **Updating a record is impossible in place.** Change `"ada"` to `"adalovelace"` and the line gets longer, so every byte after it must shift. On a 1 GB file that's rewriting 1 GB to edit 11 characters. → *slotted pages, chapter 02.*
3. **A crash mid-write leaves half a JSON object.** The file is now unparseable from that point on. → *the journal, week 5.*
4. **Two writers interleave into garbage.** Two appends can land inside each other's lines. → *locking, week 6.*
5. **You can't ask questions.** "Average age over 30" means writing Python. → *SQL, week 3.*


Notice that (2) is the one people underestimate. Variable-length records that grow are the fundamental problem with laying data out linearly, and it's the thing pages exist to contain.


---


## 1.3 Wrong answer 3: fixed-size records, so you can compute offsets


Getting warmer. If every user record is exactly 128 bytes, then user *N* is at byte offset `N × 128`. Direct addressing, no search, no shifting.


This is a real technique (it's how simple binary formats and some embedded systems work) and it's a genuine improvement. But:


**Why it breaks:**


- **You must pick a maximum size for everything.** How long is a name? 64 bytes? Someone has a longer one. 256? Now every row wastes 200 bytes. There's no correct answer, and picking one puts a hard limit into your file format forever.
- **It doesn't compose with sorting.** To insert a row in the middle of a sorted order you still have to shift everything after it — you've fixed the *addressing* problem but not the *insertion* problem.
- **Records don't align to sectors.** 128 bytes means 32 records per 4096-byte sector. Changing one record read-modify-writes the whole sector anyway (chapter 00 §0.3), so you get charged for 4096 bytes but only reasoned about 128.


That last point is the real clue. **Your unit of addressing should be your unit of I/O.** If the device charges per 4096 bytes, then 4096 bytes is what you should be counting, allocating, and thinking in.


---


## 1.4 The answer: fixed-size numbered pages


Here it is, and it's almost embarrassingly simple:


> **Divide the file into equal 4096-byte chunks. Number them 1, 2, 3, … Page *N* lives at byte offset `(N − 1) × 4096`.**


```
 file:  ┌─────────┬─────────┬─────────┬─────────┬─────────┐
        │ page 1  │ page 2  │ page 3  │ page 4  │ page 5  │  ...
        └─────────┴─────────┴─────────┴─────────┴─────────┘
 byte:  0        4096      8192     12288     16384     20480
```


That's the whole idea, and here's why it's the right one:


### It makes location arithmetic, not search


You want page 47? It's at byte 188,416. No lookup table, no index, no scan. **A page number is a physical address in disguise.** This is the single most important property in the design and everything else leans on it:


- A B+tree interior page stores child *page numbers* — 4 bytes each, and following one is a subtraction and a multiply (chapter 06).
- The freelist stores the *number* of the next free page — no allocation map to search (§1.8).
- An overflow chain is a page number per link.
- The journal records a page *number* along with its original bytes, and recovery knows exactly where to put it back (week 5).


Every "pointer" in your database is a 4-byte integer with O(1) dereference. Compare that to a pointer in a linked list on disk expressed as a byte offset: same idea, but now it's 8 bytes, it can point mid-sector, and nothing constrains it to a valid boundary. Page numbers are *typed* pointers, and the type is "one unit of I/O."


### It matches the device exactly


One page = one filesystem block = one device sector = one OS memory page (chapter 00 §0.4). No read straddles a boundary. No write costs double. Your accounting and the device's accounting are the same accounting.


### It makes variable-length data tractable


Rows are variable-length; pages aren't. So the variability is *contained inside* a fixed box. "Is there room?" becomes a question with a definite answer, asked about one 4096-byte region rather than about a whole file. When the answer is no, you allocate another page and link to it — which is a split (chapter 06) or an overflow chain (§06.5). Chapter 02 is entirely about how bytes are arranged inside one box.


### It makes caching possible


A cache needs a key. "Page 47" is a perfect key: small, an integer, and exactly one cache entry maps to exactly one unit of I/O. Try designing a cache keyed by "the row with id 500" and you'll discover you don't know how big the value is or whether two entries overlap. Chapter 04 depends completely on this.


> **Say this out loud:** "The file is an array of fixed-size numbered pages, so a page number *is* a byte offset — location is arithmetic rather than search. That makes every on-disk pointer a 4-byte integer with O(1) dereference, aligns every I/O with the device's sector, contains variable-length rows inside a fixed box, and gives the page cache a natural key."


---


## 1.5 Why do pages start at 1, not 0?


A small thing that looks like a stylistic quirk and isn't. SQLite numbers pages from 1, and your `constants.py` follows suit with `SCHEMA_ROOT_PAGE = 1`.


**Reason: you need a value that means "no page."**


Every on-disk pointer needs a null. An interior B+tree cell points to a child; an overflow cell points to a continuation page *or nothing*; the freelist head points to the first free page *or there isn't one*; a leaf's sibling pointer points to the next leaf *or it's the last*.


If pages were numbered from 0, you'd need a separate flag byte alongside every pointer, or a sentinel like `0xFFFFFFFF`, to express "nothing here." By starting at 1, **`0` is free to mean null** — and it's the value you get for free when you zero a page. That means:


- A freshly zeroed page has null pointers everywhere by default. No initialisation code that can be forgotten.
- The file format spec can say things like "Page number of the first freelist trunk page" with the convention that zero means empty, and this needs no extra field.


You can see this convention all through the SQLite format ([fileformat2.html](https://www.sqlite.org/fileformat2.html)): the freelist trunk chain terminates with a zero next-page pointer; an overflow chain's last page has zero in its first four bytes; the first-freeblock field in a page header is "zero if there are no freeblocks."


This is a general and reusable trick: **reserve the value you get for free as the value that means nothing.** It's the same reason a C string ends in `\0` and a Python `dict.get()` returns `None`.


> ⚠️ **The bug this will cause you exactly once.** Since page numbers are 1-based and Python is 0-based, the offset is `(page_id - 1) * PAGE_SIZE`, and you will at some point write `page_id * PAGE_SIZE`. Everything will be shifted by exactly one page: reading page 1 gives you page 2's bytes. Symptom: your magic-number check fails on a file you just created. Write the off-by-one into a single function on day one and never compute the offset anywhere else.


---


## 1.6 The file header: why the first page is special


### The problem


You open `app.db`. Before you can do anything you need to know: is this even a SQLite file? How big is a page? How many pages are there? Where's the first free page? What encoding is the text in?


None of that can be stored *in* the structure, because you need it to find the structure. **Chicken and egg.** How do you break it?


### The answer: by fiat


You declare, as an unchangeable rule, that certain metadata lives at a fixed known location. Byte 0. There's nowhere else it *could* go — every other location is discoverable only after you've read something, and this is the something.


The layout, from `constants.py` — and this is SQLite's exact layout, because quilldb writes the real format:


```
byte  0   magic                    16 B   b"SQLite format 3\x00"
byte 16   page size                 2 B   u16   power of 2, 512..32768; 1 means 65536
byte 18   write version             1 B   u8    1 = rollback journal, 2 = WAL
byte 19   read version              1 B   u8
byte 20   reserved space per page   1 B   u8    usually 0
byte 21   max payload fraction      1 B   u8    must be 64
byte 22   min payload fraction      1 B   u8    must be 32
byte 23   leaf payload fraction     1 B   u8    must be 32
byte 24   change counter            4 B   u32
byte 28   page count                4 B   u32   the "in-header database size"
byte 32   freelist trunk            4 B   u32   (0 = empty — see §1.5)
byte 36   freelist page count       4 B   u32
byte 40   schema cookie             4 B   u32
byte 44   schema format             4 B   u32   1..4
byte 48   default cache size        4 B   u32
byte 52   largest root page         4 B   u32   auto-vacuum only, else 0
byte 56   text encoding             4 B   u32   1 = UTF-8, 2/3 = UTF-16
byte 60   user version              4 B   u32   PRAGMA user_version
byte 64   incremental vacuum        4 B   u32
byte 68   application id            4 B   u32   PRAGMA application_id
byte 72   reserved for expansion   20 B         must be zero
byte 92   version valid for         4 B   u32
byte 96   SQLite version number     4 B   u32
          ──────────────────────────────
                                  100 B
```


Twenty-two fields, and the first thing to notice is what **isn't** there: **there is no "where does the catalog live" field.** You'd expect one — it's the most obvious question to ask at open time. Its absence is the subject of the next-but-one subsection, and it's the single most consequential decision in this header.


### Why each field exists


**Magic bytes.** So that opening a JPEG as a database fails immediately with a clear error rather than interpreting pixel data as a page header and doing something insane. This is the cheapest safety check in the entire project: 16 bytes, one comparison, and it converts a whole category of confusing corruption into one clean exception. `"SQLite format 3\000"` is 16 bytes *including* a trailing NUL, so C code can treat it as a string — and the `3` is the format generation, which is why SQLite 3.x has never broken file compatibility in 20+ years.


**Two version bytes, not one — read version and write version separately.** This looks like over-engineering until you see what it buys: a file can be declared *readable but not writable* by an older library. Set write version to 2 (WAL) and an old build that doesn't understand WAL will refuse to write while still being able to read. One byte each, and it turns "your data is inaccessible" into "your data is read-only." A single version field can only express "yes" or "no"; splitting the axis lets the format degrade gracefully instead of failing closed.


**Page size,** at offset 16 — the very first thing after the magic, and deliberately so. You cannot read *any* page, including the rest of page 1, until you know how big a page is. So it must live within the first bytes that can be read without knowing the page size. That ordering constraint is real, and it's why this field isn't sorted in with the other sizes further down.


Note the encoding trick: the field is 2 bytes, but the legal maximum page size is 65536, which needs 17 bits. So **the value `1` means 65536** — 1 is not a legal page size (it's not ≥ 512), so it can be repurposed. This is the same "encode an out-of-range value into an impossible in-range one" move as chapter 02 §2.5's content-start field, and as §1.5's "page 0 means null." Three instances of one technique in one format.


**Reserved space per page,** at offset 20. A number of bytes chopped off the *end* of every page and left for an extension to use — SQLite's encryption extension puts a per-page nonce and MAC there. This is why the overflow formulas in chapter 06 §6.5 are written in terms of **U, the *usable* size**, rather than the page size: `U = page_size − reserved_space`. Almost always 0, and the spec forbids `U` from dropping below 480.


**The three payload fractions** at 21–23, which the spec says **must be** 64, 32, and 32. They're stored, and they're not allowed to vary. A degree of freedom that was designed in, never used, and frozen at its only legal value — extremely common in mature formats, and a nice bit of colour. Chapter 06 §6.5 shows where the 64 and 32 actually appear in the overflow arithmetic.


**Change counter.** Incremented on every modification. Its job is cache validation: a connection that reopens the file compares the counter against what it last saw, and if unchanged, it can trust its entire cached page set rather than rereading. SQLite does exactly this at [atomiccommit.html §3.12](https://www.sqlite.org/atomiccommit.html) — "a counter in the first page of the database that is incremented every time the database file is modified... usually unchanged, so the cache can be reused, a significant performance savings."


**Page count.** So you know where the file ends without asking the filesystem — and more importantly, so that *recovery* knows the original size. This is subtle and it matters in week 5: if a transaction grew the file from 10 pages to 15 and then crashed, rollback must **truncate back to 10**. The filesystem will happily tell you the file is 15 pages long; only the header's recorded count knows it *should* be 10. SQLite records exactly this in its journal header for exactly this reason ([atomiccommit.html §4.4](https://www.sqlite.org/atomiccommit.html): "truncate the database back to its original size").


The spec calls it the "in-header database size" precisely because it can disagree with the file's actual length, and defines when to trust it: only if it's non-zero and the version-valid-for field matches the change counter. **A cached value with an explicit validity condition** — which is what those two fields at 92 and 96 are for.


**Freelist trunk + count** (offsets 32 and 36). The entry point to the reuse chain (§1.8). The count is redundant — you could walk the chain — but walking means reading pages, and this is one integer. Cheap denormalisation for a common question.


**Schema cookie and schema format.** The cookie is bumped whenever the schema changes; a prepared statement records the cookie it was compiled against, and if the cookie has moved, the statement is recompiled. That's how SQLite makes `ALTER TABLE` safe for statements another connection already prepared. The format number (1–4) gates *format features* — notably, serial types 8 and 9 (the zero-byte integers 0 and 1, chapter 03 §3.8) require format 4. **Versioning at the level of individual features, not the whole file.**


**Text encoding.** 1 for UTF-8, 2 and 3 for UTF-16 little- and big-endian. It's stored because it changes how every TEXT value in the file is interpreted, and because it's fixed at creation time and can never change afterwards.


**User version and application id.** Two fields SQLite itself never reads — they exist purely for *you*. `application_id` lets `file(1)` and other tools identify your app's files even though they're all "SQLite databases"; `user_version` is a schema-migration counter for application code. **Deliberately reserving space for the embedder** is a design courtesy worth noticing: SQLite is a library, so its format budgets bytes for its callers.


**Largest root page and incremental vacuum.** Both are auto-vacuum machinery. Non-zero means the file contains *pointer-map pages* interleaved among the data pages, tracking each page's parent so pages can be relocated during vacuuming. quilldb doesn't implement auto-vacuum, which means it must **refuse** a file with these set — walking such a file while ignoring the pointer maps would treat them as b-tree pages. That's exactly the tolerate-vs-refuse judgement `FileHeader.check_supported()` asks you to make.


**20 reserved bytes at 72, required to be zero.** This is not laziness. Requiring them to be zero *now* means that later, when you add a field, you know that any old file has zero there — so zero can be the field's "unset/default" value and old files upgrade for free. If you left them unspecified, they might contain anything, and you could never safely reinterpret them. **Reserve space and mandate its value; it's the cheapest forward-compatibility you will ever buy.**


**Version valid for + SQLite version number** (92, 96). The version of the library that last wrote the file, plus the change-counter value at which that's true. Together they're a diagnostic: if a file is corrupt, you know which library version produced it. And as noted above, the pairing is what makes the in-header page count trustworthy — if `version_valid_for != change_counter`, some older library modified the file without maintaining the size field, so fall back to the filesystem's length.


---


### Why page 1 does double duty — and why there's no "catalog root" field


Go back and look for the field that says where the schema lives. It isn't there.


**Because it doesn't need to be: the schema b-tree is always rooted at page 1.** The 100-byte header occupies bytes 0–99 of page 1, and page 1's *b-tree page header* begins at byte 100. Page 1 is simultaneously the file header and the root page of `sqlite_schema` — the table of tables. From the spec:


> "The 100-byte database file header is found only on page 1, which is always a table b-tree page. All other b-tree pages in the database file omit this 100-byte header."


### The cost, and it's real


**1. A conditional in every page access.** `btree.c` carries `hdrOffset = pgno==1 ? 100 : 0`. Every time any code locates a page header, it asks whether this is page 1.


**2. Page 1 has 100 fewer usable bytes than every other page.** Which leaks into the *invariants*, not just the code. The spec:


> "The number of keys on an interior b-tree page, K, is almost always at least 2... The only exception is when page 1 is an interior b-tree page. Page 1 has 100 fewer bytes of storage space available... and so sometimes (rarely) if page 1 is an interior b-tree page, it can end up holding just a single key."


Read that again, because it's unusual. A structural invariant of the whole format — "interior pages hold at least 2 keys" — has an exception, and the exception exists *because of a header offset*. That's what a leaky abstraction looks like when it's baked into a file format for 20 years.


### The alternative, and why you're not taking it


The obvious cleaner design: make page 1 header-only, waste its remaining 3,996 bytes, and root the schema at page 2. Then every page in the file has identical structure, `hdrOffset` is always 0, and the minimum-2-keys invariant has no exception. It costs one page, once, forever.


**Take that seriously, because it's genuinely better on the axis of simplicity** — and then notice why it's unavailable to you: **`sqlite3` looks for the schema at page 1 unconditionally.** There is no header field to tell it otherwise, precisely because SQLite chose to bake the constant into the format. So the moment your acceptance test is "the sqlite3 CLI can read what quilldb writes," page 1 must be the schema root. This isn't a preference you're deferring to; it's load-bearing.


**What did SQLite buy by baking it in?** Two things, and the second is the interesting one:


1. **One page saved.** In 2003, with a 1024-byte default page size and a target of small embedded databases, an entirely wasted page was a visible fraction of a small file. *(This is reasoning from context — SQLite's docs state the layout, not the motive.)*
2. **One less field that can be wrong.** A "catalog root" header field is a value that can be corrupted, can disagree with reality, must be journalled when it changes, and must be validated on every open. A hardcoded constant cannot be any of those things. **The strongest form of "this field is always correct" is not having the field.**


That second argument is the better one, and it's the one to say out loud, because it generalises: every piece of mutable metadata is a piece of metadata that can become inconsistent. Removing a degree of freedom removes a failure mode. It's the same instinct as chapter 06 §6.4's advice to keep a table's root page number fixed forever — which, notice, only works *because* `sqlite_schema` itself needs no root-page field.


> **Say this out loud:** "The file header has no field for where the schema lives, because the schema b-tree is always rooted at page 1 — the header occupies page 1's first hundred bytes and the b-tree page header starts at byte 100. It costs them a `pgno==1 ? 100 : 0` conditional in every page access, and it puts an exception into the format's own minimum-fanout invariant. What it buys is that the location of the catalog is not a value that can ever be corrupt, stale, or unjournalled. The cheapest way to guarantee a field is correct is not to have the field."




### And what about pages 2 through N?


Everything in §1.6 so far has described **one thing: the first hundred bytes of the file.** That's whole-file metadata, read once at open. It says nothing about what's on page 900.


So what does page 900 look like? It holds real content — schema rows, table rows, B+tree interiors — and something has to describe *that* page's own type, cell count, and free space. That something is a second, much smaller header, and it's a completely different structure:


```
byte  0  page type      1 B   2 = interior index, 5 = interior table,
                              10 = leaf index,   13 = leaf table
byte  1  first freeblock 2 B  u16, 0 = none
byte  3  cell count      2 B  u16
byte  5  content start   2 B  u16, 0 means 65536
byte  7  fragment bytes  1 B  u8, count of 1-3 byte holes
byte  8  right child     4 B  u32 — INTERIOR PAGES ONLY, omitted on leaves
         cell pointers   2 B x cell_count
```


**8 bytes on a leaf, 12 on an interior page** (`LEAF_HEADER_SIZE` and `INTERIOR_HEADER_SIZE` in `constants.py`). The size depends on the type byte you just read, so you cannot know where the cell pointer array starts until you've decoded byte 0. Chapter 06 §6.0 explains why the right-child pointer is the field that only interior pages need.


It exists for a different reason than the file header does. The file header answers *"what kind of file is this, and what global facts hold?"* — asked once. The page header answers *"what's on this specific 4096-byte sheet?"* — asked every single time any page is read, by the slotted-page code from chapter 02.


**Don't confuse the two, and notice the naming clash is easy to trip over:** "the header" means something different depending on which chapter you're in. This doc's convention: **file header** for the 100-byte one at the start of the file; **page header** for the 8-or-12-byte one that begins each page's own region.


The full field-by-field rationale — why 0 means 65536 at offset 5, why fragments get only one byte, why the type byte must be validated — belongs to the slotted page itself, not to the pager, so it's in [chapter 02 §2.5](02-the-slotted-page.md#25-the-page-header-8-bytes-or-12-and-what-each-one-buys). What matters here, at the pager level, is the shape of the rule — and it is *not* "every page except page 1":


> **Every page in the file, page 1 included, carries a b-tree page header. On pages 2…N it starts at byte 0. On page 1 it starts at byte 100, because the file header is sitting in front of it.**


That's the practical form of the `hdrOffset = pgno==1 ? 100 : 0` conditional, and it belongs in exactly one function in your code:


```python
def page_header_offset(page_id: int) -> int:
    return FILE_HEADER_SIZE if page_id == SCHEMA_ROOT_PAGE else 0
```


Write it once, in the pager, and never inline the comparison anywhere else — the same discipline §1.5 recommends for the `(page_id - 1) * PAGE_SIZE` off-by-one, and for the same reason. Both are one-line rules that produce baffling bugs when one call site forgets them.


> ⚠️ **The consequence people miss: page 1's usable space is 100 bytes smaller,** so every "does this cell fit?" and every overflow-threshold calculation on page 1 must subtract 100. If you compute free space from `PAGE_SIZE` uniformly, page 1 will accept a cell that doesn't fit and quietly overwrite the tail of the file header — which is to say, it will corrupt the magic bytes and make the database unopenable. This is the one place where SQLite's design choice bites your code, and it bites at exactly the spot §1.6 warned you about.




---


## 1.7 The pager: the most valuable boundary in the project


### The problem


Everything above the storage layer needs pages. Should the B+tree code call `f.seek()` and `f.read()`?


### Why not


Because of what you're going to do in weeks 5 and 6.


Consider what "give me page 47" needs to mean at each stage of the project:


| Week | "Give me page 47" actually means |
|---|---|
| 1 | `f.seek(46*4096); return f.read(4096)` |
| 1 (later) | Check the buffer pool first; only hit the file on a miss; pin it |
| 5 | If this is a write and page 47 isn't journalled yet, **copy the original into the journal first** |
| 6 | Acquire the right lock; make sure two threads get the *same* object, not two copies |
| any | If this is a `:memory:` database, return it from a dict — there is no file |


Five different behaviours. If the B+tree calls `f.read()` directly, then adding journaling in week 5 means editing every single call site in the B+tree, and every one you miss is a page that silently escapes the journal — which means a crash corrupts your database in a way no test will catch until it does.


### The answer: one narrow interface, and nobody looks behind it


```python
page = pager.read_page(47)
pager.write_page(47, page)
```


The B+tree never learns that a file exists. That ignorance is not a limitation, it's the *product*. It's what lets you change what "get a page" means four more times without touching a line of B+tree code.


This is SQLite's architecture exactly. From [arch.html](https://www.sqlite.org/arch.html):


> "The B-tree module requests information from the disk in fixed-size pages... The page cache is responsible for reading, writing, and caching these pages. **The page cache also provides the rollback and atomic commit abstraction and takes care of locking of the database file.** The B-tree driver requests particular pages from the page cache and notifies the page cache when it wants to modify pages or commit or rollback changes."


Read that middle sentence again. **Rollback, atomic commit, and locking all live in the pager** — the three hardest things in the system — and the B-tree, one of the two hardest *files* in SQLite, doesn't know about any of them. It just asks for numbered pages and says when it's about to modify one.


> ⚠️ **One honest correction to the folklore.** You'll often read "SQLite's `btree.c` never calls `read()`" — including in `guide.md` §1.1 and `roadmap.md`'s week 1 notes. That's a fair characterisation of the layering and it's true in spirit, but `arch.html` never states it as a prohibition. What it states is the positive version: the pager is the component *credited with* reading and writing, and the B-tree's whole documented vocabulary is "request a page" / "notify of modify/commit/rollback." Use the positive version. It's stronger anyway, and it can't be nitpicked.


### The general principle


> **Draw your abstraction boundary where you expect the requirements to change.**


You know — right now, in week 1 — that "fetch a page" will grow to include caching, journaling, and locking. So you put a seam there *before* you need it. This is the opposite of speculative generality (building an abstraction for a requirement you're guessing at); you have a roadmap that names the three changes.


You get this seam in **session 4 of 56**, which makes it the highest-leverage 20 lines in the project. `roadmap.md` §3 states the rule as "layers only call downward" and "every mutation goes through a transaction object, from week 1 — even before the journal exists." Both are the same instinct: put the seam in while it's cheap.


### The free win: in-memory databases


Once the pager is the only thing that touches the file, `connect(":memory:")` is a subclass with a dict instead of a file handle. `roadmap.md` §1.4 budgets 1 hour for it, and that's honest *because of this boundary*. Then your whole test suite runs against RAM and gets several times faster, which compounds over eight weeks of running tests hundreds of times a day.


That's worth noticing as a general pattern: a good abstraction boundary doesn't just accommodate future change, it *creates capabilities you weren't planning*. You didn't build the pager to get in-memory databases; you got them because the pager was properly narrow.


> **Say this out loud:** "The pager is the only component that touches the file, and it exposes exactly 'give me page N' / 'here's page N back.' That single indirection is what lets the buffer pool, the journal, and the lock manager all slot in later without the B-tree noticing — which is the same layering SQLite uses: `arch.html` puts rollback, atomic commit, and file locking all inside the pager, below the B-tree."


---


## 1.8 The freelist: reusing space


### The problem


You delete 10,000 rows. Twelve pages are now completely empty. You then insert 10,000 rows. **Does the file grow?**


If it does, you have a space leak: delete-then-insert cycles grow the file forever. A long-lived database with a steady workload would expand without bound.


### Your first instinct: shrink the file


Truncate the file to remove the empty pages. Clean!


**Why it breaks:** the empty pages aren't at the end. Page 47 is empty; pages 48–900 are full. You cannot remove a page from the middle of the file without renumbering every page after it — and every page number is a pointer stored inside other pages (§1.4). Renumbering means finding and rewriting every pointer in the database. Truncation only works if the free space happens to be at the end, which it almost never is.


*(This is exactly what `VACUUM` does: rewrite the whole database into a new file in compact order, then swap. It's expensive and requires ~2× the space, which is why it's a separate command you run deliberately rather than something that happens automatically. `roadmap.md` §11 correctly defers it.)*


### Your second instinct: a bitmap of free pages


One bit per page, somewhere in the file. Page 47 free? Check bit 47.


This is a real design — it's what many filesystems use — and it has a genuine advantage: you can find *contiguous runs* of free pages, which matters if you want to allocate several adjacent pages for sequential access.


**Why it's wrong here:**


- **It's a second structure to keep consistent.** Now every allocation touches two places — the page and the bitmap — and a crash between them leaves them disagreeing. You'd have to journal the bitmap too.
- **It needs its own space management.** A 1 GB database is 262,144 pages, so the bitmap is 32 KB = 8 pages. Where do those live? What if the database grows past what they cover? You've recreated the allocation problem one level up.
- **Finding a free page means scanning the bitmap.** Fine, but not free.


### The answer: store the free list *in the free pages*


This is the trick, and it's lovely:


> **A free page has nothing useful in it. So use it to store the number of the next free page.**


```
header.freelist_trunk = 47


page 47:  [ next = 89 ][ ...unused... ]
page 89:  [ next = 12 ][ ...unused... ]
page 12:  [ next =  0 ][ ...unused... ]     ← 0 means end of list (§1.5)
```


- **Allocate:** take the head, read its first 4 bytes, that becomes the new head. One page read.
- **Free:** write the current head into the page's first 4 bytes, point the head at this page. One page write.


Both O(1). **And it costs zero extra space** — the bookkeeping lives in space that was by definition unused. A linked list whose nodes are the free objects themselves is a classic allocator technique (it's how a C `malloc` free list works, and how a memory pool allocator works); seeing it appear here is a good sign you're learning transferable structure rather than database trivia.


Hold onto that idea, because it's right. But it's only half of what the format actually specifies, and the missing half is instructive.


### Why one level isn't enough: the freelist is *two* levels


SQLite's freelist is a linked list of **trunk** pages, where each trunk page holds an *array* of page numbers of **leaf** pages ([fileformat2.html](https://www.sqlite.org/fileformat2.html)):


```
header.freelist_trunk = 47          header.freelist_count = 363


page 47 (trunk):  [ next trunk = 89 ][ L = 120 ][ leaf# ][ leaf# ] ... x120
page 89 (trunk):  [ next trunk = 12 ][ L = 120 ][ leaf# ][ leaf# ] ... x120
page 12 (trunk):  [ next trunk =  0 ][ L = 120 ][ leaf# ][ leaf# ] ... x120


                   (120 x 3 leaves + 3 trunks = 363 free pages)
```


A trunk page is an array of 4-byte big-endian integers filling the usable page, so with a minimum usable size of 480 bytes it holds **at least 120 entries**.


**Why bother with two levels?** One sentence in the spec gives it away:


> "Freelist leaf pages contain no information. SQLite avoids reading or writing them to reduce disk I/O."


That's the whole reason. In the simple design above, freeing a page requires **writing** to it (to store the next pointer) and allocating requires **reading** it (to get the next pointer). A trunk page batches ~120 free pages into one page, so freeing 120 pages touches one page instead of 120, and the leaf pages themselves are **never touched at all.**


Per chapter 00 §0.2, a page touch is the expensive unit. **SQLite added a level of indirection specifically to convert N page touches into N/120 page touches.** That's the entire justification, and it's a good example of a design that looks like gratuitous complexity until you know which resource is scarce.


> ⚠️ **A correction worth reading carefully, because the obvious claim about this is wrong.** You'd expect the simple one-level list to be *invalid* in this format. It isn't. Write a bare `next` pointer into each freed page, **zero the rest of the page**, point `header.freelist_trunk` at the head, and `sqlite3` reads it as a chain of **trunk pages that each happen to hold zero leaves.** The page counts line up and `PRAGMA integrity_check` says `ok`. That's a verified result, not a guess — three free pages in a one-level chain, accepted.
>
> **So trunk pages are a performance requirement, not a correctness one.** The degenerate one-level form is legal. It just touches every page on free and every page on allocate, which is the entire cost the two-level design exists to avoid.
>
> **But there is a real correctness trap here, and it's a sharper lesson than the one you were expecting.** The one-level list is valid *only because you zeroed the rest of the page.* Bytes 4–7 of a trunk page are **L, the leaf count.** Free a page by writing only its first four bytes and leaving the old row data in place, and `sqlite3` reads your stale bytes as a leaf count of roughly four billion:
>
> ```
> *** in database main ***
> Main freelist: freelist leaf count too big on page 4
> ```
>
> That is a real error from a real run of the experiment. **The bytes you *don't* write are as load-bearing as the ones you do** — and notice that no amount of reading the spec would have told you which of your two plausible beliefs was correct. Running the reference implementation against your own output did. That's the argument for §1.10's `integrity_check` test in one paragraph.


Build the two-level version anyway. Not for validity — for the N/120.


**Two details from the spec worth knowing as colour:**


- Versions before 3.6.0 (2008) reported corruption if any of the **last 6 entries** of the trunk array were non-zero. The bug is long fixed, but modern SQLite *still avoids using those six slots* so that files it writes remain readable by 17-year-old libraries. That is what a real compatibility commitment costs, and it's a great example for "tell me about a time you had to live with a constraint." Leave those six slots alone in your own writer too — it costs 24 bytes per 480 and buys the same property.
- There's a subtle interaction with journaling, from `btree.c`: because freelist leaf pages are never written or journalled, if a page is freed and then *reused within the same transaction*, "the original data may be lost." SQLite keeps a bitvec (`BtShared.pHasContent`) marking pages where this optimisation must be skipped. **This is the shape of bug you should expect in week 5** — an optimisation in the storage layer that silently breaks an invariant in the durability layer. Worth remembering when your crash matrix goes red for no apparent reason.


### The honest limitation you're accepting


`roadmap.md` §1.3 cuts B-tree page merging: when deletes leave a page half-empty, you don't merge it with a sibling. Pages only return to the freelist when they become **completely** empty.


Be precise about what that means, because it's a limitation you'll put in the README and precision is the whole point:


- It is **not a leak.** Fully-empty pages are reused; the file doesn't grow without bound.
- It **is** internal fragmentation: after heavy deletion, pages sit half-full, so the tree occupies more pages than necessary and scans read more pages than necessary.
- The tree stays **correct** — every invariant in chapter 06 still holds. Only density degrades.


"No page merging after delete, so pages can sit half-empty; space is reused via the freelist but the tree isn't compacted" is specific, honest, and reads as competence. "Some limitations exist" reads as evasion.


> **Say this out loud:** "Freed pages go on a list whose nodes *are* the free pages, so the bookkeeping costs zero extra space. But it's two levels, not one: trunk pages each hold an array of ~120 free-page numbers, and the leaf pages they name are never read or written at all. That converts N page touches into N/120 when freeing in bulk, which is the only reason the extra level exists."


---


## 1.9 Roads not taken


Four alternatives to the whole "pager reads pages from a file" model. Each is a real choice made by a real system, and each is a likely follow-up question.


### `mmap` — let the OS do it


Instead of `read()` into your own buffer, ask the kernel to map the file directly into your address space. Now the file *is* an array in memory; touching a byte that isn't resident triggers a page fault and the kernel fetches it transparently.


**Wins:** no copying (`read()` copies kernel page cache → your buffer; mmap gives you the kernel's pages directly), no syscall per access, and the OS's page cache replaces your buffer pool entirely.


**Losses, and they're why you're not doing it:**
- **You lose control over eviction.** The kernel decides what stays resident, and it doesn't know a scan is one-shot (chapter 04 §4.5) or that a cursor is holding a pin.
- **You lose control over write ordering**, which is fatal for week 5. With mmap, a dirty page can be written back by the kernel at any moment. Your journal-before-data ordering becomes unenforceable without `msync` gymnastics.
- **I/O errors arrive as `SIGBUS`**, not as an exception you can catch and turn into a typed error.
- **The file can't be resized while mapped** without remapping.


BoltDB and LMDB both use mmap and both are read-optimised copy-on-write designs where the write-ordering problem is solved differently (a single atomic root-pointer swap rather than an ordered multi-page write). **The design choice is a package deal:** mmap works beautifully with copy-on-write and badly with in-place update plus a journal. SQLite supports optional mmap for reads only, via `PRAGMA mmap_size`. `roadmap.md` §1.3 correctly calls it premature.


### Direct I/O — skip the OS page cache


`O_DIRECT` bypasses the kernel's page cache entirely: your reads and writes go straight to the device. Big databases (Oracle, MySQL/InnoDB by default on Linux) do this.


**Why:** double caching. Without it, a page is cached twice — once by the OS, once by your buffer pool — wasting RAM, and the OS's eviction policy fights yours. If you're going to manage 64 GB of buffer pool intelligently, the kernel's guesses are actively unhelpful.


**Why not for you:** it demands aligned buffers and aligned sizes, it's a portability nightmare, and it *forces* you to have a good buffer pool because there's no safety net. It only pays off when your pool is large and your policy is smarter than the kernel's. At your scale the kernel's cache is a free performance win.


Worth knowing as the answer to "how would you make this faster?": *"O_DIRECT to stop double-caching, once my buffer pool's replacement policy is good enough to beat the kernel's — which it currently isn't, because it's plain LRU."*


### One file per table


Postgres does this — every table and index is its own file (several, actually, once they exceed 1 GB). SQLite deliberately does not: "Separate B-trees are used for each table and each index in the database. **All B-trees are stored in the same disk file**" ([arch.html](https://www.sqlite.org/arch.html)).


**Why SQLite chose one file:** it's the entire product. "An SQLite database is a single file you can copy, email, or embed" is the value proposition. Atomicity across tables also gets much harder with multiple files — you need a super-journal to coordinate, which SQLite does have for `ATTACH`ed databases and which is exactly the complexity it avoids in the common case.


**Why Postgres chose many:** per-table files let the OS manage them independently, allow tables on different filesystems (tablespaces), and let `DROP TABLE` be a file deletion rather than a freelist walk.


You want one file for the same reason SQLite does, and it's worth saying: single-file is a *feature*, not a simplification.


### Extents — allocate runs, not single pages


Instead of one page at a time, allocate contiguous runs of 8, 64, or 1024 pages. Filesystems (ext4, XFS) and big databases (InnoDB, SQL Server) all do this.


**Why:** sequential access (chapter 00 §0.5). If a table's pages are physically contiguous, a full scan is one large sequential read rather than thousands of scattered ones. On a spinning disk this is the difference between 8 seconds and 30 milliseconds.


**Why not for you:** it complicates allocation substantially (now you need to find contiguous runs, handle partial extents, and deal with fragmentation of the extent map itself), and the payoff is largest on the device you're least likely to be measured on. It's a great "what would you do next" answer for scan-heavy workloads.


---


## 1.10 What you're building, and why each piece is there


Mapping the theory onto `roadmap.md` week 1 sessions 3–5:


| Session | Component | The theory it implements |
|---|---|---|
| 3 | `storage/header.py` | §1.6 — breaking the chicken-and-egg by fiat; self-describing formats; reserved-must-be-zero |
| 4 | `storage/pager.py` | §1.4 page numbering, §1.5 the 1-based off-by-one, §1.7 the abstraction boundary |
| 5 | freelist | §1.8 — the list stored in the free pages themselves |


**The invariants your tests should assert** (they're the ones the theory above depends on):


1. `read_page(N)` after `write_page(N, b)` returns exactly `b`. *(The pager is a faithful array.)*
2. `read_page(N)` for N > page_count raises `PageOutOfRange` — never returns short data, never a bare `IndexError`. *(§0.6: untrusted input gets typed errors. `roadmap.md` §7: "never handle malformed external input with `assert`.")*
3. Bad magic raises immediately, before anything else is parsed. *(§1.6 — the cheapest safety check you own.)*
4. `allocate → free → allocate` returns **the same page number**, and the file does not grow. *(§1.8 — the freelist actually reuses.)*
5. Reserved header bytes 72–91 are zero on a freshly created file. *(§1.6 — forward compatibility is only real if it's enforced from day one.)*
6. Page count in the header matches reality after every operation. *(§1.6 — week 5's recovery depends on this being trustworthy.)*
7. `page_header_offset(1) == 100` and `page_header_offset(n) == 0` for all n > 1, and free-space arithmetic on page 1 accounts for the missing 100 bytes. *(§1.6 — the one place SQLite's design choice reaches into your code.)*
8. **`sqlite3` can open a freshly created file and `PRAGMA integrity_check` returns `ok`.** *(The acceptance test for the whole format. It's a two-line subprocess call and it is worth more than every other test in this list combined.)*


Number 4 is the one people skip and it's the one that catches real bugs. Number 6 is the one that will save you in week 5. Number 8 is the one that makes the other seven mostly redundant — write it in session 3, the moment `to_bytes()` produces its first hundred bytes:


```python
def test_sqlite3_can_read_our_file(tmp_path):
    db = tmp_path / "probe.db"
    Pager.create(db).close()
    out = subprocess.run(["sqlite3", str(db), "PRAGMA integrity_check;"],
                         capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "ok"
```


**Get that green before you write another line of storage code.** An empty database — one page, a valid header, an empty leaf table b-tree at offset 100 — is enough to satisfy it, and every structural mistake you make from here on will be caught by an adversarial C program written by people who have been finding these bugs since 2000.


---


## 1.11 Check yourself


1. Why is one-file-per-record a bad idea? Give the storage-amplification number.
2. What property makes a page *number* better than a byte *offset* as an on-disk pointer?
3. Why do pages start at 1?
4. Why does the page-size field sit at offset 16, ahead of everything else except the magic? What ordering constraint forces it there?
5. A 2-byte field must express page sizes up to 65536. How does the format manage it, and where have you seen the same trick twice more?
6. Why must the header record the page count, when the filesystem already knows the file's size? What two fields decide whether that count is trustworthy?
7. Why are 20 header bytes reserved *and required to be zero*? What breaks if you leave them unspecified?
8. **There is no "where does the catalog live" field. Why not, and what does its absence buy?** Give the argument that isn't "it saves a page."
9. Name the concrete cost SQLite pays for putting the file header inside page 1 — one in the code, one in the format's own invariants.
10. What's the difference between "the file header" and "a page header"? Which pages have which, and at what offset?
11. Why is a leaf's page header 8 bytes and an interior page's 12? Why can't you know which you have before reading byte 0?
12. Name the four things "give me page 47" will come to mean by week 6.
13. Why does the freelist have trunk pages instead of a simple linked list? What resource is it saving, and what happens to `integrity_check` if you use one level instead?
14. Why can't you just truncate the file to reclaim empty pages?
15. `mmap` removes a copy and a syscall per page access. Give the specific reason it's incompatible with your week-5 design.
16. Why is single-file storage a feature rather than a simplification?


---


## 1.12 Sources


- [SQLite Architecture](https://www.sqlite.org/arch.html) — the pager owns "reading, writing, and caching these pages... the rollback and atomic commit abstraction... and locking of the database file"; all B-trees live in one file. The caveat in §1.7 about the "never calls read()" claim is based on this page not stating it.
- [SQLite Database File Format](https://www.sqlite.org/fileformat2.html) — the 100-byte header field table, freelist trunk/leaf structure, the ≥120-entries derivation, "freelist leaf pages contain no information... to reduce disk I/O," the pre-3.6.0 last-six-entries bug, page-1's 100-byte offset and its effect on minimum interior fanout.
- [`btree.c`](https://github.com/sqlite/sqlite/blob/master/src/btree.c) — `hdrOffset = pgno==1 ? 100 : 0`; the `BtShared.pHasContent` bitvec and the free-page-reused-within-a-transaction hazard.
- [Atomic Commit In SQLite](https://www.sqlite.org/atomiccommit.html) — §3.12 the change counter as cache validation; §4.4 truncating the database back to its journal-recorded original size.
- [Default Page Size Change](https://www.sqlite.org/pgszchng2016.html) — context for why 1024 was chosen in 2003, relevant to §1.6's page-1 argument.


---


**Next:** [02 — The slotted page](02-slotted-page.md) — how variable-length rows fit in a fixed box, and what "free space" really means once you look closely.



