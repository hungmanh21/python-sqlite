import pytest
from hypothesis import given
from hypothesis import strategies as st


from quilldb.btree.cells import (
    MIN_LOCAL_PAYLOAD,
    decode_interior_index_cell,
    decode_interior_table_cell,
    decode_leaf_index_cell,
    decode_leaf_table_cell,
    encode_interior_index_cell,
    encode_interior_table_cell,
    encode_leaf_index_cell,
    encode_leaf_table_cell,
    local_payload_size,
    max_local_payload,
)
from quilldb.codec.varint import encode_uvarint, to_twos_complement
from quilldb.constants import USABLE_SIZE, PageType
from quilldb.errors import MalformedCellError


I64_MIN = -(2**63)
I64_MAX = 2**63 - 1


# =====================================================================
# Threshold math (§6.5) -- pin the exact numbers the theory chapter derives
# for USABLE_SIZE == 4096, so a formula typo is caught immediately rather
# than only showing up as a round-trip failure much later.
# =====================================================================




def test_max_local_payload_table_leaf_is_usable_size_minus_35() -> None:
    assert max_local_payload(PageType.LEAF_TABLE) == USABLE_SIZE - 35




@pytest.mark.parametrize("page_type", [PageType.LEAF_INDEX, PageType.INTERIOR_INDEX])
def test_max_local_payload_index_pages_use_the_stricter_formula(page_type) -> None:
    assert max_local_payload(page_type) == ((USABLE_SIZE - 12) * 64 // 255) - 23




def test_max_local_payload_rejects_interior_table() -> None:
    with pytest.raises(ValueError):
        max_local_payload(PageType.INTERIOR_TABLE)




def test_min_local_payload_matches_formula() -> None:
    assert MIN_LOCAL_PAYLOAD == ((USABLE_SIZE - 12) * 32 // 255) - 23




def test_local_payload_size_returns_p_when_it_already_fits() -> None:
    x = max_local_payload(PageType.LEAF_TABLE)
    assert local_payload_size(PageType.LEAF_TABLE, 0) == 0
    assert local_payload_size(PageType.LEAF_TABLE, x) == x




@given(st.integers(min_value=0, max_value=200_000))
def test_local_payload_size_never_exceeds_max_local(total_len: int) -> None:
    x = max_local_payload(PageType.LEAF_TABLE)
    assert local_payload_size(PageType.LEAF_TABLE, total_len) <= x




@given(st.integers(min_value=0, max_value=200_000))
def test_local_payload_size_never_below_min_local_once_it_spills(total_len: int) -> None:
    """MIN_LOCAL_PAYLOAD is a floor on the K/M decision, not on the whole
    function -- a payload that fits locally (no spill) can be shorter than
    MIN_LOCAL_PAYLOAD, e.g. total_len=0."""
    x = max_local_payload(PageType.LEAF_TABLE)
    if total_len > x:
        assert local_payload_size(PageType.LEAF_TABLE, total_len) >= MIN_LOCAL_PAYLOAD




@given(st.integers(min_value=0, max_value=200_000))
def test_overflow_spill_always_fills_overflow_pages_exactly(total_len: int) -> None:
    """Idea 3 (§6.5): the local amount is chosen so the spilled remainder
    divides evenly into (USABLE_SIZE - 4)-byte overflow pages -- unless the
    M fallback kicked in, in which case a partial last page is expected.
    """
    x = max_local_payload(PageType.LEAF_TABLE)
    local = local_payload_size(PageType.LEAF_TABLE, total_len)
    spilled = total_len - local
    if spilled <= 0:
        return
    if local == MIN_LOCAL_PAYLOAD and local != total_len:
        # the M fallback: only guaranteed when the ideal K would exceed X
        expected_k = MIN_LOCAL_PAYLOAD + ((total_len - MIN_LOCAL_PAYLOAD) % (USABLE_SIZE - 4))
        assert expected_k > x
    else:
        assert spilled % (USABLE_SIZE - 4) == 0




# =====================================================================
# TABLE LEAF cell
# =====================================================================




def test_leaf_table_cell_round_trips_without_overflow() -> None:
    encoded = encode_leaf_table_cell(rowid=42, total_payload_len=5, local_payload=b"hello")
    rowid, total_len, local, overflow_page = decode_leaf_table_cell(encoded)
    assert (rowid, total_len, local, overflow_page) == (42, 5, b"hello", 0)




def test_leaf_table_cell_round_trips_with_overflow() -> None:
    x = max_local_payload(PageType.LEAF_TABLE)
    total_len = x + 500
    local_len = local_payload_size(PageType.LEAF_TABLE, total_len)
    local_payload = b"a" * local_len


    encoded = encode_leaf_table_cell(
        rowid=7, total_payload_len=total_len, local_payload=local_payload, overflow_page=91
    )
    rowid, decoded_len, local, overflow_page = decode_leaf_table_cell(encoded)
    assert (rowid, decoded_len, local, overflow_page) == (7, total_len, local_payload, 91)




def test_leaf_table_cell_rejects_negative_rowid_range() -> None:
    # -2**63 and 2**63 - 1 are the legal extremes; both must round-trip.
    for rowid in (I64_MIN, I64_MAX, -1, 0):
        encoded = encode_leaf_table_cell(rowid=rowid, total_payload_len=3, local_payload=b"abc")
        decoded_rowid, *_ = decode_leaf_table_cell(encoded)
        assert decoded_rowid == rowid




def test_leaf_table_cell_hand_built_fixture() -> None:
    """Construct the bytes independently of encode_leaf_table_cell so a
    decode bug can't be masked by a matching encode bug (§6.2's isolation
    technique).
    """
    cell = encode_uvarint(3) + encode_uvarint(to_twos_complement(99)) + b"xyz"
    rowid, total_len, local, overflow_page = decode_leaf_table_cell(cell)
    assert (rowid, total_len, local, overflow_page) == (99, 3, b"xyz", 0)




def test_leaf_table_cell_field_order_is_length_then_rowid() -> None:
    """§6.0: swapping this order is the classic bug. A payload length of 3
    with rowid 500 must NOT be misreadable as payload length 500."""
    cell = encode_leaf_table_cell(rowid=500, total_payload_len=3, local_payload=b"abc")
    length_varint_first_byte = cell[0]
    assert length_varint_first_byte == 3  # 3 fits in one byte, unambiguous




def test_encode_leaf_table_cell_rejects_local_payload_of_wrong_length() -> None:
    with pytest.raises(ValueError):
        encode_leaf_table_cell(rowid=1, total_payload_len=10, local_payload=b"short")




def test_encode_leaf_table_cell_rejects_missing_overflow_pointer() -> None:
    x = max_local_payload(PageType.LEAF_TABLE)
    total_len = x + 500
    local_len = local_payload_size(PageType.LEAF_TABLE, total_len)
    with pytest.raises(ValueError):
        encode_leaf_table_cell(rowid=1, total_payload_len=total_len, local_payload=b"a" * local_len)




def test_decode_leaf_table_cell_rejects_truncated_cell() -> None:
    encoded = encode_leaf_table_cell(rowid=1, total_payload_len=5, local_payload=b"hello")
    with pytest.raises(MalformedCellError):
        decode_leaf_table_cell(encoded[:-2])  # payload cut short




@given(
    rowid=st.integers(min_value=I64_MIN, max_value=I64_MAX),
    payload=st.binary(min_size=0, max_size=50),
)
def test_leaf_table_cell_round_trips_property(rowid: int, payload: bytes) -> None:
    encoded = encode_leaf_table_cell(rowid=rowid, total_payload_len=len(payload), local_payload=payload)
    decoded = decode_leaf_table_cell(encoded)
    assert decoded == (rowid, len(payload), payload, 0)




# =====================================================================
# TABLE INTERIOR cell -- fixed-width child pointer, no payload, ever
# =====================================================================




def test_interior_table_cell_round_trips() -> None:
    encoded = encode_interior_table_cell(child_page=17, rowid=1000)
    assert decode_interior_table_cell(encoded) == (17, 1000)




def test_interior_table_cell_child_pointer_is_first_four_bytes() -> None:
    encoded = encode_interior_table_cell(child_page=0x01020304, rowid=1)
    assert encoded[0:4] == bytes([0x01, 0x02, 0x03, 0x04])




def test_decode_interior_table_cell_rejects_truncated_cell() -> None:
    with pytest.raises(MalformedCellError):
        decode_interior_table_cell(b"\x00\x01")




@given(
    child_page=st.integers(min_value=1, max_value=2**32 - 1),
    rowid=st.integers(min_value=I64_MIN, max_value=I64_MAX),
)
def test_interior_table_cell_round_trips_property(child_page: int, rowid: int) -> None:
    encoded = encode_interior_table_cell(child_page=child_page, rowid=rowid)
    assert decode_interior_table_cell(encoded) == (child_page, rowid)




# =====================================================================
# INDEX LEAF cell -- no rowid; the payload itself is the key
# =====================================================================




def test_leaf_index_cell_round_trips_without_overflow() -> None:
    encoded = encode_leaf_index_cell(total_payload_len=5, local_payload=b"hello")
    assert decode_leaf_index_cell(encoded) == (5, b"hello", 0)




def test_leaf_index_cell_round_trips_with_overflow() -> None:
    x = max_local_payload(PageType.LEAF_INDEX)
    total_len = x + 200
    local_len = local_payload_size(PageType.LEAF_INDEX, total_len)
    local_payload = b"b" * local_len


    encoded = encode_leaf_index_cell(total_payload_len=total_len, local_payload=local_payload, overflow_page=55)
    assert decode_leaf_index_cell(encoded) == (total_len, local_payload, 55)




def test_leaf_index_cell_threshold_is_stricter_than_table_leaf() -> None:
    # §6.5: index pages spill much sooner than table leaves, on the same USABLE_SIZE.
    assert max_local_payload(PageType.LEAF_INDEX) < max_local_payload(PageType.LEAF_TABLE)




@given(payload=st.binary(min_size=0, max_size=50))
def test_leaf_index_cell_round_trips_property(payload: bytes) -> None:
    encoded = encode_leaf_index_cell(total_payload_len=len(payload), local_payload=payload)
    assert decode_leaf_index_cell(encoded) == (len(payload), payload, 0)




# =====================================================================
# INDEX INTERIOR cell -- child pointer AND a payload (the separator key)
# =====================================================================




def test_interior_index_cell_round_trips_without_overflow() -> None:
    encoded = encode_interior_index_cell(child_page=3, total_payload_len=4, local_payload=b"key1")
    assert decode_interior_index_cell(encoded) == (3, 4, b"key1", 0)




def test_interior_index_cell_round_trips_with_overflow() -> None:
    x = max_local_payload(PageType.INTERIOR_INDEX)
    total_len = x + 300
    local_len = local_payload_size(PageType.INTERIOR_INDEX, total_len)
    local_payload = b"c" * local_len


    encoded = encode_interior_index_cell(
        child_page=8, total_payload_len=total_len, local_payload=local_payload, overflow_page=200
    )
    assert decode_interior_index_cell(encoded) == (8, total_len, local_payload, 200)




def test_interior_index_cell_child_pointer_is_first_four_bytes() -> None:
    encoded = encode_interior_index_cell(child_page=0x0A0B0C0D, total_payload_len=2, local_payload=b"hi")
    assert encoded[0:4] == bytes([0x0A, 0x0B, 0x0C, 0x0D])




def test_decode_interior_index_cell_rejects_truncated_cell() -> None:
    with pytest.raises(MalformedCellError):
        decode_interior_index_cell(b"\x00\x00\x00")




@given(
    child_page=st.integers(min_value=1, max_value=2**32 - 1),
    payload=st.binary(min_size=0, max_size=50),
)
def test_interior_index_cell_round_trips_property(child_page: int, payload: bytes) -> None:
    encoded = encode_interior_index_cell(child_page=child_page, total_payload_len=len(payload), local_payload=payload)
    assert decode_interior_index_cell(encoded) == (child_page, len(payload), payload, 0)



