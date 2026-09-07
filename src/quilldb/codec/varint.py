"""Variable-length integer encoding — SQLite's exact scheme.


Big-endian, 7 bits of payload per byte, high bit set = "another byte follows".
Small numbers cost 1 byte; the largest 64-bit values cost 9.


    5    -> 0x05                  0000_0101
    300  -> 0x82 0x2C             1000_0010  0010_1100
                                  ^ continue      ^ done


THE NINTH BYTE IS SPECIAL. It carries no continuation flag and contributes all
8 of its bits, so 8*7 + 8 = exactly 64. That's why the maximum is 9 and not the
10 that ceil(64/7) suggests. See docs/theory/03-... §3.4.


NO ZIGZAG. SQLite varints are 64-bit two's complement, so a negative value sets
its high bits and costs the full 9 bytes. Record integers dodge that a different
way: the serial type names the width (1/2/3/4/6/8 bytes) and the value is stored
two's complement at that width. See §3.5.
"""


from quilldb.errors import MalformedRecordError


def encode_uvarint(value: int) -> bytes:
    """Encode a non-negative integer.


    Args:
        value: 0 <= value < 2**64.
    Returns:
        1 to 9 bytes. Values needing more than 56 bits MUST use the 9-byte
        form: 8 bytes of 7 bits each (continuation set), then one byte
        carrying all 8 of its bits with no flag.
    Raises:
        ValueError: value is negative or >= 2**64.
    """
    if value < 0 or value >= 2**64:
        raise ValueError("value must satisfy 0 <= value < 2**64")


    if value == 0:
        return b"\x00"


    if value >= 2**56:
        # 9-byte form (§3.4): exactly 8 flagged 7-bit groups from the upper
        # bits, then one raw unflagged byte for the low 8 bits. Leading zero
        # groups are never trimmed here — the format always spends all 8.
        last_byte = value & 0xFF
        remainder = value >> 8
        groups = [((remainder >> shift) & 0x7F) | 0x80 for shift in range(49, -1, -7)]
        return bytes(groups) + bytes([last_byte])


    groups = []


    # Find the highest 7-bit group, then walk down, flagging every group
    # except the last.
    shift = ((value.bit_length() - 1) // 7) * 7
    while shift > 0:
        groups.append(((value >> shift) & 0x7F) | 0x80)
        shift -= 7
    groups.append(value & 0x7F)


    return bytes(groups)




def decode_uvarint(data: bytes | memoryview, offset: int = 0) -> tuple[int, int]:
    """Decode one varint starting at `offset`.


    Args:
        data: buffer to read from.
        offset: where the varint starts.
    Returns:
        (value, bytes_consumed). Stops at the varint's own end — trailing bytes
        in `data` are ignored, which is what lets you decode records field by field.
    Raises:
        MalformedRecordError: the buffer ends mid-varint.
    """
    result = 0
    continued = False
    bytes_consumed = 0


    for i in range(8):
        if offset + i >= len(data):
            raise MalformedRecordError("the buffer ends mid-varint")


        cur_byte = data[offset + i]
        result = (result << 7) | (cur_byte & 0x7F)
        bytes_consumed += 1


        if cur_byte & 0x80 == 0:
            continued = False
            break
        continued = True


    if continued:
        if offset + 8 >= len(data):
            raise MalformedRecordError("the buffer ends mid-varint")
        result = (result << 8) | data[offset + 8]
        bytes_consumed += 1


    return result, bytes_consumed




def to_twos_complement(value: int) -> int:
    """Reinterpret a signed 64-bit integer as the u64 bit pattern to encode.


    There is no zigzag in this format. Rowids are signed and stored as the
    varint of their two's-complement bit pattern, so negative rowids cost the
    full 9 bytes. SQLite accepts that; negative rowids are pathological.


    Args:
        value: -2**63 <= value < 2**63.
    """
    # Negative Python ints behave as if sign-extended with infinite 1-bits,
    # so masking to 64 bits reproduces the two's-complement pattern directly.
    return value & (2**64 - 1)




def from_twos_complement(value: int) -> int:
    """Reinterpret a decoded u64 as a signed 64-bit integer. Inverse of above."""
    if value & (2**63):  # sign bit set -> represents a negative number
        return value - 2**64
    return value