import pytest

from quilldb.btree.btree import BTree
from quilldb.btree.cells import (
    decode_interior_table_cell,
    decode_leaf_table_cell,
    encode_interior_table_cell,
    encode_leaf_table_cell,
)
from quilldb.btree.validate import validate_btree
from quilldb.constants import PageType
from quilldb.errors import DuplicateRowIDError
from quilldb.storage.bufferpool import BufferPool
from quilldb.storage.overflow import read_overflow_chain
from quilldb.storage.page import PageBody, parse_page, serialize_page
from quilldb.storage.pager import Pager


@pytest.fixture
def pager(tmp_path):
    p = Pager.create(tmp_path / "test.db")
    yield p
    p.close()




@pytest.fixture
def pool(pager):
    return BufferPool(pager, capacity=16)




def _leaf_cell(rowid: int) -> bytes:
    payload = f"row{rowid}".encode()
    return encode_leaf_table_cell(rowid, len(payload), payload)




def _write_page(pager: Pager, pool: BufferPool, body: PageBody) -> int:
    page_id = pager._allocate_page()
    with pool.pinned(page_id, dirty=True) as raw:
        raw[:] = serialize_page(body)
    return page_id




def _build_three_leaf_tree(pager: Pager, pool: BufferPool) -> tuple[BTree, int, int, int]:
    """Root -> {leaf_lo (1..5), leaf_mid (10..15), leaf_hi (20..25) via right_child}.


    Mirrors §6.1's convention deliberately: leaf_lo's own max rowid (5) IS the
    separator between it and leaf_mid, so probing exactly that key exercises
    the "equal goes left" boundary without any extra setup.
    """
    leaf_lo = _write_page(pager, pool, PageBody(PageType.LEAF_TABLE, cells=[_leaf_cell(r) for r in [1, 3, 5]]))
    leaf_mid = _write_page(
        pager, pool, PageBody(PageType.LEAF_TABLE, cells=[_leaf_cell(r) for r in [10, 12, 15]])
    )
    leaf_hi = _write_page(
        pager, pool, PageBody(PageType.LEAF_TABLE, cells=[_leaf_cell(r) for r in [20, 22, 25]])
    )


    root_body = PageBody(
        PageType.INTERIOR_TABLE,
        cells=[
            encode_interior_table_cell(leaf_lo, 5),
            encode_interior_table_cell(leaf_mid, 15),
        ],
        right_child=leaf_hi,
    )
    root = _write_page(pager, pool, root_body)


    return BTree(pager, pool, root), leaf_lo, leaf_mid, leaf_hi




# =====================================================================
# Descent through a hand-built, multi-level tree (§6.1, §6.8 session 3) --
# never through insert, so a failure here is unambiguously in search/descent.
# =====================================================================




def test_finds_key_in_leftmost_leaf(pager, pool) -> None:
    bt, leaf_lo, _, _ = _build_three_leaf_tree(pager, pool)
    assert bt.search(3) == (leaf_lo, 1)




def test_boundary_key_equal_to_separator_goes_left(pager, pool) -> None:
    bt, leaf_lo, _, _ = _build_three_leaf_tree(pager, pool)
    assert bt.search(5) == (leaf_lo, 2)




def test_finds_key_in_middle_leaf(pager, pool) -> None:
    bt, _, leaf_mid, _ = _build_three_leaf_tree(pager, pool)
    assert bt.search(12) == (leaf_mid, 1)




def test_finds_key_in_rightmost_leaf_via_right_child(pager, pool) -> None:
    bt, _, _, leaf_hi = _build_three_leaf_tree(pager, pool)
    assert bt.search(22) == (leaf_hi, 1)




def test_absent_key_inside_a_leafs_range_returns_none(pager, pool) -> None:
    """7 routes to leaf_mid (6..15's separator range) but leaf_mid holds no rowid 7."""
    bt, *_ = _build_three_leaf_tree(pager, pool)
    assert bt.search(7) is None




def test_absent_key_past_every_leaf_returns_none(pager, pool) -> None:
    bt, *_ = _build_three_leaf_tree(pager, pool)
    assert bt.search(999) is None




def test_single_page_tree_root_is_a_leaf(pager, pool) -> None:
    leaf = _write_page(pager, pool, PageBody(PageType.LEAF_TABLE, cells=[_leaf_cell(1)]))
    bt = BTree(pager, pool, leaf)
    assert bt.search(1) == (leaf, 0)
    assert bt.search(2) is None




def test_search_does_not_leak_pins(pager, pool) -> None:
    """A small pool that would raise PoolExhaustedError if any get_page() along
    the descent path were left unpinned.
    """
    small_pool = BufferPool(pager, capacity=2)
    bt, leaf_lo, leaf_mid, _ = _build_three_leaf_tree(pager, small_pool)


    for key in [3, 5, 12, 22, 999, 1, 30]:
        bt.search(key)


    # Still usable afterwards -- a leak would have exhausted capacity=2 long before this.
    with small_pool.pinned(leaf_lo), small_pool.pinned(leaf_mid):
        pass




# =====================================================================
# insert -- sorted placement into an existing leaf. Splitting a full leaf
# is covered separately in test_btree_splits.py.
# =====================================================================




def test_insert_into_empty_leaf(pager, pool) -> None:
    leaf = _write_page(pager, pool, PageBody(PageType.LEAF_TABLE))
    bt = BTree(pager, pool, leaf)


    bt.insert(1, b"hello")


    assert bt.search(1) == (leaf, 0)




def test_insert_at_start_middle_and_end_keeps_sorted_order(pager, pool) -> None:
    leaf = _write_page(pager, pool, PageBody(PageType.LEAF_TABLE, cells=[_leaf_cell(10), _leaf_cell(30)]))
    bt = BTree(pager, pool, leaf)


    bt.insert(20, b"middle")  # between existing cells
    bt.insert(5, b"start")  # before every existing cell
    bt.insert(40, b"end")  # after every existing cell


    raw = pool.get_page(leaf)
    body = parse_page(raw)
    pool.unpin(leaf)
    rowids = [decode_leaf_table_cell(cell)[0] for cell in body.cells]
    assert rowids == [5, 10, 20, 30, 40]




def test_insert_routes_through_a_multilevel_tree_to_the_right_leaf(pager, pool) -> None:
    bt, _, leaf_mid, _ = _build_three_leaf_tree(pager, pool)


    bt.insert(13, b"new-row")


    assert bt.search(13) == (leaf_mid, 2)




def test_insert_persists_the_exact_payload(pager, pool) -> None:
    leaf = _write_page(pager, pool, PageBody(PageType.LEAF_TABLE))
    bt = BTree(pager, pool, leaf)
    payload = b"a small row"


    bt.insert(7, payload)


    page_id, slot = bt.search(7)
    raw = pool.get_page(page_id)
    body = parse_page(raw)
    pool.unpin(page_id)
    assert decode_leaf_table_cell(body.cells[slot]) == (7, len(payload), payload, 0)




def test_insert_spills_an_oversized_payload_to_overflow(pager, pool) -> None:
    leaf = _write_page(pager, pool, PageBody(PageType.LEAF_TABLE))
    bt = BTree(pager, pool, leaf)
    payload = bytes((i * 3) % 256 for i in range(10_000))  # comfortably spills


    bt.insert(99, payload)


    page_id, slot = bt.search(99)
    raw = pool.get_page(page_id)
    body = parse_page(raw)
    pool.unpin(page_id)
    rowid, total_len, local_payload, overflow_page = decode_leaf_table_cell(body.cells[slot])
    assert (rowid, total_len) == (99, len(payload))
    assert overflow_page != 0


    rest = read_overflow_chain(pager, pool, overflow_page, total_len - len(local_payload))
    assert local_payload + rest == payload




def test_insert_duplicate_rowid_raises_and_leaves_the_page_untouched(pager, pool) -> None:
    leaf = _write_page(pager, pool, PageBody(PageType.LEAF_TABLE, cells=[_leaf_cell(5)]))
    bt = BTree(pager, pool, leaf)


    with pytest.raises(DuplicateRowIDError):
        bt.insert(5, b"clobber?")


    raw = pool.get_page(leaf)
    body = parse_page(raw)
    pool.unpin(leaf)
    assert len(body.cells) == 1
    assert decode_leaf_table_cell(body.cells[0])[2] == b"row5"  # unchanged




def test_insert_releases_leaf_pin_when_overflow_write_fails(pager, pool, monkeypatch) -> None:
    """An overflow-write error must not strand the target leaf in the buffer pool."""
    leaf = _write_page(pager, pool, PageBody(PageType.LEAF_TABLE))
    pool.flush_all()  # The small pool below must read the initialized leaf from disk.
    small_pool = BufferPool(pager, capacity=1)
    bt = BTree(pager, small_pool, leaf)


    def fail_overflow_write(*_args, **_kwargs):
        raise OSError("simulated overflow write failure")


    monkeypatch.setattr("quilldb.btree.btree.write_overflow_chain", fail_overflow_write)


    with pytest.raises(OSError, match="simulated overflow write failure"):
        bt.insert(1, b"x" * 10_000)


    # This fetch needs to evict the target leaf. It can only do so if insert()
    # released the leaf pin while propagating the overflow-write exception.
    other_page = pager._allocate_page()
    with small_pool.pinned(other_page):
        pass




def test_insert_does_not_leak_pins(pager) -> None:
    small_pool = BufferPool(pager, capacity=2)
    bt, leaf_lo, leaf_mid, _ = _build_three_leaf_tree(pager, small_pool)


    bt.insert(13, b"row13")
    bt.insert(2, b"row2")


    # Still usable afterwards -- a leaked pin on the leaf (success path) or on
    # an ancestor (the duplicate/full-page raise paths) would exhaust capacity=2.
    with pytest.raises(DuplicateRowIDError):
        bt.insert(13, b"dup")
    with small_pool.pinned(leaf_lo), small_pool.pinned(leaf_mid):
        pass



# =====================================================================
# delete -- remove a leaf cell, reclaim empty leaves, and shrink the root.
# =====================================================================




def test_delete_removes_a_cell_and_reports_a_missing_rowid(pager, pool) -> None:
    bt, _, leaf_mid, _ = _build_three_leaf_tree(pager, pool)


    assert bt.delete(12) is True
    assert bt.search(12) is None
    assert bt.delete(12) is False


    raw = pool.get_page(leaf_mid)
    try:
        assert [decode_leaf_table_cell(cell)[0] for cell in parse_page(raw).cells] == [10, 15]
    finally:
        pool.unpin(leaf_mid)
    validate_btree(pager, pool, bt.root)




def test_delete_empty_leaf_unlinks_it_from_a_parent_with_three_children(pager, pool) -> None:
    bt, leaf_lo, leaf_mid, leaf_hi = _build_three_leaf_tree(pager, pool)


    for rowid in [10, 12, 15]:
        assert bt.delete(rowid) is True


    root_raw = pool.get_page(bt.root)
    try:
        root = parse_page(root_raw)
    finally:
        pool.unpin(bt.root)


    assert [(decode_interior_table_cell(cell)) for cell in root.cells] == [(leaf_lo, 5)]
    assert root.right_child == leaf_hi
    assert pager._allocate_page() == leaf_mid
    validate_btree(pager, pool, bt.root)




def test_delete_from_one_of_two_root_children_collapses_the_root(pager, pool) -> None:
    """A root left with one child is pulled up: real sqlite3 calls a
    zero-cell interior page malformed (NOTES.md B8-1). The root keeps its
    page number and the vacated child goes to the freelist.
    """
    left = _write_page(pager, pool, PageBody(PageType.LEAF_TABLE, cells=[_leaf_cell(1)]))
    right = _write_page(pager, pool, PageBody(PageType.LEAF_TABLE, cells=[_leaf_cell(2)]))
    root = _write_page(
        pager,
        pool,
        PageBody(
            PageType.INTERIOR_TABLE,
            cells=[encode_interior_table_cell(left, 1)],
            right_child=right,
        ),
    )
    bt = BTree(pager, pool, root)

    assert bt.delete(1) is True
    assert bt.root == root
    assert bt.search(2) == (root, 0)

    raw = pool.get_page(root)
    try:
        root_body = parse_page(raw)
    finally:
        pool.unpin(root)
    assert root_body.page_type is PageType.LEAF_TABLE
    assert len(root_body.cells) == 1
    validate_btree(pager, pool, root)


def test_delete_bypasses_a_non_root_interior_page_left_with_one_child(pager, pool) -> None:
    """root -> [mid_a, mid_b]; each mid has two leaves. Emptying one leaf of
    mid_a leaves it with one child, so the root should point at that leaf
    directly and mid_a should be freed.
    """
    leaves = [
        _write_page(pager, pool, PageBody(PageType.LEAF_TABLE, cells=[_leaf_cell(i)])) for i in (1, 2, 3, 4)
    ]
    mid_a = _write_page(
        pager,
        pool,
        PageBody(PageType.INTERIOR_TABLE, cells=[encode_interior_table_cell(leaves[0], 1)], right_child=leaves[1]),
    )
    mid_b = _write_page(
        pager,
        pool,
        PageBody(PageType.INTERIOR_TABLE, cells=[encode_interior_table_cell(leaves[2], 3)], right_child=leaves[3]),
    )
    root = _write_page(
        pager,
        pool,
        PageBody(PageType.INTERIOR_TABLE, cells=[encode_interior_table_cell(mid_a, 2)], right_child=mid_b),
    )
    bt = BTree(pager, pool, root)

    assert bt.delete(1) is True

    raw = pool.get_page(root)
    try:
        root_body = parse_page(raw)
    finally:
        pool.unpin(root)
    child, _ = decode_interior_table_cell(root_body.cells[0])
    assert child == leaves[1]
    for rowid in (2, 3, 4):
        assert bt.search(rowid) is not None
    assert bt.search(1) is None
    validate_btree(pager, pool, root)


def _full_interior(pager, pool, first_key: int) -> tuple[int, int]:
    """An interior page filled until no more cells fit, over one-row leaves
    with keys first_key, first_key+1, ...; returns (page, last_key)."""
    body = PageBody(PageType.INTERIOR_TABLE)
    key = first_key
    while True:
        leaf = _write_page(pager, pool, PageBody(PageType.LEAF_TABLE, cells=[_leaf_cell(key)]))
        cell = encode_interior_table_cell(leaf, key)
        if not body.fits(len(cell)):
            body.right_child = leaf
            return _write_page(pager, pool, body), key
        body.insert_cell(len(body.cells), cell)
        key += 1


@pytest.mark.parametrize("sibling_on_right", [True, False])
def test_delete_rotates_from_a_full_sibling(pager, pool, sibling_on_right) -> None:
    """A single-child interior page next to a FULL sibling cannot merge into
    it, so one child rotates over; the grandparent keeps both children."""
    if sibling_on_right:
        lone_leaf = _write_page(pager, pool, PageBody(PageType.LEAF_TABLE, cells=[_leaf_cell(1)]))
        keeper = _write_page(pager, pool, PageBody(PageType.LEAF_TABLE, cells=[_leaf_cell(2)]))
        lone = _write_page(
            pager, pool,
            PageBody(PageType.INTERIOR_TABLE, cells=[encode_interior_table_cell(lone_leaf, 1)], right_child=keeper),
        )
        full, last = _full_interior(pager, pool, 3)
        root = _write_page(
            pager, pool,
            PageBody(PageType.INTERIOR_TABLE, cells=[encode_interior_table_cell(lone, 2)], right_child=full),
        )
        gone, kept = 1, [2, *range(3, last + 1)]
    else:
        full, last = _full_interior(pager, pool, 1)
        lone_leaf = _write_page(pager, pool, PageBody(PageType.LEAF_TABLE, cells=[_leaf_cell(last + 1)]))
        keeper = _write_page(pager, pool, PageBody(PageType.LEAF_TABLE, cells=[_leaf_cell(last + 2)]))
        lone = _write_page(
            pager, pool,
            PageBody(PageType.INTERIOR_TABLE, cells=[encode_interior_table_cell(lone_leaf, last + 1)], right_child=keeper),
        )
        root = _write_page(
            pager, pool,
            PageBody(PageType.INTERIOR_TABLE, cells=[encode_interior_table_cell(full, last)], right_child=lone),
        )
        gone, kept = last + 1, [*range(1, last + 1), last + 2]
    bt = BTree(pager, pool, root)

    assert bt.delete(gone) is True

    raw = pool.get_page(root)
    try:
        assert len(parse_page(raw).cells) == 1  # still two children: a rotation, not a merge
    finally:
        pool.unpin(root)
    assert bt.search(gone) is None
    assert all(bt.search(k) is not None for k in kept)
    validate_btree(pager, pool, root)


def test_delete_last_row_collapses_root_to_an_empty_leaf(pager, pool) -> None:
    left = _write_page(pager, pool, PageBody(PageType.LEAF_TABLE, cells=[_leaf_cell(1)]))
    right = _write_page(pager, pool, PageBody(PageType.LEAF_TABLE, cells=[_leaf_cell(2)]))
    root = _write_page(
        pager,
        pool,
        PageBody(
            PageType.INTERIOR_TABLE,
            cells=[encode_interior_table_cell(left, 1)],
            right_child=right,
        ),
    )
    bt = BTree(pager, pool, root)


    assert bt.delete(1) is True
    assert bt.delete(2) is True
    assert bt.root == root
    assert bt.search(1) is None
    assert bt.search(2) is None


    raw = pool.get_page(root)
    try:
        root_body = parse_page(raw)
    finally:
        pool.unpin(root)
    assert root_body.page_type is PageType.LEAF_TABLE
    assert root_body.cells == []
    validate_btree(pager, pool, root)




def test_delete_frees_an_overflow_chain_for_reuse(pager, pool) -> None:
    """The counterpart to test_insert_spills_an_oversized_payload_to_overflow:
    a deleted row's overflow pages must come back through the SAME freelist
    allocate_page() draws from, not just vanish from the tree.
    """
    leaf = _write_page(pager, pool, PageBody(PageType.LEAF_TABLE))
    bt = BTree(pager, pool, leaf)
    payload = bytes((i * 3) % 256 for i in range(10_000))  # comfortably spills
    bt.insert(99, payload)


    page_id, slot = bt.search(99)
    raw = pool.get_page(page_id)
    overflow_page = decode_leaf_table_cell(parse_page(raw).cells[slot])[3]
    pool.unpin(page_id)
    assert overflow_page != 0


    freelist_before = pager._header.freelist_count
    assert bt.delete(99) is True
    assert bt.search(99) is None
    freed = pager._header.freelist_count - freelist_before
    assert freed >= 1  # at least the overflow chain's own pages


    reused = {pool.allocate_page() for _ in range(freed)}
    assert overflow_page in reused
    validate_btree(pager, pool, bt.root)