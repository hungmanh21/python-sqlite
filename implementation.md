# quilldb — Implementation Plan


Companion to `guide.md` (concepts) and `roadmap.md` (schedule). **This is the coding spec.**


How to use it: for each file you get the **signatures with docstrings** (stubbed with
`NotImplementedError`) and the **tests already written**. Your job is to make the tests pass. The
tests *are* the specification — when they're green, that file is done.


Copy the stubs and tests in verbatim. Write the bodies yourself.


---


## Session 0: The First 30 Minutes


Right now, before anything else:


```bash
cd /prj/corp/airesearch/lasvegas/vol11-scratch/users/hmanh/road_to_l6/sqlite_scratch
mkdir -p quilldb/{src/quilldb/{codec,storage,btree},tests/unit}
cd quilldb
git init
python -m venv .venv && source .venv/bin/activate
pip install pytest pytest-cov hypothesis mypy ruff
```


`pyproject.toml`:


```toml
[project]
name = "quilldb"
version = "0.1.0"
requires-python = ">=3.11"


[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"


[tool.setuptools.packages.find]
where = ["src"]


[tool.pytest.ini_options]
testpaths = ["tests"]


[tool.mypy]
strict = true
files = ["src"]


[tool.ruff]
line-length = 100
```


Then:


```bash
pip install -e .
touch src/quilldb/__init__.py src/quilldb/codec/__init__.py src/quilldb/storage/__init__.py
echo "def test_setup_works(): assert True" > tests/unit/test_smoke.py
pytest
git add -A && git commit -m "Project scaffolding"
```


One passing test and one commit. You've started.


---


## Week 1 File Map


Build in this order. Each file depends only on the ones above it.


| # | File | What it does | LOC |
|---|---|---|---|
| 1 | `src/quilldb/errors.py` | Exception hierarchy | ~30 |
| 2 | `src/quilldb/constants.py` | Page size, magic bytes, offsets | ~25 |
| 3 | `src/quilldb/codec/varint.py` | Variable-length integers | ~60 |
| 4 | `src/quilldb/codec/record.py` | Rows ↔ bytes | ~120 |
| 5 | `src/quilldb/storage/header.py` | The 100-byte file header | ~90 |
| 6 | `src/quilldb/storage/pager.py` | Read/write/allocate/free pages | ~130 |
| 7 | `src/quilldb/storage/page.py` | Slotted page layout | ~140 |
| 8 | `src/quilldb/storage/bufferpool.py` | LRU cache with pin counts | ~110 |
| 9 | `src/quilldb/cli.py` | `quilldb inspect` | ~40 |


~750 lines. That's Week 1.


> **Note:** this reorders the guide slightly — the record codec moves from session 7 to session 3.
> `varint` and `record` are pure functions with no I/O, so doing them back-to-back keeps you in one
> mode and means you have real data to write once the pager exists.


---


## 1. `errors.py`


Every failure mode gets a type. **Never use `assert` for malformed input** — asserts vanish under
`python -O`, and a bare `IndexError` from deep in a decoder tells you nothing.


```python
"""Exception hierarchy for quilldb.


Rule: anything derived from CorruptDatabaseError means the FILE is bad.
Anything else means the CALLER did something wrong, or is internal control flow.
"""




class QuillDBError(Exception):
    """Base for every error quilldb raises."""




class DatabaseError(QuillDBError):
    """A problem with a database file or its contents."""




class CorruptDatabaseError(DatabaseError):
    """The file violates the format. Never raise this for caller mistakes."""




class InvalidHeaderError(CorruptDatabaseError):
    """Magic bytes wrong, page size illegal, or header self-inconsistent."""




class PageOutOfRangeError(CorruptDatabaseError):
    """Asked for a page number outside the file."""




class InvalidPageTypeError(CorruptDatabaseError):
    """A page's type byte is not a known PageType."""




class MalformedCellError(CorruptDatabaseError):
    """A cell offset or length points outside its page."""




class MalformedRecordError(CorruptDatabaseError):
    """A record header or body is truncated or has a reserved serial type."""




class OverflowCycleError(CorruptDatabaseError):
    """An overflow page chain loops back on itself."""




class BTreeInvariantError(CorruptDatabaseError):
    """The validator found a structural violation."""




class PageFullError(QuillDBError):
    """Not enough room on a page. Internal control flow — triggers a split."""




class UnsupportedFeatureError(QuillDBError):
    """A valid file using a feature quilldb does not implement."""
```


No tests needed. Just write it.


---


## 2. `constants.py`


> **`src/quilldb/constants.py` is the source of truth — read it, don't read this.** It used to be
> duplicated here and the two copies drifted (this file said `MAX_VARINT_BYTES = 10`, the code said
> `8`, and the format says `9`). What follows is a summary of the shape; the offsets live in one file
> only, and every value in it comes from
> [fileformat2.html](https://www.sqlite.org/fileformat2.html).


quilldb writes the **real SQLite on-disk format**, so nothing in `constants.py` is a design choice:


```python
PAGE_SIZE = 4096
MAGIC = b"SQLite format 3\x00"     # exactly 16 bytes, NUL-terminated


FILE_HEADER_SIZE = 100
SCHEMA_ROOT_PAGE = 1               # page 1 IS the sqlite_schema root b-tree page
SCHEMA_PAGE_HEADER_OFFSET = 100    # ...so its page header starts after the file header


RESERVED_SPACE = 0
USABLE_SIZE = PAGE_SIZE - RESERVED_SPACE   # "U" in the overflow formulas


LEAF_HEADER_SIZE = 8               # leaves omit the right-child pointer
INTERIOR_HEADER_SIZE = 12
CELL_POINTER_SIZE = 2
MAX_VARINT_BYTES = 9               # 8 x 7 bits + 1 x 8 bits = 64


class PageType(IntEnum):
    INTERIOR_INDEX = 2
    INTERIOR_TABLE = 5
    LEAF_INDEX = 10
    LEAF_TABLE = 13
```


Plus 22 `OFF_*` file-header offsets. Note `PageType` has no `OVERFLOW` or `FREELIST` member:
**overflow and freelist pages carry no type byte at all** in this format, and are identified only by
how you arrive at them.


**Page header layout** — 8 bytes on a leaf, 12 on an interior page:


```
offset  size  field
     0     1  page type (PageType: 2, 5, 10, or 13 — nothing else is legal)
     1     2  first freeblock offset, 0 if none
     3     2  cell count
     5     2  content start   (byte offset where cell data begins; 0 means 65536)
     7     1  fragmented free bytes (never above 60)
     8     4  right child     (INTERIOR pages ONLY — absent, not zeroed, on leaves)
     ?     ?  cell pointer array — 2 bytes per cell, in key order
```


Two rules that follow, and each is a bug you'll write once:


1. **The header's size depends on its own first byte.** Read byte 0, decode the type, then you know
   whether the cell pointer array starts at 8 or 12. Put it behind `header_size(page_type)`.
2. **Page 1's page header starts at byte 100**, behind the file header, and page 1 has 100 fewer
   usable bytes. Put that behind `page_header_offset(page_id)` and never inline the comparison.


Chapter 02 §2.5 explains what each field buys and why the 8/12 asymmetry is worth its branch.


---


## 3. `codec/varint.py`


### The stub


```python
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


from quilldb.constants import MAX_VARINT_BYTES
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
    raise NotImplementedError




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
    raise NotImplementedError




def to_twos_complement(value: int) -> int:
    """Reinterpret a signed 64-bit integer as the u64 bit pattern to encode.


    There is no zigzag in this format. Rowids are signed and stored as the
    varint of their two's-complement bit pattern, so negative rowids cost the
    full 9 bytes. SQLite accepts that; negative rowids are pathological.


    Args:
        value: -2**63 <= value < 2**63.
    """
    raise NotImplementedError




def from_twos_complement(value: int) -> int:
    """Reinterpret a decoded u64 as a signed 64-bit integer. Inverse of above."""
    raise NotImplementedError
```


**Hints, not answers.** Encoding: split into 7-bit groups most-significant-first; set the high bit on
every byte except the last — *except* when the value needs more than 56 bits, where you emit 8
continuation bytes and then the low 8 bits raw. Decoding: loop at most 8 times accumulating 7 bits
each; if you reach the 9th byte, shift by 8 and take the whole byte. Two's complement:
`value & 0xFFFF_FFFF_FFFF_FFFF` one way, and subtract `2**64` if the top bit is set the other way.


> ⚠️ **The 8→9 byte transition is the bug in this file.** Everything below 2⁵⁶ works with a naive
> 7-bits-per-byte loop, so a test suite that only checks small numbers passes while the encoder is
> wrong for large rowids and large payload lengths — which you won't hit until a table gets big or a
> row gets long. The parametrised boundary test below exists specifically for this.


### The tests


`tests/unit/test_varint.py`:


```python
import pytest
from hypothesis import given, strategies as st


from quilldb.codec.varint import (
    decode_uvarint, encode_uvarint, from_twos_complement, to_twos_complement,
)
from quilldb.errors import MalformedRecordError




@given(st.integers(min_value=0, max_value=2**64 - 1))
def test_uvarint_roundtrip(n: int) -> None:
    encoded = encode_uvarint(n)
    value, consumed = decode_uvarint(encoded)
    assert value == n
    assert consumed == len(encoded)




@pytest.mark.parametrize(
    "n,expected_len",
    [
        (0, 1), (1, 1), (127, 1),            # 7 bits
        (128, 2), (16_383, 2),               # 14 bits
        (16_384, 3), (2**21 - 1, 3),         # 21 bits
        (2**21, 4),
        (2**49 - 1, 7), (2**49, 8),          # 49 -> 56 bits
        (2**56 - 1, 8),                      # last value the 7-bit loop handles
        (2**56, 9),                          # THE ninth-byte transition
        (2**64 - 1, 9),                      # worst case
    ],
)
def test_uvarint_length_boundaries(n: int, expected_len: int) -> None:
    """Every encoded length must be exercised. This catches off-by-one shifts.


    The (2**56, 9) row is the one that matters: a naive 7-bits-per-byte encoder
    produces 10 bytes here and passes every other row in this table.
    """
    assert len(encode_uvarint(n)) == expected_len




def test_uvarint_known_encodings() -> None:
    """Pin the wire format so a future refactor can't silently change it."""
    assert encode_uvarint(0) == b"\x00"
    assert encode_uvarint(5) == b"\x05"
    assert encode_uvarint(127) == b"\x7f"
    assert encode_uvarint(128) == b"\x81\x00"
    assert encode_uvarint(300) == b"\x82\x2c"
    assert encode_uvarint(2**64 - 1) == b"\xff" * 9   # all nine bytes saturated




def test_decode_stops_at_varint_boundary() -> None:
    data = encode_uvarint(300) + b"\xff\xff\xff"
    value, consumed = decode_uvarint(data)
    assert (value, consumed) == (300, 2)




def test_decode_at_offset() -> None:
    data = b"junkjunk" + encode_uvarint(999)
    value, _ = decode_uvarint(data, offset=8)
    assert value == 999




def test_decode_truncated_raises() -> None:
    with pytest.raises(MalformedRecordError):
        decode_uvarint(b"\x82")            # continuation bit set, nothing follows




def test_decode_empty_raises() -> None:
    with pytest.raises(MalformedRecordError):
        decode_uvarint(b"")




def test_nine_bytes_always_terminates() -> None:
    """There is no 'overlong varint' error, because byte 9 has no flag to set.


    Nine 0xFF bytes is the largest legal varint, not a malformed one. Trailing
    bytes after it must be left alone.
    """
    value, consumed = decode_uvarint(b"\xff" * 11)
    assert (value, consumed) == (2**64 - 1, 9)




def test_encode_rejects_negative() -> None:
    with pytest.raises(ValueError):
        encode_uvarint(-1)




def test_encode_rejects_too_large() -> None:
    with pytest.raises(ValueError):
        encode_uvarint(2**64)




@given(st.integers(min_value=-(2**63), max_value=2**63 - 1))
def test_twos_complement_roundtrip(n: int) -> None:
    """Signed rowids survive the trip through an unsigned varint."""
    encoded = encode_uvarint(to_twos_complement(n))
    raw, consumed = decode_uvarint(encoded)
    assert from_twos_complement(raw) == n
    assert consumed == len(encoded)




def test_negative_rowids_cost_nine_bytes() -> None:
    """Not a bug — the documented consequence of having no zigzag.


    If this ever passes with a smaller number, someone reintroduced zigzag and
    every file quilldb writes is now unreadable by sqlite3.
    """
    for n in (-1, -5, -(2**62)):
        assert len(encode_uvarint(to_twos_complement(n))) == 9




def test_twos_complement_known_values() -> None:
    assert to_twos_complement(0) == 0
    assert to_twos_complement(-1) == 2**64 - 1
    assert to_twos_complement(-(2**63)) == 2**63
    assert from_twos_complement(2**64 - 1) == -1




def test_memoryview_input_works() -> None:
    """Decoders must accept memoryview so page reads stay zero-copy."""
    buf = memoryview(bytearray(encode_uvarint(12345)))
    assert decode_uvarint(buf)[0] == 12345
```


---


## 4. `codec/record.py`


### The stub


```python
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


from collections.abc import Sequence


Value = None | int | float | str | bytes




def serial_type_for(value: Value) -> tuple[int, bytes]:
    """Classify one value.


    Returns:
        (serial_type, encoded_body_bytes). Body is b"" for NULL, 0 and 1.
    Raises:
        TypeError: unsupported Python type.
        ValueError: integer outside signed 64-bit range.
    """
    raise NotImplementedError




def decode_value(serial_type: int, data: bytes | memoryview, offset: int) -> tuple[Value, int]:
    """Decode one value of a known serial type.


    Returns:
        (value, bytes_consumed).
    Raises:
        MalformedRecordError: reserved type, or buffer too short.
    """
    raise NotImplementedError




def encode_record(values: Sequence[Value]) -> bytes:
    """Encode a row.


    Args:
        values: the column values, in column order.
    Returns:
        The complete record. encode_record([]) is legal and yields just a header.
    """
    raise NotImplementedError




def decode_record(data: bytes | memoryview, offset: int = 0) -> tuple[Value, ...]:
    """Decode a full row starting at `offset`.


    Raises:
        MalformedRecordError: truncated, or header_len inconsistent with content.
    """
    raise NotImplementedError




def decode_column(data: bytes | memoryview, offset: int, column: int) -> Value:
    """Decode ONE column without materializing the others.


    This is the payoff for the manifest design — read the header, sum the
    preceding lengths, jump. Used by Filter to test one column cheaply.


    Raises:
        IndexError: column >= number of columns in the record.
    """
    raise NotImplementedError
```


**Hints.** Use `struct.pack(">d", x)` for floats. For integers, pick the *smallest* type that fits
(signed): `-128..127` → 1 byte, and so on. `int.to_bytes(n, "big", signed=True)` and
`int.from_bytes(b, "big", signed=True)` do the work. Handle 0 and 1 specially *before* the width
check so they get types 8 and 9.


Careful with header_len: it counts itself. If the serial types occupy 3 bytes and header_len is 1
byte, header_len == 4. If your serial types are long enough that header_len needs 2 bytes, the value
changes — a fixed point worth thinking about. Simplest correct approach: compute the type bytes,
then loop until `len(encode_uvarint(candidate)) + len(type_bytes) == candidate`.


### The tests


`tests/unit/test_record.py`:


```python
import pytest
from hypothesis import given, strategies as st


from quilldb.codec.record import (
    Value, decode_column, decode_record, encode_record, serial_type_for,
)
from quilldb.errors import MalformedRecordError


values = st.one_of(
    st.none(),
    st.integers(min_value=-(2**63), max_value=2**63 - 1),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(),
    st.binary(),
)




@given(st.lists(values, max_size=20))
def test_record_roundtrip(row: list[Value]) -> None:
    assert decode_record(encode_record(row)) == tuple(row)




@pytest.mark.parametrize(
    "value,expected_type",
    [
        (None, 0),
        (0, 8), (1, 9),                    # value encoded in the type itself
        (5, 1), (-5, 1), (127, 1), (-128, 1),
        (128, 2), (300, 2), (-32_768, 2),
        (2**23, 4), (2**40, 5), (2**62, 6),
        (1.5, 7), (0.0, 7),                # floats never use the int shortcut
        (b"", 12), (b"a", 14), (b"ab", 16),
        ("", 13), ("a", 15), ("ab", 17),
    ],
)
def test_serial_types(value: Value, expected_type: int) -> None:
    assert serial_type_for(value)[0] == expected_type




def test_null_and_small_ints_occupy_no_body_bytes() -> None:
    """A table full of NULLs really is cheaper on disk."""
    for value in (None, 0, 1):
        assert serial_type_for(value)[1] == b""




def test_text_length_is_bytes_not_characters() -> None:
    """Classic bug: len("café") is 4 in Python but 5 bytes in UTF-8."""
    assert serial_type_for("café")[0] == 13 + 2 * 5




def test_multibyte_text_roundtrip() -> None:
    row = ("café", "日本語", "🎉")
    assert decode_record(encode_record(row)) == row




def test_empty_record() -> None:
    assert decode_record(encode_record([])) == ()




def test_decode_at_offset() -> None:
    data = b"xxxx" + encode_record((1, "a"))
    assert decode_record(data, offset=4) == (1, "a")




def test_decode_ignores_trailing_bytes() -> None:
    """Records live inside pages — there is always data after them."""
    data = encode_record((1, "a")) + b"garbage"
    assert decode_record(data) == (1, "a")




@pytest.mark.parametrize("column,expected", [(0, 42), (1, "ada"), (2, None), (3, 1.5)])
def test_decode_single_column(column: int, expected: Value) -> None:
    data = encode_record((42, "ada", None, 1.5))
    assert decode_column(data, 0, column) == expected




def test_decode_column_out_of_range() -> None:
    data = encode_record((1, 2))
    with pytest.raises(IndexError):
        decode_column(data, 0, 5)




def test_truncated_body_raises() -> None:
    data = encode_record((1, "hello"))
    with pytest.raises(MalformedRecordError):
        decode_record(data[:-3])




def test_truncated_header_raises() -> None:
    with pytest.raises(MalformedRecordError):
        decode_record(b"\x05")             # claims a 5-byte header, supplies none




@pytest.mark.parametrize("reserved", [10, 11])
def test_reserved_serial_types_raise(reserved: int) -> None:
    from quilldb.codec.varint import encode_uvarint
    body = encode_uvarint(reserved)
    data = encode_uvarint(1 + len(body)) + body
    with pytest.raises(MalformedRecordError):
        decode_record(data)




def test_int_out_of_range_rejected() -> None:
    with pytest.raises(ValueError):
        serial_type_for(2**63)




def test_unsupported_type_rejected() -> None:
    with pytest.raises(TypeError):
        serial_type_for({"a": 1})          # type: ignore[arg-type]




@given(st.lists(values, min_size=1, max_size=10))
def test_decode_column_matches_full_decode(row: list[Value]) -> None:
    """The fast path must agree with the slow path. Always."""
    data = encode_record(row)
    full = decode_record(data)
    for i in range(len(row)):
        assert decode_column(data, 0, i) == full[i]
```


That last test is the pattern to reuse everywhere: **two implementations of the same question must
agree.** It's how you test things with no external oracle.


---


## 5. `storage/header.py`


```python
"""The 100-byte SQLite file header, living at the start of page 1.


This is SQLite's exact layout (fileformat2.html §1.3). Note what that means:
page 1 is NOT header-only. It is simultaneously the file header (bytes 0-99)
and the root b-tree page of `sqlite_schema`, whose own page header begins at
byte 100. Page 1 therefore has 100 fewer usable bytes than every other page.
See docs/theory/01-pages-and-the-pager.md §1.6.
"""


from dataclasses import dataclass




@dataclass
class FileHeader:
    """Defaults describe a freshly created, empty database: exactly ONE page."""


    page_size: int = 4096
    write_version: int = 1           # 1 = rollback journal, 2 = WAL
    read_version: int = 1
    reserved_space: int = 0
    change_counter: int = 0
    page_count: int = 1              # page 1 alone is a valid empty database
    freelist_trunk: int = 0          # 0 means empty
    freelist_count: int = 0
    schema_cookie: int = 0
    schema_format: int = 4           # 4 enables serial types 8 and 9
    default_cache_size: int = 0
    largest_root_page: int = 0       # auto-vacuum only
    text_encoding: int = 1           # UTF-8
    user_version: int = 0
    incremental_vacuum: int = 0
    application_id: int = 0
    version_valid_for: int = 0
    sqlite_version: int = 3045000


    @property
    def usable_size(self) -> int:
        """"U" in the overflow formulas. Never allowed below 480."""
        return self.page_size - self.reserved_space


    def to_bytes(self) -> bytes:
        """Serialize to exactly 100 bytes. Reserved bytes 72..91 must be zero."""
        raise NotImplementedError


    def check_supported(self) -> None:
        """Refuse files that are valid SQLite but outside quilldb's subset.


        The interesting decision in this module: for each of page_size,
        write_version, read_version, reserved_space, text_encoding,
        schema_format, largest_root_page and incremental_vacuum, choose REFUSE
        or TOLERATE. Reason from: TOLERATE is safe only when misreading the
        field cannot corrupt data or return wrong answers.
        """
        raise NotImplementedError


    @classmethod
    def from_bytes(cls, data: bytes | memoryview) -> "FileHeader":
        """Parse and validate, then call check_supported().


        Raises:
            InvalidHeaderError: buffer too short, bad magic, page size not a
                power of two in 512..65536, page_count == 0, a payload fraction
                that isn't 64/32/32, or usable_size below 480.
        """
        raise NotImplementedError
```


> **Watch the page-size encoding.** The field is 2 bytes but page sizes run to 65536, so **the
> stored value 1 means 65536**. Decode it before validating, or you'll reject a legal file for having
> a page size of 1.


`tests/unit/test_header.py` — see `src/tests/unit/test_header.py` for the full suite. The shape:


```python
def test_roundtrip() -> None:
    header = FileHeader(page_count=7, freelist_trunk=3, freelist_count=1, change_counter=99)
    assert FileHeader.from_bytes(header.to_bytes()) == header




def test_starts_with_magic() -> None:
    assert FileHeader().to_bytes()[:16] == b"SQLite format 3\x00"




def test_payload_fractions_are_64_32_32() -> None:
    """The spec fixes these. sqlite3 checks them."""
    assert FileHeader().to_bytes()[21:24] == bytes([64, 32, 32])




def test_reserved_bytes_are_zero() -> None:
    """Bytes 72..91 only — everything else is a real field."""
    assert FileHeader().to_bytes()[72:92] == b"\x00" * 20




def test_bad_magic_rejected() -> None:
    data = bytearray(FileHeader().to_bytes())
    data[0:4] = b"JUNK"          # NOT b"SQLi" — that prefix is now correct
    with pytest.raises(InvalidHeaderError):
        FileHeader.from_bytes(bytes(data))




@pytest.mark.parametrize(
    "offset,size,value,what",
    [
        (16, 2, 8192, "a legal but unsupported page size"),
        (18, 1, 2, "WAL mode"),
        (20, 1, 32, "reserved space at the end of every page"),
        (56, 4, 2, "UTF-16le text"),
        (52, 4, 900, "auto-vacuum"),
        (44, 4, 1, "schema format 1, which lacks serial types 8 and 9"),
    ],
)
def test_unsupported_features_are_refused(offset, size, value, what) -> None:
    """Valid SQLite, outside quilldb's subset. REFUSE, never silently misread."""
    data = bytearray(FileHeader().to_bytes())
    data[offset:offset + size] = value.to_bytes(size, "big")
    with pytest.raises(InvalidHeaderError):
        FileHeader.from_bytes(bytes(data))




def test_sqlite3_accepts_our_header(tmp_path) -> None:
    """The acceptance test for the entire format.


    100 bytes of header, then an empty leaf table b-tree at offset 100, is a
    complete one-page SQLite database.
    """
    db = tmp_path / "probe.db"
    page = bytearray(4096)
    page[:100] = FileHeader().to_bytes()
    page[100] = 13                              # PageType.LEAF_TABLE
    page[105:107] = (4096).to_bytes(2, "big")   # content start = end of page
    db.write_bytes(bytes(page))


    result = subprocess.run(["sqlite3", str(db), "PRAGMA integrity_check;"],
                            capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "ok"
```


---


## 6. `storage/pager.py`


The layer everything above depends on. Nothing else in quilldb may call `read()` or `write()`.


```python
"""Page-level file access.


Page N occupies bytes [(N-1) * PAGE_SIZE, N * PAGE_SIZE). Page numbering starts
at 1 so that 0 can mean "no page" in child and freelist pointers.


Freed pages form a linked list: each free page stores the next free page number
in its first 4 bytes, and the head is in the file header.
"""


from pathlib import Path




class Pager:
    def __init__(self, path: Path) -> None:
        """Prefer Pager.create() or Pager.open()."""
        raise NotImplementedError


    @classmethod
    def create(cls, path: Path) -> "Pager":
        """Create a new database file: page 1 header, page 2 empty leaf.


        Raises:
            FileExistsError: path already exists.
        """
        raise NotImplementedError


    @classmethod
    def open(cls, path: Path) -> "Pager":
        """Open an existing file and validate its header.


        Raises:
            FileNotFoundError, InvalidHeaderError.
            CorruptDatabaseError: file length is not a whole number of pages,
                or is shorter than the header's page_count claims.
        """
        raise NotImplementedError


    @property
    def page_count(self) -> int:
        raise NotImplementedError


    def read_page(self, page_id: int) -> bytearray:
        """Read one page.


        Returns:
            A fresh mutable 4096-byte bytearray. Mutating it does NOT write to
            disk — call write_page.
        Raises:
            PageOutOfRangeError: page_id < 1 or > page_count.
        """
        raise NotImplementedError


    def write_page(self, page_id: int, data: bytes | bytearray) -> None:
        """Write one page.


        Raises:
            PageOutOfRangeError, ValueError: len(data) != PAGE_SIZE.
        """
        raise NotImplementedError


    def allocate_page(self) -> int:
        """Get a page for new data — reusing a freed one if any.


        Returns:
            The page number. Contents are undefined; the caller must initialize.
        """
        raise NotImplementedError


    def free_page(self, page_id: int) -> None:
        """Return a page to the freelist.


        Raises:
            PageOutOfRangeError.
            ValueError: page_id is 1 (the header page).
        """
        raise NotImplementedError


    def sync(self) -> None:
        """os.fsync. Becomes load-bearing in Week 5."""
        raise NotImplementedError


    def close(self) -> None:
        """Flush the header and close the file."""
        raise NotImplementedError
```


`tests/unit/test_pager.py`:


```python
import pytest


from quilldb.constants import PAGE_SIZE
from quilldb.errors import CorruptDatabaseError, PageOutOfRangeError
from quilldb.storage.pager import Pager




@pytest.fixture
def pager(tmp_path):
    p = Pager.create(tmp_path / "test.db")
    yield p
    p.close()




def test_create_makes_two_pages(pager) -> None:
    assert pager.page_count == 2




def test_file_size_is_whole_pages(tmp_path) -> None:
    path = tmp_path / "t.db"
    p = Pager.create(path)
    p.close()
    assert path.stat().st_size % PAGE_SIZE == 0




def test_write_then_read(pager) -> None:
    page_id = pager.allocate_page()
    data = bytearray(b"\xab" * PAGE_SIZE)
    pager.write_page(page_id, data)
    assert pager.read_page(page_id) == data




def test_read_returns_independent_copy(pager) -> None:
    """Mutating a returned page must not silently change what's on disk."""
    page_id = pager.allocate_page()
    pager.write_page(page_id, bytearray(PAGE_SIZE))
    page = pager.read_page(page_id)
    page[0] = 0xFF
    assert pager.read_page(page_id)[0] == 0x00




def test_survives_reopen(tmp_path) -> None:
    path = tmp_path / "t.db"
    p = Pager.create(path)
    page_id = p.allocate_page()
    p.write_page(page_id, bytearray(b"\x42" * PAGE_SIZE))
    p.close()


    p2 = Pager.open(path)
    assert p2.read_page(page_id)[0] == 0x42
    assert p2.page_count >= page_id
    p2.close()




@pytest.mark.parametrize("bad_id", [0, -1, 9999])
def test_out_of_range_reads_rejected(pager, bad_id: int) -> None:
    with pytest.raises(PageOutOfRangeError):
        pager.read_page(bad_id)




def test_wrong_size_write_rejected(pager) -> None:
    with pytest.raises(ValueError):
        pager.write_page(2, b"too short")




def test_allocate_grows_file(pager) -> None:
    before = pager.page_count
    new_id = pager.allocate_page()
    assert new_id == before + 1
    assert pager.page_count == before + 1




def test_freed_page_is_reused(pager) -> None:
    """The whole point of a freelist: DELETE then INSERT must not grow the file."""
    a, b, c = pager.allocate_page(), pager.allocate_page(), pager.allocate_page()
    count_before = pager.page_count
    pager.free_page(b)
    assert pager.allocate_page() == b
    assert pager.page_count == count_before




def test_freelist_is_lifo_and_exhausts(pager) -> None:
    ids = [pager.allocate_page() for _ in range(3)]
    for page_id in ids:
        pager.free_page(page_id)
    reused = {pager.allocate_page() for _ in range(3)}
    assert reused == set(ids)
    # freelist now empty -> next allocation must extend the file
    assert pager.allocate_page() == pager.page_count




def test_freelist_survives_reopen(tmp_path) -> None:
    path = tmp_path / "t.db"
    p = Pager.create(path)
    page_id = p.allocate_page()
    p.free_page(page_id)
    p.close()


    p2 = Pager.open(path)
    assert p2.allocate_page() == page_id
    p2.close()




def test_cannot_free_header_page(pager) -> None:
    with pytest.raises(ValueError):
        pager.free_page(1)




def test_truncated_file_rejected(tmp_path) -> None:
    path = tmp_path / "t.db"
    p = Pager.create(path)
    p.allocate_page()
    p.close()
    with open(path, "r+b") as f:
        f.truncate(PAGE_SIZE + 100)        # not a whole number of pages
    with pytest.raises(CorruptDatabaseError):
        Pager.open(path)




def test_open_missing_file_raises(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        Pager.open(tmp_path / "nope.db")




def test_create_existing_file_raises(tmp_path) -> None:
    path = tmp_path / "t.db"
    Pager.create(path).close()
    with pytest.raises(FileExistsError):
        Pager.create(path)
```


---


## 7. `storage/page.py`


**The design decision that makes this easy.** Rather than manipulating bytes in place, parse a page
into a Python object, mutate that, and re-serialize. Slower; far simpler; far fewer corruption paths.
This is the roadmap's §10.2 "reconstruct pages before incremental fragmentation management."


```python
"""Slotted page layout.


    +--------+-------------+-----------+------------------+
    | header | cell ptrs ->|   free    |<- cell data      |
    | 12 B   | 2 B each    |           |   variable       |
    +--------+-------------+-----------+------------------+


Cell pointers grow down from byte 12; cell data grows up from byte 4095. The
page is full when they meet. Pointers are in KEY order; data is in whatever
order it was written.


We parse to a PageBody, mutate the Python list, and re-serialize. SQLite instead
edits bytes in place and tracks freeblocks — faster, much harder to get right.
See ADR-003.
"""


from dataclasses import dataclass, field


from quilldb.constants import PageType




@dataclass
class PageBody:
    """A page as Python objects. `cells` is in key order."""


    page_type: PageType
    cells: list[bytes] = field(default_factory=list)
    right_child: int = 0             # INTERIOR only


    @property
    def cell_count(self) -> int:
        raise NotImplementedError


    def used_bytes(self) -> int:
        """Bytes needed to serialize: header + pointers + cell data."""
        raise NotImplementedError


    def free_bytes(self) -> int:
        """PAGE_SIZE - used_bytes()."""
        raise NotImplementedError


    def fits(self, payload_len: int) -> bool:
        """Would one more cell of `payload_len` bytes fit?


        Remember the 2-byte pointer, not just the payload.
        """
        raise NotImplementedError


    def insert_cell(self, index: int, payload: bytes) -> None:
        """Insert at `index`, shifting later cells right.


        Raises:
            PageFullError: does not fit. Caller splits.
            IndexError: index out of range.
        """
        raise NotImplementedError


    def delete_cell(self, index: int) -> None:
        raise NotImplementedError




def parse_page(data: bytes | memoryview) -> PageBody:
    """Decode a raw page.


    Raises:
        InvalidPageTypeError: unknown type byte.
        MalformedCellError: a cell pointer or length falls outside the page,
            or cells overlap.
    """
    raise NotImplementedError




def serialize_page(body: PageBody) -> bytearray:
    """Encode to exactly PAGE_SIZE bytes.


    Writes cell data packed tight against the end of the page, so a
    freshly-serialized page has zero fragmentation.


    Raises:
        PageFullError: body.used_bytes() > PAGE_SIZE.
    """
    raise NotImplementedError
```


`tests/unit/test_page.py`:


```python
import pytest
from hypothesis import given, strategies as st


from quilldb.constants import PAGE_SIZE, PageType
from quilldb.errors import InvalidPageTypeError, MalformedCellError, PageFullError
from quilldb.storage.page import PageBody, parse_page, serialize_page




def test_empty_page_roundtrip() -> None:
    body = PageBody(page_type=PageType.LEAF)
    assert parse_page(serialize_page(body)) == body




def test_serialize_is_exactly_page_size() -> None:
    body = PageBody(PageType.LEAF, cells=[b"hello", b"world"])
    assert len(serialize_page(body)) == PAGE_SIZE




def test_cells_roundtrip_in_order() -> None:
    cells = [b"aaa", b"bb", b"cccc"]
    body = PageBody(PageType.LEAF, cells=list(cells))
    assert parse_page(serialize_page(body)).cells == cells




def test_right_child_roundtrip() -> None:
    body = PageBody(PageType.INTERIOR, cells=[b"x"], right_child=77)
    assert parse_page(serialize_page(body)).right_child == 77




@given(st.lists(st.binary(min_size=1, max_size=200), max_size=15))
def test_roundtrip_property(cells: list[bytes]) -> None:
    body = PageBody(PageType.LEAF, cells=cells)
    assert parse_page(serialize_page(body)).cells == cells




@pytest.mark.parametrize("index", [0, 1, 2])
def test_insert_at_position(index: int) -> None:
    body = PageBody(PageType.LEAF, cells=[b"a", b"c"])
    body.insert_cell(index, b"NEW")
    assert body.cells[index] == b"NEW"
    assert body.cell_count == 3




def test_delete_cell() -> None:
    body = PageBody(PageType.LEAF, cells=[b"a", b"b", b"c"])
    body.delete_cell(1)
    assert body.cells == [b"a", b"c"]




def test_free_bytes_accounting() -> None:
    body = PageBody(PageType.LEAF)
    empty = body.free_bytes()
    body.insert_cell(0, b"x" * 100)
    assert body.free_bytes() == empty - 100 - 2      # payload + pointer




def test_fits_accounts_for_pointer() -> None:
    """The classic off-by-two: a cell costs payload + 2 bytes of pointer."""
    body = PageBody(PageType.LEAF)
    exact = body.free_bytes() - 2
    assert body.fits(exact)
    assert not body.fits(exact + 1)




def test_fill_page_exactly_then_overflow() -> None:
    body = PageBody(PageType.LEAF)
    payload = b"x" * 100
    while body.fits(len(payload)):
        body.insert_cell(body.cell_count, payload)
    assert len(serialize_page(body)) == PAGE_SIZE     # exactly full is still valid
    with pytest.raises(PageFullError):
        body.insert_cell(body.cell_count, payload)




def test_delete_then_insert_reclaims_space() -> None:
    """Re-serializing must defragment — no space leaked by churn."""
    body = PageBody(PageType.LEAF)
    while body.fits(100):
        body.insert_cell(body.cell_count, b"y" * 100)
    body.delete_cell(0)
    body = parse_page(serialize_page(body))
    assert body.fits(100)




def test_bad_page_type_rejected() -> None:
    data = bytearray(serialize_page(PageBody(PageType.LEAF)))
    data[0] = 99
    with pytest.raises(InvalidPageTypeError):
        parse_page(bytes(data))




def test_cell_pointer_past_end_rejected() -> None:
    data = bytearray(serialize_page(PageBody(PageType.LEAF, cells=[b"abc"])))
    data[12:14] = (PAGE_SIZE + 10).to_bytes(2, "big")
    with pytest.raises(MalformedCellError):
        parse_page(bytes(data))




def test_cell_pointer_into_header_rejected() -> None:
    data = bytearray(serialize_page(PageBody(PageType.LEAF, cells=[b"abc"])))
    data[12:14] = (3).to_bytes(2, "big")
    with pytest.raises(MalformedCellError):
        parse_page(bytes(data))




def test_absurd_cell_count_rejected() -> None:
    data = bytearray(serialize_page(PageBody(PageType.LEAF)))
    data[2:4] = (5000).to_bytes(2, "big")            # more pointers than fit
    with pytest.raises(MalformedCellError):
        parse_page(bytes(data))
```


Those last four are your first **corruption tests**. Get in the habit now: every parser gets tests
that feed it deliberately broken bytes and assert a *typed* error. This is what makes a
defensively-written database, and it's cheap while the parser is fresh in your head.


---


## 8. `storage/bufferpool.py`


```python
"""Bounded page cache with pin counts and LRU eviction.


Three rules:
  1. A modified ("dirty") page must be written before it leaves the cache.
  2. A pinned page (someone is using it) can never be evicted.
  3. When full, evict the least recently used unpinned page.


Two callers asking for page N get THE SAME bytearray object. This is what makes
mutations visible across cursors — and what makes Week 6 thread safety matter.
"""


from collections.abc import Iterator
from contextlib import contextmanager


from quilldb.storage.pager import Pager




class BufferPool:
    def __init__(self, pager: Pager, capacity: int = 128) -> None:
        """Args:
            capacity: max pages held in memory. Must be >= 1.
        """
        raise NotImplementedError


    def pin(self, page_id: int) -> bytearray:
        """Get a page and increment its pin count.


        Returns:
            The cached bytearray — the same object on every call for this page.
            You MUST call unpin() when done. Prefer the `pinned()` context manager.
        Raises:
            PageOutOfRangeError.
            RuntimeError: cache is full and every page is pinned.
        """
        raise NotImplementedError


    def unpin(self, page_id: int, dirty: bool = False) -> None:
        """Release a pin.


        Args:
            dirty: True if you modified the page. Sticky — once dirty, a page
                stays dirty until written.
        Raises:
            ValueError: page not pinned.
        """
        raise NotImplementedError


    @contextmanager
    def pinned(self, page_id: int, dirty: bool = False) -> Iterator[bytearray]:
        """Pin for the duration of a block. Use this everywhere.


            with pool.pinned(4, dirty=True) as page:
                page[0] = 1
        """
        raise NotImplementedError


    def flush_page(self, page_id: int) -> None:
        raise NotImplementedError


    def flush_all(self) -> None:
        """Write every dirty page. Call before close and before commit."""
        raise NotImplementedError


    @property
    def stats(self) -> dict[str, int]:
        """{"hits", "misses", "evictions"} — for the README benchmark table."""
        raise NotImplementedError
```


**Hint.** `collections.OrderedDict` with `move_to_end()` gives you LRU in two lines. Keep pin counts
and dirty flags in a parallel dict, or wrap each entry in a small dataclass.


`tests/unit/test_bufferpool.py`:


```python
import pytest


from quilldb.constants import PAGE_SIZE
from quilldb.storage.bufferpool import BufferPool
from quilldb.storage.pager import Pager




@pytest.fixture
def pool(tmp_path):
    pager = Pager.create(tmp_path / "t.db")
    for _ in range(20):
        pager.allocate_page()
    for page_id in range(2, pager.page_count + 1):
        pager.write_page(page_id, bytearray([page_id % 256]) * PAGE_SIZE)
    yield BufferPool(pager, capacity=4)
    pager.close()




def test_same_page_returns_same_object(pool) -> None:
    """Critical: two cursors on one page must see each other's writes."""
    a = pool.pin(5)
    b = pool.pin(5)
    assert a is b
    pool.unpin(5)
    pool.unpin(5)




def test_cache_hit_counted(pool) -> None:
    with pool.pinned(5):
        pass
    before = pool.stats["hits"]
    with pool.pinned(5):
        pass
    assert pool.stats["hits"] == before + 1




def test_evicts_when_full(pool) -> None:
    for page_id in range(2, 8):                      # 6 pages, capacity 4
        with pool.pinned(page_id):
            pass
    assert pool.stats["evictions"] > 0




def test_never_evicts_pinned_page(pool) -> None:
    held = pool.pin(2)
    held[0] = 0xEE
    for page_id in range(3, 10):
        with pool.pinned(page_id):
            pass
    assert pool.pin(2) is held                       # still resident
    pool.unpin(2)
    pool.unpin(2, dirty=True)




def test_dirty_page_written_before_eviction(pool, tmp_path) -> None:
    with pool.pinned(3, dirty=True) as page:
        page[0] = 0x99
    for page_id in range(4, 12):                     # force eviction of page 3
        with pool.pinned(page_id):
            pass
    pool.flush_all()
    assert Pager.open(tmp_path / "t.db").read_page(3)[0] == 0x99




def test_clean_page_not_written(pool) -> None:
    """Evicting a page nobody modified must not cost a write."""
    with pool.pinned(3) as page:
        _ = page[0]
    for page_id in range(4, 12):
        with pool.pinned(page_id):
            pass
    # no assertion on disk state; assert via a write counter you add to Pager,
    # or skip this one until you have that instrumentation.




def test_dirty_flag_is_sticky(pool) -> None:
    with pool.pinned(3, dirty=True) as page:
        page[0] = 0x11
    with pool.pinned(3, dirty=False):                # second, clean access
        pass
    pool.flush_all()
    assert pool.pin(3)[0] == 0x11
    pool.unpin(3)




def test_all_pinned_raises(pool) -> None:
    pins = [pool.pin(page_id) for page_id in range(2, 6)]   # capacity is 4
    with pytest.raises(RuntimeError):
        pool.pin(10)
    for page_id in range(2, 6):
        pool.unpin(page_id)




def test_unpin_not_pinned_raises(pool) -> None:
    with pytest.raises(ValueError):
        pool.unpin(7)




def test_context_manager_unpins_on_exception(pool) -> None:
    """A leaked pin makes a page permanently unevictable — silent, fatal later."""
    with pytest.raises(ZeroDivisionError):
        with pool.pinned(5):
            raise ZeroDivisionError
    pool.pin(5)                                       # would raise if pin leaked
    pool.unpin(5)




def test_lru_order(pool) -> None:
    for page_id in (2, 3, 4, 5):
        with pool.pinned(page_id):
            pass
    with pool.pinned(2):                              # refresh 2; now 3 is LRU
        pass
    with pool.pinned(6):
        pass                                          # should evict 3, not 2
    before = pool.stats["misses"]
    with pool.pinned(2):
        pass
    assert pool.stats["misses"] == before             # 2 was a hit
```


---


## 9. `cli.py`


```python
"""python -m quilldb inspect <file>"""




def cmd_inspect(path: str) -> int:
    """Print header fields. Returns a process exit code."""
    raise NotImplementedError




def main(argv: list[str] | None = None) -> int:
    raise NotImplementedError
```


Target output:


```
$ python -m quilldb inspect demo.db
File:            demo.db
Format version:  1
Page size:       4096
Total pages:     7
Freelist head:   0 (0 pages free)
Catalog root:    2
Change counter:  3
```


Add `src/quilldb/__main__.py`:


```python
import sys
from quilldb.cli import main
sys.exit(main(sys.argv[1:]))
```


---


## Week 1 Definition of Done


```bash
pytest -q                 # all green
mypy                      # clean under strict
ruff check src tests      # clean
```


Plus this works end to end:


```python
from pathlib import Path
from quilldb.codec.record import decode_record, encode_record
from quilldb.storage.bufferpool import BufferPool
from quilldb.storage.pager import Pager
from quilldb.storage.page import PageBody, parse_page, serialize_page
from quilldb.constants import PageType


path = Path("demo.db")
pager = Pager.create(path)
pool = BufferPool(pager, capacity=16)


body = PageBody(PageType.LEAF)
body.insert_cell(0, encode_record((1, "ada", 36)))
with pool.pinned(2, dirty=True) as page:
    page[:] = serialize_page(body)
pool.flush_all()
pager.close()


pager = Pager.open(path)
reloaded = parse_page(pager.read_page(2))
assert decode_record(reloaded.cells[0]) == (1, "ada", 36)
print("round-trip through disk: OK")
```


**Write that script as `examples/week1_smoke.py` and commit it.** It's your proof, and in week 8 it
becomes the first frame of your demo.


---


# Week 2 Spec


Same shape, less hand-holding — by now you'll have the rhythm.


| # | File | What it does |
|---|---|---|
| 1 | `btree/cells.py` | Encode/decode leaf and interior cells |
| 2 | `btree/btree.py` | search, insert, split |
| 3 | `btree/cursor.py` | Ordered traversal |
| 4 | `btree/overflow.py` | Payloads larger than a page |
| 5 | `btree/validate.py` | Structural invariant checker |


### `btree/cells.py`


```python
"""Cell formats.


Leaf cell:      [key svarint][payload_len uvarint][payload bytes]
Interior cell:  [child_page u32][key svarint]


Interior cells hold no payload — an interior page is pure navigation. The key is
a separator: "everything in child_page is < key".
"""


from dataclasses import dataclass




@dataclass(frozen=True)
class LeafCell:
    key: int
    payload: bytes


    def encode(self) -> bytes: ...
    @classmethod
    def decode(cls, data: bytes | memoryview, offset: int = 0) -> "LeafCell": ...




@dataclass(frozen=True)
class InteriorCell:
    child_page: int
    key: int


    def encode(self) -> bytes: ...
    @classmethod
    def decode(cls, data: bytes | memoryview, offset: int = 0) -> "InteriorCell": ...
```


### `btree/btree.py`


```python
class BTree:
    def __init__(self, pool: BufferPool, root_page_id: int) -> None: ...


    @classmethod
    def create(cls, pool: BufferPool) -> "BTree":
        """Allocate an empty leaf and return a tree rooted there."""


    def search(self, key: int) -> bytes | None:
        """Point lookup. Returns payload, or None if absent."""


    def insert(self, key: int, payload: bytes) -> None:
        """Insert or replace. Splits pages as needed; may increase tree height."""


    def delete(self, key: int) -> bool:
        """Returns True if a row was removed. Week 4 — stub it now."""


    # --- internals you'll need ---


    def _find_leaf(self, key: int) -> list[tuple[int, int]]:
        """Descend from root to the leaf that would hold `key`.


        Returns:
            The path as [(page_id, cell_index), ...], root first, leaf last.
            The cursor needs this to walk back up and across.
        """


    def _split_leaf(self, page_id: int, path: list[tuple[int, int]]) -> None:
        """Split a full leaf, then insert a separator into the parent.


        If the parent is also full, this recurses. If the ROOT splits, allocate
        a new root and the tree gets one level taller.
        """
```


**Two hints that will save you an evening.** Do `_find_leaf` and `search` *before* `insert`, and
test them against a hand-built fixture tree — don't wait for insert to work. And write
`dump_tree()` in `validate.py` before you attempt splits; you will need it.


### `btree/validate.py`


```python
@dataclass
class ValidationReport:
    errors: list[str]


    @property
    def is_valid(self) -> bool: ...
    def raise_if_invalid(self) -> None:
        """Raises BTreeInvariantError listing every problem found."""




def validate_btree(pool: BufferPool, root_page_id: int) -> ValidationReport:
    """Walk the whole tree and check every invariant:


      - keys strictly increasing within each page
      - every key on a child page inside the range its parent's separators claim
      - every page reachable exactly once (no shared pages, no orphans)
      - all leaves at identical depth
      - every cell offset inside its page
      - no overflow chain cycles
    """




def dump_tree(pool: BufferPool, root_page_id: int, out=sys.stdout) -> None:
    """Print the tree as indented text. Write this FIRST, before splits."""
```


### Week 2 tests


Test file names and the cases that matter:


**`test_cells.py`** — round-trip both cell types; negative keys; empty payload; Hypothesis
round-trip; truncated cell raises `MalformedCellError`.


**`test_btree_search.py`** — build a 2-level tree by hand in a fixture. Find every present key; miss
correctly on absent keys; miss on an empty tree; find keys at both extremes.


**`test_btree_insert.py`** — insert into empty; at start, middle, end; replacing an existing key;
fill a page exactly; then the split cases:


```python
def test_leaf_split_preserves_all_keys(pool) -> None:
    tree = BTree.create(pool)
    keys = list(range(200))                          # forces at least one split
    for k in keys:
        tree.insert(k, f"row{k}".encode())
    for k in keys:
        assert tree.search(k) is not None
    validate_btree(pool, tree.root_page_id).raise_if_invalid()




def test_root_split_increases_height(pool) -> None:
    tree = BTree.create(pool)
    for k in range(5000):
        tree.insert(k, b"x" * 200)
    assert tree_height(pool, tree.root_page_id) >= 3
    validate_btree(pool, tree.root_page_id).raise_if_invalid()




@pytest.mark.parametrize("order", ["ascending", "descending", "random"])
def test_insertion_order_does_not_matter(pool, order: str) -> None:
    """Ascending is the easy path; descending and random find different bugs."""
    keys = list(range(1000))
    if order == "descending":
        keys.reverse()
    elif order == "random":
        random.Random(42).shuffle(keys)              # seeded: failures reproduce
    tree = BTree.create(pool)
    for k in keys:
        tree.insert(k, b"v")
    assert list(TableCursor(pool, tree.root_page_id).scan_keys()) == sorted(keys)
    validate_btree(pool, tree.root_page_id).raise_if_invalid()
```


**`test_btree_stress.py`** — the one that proves it:


```python
@given(st.lists(st.integers(0, 10_000), min_size=1, max_size=2000))
@settings(deadline=None, max_examples=25)
def test_btree_matches_dict_oracle(tmp_path_factory, keys: list[int]) -> None:
    """Compare against a Python dict — your oracle for weeks 2-4."""
    pager = Pager.create(tmp_path_factory.mktemp("d") / "t.db")
    pool = BufferPool(pager, capacity=32)
    tree = BTree.create(pool)
    oracle: dict[int, bytes] = {}


    for k in keys:
        payload = f"v{k}".encode()
        tree.insert(k, payload)
        oracle[k] = payload


    for k, expected in oracle.items():
        assert tree.search(k) == expected
    assert list(TableCursor(pool, tree.root_page_id).scan_keys()) == sorted(oracle)
    validate_btree(pool, tree.root_page_id).raise_if_invalid()
    pager.close()
```


**`test_overflow.py`** — a 10KB payload round-trips; boundary sizes (exactly fits / one byte over);
a chain pointing to itself raises `OverflowCycleError`.


**`test_validate.py`** — deliberately corrupt a valid tree and confirm each invariant fires:
swap two keys on a page (ordering), point two parents at one child (shared page), truncate a child
pointer (unreachable), make one leaf deeper (height).


---


# Weeks 3–8: Module Maps


Deliberately less detailed, for a reason I flagged earlier: signatures written now for week 7 would
be fiction. Your week-3 operator interface will be shaped by what the B+tree cursor actually turns
out to look like, and guessing that in August means rewriting it in October. Ask me for the full spec
at the start of each week — that's ~20 minutes of work each time, and it'll be right.


### Week 3


| File | Key signatures |
|---|---|
| `sql/tokens.py` | `class TokenType(Enum)`, `@dataclass Token(type, value, position)` |
| `sql/tokenizer.py` | `def tokenize(sql: str) -> list[Token]` |
| `sql/ast.py` | `CreateTable`, `Insert`, `Select`, `BinaryOp`, `Column`, `Literal`, `Parameter` |
| `sql/parser.py` | `def parse(sql: str) -> Statement`; `Parser._expression(min_binding_power: int)` |
| `catalog/schema.py` | `@dataclass TableSchema(name, columns, root_page, indexes)` |
| `catalog/catalog.py` | `create_table`, `get_table`, `list_tables` — rows in the page-2 B+tree |
| `sql/binder.py` | `def bind(stmt: Statement, catalog: Catalog) -> BoundStatement` |
| `exec/operators.py` | `class Operator: def next(self) -> tuple[Value, ...] | None` |
| `exec/expressions.py` | `def evaluate(expr: BoundExpr, row: tuple[Value, ...]) -> Value` |
| `api/connection.py` | `connect(path)`, `Connection.execute(sql, params) -> Cursor` |


The operator base class, since everything hangs off it:


```python
class Operator:
    def open(self) -> None: ...
    def next(self) -> tuple[Value, ...] | None:
        """One row, or None when exhausted."""
    def close(self) -> None: ...
    def explain(self, depth: int = 0) -> str: ...
```


### Week 4


`btree/index.py` (`encode_index_key`, `IndexBTree`), `plan/planner.py`
(`choose_access_path(table, predicates) -> AccessPath`), `plan/explain.py`, plus `IndexScan`,
`Delete`, `Update` operators.


The planner's core is the leading-column rule from `guide.md` §4.2.


### Week 5


`txn/journal.py`, `txn/transaction.py`, `txn/recovery.py`. The one signature worth pinning now,
because it's hard to guess and it's the heart of the week:


```python
class FaultyFile:
    """Wraps a file object and fails deterministically, for crash testing."""


    def __init__(self, real_file, fail_at_write: int | None = None,
                 fail_at_sync: int | None = None) -> None: ...


    def write(self, data: bytes) -> int:
        """Raises SimulatedCrash when this is write number `fail_at_write`."""


    def flush_and_sync(self) -> None:
        """Raises SimulatedCrash when this is sync number `fail_at_sync`."""
```


### Week 6


`txn/locks.py`:


```python
class LockMode(Enum):
    SHARED = 1
    EXCLUSIVE = 2




class LockManager:
    def acquire(self, txn_id: int, resource: str, mode: LockMode,
                timeout: float | None = None) -> None:
        """Blocks until granted.


        Raises:
            DeadlockError: this transaction was chosen as the victim.
            LockTimeoutError: timeout expired.
        """


    def release_all(self, txn_id: int) -> None:
        """Called at commit/rollback. Never mid-transaction — that's 2PL."""


    def _detect_deadlock(self, waiting_txn: int) -> int | None:
        """Cycle-check the wait-for graph. Returns the victim's txn id."""
```


### Weeks 7–8


`exec/join.py`, `exec/aggregate.py`, `exec/sort.py`, then docs and benchmarks. Specs when you get
there.


---


## Working Rules


**Order within a session:** read the tests → copy the stub → run `pytest` and watch it fail for the
right reason → implement → green → commit. Never write implementation before you've seen the test
fail; a test that passes against `NotImplementedError` is testing nothing.


**When stuck 45 minutes:** stop and build visibility. `hexdump(page)` with labeled offsets;
`dump_tree()`; a `--verbose` flag that logs every page read. In this project most "I'm stuck" is
"I can't see what's happening."


**Seed your randomness.** `random.Random(42)`, not `random.shuffle`. A failure you can't reproduce
costs an hour.


**`NOTES.md` after every bug.** Symptom → what you assumed → what it was → fix. Four lines. This is
where your interview stories come from.


**Commit message convention** — keeps the graph readable, and it's free:


```
storage: add slotted page parse/serialize
btree: fix separator key off-by-one in leaf split
test: cover overflow chain cycle detection
```




