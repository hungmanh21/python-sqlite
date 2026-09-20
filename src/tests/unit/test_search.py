import pytest
from hypothesis import given
from hypothesis import strategies as st

from quilldb.btree.cells import encode_interior_table_cell, encode_leaf_table_cell
from quilldb.btree.search import (
    binary_search_leaf,
    child_for_key,
    children_of_interior,
    leaf_slot_lower_bound,
)
from quilldb.constants import PageType
from quilldb.storage.page import PageBody


def _leaf_cell(rowid: int) -> bytes:
    payload = f"row{rowid}".encode()
    return encode_leaf_table_cell(rowid, len(payload), payload)




def _leaf_page(rowids: list[int]) -> PageBody:
    return PageBody(PageType.LEAF_TABLE, cells=[_leaf_cell(r) for r in rowids])




def _interior_page(separators: list[tuple[int, int]], right_child: int) -> PageBody:
    """separators: [(child_page, separator_rowid), ...] in ascending order."""
    cells = [encode_interior_table_cell(child, sep) for child, sep in separators]
    return PageBody(PageType.INTERIOR_TABLE, cells=cells, right_child=right_child)




# =====================================================================
# binary_search_leaf -- hand-built pages, per §6.1's advice to test search
# in isolation from the parser and the I/O underneath it.
# =====================================================================




def test_finds_every_present_key() -> None:
    page = _leaf_page([10, 20, 30, 40, 50])
    for index, rowid in enumerate([10, 20, 30, 40, 50]):
        assert binary_search_leaf(page, rowid) == index




@pytest.mark.parametrize("key", [5, 15, 25, 35, 45, 55])
def test_returns_none_for_keys_between_or_outside(key: int) -> None:
    page = _leaf_page([10, 20, 30, 40, 50])
    assert binary_search_leaf(page, key) is None




def test_empty_page_returns_none() -> None:
    page = _leaf_page([])
    assert binary_search_leaf(page, 1) is None




def test_single_cell_hit_and_miss() -> None:
    page = _leaf_page([42])
    assert binary_search_leaf(page, 42) == 0
    assert binary_search_leaf(page, 41) is None




@given(st.lists(st.integers(min_value=0, max_value=10_000), min_size=0, max_size=50, unique=True))
def test_matches_linear_scan_oracle(rowids: list[int]) -> None:
    rowids = sorted(rowids)
    page = _leaf_page(rowids)
    for probe in [*rowids, -1, 10_001]:
        expected = rowids.index(probe) if probe in rowids else None
        assert binary_search_leaf(page, probe) == expected




# =====================================================================
# child_for_key -- the boundary condition is the point: a key equal to a
# separator must go LEFT (§6.1), and split's separator promotion depends on
# descent agreeing with this exactly.
# =====================================================================




def _standard_interior_page() -> PageBody:
    # keys <= 10 -> 100; 10 < keys <= 20 -> 200; 20 < keys <= 30 -> 300; keys > 30 -> 400
    return _interior_page([(100, 10), (200, 20), (300, 30)], right_child=400)




@pytest.mark.parametrize(
    ("key", "expected_child"),
    [
        (1, 100),
        (10, 100),  # boundary: key == separator goes LEFT
        (11, 200),
        (20, 200),  # boundary
        (21, 300),
        (30, 300),  # boundary
        (31, 400),
        (999, 400),
    ],
)
def test_boundary_and_range_routing(key: int, expected_child: int) -> None:
    assert child_for_key(_standard_interior_page(), key) == expected_child




def test_single_separator_page() -> None:
    page = _interior_page([(1, 5)], right_child=2)
    assert child_for_key(page, 5) == 1
    assert child_for_key(page, 6) == 2




@given(
    st.lists(st.integers(min_value=0, max_value=10_000), min_size=1, max_size=30, unique=True),
    st.integers(min_value=-1, max_value=10_001),
)
def test_matches_linear_scan_oracle_interior(separators: list[int], probe: int) -> None:
    separators = sorted(separators)
    # Child page numbers just need to be distinct and identifiable; index+1 works.
    page = _interior_page(
        [(i + 1, sep) for i, sep in enumerate(separators)], right_child=len(separators) + 1
    )


    expected = len(separators) + 1
    for i, sep in enumerate(separators):
        if probe <= sep:
            expected = i + 1
            break


    assert child_for_key(page, probe) == expected




# =====================================================================
# children_of_interior -- the enumeration TableCursor needs that
# child_for_key alone can't give it (§6.3's "children() helper").
# =====================================================================




def test_children_of_interior_is_cells_then_right_child() -> None:
    page = _standard_interior_page()
    assert children_of_interior(page) == [100, 200, 300, 400]




def test_children_of_interior_with_no_cells_is_just_right_child() -> None:
    page = PageBody(PageType.INTERIOR_TABLE, cells=[], right_child=42)
    assert children_of_interior(page) == [42]




# =====================================================================
# leaf_slot_lower_bound -- same binary search as binary_search_leaf, but it
# never discards the boundary on a miss (§6.1: "the same code path as
# seek()"). Matched against a linear-scan oracle, same as the other two.
# =====================================================================




@pytest.mark.parametrize(
    ("key", "expected_index"),
    [(5, 0), (10, 0), (11, 1), (20, 1), (21, 2), (30, 2), (31, 3), (999, 3)],
)
def test_leaf_slot_lower_bound_boundary_and_range(key: int, expected_index: int) -> None:
    page = _leaf_page([10, 20, 30])
    assert leaf_slot_lower_bound(page, key) == expected_index




def test_leaf_slot_lower_bound_empty_page_is_zero() -> None:
    assert leaf_slot_lower_bound(_leaf_page([]), 5) == 0




@given(st.lists(st.integers(min_value=0, max_value=10_000), min_size=0, max_size=50, unique=True))
def test_leaf_slot_lower_bound_matches_linear_scan_oracle(rowids: list[int]) -> None:
    rowids = sorted(rowids)
    page = _leaf_page(rowids)
    for probe in [*rowids, -1, 10_001]:
        expected = next((i for i, r in enumerate(rowids) if r >= probe), len(rowids))
        assert leaf_slot_lower_bound(page, probe) == expected