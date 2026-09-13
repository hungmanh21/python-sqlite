import pytest
from hypothesis import given
from hypothesis import strategies as st


from quilldb.btree.split import split_cells, split_interior_cells




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




# =====================================================================
# split_interior_cells: unlike split_cells, the separator is CONSUMED --
# one whole (child, key) pair disappears from both halves. Its key is
# promoted; its child survives as the left half's own right_child.
# =====================================================================




def _interior_cells_and_keys(
    seps: list[int], start_child: int = 100
) -> tuple[list[bytes], list[int], list[int]]:
    """Cheap stand-in cells -- split_interior_cells never decodes them,
    so their content doesn't matter, only that each one travels with its
    key and child intact. children[i] is arbitrary but distinct from
    keys[i], so a test mixing the two up would be caught immediately.
    """
    children = [start_child + i for i in range(len(seps))]
    cells = [f"cell(child={c},sep={s})".encode() for c, s in zip(children, seps)]
    return cells, seps, children




_RIGHT_CHILD = 999  # arbitrary, distinct from every start_child+i used above




def test_interior_split_near_the_middle_for_an_even_cell_count() -> None:
    cells, keys, children = _interior_cells_and_keys(list(range(1, 11)))  # 10 cells: 1..10
    left, left_right_child, right, right_right_child, separator = split_interior_cells(
        cells, keys, children, _RIGHT_CHILD, is_rightmost=False
    )
    m = len(left)
    assert left == cells[:m]
    assert right == cells[m + 1 :]
    assert separator == keys[m]
    assert left_right_child == children[m]
    assert right_right_child == _RIGHT_CHILD




def test_interior_split_near_the_middle_for_an_odd_cell_count() -> None:
    cells, keys, children = _interior_cells_and_keys(list(range(1, 10)))  # 9 cells: 1..9
    left, left_right_child, right, right_right_child, separator = split_interior_cells(
        cells, keys, children, _RIGHT_CHILD, is_rightmost=False
    )
    m = len(left)
    assert left == cells[:m]
    assert right == cells[m + 1 :]
    assert separator == keys[m]
    assert left_right_child == children[m]
    assert right_right_child == _RIGHT_CHILD




def test_interior_split_minimum_splittable_size_is_two_cells() -> None:
    cells, keys, children = _interior_cells_and_keys([1, 2])
    left, _, right, right_right_child, _ = split_interior_cells(
        cells, keys, children, _RIGHT_CHILD, is_rightmost=False
    )
    # Only 2 cells means exactly one is consumed -- whichever half keeps
    # the other ends up with 1 cell, the other with 0.
    assert len(left) + len(right) == 1
    assert right_right_child == _RIGHT_CHILD




def test_interior_split_fewer_than_two_cells_raises() -> None:
    with pytest.raises(ValueError):
        split_interior_cells([b"only"], [1], [100], _RIGHT_CHILD, is_rightmost=False)
    with pytest.raises(ValueError):
        split_interior_cells([], [], [], _RIGHT_CHILD, is_rightmost=False)




def test_interior_rightmost_split_promotes_only_the_last_cell() -> None:
    cells, keys, children = _interior_cells_and_keys(list(range(1, 11)))  # 10 cells
    left, left_right_child, right, right_right_child, separator = split_interior_cells(
        cells, keys, children, _RIGHT_CHILD, is_rightmost=True
    )
    assert left == cells[:9]
    assert right == []
    assert separator == keys[9]
    assert left_right_child == children[9]
    assert right_right_child == _RIGHT_CHILD




def test_interior_rightmost_split_of_the_minimum_two_cells() -> None:
    cells, keys, children = _interior_cells_and_keys([1, 2])
    left, left_right_child, right, right_right_child, separator = split_interior_cells(
        cells, keys, children, _RIGHT_CHILD, is_rightmost=True
    )
    assert left == cells[:1]
    assert right == []
    assert separator == keys[1]
    assert left_right_child == children[1]
    assert right_right_child == _RIGHT_CHILD




@given(_KEY_LISTS, st.booleans())
def test_interior_exactly_one_cell_is_consumed(keys: list[int], is_rightmost: bool) -> None:
    """left + [the consumed cell] + right must reconstruct `cells` exactly --
    nothing else added, dropped, or reordered.
    """
    cells, keys, children = _interior_cells_and_keys(sorted(keys))
    left, left_right_child, right, _, separator = split_interior_cells(
        cells, keys, children, _RIGHT_CHILD, is_rightmost
    )
    m = len(left)
    assert left + right == cells[:m] + cells[m + 1 :]
    assert left + [cells[m]] + right == cells
    assert separator == keys[m]
    assert left_right_child == children[m]




@given(_KEY_LISTS, st.booleans())
def test_interior_right_right_child_is_always_the_original(keys: list[int], is_rightmost: bool) -> None:
    cells, keys, children = _interior_cells_and_keys(sorted(keys))
    *_, right_right_child, _ = split_interior_cells(cells, keys, children, _RIGHT_CHILD, is_rightmost)
    assert right_right_child == _RIGHT_CHILD




@given(_KEY_LISTS)
def test_interior_rightmost_mode_always_leaves_right_cells_empty(keys: list[int]) -> None:
    cells, keys, children = _interior_cells_and_keys(sorted(keys))
    _, _, right, _, _ = split_interior_cells(cells, keys, children, _RIGHT_CHILD, is_rightmost=True)
    assert right == []




@given(_KEY_LISTS, st.booleans())
def test_interior_split_does_not_mutate_its_inputs(keys: list[int], is_rightmost: bool) -> None:
    cells, keys, children = _interior_cells_and_keys(sorted(keys))
    cells_before, keys_before, children_before = list(cells), list(keys), list(children)
    split_interior_cells(cells, keys, children, _RIGHT_CHILD, is_rightmost)
    assert cells == cells_before
    assert keys == keys_before
    assert children == children_before