# ADR-008: Repack a page on delete instead of maintaining freeblocks


## Status


Accepted (decided in week 1 with the slotted page; recorded in week 8).


## Context


A slotted page keeps cell pointers growing forward and cell content growing backward from the end, with
free space between. Deleting a cell in the middle leaves a hole in the content area. SQLite tracks holes
in a chain of *freeblocks* threaded through the page, counts leftover 1 to 3 byte gaps in a *fragment*
counter, and defragments when fragments pass a cap of 60 bytes. Reusing holes correctly means walking and
coalescing that chain on both delete and insert.


## Decision


On every delete, quilldb **repacks the page**: the surviving cells slide together, the pointer array is
compacted, and the freeblock and fragment fields are written as zero. A page with no holes is a
perfectly well-formed SQLite page (the fields exist and correctly say there are none), so this changes
only the algorithm, never the format.


## Consequences


- **`free_space()` is always one contiguous number.** Fragments cannot accumulate, and the
  defragmentation pass, the 60-byte cap and freeblock coalescing do not exist. The delete path is much
  smaller than SQLite's.
- **Every delete costs O(page size)** instead of O(1): the survivors are moved even when only one small
  cell was removed. At a 4096-byte page this is bounded, and it was judged the right tradeoff at
  quilldb's write volume and the wrong one at SQLite's. **Not measured**: no benchmark compares delete
  cost against a freeblock implementation.
- **`integrity_check` is unaffected.** It has no opinion about how holes are represented; a page written
  this way passes.
- The related choice, a one-level freelist, follows the same rule: a linked list of freed pages is legal
  SQLite (verified by building one and running `integrity_check`), and SQLite's trunk/leaf design exists
  only so freeing and allocating touch about 1/120th as many pages. A freed page is zeroed in full,
  because stale bytes at offsets 4 to 7 make `sqlite3` report "freelist leaf count too big".


## Alternatives considered


- **SQLite's freeblock chain and fragment counter.** The exact reference behaviour and O(1) deletes, at
  the cost of a chain to maintain on every insert and delete and a defragmentation policy with its own
  edge cases. Nothing in the acceptance test requires it.
- **Repack lazily, only when an insert needs contiguous space.** Cheaper deletes, but it reintroduces
  holes and a fragmented `free_space()`, which is the bookkeeping repacking exists to avoid.
