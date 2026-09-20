# quilldb — Implementation Plan: Week 4, Mutation and Indexes


← [Index](README.md)  ·  Prev: [Week 3 — SQL](week-3-sql.md)  ·  Next: [Week 5 — Transactions](week-5-transactions.md)


---


# Week 4 Spec — Deletion, Indexes, and a Planner


> **Read first:** [chapter 10](../theory/btree/10-deletion-and-space-reuse.md) before the delete
> session, [chapter 11](../theory/btree/11-index-b-trees.md) before the index sessions, and
> [chapter 12](../theory/plan/12-the-query-planner.md) before the planner. Chapters 10–11 are ~40
> minutes each; the expanded cost-based planner chapter is ~60 minutes. Each answers a design question
> this spec assumes you've already settled.


Week 3 gave you a database you could read from. This week makes it a database you can *keep data in*
and one that gets dramatically faster when you tell it what you query on.


**Two independent deliverables**, and it's worth knowing which is which if you have to descope.
Deletion and update are *correctness* work — they close the gap between "append-only log with SQL" and
"database." Indexes and the planner are *performance* work, and they produce the single most legible
artifact in the whole project: the before/after page-read number that goes in the README.


If the week runs short, **ship indexes and cut `UPDATE`.** `DELETE` plus `INSERT` covers the same
demo, and the index number is what an interviewer remembers.


---


## Week 4 contract


Added to the SQL surface this week:


```sql
DELETE FROM users WHERE age < 18;
UPDATE users SET age = age + 1 WHERE id = 4;
CREATE INDEX idx_email ON users (email);
CREATE UNIQUE INDEX idx_ssn ON users (ssn);
ANALYZE;
EXPLAIN SELECT * FROM users WHERE email = ?;
EXPLAIN ANALYZE SELECT * FROM users WHERE email = ?;
```


Supported:


- `DELETE FROM t [WHERE ...]` — including no `WHERE`, meaning all rows
- `UPDATE t SET col = expr [, col = expr]* [WHERE ...]`
- `CREATE [UNIQUE] INDEX name ON table (col [, col]*)`
- `ANALYZE [table-or-index]` collecting table and index-prefix statistics into `quill_stat1`
- `EXPLAIN <select>` returning estimated rows and costs without executing
- `EXPLAIN ANALYZE <select>` executing once and adding actual rows and page reads
- indexes are maintained automatically by `INSERT`, `UPDATE`, and `DELETE`


Explicitly **not** supported, and rejected with a typed error rather than misinterpreted:


- `DROP INDEX`, `DROP TABLE`, `ALTER TABLE`
- partial indexes (`... WHERE`), expression indexes (`... ON t(lower(x))`), `COLLATE` in an index
- `DESC` in an index definition — `BINARY` ascending only
- `WITHOUT ROWID`, `AUTOINCREMENT`, foreign keys, `CHECK`
- `INSERT OR REPLACE` / `ON CONFLICT` — a `UNIQUE` violation is always an error
- skip-scan, `OR`-decomposition, and multi-index intersection in the planner


Three semantic rules that are not negotiable:


1. **An index must never change a query's results, only its speed.** This is a property test, not a
   hope. See "the two tests that matter" below.
2. **A `UNIQUE` violation raises before any page is written.** Check every index for conflicts first,
   then mutate. Until week 5 you have no rollback, so a half-applied insert is permanent damage.
3. **Index entries are records whose last column is the rowid** — chapter 11 §11.4. Not a bare varint
   appended after the record. Getting this wrong produces a file `sqlite3` will read as corrupt.


---


## Week 4 file map


```
src/quilldb/
├── btree/
│   ├── btree.py         + delete()
│   └── index.py         NEW — encode_index_key, IndexBTree
├── catalog/
│   ├── catalog.py       + create_index(), indexes_for()
│   └── stats.py         NEW — ANALYZE, TableStats, IndexStats, quill_stat1
├── plan/                NEW
│   ├── planner.py       enumerate_access_paths(), choose_access_path()
│   ├── cardinality.py   estimate rows from prefix statistics
│   ├── cost.py          page-oriented cost model
│   └── explain.py       format_plan()
├── sql/
│   ├── ast.py           + Delete, Update, CreateIndex, Analyze, Explain
│   └── parser.py        + the new statements
├── exec/
│   └── operators.py     + IndexScan, DeleteOp, UpdateOp
└── errors.py            + UniqueViolationError
```


---


## 21. Week 4 additions to `errors.py`


```python
class UniqueViolationError(QuillDBError):
    """An insert or update would duplicate a UNIQUE index key.


    Raised BEFORE any page is modified. Carries the index name and the
    conflicting key so the message is actionable.
    """


    def __init__(self, index_name: str, key: tuple[object, ...]) -> None:
        self.index_name = index_name
        self.key = key
        super().__init__(f"UNIQUE constraint failed on index {index_name!r}: key {key!r} already exists")
```


Nothing else new. Resist adding an `IndexCorruptError` — index inconsistency is a *bug*, not a
condition, and it should trip an assertion in the validator rather than become a catchable exception.


---


## 22. `btree/btree.py` — deletion


### The stub


```python
    def delete(self, rowid: int) -> bool:
        """Remove the row with this rowid.


        Returns:
            True if a row was removed, False if the rowid wasn't present.


        Algorithm (chapter 10 §10.4 explains why this is allowed to be simple):
          1. Descend to the leaf that would hold `rowid`, keeping the path.
          2. Find the cell; if it isn't there, return False.
          3. Remove it and repack the page — no freeblock chain (chapter 02 §2.4).
          4. If the page is now empty AND is not the root:
               a. remove the separator cell pointing at it from the parent,
               b. free the page to the freelist, fully zeroed,
               c. if the parent is now empty, recurse from (4).
          5. If the ROOT is empty and is an interior page, convert it back to a
             leaf in place. Its page number must NOT change — sqlite_schema
             records it.


        Deliberately NOT done: merging underfull siblings. SQLite rebalances
        when free space exceeds 2/3 of a page; quilldb never does. The tree is
        still valid, just less dense. Chapter 10 §10.3.
        """
```


### Three traps, in the order you'll hit them


**1. Freeing a page must zero the whole page.** You proved this in week 1: bytes 4–7 of a freelist
trunk page are the leaf count, and stale row data there makes `sqlite3` report `Main freelist:
freelist leaf count too big`. Deletion is now a far busier source of freed pages than week 1 was, so
this bug will find you if the test doesn't.


**2. Removing the separator from the parent is a *different* operation from removing a row.** The
parent's cell is `[left child][rowid]` with no payload. You're deleting a cell by *slot index*, not by
key. Keep those two code paths visibly separate or you'll pass a rowid where a slot is wanted.


**3. Root collapse must preserve the page number.** Convert page N from an interior page to a leaf by
rewriting its header in place. Do not allocate a new leaf and update `sqlite_schema` — that works, and
it silently breaks any cursor holding the old root, and it's more code.


### The tests


```python
def test_delete_returns_false_for_missing_rowid(tree):
    assert tree.delete(999) is False




def test_delete_every_row_in_random_order(tmp_path):
    """The single highest-value delete test."""
    tree = build_tree(tmp_path, rows=2000)
    order = list(range(1, 2001))
    random.Random(42).shuffle(order)          # seeded — a flake you can't reproduce costs an hour
    for i, rowid in enumerate(order):
        assert tree.delete(rowid) is True
        if i % 50 == 0:
            assert validate(tree).is_valid    # every 50th, not every one: keeps the test under a second
    assert validate(tree).is_valid
    assert list(tree.scan()) == []




def test_root_reverts_to_leaf_when_tree_empties(tmp_path):
    tree = build_tree(tmp_path, rows=2000)
    root = tree.root_page
    assert page_type(tree, root) == PageType.INTERIOR_TABLE     # it split during build
    for rowid in range(1, 2001):
        tree.delete(rowid)
    assert page_type(tree, root) == PageType.LEAF_TABLE          # collapsed
    assert tree.root_page == root                                # and did NOT move




def test_space_is_reused_not_just_freed(tmp_path):
    """Proves the freelist is consumed, not merely appended to."""
    db = connect(tmp_path / "t.db")
    insert_rows(db, 2000)
    pages_after_first_fill = db.page_count
    delete_all(db)
    insert_rows(db, 2000)
    assert db.page_count == pages_after_first_fill




def test_sqlite3_accepts_a_delete_heavy_database(tmp_path):
    db = connect(tmp_path / "t.db")
    insert_rows(db, 2000)
    db.execute("DELETE FROM t WHERE id % 3 <> 0")
    db.close()
    out = subprocess.run(["sqlite3", str(tmp_path / "t.db"), "PRAGMA integrity_check;"],
                         capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "ok"
```


`test_space_is_reused_not_just_freed` is the one that catches the most common freelist bug — appending
to the freelist correctly while never allocating from it. The page count is the giveaway.


---


## 23. `btree/index.py` — NEW


### The stub


```python
"""Index b-trees: the same B+tree with the payload thrown away.


An index entry is a RECORD whose columns are (indexed values..., rowid) —
the rowid is a record field with an integer serial type, NOT a bare varint
appended afterwards. See docs/theory/btree/11-index-b-trees.md §11.4, which
proves it: rowid 0 and 1 occupy ZERO body bytes (serial types 8 and 9), and
no varint can encode a value in zero bytes.


Pages are types 2 (interior index) and 10 (leaf index). Index leaf cells are
[payload-length varint][payload] — no rowid varint, unlike table leaf cells.
"""




def encode_index_key(values: Sequence[Value], rowid: int) -> bytes:
    """Build the index key for a row.


    That's it — it's one line. The rowid is just the last column, so the
    week-1 record encoder already does the whole job.
    """
    return encode_record([*values, rowid])




def compare_keys(a: Sequence[Value], b: Sequence[Value]) -> int:
    """Compare two decoded keys column by column. Returns -1, 0, or 1.


    Uses SQLite's cross-type ordering (chapter 03): NULL < numbers < text
    < blob. Compares min(len(a), len(b)) columns, so a 2-column probe can be
    compared against a 3-column stored key — which is exactly what a prefix
    seek needs. BINARY collation only.
    """




class IndexBTree:
    def __init__(self, pager: Pager, root_page: int, n_key_columns: int, *, unique: bool) -> None:
        """`n_key_columns` counts the DECLARED columns, excluding the rowid.


        `unique` changes only the conflict check, never the stored bytes — a
        UNIQUE index is byte-identical to a non-unique one (§11.3).
        """


    def insert(self, values: Sequence[Value], rowid: int) -> None: ...


    def delete(self, values: Sequence[Value], rowid: int) -> bool:
        """Remove the entry for exactly this (values, rowid).


        Returns False if absent. The rowid in the key is what makes "exactly
        this one" expressible among duplicates (§11.3, reason 3).
        """


    def find_conflict(self, values: Sequence[Value]) -> int | None:
        """For UNIQUE indexes: the rowid of an existing entry with these key
        values, or None. Compares only the first `n_key_columns` — the rowid
        is excluded from the uniqueness test but present in the key.


        NULL never conflicts: SQL says NULLs aren't equal to each other, so a
        UNIQUE index accepts many NULL keys. Return None if any value is NULL.
        """


    def seek_eq(self, values: Sequence[Value]) -> Iterator[int]:
        """Yield rowids whose leading columns equal `values`, in index order.


        ⚠️ THIS IS A RANGE SCAN, NOT A POINT LOOKUP. Every stored key ends in
        a rowid, so no stored key ever equals your probe. Seek to the first
        entry >= `values` and walk forward while the leading columns still
        match, stopping at the first that doesn't.


        Getting this wrong yields ONE row where you should yield all matches —
        and it passes every test whose keys happen to be unique. Your fixtures
        MUST contain duplicates.
        """


    def seek_range(
        self,
        low: Sequence[Value] | None = None,
        high: Sequence[Value] | None = None,
        *,
        low_inclusive: bool = True,
        high_inclusive: bool = True,
    ) -> Iterator[int]:
        """Yield rowids in key order within the bounds. None means unbounded."""


    def scan(self) -> Iterator[tuple[list[Value], int]]:
        """Every entry in key order, as (key values, rowid). For validation."""
```


### The trap that will cost you the most time


`seek_eq` is a range scan. Read that docstring twice. The failure mode is silent: with unique test
data, "seek then return the single match" and "seek then scan the matching run" are
indistinguishable. Then you run it on a `city` column and get one row out of 40,000.


### The tests


```python
@pytest.mark.parametrize("rowid", [0, 1, 162, 2**40, 2**63 - 1, -1, -2**63])
def test_index_key_roundtrips_any_rowid(rowid):
    key = encode_index_key(["bb1", "cc1"], rowid)
    values = decode_record(key)
    assert values == ["bb1", "cc1", rowid]




def test_rowid_zero_and_one_cost_no_body_bytes():
    """Proves the rowid is a record field, not a varint (§11.4)."""
    # serial types 8 and 9 encode the constants 0 and 1 in the type itself
    assert len(encode_index_key([], 0)) == len(encode_index_key([], 1))
    assert len(encode_index_key([], 0)) < len(encode_index_key([], 2))




def test_seek_eq_returns_all_duplicates_in_rowid_order(index):
    for rowid in [17, 92, 415, 3]:
        index.insert(["Hanoi"], rowid)
    index.insert(["Hue"], 50)
    assert list(index.seek_eq(["Hanoi"])) == [3, 17, 92, 415]




def test_seek_eq_on_prefix_of_composite_index(index2):   # index on (b, c)
    index2.insert(["x", "1"], 1)
    index2.insert(["x", "2"], 2)
    index2.insert(["y", "1"], 3)
    assert list(index2.seek_eq(["x"])) == [1, 2]




def test_unique_allows_many_nulls(uniq):
    uniq.insert([None], 1)
    uniq.insert([None], 2)          # must not raise
    assert uniq.find_conflict([None]) is None




def test_delete_removes_only_the_named_entry(index):
    for rowid in [17, 92, 415]:
        index.insert(["Hanoi"], rowid)
    assert index.delete(["Hanoi"], 92) is True
    assert list(index.seek_eq(["Hanoi"])) == [17, 415]
    assert index.delete(["Hanoi"], 92) is False




def test_sqlite3_reads_our_index(tmp_path):
    db = connect(tmp_path / "t.db")
    db.execute("CREATE TABLE t (a INTEGER PRIMARY KEY, b TEXT, c TEXT)")
    db.execute("CREATE INDEX ix ON t (b, c)")
    for i in range(200):
        db.execute("INSERT INTO t VALUES (?, ?, ?)", (i, f"b{i:04d}", f"c{i:04d}"))
    db.close()
    # integrity_check walks index trees AND cross-checks them against the table
    out = subprocess.run(["sqlite3", str(tmp_path / "t.db"), "PRAGMA integrity_check;"],
                         capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "ok"
```


`test_sqlite3_reads_our_index` is worth more than it looks. `integrity_check` doesn't merely validate
the index tree's structure — it verifies that **every index entry corresponds to a real row and every
row has an entry in every index**. That's your index-consistency check, written in C by people who
have been finding these bugs since 2000, for free, on every test run.


---


## 24. `catalog/catalog.py` — index registration


Indexes live in `sqlite_schema` alongside tables, using the same five columns:


| type | name | tbl_name | rootpage | sql |
|---|---|---|---|---|
| `'table'` | `'users'` | `'users'` | 2 | `CREATE TABLE users (...)` |
| `'index'` | `'idx_email'` | `'users'` | 4 | `CREATE INDEX idx_email ON users (email)` |


```python
    def create_index(self, name: str, table: str, columns: list[str], *, unique: bool) -> IndexSchema:
        """Allocate a root page, write the sqlite_schema row, bump the schema cookie.


        Raises:
            SchemaError: name already exists, table doesn't, or a column doesn't.


        Note `tbl_name` is the INDEXED table, not the index — that's how you
        find every index on a table with one scan of sqlite_schema.
        """


    def indexes_for(self, table: str) -> list[IndexSchema]:
        """Every index on `table`, in creation order.


        Candidate enumeration is deterministic. Cost is the primary choice;
        index name is only the final tie-breaker for exactly equal costs.
        """
```


Two details worth getting right because they're cheap and they're what a reader checks:


- **`CREATE INDEX` on a non-empty table must backfill.** Scan the table, insert an entry per row. Do
  it before writing the `sqlite_schema` row, so a failure leaves no half-built index visible.
- **Store the original `CREATE INDEX` text verbatim** in `sql`, exactly as you did for tables in week 3.
  SQLite re-parses this on open; so do you.


---


## 25. `catalog/stats.py` and the cost-based planner — NEW


### `ANALYZE`: small statistics with a large effect


Create `quill_stat1(tbl, idx, stat)` lazily on the first `ANALYZE`. Its encoding deliberately mirrors
SQLite's `sqlite_stat1`:


```text
tbl  idx  stat
t    abc  "10000 100 10 2"
```


For the three-column index `(a,b,c)`, this means 10,000 index rows, about 100 rows per distinct `a`,
10 per distinct `(a,b)`, and 2 per distinct `(a,b,c)`. An entry with `idx IS NULL` stores only the
table row count. Statistics are estimates and may become stale after writes; stale statistics may
produce a slower plan but must never change query results.


Compute every prefix count in one ordered index scan. Keep the previous key, count how many distinct
prefixes of length `1..K` occur, then store `ceil(total_rows / distinct_prefixes[n])` for each prefix.
That is O(N×K), uses O(K) counters, and does not need one hash set per column prefix.


```python
@dataclass(frozen=True)
class TableStats:
    row_count: int
    page_count: int
    height: int          # root→leaf pages read for one rowid lookup, leaf INCLUSIVE




@dataclass(frozen=True)
class IndexStats:
    row_count: int
    rows_per_prefix: tuple[int, ...]
    leaf_pages: int
    height: int          # same convention: root→leaf inclusive




def analyze(database: Database, target: str | None = None) -> None:
    """Scan committed tables/indexes and replace their quill_stat1 rows.


    Runs inside one ordinary transaction: DELETE the target's existing rows,
    INSERT the new ones, commit. The undo journal already makes that atomic, so
    there is NO shadow table and no root-page swap — a reader sees either the
    old complete statistics or the new complete statistics. That's what the
    week-5 `analyze_refresh` crash scenario asserts.


    After committing, bump the existing schema cookie so cached plans in every
    connection are invalidated. Reusing the schema cookie over-invalidates
    slightly — connections also re-read the schema they didn't need to — but it
    avoids inventing a second cross-connection versioning protocol for a table
    that changes only when someone types ANALYZE.
    """
```


`height` is on **both** stats objects, and it is the single most load-bearing number in the cost model
below: `IndexStats.height` is paid once per seek, but `TableStats.height` is paid *per fetched row* on a
non-covering index, so it is what makes a low-selectivity index lose. Define it once — pages read from
root to leaf **inclusive**, so a two-level tree has `height == 2` — and use that convention in the
`estimated_leaf_pages` arithmetic too, where it prevents double-counting the first leaf.


If statistics are absent, malformed, or stale, planning still works with documented defaults:


```python
DEFAULT_TABLE_ROWS = 1_000
DEFAULT_EQUALITY_SELECTIVITY = 0.10
DEFAULT_RANGE_SELECTIVITY = 0.25
DEFAULT_RESIDUAL_SELECTIVITY = 0.50
MIN_ESTIMATED_ROWS = 1
```


These constants are deliberately centralized and test-visible. Never reject a query merely because
`ANALYZE` has not run.


`startup` matters for exactly two things and is worth keeping honest about both: it's what makes a
pipeline breaker like `Sort` expensive under `LIMIT` (§18.4 — a top-K heap has a low startup, a full sort
does not), and it's the tie-breaker in `choose_access_path` when two plans have equal `total`. For a
`SeqScan` and an `IndexScan` — neither of which blocks — `startup` is just the cost of getting positioned:
0 for a scan, the seek cost for an index.


### Candidate generation is separate from choosing


```python
@dataclass(frozen=True)
class PlanCost:
    startup: float      # cost paid before this operator can emit its FIRST row
    total: float        # cost to run it to completion




@dataclass(frozen=True)
class AccessPath:
    kind: Literal["seq_scan", "index_scan"]
    index: IndexSchema | None
    seek_terms: list[Predicate]     # the index-prefix terms this path will seek on
    residual: list[Predicate]       # everything else — becomes a Filter above
    rows_fetched: int               # rows read from the tree, BEFORE residual filtering
    est_rows: int                   # rows emitted, AFTER residual filtering
    cost: PlanCost
    covering: bool
    output_order: tuple[str, ...]    # index columns in physical ascending order




def is_sargable(pred: Predicate, column: str) -> bool:
    """Can `pred` drive a seek on `column`?


    Requires the column BARE on one side of a comparison (chapter 12 §12.2):
    `col = expr`, `col > expr`, `expr <= col`, `col IS NULL`, `col IN (...)`.


    Explicitly NOT sargable, and each is a test case:
      - lower(col) = 'x'    the index is sorted by col, not lower(col)
      - col + 0 = 5         same reason
      - a = 1 OR b = 2      dropping an OR branch loses rows
    """




def enumerate_access_paths(table: TableSchema, indexes: list[IndexSchema],
                           predicates: list[Predicate], required_columns: set[str],
                           stats: StatisticsCatalog) -> list[AccessPath]:
    """Return SeqScan plus every legal normal IndexScan candidate.


    For each index, walk its columns LEFT TO RIGHT (chapter 12 §12.3):
      - consume sargable EQUALITY terms while they're available;
      - at the first column with only inequalities, consume up to two of them
        and STOP;
      - at the first column with no usable term, STOP (no gaps).
    Everything unconsumed goes to `residual`. Index legality is a rule; it does
    not mean the index wins. Always emit SeqScan because a low-selectivity index
    can cost more than reading the table once.
    """




def choose_access_path(candidates: list[AccessPath]) -> AccessPath:
    """Choose minimum total cost; then startup cost; then stable plan identity."""
```


This separation is load-bearing. **Rules generate legal candidates; estimates and costs choose among
them.** A planner that returns the first legal index is still rule-based even if it prints a made-up
`cost` field.


### Cardinality estimation


Use prefix statistics when they match the seek terms:


```text
index (a,b,c), stat "10000 100 10 2"
a=?                 → rows_fetched ≈ 100
a=? AND b=?         → rows_fetched ≈ 10
a=? AND b=? AND c=? → rows_fetched ≈ 2
```


For `IN`, multiply by the number of distinct constants and cap at the input size. For a range, apply
the documented range fraction to the rows remaining after equality-prefix terms. Residual predicates
may lower `est_rows` seen by parent operators, but they do **not** lower `rows_fetched`: the scan still
reads those candidates before filtering them.


SQLite's `stat1` averages cannot describe skew or correlation. Preserve that limitation rather than
pretending the estimate is exact; chapter 12 includes the failure case.


### A small, explicit cost model


Keep every constant in `plan/cost.py`, with cost measured in approximate page-read equivalents:


```python
CPU_PER_ROW = 0.01
SEQ_PAGE_COST = 1.0          # a page reached by walking forward
RANDOM_PAGE_COST = 4.0       # a page reached by a fresh root-to-leaf descent


seq_total = SEQ_PAGE_COST * table_pages + CPU_PER_ROW * table_rows


rows_per_leaf = max(1, index_rows / index_leaf_pages)
rows_fetched = max(MIN_ESTIMATED_ROWS, rows_fetched)          # never 0 — see below
estimated_leaf_pages = ceil(rows_fetched / rows_per_leaf)


# height is root→leaf INCLUSIVE, so the descent already paid for one leaf;
# only the leaves after the first are additional, and they're sequential.
seek_cost = (
    RANDOM_PAGE_COST * index_height
    + SEQ_PAGE_COST * max(0, estimated_leaf_pages - 1)
)
table_lookup_cost = RANDOM_PAGE_COST * table_height           # PER FETCHED ROW


index_total = (
    seek_cost
    + (0 if covering else rows_fetched * table_lookup_cost)
    + CPU_PER_ROW * rows_fetched
)
```


Three things in there are deliberate and each is a place the model would otherwise lie:


- **`RANDOM_PAGE_COST > SEQ_PAGE_COST`, or the constant is decorative.** If a random seek and a
  sequential step both cost 1.0, the model cannot express the one distinction it exists to express, and
  calling it "page-oriented" overstates it. 4.0 is the conventional starting ratio (it's PostgreSQL's
  default `random_page_cost`); the exact value is yours to calibrate, but it must be greater than 1.
- **`height` is leaf-inclusive, so don't charge for the first leaf twice.** Adding
  `index_height + estimated_leaf_pages` counts the leaf you already descended to. It's a rounding-level
  error on a big range scan and a ~30% error on the single-row lookup that is your headline benchmark.
- **Clamp `rows_fetched` to at least 1.** `MIN_ESTIMATED_ROWS` is not only about `est_rows`: if
  `rows_fetched` reaches 0, `estimated_leaf_pages` is 0 and an index that matches nothing looks *free*,
  which makes it beat every real plan. A zero-row estimate is a prediction, not a promise, and the plan
  still has to descend the tree to discover it.


Worked, for the README benchmark — 100k rows, `table_pages=2417`, `table_height=3`, a unique index on
`email` with `index_height=3` and ~200 rows per leaf:


```text
seq_total   = 1.0 × 2417 + 0.01 × 100000        = 3417.00
index_total = 4.0 × 3 + 1.0 × max(0, 1-1)                      # seek  = 12.00
            + 1 × (4.0 × 3)                                    # fetch = 12.00
            + 0.01 × 1                                         # cpu   =  0.01
            =                                                    24.01
```


So `EXPLAIN` prints `cost=24.01` against a scan's `3417.00` — a 142× predicted advantage, next to a
*measured* ~4 pages versus ~2,417. The estimate and the measurement are different quantities and are
allowed to disagree; that's exactly why `EXPLAIN` and `EXPLAIN ANALYZE` print them separately.


This intentionally captures the important facts, not every device detail: a seek pays tree height;
matching entries occupy leaf pages; a non-covering index pays a second table lookup per candidate;
and a covering index avoids that lookup. Calibrate constants with page counters, but never tune them
to make one fixture pass.


### The invariant that makes this safe


> `seek_terms` ∪ `residual` == the original predicate list, always. Every term is either seeked on or
> filtered, and no term is both.


Assert it. A dropped residual returns **wrong rows**, and only on data where the seek prefix isn't
already selective enough to hide it — which your small fixtures probably are.


### The tests


```python
# expected_seek is the seek prefix an IndexScan candidate must have.
# expected_seek == [] means NO IndexScan candidate may be generated at all.
@pytest.mark.parametrize("sql,expected_seek", [
    ("WHERE a = 1",                     ["a"]),
    ("WHERE a = 1 AND b = 2",           ["a", "b"]),
    ("WHERE a = 1 AND b = 2 AND c = 3", ["a", "b", "c"]),
    ("WHERE a IN (1,2,3) AND b = 2",    ["a", "b"]),
    ("WHERE a IS NULL AND b = 2",       ["a", "b"]),
    ("WHERE a = 1 AND b > 2 AND b < 5", ["a", "b"]),
    ("WHERE a = 1 AND b > 2 AND c = 3", ["a", "b"]),   # c dropped: right of an inequality
    ("WHERE a = 1 AND c = 3",           ["a"]),        # c dropped: gap at b
    ("WHERE c = 3",                     []),          # no usable prefix
    ("WHERE b = 2",                     []),          # no skip-scan in quilldb
    ("WHERE lower(a) = 'x'",            []),          # not sargable
    ("WHERE a = 1 OR b = 2",            []),          # OR
])
def test_leading_column_rule_controls_candidates(index_abc, sql, expected_seek):
    candidates = candidates_for(index_abc, sql)
    index_paths = [p for p in candidates if p.kind == "index_scan"]


    # SeqScan is ALWAYS a candidate, in every one of these 12 cases.
    assert any(p.kind == "seq_scan" for p in candidates)


    if not expected_seek:
        # The four ineligible cases. This is the assertion that matters: it is
        # not enough that SeqScan exists, no index seek may be OFFERED.
        assert index_paths == [], f"illegal seek generated for {sql}"
    else:
        assert [[t.column for t in p.seek_terms] for p in index_paths] == [expected_seek]




def test_every_predicate_is_either_seeked_or_residual(index_abc):
    preds = parse_where("WHERE a = 1 AND c = 3 AND d = 9")
    for path in enumerate_access_paths(TABLE, [index_abc], preds, REQUIRED, STATS):
        assert set(path.seek_terms) | set(path.residual) == set(preds)
        assert not (set(path.seek_terms) & set(path.residual))




def test_statistics_choose_the_selective_index(db):
    # x has 2 distinct values (~5000 rows/value); y has 1000 (~10/value).
    db.execute("ANALYZE")
    plan = db.execute("EXPLAIN SELECT * FROM t WHERE x=1 AND y=5").text
    assert "y_idx" in plan




def test_changing_only_statistics_flips_the_plan(db):
    original = explain(db, "SELECT * FROM t WHERE x=1 AND y=5")
    replace_stat(db, "x_idx", "10000 1")
    replace_stat(db, "y_idx", "10000 9000")
    invalidate_plans(db)
    changed = explain(db, "SELECT * FROM t WHERE x=1 AND y=5")
    assert index_used(original) != index_used(changed)




def test_low_selectivity_index_can_lose_to_seq_scan(db):
    db.execute("ANALYZE")
    assert plan_kind(db, "SELECT * FROM t WHERE boolean_col=1") == "seq_scan"
```


The parametrized table is lifted directly from real `EXPLAIN QUERY PLAN` output against SQLite 3.37
(chapter 12 §12.3). Note the `if not expected_seek` branch: an earlier version of this test asserted only
that a *matching* candidate existed, which the four ineligible rows pass **trivially** — SeqScan is always
emitted, so `("WHERE c = 3", "seq_scan", [])` stays green even if the planner also offers an illegal
`IndexScan(abc, c=3)`. Legality tests have to assert what is *absent*; that's the point chapter 12 §12.6
makes in trap 2. The three cost tests then prove the choice is genuinely cost-based. Skip-scan remains
deferred; ordinary access-path choice does not depend on it.


---


## 26. `plan/explain.py` and 27. `exec/operators.py` additions


```python
def format_plan(op: Operator) -> str:
    """Render the operator tree with box-drawing characters.


    Project  [id, email, age]
    └─ IndexScan  idx_email  (email = ?)   est_rows=1  startup=12.00  cost=24.01
    """
```


Plain `EXPLAIN` must not execute the query: it prints `est_rows`, startup cost, and total cost.
`EXPLAIN ANALYZE` executes the selected tree once and adds `actual_rows` and the buffer pool's measured
`pages_read`. Never label an estimate as an actual counter.


```python
class IndexScan(Operator):
    """Seek an index, then fetch each row from the table tree by rowid.


    Two cursors, and the second one is the "double lookup" of §11.5. If every
    column the query needs is in the index, skip it — that's a covering index,
    and printing `IndexScan (covering)` in EXPLAIN is a nice touch that costs
    one line.
    """




class DeleteOp(Operator):
    """Delete rows matching a predicate.


    ⚠️ COLLECT ROWIDS FIRST, THEN DELETE. Deleting while iterating a cursor
    over the same tree mutates the structure under the cursor — you'll skip
    rows or revisit them. Materialize the rowid list, then loop.


    For each rowid: delete from every index first, then the table. That order
    matters — you need the row's column values to build the index keys, so
    read them before the row is gone.
    """




class UpdateOp(Operator):
    """Update rows matching a predicate.


    Same collect-first rule. Per row:
      1. read the old values
      2. compute the new values
      3. check every UNIQUE index for conflicts — raise BEFORE any write
      4. delete old index entries, write the row, insert new index entries
    """
```


That `DeleteOp` warning is the bug you are most likely to write this week. Mutating a tree while a
cursor walks it is undefined behaviour in your own code, and the symptom — a handful of rows
mysteriously surviving a `DELETE` — looks like a B-tree bug rather than an iteration bug.


---


## The two tests that matter more than the rest


Everything above has unit tests. These two are *property* tests, and between them they catch almost
every week-4 bug that unit tests miss.


```python
@given(ops=lists(mutation_ops(), min_size=1, max_size=200))
def test_index_always_agrees_with_the_table(tmp_path, ops):
    """Derived data must match its source. The whole reason week 5 exists."""
    db = fresh_db_with_index(tmp_path)
    for op in ops:
        apply(db, op)
        # every rowid reachable through the index == every rowid in the table
        via_index = sorted(db.index("ix").scan_rowids())
        via_table = sorted(rowid for rowid, _ in db.table("t").scan())
        assert via_index == via_table
        # and each index entry's key actually matches its row's columns
        for key, rowid in db.index("ix").scan():
            assert key == columns_of(db.table("t").get(rowid), ["b", "c"])




@given(query=select_queries())
def test_an_index_never_changes_results(tmp_path, query):
    """A planner is an optimization. An optimization that changes answers is a
    bug with a nice name."""
    without = run_on_fresh_db(tmp_path / "a.db", query, indexes=[])
    with_ix  = run_on_fresh_db(tmp_path / "b.db", query, indexes=["CREATE INDEX ix ON t (b, c)"])
    assert without == with_ix
```


The first is the roadmap's index-vs-scan consistency check. **Reuse it verbatim inside the week-5 crash
loop** — "indexes and tables never disagree after any crash point" is one of week 5's success criteria,
and it's this same assertion called after recovery.


The second one deserves a moment. It's easy to write a planner that's fast and wrong, because a wrong
plan usually returns *a* plausible answer. Running everything twice and diffing is the only cheap way
to know. It's also the test that catches a dropped `residual` term, which is otherwise invisible.


---


## The benchmark (this number goes in the README)


```python
# benchmarks/index_lookup.py
def main() -> None:
    """Same query, same data, with and without an index. Report PAGE READS."""
    for use_index in (False, True):
        db = build(rows=100_000, with_index=use_index)
        db.reset_counters()
        db.execute("SELECT * FROM users WHERE email = ?", ("u50000@x.com",)).fetchall()
        print(f"index={use_index}: {db.pages_read} page reads, {db.rows_examined} rows examined")
```


Report **page reads**, not seconds. Chapter 19 makes the full argument, but the short version: page
reads are what the algorithm determines, they're deterministic, they don't depend on your laptop's
thermal state, and nobody can wonder whether you benchmarked a warm cache. The roadmap's target shape
is ~4 reads against ~2,417 — a number an interviewer can check the arithmetic on (chapter 19 §19.5 shows
the full result statement, including the conditions that make it falsifiable).


Also record the *write* side honestly: how much slower is `INSERT` with three indexes than with none?
Every index is a write-amplification tax, and volunteering that number is more persuasive than the
lookup speedup alone.


---


## Week 4 sessions


| # | 2 hours on | Done when |
|---|---|---|
| 1 | `DELETE` / `UPDATE` / `CREATE INDEX` / `ANALYZE` / both `EXPLAIN` forms in parser | all forms parse; unsupported forms raise typed errors |
| 2 | `BTree.delete`, empty-page freeing, root collapse | delete-2000-in-random-order is green, validator clean each step |
| 3 | `index.py`: `encode_index_key`, `compare_keys`, `insert`, `seek_eq` | duplicates test passes; `sqlite3 integrity_check` is `ok` |
| 4 | `IndexBTree.delete`, `find_conflict`, `catalog.create_index` with backfill | `CREATE INDEX` on a populated table produces a complete index |
| 5 | Index maintenance in `InsertOp` / `DeleteOp` / `UpdateOp` | the index-vs-table property test is green |
| 6 | Candidate generation, `is_sargable`, `IndexScan` | the 12-row legality table and residual invariant are green |
| 7 | `ANALYZE`, `quill_stat1`, cardinality estimates | known distributions produce expected prefix estimates |
| 8 | Cost model and access-path selection | selective-index, stats-flip, seq-scan-wins, covering-index tests are green |
| 9 | `EXPLAIN` / `EXPLAIN ANALYZE`, benchmark, results-unchanged property | estimates and actual counters are distinct; README number is reproducible |


Sessions 5 and 8 are the ones most likely to run long. Do not collapse candidate generation and
costing into one function to save time; that would make the implementation look cost-based while
remaining impossible to test as a cost-based pipeline. Move the presentation polish, not the
correctness and plan-choice tests.


---


## Week 4 definition of done


- [ ] `DELETE` and `UPDATE` work, including no-`WHERE` (all rows) and multi-column `SET`
- [ ] Deleting every row in random order leaves a valid tree; the validator is clean throughout
- [ ] The root reverts from interior to leaf when the tree empties, **at the same page number**
- [ ] Delete-then-reinsert the same volume leaves `page_count` unchanged — the freelist is *consumed*
- [ ] Freed pages are fully zeroed (the week-1 `freelist leaf count too big` trap)
- [ ] `CREATE INDEX` and `CREATE UNIQUE INDEX` work, including backfill on a populated table
- [ ] Index entries are records ending in the rowid; rowids 0 and 1 cost zero body bytes
- [ ] `seek_eq` returns **all** duplicates, and a test with duplicate keys proves it
- [ ] A `UNIQUE` violation raises `UniqueViolationError` **before any page is written**
- [ ] A `UNIQUE` index accepts multiple NULLs
- [ ] Indexes stay consistent through insert / update / delete — property test, ≥200 random ops
- [ ] Adding an index never changes query results — property test over generated queries
- [ ] The 12-case table verifies which index candidates are legal, including all four ineligible cases
- [ ] `seek_terms ∪ residual` equals the input predicates, asserted
- [ ] `ANALYZE` persists K+1 prefix statistics and missing statistics use documented defaults
- [ ] Statistics choose `y_idx` (~10 rows) over `x_idx` (~5,000 rows) for the two-index fixture
- [ ] Changing only statistics flips the chosen index; query results remain identical
- [ ] A low-selectivity applicable index can lose to SeqScan; a covering index gets the cheaper cost
- [ ] `EXPLAIN` prints estimates without execution; `EXPLAIN ANALYZE` prints measured rows/page reads
- [ ] `sqlite3 f.db "PRAGMA integrity_check"` is `ok` after an index-heavy *and* a delete-heavy workload
- [ ] Benchmark in the README: ~4 page reads indexed vs ~2,417 scanned, plus the insert-side cost
- [ ] `NOTES.md` has an entry for every bug that took over 20 minutes


**The five bullets to protect if the week runs short:** the two property tests, the statistics-flip
test, `integrity_check` on a delete-heavy database, and the benchmark. Those are the claims that make
the cost-based planner and storage engine independently verifiable.