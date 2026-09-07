import pytest
from hypothesis import given
from hypothesis import strategies as st

from quilldb.constants import PAGE_SIZE, PageType
from quilldb.errors import InvalidPageTypeError, MalformedCellError, PageFullError
from quilldb.storage.page import PageBody, parse_page, serialize_page


def test_empty_page_roundtrip() -> None:
    body = PageBody(page_type=PageType.LEAF_TABLE)
    assert parse_page(serialize_page(body)) == body




def test_serialize_is_exactly_page_size() -> None:
    body = PageBody(PageType.LEAF_TABLE, cells=[b"hello", b"world"])
    assert len(serialize_page(body)) == PAGE_SIZE




def test_cells_roundtrip_in_order() -> None:
    cells = [b"aaa", b"bb", b"cccc"]
    body = PageBody(PageType.LEAF_TABLE, cells=list(cells))
    assert parse_page(serialize_page(body)).cells == cells




def test_right_child_roundtrip() -> None:
    body = PageBody(PageType.INTERIOR_TABLE, cells=[b"x"], right_child=77)
    assert parse_page(serialize_page(body)).right_child == 77




@given(st.lists(st.binary(min_size=1, max_size=200), max_size=15))
def test_roundtrip_property(cells: list[bytes]) -> None:
    body = PageBody(PageType.LEAF_TABLE, cells=cells)
    assert parse_page(serialize_page(body)).cells == cells




@pytest.mark.parametrize("index", [0, 1, 2])
def test_insert_at_position(index: int) -> None:
    body = PageBody(PageType.LEAF_TABLE, cells=[b"a", b"c"])
    body.insert_cell(index, b"NEW")
    assert body.cells[index] == b"NEW"
    assert body.cell_count == 3




def test_delete_cell() -> None:
    body = PageBody(PageType.LEAF_TABLE, cells=[b"a", b"b", b"c"])
    body.delete_cell(1)
    assert body.cells == [b"a", b"c"]




def test_free_bytes_accounting() -> None:
    body = PageBody(PageType.LEAF_TABLE)
    empty = body.free_bytes()
    body.insert_cell(0, b"x" * 100)
    assert body.free_bytes() == empty - 100 - 2      # payload + pointer




def test_fits_accounts_for_pointer() -> None:
    """The classic off-by-two: a cell costs payload + 2 bytes of pointer."""
    body = PageBody(PageType.LEAF_TABLE)
    exact = body.free_bytes() - 2
    assert body.fits(exact)
    assert not body.fits(exact + 1)




def test_fill_page_exactly_then_overflow() -> None:
    body = PageBody(PageType.LEAF_TABLE)
    payload = b"x" * 100
    while body.fits(len(payload)):
        body.insert_cell(body.cell_count, payload)
    assert len(serialize_page(body)) == PAGE_SIZE     # exactly full is still valid
    with pytest.raises(PageFullError):
        body.insert_cell(body.cell_count, payload)




def test_delete_then_insert_reclaims_space() -> None:
    """Re-serializing must defragment — no space leaked by churn."""
    body = PageBody(PageType.LEAF_TABLE)
    while body.fits(100):
        body.insert_cell(body.cell_count, b"y" * 100)
    body.delete_cell(0)
    body = parse_page(serialize_page(body))
    assert body.fits(100)




def test_bad_page_type_rejected() -> None:
    data = bytearray(serialize_page(PageBody(PageType.LEAF_TABLE)))
    data[0] = 99
    with pytest.raises(InvalidPageTypeError):
        parse_page(bytes(data))




def test_cell_pointer_past_end_rejected() -> None:
    data = bytearray(serialize_page(PageBody(PageType.LEAF_TABLE, cells=[b"abc"])))
    data[8:10] = (PAGE_SIZE + 10).to_bytes(2, "big")
    with pytest.raises(MalformedCellError):
        parse_page(bytes(data))




def test_cell_pointer_into_header_rejected() -> None:
    data = bytearray(serialize_page(PageBody(PageType.LEAF_TABLE, cells=[b"abc"])))
    data[8:10] = (3).to_bytes(2, "big")
    with pytest.raises(MalformedCellError):
        parse_page(bytes(data))




def test_absurd_cell_count_rejected() -> None:
    data = bytearray(serialize_page(PageBody(PageType.LEAF_TABLE)))
    data[3:5] = (5000).to_bytes(2, "big")            # more pointers than fit
    with pytest.raises(MalformedCellError):
        parse_page(bytes(data))