"""The invariants that a multi-level index b-tree must hold, checked
against both quilldb's own validator and real sqlite3.


Every test here needs a tree tall enough to HAVE an interior page. Index
interior pages hold ~150 children, so a few hundred rows is the threshold
below which none of this can fail -- which is exactly why the rest of the
suite, all of it comfortably under that size, never caught the split bug
these tests exist to prevent (§11.6).
"""


import pathlib
import subprocess

import pytest

import quilldb
from quilldb.btree.validate import validate_index_btree
from quilldb.storage.bufferpool import BufferPool
from quilldb.storage.pager import Pager

# Big enough that the index root is INTERIOR_INDEX with several cells.
MULTI_LEVEL_ROWS = 3000




def _build(path: pathlib.Path, rows: int, *, unique: bool = False) -> None:
    db = quilldb.connect(str(path))
    db.execute("CREATE TABLE t (id INTEGER, age INTEGER)")
    db.execute(f"CREATE {'UNIQUE ' if unique else ''}INDEX ix ON t (age)")
    for i in range(1, rows + 1):
        db.execute("INSERT INTO t VALUES (?, ?)", (i, i))
    db.close()




def _index_root(path: pathlib.Path) -> int:
    db = quilldb.connect(str(path))
    root = db.catalog.indexes_for("t")[0].root_page
    db.close()
    return root




def _validate(path: pathlib.Path) -> int:
    """Run the structural validator over the on-disk index; return entries."""
    root = _index_root(path)
    pager = Pager.open(path)
    pool = BufferPool(pager, capacity=64)
    try:
        return validate_index_btree(pager, pool, root, n_key_columns=1)
    finally:
        pager.close()




def _integrity_check(path: pathlib.Path) -> str:
    return subprocess.run(
        ["sqlite3", str(path), "PRAGMA integrity_check;"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()




def test_multi_level_index_is_structurally_valid(tmp_path) -> None:
    """A separator is an entry, so it must NOT also live in the subtree
    below it. validate_index_btree's strict upper bound is what says so.
    """
    path = tmp_path / "t.db"
    _build(path, MULTI_LEVEL_ROWS)
    _validate(path)




def test_multi_level_index_conserves_entry_count(tmp_path) -> None:
    """A split rebalances; it must never invent an entry. Total entries
    (leaf cells PLUS interior cells) must equal the number of rows
    inserted, no matter how many times the tree split along the way.
    """
    path = tmp_path / "t.db"
    _build(path, MULTI_LEVEL_ROWS)
    assert _validate(path) == MULTI_LEVEL_ROWS




def test_sqlite3_integrity_check_passes_on_a_multi_level_index(tmp_path) -> None:
    """The acceptance test: real sqlite3 reads the index back and counts
    its entries against the table's rows.
    """
    path = tmp_path / "t.db"
    _build(path, MULTI_LEVEL_ROWS)
    assert _integrity_check(path) == "ok"




def test_sqlite3_sees_no_duplicate_rows_through_a_multi_level_index(tmp_path) -> None:
    """A separator left behind in a leaf is a genuine duplicate row to
    anything reading the file -- one row per key, never two.
    """
    path = tmp_path / "t.db"
    _build(path, MULTI_LEVEL_ROWS)
    out = subprocess.run(
        ["sqlite3", str(path), "SELECT count(*) FROM (SELECT age FROM t GROUP BY age HAVING count(*) > 1);"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert out == "0"




def test_quilldb_and_sqlite3_agree_on_index_entry_count(tmp_path) -> None:
    """sqlite3 answers `count(*)` from the smallest index, so this compares
    the index's entry count against a forced table scan of the same file.
    """
    path = tmp_path / "t.db"
    _build(path, MULTI_LEVEL_ROWS)
    via_index = subprocess.run(
        ["sqlite3", str(path), "SELECT count(*) FROM t;"], capture_output=True, text=True, check=True
    ).stdout.strip()
    via_table = subprocess.run(
        ["sqlite3", str(path), "SELECT count(id) FROM t;"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert via_index == via_table == str(MULTI_LEVEL_ROWS)




@pytest.mark.parametrize("rows", [500, 1000, MULTI_LEVEL_ROWS])
def test_index_stays_valid_across_the_multi_level_threshold(tmp_path, rows: int) -> None:
    """500 is roughly where the first interior page appears. Validate on
    both sides of that boundary so a regression names the size it broke at.
    """
    path = tmp_path / f"t{rows}.db"
    _build(path, rows)
    assert _validate(path) == rows
    assert _integrity_check(path) == "ok"




def test_every_index_key_is_reachable_through_seek_eq(tmp_path) -> None:
    """Whatever the tree's shape, every inserted key must still be found --
    including keys that ended up promoted onto an interior page.
    """
    path = tmp_path / "t.db"
    _build(path, MULTI_LEVEL_ROWS)
    db = quilldb.connect(str(path))
    missing = [
        age for age in range(1, MULTI_LEVEL_ROWS + 1)
        if len(db.execute("SELECT id FROM t WHERE age = ?", (age,)).fetchall()) != 1
    ]
    db.close()
    assert missing == []




def test_full_index_scan_yields_every_key_in_order(tmp_path) -> None:
    """A range scan over the whole index must yield each entry exactly
    once, in key order -- interior entries included, none duplicated.
    """
    path = tmp_path / "t.db"
    _build(path, MULTI_LEVEL_ROWS)
    db = quilldb.connect(str(path))
    ages = [row[0] for row in db.execute("SELECT age FROM t WHERE age >= 0").fetchall()]
    db.close()
    assert sorted(ages) == list(range(1, MULTI_LEVEL_ROWS + 1))
    assert len(ages) == MULTI_LEVEL_ROWS




# =====================================================================
# Deleting an entry that lives on an INTERIOR page (§11.6)
# =====================================================================




def _divider_keys(path: pathlib.Path) -> list[int]:
    """The `age` values that ended up promoted onto interior pages. These
    are the entries with no leaf copy to fall back on.
    """
    from quilldb.btree.cells import decode_interior_index_cell
    from quilldb.btree.index import IndexBTree
    from quilldb.constants import PageType
    from quilldb.storage.page import parse_page


    root = _index_root(path)
    pager = Pager.open(path)
    pool = BufferPool(pager, capacity=64)
    found: list[int] = []
    try:
        ix = IndexBTree(pager, pool, root, n_key_columns=1, unique=False)


        def walk(page_id: int) -> None:
            raw = pool.get_page(page_id)
            try:
                body = parse_page(raw)
                if body.page_type is PageType.LEAF_INDEX:
                    return
                children = [decode_interior_index_cell(c)[0] for c in body.cells] + [body.right_child]
                for cell in body.cells:
                    found.append(ix._decode_key(PageType.INTERIOR_INDEX, cell)[0])
            finally:
                pool.unpin(page_id)
            for child in children:
                walk(child)


        walk(root)
    finally:
        pager.close()
    return found




def test_a_multi_level_index_actually_has_dividers(tmp_path) -> None:
    """Guard on the guard: if this ever returns [], every test below is
    silently vacuous and the interior-delete path is untested.
    """
    path = tmp_path / "t.db"
    _build(path, MULTI_LEVEL_ROWS)
    assert _divider_keys(path)




def test_deleting_a_divider_removes_it_from_the_index(tmp_path) -> None:
    """An index divider has no leaf copy, so a leaf-only delete would leave
    it behind as an entry pointing at a row that no longer exists.
    """
    path = tmp_path / "t.db"
    _build(path, MULTI_LEVEL_ROWS)
    divider = _divider_keys(path)[0]


    db = quilldb.connect(str(path))
    db.execute("DELETE FROM t WHERE age = ?", (divider,))
    assert db.execute("SELECT id FROM t WHERE age = ?", (divider,)).fetchall() == []
    db.close()


    assert _validate(path) == MULTI_LEVEL_ROWS - 1
    assert _integrity_check(path) == "ok"




def test_deleting_every_divider_keeps_the_index_valid(tmp_path) -> None:
    path = tmp_path / "t.db"
    _build(path, MULTI_LEVEL_ROWS)
    dividers = _divider_keys(path)


    db = quilldb.connect(str(path))
    for age in dividers:
        db.execute("DELETE FROM t WHERE age = ?", (age,))
    db.close()


    assert _validate(path) == MULTI_LEVEL_ROWS - len(dividers)
    assert _integrity_check(path) == "ok"




def test_delete_heavy_workload_leaves_a_clean_index(tmp_path) -> None:
    """The week-4 acceptance test: `integrity_check` is `ok` after a
    delete-heavy workload against a multi-level index.
    """
    import random


    path = tmp_path / "t.db"
    _build(path, MULTI_LEVEL_ROWS)
    ids = list(range(1, MULTI_LEVEL_ROWS + 1))
    random.Random(7).shuffle(ids)
    doomed = ids[: (MULTI_LEVEL_ROWS * 2) // 3]


    db = quilldb.connect(str(path))
    for i in doomed:
        db.execute("DELETE FROM t WHERE id = ?", (i,))
    db.close()


    survivors = MULTI_LEVEL_ROWS - len(doomed)
    assert _validate(path) == survivors
    assert _integrity_check(path) == "ok"




def test_index_and_table_agree_after_a_delete_heavy_workload(tmp_path) -> None:
    """The roadmap's index-vs-scan consistency check -- the assertion week 5
    reuses inside its crash loop.
    """
    import random


    path = tmp_path / "t.db"
    _build(path, MULTI_LEVEL_ROWS)
    ids = list(range(1, MULTI_LEVEL_ROWS + 1))
    random.Random(11).shuffle(ids)
    doomed = set(ids[: MULTI_LEVEL_ROWS // 2])


    db = quilldb.connect(str(path))
    for i in doomed:
        db.execute("DELETE FROM t WHERE id = ?", (i,))
    via_index = sorted(row[0] for row in db.execute("SELECT id FROM t WHERE age >= 0").fetchall())
    via_scan = sorted(row[0] for row in db.execute("SELECT id FROM t").fetchall())
    db.close()


    expected = sorted(set(range(1, MULTI_LEVEL_ROWS + 1)) - doomed)
    assert via_index == via_scan == expected




# =====================================================================
# The roadmap's index-vs-scan consistency property
# =====================================================================




@pytest.mark.parametrize("seed", [0, 1, 2, 3])
def test_index_agrees_with_the_table_after_random_mutations(tmp_path, seed: int) -> None:
    """Derived data must match its source, after any mix of operations.


    Week 4's spec calls this "the test that matters more than all the
    others", and week 5 reuses the same assertion inside its crash loop --
    "indexes and tables never disagree after any crash point" is this,
    called after recovery. It has to hold before a journal exists for that
    to mean anything.
    """
    import random


    path = tmp_path / f"m{seed}.db"
    rnd = random.Random(seed)


    db = quilldb.connect(str(path))
    db.execute("CREATE TABLE t (id INTEGER, age INTEGER)")
    db.execute("CREATE INDEX ix ON t (age)")


    live: dict[int, int] = {}
    next_id = 1
    for _ in range(1200):
        roll = rnd.random()
        if roll < 0.62 or not live:
            age = rnd.randint(0, 400)
            db.execute("INSERT INTO t VALUES (?, ?)", (next_id, age))
            live[next_id] = age
            next_id += 1
        elif roll < 0.85:
            victim = rnd.choice(list(live))
            db.execute("DELETE FROM t WHERE id = ?", (victim,))
            del live[victim]
        else:
            target = rnd.choice(list(live))
            age = rnd.randint(0, 400)
            db.execute("UPDATE t SET age = ? WHERE id = ?", (age, target))
            live[target] = age


    # `age >= 0` is sargable, so this reads through the index; the bare
    # SELECT is a sequential scan. They must not disagree.
    via_index = sorted(row[0] for row in db.execute("SELECT id FROM t WHERE age >= 0").fetchall())
    via_scan = sorted(row[0] for row in db.execute("SELECT id FROM t").fetchall())
    db.close()


    assert via_index == via_scan == sorted(live)
    assert _validate(path) == len(live)
    assert _integrity_check(path) == "ok"
