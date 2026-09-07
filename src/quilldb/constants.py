"""Format constants. Change nothing here without updating docs/file-format.md.


quilldb writes the **real SQLite on-disk format**, byte for byte. Every offset,
magic value, and type code below comes from https://www.sqlite.org/fileformat2.html
The acceptance test for this module is not a unit test: it is that `sqlite3` can
open a file quilldb created and pass `PRAGMA integrity_check`.


See docs/theory/01-pages-and-pager.md §1.6 for why each header field exists.
"""


from enum import IntEnum

PAGE_SIZE = 4096                   # SQLite's default since 3.12.0 (2016)
MAGIC = b"SQLite format 3\x00"     # exactly 16 bytes, NUL-terminated


FILE_HEADER_SIZE = 100
SCHEMA_ROOT_PAGE = 1               # page 1 IS the sqlite_schema root b-tree page
SCHEMA_PAGE_HEADER_OFFSET = 100    # ...so its page header starts after the file header


RESERVED_SPACE = 0                 # bytes reserved at the END of every page
USABLE_SIZE = PAGE_SIZE - RESERVED_SPACE   # "U" in the overflow formulas


LEAF_HEADER_SIZE = 8               # leaf pages omit the right-child pointer
INTERIOR_HEADER_SIZE = 12
CELL_POINTER_SIZE = 2
MAX_VARINT_BYTES = 9               # 8 bytes x 7 bits + 1 byte x 8 bits = 64 bits


# The spec fixes these three at exactly these values; they are stored anyway.
MAX_EMBEDDED_PAYLOAD_FRACTION = 64
MIN_EMBEDDED_PAYLOAD_FRACTION = 32
LEAF_PAYLOAD_FRACTION = 32


TEXT_ENCODING_UTF8 = 1
SCHEMA_FORMAT = 4                  # 4 enables serial types 8 and 9 (integers 0 and 1)
FILE_FORMAT_LEGACY = 1             # rollback journal; 2 would mean WAL
SQLITE_VERSION_NUMBER = 3045000    # what we claim last wrote this file


# --- file header field offsets (all big-endian) ---
OFF_MAGIC = 0                      # 16 bytes
OFF_PAGE_SIZE = 16                 # u16; the value 1 means 65536
OFF_WRITE_VERSION = 18             # u8: 1 = legacy journal, 2 = WAL
OFF_READ_VERSION = 19              # u8
OFF_RESERVED_SPACE = 20            # u8
OFF_MAX_PAYLOAD_FRACTION = 21      # u8, must be 64
OFF_MIN_PAYLOAD_FRACTION = 22      # u8, must be 32
OFF_LEAF_PAYLOAD_FRACTION = 23     # u8, must be 32
OFF_CHANGE_COUNTER = 24            # u32
OFF_PAGE_COUNT = 28                # u32  the "in-header database size"
OFF_FREELIST_TRUNK = 32            # u32  first freelist trunk page (0 = empty)
OFF_FREELIST_COUNT = 36            # u32  total pages on the freelist
OFF_SCHEMA_COOKIE = 40             # u32  bumped on every schema change
OFF_SCHEMA_FORMAT = 44             # u32  1..4
OFF_DEFAULT_CACHE_SIZE = 48        # u32
OFF_LARGEST_ROOT_PAGE = 52         # u32  auto-vacuum only, otherwise 0
OFF_TEXT_ENCODING = 56             # u32  1 = UTF-8, 2 = UTF-16le, 3 = UTF-16be
OFF_USER_VERSION = 60              # u32  PRAGMA user_version
OFF_INCREMENTAL_VACUUM = 64        # u32
OFF_APPLICATION_ID = 68            # u32  PRAGMA application_id
OFF_RESERVED = 72                  # 20 bytes, must be zero
RESERVED_LEN = 20
OFF_VERSION_VALID_FOR = 92         # u32  the change counter this version is valid for
OFF_SQLITE_VERSION = 96            # u32  SQLITE_VERSION_NUMBER




class PageType(IntEnum):
    """The b-tree page type byte at offset 0 of a page.


    These are SQLite's values, and `decodeFlags` in btree.c accepts no others.
    Two independent facts (leaf vs interior, table vs index) are packed into one
    enumerated byte rather than separate flag bits.


    Note what is NOT here: overflow and freelist pages carry no type byte at
    all. They are identified purely by how you arrive at them, which is why a
    corrupt pointer into one produces garbage rather than a clean error.
    """


    INTERIOR_INDEX = 2
    INTERIOR_TABLE = 5
    LEAF_INDEX = 10
    LEAF_TABLE = 13


    @property
    def is_leaf(self) -> bool:
        return self in (PageType.LEAF_INDEX, PageType.LEAF_TABLE)


    @property
    def is_table(self) -> bool:
        return self in (PageType.INTERIOR_TABLE, PageType.LEAF_TABLE)


    @property
    def header_size(self) -> int:
        """8 for leaves, 12 for interior pages — the right-child pointer."""
        return LEAF_HEADER_SIZE if self.is_leaf else INTERIOR_HEADER_SIZE



