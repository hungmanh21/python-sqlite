import pytest
from hypothesis import given
from hypothesis import strategies as st


from quilldb.btree.split import split_cells




def _cells_and_keys(keys: list[int]) -> tuple[list[bytes], list[int]]:
    """Cheap stand-in cells -- split_cells never decodes them, so their
    content doesn't matter, only that each one travels with its key intact.
    """
    return [f"cell{k}".encode() for k in keys], keys




# =====================================================================
# The middle case: roughly half and half, left-biased on an odd count.
# =====================================================================




def test_splits_evenly_for_an_even_count() -> None:
    cells, keys = _cells_and_keys(list(range(1, 11)))  # 10 cells: 1..10
    left, right, separator = split_cells(cells, keys, is_rightmost=False)
    assert left == cells[:5]
    assert right == cells[5:]
    assert separator == 5




def test_left_half_gets_the_extra_cell_on_an_odd_count() -> None:
    cells, keys = _cells_and_keys(list(range(1, 10)))  # 9 cells: 1..9
    left, right, separator = split_cells(cells, keys, is_rightmost=False)
    assert left == cells[:5]
    assert right == cells[5:]
    assert separator == 5




def test_minimum_splittable_size_is_two_cells() -> None:
    cells, keys = _cells_and_keys([1, 2])
    left, right, separator = split_cells(cells, keys, is_rightmost=False)
    assert (left, right, separator) == ([cells[0]], [cells[1]], 1)




def test_fewer_than_two_cells_raises() -> None:
    with pytest.raises(ValueError):
        split_cells([b"only"], [1], is_rightmost=False)
    with pytest.raises(ValueError):
        split_cells([], [], is_rightmost=False)




# =====================================================================
# The rightmost optimisation (chapter 05 §5.7): peel off just the last
# cell, not roughly half -- the append-only fast path.
# =====================================================================




def test_rightmost_split_moves_only_the_last_cell() -> None:
    cells, keys = _cells_and_keys(list(range(1, 11)))  # 10 cells
    left, right, separator = split_cells(cells, keys, is_rightmost=True)
    assert left == cells[:9]
    assert right == cells[9:]
    assert separator == 9




def test_rightmost_split_of_the_minimum_two_cells() -> None:
    cells, keys = _cells_and_keys([1, 2])
    left, right, separator = split_cells(cells, keys, is_rightmost=True)
    assert (left, right, separator) == ([cells[0]], [cells[1]], 1)




# =====================================================================
# Invariants that must hold regardless of size or mode -- checked with
# Hypothesis instead of hand-picked cases, per §6.2's "get a source of
# truth that doesn't depend on the code under test."
# =====================================================================


_KEY_LISTS = st.lists(st.integers(min_value=0, max_value=10_000), min_size=2, max_size=50, unique=True)




@given(_KEY_LISTS, st.booleans())
def test_left_and_right_reconstruct_the_original_cells_in_order(keys: list[int], is_rightmost: bool) -> None:
    cells, keys = _cells_and_keys(sorted(keys))
    left, right, _ = split_cells(cells, keys, is_rightmost)
    assert left + right == cells




@given(_KEY_LISTS, st.booleans())
def test_separator_is_the_largest_key_in_the_left_half(keys: list[int], is_rightmost: bool) -> None:
    cells, keys = _cells_and_keys(sorted(keys))
    left, _, separator = split_cells(cells, keys, is_rightmost)
    assert separator == keys[len(left) - 1]




@given(_KEY_LISTS)
def test_rightmost_mode_always_peels_off_exactly_one_cell(keys: list[int]) -> None:
    cells, keys = _cells_and_keys(sorted(keys))
    _, right, _ = split_cells(cells, keys, is_rightmost=True)
    assert len(right) == 1




@given(_KEY_LISTS, st.booleans())
def test_does_not_mutate_its_inputs(keys: list[int], is_rightmost: bool) -> None:
    cells, keys = _cells_and_keys(sorted(keys))
    cells_before, keys_before = list(cells), list(keys)
    split_cells(cells, keys, is_rightmost)
    assert cells == cells_before
    assert keys == keys_before