# 11 — Index B-trees


> **Read during week 4, before you build `btree/index.py`.** Covers `CREATE INDEX`, index key
> encoding, and the `IndexScan` operator.
>
> **Time:** ~45 minutes. **Prerequisites:** [chapter 03](../codec/03-encoding-varints-and-records.md)
> (records and serial types), [chapter 06](06-b-tree-mechanics.md) (cells and the four cell formats).


---


## 11.0 The problem: your B-tree is fast, and useless for this query


Week 2 built a B+tree keyed by rowid. `SELECT * FROM users WHERE id = 4173` is four page reads out of
2,400 — the whole point of chapter 05.


Now:


```sql
SELECT * FROM users WHERE email = 'ada@example.com';
```


Four page reads becomes **2,400**. The tree is sorted by rowid, and you're asking about email. Sorted
order is only useful along the axis it's sorted on, and there is exactly one such axis. The structure
that made the first query 600× faster does nothing at all for the second.


**How do you make lookups by a non-key column fast, without a separate structure per query pattern
and without doubling your write cost?** This is the question indexes answer, and it's worth sitting
with for a minute because the answer is more elegant than most people expect.


---


## 11.1 Your first instinct: keep a second copy, sorted by email


Store the whole table twice — once ordered by rowid, once ordered by email. Now both queries are four
reads.


**Why it breaks, and it's not the reason you'd guess first.** The obvious objection is space: 2× for
one indexed column, 4× for three. That's real but survivable; disk is cheap.


The fatal objection is **write cost and the consistency problem behind it.** Every `INSERT` now writes
two trees. Every `UPDATE` to *any* column rewrites the row in *both* copies, even an update to a
column neither tree is sorted on. And if the second write fails while the first succeeded, you have
two copies of the table that disagree — and no way to know which is right.


Now the insight that unlocks it. Look at what the second copy is actually *for*: you searched it by
email, and then you... already had the row. You didn't need the row to be there. **You needed a way to
get from an email to a place where the row already lives.** The row's address is a rowid, and a rowid
is at most 8 bytes.


> So the second structure doesn't need the data. It needs `(email → rowid)`, sorted by email. That's
> smaller, and — the part that matters — it only has to be rewritten when `email` changes.


---


## 11.2 The answer: it's the same B-tree with the payload thrown away


An index is not a new data structure. It is your existing B+tree, holding cells whose key is the
indexed column(s) and which carry **no payload of their own**. That's why the roadmap says index
B-trees "use the *same* `btree.c` code" — SQLite did not write a second tree implementation.


Chapter 06 §6.0 gave you four cell formats. Here are the two that matter now, and the difference
between them is the whole design:


```
TABLE leaf cell:  [payload-length varint][rowid varint][payload]
INDEX leaf cell:  [payload-length varint][payload]
                                          ^^^^^^^^^ the key IS the payload
```


Verified on a real file — `t(a,b,c)` with `CREATE INDEX ix ON t(b,c)`, five rows, index root at page 3:


```
=== page 3: type byte = 10 (leaf index)
    header[0:8] = 0a 00 00 00 05 0f c5 00        ncell = 5
    cell pointers = [4085, 4073, 4061, 4049, 4037]


  cell[0] @4085  [payload-len=10][payload]
        payload: 04 13 13 09 62 62 31 63 63 31
        record header size = 4, three columns:
           col0  serial type 19  TEXT(3)  'bb1'
           col1  serial type 19  TEXT(3)  'cc1'
           col2  serial type 9   integer  1        <-- the rowid
```


And the same database's table leaf, for contrast:


```
=== page 2: type byte = 13 (leaf table)
  cell[0] @4083  [payload-len=11][rowid=1][payload]
        payload: 04 01 13 13 0a 62 62 31 63 63 31
           col0 integer 10 | col1 TEXT(3) 'bb1' | col2 TEXT(3) 'cc1'
```


### The asymmetry, stated crisply


> **In a table leaf, the record holds `(a, b, c)` and the rowid sits *outside* the record, in the cell
> header. In an index leaf, the record holds `(b, c, rowid)` and the rowid sits *inside* the record.**


Same rowid, same tree code, opposite side of the boundary. Once you can say that sentence you
understand index b-trees.


### That there's no rowid varint in an index cell is provable, not assumed


You don't have to trust the field labels. Count bytes. `cell[0]` starts at offset 4085 and the page
ends at 4096, so the cell occupies exactly **11 bytes**. The length varint is `0a` — one byte, value
10. 1 + 10 = 11. **There is no byte left over for a rowid varint.**


Do the same for the table leaf: cell at 4083 → 13 bytes. Length varint `0b` = 11, rowid varint `01`
= 1 byte, payload 11 bytes. 1 + 1 + 11 = 13. Both accounted for exactly.


This is a habit worth keeping when reading any binary format: **the offsets and the page boundary
give you an independent check on your field list.** If your parse doesn't add up to the byte count,
your field list is wrong, and you find out immediately rather than three chapters later.


### Page types


| Page kind | type byte | header size |
|---|---|---|
| interior index | **2** | 12 |
| interior table | 5 | 12 |
| **leaf index** | **10** | 8 |
| leaf table | 13 | 8 |


Nothing new — indexes reuse the same page machinery, and `decodeFlags` accepts these four values and
nothing else (chapter 02 §2.5). You find an index's root page the same way you find a table's:
`SELECT rootpage FROM sqlite_schema WHERE name = 'ix'`.


> **Say this out loud:** "An index is the same B+tree with the payload removed. A table leaf cell is
> length, rowid, then the row; an index leaf cell is length then the key, where the key is a record
> containing the indexed columns *plus the rowid*. So the rowid moves from outside the record to
> inside it, and the same tree code serves both."


---


## 11.3 Why the rowid is glued onto every key


The rowid's presence in the key does three separate jobs, and it's worth separating them because only
the first is obvious.


**1. You need it as the answer.** The index's whole purpose is to hand you a row address. Without the
rowid in the entry, finding `'ada@example.com'` in the index tells you the row exists and nothing
about where.


**2. It makes every key unique, which deletes an entire category of code.** Consider an index on
`city` with 40,000 rows in `'Hanoi'`. Without the rowid, you have 40,000 identical keys — and a B-tree
with duplicate keys needs answers to awkward questions: which one does a search land on? How do you
iterate all of them? What does a split do when the separator value appears on both sides?


Append the rowid and every key becomes distinct, because rowids are. Duplicates become *adjacent
distinct keys*: `('Hanoi', 17)`, `('Hanoi', 92)`, `('Hanoi', 415)`. Searching for all Hanoi rows
becomes "seek to the first key ≥ `('Hanoi', -∞)`, then scan forward while the first column is
`'Hanoi'`" — an ordinary range scan with no duplicate handling anywhere.


**This is a technique, not a quirk:** when duplicates make a structure awkward, extend the key with
something you already have that's unique, and the awkwardness disappears. The cost is a few bytes per
entry.


**3. It makes deletion addressable.** `DELETE FROM users WHERE id = 92` has to remove *that row's*
entry from the `city` index — not one of the other 39,999 `'Hanoi'` entries. The rowid in the key is
what lets you construct the exact entry to delete: you have the row, so you have its city and its
rowid, so you can build the key and delete it directly. Without it, you would have to scan all 40,000
duplicates comparing payloads.


### The verified surprise: a UNIQUE index still appends the rowid


If reason 2 is about disambiguating duplicates, a `UNIQUE` index has no duplicates and shouldn't need
the rowid. That's a genuinely good inference, and it's wrong. `CREATE UNIQUE INDEX uix ON t(b,c)` over
the same rows:


```
  cell[0] @4085  [payload-len=10][payload]
        payload: 04 13 13 09 62 62 31 63 63 31    <-- byte-identical to the non-unique index
```


Not one byte differs. So how is uniqueness enforced? **By comparing only the first N columns** — the
declared key columns — and ignoring the trailing rowid during the uniqueness check, while still using
it for ordering and addressing. The rowid is stored in every index entry; the *comparison routine*,
not the key format, is what changes for `UNIQUE`.

Storing the rowid even here is the right call, for two reasons that have nothing to do with
disambiguating duplicates:

1. **One key format, one comparison routine.** Every index — unique or not — stores the same
   `(key columns..., rowid)` layout. Uniqueness becomes a parameter of the comparison ("only the
   first N columns count"), not a second on-disk format. Dropping the rowid from unique indexes
   would fork both the layout and the code that reads it.
2. **Deletion still needs to be addressable (reason 3 above).** Even in a unique index, `DELETE FROM
   t WHERE id = 92` must remove *that row's* entry, not just any entry with a matching key. Since the
   key columns are unique, in practice there's only one entry to find — but the engine still needs
   the rowid on hand to construct the key and locate it, and keeping it in the entry is what makes
   that construction trivial and uniform across every index, unique or not.


The same holds for indexes SQLite creates implicitly. `b TEXT UNIQUE` produces
`sqlite_autoindex_t2_1`, and its entry for `('zz7', rowid 7)` is `03 13 01 7a 7a 37 07` — key columns
then rowid, exactly like an explicit index.


> ⚠️ **This one is worth noticing as a lesson about optimizing.** Dropping the rowid from unique
> indexes looks free: the key is already unique, so why store more? But it would fork the format,
> fork the comparison logic, and remove the ability to address a specific entry — three costs to save
> one to eight bytes on a subset of indexes. **A local saving that forks a code path is usually a bad
> trade,** and recognizing that shape is more valuable than the specific fact.


---


## 11.4 The trailing rowid is a record *field*, not an appended varint


This is the detail most likely to cost you an afternoon, because both designs are plausible and they
produce different bytes.


**The plausible-but-wrong model:** an index key is a record of the indexed columns, with the rowid
tacked on afterwards as a bare varint — the same encoding table leaf cells use for their rowid.


**What it actually is:** the rowid is *one more column of the record*, with its own serial type in the
record header, exactly like the indexed columns.


The evidence is decisive rather than suggestive. Index the same table with deliberately chosen rowids
and look at what the last column costs:


| rowid | serial type | body bytes | body hex | a bare varint would be |
|---|---|---|---|---|
| 0 | **8** (`int0`) | **0** | *(none)* | `00` — 1 byte |
| 1 | **9** (`int1`) | **0** | *(none)* | `01` — 1 byte |
| 162 | 2 | 2 | `00 a2` | `81 22` |
| 1,099,511,627,776 | 5 | 6 | `01 00 00 00 00 00` | 6 bytes, different ones |
| 9,223,372,036,854,775,807 | 6 | 8 | `7f ff ff ff ff ff ff ff` | 9 bytes, `ff…ff 7f` |


**Rowids 0 and 1 occupy zero body bytes.** No varint encoding can represent a value in zero bytes —
the minimum is one. Those are serial types 8 and 9 from chapter 03's table, the ones that encode the
constants 0 and 1 in the type itself and consume nothing in the body. The 162 case is equally
conclusive: stored as fixed-width big-endian `00 a2`, where a varint would be `81 22`.


### Why it's a column, and not the shortcut


Because of what a comparison has to do. Searching an index means comparing your probe key against
stored keys, and if the key is *entirely* a record, then comparison is: decode both records, compare
column by column using the ordinary value ordering from chapter 03 (NULL < numeric < text < blob).
One routine, and it's the routine you already wrote.


Make the rowid a bare trailing varint instead and the record stops describing its own contents. The
header says "3 columns" but there are 4 values; every reader needs out-of-band knowledge that "for
index pages, there's one more thing after the record." Every comparison needs a special final step.
Every length calculation needs a special case.


> **The general principle:** if a structure already has a general mechanism, prefer expressing the new
> thing *in* that mechanism over bolting it alongside — even when the bolt-on is marginally smaller.
> Uniformity is what lets the same code serve four cell formats.


And here it isn't even smaller. For the small rowids that dominate a real table, serial types 8 and 9
make it **free**, beating the varint by a byte on the two most common values in a fresh table.


---


## 11.5 The double lookup, and how a covering index deletes half of it


An index gets you a rowid. If the query wants columns the index doesn't have, you then search the
*table* tree for that rowid. Two descents, not one:


```
SELECT age FROM users WHERE email = 'ada@example.com';


  1. descend the EMAIL index   ─── 3 reads ──►  rowid = 4173
  2. descend the TABLE tree    ─── 4 reads ──►  the row, then read `age`
                                    ────────
                                     7 reads
```


Still vastly better than 2,400. But notice that step 2 exists only because `age` wasn't in the index.
Put it there — `CREATE INDEX ix ON users(email, age)` — and the index entry already contains
everything the query asked for. Step 2 disappears. That's a **covering index**, and SQLite's
optimizer documentation puts the payoff plainly:


> "When an index contains all of the data needed for a query and when the original table never needs
> to be consulted… saves one binary search for each row and can make many queries run twice as fast."


The planner tells you when it happens. With an index on `t(a,b,c)`:


```
SELECT a,b,c FROM t WHERE a=1      →  SEARCH t USING COVERING INDEX abc (a=?)
SELECT c     FROM t WHERE a=1      →  SEARCH t USING COVERING INDEX abc (a=?)
SELECT a,b,c,d FROM t WHERE a=1    →  SEARCH t USING INDEX abc (a=?)          ← d isn't in the index
SELECT count(*) FROM t             →  SCAN   t USING COVERING INDEX abc
SELECT b FROM t WHERE c=3          →  SCAN   t USING COVERING INDEX abc
```


The word `COVERING` appears exactly when every referenced column is available in the index, and
vanishes the moment `d` joins the select list. Two of those lines are worth a second look:


- **`count(*)` scans the index, not the table.** Counting needs no column values at all, so the
  planner picks whichever tree is *narrower* — and an index on three columns is narrower than a table
  with more. Fewer bytes means fewer pages means fewer reads. Nothing about this is a "seek"; it's
  purely an I/O-volume decision.
- **`WHERE c=3` uses the index even though `c` is the *third* column**, which §12.5 will explain is
  unusable for seeking. The planner can't seek, so it scans — but it scans the *index*, because that's
  still less I/O than scanning the table. **An index can help a query it cannot seek.**


That last point is the one people miss. "Will this query use my index?" and "will this query *seek*
in my index?" are different questions, and `EXPLAIN QUERY PLAN` distinguishes them with `SEARCH`
versus `SCAN`.


> **Say this out loud:** "A non-covering index costs two descents — one to find the rowid, one to fetch
> the row. A covering index contains every column the query needs, so the second descent disappears
> and it's roughly twice as fast. And a wide table with a narrow index means even a full `SCAN` of the
> index can beat a scan of the table, because the cost is pages, not rows."


---


## 11.6 Interior index pages carry the whole key, and that costs you depth


In a table b-tree, an interior cell is `[4-byte child][rowid varint]` and carries no payload —
separators are pure routing values (chapter 10 §10.4). Index interior cells are different, and the
difference has consequences. Verified on an index over 2000 rows:


```
=== page 3: type byte = 2 (interior index)
    header[0:12] = 02 00 00 00 0c 0e d4 00 00 00 00 1c
    ncell = 12    rightmost child = page 28


  cell[0] @4071  [child=6][payload-len=20][payload]
        payload: 04 1b 1b 02 62 30 30 30 31 36 32 63 30 30 30 31 36 32 00 a2
           col0 TEXT(7) 'b000162' | col1 TEXT(7) 'c000162' | col2 integer 162
```


**The divider key is the full key** — both text columns and the rowid, uncompressed and untruncated.
It is simultaneously a fence and a complete index entry.


Two consequences:


**1. Fanout drops, so index trees are deeper than table trees.** Compare the arithmetic on a
4096-byte page with a 12-byte interior header (4084 usable, and each cell also needs its 2-byte
pointer):


| | bytes per interior entry | children per page |
|---|---|---|
| table interior | 4 (child) + ~3 (rowid varint) + 2 (pointer) ≈ **9** | ~450 |
| index interior, 20-byte key | 4 + 1 (length) + 20 + 2 ≈ **27** | ~150 |


Both are fine — 150³ is still 3.4 million rows in three levels — but an index on a long `TEXT` column
has genuinely lower fanout than the table it indexes, and an index on a 200-byte column has *much*
lower. **This is the concrete reason "don't index a long text column" is standard advice**, and it's a
better answer than "it's slow": the key length divides your fanout, which multiplies your depth.


**2. Deleting an index entry can require touching an interior page.** Because the divider *is* a real
entry, removing that entry means the fence must be replaced with a key promoted from a leaf below —
the classic delete-from-an-internal-node problem, and the reason chapter 10 §10.4 could promise that
*table* deletion never touches the parent while index deletion isn't so lucky.


Chapter 06's roads-not-taken covered the fix SQLite doesn't apply here: **suffix truncation**, where
a divider is shortened to the shortest string that still separates its neighbours (`'b000162'` might
become `'b0001'`). It would raise fanout on text indexes substantially. SQLite doesn't do it, and the
reason is the one from §11.3 — a divider that is also a real entry can't be truncated without
becoming a different kind of thing, which forks the format.


---


## 11.7 `WITHOUT ROWID`: what if the table just *were* the index?


The double lookup in §11.5 exists because the table is keyed by rowid and the index is keyed by
something else. Follow that thought to its conclusion: **what if the table were keyed by its primary
key directly?** Then a PK lookup is one descent, and there is no rowid to indirect through.


That's `WITHOUT ROWID`, and the implementation is not a new page type — it reuses the index b-tree
wholesale. A census of every page in a `WITHOUT ROWID` database of 2000 rows:


```
  type byte  2 (interior index):  1 page
  type byte 10 (leaf index):     39 pages
  type byte 13 (leaf table):      1 page      <-- page 1, sqlite_schema, which IS a rowid table
```


**Zero table b-tree pages belong to the table itself.** And its cells hold the entire row with no
trailing rowid, because the primary key *is* the key:


```
=== wt(k TEXT PRIMARY KEY, v TEXT, extra TEXT) WITHOUT ROWID — root page 2
    header[0:8] = 0a 00 00 00 03 0f df 00        <-- 0x0a = 10 = leaf INDEX
  cell[0] @4085  [payload-len=10][payload]
        col0 TEXT(2) 'k1' | col1 TEXT(2) 'v1' | col2 TEXT(2) 'e1'
```


### One surprise worth knowing


SQLite **reorders the record so the primary-key columns come first**, regardless of how you declared
them. `CREATE TABLE w(v TEXT, k TEXT PRIMARY KEY, z TEXT) WITHOUT ROWID` storing `('VVV','KKK','ZZZ')`
lands on disk as:


```
  payload: 04 13 13 13 4b 4b 4b 56 56 56 5a 5a 5a
           col0 = 'KKK'   col1 = 'VVV'   col2 = 'ZZZ'
           ^^^^^ the PK, moved to the front
```


Declared order `(v, k, z)`; stored order `(k, v, z)`. It has to be this way — the key must be a
*prefix* of the record for column-by-column comparison to order the tree correctly. Which means
**logical column order and physical column order are not the same thing**, and anything reading these
pages needs the schema to map between them.


### What it buys and what it costs


| | rowid table | `WITHOUT ROWID` table |
|---|---|---|
| Lookup by primary key | 2 descents (PK index → table) | **1 descent** |
| Lookup by rowid | 1 descent | there is no rowid |
| Secondary index entry | key + small rowid (1–8 bytes) | key + **the full primary key** |
| Good when | PK is an integer, or you have many secondary indexes | PK is the natural access path, rows are small |


The cost is the row that matters: every secondary index now stores the whole primary key as its
address instead of a compact integer. A `WITHOUT ROWID` table with a 40-byte text PK and three
secondary indexes pays 40 bytes per entry per index instead of ~4 — which lowers those indexes' fanout
too, per §11.6.


**This is the clustered-vs-heap decision, and every engine picks a side:** InnoDB *always* clusters by
primary key, so every MySQL table behaves like `WITHOUT ROWID` and every secondary index stores the
PK. PostgreSQL never clusters — rows live in a heap and every index points at a physical tuple id.
SQLite offers both and defaults to the heap-ish rowid table. Being able to say which engine does
which, and why the tradeoff is the size of the secondary-index entry, is a strong five-sentence answer
to a common interview question.


**quilldb builds rowid tables only.** `WITHOUT ROWID` is a schema feature, not a format extension, so
skipping it costs no fidelity — and the code you'd write is the index b-tree you already have.


---


## 11.8 Comparing keys: what "lexicographic" actually means


Your `IndexBTree` needs a comparison function, and "compare the keys" hides three decisions.


**1. Column by column, left to right, using the type ordering.** Chapter 03 established SQLite's
cross-type order: **NULL < integers and reals (by value) < text < blob.** Comparison decodes both
records and walks columns, returning at the first difference. It does *not* memcmp the encoded bytes —
that would give the wrong answer the moment two keys have different serial types for the same column.


**2. NULLs sort first, and they're allowed in an index.** A NULL indexed column gets serial type 0 and
sorts before everything, which is verified behaviour: an all-NULL index entry occupies the first
position in the leaf. Note this is index ordering, not `UNIQUE` semantics — SQL says NULLs aren't equal
to each other, so a unique index accepts many NULLs. Two rules that seem to contradict and don't:
ordering asks "which comes first," uniqueness asks "are these equal," and NULL answers those
differently.


**3. Text needs a collation.** `'ADA' = 'ada'` depends on the collating sequence: `BINARY` (default,
`NOCASE` (ASCII case-insensitive), or `RTRIM` (ignores trailing spaces). The collation is a
property of the *index*, which produces a consequence people trip over: **an index built with one
collation cannot serve a query that needs another.** `CREATE INDEX ON users(email)` is `BINARY`, so
`WHERE email = 'ADA@X.COM' COLLATE NOCASE` cannot use it — the index's sort order isn't the order the
query needs. This is the same principle as the leading-column rule in chapter 12: an index helps only
when its ordering matches the ordering the query asks about.

**What `BINARY` actually means.** It's not "compare as raw bytes" in the way rule 1 already ruled out
— you still decode the record and get to a Python/Rust/whatever string. `BINARY` says: once you have
that string, compare it byte-by-byte over its *encoded form* (UTF-8, for SQLite) with no
transformation first. No case-folding (`'ADA'` and `'ada'` are unequal, and `'B' < 'a'` because
uppercase bytes sort below lowercase in ASCII/UTF-8), no accent-stripping, no locale awareness — just
`memcmp` on the bytes of the string once you're down to that one column. `NOCASE` and `RTRIM` are
`BINARY` plus a transform applied before the comparison (uppercase everything; strip trailing spaces);
they are not different byte-orderings, they're different *preprocessing*. That's also why `NOCASE`
must stay ASCII-only in SQLite's built-in version — a real Unicode case fold needs locale-aware
tables SQLite doesn't ship by default.

quilldb implements `BINARY` only — plain byte comparison of the UTF-8 string, no case-folding — which
is the default and covers every test you'll write. Say so in the docs rather than leaving it implied.


---


## 11.9 What you're building


Roadmap week 4, 5.5 hours across three tasks. The whole key encoder is one line, and that's the point:


```python
def encode_index_key(values: list[Value], rowid: int) -> bytes:
    """An index key is just a record whose last column is the rowid."""
    return encode_record([*values, rowid])
```


If that surprises you, re-read §11.4 — it's the payoff of the rowid being a column. You already wrote
this code in week 1.


```python
class IndexBTree:
    def insert(self, values: list[Value], rowid: int) -> None: ...
    def delete(self, values: list[Value], rowid: int) -> None: ...
    def seek_eq(self, values: list[Value]) -> Iterator[int]:
        """Yield rowids whose leading columns equal `values`."""
    def seek_range(self, low, high, *, low_inclusive, high_inclusive) -> Iterator[int]: ...
```


**Four things to get right:**


1. **`seek_eq` is a range scan, not a point lookup.** Because every key ends in a rowid, no key equals
   your probe. Seek to the first entry ≥ `(values..., -∞)` and walk forward while the leading columns
   still match. Getting this wrong gives you *one* row where you should get all matches — and it
   passes every test whose data happens to have unique keys, which is why your test data must include
   duplicates.
2. **Uniqueness compares the leading N columns only.** Ignore the rowid for the violation check, use
   it for ordering. And raise **before** writing anything (roadmap week 4: "a `UNIQUE` violation raises
   *before* any page is written").
3. **Maintenance is delete-then-insert on the old and new values.** For `UPDATE`, the old index entry
   is built from the row's *pre-update* values — so read the row before you overwrite it. Forgetting
   this leaves orphaned entries that make the index disagree with the table.
4. **Index deletion must handle the interior-page case** (§11.6), or must avoid creating it. The
   cheap correct option: never delete a divider — if the entry to remove lives on an interior page,
   leave the divider in place as a routing-only fence and remove only the leaf copy. Document it,
   because a divider that no longer corresponds to a live entry is legal but *is* something your
   validator must not flag.


**The test that matters more than all the others:** a property test asserting that a full index scan
and a full table scan plus filter return identical rows, run after every mutation. The roadmap calls
this the index-vs-scan consistency check, and it's what catches every "the index and the table
disagree" bug in one assertion. Run it inside the week-5 crash loop too.


Also assert the *negative* direction: adding an index must never change a query's **results**, only
its speed. Run your whole differential test suite with and without each index and compare outputs.


---


## 11.10 Roads not taken


### Hash indexes


Hash the key, store entries in buckets. **O(1) equality** instead of O(log n).


**Used by:** PostgreSQL (`USING HASH`, and rarely chosen), in-memory engines, and every hash join.


**Why not here, and it's the same reason as chapter 05 §5.4:** a hash destroys order. No range scans,
no `ORDER BY`, no prefix matching, no `LIKE 'ada%'` — and the leading-column trick in §11.3 that turns
duplicates into adjacent distinct keys stops working, because "adjacent" is meaningless. You'd be
buying a constant factor on equality lookups and giving up every ordered operation. SQLite doesn't
implement hash indexes at all, and neither should you.


### Partial and expression indexes (things SQLite has and you won't build)


`CREATE INDEX ix ON orders(user_id) WHERE status = 'open'` indexes only the matching rows — much
smaller, and perfect when queries always carry that predicate. `CREATE INDEX ix ON users(lower(email))`
indexes a computed value, which is how you make `WHERE lower(email) = ?` sargable at all (chapter 12
§12.2 explains why it otherwise isn't).


Both are genuinely useful, both are real SQLite features, and both are cheap *conceptually* but
require the binder and planner to match an expression tree against an index definition — which is more
week-4 scope than the schedule has. **Worth naming as future work precisely because they show you know
what an index can be**, beyond "a sorted copy of a column."


### Bitmap indexes


One bitmap per distinct value; `AND`/`OR` predicates become bitwise operations over them.
**Used by:** Oracle, and column stores generally. **Why not:** they shine on low-cardinality columns
in read-mostly analytic workloads, and degrade badly under updates because a single row change touches
every bitmap. Wrong shape for OLTP.


### Index-organized everything


Force every table to be `WITHOUT ROWID`. **Used by:** InnoDB, effectively. **Why not:** §11.7's table
— you'd pay full-PK addresses in every secondary index, and lose the cheap integer identity that makes
rowid tables pleasant. Offering both is strictly more flexible, which is why SQLite does.


---


## 11.11 Check yourself


1. Why doesn't a rowid-keyed B+tree help `WHERE email = ?` at all?
2. Keeping a second copy of the whole table sorted by email is wrong — and space isn't the main
   reason. What is?
3. Write out the index leaf cell format and the table leaf cell format, and state where the rowid is
   in each.
4. An index cell starts at offset 4085 on a 4096-byte page and its length varint is `0a`. Prove there
   is no rowid varint in it.
5. Give the three separate jobs the trailing rowid does.
6. An index on `city` has 40,000 rows in `'Hanoi'`. What does appending the rowid do to the
   duplicate-key problem, and what does a "find all Hanoi rows" search become?
7. A `UNIQUE` index has no duplicate keys, so why does it still store the rowid? How is uniqueness
   enforced instead?
8. Rowid 0 occupies zero bytes in an index key. What does that prove about the encoding?
9. Why is the rowid a record column rather than a varint appended after the record?
10. What is a covering index and roughly what does it save? Why does `SELECT count(*)` use one?
11. `WHERE c = 3` on an index over `(a,b,c)` can't seek. Why might the planner still use the index?
12. Why is an index on a 200-byte text column deeper than an index on an integer, and what's the
    arithmetic?
13. Why can index deletion touch an interior page when table deletion never needs to?
14. In a `WITHOUT ROWID` table, what happens to declared column order, and why must it?
15. What does `WITHOUT ROWID` do to the size of secondary index entries? Which engine always behaves
    that way, and which never does?
16. Why can't `WHERE email = 'X' COLLATE NOCASE` use a `BINARY` index on `email`?
17. Why must your index test data contain duplicate keys?


---


## 11.12 Sources


- [SQLite Database File Format](https://www.sqlite.org/fileformat2.html) — the four cell formats;
  index leaf cells as `[payload length][payload]` with no rowid field; page types 2 and 10; serial
  types 8 and 9 encoding the constants 0 and 1 in zero body bytes.
- [The SQLite Query Optimizer Overview](https://www.sqlite.org/optoverview.html) — covering indexes:
  "When an index contains all of the data needed for a query and when the original table never needs
  to be consulted… saves one binary search for each row and can make many queries run twice as fast."
- [`WITHOUT ROWID` tables](https://www.sqlite.org/withoutrowid.html) — clustered storage, and the
  tradeoffs against rowid tables.
- [Datatypes in SQLite](https://www.sqlite.org/datatype3.html) — the cross-type sort order and the
  `BINARY` / `NOCASE` / `RTRIM` collating sequences.
- **Measurements in §11.2, §11.3, §11.4, §11.6 and §11.7** were taken on `sqlite3` 3.37.2 by dumping
  raw pages with a hand-written parser. The rowid-encoding table in §11.4 used adversarially chosen
  rowids (0, 1, 162, 2⁴⁰, 2⁶³−1) specifically to distinguish record-field encoding from varint
  encoding; the `EXPLAIN QUERY PLAN` output in §11.5 is verbatim. The string `USING COVERING INDEX`
  does not appear in `optoverview.html` — that exact wording is verified empirically only.


---


**Next:** [12 — The query planner](../plan/12-the-query-planner.md) — how the engine decides to use
the index you just built, and why `WHERE lower(email) = 'x'` throws all of it away.