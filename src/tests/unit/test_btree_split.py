"""Acceptance tests for table-leaf splits and separator promotion."""


import pytest


from quilldb.btree.btree import BTree
from quilldb.btree.cells import (
    decode_interior_table_cell,
    decode_leaf_table_cell,
    encode_interior_table_cell,
    encode_leaf_table_cell,
)
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




def _read_page(pool: BufferPool, page_id: int) -> PageBody:
    raw = pool.get_page(page_id)
    try:
        return parse_page(raw)
    finally:
        pool.unpin(page_id)




def _rowids(body: PageBody) -> list[int]:
    return [decode_leaf_table_cell(cell)[0] for cell in body.cells]




def _full_leaf(start_rowid: int) -> tuple[PageBody, int]:
    """Return a packed leaf and the first rowid that will not fit in it."""
    body = PageBody(PageType.LEAF_TABLE)
    rowid = start_rowid
    while body.fits(len(_leaf_cell(rowid))):
        body.insert_cell(body.cell_count, _leaf_cell(rowid))
        rowid += 1
    return body, rowid




def test_root_leaf_split_keeps_root_page_and_promotes_left_maximum(pager, pool) -> None:
    """A root leaf becomes an interior root with two ordered leaf children."""
    full_leaf, inserted_rowid = _full_leaf(1)
    root = _write_page(pager, pool, full_leaf)
    tree = BTree(pager, pool, root)


    tree.insert(inserted_rowid, _payload(inserted_rowid))


    root_body = _read_page(pool, root)
    assert root_body.page_type is PageType.INTERIOR_TABLE
    assert len(root_body.cells) == 1


    left_page, separator = decode_interior_table_cell(root_body.cells[0])
    right_page = root_body.right_child
    assert left_page != root
    assert right_page != root


    left_rowids = _rowids(_read_page(pool, left_page))
    right_rowids = _rowids(_read_page(pool, right_page))
    assert separator == left_rowids[-1]
    assert left_rowids[-1] < right_rowids[0]
    assert left_rowids + right_rowids == list(range(1, inserted_rowid + 1))


    for rowid in range(1, inserted_rowid + 1):
        assert tree.search(rowid) is not None




def test_leaf_split_promotes_separator_into_existing_parent(pager, pool) -> None:
    """Splitting a right child adds its left maximum to the parent."""
    left_leaf = _write_page(pager, pool, PageBody(PageType.LEAF_TABLE, cells=[_leaf_cell(1)]))
    full_right_leaf, inserted_rowid = _full_leaf(100)
    right_leaf = _write_page(pager, pool, full_right_leaf)
    root = _write_page(
        pager,
        pool,
        PageBody(
            PageType.INTERIOR_TABLE,
            cells=[encode_interior_table_cell(left_leaf, 1)],
            right_child=right_leaf,
        ),
    )
    tree = BTree(pager, pool, root)


    tree.insert(inserted_rowid, _payload(inserted_rowid))


    root_body = _read_page(pool, root)
    assert root_body.page_type is PageType.INTERIOR_TABLE
    assert len(root_body.cells) == 2


    first_child, first_separator = decode_interior_table_cell(root_body.cells[0])
    split_left_page, promoted_separator = decode_interior_table_cell(root_body.cells[1])
    split_right_page = root_body.right_child
    assert (first_child, first_separator) == (left_leaf, 1)
    assert split_left_page == right_leaf


    split_left_rowids = _rowids(_read_page(pool, split_left_page))
    split_right_rowids = _rowids(_read_page(pool, split_right_page))
    assert promoted_separator == split_left_rowids[-1]
    assert split_left_rowids[-1] < split_right_rowids[0]


    expected_rowids = [1, *range(100, inserted_rowid + 1)]
    for rowid in expected_rowids:
        assert tree.search(rowid) is not None