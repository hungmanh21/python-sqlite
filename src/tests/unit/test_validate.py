"""Acceptance tests for validate_btree() -- the structural safety net (§6.6).


Every test here hand-builds a small tree (valid or deliberately broken) and
asserts validate_btree() accepts or rejects it. This is the same "no Pager
tricks, just pages built by hand" style test_btree_splits.py uses for the
split cascade, for the same reason: a validator failure must be traceable to
the exact invariant it's supposed to catch, not to some unrelated fixture
detail.
"""


import pytest


from quilldb.btree.cells import (
    encode_interior_table_cell,
    encode_leaf_index_cell,
    encode_leaf_table_cell,
    local_payload_size,
)
from quilldb.btree.validate import validate_btree
from quilldb.constants import PageType
from quilldb.errors import BTreeInvariantError, OverflowCycleError
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




def _payload(rowid: int) -> bytes:
    return f"row{rowid}".encode()




def _leaf_cell(rowid: int) -> bytes:
    payload = _payload(rowid)
    return encode_leaf_table_cell(rowid, len(payload), payload)




def _write_page(pager: Pager, pool: BufferPool, body: PageBody) -> int:
    page_id = pager.allocate_page()
    with pool.pinned(page_id, dirty=True) as raw:
        raw[:] = serialize_page(body)
    return page_id




def test_single_leaf_is_valid(pager, pool) -> None:
    root = _write_page(
        pager, pool, PageBody(PageType.LEAF_TABLE, cells=[_leaf_cell(1), _leaf_cell(2), _leaf_cell(3)])
    )
    validate_btree(pager, pool, root)  # must not raise




def test_two_level_tree_is_valid(pager, pool) -> None:
    left = _write_page(pager, pool, PageBody(PageType.LEAF_TABLE, cells=[_leaf_cell(1), _leaf_cell(2)]))
    right = _write_page(pager, pool, PageBody(PageType.LEAF_TABLE, cells=[_leaf_cell(5), _leaf_cell(6)]))
    root = _write_page(
        pager,
        pool,
        PageBody(PageType.INTERIOR_TABLE, cells=[encode_interior_table_cell(left, 2)], right_child=right),
    )
    validate_btree(pager, pool, root)  # must not raise




def test_three_level_tree_is_valid(pager, pool) -> None:
    """A proper 3-level tree: EVERY leaf is 2 levels below the root -- not a
    mix of leaves and interiors at the same level (that would be an uneven
    height, which is itself a violation, not a shape this test should use).
    """
    leaf_a = _write_page(pager, pool, PageBody(PageType.LEAF_TABLE, cells=[_leaf_cell(1)]))
    leaf_b = _write_page(pager, pool, PageBody(PageType.LEAF_TABLE, cells=[_leaf_cell(10)]))
    leaf_c = _write_page(pager, pool, PageBody(PageType.LEAF_TABLE, cells=[_leaf_cell(20)]))
    leaf_d = _write_page(pager, pool, PageBody(PageType.LEAF_TABLE, cells=[_leaf_cell(30)]))
    left = _write_page(
        pager, pool, PageBody(PageType.INTERIOR_TABLE, cells=[encode_interior_table_cell(leaf_a, 1)], right_child=leaf_b)
    )
    right = _write_page(
        pager, pool, PageBody(PageType.INTERIOR_TABLE, cells=[encode_interior_table_cell(leaf_c, 20)], right_child=leaf_d)
    )
    root = _write_page(
        pager, pool, PageBody(PageType.INTERIOR_TABLE, cells=[encode_interior_table_cell(left, 10)], right_child=right)
    )
    validate_btree(pager, pool, root)  # must not raise




def test_unsorted_leaf_rowids_raise(pager, pool) -> None:
    root = _write_page(pager, pool, PageBody(PageType.LEAF_TABLE, cells=[_leaf_cell(3), _leaf_cell(1)]))
    with pytest.raises(BTreeInvariantError):
        validate_btree(pager, pool, root)




def test_unsorted_interior_separators_raise(pager, pool) -> None:
    left = _write_page(pager, pool, PageBody(PageType.LEAF_TABLE, cells=[_leaf_cell(1)]))
    mid = _write_page(pager, pool, PageBody(PageType.LEAF_TABLE, cells=[_leaf_cell(10)]))
    right = _write_page(pager, pool, PageBody(PageType.LEAF_TABLE, cells=[_leaf_cell(20)]))
    root = _write_page(
        pager,
        pool,
        PageBody(
            PageType.INTERIOR_TABLE,
            # 10 before 1 -- separators must be strictly ascending.
            cells=[encode_interior_table_cell(left, 10), encode_interior_table_cell(mid, 1)],
            right_child=right,
        ),
    )
    with pytest.raises(BTreeInvariantError):
        validate_btree(pager, pool, root)




def test_key_greater_than_its_separator_raises(pager, pool) -> None:
    """Left child holds a key that should have gone right instead."""
    left = _write_page(pager, pool, PageBody(PageType.LEAF_TABLE, cells=[_leaf_cell(1), _leaf_cell(99)]))
    right = _write_page(pager, pool, PageBody(PageType.LEAF_TABLE, cells=[_leaf_cell(100)]))
    root = _write_page(
        pager,
        pool,
        PageBody(PageType.INTERIOR_TABLE, cells=[encode_interior_table_cell(left, 2)], right_child=right),
    )
    with pytest.raises(BTreeInvariantError):
        validate_btree(pager, pool, root)




def test_right_child_key_not_greater_than_separator_raises(pager, pool) -> None:
    """Right child holds a key that should have gone left instead."""
    left = _write_page(pager, pool, PageBody(PageType.LEAF_TABLE, cells=[_leaf_cell(1)]))
    right = _write_page(pager, pool, PageBody(PageType.LEAF_TABLE, cells=[_leaf_cell(2)]))
    root = _write_page(
        pager,
        pool,
        PageBody(PageType.INTERIOR_TABLE, cells=[encode_interior_table_cell(left, 5)], right_child=right),
    )
    with pytest.raises(BTreeInvariantError):
        validate_btree(pager, pool, root)




def test_page_reachable_twice_raises(pager, pool) -> None:
    """Same child page used for both a separator cell and right_child --
    ambiguous ownership, not a valid tree shape.
    """
    shared = _write_page(pager, pool, PageBody(PageType.LEAF_TABLE, cells=[_leaf_cell(1)]))
    root = _write_page(
        pager,
        pool,
        PageBody(PageType.INTERIOR_TABLE, cells=[encode_interior_table_cell(shared, 1)], right_child=shared),
    )
    with pytest.raises(BTreeInvariantError):
        validate_btree(pager, pool, root)




def test_uneven_leaf_depth_raises(pager, pool) -> None:
    """Left subtree is a bare leaf; right subtree is one level deeper."""
    left_leaf = _write_page(pager, pool, PageBody(PageType.LEAF_TABLE, cells=[_leaf_cell(1)]))
    right_left_leaf = _write_page(pager, pool, PageBody(PageType.LEAF_TABLE, cells=[_leaf_cell(10)]))
    right_right_leaf = _write_page(pager, pool, PageBody(PageType.LEAF_TABLE, cells=[_leaf_cell(20)]))
    right_subtree = _write_page(
        pager,
        pool,
        PageBody(
            PageType.INTERIOR_TABLE,
            cells=[encode_interior_table_cell(right_left_leaf, 10)],
            right_child=right_right_leaf,
        ),
    )
    root = _write_page(
        pager,
        pool,
        PageBody(
            PageType.INTERIOR_TABLE, cells=[encode_interior_table_cell(left_leaf, 5)], right_child=right_subtree
        ),
    )
    with pytest.raises(BTreeInvariantError):
        validate_btree(pager, pool, root)




def test_wrong_page_type_raises(pager, pool) -> None:
    """A LEAF_INDEX page reached where a LEAF_TABLE is required."""
    wrong_type_leaf = _write_page(
        pager, pool, PageBody(PageType.LEAF_INDEX, cells=[encode_leaf_index_cell(3, b"abc")])
    )
    right = _write_page(pager, pool, PageBody(PageType.LEAF_TABLE, cells=[_leaf_cell(5)]))
    root = _write_page(
        pager,
        pool,
        PageBody(
            PageType.INTERIOR_TABLE, cells=[encode_interior_table_cell(wrong_type_leaf, 2)], right_child=right
        ),
    )
    with pytest.raises(BTreeInvariantError):
        validate_btree(pager, pool, root)




def test_overflow_cycle_is_detected(pager, pool) -> None:
    """A spilled cell whose overflow chain loops back on itself."""
    page_a = pager.allocate_page()
    page_b = pager.allocate_page()
    with pool.pinned(page_a, dirty=True) as raw_a:
        raw_a[0:4] = page_b.to_bytes(4, "big")
    with pool.pinned(page_b, dirty=True) as raw_b:
        raw_b[0:4] = page_a.to_bytes(4, "big")  # loops back to A instead of ending at 0


    total_len = 100_000  # comfortably bigger than max_local_payload(LEAF_TABLE)
    local_len = local_payload_size(PageType.LEAF_TABLE, total_len)
    cell = encode_leaf_table_cell(1, total_len, b"x" * local_len, overflow_page=page_a)


    root = _write_page(pager, pool, PageBody(PageType.LEAF_TABLE, cells=[cell]))
    with pytest.raises(OverflowCycleError):
        validate_btree(pager, pool, root)