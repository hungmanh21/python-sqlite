"""The four b-tree cell formats, and the overflow threshold math they share.


    TABLE LEAF (0x0D):      [ payload len varint ][ rowid varint ][ local payload ][ overflow u32? ]
    TABLE INTERIOR (0x05):  [ child page u32 ][ rowid varint ]
    INDEX LEAF (0x0A):      [ payload len varint ][ local payload ][ overflow u32? ]
    INDEX INTERIOR (0x02):  [ child page u32 ][ payload len varint ][ local payload ][ overflow u32? ]


Pure encode/decode -- no Pager, no BufferPool, no page allocation. Writing an
oversized payload's overflow chain to disk is storage/overflow.py's job; this
module only decides *how many bytes stay local* and packs/unpacks the cell
bytes PageBody.cells already treats as opaque. See docs/theory/06-b-tree-mechanics.md.


Table interior cells carry no payload at all -- not "a small payload", none
(§6.0) -- which is why max_local_payload() has nothing to compute for them.


decode_* MUST recompute the local/overflow boundary via local_payload_size(),
never infer it from len(cell) -- a payload that spills exactly 4 bytes makes
"local bytes + 4-byte pointer" and "total payload, no overflow" the same
length. The formula is the only thing that disambiguates them, which is also
why a writer that keeps one byte more or fewer than the formula says produces
a file a spec-following reader silently misparses (§6.5).
"""


from quilldb.codec.varint import (
    decode_uvarint,
    encode_uvarint,
    from_twos_complement,
    to_twos_complement,
)
from quilldb.constants import USABLE_SIZE, PageType
from quilldb.errors import MalformedCellError


# M -- the minimum payload that must always stay local, even when P is huge.
# Independent of page type (unlike X), so it's a module constant, not a function.
MIN_LOCAL_PAYLOAD = ((USABLE_SIZE - 12) * 32 // 255) - 23




def max_local_payload(page_type: PageType) -> int:
    """X: the most payload bytes `page_type` may store directly on the page.


    Args:
        page_type: LEAF_TABLE, LEAF_INDEX, or INTERIOR_INDEX. Never
            INTERIOR_TABLE -- table interior cells have no payload to split.
    Returns:
        USABLE_SIZE - 35 for LEAF_TABLE (nearly a whole page); the stricter
        ((USABLE_SIZE - 12) * 64 // 255) - 23 for the two INDEX page types
        (roughly a quarter page -- it's what bounds their fanout to >= 4).
    Raises:
        ValueError: page_type is INTERIOR_TABLE.
    """
    if page_type == PageType.INTERIOR_TABLE:
        raise ValueError("INTERIOR_TABLE cells carry no payload; there is no threshold to compute")
    if page_type == PageType.LEAF_TABLE:
        return USABLE_SIZE - 35
    return ((USABLE_SIZE - 12) * 64 // 255) - 23




def local_payload_size(page_type: PageType, total_payload_len: int) -> int:
    """K/M/P decision (§6.5): how many of `total_payload_len` bytes are local.


    Args:
        page_type: same restriction as max_local_payload.
        total_payload_len: P, the full payload size including whatever spills.
    Returns:
        total_payload_len itself if it already fits within X. Otherwise the
        remainder-trick value K if K <= X, else the bare minimum M -- chosen
        so the *spilled* amount (P - result) divides evenly into
        (USABLE_SIZE - 4)-byte overflow pages with nothing wasted on the last
        one, except in the M fallback.
    """
    x = max_local_payload(page_type)
    if total_payload_len <= x:
        return total_payload_len


    k = MIN_LOCAL_PAYLOAD + ((total_payload_len - MIN_LOCAL_PAYLOAD) % (USABLE_SIZE - 4))
    return k if k <= x else MIN_LOCAL_PAYLOAD




def _require_valid_split(
    page_type: PageType, total_payload_len: int, local_payload: bytes, overflow_page: int
) -> None:
    """Shared encode-time guard for the three payload-carrying cell formats.


    Raises:
        ValueError: len(local_payload) doesn't match what
            local_payload_size() says it must be for this total_payload_len,
            or overflow_page's presence (zero vs. nonzero) disagrees with
            whether local_payload is actually short of total_payload_len.
            0 is never a real page number (page 1 is the schema root), so
            it's a safe "no overflow" sentinel -- same convention as
            right_child and freelist_trunk elsewhere in this codebase.
    """
    expected = local_payload_size(page_type, total_payload_len)
    if len(local_payload) != expected:
        raise ValueError(
            f"local_payload is {len(local_payload)} bytes; local_payload_size() says {expected}"
        )


    # These two facts must agree, and disagreement is a caller bug either way:
    # claiming overflow without a short payload, or a short payload with no
    # pointer to the rest of it, both describe an unrepresentable cell.
    has_overflow = overflow_page != 0
    payload_spilled = len(local_payload) < total_payload_len
    if has_overflow != payload_spilled:
        raise ValueError(
            f"overflow_page={overflow_page} disagrees with "
            f"local_payload spilling {total_payload_len - len(local_payload)} bytes"
        )




def encode_leaf_table_cell(
    rowid: int, total_payload_len: int, local_payload: bytes, overflow_page: int = 0
) -> bytes:
    """Pack a TABLE LEAF cell: payload length first, then rowid (§6.0 -- the
    length is what cellSizePtr()-style skipping needs, so it's front-loaded).


    Args:
        rowid: -2**63 <= rowid < 2**63. Encoded as the varint of its
            two's-complement bit pattern (no zigzag -- see varint.py).
        total_payload_len: P, the full logical payload size.
        local_payload: exactly the bytes local_payload_size() says belong on
            this page; the rest of the payload lives in the overflow chain.
        overflow_page: first page of the overflow chain, or 0 if
            len(local_payload) == total_payload_len.
    Raises:
        ValueError: see _require_valid_split.
    """
    _require_valid_split(PageType.LEAF_TABLE, total_payload_len, local_payload, overflow_page)
    return (
        encode_uvarint(total_payload_len)
        + encode_uvarint(to_twos_complement(rowid))
        + local_payload
        + (overflow_page.to_bytes(4, "big") if overflow_page else b"")
    )




def decode_leaf_table_cell(cell: bytes) -> tuple[int, int, bytes, int]:
    """Inverse of encode_leaf_table_cell.


    Returns:
        (rowid, total_payload_len, local_payload, overflow_page). overflow_page
        is 0 when the payload never spilled.
    Raises:
        MalformedCellError: cell is too short for the fields it declares.
    """
    total_payload_len, n1 = decode_uvarint(cell, 0)
    raw_rowid, n2 = decode_uvarint(cell, n1)
    rowid = from_twos_complement(raw_rowid)


    local_len = local_payload_size(PageType.LEAF_TABLE, total_payload_len)
    header_len = n1 + n2
    local_payload = cell[header_len : header_len + local_len]
    if len(local_payload) < local_len:
        raise MalformedCellError("cell is shorter than its declared local payload")


    if local_len < total_payload_len:
        overflow_bytes = cell[header_len + local_len : header_len + local_len + 4]
        if len(overflow_bytes) < 4:
            raise MalformedCellError("cell is missing its overflow page number")
        overflow_page = int.from_bytes(overflow_bytes, "big")
    else:
        overflow_page = 0


    return (rowid, total_payload_len, local_payload, overflow_page)




def encode_interior_table_cell(child_page: int, rowid: int) -> bytes:
    """Pack a TABLE INTERIOR cell: fixed-width child pointer first (§6.0 --
    descent only ever reads this field, so it must be addressable without
    parsing a varint), then the separator key. No payload, ever.


    Args:
        child_page: 1 <= child_page <= page_count. This is the LEFT child --
            "keys <= rowid go here" (SQLite's convention, §6.1).
        rowid: same range/encoding as encode_leaf_table_cell.
    """
    return child_page.to_bytes(4, "big") + encode_uvarint(to_twos_complement(rowid))




def decode_interior_table_cell(cell: bytes) -> tuple[int, int]:
    """Inverse of encode_interior_table_cell. Returns (child_page, rowid).


    Raises:
        MalformedCellError: cell is shorter than the fixed 4-byte child field.
    """
    if len(cell) < 4:
        raise MalformedCellError("cell is shorter than the fixed 4-byte child field")


    child_page = int.from_bytes(cell[0:4], "big")
    raw_rowid, _ = decode_uvarint(cell, 4)
    rowid = from_twos_complement(raw_rowid)
    return (child_page, rowid)




def encode_leaf_index_cell(total_payload_len: int, local_payload: bytes, overflow_page: int = 0) -> bytes:
    """Pack an INDEX LEAF cell. Same shape as the table leaf cell minus the
    rowid -- an index cell's key *is* the payload (the indexed columns plus
    rowid, already encoded as a record), so there's nothing separate to store.
    """
    _require_valid_split(PageType.LEAF_INDEX, total_payload_len, local_payload, overflow_page)
    return (
        encode_uvarint(total_payload_len)
        + local_payload
        + (overflow_page.to_bytes(4, "big") if overflow_page else b"")
    )




def decode_leaf_index_cell(cell: bytes) -> tuple[int, bytes, int]:
    """Inverse of encode_leaf_index_cell. Returns (total_payload_len, local_payload, overflow_page)."""
    total_payload_len, n1 = decode_uvarint(cell, 0)


    local_len = local_payload_size(PageType.LEAF_INDEX, total_payload_len)
    local_payload = cell[n1 : n1 + local_len]
    if len(local_payload) < local_len:
        raise MalformedCellError("cell is shorter than its declared local payload")


    if local_len < total_payload_len:
        overflow_bytes = cell[n1 + local_len : n1 + local_len + 4]
        if len(overflow_bytes) < 4:
            raise MalformedCellError("cell is missing its overflow page number")
        overflow_page = int.from_bytes(overflow_bytes, "big")
    else:
        overflow_page = 0


    return (total_payload_len, local_payload, overflow_page)




def encode_interior_index_cell(
    child_page: int, total_payload_len: int, local_payload: bytes, overflow_page: int = 0
) -> bytes:
    """Pack an INDEX INTERIOR cell: child pointer first, then the same
    payload+overflow shape as the index leaf cell. Unlike table interiors,
    index interiors DO carry a payload -- the separator key IS the indexed
    value, there's nowhere else to put it (§6.0). That's why its threshold
    formula is the stricter index one, not "no threshold at all".
    """
    _require_valid_split(PageType.INTERIOR_INDEX, total_payload_len, local_payload, overflow_page)
    return (
        child_page.to_bytes(4, "big")
        + encode_uvarint(total_payload_len)
        + local_payload
        + (overflow_page.to_bytes(4, "big") if overflow_page else b"")
    )




def decode_interior_index_cell(cell: bytes) -> tuple[int, int, bytes, int]:
    """Inverse of encode_interior_index_cell.


    Returns:
        (child_page, total_payload_len, local_payload, overflow_page).
    Raises:
        MalformedCellError: cell is shorter than the fixed 4-byte child field.
    """
    if len(cell) < 4:
        raise MalformedCellError("cell is shorter than the fixed 4-byte child field")
    child_page = int.from_bytes(cell[0:4], "big")


    total_payload_len, n1 = decode_uvarint(cell, 4)


    local_len = local_payload_size(PageType.INTERIOR_INDEX, total_payload_len)
    header_len = 4 + n1
    local_payload = cell[header_len : header_len + local_len]
    if len(local_payload) < local_len:
        raise MalformedCellError("cell is shorter than its declared local payload")


    if local_len < total_payload_len:
        overflow_bytes = cell[header_len + local_len : header_len + local_len + 4]
        if len(overflow_bytes) < 4:
            raise MalformedCellError("cell is missing its overflow page number")
        overflow_page = int.from_bytes(overflow_bytes, "big")
    else:
        overflow_page = 0


    return (child_page, total_payload_len, local_payload, overflow_page)



