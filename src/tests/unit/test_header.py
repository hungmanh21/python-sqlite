import subprocess


import pytest


from quilldb.constants import (
    MAGIC,
    OFF_PAGE_COUNT,
    OFF_PAGE_SIZE,
    OFF_RESERVED,
    OFF_RESERVED_SPACE,
    OFF_TEXT_ENCODING,
    OFF_WRITE_VERSION,
    RESERVED_LEN,
)
from quilldb.errors import InvalidHeaderError
from quilldb.storage.header import FileHeader




def test_roundtrip() -> None:
    header = FileHeader(
        page_count=7, freelist_trunk=3, freelist_count=1, change_counter=99,
        schema_cookie=4, user_version=12,
    )
    assert FileHeader.from_bytes(header.to_bytes()) == header




def test_is_exactly_100_bytes() -> None:
    assert len(FileHeader().to_bytes()) == 100




def test_starts_with_magic() -> None:
    assert FileHeader().to_bytes()[:16] == b"SQLite format 3\x00"
    assert MAGIC == b"SQLite format 3\x00"




def test_reserved_bytes_are_zero() -> None:
    """Bytes 72..91 only. Everything else in the header is a real field now."""
    data = FileHeader().to_bytes()
    assert data[OFF_RESERVED:OFF_RESERVED + RESERVED_LEN] == b"\x00" * RESERVED_LEN




def test_payload_fractions_are_64_32_32() -> None:
    """The spec fixes these. sqlite3 checks them."""
    assert FileHeader().to_bytes()[21:24] == bytes([64, 32, 32])




def test_empty_database_is_one_page() -> None:
    """Page 1 alone is a valid database: file header + empty leaf table b-tree."""
    assert FileHeader().page_count == 1




def test_bad_magic_rejected() -> None:
    data = bytearray(FileHeader().to_bytes())
    data[0:4] = b"JUNK"
    with pytest.raises(InvalidHeaderError):
        FileHeader.from_bytes(bytes(data))




def test_truncated_magic_rejected() -> None:
    """The full 16 bytes must match, including the trailing NUL."""
    data = bytearray(FileHeader().to_bytes())
    data[15] = ord("X")
    with pytest.raises(InvalidHeaderError):
        FileHeader.from_bytes(bytes(data))




def test_illegal_page_size_rejected() -> None:
    """Not a power of two -> no version of SQLite ever wrote this."""
    data = bytearray(FileHeader().to_bytes())
    data[OFF_PAGE_SIZE:OFF_PAGE_SIZE + 2] = (5000).to_bytes(2, "big")
    with pytest.raises(InvalidHeaderError):
        FileHeader.from_bytes(bytes(data))




def test_page_size_one_means_65536() -> None:
    """The 2-byte field can't hold 65536, so 1 is repurposed to mean it."""
    data = bytearray(FileHeader().to_bytes())
    data[OFF_PAGE_SIZE:OFF_PAGE_SIZE + 2] = (1).to_bytes(2, "big")
    with pytest.raises(InvalidHeaderError):
        FileHeader.from_bytes(bytes(data))       # legal SQLite, unsupported here
    # ...but it must be understood as 65536, not as 1, when refusing it.
    assert FileHeader(page_size=65536).to_bytes()[OFF_PAGE_SIZE:OFF_PAGE_SIZE + 2] == b"\x00\x01"




def test_zero_page_count_rejected() -> None:
    data = bytearray(FileHeader().to_bytes())
    data[OFF_PAGE_COUNT:OFF_PAGE_COUNT + 4] = (0).to_bytes(4, "big")
    with pytest.raises(InvalidHeaderError):
        FileHeader.from_bytes(bytes(data))




def test_short_buffer_rejected() -> None:
    with pytest.raises(InvalidHeaderError):
        FileHeader.from_bytes(b"\x00" * 50)




# --- Valid SQLite files that quilldb does not support. Each must be REFUSED
# --- with InvalidHeaderError, never silently misread. See check_supported().


@pytest.mark.parametrize(
    "offset,size,value,what",
    [
        (OFF_PAGE_SIZE, 2, 8192, "a legal but unsupported page size"),
        (OFF_WRITE_VERSION, 1, 2, "WAL mode"),
        (OFF_RESERVED_SPACE, 1, 32, "reserved space at the end of every page"),
        (OFF_TEXT_ENCODING, 4, 2, "UTF-16le text"),
        (52, 4, 900, "auto-vacuum (largest root page set)"),
        (64, 4, 1, "incremental vacuum"),
        (44, 4, 1, "schema format 1, which lacks serial types 8 and 9"),
    ],
)
def test_unsupported_features_are_refused(offset: int, size: int, value: int, what: str) -> None:
    data = bytearray(FileHeader().to_bytes())
    data[offset:offset + size] = value.to_bytes(size, "big")
    with pytest.raises(InvalidHeaderError):
        FileHeader.from_bytes(bytes(data))




def test_sqlite3_accepts_our_header(tmp_path) -> None:
    """The acceptance test for the entire format.


    A one-page database -- 100 bytes of header, then an empty leaf table b-tree
    at offset 100 -- is enough for sqlite3 to open and verify.
    """
    db = tmp_path / "probe.db"
    page = bytearray(4096)
    page[:100] = FileHeader().to_bytes()
    page[100] = 13          # PageType.LEAF_TABLE
    page[105:107] = (4096).to_bytes(2, "big")   # content start = end of page
    db.write_bytes(bytes(page))


    result = subprocess.run(
        ["sqlite3", str(db), "PRAGMA integrity_check;"],
        capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == "ok"




