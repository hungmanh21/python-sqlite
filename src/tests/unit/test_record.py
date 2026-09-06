import math
import struct


import pytest
from hypothesis import given, strategies as st


from quilldb.codec.record import (
    decode_column,
    decode_record,
    decode_value,
    encode_record,
    serial_type_for,
)
from quilldb.errors import MalformedRecordError


I64_MIN = -(2**63)
I64_MAX = 2**63 - 1




# =====================================================================
# serial_type_for — classifying one Python value
# =====================================================================


# --- the three zero-body-byte special cases ---


def test_null_is_type_0_with_empty_body() -> None:
    assert serial_type_for(None) == (0, b"")




def test_integer_zero_is_type_8_not_type_1() -> None:
    assert serial_type_for(0) == (8, b"")




def test_integer_one_is_type_9_not_type_1() -> None:
    assert serial_type_for(1) == (9, b"")




def test_float_zero_is_type_7_not_type_8() -> None:
    # 0 (int) and 0.0 (float) must NOT collapse to the same serial type —
    # type 8 means "the integer 0," not "any zero."
    assert serial_type_for(0.0) == (7, struct.pack(">d", 0.0))




# --- plain small integers ---


def test_five_is_one_byte_int() -> None:
    assert serial_type_for(5) == (1, b"\x05")




def test_negative_five_is_one_byte_two_complement() -> None:
    assert serial_type_for(-5) == (1, b"\xfb")




# --- every width boundary: (value, expected_serial_type) ---


WIDTH_BOUNDARIES = [
    (127, 1), (128, 2),
    (-128, 1), (-129, 2),
    (32767, 2), (32768, 3),
    (-32768, 2), (-32769, 3),
    (2**23 - 1, 3), (2**23, 4),
    (-(2**23), 3), (-(2**23) - 1, 4),
    (2**31 - 1, 4), (2**31, 5),
    (-(2**31), 4), (-(2**31) - 1, 5),
    (2**47 - 1, 5), (2**47, 6),
    (-(2**47), 5), (-(2**47) - 1, 6),
    (2**63 - 1, 6),
    (-(2**63), 6),
]




@pytest.mark.parametrize("value,expected_type", WIDTH_BOUNDARIES)
def test_integer_width_boundaries(value: int, expected_type: int) -> None:
    stype, body = serial_type_for(value)
    assert stype == expected_type
    width = {1: 1, 2: 2, 3: 3, 4: 4, 5: 6, 6: 8}[expected_type]
    assert len(body) == width
    assert int.from_bytes(body, "big", signed=True) == value




def test_integer_above_i64_max_rejected() -> None:
    with pytest.raises(ValueError):
        serial_type_for(2**63)




def test_integer_below_i64_min_rejected() -> None:
    with pytest.raises(ValueError):
        serial_type_for(-(2**63) - 1)




# --- floats ---


def test_float_body_is_big_endian_float64() -> None:
    assert serial_type_for(3.5) == (7, struct.pack(">d", 3.5))




def test_negative_zero_float_roundtrips_bit_exact() -> None:
    stype, body = serial_type_for(-0.0)
    assert stype == 7
    (decoded,) = struct.unpack(">d", body)
    assert math.copysign(1.0, decoded) == -1.0




def test_nan_float_preserves_bit_pattern() -> None:
    stype, body = serial_type_for(float("nan"))
    assert stype == 7
    (decoded,) = struct.unpack(">d", body)
    assert math.isnan(decoded)




# --- text: (n*2)+13, odd, length in BYTES not characters ---


def test_empty_string_is_type_13() -> None:
    assert serial_type_for("") == (13, b"")




def test_one_char_ascii_string_is_type_15() -> None:
    assert serial_type_for("a") == (15, b"a")




def test_three_char_ascii_string_is_type_19() -> None:
    # Not type 17 -- (n*2)+13 for n=3 is 19. (The theory doc's own worked
    # example at "type=17 (text,3ch)" has this off by one; trust the
    # formula in its serial-type table over its narrative example.)
    assert serial_type_for("ada") == (19, b"ada")




def test_multibyte_utf8_counts_bytes_not_characters() -> None:
    # "café" is 5 BYTES (é is 2 bytes in UTF-8) -> (5*2)+13 = 23.
    stype, body = serial_type_for("café")
    assert body == "café".encode("utf-8")
    assert len(body) == 5
    assert stype == 23




# --- blob: (n*2)+12, even ---


def test_empty_blob_is_type_12() -> None:
    assert serial_type_for(b"") == (12, b"")




def test_two_byte_blob_is_type_16() -> None:
    assert serial_type_for(b"\x01\x02") == (16, b"\x01\x02")




# --- unsupported Python types ---


@pytest.mark.parametrize("value", [[1, 2], {"a": 1}, object(), (1, 2)])
def test_unsupported_python_type_raises_type_error(value: object) -> None:
    with pytest.raises(TypeError):
        serial_type_for(value)  # type: ignore[arg-type]




# --- round trips, property-based ---


@given(st.integers(min_value=I64_MIN, max_value=I64_MAX))
def test_integer_roundtrip_property(value: int) -> None:
    stype, body = serial_type_for(value)
    decoded, consumed = decode_value(stype, body, 0)
    assert decoded == value
    assert consumed == len(body)




@given(st.floats(allow_nan=False))
def test_float_roundtrip_property(value: float) -> None:
    stype, body = serial_type_for(value)
    decoded, consumed = decode_value(stype, body, 0)
    assert decoded == value
    assert consumed == 8




@given(st.text())
def test_text_roundtrip_property(value: str) -> None:
    stype, body = serial_type_for(value)
    decoded, consumed = decode_value(stype, body, 0)
    assert decoded == value
    assert consumed == len(value.encode("utf-8"))




@given(st.binary())
def test_blob_roundtrip_property(value: bytes) -> None:
    stype, body = serial_type_for(value)
    decoded, consumed = decode_value(stype, body, 0)
    assert decoded == value
    assert consumed == len(value)




# =====================================================================
# decode_value — decoding one value of a KNOWN serial type
# =====================================================================


def test_decode_null() -> None:
    assert decode_value(0, b"", 0) == (None, 0)




def test_decode_integer_zero_special_case() -> None:
    assert decode_value(8, b"", 0) == (0, 0)




def test_decode_integer_one_special_case() -> None:
    assert decode_value(9, b"", 0) == (1, 0)




def test_decode_one_byte_int() -> None:
    assert decode_value(1, b"\xfb", 0) == (-5, 1)




def test_decode_eight_byte_int() -> None:
    body = (2**62).to_bytes(8, "big", signed=True)
    assert decode_value(6, body, 0) == (2**62, 8)




def test_decode_float() -> None:
    assert decode_value(7, struct.pack(">d", 3.5), 0) == (3.5, 8)




def test_decode_text() -> None:
    assert decode_value(19, b"ada", 0) == ("ada", 3)




def test_decode_blob() -> None:
    assert decode_value(16, b"\x01\x02", 0) == (b"\x01\x02", 2)




def test_decode_respects_offset() -> None:
    buf = b"\x00\x00" + b"\xfb"
    assert decode_value(1, buf, offset=2) == (-5, 1)




def test_decode_ignores_trailing_bytes() -> None:
    # decode_value must consume exactly the width the serial type names,
    # regardless of what follows in the buffer -- same "stop at your own
    # end" contract as decode_uvarint.
    buf = b"\xfb\xff\xff\xff"
    assert decode_value(1, buf, 0) == (-5, 1)




# --- reserved types ---


@pytest.mark.parametrize("reserved_type", [10, 11])
def test_decode_reserved_type_raises(reserved_type: int) -> None:
    with pytest.raises(MalformedRecordError):
        decode_value(reserved_type, b"", 0)




# --- truncated buffers ---


def test_decode_int_truncated_raises() -> None:
    with pytest.raises(MalformedRecordError):
        decode_value(6, b"\x00\x00\x00", 0)  # type 6 needs 8 bytes, has 3




def test_decode_text_truncated_raises() -> None:
    with pytest.raises(MalformedRecordError):
        decode_value(19, b"ad", 0)  # type 19 = 3-byte text, has 2




def test_decode_blob_truncated_raises() -> None:
    with pytest.raises(MalformedRecordError):
        decode_value(16, b"\x01", 0)  # type 16 = 2-byte blob, has 1




# =====================================================================
# encode_record / decode_record — the full row
# =====================================================================


def test_empty_record_is_just_the_self_inclusive_size_byte() -> None:
    assert encode_record([]) == b"\x01"
    assert decode_record(b"\x01") == ()




def test_single_int_column() -> None:
    encoded = encode_record([5])
    # header_size(=2, includes itself) . type(1) . body(0x05)
    assert encoded == b"\x02\x01\x05"
    assert decode_record(encoded) == (5,)




def test_docstring_row_roundtrips() -> None:
    row = (1, "ada", 36)
    encoded = encode_record(list(row))
    assert decode_record(encoded) == row




def test_mixed_null_and_blob_roundtrip() -> None:
    row = (None, b"\x00\xff", 3.25, "x")
    encoded = encode_record(list(row))
    assert decode_record(encoded) == row




def test_decode_record_reads_from_offset() -> None:
    prefix = b"\xde\xad\xbe\xef"
    encoded = encode_record([1, "ada", 36])
    buf = prefix + encoded
    assert decode_record(buf, offset=len(prefix)) == (1, "ada", 36)




def test_out_of_range_int_propagates_value_error() -> None:
    with pytest.raises(ValueError):
        encode_record([2**63])




# --- the header-size self-inclusion trap (theory doc §3.7) ---


def test_header_size_straddling_127_bytes() -> None:
    # 127 int8 columns: each contributes exactly 1 header byte (type 1
    # fits in a 1-byte varint). header_size = 1 (itself) + 127 = 128,
    # which is the exact point where the size varint itself grows from
    # 1 byte to 2 -- and growing changes header_size again. A naive
    # non-fixed-point computation gets this wrong.
    values = list(range(127))  # 0..126, all fit in int8
    encoded = encode_record(values)
    assert decode_record(encoded) == tuple(values)




# --- malformed records ---


def test_decode_record_reserved_header_type_raises() -> None:
    encoded = bytes([2, 10])  # header_size=2, single column of type 10
    with pytest.raises(MalformedRecordError):
        decode_record(encoded)




def test_decode_record_truncated_body_raises() -> None:
    encoded = bytes([2, 1])  # header claims one int8 column, body missing
    with pytest.raises(MalformedRecordError):
        decode_record(encoded)




def test_decode_record_truncated_header_raises() -> None:
    encoded = bytes([5, 1])  # header claims 5 bytes, buffer has 2 total
    with pytest.raises(MalformedRecordError):
        decode_record(encoded)




def test_decode_record_header_length_mismatch_raises() -> None:
    # header_size claims 2 bytes total, but the first type varint (200,
    # a made-up 2-byte-encoded value) overruns that boundary by itself.
    # This is the self-consistency check §3.7 requires: after parsing
    # the header's type codes, position must land EXACTLY on header_size.
    encoded = bytes([2, 0x81, 0x48])
    with pytest.raises(MalformedRecordError):
        decode_record(encoded)




# --- round trip, property-based, over mixed rows ---


_VALUE_STRATEGY = st.one_of(
    st.none(),
    st.integers(min_value=I64_MIN, max_value=I64_MAX),
    st.floats(allow_nan=False),
    st.text(),
    st.binary(),
)




@given(st.lists(_VALUE_STRATEGY, max_size=12))
def test_record_roundtrip_property(values: list) -> None:
    encoded = encode_record(values)
    assert decode_record(encoded) == tuple(values)




# =====================================================================
# decode_column — one column without materializing the others
# =====================================================================


def test_decode_column_matches_decode_record_for_each_index() -> None:
    row = (1, "ada", 36, None, b"\x01\x02", 7.5)
    encoded = encode_record(list(row))
    for i in range(len(row)):
        assert decode_column(encoded, 0, i) == row[i]




@given(st.lists(_VALUE_STRATEGY, min_size=1, max_size=12))
def test_decode_column_matches_decode_record_property(values: list) -> None:
    encoded = encode_record(values)
    decoded_row = decode_record(encoded)
    for i in range(len(values)):
        assert decode_column(encoded, 0, i) == decoded_row[i]




def test_decode_column_out_of_range_raises_index_error() -> None:
    encoded = encode_record([1, 2, 3])
    with pytest.raises(IndexError):
        decode_column(encoded, 0, 3)




def test_decode_column_on_empty_record_raises_index_error() -> None:
    encoded = encode_record([])
    with pytest.raises(IndexError):
        decode_column(encoded, 0, 0)




def test_decode_column_reads_from_offset() -> None:
    prefix = b"\xff\xff"
    encoded = encode_record([1, "ada", 36])
    buf = prefix + encoded
    assert decode_column(buf, len(prefix), 1) == "ada"



