import pytest


from quilldb.btree.btree import BTree
from quilldb.btree.cells import encode_interior_table_cell, encode_leaf_table_cell
from quilldb.constants import PageType
from quilldb.storage.bufferpool import BufferPool
from quilldb.storage.page import PageBody, serialize_page
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
    page_id = pager.allocate_page()
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



