# ADR-005: Free pages when they empty, but do not merge underfull siblings


## Status


Accepted (decided in week 4; recorded in week 8).


## Context


Deleting rows leaves pages sparse. SQLite responds in `balance()` by redistributing cells among up to
three sibling pages once a page falls below roughly a third full (the gate is `nFree*3 <= usableSize*2`),
which is deliberately below the textbook one half so a tree at the boundary does not thrash between
splitting and merging. Doing the same needs sibling lookup, cell redistribution across pages and parent
divider updates: a second, larger piece of b-tree machinery than the insert path.

Not every empty page is optional, though. Real `sqlite3` rejects a page with zero cells as
`database disk image is malformed`, a hard parse error rather than an `integrity_check` finding
(`NOTES.md` B4-4 and B4-5). So the question is only about pages that are underfull, not empty.


## Decision


- A page that loses its **last** cell is repaired immediately. In the index tree it is merged into or
  rotated with a sibling and freed (`IndexBTree._rebalance`); in the table tree it is unlinked from its
  parent and freed to the freelist. An emptied root reverts to an empty leaf **in place**, because
  `sqlite_schema` records the root's page number.
- A page that is merely **sparse** is left alone. Splits are two-way only, and an interior page left with
  a single child is tolerated rather than collapsed, which costs a little wasted height.


## Consequences


- **Every file remains a valid SQLite file.** Occupancy is not a format constraint and `integrity_check`
  checks structure, not density. This is a missing algorithm, not a format divergence.
- **Delete-heavy workloads leave the file fuller than necessary, and scans pay for it.** Measured on
  20,000 rows of about 70 bytes each, deleting 90% of them (every row whose `k <> 0`, scattered across the
  table), inside one transaction:

  | | Pages before | Pages after | Freelist after |
  |---|---:|---:|---:|
  | quilldb | 362 | 362 | **0** |
  | SQLite 3.50.4 | 361 | 361 | **316** |

  quilldb freed nothing, because no page emptied; SQLite merged the sparse pages and freed 316. A full
  scan of the 2,000 surviving rows in quilldb reads **360 pages** (`EXPLAIN ANALYZE`), essentially the
  cost before the delete. (SQLite's scan was not measured, but 45 pages remain in use.) Neither file
  shrinks, since neither ran `VACUUM`; the difference is how much space can be reused and how many pages
  a scan must read.
- **Space is reclaimed only by reuse.** Later inserts fill the sparse pages, so the effect is largest for
  a table that shrinks and stays small. `VACUUM` and `auto_vacuum` are not implemented (ADR-002).
- The two rebalancing paths differ on purpose: the index tree needs merge and rotate to avoid a zero-cell
  page at all, so the machinery for merging two pages exists there; what is missing is the *trigger*
  (underfull, rather than empty).


## Alternatives considered


- **Merge below one third, as SQLite does.** The right behaviour, and the honest description of the gap:
  the delete path would need the same sibling redistribution the insert path does not have.
- **Merge below one half (the textbook rule).** Simpler to state, but produces split/merge thrashing for
  workloads that hover around the boundary, which is why SQLite does not do it.
- **Tombstones instead of removal (PostgreSQL's MVCC).** Deletion becomes a tiny fixed-size write and
  snapshot isolation comes almost free, at the price of bloat and a vacuum process. It chooses what to
  make cheap differently and does not fit a single-writer rollback-journal engine (ADR-004).
- **Compaction on demand (`VACUUM`).** Needs a child-to-parent pointer to relocate pages, which the base
  format lacks; SQLite gets it from opt-in pointer-map pages that make every structural change write a
  second page (chapter 10 §10.7).
