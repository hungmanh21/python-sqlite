import pytest

from quilldb.btree.cells import (
    decode_interior_index_cell,
    encode_interior_index_cell,
    encode_leaf_index_cell,
)
from quilldb.btree.index import IndexBTree, compare_keys, encode_index_key
from quilldb.codec.record import decode_record
from quilldb.constants import PageType
from quilldb.storage.bufferpool import BufferPool
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




def _write_page(pager: Pager, pool: BufferPool, body: PageBody) -> int:
    page_id = pager.allocate_page()
    with pool.pinned(page_id, dirty=True) as raw:
        raw[:] = serialize_page(body)
    return page_id




def _new_index_root(pager: Pager, pool: BufferPool) -> int:
    return _write_page(pager, pool, PageBody(PageType.LEAF_INDEX))




@pytest.fixture
def index(pager, pool):
    return IndexBTree(pager, pool, _new_index_root(pager, pool), n_key_columns=1, unique=False)




@pytest.fixture
def index2(pager, pool):
    return IndexBTree(pager, pool, _new_index_root(pager, pool), n_key_columns=2, unique=False)




@pytest.fixture
def uniq(pager, pool):
    return IndexBTree(pager, pool, _new_index_root(pager, pool), n_key_columns=1, unique=True)




def _leaf_index_cell(values: list, rowid: int) -> bytes:
    payload = encode_index_key(values, rowid)
    return encode_leaf_index_cell(len(payload), payload)




def _interior_index_cell(child_page: int, values: list, rowid: int) -> bytes:
    payload = encode_index_key(values, rowid)
    return encode_interior_index_cell(child_page, len(payload), payload)




def _fixed_width_leaf_cell(n: int) -> bytes:
    """A leaf cell whose key is fixed-width ("k000042") so cell size stays
    uniform while a leaf/interior page is packed toward full -- mirrors
    test_btree_splits.py's _full_leaf, generalized to index cells.
    """
    return _leaf_index_cell([f"k{n:06d}"], n)




def _full_index_leaf(start: int) -> tuple[PageBody, int]:
    """A packed LEAF_INDEX page plus the first key that will not fit in it."""
    body = PageBody(PageType.LEAF_INDEX)
    n = start
    while body.fits(len(_fixed_width_leaf_cell(n))):
        body.insert_cell(body.cell_count, _fixed_width_leaf_cell(n))
        n += 1
    return body, n




def _full_index_interior(right_child: int, start_child: int = 10_000) -> tuple[PageBody, int]:
    """A packed INTERIOR_INDEX page (given right_child) plus the first fake
    child that would not fit. Fake separators are "0"-prefixed so they sort
    below every real "k......" key used in these tests -- real lookups never
    dereference these placeholder children.
    """
    body = PageBody(PageType.INTERIOR_INDEX, right_child=right_child)
    child = start_child
    while True:
        cell = _interior_index_cell(child, [f"0{child:06d}"], child)
        if not body.fits(len(cell)):
            return body, child
        body.insert_cell(body.cell_count, cell)
        child += 1




def _build_two_leaf_index(pager: Pager, pool: BufferPool) -> tuple[IndexBTree, int, int, int]:
    """Root -> {leaf_a ["A"], divider ["a"], leaf_b ["b"] via right_child} --
    hand-built, never through insert(), so a delete-cascade failure here is
    unambiguous.


    Three entries, not two: the root's divider ("a", 1) is itself one of
    them (SS11.6) and so must NOT be repeated in the leaf below it, which
    therefore carries a strictly smaller key of its own.
    """
    leaf_a = _write_page(pager, pool, PageBody(PageType.LEAF_INDEX, cells=[_leaf_index_cell(["A"], 0)]))
    leaf_b = _write_page(pager, pool, PageBody(PageType.LEAF_INDEX, cells=[_leaf_index_cell(["b"], 2)]))
    root = _write_page(
        pager,
        pool,
        PageBody(
            PageType.INTERIOR_INDEX,
            cells=[_interior_index_cell(leaf_a, ["a"], 1)],
            right_child=leaf_b,
        ),
    )
    return IndexBTree(pager, pool, root, n_key_columns=1, unique=False), leaf_a, leaf_b, root




# =====================================================================
# encode_index_key
# =====================================================================




@pytest.mark.parametrize("rowid", [0, 1, 162, 2**40, 2**63 - 1, -1, -(2**63)])
def test_index_key_roundtrips_any_rowid(rowid) -> None:
    key = encode_index_key(["bb1", "cc1"], rowid)
    assert decode_record(key) == ("bb1", "cc1", rowid)




def test_rowid_zero_and_one_cost_no_body_bytes() -> None:
    """Proves the rowid is a record field, not a varint (§11.4)."""
    assert len(encode_index_key([], 0)) == len(encode_index_key([], 1))
    assert len(encode_index_key([], 0)) < len(encode_index_key([], 2))




# =====================================================================
# compare_keys
# =====================================================================




def test_compare_keys_null_orders_before_numeric_text_and_blob() -> None:
    assert compare_keys([None], [0]) == -1
    assert compare_keys([0], ["a"]) == -1
    assert compare_keys(["a"], [b"a"]) == -1




def test_compare_keys_numeric_class_spans_int_and_float() -> None:
    assert compare_keys([4], [4.5]) == -1
    assert compare_keys([5], [4.5]) == 1
    assert compare_keys([4], [4.0]) == 0




def test_compare_keys_compares_only_the_shorter_length() -> None:
    """The prefix rule seek_eq (and descent) depends on."""
    assert compare_keys(["x"], ["x", 999]) == 0
    assert compare_keys(["x", 999], ["x"]) == 0




def test_compare_keys_text_is_binary_byte_order() -> None:
    assert compare_keys(["Hanoi"], ["Hue"]) == -1
    assert compare_keys(["b"], ["a"]) == 1
    assert compare_keys(["a"], ["a"]) == 0




# =====================================================================
# insert / seek_eq -- the duplicates behavior the roadmap flags as the
# trap: unique test data can't tell "one match" from "the whole run".
# =====================================================================




def test_seek_eq_returns_all_duplicates_in_rowid_order(index) -> None:
    for rowid in [17, 92, 415, 3]:
        index.insert(["Hanoi"], rowid)
    index.insert(["Hue"], 50)
    assert list(index.seek_eq(["Hanoi"])) == [3, 17, 92, 415]




def test_seek_eq_on_prefix_of_composite_index(index2) -> None:
    index2.insert(["x", "1"], 1)
    index2.insert(["x", "2"], 2)
    index2.insert(["y", "1"], 3)
    assert list(index2.seek_eq(["x"])) == [1, 2]




def test_seek_eq_returns_nothing_for_an_absent_key(index) -> None:
    index.insert(["Hanoi"], 1)
    assert list(index.seek_eq(["Da Nang"])) == []




def test_insert_persists_a_spilled_key(pager, pool) -> None:
    idx = IndexBTree(pager, pool, _new_index_root(pager, pool), n_key_columns=1, unique=False)
    long_value = "x" * 10_000
    idx.insert([long_value], 1)
    assert list(idx.seek_eq([long_value])) == [1]




# =====================================================================
# insert -- splitting a full leaf (module docstring's one-level-split
# scope cut: a full parent raises rather than cascading further up).
# =====================================================================




def test_insert_splits_a_full_root_leaf_and_promotes_the_left_maximum(pager, pool) -> None:
    full_leaf, next_n = _full_index_leaf(0)
    root = _write_page(pager, pool, full_leaf)
    idx = IndexBTree(pager, pool, root, n_key_columns=1, unique=False)


    idx.insert([f"k{next_n:06d}"], next_n)


    raw = pool.get_page(root)
    try:
        root_body = parse_page(raw)
    finally:
        pool.unpin(root)
    assert root_body.page_type is PageType.INTERIOR_INDEX
    assert len(root_body.cells) == 1


    left_page, *_ = decode_interior_index_cell(root_body.cells[0])
    right_page = root_body.right_child
    assert left_page != root
    assert right_page != root


    assert list(idx.scan()) == [([f"k{n:06d}"], n) for n in range(next_n + 1)]
    for n in range(next_n + 1):
        assert list(idx.seek_eq([f"k{n:06d}"])) == [n]




def test_insert_splits_a_leaf_and_promotes_into_an_existing_parent(pager, pool) -> None:
    """Note the fixture: the root's separator ("a", -1) does NOT also appear
    in the leaf below it. An index interior cell is a live entry (SS11.6), so
    a hand-built tree that repeats its separator downstairs is malformed --
    it is the very shape a copy-up leaf split would wrongly produce. The left
    leaf therefore holds a strictly smaller key of its own.
    """
    left_leaf = _write_page(pager, pool, PageBody(PageType.LEAF_INDEX, cells=[_leaf_index_cell(["A"], -2)]))
    full_right_leaf, next_n = _full_index_leaf(0)
    right_leaf = _write_page(pager, pool, full_right_leaf)
    root = _write_page(
        pager,
        pool,
        PageBody(
            PageType.INTERIOR_INDEX,
            cells=[_interior_index_cell(left_leaf, ["a"], -1)],
            right_child=right_leaf,
        ),
    )
    idx = IndexBTree(pager, pool, root, n_key_columns=1, unique=False)


    idx.insert([f"k{next_n:06d}"], next_n)


    raw = pool.get_page(root)
    try:
        root_body = parse_page(raw)
    finally:
        pool.unpin(root)
    assert root_body.page_type is PageType.INTERIOR_INDEX
    assert len(root_body.cells) == 2


    first_child, *_ = decode_interior_index_cell(root_body.cells[0])
    split_left_page, *_ = decode_interior_index_cell(root_body.cells[1])
    assert first_child == left_leaf
    assert split_left_page == right_leaf
    assert root_body.right_child not in (left_leaf, right_leaf, root)


    # In-order: left leaf, then the root's own entry, then the right subtree.
    expected = [(["A"], -2), (["a"], -1)] + [([f"k{n:06d}"], n) for n in range(next_n + 1)]
    assert list(idx.scan()) == expected




def test_insert_cascades_when_the_parent_has_no_room_for_the_promoted_separator(pager, pool) -> None:
    """A full leaf under a FULL parent used to be the ceiling: insert()
    raised, and an index could never grow past two levels. _promote_separator
    now splits the parent instead, and when that parent is the root the tree
    gains a level -- with the root keeping its page number, because
    sqlite_schema records it.
    """
    full_leaf, next_n = _full_index_leaf(0)
    leaf_page = _write_page(pager, pool, full_leaf)
    full_parent, _ = _full_index_interior(leaf_page)
    root = _write_page(pager, pool, full_parent)
    idx = IndexBTree(pager, pool, root, n_key_columns=1, unique=False)


    idx.insert([f"k{next_n:06d}"], next_n)  # must not raise


    assert idx.root == root  # the root page number never moves


    raw = pool.get_page(root)
    try:
        root_body = parse_page(raw)
    finally:
        pool.unpin(root)


    # The root split: it is now a 1-cell interior page whose two children
    # are BOTH freshly allocated pages holding its former content.
    assert root_body.page_type is PageType.INTERIOR_INDEX
    assert len(root_body.cells) == 1
    new_left, *_ = decode_interior_index_cell(root_body.cells[0])
    assert new_left not in (root, leaf_page)
    assert root_body.right_child not in (root, new_left)


    # ...and the level below the root is now interior, not leaf: depth grew.
    raw = pool.get_page(new_left)
    try:
        assert parse_page(raw).page_type is PageType.INTERIOR_INDEX
    finally:
        pool.unpin(new_left)




# =====================================================================
# find_conflict
# =====================================================================




def test_unique_allows_many_nulls(uniq) -> None:
    uniq.insert([None], 1)
    uniq.insert([None], 2)  # must not raise
    assert uniq.find_conflict([None]) is None




def test_find_conflict_reports_the_conflicting_rowid(uniq) -> None:
    uniq.insert(["a@b.com"], 7)
    assert uniq.find_conflict(["a@b.com"]) == 7
    assert uniq.find_conflict(["nope@b.com"]) is None




# =====================================================================
# delete
# =====================================================================




def test_delete_removes_only_the_named_entry(index) -> None:
    for rowid in [17, 92, 415]:
        index.insert(["Hanoi"], rowid)
    assert index.delete(["Hanoi"], 92) is True
    assert list(index.seek_eq(["Hanoi"])) == [17, 415]
    assert index.delete(["Hanoi"], 92) is False




def test_delete_last_entry_collapses_root_to_an_empty_leaf(index) -> None:
    index.insert(["only"], 1)
    assert index.delete(["only"], 1) is True
    assert list(index.scan()) == []


    raw = index.pool.get_page(index.root)
    try:
        body = parse_page(raw)
    finally:
        index.pool.unpin(index.root)
    assert body.page_type is PageType.LEAF_INDEX
    assert body.cells == []




def test_delete_emptying_a_leaf_merges_and_shrinks_the_tree(pager, pool) -> None:
    """Emptying leaf_a must not destroy its divider ("a", 1) along with the
    parent cell -- the divider descends into sibling leaf_b instead. The
    root is then an interior page with no cells left, which is a level the
    tree no longer needs, so its only child is pulled up INTO the root page
    (the root's page number is recorded in sqlite_schema and cannot move).


    Net effect: three entries minus one leaves two, which fit a single
    page, so the index is a one-page leaf root again -- and both former
    leaves are back on the freelist.
    """
    idx, leaf_a, leaf_b, root = _build_two_leaf_index(pager, pool)


    assert idx.delete(["A"], 0) is True
    assert list(idx.seek_eq(["A"])) == []
    assert list(idx.scan()) == [(["a"], 1), (["b"], 2)]


    raw = pool.get_page(root)
    try:
        root_body = parse_page(raw)
    finally:
        pool.unpin(root)


    assert idx.root == root                       # the root page number never moves
    assert root_body.page_type is PageType.LEAF_INDEX
    assert len(root_body.cells) == 2              # both surviving entries live here now
    assert root_body.right_child == 0


    # leaf_a was merged away and leaf_b was pulled up, so both are reusable.
    assert {pool.allocate_page(), pool.allocate_page()} == {leaf_a, leaf_b}




# =====================================================================
# scan / seek_range
# =====================================================================




def test_scan_returns_every_entry_in_key_order(index) -> None:
    index.insert(["b"], 2)
    index.insert(["a"], 1)
    index.insert(["c"], 3)
    assert list(index.scan()) == [(["a"], 1), (["b"], 2), (["c"], 3)]




def test_seek_range_respects_inclusive_bounds(index) -> None:
    for value, rowid in [("a", 1), ("b", 2), ("c", 3), ("d", 4)]:
        index.insert([value], rowid)


    assert list(index.seek_range(["b"], ["c"])) == [2, 3]
    assert list(index.seek_range(["b"], ["c"], low_inclusive=False)) == [3]
    assert list(index.seek_range(["b"], ["c"], high_inclusive=False)) == [2]
    assert list(index.seek_range(low=["c"])) == [3, 4]
    assert list(index.seek_range(high=["b"])) == [1, 2]