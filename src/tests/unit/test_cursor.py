import pytest

from quilldb.btree.cells import (
    encode_interior_table_cell,
    encode_leaf_table_cell,
    local_payload_size,
)
from quilldb.btree.cursor import TableCursor
from quilldb.constants import PageType
from quilldb.storage.bufferpool import BufferPool
from quilldb.storage.overflow import write_overflow_chain
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




def _build_three_leaf_tree(pager: Pager, pool: BufferPool) -> tuple[int, list[int]]:
    """Root -> {leaf_lo (1,3,5), leaf_mid (10,12,15), leaf_hi (20,22,25) via right_child}.


    Same shape as test_btree.py's fixture -- separators exactly match each
    leaf's real max rowid, so every key routes straight to the leaf that
    actually holds it (or would, if present). All rowids in order: the
    property this file's traversal tests check against.
    """
    all_rowids = [1, 3, 5, 10, 12, 15, 20, 22, 25]
    leaf_lo = _write_page(pager, pool, PageBody(PageType.LEAF_TABLE, cells=[_leaf_cell(r) for r in [1, 3, 5]]))
    leaf_mid = _write_page(
        pager, pool, PageBody(PageType.LEAF_TABLE, cells=[_leaf_cell(r) for r in [10, 12, 15]])
    )
    leaf_hi = _write_page(
        pager, pool, PageBody(PageType.LEAF_TABLE, cells=[_leaf_cell(r) for r in [20, 22, 25]])
    )
    root_body = PageBody(
        PageType.INTERIOR_TABLE,
        cells=[encode_interior_table_cell(leaf_lo, 5), encode_interior_table_cell(leaf_mid, 15)],
        right_child=leaf_hi,
    )
    root = _write_page(pager, pool, root_body)
    return root, all_rowids




def _build_tree_with_separator_gap(pager: Pager, pool: BufferPool) -> int:
    """Root separators [5, 15], but leaf_mid's real cells stop at 12 -- 13,14,15
    are "promised" by the separator but don't exist. Not a tree insert() would
    ever produce, but a structurally valid one, deliberately built to exercise
    seek()'s cross-leaf hop: landing on a leaf where every rowid is < key even
    though descent routed here correctly.
    """
    leaf_lo = _write_page(pager, pool, PageBody(PageType.LEAF_TABLE, cells=[_leaf_cell(r) for r in [1, 3, 5]]))
    leaf_mid = _write_page(pager, pool, PageBody(PageType.LEAF_TABLE, cells=[_leaf_cell(r) for r in [10, 12]]))
    leaf_hi = _write_page(
        pager, pool, PageBody(PageType.LEAF_TABLE, cells=[_leaf_cell(r) for r in [20, 22, 25]])
    )
    root_body = PageBody(
        PageType.INTERIOR_TABLE,
        cells=[encode_interior_table_cell(leaf_lo, 5), encode_interior_table_cell(leaf_mid, 15)],
        right_child=leaf_hi,
    )
    return _write_page(pager, pool, root_body)




# =====================================================================
# first / last
# =====================================================================




def test_first_positions_at_smallest_key(pager, pool) -> None:
    root, _ = _build_three_leaf_tree(pager, pool)
    cur = TableCursor(pager, pool, root)
    cur.first()
    assert cur.valid
    assert cur.rowid() == 1
    cur.close()




def test_last_positions_at_largest_key(pager, pool) -> None:
    root, _ = _build_three_leaf_tree(pager, pool)
    cur = TableCursor(pager, pool, root)
    cur.last()
    assert cur.valid
    assert cur.rowid() == 25
    cur.close()




def test_first_on_empty_tree_is_unpositioned(pager, pool) -> None:
    empty_root = _write_page(pager, pool, PageBody(PageType.LEAF_TABLE))
    cur = TableCursor(pager, pool, empty_root)
    cur.first()
    assert not cur.valid
    cur.close()




def test_last_on_empty_tree_is_unpositioned(pager, pool) -> None:
    empty_root = _write_page(pager, pool, PageBody(PageType.LEAF_TABLE))
    cur = TableCursor(pager, pool, empty_root)
    cur.last()
    assert not cur.valid
    cur.close()




# =====================================================================
# next / prev -- must cross leaf boundaries, not just walk one page.
# =====================================================================




def test_next_visits_every_rowid_in_order_across_leaves(pager, pool) -> None:
    root, all_rowids = _build_three_leaf_tree(pager, pool)
    cur = TableCursor(pager, pool, root)
    cur.first()
    seen = [cur.rowid()]
    while cur.next():
        seen.append(cur.rowid())
    assert seen == all_rowids
    cur.close()




def test_prev_visits_every_rowid_in_reverse_across_leaves(pager, pool) -> None:
    root, all_rowids = _build_three_leaf_tree(pager, pool)
    cur = TableCursor(pager, pool, root)
    cur.last()
    seen = [cur.rowid()]
    while cur.prev():
        seen.append(cur.rowid())
    assert seen == list(reversed(all_rowids))
    cur.close()




def test_next_past_the_end_returns_false_and_invalidates(pager, pool) -> None:
    root, _ = _build_three_leaf_tree(pager, pool)
    cur = TableCursor(pager, pool, root)
    cur.last()
    assert cur.next() is False
    assert not cur.valid
    cur.close()




def test_prev_past_the_start_returns_false_and_invalidates(pager, pool) -> None:
    root, _ = _build_three_leaf_tree(pager, pool)
    cur = TableCursor(pager, pool, root)
    cur.first()
    assert cur.prev() is False
    assert not cur.valid
    cur.close()




def test_next_on_unpositioned_cursor_returns_false(pager, pool) -> None:
    root, _ = _build_three_leaf_tree(pager, pool)
    cur = TableCursor(pager, pool, root)
    assert cur.next() is False
    cur.close()




# =====================================================================
# seek
# =====================================================================




def test_seek_exact_match_returns_true(pager, pool) -> None:
    root, _ = _build_three_leaf_tree(pager, pool)
    cur = TableCursor(pager, pool, root)
    assert cur.seek(12) is True
    assert cur.rowid() == 12
    cur.close()




def test_seek_miss_returns_false_positioned_at_next_larger(pager, pool) -> None:
    root, _ = _build_three_leaf_tree(pager, pool)
    cur = TableCursor(pager, pool, root)
    assert cur.seek(7) is False
    assert cur.rowid() == 10
    cur.close()




def test_seek_then_next_continues_a_range_scan(pager, pool) -> None:
    """The idiom seek() exists for: WHERE rowid >= 12."""
    root, _ = _build_three_leaf_tree(pager, pool)
    cur = TableCursor(pager, pool, root)
    cur.seek(12)
    seen = [cur.rowid()]
    while cur.next():
        seen.append(cur.rowid())
    assert seen == [12, 15, 20, 22, 25]
    cur.close()




def test_seek_past_every_key_leaves_cursor_unpositioned(pager, pool) -> None:
    root, _ = _build_three_leaf_tree(pager, pool)
    cur = TableCursor(pager, pool, root)
    assert cur.seek(999) is False
    assert not cur.valid
    cur.close()




def test_seek_hops_to_next_leaf_when_landed_leaf_is_a_dead_end(pager, pool) -> None:
    """The separator-gap fixture: descent correctly lands on leaf_mid for key
    14, but leaf_mid's real max is 12 -- seek must hop onward to leaf_hi.
    """
    root = _build_tree_with_separator_gap(pager, pool)
    cur = TableCursor(pager, pool, root)
    assert cur.seek(14) is False
    assert cur.rowid() == 20
    cur.close()




# =====================================================================
# rowid / record
# =====================================================================




def test_rowid_raises_when_unpositioned(pager, pool) -> None:
    root, _ = _build_three_leaf_tree(pager, pool)
    cur = TableCursor(pager, pool, root)
    with pytest.raises(ValueError):
        cur.rowid()
    cur.close()




def test_record_raises_when_unpositioned(pager, pool) -> None:
    root, _ = _build_three_leaf_tree(pager, pool)
    cur = TableCursor(pager, pool, root)
    with pytest.raises(ValueError):
        cur.record()
    cur.close()




def test_record_returns_local_payload_when_it_never_spilled(pager, pool) -> None:
    root, _ = _build_three_leaf_tree(pager, pool)
    cur = TableCursor(pager, pool, root)
    cur.seek(12)
    assert cur.record() == b"row12"
    cur.close()




def test_record_reassembles_a_spilled_payload(pager, pool) -> None:
    payload = bytes((i * 7) % 256 for i in range(10_000))  # comfortably spills
    local_len = local_payload_size(PageType.LEAF_TABLE, len(payload))
    overflow_page = write_overflow_chain(pager, pool, payload[local_len:])
    cell = encode_leaf_table_cell(99, len(payload), payload[:local_len], overflow_page)
    root = _write_page(pager, pool, PageBody(PageType.LEAF_TABLE, cells=[cell]))


    cur = TableCursor(pager, pool, root)
    cur.first()
    assert cur.rowid() == 99
    assert cur.record() == payload
    cur.close()




# =====================================================================
# Pinning discipline (§6.3 point 1) -- the thing that's "easy to get wrong".
# =====================================================================




def test_close_releases_every_pin_on_the_path(pager, pool) -> None:
    small_pool = BufferPool(pager, capacity=3)
    root, _ = _build_three_leaf_tree(pager, small_pool)


    cur = TableCursor(pager, small_pool, root)
    cur.last()  # holds pins on root + leaf_hi
    cur.close()


    # Fully usable afterwards -- a leak would exhaust capacity=3 quickly.
    for _ in range(5):
        cur2 = TableCursor(pager, small_pool, root)
        cur2.first()
        cur2.close()




def test_context_manager_releases_pins_on_normal_exit(pager, pool) -> None:
    small_pool = BufferPool(pager, capacity=3)
    root, _ = _build_three_leaf_tree(pager, small_pool)


    with TableCursor(pager, small_pool, root) as cur:
        cur.first()
        assert cur.rowid() == 1


    with TableCursor(pager, small_pool, root) as cur:
        cur.last()
        assert cur.rowid() == 25




def test_context_manager_releases_pins_even_on_exception(pager, pool) -> None:
    small_pool = BufferPool(pager, capacity=3)
    root, _ = _build_three_leaf_tree(pager, small_pool)


    with pytest.raises(RuntimeError), TableCursor(pager, small_pool, root) as cur:
        cur.first()
        raise RuntimeError("boom")


    with TableCursor(pager, small_pool, root) as cur:
        cur.last()
        assert cur.rowid() == 25




def test_scanning_the_whole_tree_does_not_leak_pins(pager, pool) -> None:
    """A pool sized for exactly one path's depth (root + leaf = 2): if next()
    ever failed to unpin a level it popped, the very next cross-leaf hop
    would need a third slot and raise PoolExhaustedError mid-scan -- so
    simply finishing the scan is the proof.
    """
    small_pool = BufferPool(pager, capacity=2)
    root, all_rowids = _build_three_leaf_tree(pager, small_pool)


    with TableCursor(pager, small_pool, root) as cur:
        cur.first()
        seen = [cur.rowid()]
        while cur.next():
            seen.append(cur.rowid())
        assert seen == all_rowids



