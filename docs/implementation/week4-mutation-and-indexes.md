# quilldb — Implementation Plan: Week 4, Mutation and Indexes


← [Index](README.md)  ·  Prev: [Week 3 — SQL](week-3-sql.md)  ·  Next: [Week 5 — Transactions](week-5-transactions.md)


---


# Week 4 Spec — Deletion, Indexes, and a Planner


> **Read first:** [chapter 10](../theory/btree/10-deletion-and-space-reuse.md) before the delete
> session, [chapter 11](../theory/btree/11-index-b-trees.md) before the index sessions, and
> [chapter 12](../theory/plan/12-the-query-planner.md) before the planner. Each is ~40 minutes and
> each answers a design question this spec assumes you've already settled.


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
EXPLAIN SELECT * FROM users WHERE email = ?;
```


Supported:


- `DELETE FROM t [WHERE ...]` — including no `WHERE`, meaning all rows
- `UPDATE t SET col = expr [, col = expr]* [WHERE ...]`
- `CREATE [UNIQUE] INDEX name ON table (col [, col]*)`
- `EXPLAIN <select>` returning the operator tree as text
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
│   └── catalog.py       + create_index(), indexes_for()
├── plan/                NEW
│   ├── planner.py       AccessPath, choose_access_path
│   └── explain.py       format_plan()
├── sql/
│   ├── ast.py           + Delete, Update, CreateIndex, Explain
│   └── parser.py        + the three statements
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
        """Every index on `table`, in creation order. Deterministic order
        matters: the planner breaks ties by it, and EXPLAIN output is asserted
        in tests.
        """
```


Two details worth getting right because they're cheap and they're what a reader checks:


- **`CREATE INDEX` on a non-empty table must backfill.** Scan the table, insert an entry per row. Do
  it before writing the `sqlite_schema` row, so a failure leaves no half-built index visible.
- **Store the original `CREATE INDEX` text verbatim** in `sql`, exactly as you did for tables in week 3.
  SQLite re-parses this on open; so do you.


---


## 25. `plan/planner.py` — NEW


### The stub


```python
@dataclass(frozen=True)
class AccessPath:
    kind: Literal["seq_scan", "index_scan"]
    index: IndexSchema | None
    seek_terms: list[Predicate]     # the index-prefix terms this path will seek on
    residual: list[Predicate]       # everything else — becomes a Filter above
    est_rows: int




def is_sargable(pred: Predicate, column: str) -> bool:
    """Can `pred` drive a seek on `column`?


    Requires the column BARE on one side of a comparison (chapter 12 §12.2):
    `col = expr`, `col > expr`, `expr <= col`, `col IS NULL`, `col IN (...)`.


    Explicitly NOT sargable, and each is a test case:
      - lower(col) = 'x'    the index is sorted by col, not lower(col)
      - col + 0 = 5         same reason
      - a = 1 OR b = 2      dropping an OR branch loses rows
    """




def choose_access_path(table: TableSchema, indexes: list[IndexSchema],
                       predicates: list[Predicate]) -> AccessPath:
    """Pick how to read `table`.


    For each index, walk its columns LEFT TO RIGHT (chapter 12 §12.3):
      - consume sargable EQUALITY terms while they're available;
      - at the first column with only inequalities, consume up to two of them
        and STOP;
      - at the first column with no usable term, STOP (no gaps).
    Everything unconsumed goes to `residual`.


    Score by number of columns consumed; longest wins. Break ties by index
    NAME so EXPLAIN output is deterministic and tests don't flake. If nothing
    consumes at least one column, return a seq_scan with all predicates as
    residual.
    """
```


### The invariant that makes this safe


> `seek_terms` ∪ `residual` == the original predicate list, always. Every term is either seeked on or
> filtered, and no term is both.


Assert it. A dropped residual returns **wrong rows**, and only on data where the seek prefix isn't
already selective enough to hide it — which your small fixtures probably are.


### The tests


```python
@pytest.mark.parametrize("sql,expected_kind,expected_seek", [
    ("WHERE a = 1",                   "index_scan", ["a"]),
    ("WHERE a = 1 AND b = 2",         "index_scan", ["a", "b"]),
    ("WHERE a = 1 AND b = 2 AND c = 3", "index_scan", ["a", "b", "c"]),
    ("WHERE a IN (1,2,3) AND b = 2",  "index_scan", ["a", "b"]),
    ("WHERE a IS NULL AND b = 2",     "index_scan", ["a", "b"]),
    ("WHERE a = 1 AND b > 2 AND b < 5", "index_scan", ["a", "b"]),
    ("WHERE a = 1 AND b > 2 AND c = 3", "index_scan", ["a", "b"]),   # c dropped: right of an inequality
    ("WHERE a = 1 AND c = 3",         "index_scan", ["a"]),          # c dropped: gap at b
    ("WHERE c = 3",                   "seq_scan",   []),             # no usable prefix
    ("WHERE b = 2",                   "seq_scan",   []),             # no skip-scan in quilldb
    ("WHERE lower(a) = 'x'",          "seq_scan",   []),             # not sargable
    ("WHERE a = 1 OR b = 2",          "seq_scan",   []),             # OR
])
def test_leading_column_rule(index_abc, sql, expected_kind, expected_seek):
    path = plan_for(index_abc, sql)
    assert path.kind == expected_kind
    assert [t.column for t in path.seek_terms] == expected_seek




def test_every_predicate_is_either_seeked_or_residual(index_abc):
    preds = parse_where("WHERE a = 1 AND c = 3 AND d = 9")
    path = choose_access_path(TABLE, [index_abc], preds)
    assert set(path.seek_terms) | set(path.residual) == set(preds)
    assert not (set(path.seek_terms) & set(path.residual))
```


That parametrized table is lifted directly from real `EXPLAIN QUERY PLAN` output against SQLite 3.37
(chapter 12 §12.3), so it isn't a guess about what the rules should be — it's what they *are*, minus
the skip-scan rows, which quilldb deliberately doesn't implement.


---


## 26. `plan/explain.py` and 27. `exec/operators.py` additions


```python
def format_plan(op: Operator) -> str:
    """Render the operator tree with box-drawing characters.


    Project  [id, email, age]
    └─ IndexScan  idx_email  (email = ?)   est_rows=1  pages_read=3
    """
```


**`pages_read` must come from the buffer pool's real counter, not an estimate.** A measured number is
evidence; an estimate is a claim, and this string is destined for the README.


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
is ~3 reads against ~2,400 — a number an interviewer can check the arithmetic on.


Also record the *write* side honestly: how much slower is `INSERT` with three indexes than with none?
Every index is a write-amplification tax, and volunteering that number is more persuasive than the
lookup speedup alone.


---


## Week 4 sessions


| # | 2 hours on | Done when |
|---|---|---|
| 1 | `DELETE` / `UPDATE` / `CREATE INDEX` / `EXPLAIN` in `ast.py` + `parser.py` | all four parse; unsupported forms raise typed errors |
| 2 | `BTree.delete`, empty-page freeing, root collapse | delete-2000-in-random-order is green, validator clean each step |
| 3 | `index.py`: `encode_index_key`, `compare_keys`, `insert`, `seek_eq` | duplicates test passes; `sqlite3 integrity_check` is `ok` |
| 4 | `IndexBTree.delete`, `find_conflict`, `catalog.create_index` with backfill | `CREATE INDEX` on a populated table produces a complete index |
| 5 | Index maintenance in `InsertOp` / `DeleteOp` / `UpdateOp` | the index-vs-table property test is green |
| 6 | `planner.py`, `is_sargable`, `IndexScan` operator | the 12-row parametrized planner table is green |
| 7 | `EXPLAIN` formatting, the benchmark, the results-unchanged property test | the README number exists and is reproducible |


Session 5 is the one that runs long. If it does, take the time out of session 7's benchmark — the
consistency property is load-bearing for week 5, and the benchmark can be produced in twenty minutes
once everything works.


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
- [ ] The 12-case planner table matches, including all four `seq_scan` fallbacks
- [ ] `seek_terms ∪ residual` equals the input predicates, asserted
- [ ] `EXPLAIN` prints the tree with **measured** `pages_read`
- [ ] `sqlite3 f.db "PRAGMA integrity_check"` is `ok` after an index-heavy *and* a delete-heavy workload
- [ ] Benchmark in the README: ~3 page reads indexed vs ~2,400 scanned, plus the insert-side cost
- [ ] `NOTES.md` has an entry for every bug that took over 20 minutes


**The four bullets to protect if the week runs short:** the two property tests, `integrity_check` on a
delete-heavy database, and the benchmark. Those are the ones that appear in the README or make week 5
possible. Everything else is recoverable later.