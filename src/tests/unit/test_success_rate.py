"""Acceptance tests for Week 2's "success state" (roadmap.md): properties a
correct table b-tree must hold end-to-end, under real insert() traffic, not
just the individual pieces (cells, split, validate) already tested in
isolation elsewhere.


Deliberately NOT duplicated here, because each already has focused coverage:
    - overflow cycle raises OverflowCycleError: test_overflow.py, test_validate_btree.py
    - unsorted keys raise BTreeInvariantError: test_validate_btree.py
    - a cell pointer out of bounds raises MalformedCellError: test_page.py
    - a single 10KB payload round-trips through overflow: test_overflow.py,
      test_btree.py's test_insert_spills_an_oversized_payload_to_overflow


What's new here is scale and randomness: real insert() traffic (not hand-
built pages) exercised in the thousands-to-hundred-thousands, checked
against oracles (a dict, `sorted()`) instead of hand-picked expected values.
"""


import random

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from quilldb.btree.btree import BTree
from quilldb.btree.cells import decode_leaf_table_cell
from quilldb.btree.cursor import TableCursor
from quilldb.btree.validate import validate_btree
from quilldb.constants import PageType
from quilldb.storage.bufferpool import BufferPool
from quilldb.storage.overflow import read_overflow_chain
from quilldb.storage.page import PageBody, parse_page, serialize_page
from quilldb.storage.pager import Pager


def _new_tree(pager: Pager, pool: BufferPool) -> BTree:
    """A single empty leaf as a table root -- page 1 is reserved for
    sqlite_schema (§1.6) and never used as a bare table root, matching every
    other insert()-driven test in this codebase.
    """
    root = pager.allocate_page()
    with pool.pinned(root, dirty=True) as raw:
        raw[:] = serialize_page(PageBody(PageType.LEAF_TABLE))
    return BTree(pager, pool, root)




def _payload(rowid: int) -> bytes:
    return f"row-{rowid}".encode()




def _scan_rowids(pager: Pager, pool: BufferPool, root: int) -> list[int]:
    with TableCursor(pager, pool, root) as cursor:
        rowids = []
        cursor.first()
        while cursor.valid:
            rowids.append(cursor.rowid())
            cursor.next()
        return rowids




# =====================================================================
# Insertion order: ascending, descending, and random must all end up as a
# validator-clean tree whose scan order matches sorted(keys).
# =====================================================================




@pytest.mark.parametrize("order", ["ascending", "descending", "random"])
def test_every_insertion_order_produces_a_valid_tree(tmp_path, order: str) -> None:
    pager = Pager.create(tmp_path / f"{order}.db")
    try:
        pool = BufferPool(pager, capacity=64)
        bt = _new_tree(pager, pool)
        keys = list(range(1, 3001))
        if order == "descending":
            keys.reverse()
        elif order == "random":
            random.Random(0).shuffle(keys)


        for key in keys:
            bt.insert(key, _payload(key))


        validate_btree(pager, pool, bt.root)
        assert _scan_rowids(pager, pool, bt.root) == sorted(keys)
    finally:
        pager.close()




# =====================================================================
# scan() == sorted(set(keys)) -- property test over random key sets,
# instead of the three fixed shapes above.
# =====================================================================


_KEY_LISTS = st.lists(st.integers(min_value=-1_000_000, max_value=1_000_000), min_size=0, max_size=150, unique=True)




@given(keys=_KEY_LISTS)
@settings(deadline=None, max_examples=25)
def test_scan_matches_sorted_unique_keys(tmp_path_factory, keys: list[int]) -> None:
    path = tmp_path_factory.mktemp("scan") / "t.db"
    pager = Pager.create(path)
    try:
        pool = BufferPool(pager, capacity=64)
        bt = _new_tree(pager, pool)
        for key in keys:
            bt.insert(key, _payload(key))


        assert _scan_rowids(pager, pool, bt.root) == sorted(keys)
    finally:
        pager.close()




# =====================================================================
# Point lookups against a dict oracle -- both present AND absent keys.
# =====================================================================




def test_point_lookups_match_a_dict_oracle_for_present_and_absent_keys(tmp_path) -> None:
    rng = random.Random(1234)
    universe = rng.sample(range(-500_000, 500_000), 1000)
    present, absent = universe[:600], universe[600:]


    pager = Pager.create(tmp_path / "oracle.db")
    try:
        pool = BufferPool(pager, capacity=64)
        bt = _new_tree(pager, pool)
        oracle = {key: _payload(key) for key in present}
        for key, payload in oracle.items():
            bt.insert(key, payload)


        for key, expected_payload in oracle.items():
            found = bt.search(key)
            assert found is not None
            page_id, slot = found
            raw = pool.get_page(page_id)
            body = parse_page(raw)
            pool.unpin(page_id)
            rowid, total_len, local_payload, _overflow_page = decode_leaf_table_cell(body.cells[slot])
            assert (rowid, total_len, local_payload) == (key, len(expected_payload), expected_payload)


        for key in absent:
            assert bt.search(key) is None


        validate_btree(pager, pool, bt.root)
    finally:
        pager.close()




# =====================================================================
# Close/reopen durability: a tree built with real inserts (including one
# oversized, overflow-spilling payload) must read back correctly from a
# brand new Pager/BufferPool/BTree opened against the same file.
# =====================================================================




def test_tree_survives_close_and_reopen(tmp_path) -> None:
    path = tmp_path / "durable.db"
    pager = Pager.create(path)
    pool = BufferPool(pager, capacity=64)
    bt = _new_tree(pager, pool)


    keys = list(range(1, 501))
    for key in keys:
        bt.insert(key, _payload(key))
    big_payload = bytes((i * 7) % 256 for i in range(10_000))
    bt.insert(999_999, big_payload)
    root = bt.root


    pool.flush_all()
    pager.close()


    reopened_pager = Pager.open(path)
    try:
        reopened_pool = BufferPool(reopened_pager, capacity=64)
        validate_btree(reopened_pager, reopened_pool, root)
        assert _scan_rowids(reopened_pager, reopened_pool, root) == [*keys, 999_999]


        reopened_bt = BTree(reopened_pager, reopened_pool, root)
        page_id, slot = reopened_bt.search(999_999)
        raw = reopened_pool.get_page(page_id)
        body = parse_page(raw)
        reopened_pool.unpin(page_id)
        _, total_len, local_payload, overflow_page = decode_leaf_table_cell(body.cells[slot])
        rest = read_overflow_chain(reopened_pager, reopened_pool, overflow_page, total_len - len(local_payload))
        assert local_payload + rest == big_payload
    finally:
        reopened_pager.close()




# =====================================================================
# 100k random-order inserts -> a validator-clean tree. The roadmap's
# headline fuzz test -- slow (walks the whole tree, plus ~100k splits),
# so it's marked to skip a fast default run: `pytest -m slow` runs it.
# =====================================================================




@pytest.mark.slow
def test_100k_random_inserts_produce_a_validator_clean_tree(tmp_path) -> None:
    pager = Pager.create(tmp_path / "fuzz.db")
    try:
        pool = BufferPool(pager, capacity=256)
        bt = _new_tree(pager, pool)
        keys = list(range(1, 100_001))
        random.Random(42).shuffle(keys)


        for key in keys:
            bt.insert(key, _payload(key))


        validate_btree(pager, pool, bt.root)
        assert _scan_rowids(pager, pool, bt.root) == sorted(keys)
    finally:
        pager.close()