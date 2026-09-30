# File format: what is on disk

A quilldb database is a **real SQLite 3 file**, byte for byte. There is no quilldb-specific container:
`sqlite3 file.db` opens it, `PRAGMA integrity_check` returns `ok`, and a file SQLite wrote can be read
back (within the limits in [What is refused](#what-is-refused-and-why)). The reference is
[sqlite.org/fileformat2.html](https://www.sqlite.org/fileformat2.html); every offset and type code in
[`constants.py`](../src/quilldb/constants.py) is copied from it, not invented.

Multibyte integers are **big-endian**. The file is a sequence of **4096-byte pages**, numbered from 1
(so that 0 can mean "no page" in every pointer). Page *n* starts at byte `(n - 1) * 4096`.

To look at a real file yourself:

```console
$ quilldb inspect app.db        # the header, decoded
$ quilldb pages app.db          # one line per page: number, type, cell count
$ quilldb btree app.db --root 2 # a table's tree, with rowid ranges
$ quilldb validate app.db       # quilldb's own validators, then real sqlite3's
```

## The file at a glance

```
page 1   [ 100-byte file header ][ sqlite_schema root page (b-tree header at byte 100) ]
page 2+  table leaves and interior pages, index pages, overflow pages, freelist pages
```

Page 1 is two things at once: the file header **and** the root of the `sqlite_schema` table. That is why
page 1 has 100 fewer usable bytes than any other page, and why its b-tree header starts at byte 100. The
cell offsets on it stay measured from byte 0 of the page, exactly as SQLite writes them
(`PageBody.header_offset`).

An empty database is exactly one page.

## The 100-byte header

| Offset | Size | Field | quilldb writes | Notes |
|---:|---:|---|---|---|
| 0 | 16 | magic | `SQLite format 3\0` | |
| 16 | 2 | page size | 4096 | |
| 18 | 1 | write version | 1 | 1 = rollback journal, 2 = WAL |
| 19 | 1 | read version | 1 | |
| 20 | 1 | reserved bytes per page | 0 | |
| 21 / 22 / 23 | 1 each | payload fractions | 64 / 32 / 32 | the spec fixes these |
| 24 | 4 | file change counter | bumped per commit | |
| 28 | 4 | database size in pages | page count | |
| 32 | 4 | first freelist trunk page | 0 if none | |
| 36 | 4 | total freelist pages | | trunks plus leaves |
| 40 | 4 | schema cookie | bumped on each schema change | also invalidates other connections' catalogs and the plan cache |
| 44 | 4 | schema format | 4 | 4 enables serial types 8 and 9 |
| 56 | 4 | text encoding | 1 (UTF-8) | |
| 92 | 4 | version-valid-for | the change counter | |
| 96 | 4 | SQLite version number | 3045000 | what we claim last wrote the file |

The remaining fields (default cache size, largest root page, user version, incremental vacuum,
application id, 20 reserved zero bytes) are read and written but carry no behavior.
[`storage/header.py`](../src/quilldb/storage/header.py) has the full list.

## A b-tree page

```
+-----------------+--------------------+----------------+---------------------+
| page header     | cell pointer array |  free space    |  cell content       |
| 8 or 12 bytes   | 2 bytes per cell   |                |  grows down from    |
|                 | in KEY order       |                |  the end of the page|
+-----------------+--------------------+----------------+---------------------+
```

| Offset | Size | Field |
|---:|---:|---|
| 0 | 1 | page type: **2** interior index, **5** interior table, **10** leaf index, **13** leaf table |
| 1 | 2 | first freeblock (0: none) |
| 3 | 2 | number of cells |
| 5 | 2 | start of cell content area |
| 7 | 1 | fragmented free bytes |
| 8 | 4 | right-most child pointer (**interior pages only**, so their header is 12 bytes) |

Two facts to hold onto:

- **Pointers are in key order; the cell bodies are in whatever order they were written.** The pointer
  array is what gives the page its order.
- **quilldb repacks a page on every change** instead of maintaining a freeblock chain, so it always
  writes a first-freeblock of 0 and no fragments. A page with no holes is well-formed SQLite. See
  [ADR-004](decisions/ADR-004-undo-journal-not-wal.md) for the journal and
  [ADR-008](decisions/ADR-008-repack-pages-on-delete.md) for the repack.

Overflow and freelist pages have **no type byte**. They are recognized only by how you arrived at
them, so a corrupt pointer into one produces garbage, not a clean error.

## Cells

Four cell shapes, one per page type. `varint` is SQLite's 1 to 9 byte encoding: 7 bits per byte, high bit
means "more follows", and a ninth byte, if present, contributes all 8 bits.

| Page type | Cell layout |
|---|---|
| Leaf table (13) | `varint` payload size, `varint` rowid, payload, then a 4-byte first-overflow-page number **only if the payload spilled** |
| Interior table (5) | 4-byte left child page, `varint` rowid (a separator; no payload) |
| Leaf index (10) | `varint` payload size, payload, optional 4-byte overflow page |
| Interior index (2) | 4-byte left child page, `varint` payload size, payload, optional 4-byte overflow page |

**Table trees key on the rowid.** Interior cells are pure routing: a separator is any value at least every
key in the left subtree and less than every key in the right one.

**Index trees key on the indexed columns, with the rowid appended as the last column of every entry**, and
the record is the whole key. An interior *index* cell is a live entry, not just a signpost, which is why
deleting from an index tree needs merge and rotate where the table tree can simply unlink a page
([chapter 11](theory/btree/11-index-b-trees.md)).

## Records

A row's payload is a **record**: a type manifest, then the values.

```
[header size varint][serial type varint]...[value][value]...
```

The header size counts itself. The serial types say each value's type *and* length, so column 5 can be
found without decoding columns 1 to 4.

| Serial type | Meaning | Bytes |
|---:|---|---:|
| 0 | NULL | 0 |
| 1, 2, 3, 4 | integer | 1, 2, 3, 4 |
| 5 | integer | 6 |
| 6 | integer | 8 |
| 7 | IEEE 754 float | 8 |
| 8 / 9 | the integer 0 / 1 | 0 (the value is in the type) |
| 10, 11 | reserved: **rejected** as a malformed record | |
| N ≥ 12, even | blob | (N − 12) / 2 |
| N ≥ 13, odd | text (UTF-8) | (N − 13) / 2 |

Lengths are in **bytes**, not characters: `"café"` is 5 bytes, serial type 23. Integers always take the
narrowest width they fit in.

## Overflow chains

When a payload is too big for its page, the first part stays in the cell and the rest goes to a linked
list of overflow pages:

```
page A: [next -> B (4 bytes)][4092 bytes of payload]
page B: [next -> C (4 bytes)][4092 bytes of payload]
page C: [next ->  0 (4 bytes)][the rest]          0 = end of chain
```

How much stays local, with `U` = 4096 usable bytes, `P` = payload size:

| | Formula | At 4096 |
|---|---|---:|
| `X`, the most a leaf-table cell keeps local | `U - 35` | 4061 |
| `X`, for index pages | `((U - 12) * 64 / 255) - 23` | 1002 |
| `M`, the minimum always kept local | `((U - 12) * 32 / 255) - 23` | 489 |

If `P <= X` it all stays local. Otherwise the local size is `K = M + ((P - M) mod (U - 4))` when
`K <= X`, and `M` when not. The remainder trick is chosen so the spilled part fills whole overflow pages
with nothing wasted on the last one (except in the `M` fallback). The 1002-byte index limit is what
keeps an index page's fanout at four or more. Implementation:
[`btree/cells.py`](../src/quilldb/btree/cells.py) (`local_payload_size`) and
[`storage/overflow.py`](../src/quilldb/storage/overflow.py).

## The freelist

Pages that were freed are kept in a two-level structure, and the file does not shrink.

```
header.freelist_trunk -> trunk page: [next trunk (4)][leaf count L (4)][L leaf page numbers (4 each)]
```

Allocation takes the trunk's last leaf, or the trunk itself once it has none; with no free pages the file
grows by one. Freeing appends to a trunk. `header.freelist_count` is the total of trunks and leaves.
`VACUUM` is not implemented, so the file only grows or holds steady.

## `sqlite_schema` and quilldb's own table

`sqlite_schema` is an ordinary table b-tree rooted at page 1, with SQLite's five columns:
`(type, name, tbl_name, rootpage, sql)`. The `sql` column holds the **original `CREATE` text**, and it is
re-parsed once per open. That is what makes a file self-describing and lets a newer quilldb reinterpret an
older file without a migration.

- Only `table` and `index` rows are understood. Any other object type raises `UnsupportedFeatureError`.
- The schema table is never split: it is worked directly on page 1, which limits a database to roughly
  50 tables (about 53 at the length of `CREATE TABLE t (id INTEGER, name TEXT, age INTEGER)`), after which
  `CREATE TABLE` raises `PageFullError`. Lifting that means threading the header offset through every
  descent in the b-tree code.
- `ANALYZE` stores planner statistics in an ordinary table called **`quill_stat1`** (not SQLite's
  `sqlite_stat1`), so it appears in `.tables` like any other.

## The rollback journal (a separate file)

The journal is the one part of the format that is **not** SQLite's: `app.db-journal` uses SQLite's layout
but its own magic, `quilldbj`, in place of SQLite's, so the two engines cannot mistake each other's
journals. Real SQLite will not roll back a quilldb journal, and quilldb will not roll back SQLite's.

```
header, padded to 512 bytes:  magic(8) | record count(4) | nonce(4) | page count before(4) | sector size(4) | page size(4)
records, no padding:          [page id (4)][original page image (4096)][checksum (4)]
```

The magic and the record count are written as **zero** first and stamped only after the body is synced, so
a torn journal is rejected two independent ways. Recovery and the ordering that makes commit atomic are in
[durability.md](durability.md). Only a hot (present) journal matters: after a clean commit it is deleted,
and that deletion is the commit point.

## What is refused, and why

Opening a file that SQLite wrote is fine when it stays within this list. Otherwise quilldb raises
`InvalidHeaderError` rather than misreading bytes. Each refusal is one place where a different value would
change *where bytes live or what they mean*:

| If the header says | quilldb | Why |
|---|---|---|
| bad magic, page size not a power of two, in-header size 0 | refused | not a SQLite file, or a damaged one |
| page size other than 4096 | refused | 4096 is hard-coded throughout |
| payload fractions other than 64 / 32 / 32 | refused | the spec says "must be" |
| read or write version 2 (WAL) | refused | only rollback journals are implemented ([ADR-004](decisions/ADR-004-undo-journal-not-wal.md)) |
| reserved bytes per page other than 0 | refused | every page would be shorter than assumed |
| text encoding other than UTF-8 | refused | the record codec assumes UTF-8 |
| schema format other than 4 | refused | formats 1 to 3 lack serial types 8 and 9 |
| auto-vacuum or incremental vacuum set | refused | pointer-map pages would be interleaved among the pages a walk visits |

Format fidelity and feature completeness are separate claims. `VACUUM`, WAL, auto-vacuum, views,
triggers, and sibling merging in table trees are missing *features*. Anything that touches bytes on disk
matches the format exactly.

## How that is checked

- **`PRAGMA integrity_check` is the acceptance test.** [`quilldb validate`](../README.md) runs quilldb's
  own tree validators and then real `sqlite3` read-only, and fails if either finds a problem.
- **Real SQLite rejects some things as a hard parse error**, not an `integrity_check` finding. A page with
  zero cells is one: it reports `database disk image is malformed`. quilldb once wrote those after
  certain deletes; see `NOTES.md` B8-1. The lesson is that a quilldb-side validator is not enough, so the
  differential tests open the file with real `sqlite3`.
- [`src/tests/differential/`](../src/tests/differential/) runs the same SQL through quilldb and real
  `sqlite3` and diffs the results.

Further reading: [pages and the pager](theory/storage/01-pages-and-pager.md),
[the slotted page](theory/storage/02-slotted-page.md),
[varints and records](theory/codec/03-encoding-varints-and-records.md),
[B-tree mechanics (overflow)](theory/btree/06-b-tree-mechanics.md).
