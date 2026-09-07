"""Row <-> bytes.


A record is a type manifest followed by the raw values:


    [header_len varint][serial type][serial type]...[value][value]...


`header_len` counts itself plus all the serial types, so the body starts at
`offset + header_len`. Serial types tell you each value's type AND byte length,
which is what lets you skip to column N without decoding columns 0..N-1.


Serial types (same scheme as SQLite):
      0  NULL              0 bytes
      1  int, 1 byte
      2  int, 2 bytes
      3  int, 3 bytes
      4  int, 4 bytes
      5  int, 6 bytes
      6  int, 8 bytes
      7  float64           8 bytes
      8  integer 0         0 bytes   <- the value is in the type
      9  integer 1         0 bytes
  10,11  reserved -> MalformedRecordError
  N>=12 even  blob, (N-12)//2 bytes
  N>=13 odd   text, (N-13)//2 bytes of UTF-8


Note the lengths are BYTES, not characters. "café" is 5 bytes -> type 23.
"""


import struct
from collections.abc import Sequence

from quilldb.codec.varint import decode_uvarint, encode_uvarint
from quilldb.errors import MalformedRecordError

Value = None | int | float | str | bytes


# Byte width for each fixed-width signed-integer serial type. One table,
# shared by the encode side (serial_type_for) and both decode sides
# (decode_value, _body_length) -- so there is exactly one place that knows
# "type 5 means 6 bytes."
_INT_WIDTH_BY_TYPE = {1: 1, 2: 2, 3: 3, 4: 4, 5: 6, 6: 8}


# (serial_type, min_value, max_value), narrowest width first. serial_type_for
# picks the first range a value fits in, so integers always get the smallest
# encoding available.
_INT_RANGES = (
    (1, -128, 127),
    (2, -32768, 32767),
    (3, -(2**23), 2**23 - 1),
    (4, -(2**31), 2**31 - 1),
    (5, -(2**47), 2**47 - 1),
    (6, -(2**63), 2**63 - 1),
)




def serial_type_for(value: Value) -> tuple[int, bytes]:
    """Classify one value.


    Returns:
        (serial_type, encoded_body_bytes). Body is b"" for NULL, 0 and 1.
    Raises:
        TypeError: unsupported Python type.
        ValueError: integer outside signed 64-bit range.
    """
    if value is None:
        return 0, b""


    if isinstance(value, int):
        # 0 and 1 get their own zero-body serial types instead of going
        # through the width table below -- SQLite's "static Huffman code"
        # for the two most common integers (theory doc §3.8).
        if value == 0:
            return 8, b""
        if value == 1:
            return 9, b""


        for serial_type, low, high in _INT_RANGES:
            if low <= value <= high:
                width = _INT_WIDTH_BY_TYPE[serial_type]
                return serial_type, value.to_bytes(width, "big", signed=True)


        raise ValueError("integer outside the signed 64-bit range")


    if isinstance(value, float):
        return 7, struct.pack(">d", value)


    if isinstance(value, str):
        encoded = value.encode("utf-8")
        return len(encoded) * 2 + 13, encoded


    if isinstance(value, bytes):
        return len(value) * 2 + 12, value


    raise TypeError("value must be None, int, float, str, or bytes")




def _body_length(serial_type: int) -> int:
    """Byte length implied by a serial type alone -- no buffer access.


    This is what lets decode_column skip a column without decoding it:
    every column before the one you want only needs its length, never its
    actual bytes.
    """
    if serial_type in (0, 8, 9):
        return 0
    if serial_type in _INT_WIDTH_BY_TYPE:
        return _INT_WIDTH_BY_TYPE[serial_type]
    if serial_type == 7:
        return 8
    if serial_type in (10, 11):
        raise MalformedRecordError(f"reserved serial type {serial_type}")
    # Every serial type 0-11 is handled above, so anything left is >=12:
    # even -> blob, odd -> text, via the (N-12)/2 / (N-13)/2 formulas.
    if serial_type % 2 == 0:
        return (serial_type - 12) // 2
    return (serial_type - 13) // 2




def decode_value(serial_type: int, data: bytes | memoryview, offset: int) -> tuple[Value, int]:
    """Decode one value of a known serial type.


    Returns:
        (value, bytes_consumed).
    Raises:
        MalformedRecordError: reserved type, or buffer too short.
    """
    if serial_type == 0:
        return None, 0
    if serial_type == 8:
        return 0, 0
    if serial_type == 9:
        return 1, 0


    length = _body_length(serial_type)
    if offset + length > len(data):
        raise MalformedRecordError(f"buffer too short for serial type {serial_type}")


    body = data[offset : offset + length]
    if serial_type in _INT_WIDTH_BY_TYPE:
        return int.from_bytes(body, "big", signed=True), length
    if serial_type == 7:
        return struct.unpack(">d", body)[0], length
    if serial_type % 2 == 0:
        return bytes(body), length
    return bytes(body).decode("utf-8"), length




def encode_record(values: Sequence[Value]) -> bytes:
    """Encode a row.


    Args:
        values: the column values, in column order.
    Returns:
        The complete record. encode_record([]) is legal and yields just a header.
    """
    pairs = [serial_type_for(v) for v in values]
    type_varints = [encode_uvarint(serial_type) for serial_type, _ in pairs]
    bodies = [body for _, body in pairs]


    # header_size counts itself, so growing it (e.g. past 127, where its own
    # varint grows from 1 byte to 2) can push the total past the guess that
    # produced it. Iterate until the guess is self-consistent.
    header_size = 1 + sum(len(v) for v in type_varints)
    while True:
        actual = len(encode_uvarint(header_size)) + sum(len(v) for v in type_varints)
        if actual == header_size:
            break
        header_size = actual


    header = encode_uvarint(header_size) + b"".join(type_varints)
    body = b"".join(bodies)
    return header + body




def decode_record(data: bytes | memoryview, offset: int = 0) -> tuple[Value, ...]:
    """Decode a full row starting at `offset`.


    Raises:
        MalformedRecordError: truncated, or header_len inconsistent with content.
    """
    header_size, consumed = decode_uvarint(data, offset)
    body_start = offset + header_size
    pos = offset + consumed  # first byte after the size varint


    serial_types = []
    while pos < body_start:
        serial_type, consumed = decode_uvarint(data, pos)
        serial_types.append(serial_type)
        pos += consumed


    if pos != body_start:
        # A type varint overran the boundary header_size promised: the
        # header is internally inconsistent, not merely short.
        raise MalformedRecordError("header size inconsistent with type codes")


    values = []
    body_offset = body_start
    for serial_type in serial_types:
        value, consumed = decode_value(serial_type, data, body_offset)
        values.append(value)
        body_offset += consumed


    return tuple(values)




def decode_column(data: bytes | memoryview, offset: int, column: int) -> Value:
    """Decode ONE column without materializing the others.


    This is the payoff for the manifest design — read the header, sum the
    preceding lengths, jump. Used by Filter to test one column cheaply.


    Raises:
        IndexError: column >= number of columns in the record.
    """
    header_size, consumed = decode_uvarint(data, offset)
    body_start = offset + header_size
    pos = offset + consumed


    body_offset = body_start
    index = 0
    while pos < body_start:
        serial_type, consumed = decode_uvarint(data, pos)
        pos += consumed


        if index == column:
            value, _ = decode_value(serial_type, data, body_offset)
            return value


        # Skip this column by length only -- never decode a value we're
        # not going to return.
        body_offset += _body_length(serial_type)
        index += 1


    raise IndexError(f"column {column} out of range")



