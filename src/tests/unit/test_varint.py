import pytest
from hypothesis import given
from hypothesis import strategies as st

from quilldb.codec.varint import (
    decode_uvarint,
    encode_uvarint,
    from_twos_complement,
    to_twos_complement,
)
from quilldb.errors import MalformedRecordError

U64_MAX = 2**64 - 1
I64_MIN = -(2**63)
I64_MAX = 2**63 - 1




# --- known vectors, straight from the module docstring ---


def test_five_is_one_byte() -> None:
    assert encode_uvarint(5) == b"\x05"




def test_three_hundred_is_two_bytes() -> None:
    assert encode_uvarint(300) == b"\x82\x2c"




def test_zero_is_one_byte() -> None:
    assert encode_uvarint(0) == b"\x00"




# --- the 7-bit boundary: where byte count grows ---


def test_127_fits_in_one_byte() -> None:
    assert len(encode_uvarint(127)) == 1




def test_128_needs_two_bytes() -> None:
    assert len(encode_uvarint(128)) == 2




# --- the 8-byte / 9-byte special case (theory doc §3.4) ---


def test_largest_eight_byte_value_stays_eight_bytes() -> None:
    value = 2**56 - 1          # 8 bytes * 7 bits, all ones
    assert len(encode_uvarint(value)) == 8




def test_first_nine_byte_value_uses_ninth_byte() -> None:
    value = 2**56              # one past the 8-byte ceiling
    encoded = encode_uvarint(value)
    assert len(encoded) == 9
    assert encoded[-1] == 0x00     # the new bit lands in the all-data 9th byte




def test_u64_max_is_exactly_nine_bytes() -> None:
    encoded = encode_uvarint(U64_MAX)
    assert len(encoded) == 9
    assert encoded[-1] == 0xFF     # ninth byte has no flag bit, all 8 bits are data




# --- round trips, exact values first, then property-based over the full range ---


@pytest.mark.parametrize("value", [0, 1, 5, 127, 128, 300, 2**14 - 1, 2**14, 2**56 - 1, 2**56, U64_MAX])
def test_roundtrip_exact_values(value: int) -> None:
    encoded = encode_uvarint(value)
    decoded, consumed = decode_uvarint(encoded)
    assert decoded == value
    assert consumed == len(encoded)




@given(st.integers(min_value=0, max_value=U64_MAX))
def test_roundtrip_property(value: int) -> None:
    encoded = encode_uvarint(value)
    assert len(encoded) <= 9
    decoded, consumed = decode_uvarint(encoded)
    assert decoded == value
    assert consumed == len(encoded)




# --- decode must stop at the varint's own end, ignoring trailing bytes ---


def test_decode_ignores_trailing_bytes() -> None:
    encoded = encode_uvarint(300) + b"\xff\xff\xff"
    decoded, consumed = decode_uvarint(encoded)
    assert decoded == 300
    assert consumed == 2




def test_decode_reads_from_offset() -> None:
    buf = b"\x00\x00" + encode_uvarint(300)
    decoded, consumed = decode_uvarint(buf, offset=2)
    assert decoded == 300
    assert consumed == 2




# --- malformed input ---


def test_decode_truncated_continuation_raises() -> None:
    truncated = encode_uvarint(2**20)[:-1]     # drop the final, flag-clear byte
    assert all(b & 0x80 for b in truncated)
    with pytest.raises(MalformedRecordError):
        decode_uvarint(truncated)




def test_decode_empty_buffer_raises() -> None:
    with pytest.raises(MalformedRecordError):
        decode_uvarint(b"")




# --- input validation on encode ---


def test_encode_negative_rejected() -> None:
    with pytest.raises(ValueError):
        encode_uvarint(-1)




def test_encode_too_large_rejected() -> None:
    with pytest.raises(ValueError):
        encode_uvarint(2**64)




# --- two's complement: no zigzag, negatives cost the full 9 bytes ---


def test_negative_one_is_all_bits_set() -> None:
    assert to_twos_complement(-1) == U64_MAX




def test_negative_rowid_costs_nine_bytes() -> None:
    assert len(encode_uvarint(to_twos_complement(-5))) == 9




def test_positive_value_unchanged_by_twos_complement() -> None:
    assert to_twos_complement(300) == 300




@given(st.integers(min_value=I64_MIN, max_value=I64_MAX))
def test_twos_complement_roundtrip_property(value: int) -> None:
    u = to_twos_complement(value)
    assert 0 <= u <= U64_MAX
    assert from_twos_complement(u) == value



