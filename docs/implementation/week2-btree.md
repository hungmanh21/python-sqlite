# quilldb — Implementation Plan: Week 2, The B+Tree


← [Index](README.md)  ·  Prev: [Week 1](week-1-storage.md)  ·  Next: [Week 3 — SQL](week-3-sql.md)


---


# Week 2 Spec


Same shape, less hand-holding — by now you'll have the rhythm.


| # | File | What it does |
|---|---|---|
| 1 | `btree/cells.py` | Encode/decode leaf and interior cells |
| 2 | `btree/btree.py` | search, insert, split |
| 3 | `btree/cursor.py` | Ordered traversal |
| 4 | `btree/overflow.py` | Payloads larger than a page |
| 5 | `btree/validate.py` | Structural invariant checker |


### `btree/cells.py`


```python
"""Cell formats.


Leaf cell:      [key svarint][payload_len uvarint][payload bytes]
Interior cell:  [child_page u32][key svarint]


Interior cells hold no payload — an interior page is pure navigation. The key is
a separator: "everything in child_page is < key".
"""


from dataclasses import dataclass




@dataclass(frozen=True)
class LeafCell:
    key: int
    payload: bytes


    def encode(self) -> bytes: ...
    @classmethod
    def decode(cls, data: bytes | memoryview, offset: int = 0) -> "LeafCell": ...




@dataclass(frozen=True)
class InteriorCell:
    child_page: int
    key: int


    def encode(self) -> bytes: ...
    @classmethod
    def decode(cls, data: bytes | memoryview, offset: int = 0) -> "InteriorCell": ...
```


### `btree/btree.py`


```python
class BTree:
    def __init__(self, pool: BufferPool, root_page_id: int) -> None: ...


    @classmethod
    def create(cls, pool: BufferPool) -> "BTree":
        """Allocate an empty leaf and return a tree rooted there."""


    def search(self, key: int) -> bytes | None:
        """Point lookup. Returns payload, or None if absent."""


    def insert(self, key: int, payload: bytes) -> None:
        """Insert or replace. Splits pages as needed; may increase tree height."""


    def delete(self, key: int) -> bool:
        """Returns True if a row was removed. Week 4 — stub it now."""


    # --- internals you'll need ---


    def _find_leaf(self, key: int) -> list[tuple[int, int]]:
        """Descend from root to the leaf that would hold `key`.


        Returns:
            The path as [(page_id, cell_index), ...], root first, leaf last.
            The cursor needs this to walk back up and across.
        """


    def _split_leaf(self, page_id: int, path: list[tuple[int, int]]) -> None:
        """Split a full leaf, then insert a separator into the parent.


        If the parent is also full, this recurses. If the ROOT splits, allocate
        a new root and the tree gets one level taller.
        """
```


**Two hints that will save you an evening.** Do `_find_leaf` and `search` *before* `insert`, and
test them against a hand-built fixture tree — don't wait for insert to work. And write
`dump_tree()` in `validate.py` before you attempt splits; you will need it.


### `btree/validate.py`


```python
@dataclass
class ValidationReport:
    errors: list[str]


    @property
    def is_valid(self) -> bool: ...
    def raise_if_invalid(self) -> None:
        """Raises BTreeInvariantError listing every problem found."""




def validate_btree(pool: BufferPool, root_page_id: int) -> ValidationReport:
    """Walk the whole tree and check every invariant:


      - keys strictly increasing within each page
      - every key on a child page inside the range its parent's separators claim
      - every page reachable exactly once (no shared pages, no orphans)
      - all leaves at identical depth
      - every cell offset inside its page
      - no overflow chain cycles
    """




def dump_tree(pool: BufferPool, root_page_id: int, out=sys.stdout) -> None:
    """Print the tree as indented text. Write this FIRST, before splits."""
```


### Week 2 tests


Test file names and the cases that matter:


**`test_cells.py`** — round-trip both cell types; negative keys; empty payload; Hypothesis
round-trip; truncated cell raises `MalformedCellError`.


**`test_btree_search.py`** — build a 2-level tree by hand in a fixture. Find every present key; miss
correctly on absent keys; miss on an empty tree; find keys at both extremes.


**`test_btree_insert.py`** — insert into empty; at start, middle, end; replacing an existing key;
fill a page exactly; then the split cases:


```python
def test_leaf_split_preserves_all_keys(pool) -> None:
    tree = BTree.create(pool)
    keys = list(range(200))                          # forces at least one split
    for k in keys:
        tree.insert(k, f"row{k}".encode())
    for k in keys:
        assert tree.search(k) is not None
    validate_btree(pool, tree.root_page_id).raise_if_invalid()




def test_root_split_increases_height(pool) -> None:
    tree = BTree.create(pool)
    for k in range(5000):
        tree.insert(k, b"x" * 200)
    assert tree_height(pool, tree.root_page_id) >= 3
    validate_btree(pool, tree.root_page_id).raise_if_invalid()




@pytest.mark.parametrize("order", ["ascending", "descending", "random"])
def test_insertion_order_does_not_matter(pool, order: str) -> None:
    """Ascending is the easy path; descending and random find different bugs."""
    keys = list(range(1000))
    if order == "descending":
        keys.reverse()
    elif order == "random":
        random.Random(42).shuffle(keys)              # seeded: failures reproduce
    tree = BTree.create(pool)
    for k in keys:
        tree.insert(k, b"v")
    assert list(TableCursor(pool, tree.root_page_id).scan_keys()) == sorted(keys)
    validate_btree(pool, tree.root_page_id).raise_if_invalid()
```


**`test_btree_stress.py`** — the one that proves it:


```python
@given(st.lists(st.integers(0, 10_000), min_size=1, max_size=2000))
@settings(deadline=None, max_examples=25)
def test_btree_matches_dict_oracle(tmp_path_factory, keys: list[int]) -> None:
    """Compare against a Python dict — your oracle for weeks 2-4."""
    pager = Pager.create(tmp_path_factory.mktemp("d") / "t.db")
    pool = BufferPool(pager, capacity=32)
    tree = BTree.create(pool)
    oracle: dict[int, bytes] = {}


    for k in keys:
        payload = f"v{k}".encode()
        tree.insert(k, payload)
        oracle[k] = payload


    for k, expected in oracle.items():
        assert tree.search(k) == expected
    assert list(TableCursor(pool, tree.root_page_id).scan_keys()) == sorted(oracle)
    validate_btree(pool, tree.root_page_id).raise_if_invalid()
    pager.close()
```


**`test_overflow.py`** — a 10KB payload round-trips; boundary sizes (exactly fits / one byte over);
a chain pointing to itself raises `OverflowCycleError`.


**`test_validate.py`** — deliberately corrupt a valid tree and confirm each invariant fires:
swap two keys on a page (ordering), point two parents at one child (shared page), truncate a child
pointer (unreachable), make one leaf deeper (height).


---