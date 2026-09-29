# ADR-001: BufferPool caches raw page bytes, not parsed PageBody objects


## Status


Accepted.


## Context


`docs/theory/storage/04-buffer-pool.md` §4.2 argues the pool should cache
*parsed* page objects rather than raw bytes: in Python, re-running
`struct.unpack` on every access is real, avoidable cost that a C
implementation wouldn't pay.


Building it that way means composing `page_header_offset()` with
`parse_page`/`serialize_page`, because page 1's b-tree header starts at byte
100, not 0 — and neither function currently accounts for that offset, or for
operating on a usable region shorter than `PAGE_SIZE`. Solving that
composition was out of scope for week 1 session 7.


## Decision


`BufferPool.get_page` returns the raw `bytearray` from `Pager.read_page`,
unparsed. Pin counts, dirty tracking, and LRU eviction all operate on that
raw buffer. Decoding a page into a `PageBody` (or serializing one back) is
the caller's job, done on the bytes the pool hands back.


## Why this isn't just a shortcut


Checked against the primary source, not assumed: SQLite's own page cache
(`pager.c`, `pcache.c`/`pcache1.c`) stores exactly this — raw, page-numbered
byte buffers, with no b-tree awareness at all. All header/cell decoding
happens one layer up, in `btree.c`, which is a *client* of the page cache,
not part of it:


> "The page cache is responsible for reading, writing, and caching [pages]...
> The B-tree module then interprets that raw data."
> — https://www.sqlite.org/arch.html


SQLite's B-tree layer does keep a thin decoded overlay per page (`MemPage`:
cell count, header size, content-start offset — a handful of integers, not
a materialized list of every cell's bytes), recomputed via `btreeInitPage`
each time the page is fetched from the cache. That is a second,
B-tree-owned cache layered *on top of* pcache, with a lifetime tied to the
pcache entry underneath it — not a property of the page cache itself.


## Consequences


- Matches the real architecture's layering, not just its terminology.
- Defers the page-1-offset composition problem entirely: `BufferPool` never
  parses a page, so it never has to get that offset right.
- The theory doc's Python-specific optimization (cache the decode, not just
  the bytes) is still available — as a future `MemPage`-style layer built
  *on top of* `BufferPool` once the B+tree exists to need it, rather than
  something baked into the page cache itself.
- Whoever builds the B+tree (week 2) calls `parse_page`/`serialize_page`
  directly on the bytes `BufferPool` returns, and is the one who has to
  solve the page-1 offset problem — currently still unsolved anywhere in
  the codebase.


## Update (week 8): what happened next


The page-1 offset problem was solved the way this ADR predicted it would have to be — one layer above the
pool. `parse_page` takes a `header_offset` argument (100 for page 1, 0 otherwise), and callers that touch
page 1 pass `pager.page_header_offset(page_id)`; `BufferPool` still never parses anything. See
`catalog/catalog.py`, which reads the schema root this way.


The "decoded overlay on top of the pool" idea in the third Consequences bullet has **not** been built; the
code parses on every access. It remains available if profiling ever points at repeated `parse_page` calls.
*(TODO before publishing: state here whether it was ever profiled — this update records what the code does,
not why the overlay was skipped.)*


**Alternatives considered** (this ADR predates the template's fourth heading): caching parsed `PageBody`
objects in the pool, as chapter 04 §4.2 argues. It is faster per access in Python, but it welds the pool to
the page format and puts the page-1 offset inside the cache instead of in its callers. Weeks 5–6 also
added a second reason to be wary: with journalling and rollback, every cached parse is another copy of a
page that would have to be invalidated. That is a design argument, not a measured one.




