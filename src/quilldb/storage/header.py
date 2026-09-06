"""The 100-byte SQLite file header, living at the start of page 1.


This is SQLite's exact layout (fileformat2.html §1.3). Note what that means:
page 1 is NOT header-only. It is simultaneously the file header (bytes 0-99)
and the root b-tree page of `sqlite_schema`, whose own page header begins at
byte 100. Page 1 therefore has 100 fewer usable bytes than every other page.


That one fact is the source of `hdrOffset = pgno==1 ? 100 : 0` in btree.c, and
of the format's only irregular invariant: an interior page normally holds >= 2
keys, but page 1 may rarely hold just one. See docs/theory/01-pages-and-the-pager.md
§1.6 for why SQLite accepted that cost and what each field below buys.


All multibyte fields are big-endian.
"""


from dataclasses import dataclass


from quilldb.constants import (
    FILE_FORMAT_LEGACY,
    FILE_HEADER_SIZE,
    LEAF_PAYLOAD_FRACTION,
    MAGIC,
    MAX_EMBEDDED_PAYLOAD_FRACTION,
    MIN_EMBEDDED_PAYLOAD_FRACTION,
    OFF_APPLICATION_ID,
    OFF_CHANGE_COUNTER,
    OFF_DEFAULT_CACHE_SIZE,
    OFF_FREELIST_COUNT,
    OFF_FREELIST_TRUNK,
    OFF_INCREMENTAL_VACUUM,
    OFF_LARGEST_ROOT_PAGE,
    OFF_LEAF_PAYLOAD_FRACTION,
    OFF_MAGIC,
    OFF_MAX_PAYLOAD_FRACTION,
    OFF_MIN_PAYLOAD_FRACTION,
    OFF_PAGE_COUNT,
    OFF_PAGE_SIZE,
    OFF_READ_VERSION,
    OFF_RESERVED_SPACE,
    OFF_SCHEMA_COOKIE,
    OFF_SCHEMA_FORMAT,
    OFF_SQLITE_VERSION,
    OFF_TEXT_ENCODING,
    OFF_USER_VERSION,
    OFF_VERSION_VALID_FOR,
    OFF_WRITE_VERSION,
    PAGE_SIZE,
    RESERVED_SPACE,
    SCHEMA_FORMAT,
    SQLITE_VERSION_NUMBER,
    TEXT_ENCODING_UTF8,
)
from quilldb.errors import InvalidHeaderError




@dataclass
class FileHeader:
    """Defaults describe a freshly created, empty quilldb/SQLite database.


    An empty database is exactly ONE page: page 1, an empty leaf table b-tree
    holding no schema rows, with this header in its first 100 bytes.
    """


    page_size: int = PAGE_SIZE
    write_version: int = FILE_FORMAT_LEGACY   # 1 = rollback journal, 2 = WAL
    read_version: int = FILE_FORMAT_LEGACY
    reserved_space: int = RESERVED_SPACE
    change_counter: int = 0
    page_count: int = 1                       # page 1 alone is a valid empty db
    freelist_trunk: int = 0                   # 0 means empty
    freelist_count: int = 0
    schema_cookie: int = 0                    # bump on every schema change
    schema_format: int = SCHEMA_FORMAT
    default_cache_size: int = 0
    largest_root_page: int = 0                # auto-vacuum only
    text_encoding: int = TEXT_ENCODING_UTF8
    user_version: int = 0
    incremental_vacuum: int = 0
    application_id: int = 0
    version_valid_for: int = 0
    sqlite_version: int = SQLITE_VERSION_NUMBER


    @property
    def usable_size(self) -> int:
        """"U" in the overflow threshold formulas. Never allowed below 480."""
        return self.page_size - self.reserved_space


    def to_bytes(self) -> bytes:
        """Serialize to exactly 100 bytes. Reserved bytes 72..91 stay zero."""
        data = bytearray(FILE_HEADER_SIZE)  # zero-filled; reserved region stays 0
        data[OFF_MAGIC:OFF_MAGIC + len(MAGIC)] = MAGIC


        # The value 1 encodes a 65536-byte page, which cannot fit in u16.
        encoded_page_size = 1 if self.page_size == 65536 else self.page_size
        data[OFF_PAGE_SIZE:OFF_PAGE_SIZE + 2] = encoded_page_size.to_bytes(2, "big")


        data[OFF_WRITE_VERSION] = self.write_version
        data[OFF_READ_VERSION] = self.read_version
        data[OFF_RESERVED_SPACE] = self.reserved_space
        data[OFF_MAX_PAYLOAD_FRACTION] = MAX_EMBEDDED_PAYLOAD_FRACTION
        data[OFF_MIN_PAYLOAD_FRACTION] = MIN_EMBEDDED_PAYLOAD_FRACTION
        data[OFF_LEAF_PAYLOAD_FRACTION] = LEAF_PAYLOAD_FRACTION


        for offset, value in (
            (OFF_CHANGE_COUNTER, self.change_counter),
            (OFF_PAGE_COUNT, self.page_count),
            (OFF_FREELIST_TRUNK, self.freelist_trunk),
            (OFF_FREELIST_COUNT, self.freelist_count),
            (OFF_SCHEMA_COOKIE, self.schema_cookie),
            (OFF_SCHEMA_FORMAT, self.schema_format),
            (OFF_DEFAULT_CACHE_SIZE, self.default_cache_size),
            (OFF_LARGEST_ROOT_PAGE, self.largest_root_page),
            (OFF_TEXT_ENCODING, self.text_encoding),
            (OFF_USER_VERSION, self.user_version),
            (OFF_INCREMENTAL_VACUUM, self.incremental_vacuum),
            (OFF_APPLICATION_ID, self.application_id),
            (OFF_VERSION_VALID_FOR, self.version_valid_for),
            (OFF_SQLITE_VERSION, self.sqlite_version),
        ):
            data[offset:offset + 4] = value.to_bytes(4, "big")


        return bytes(data)


    def check_supported(self) -> None:
        """Refuse files whose header is valid SQLite but outside quilldb's subset.


        Raises:
            InvalidHeaderError: the file is well-formed but unsupported here.
        """
        # TODO(human): decide, field by field, what quilldb refuses vs tolerates.
        #
        # Every field below can legally hold values quilldb does not implement.
        # For each one you must pick REFUSE (raise InvalidHeaderError) or
        # TOLERATE (read the file anyway and ignore the field):
        #
        #   page_size            not 4096 — you hardcoded 4096 everywhere
        #   write_version == 2   the file is in WAL mode; you only do journals
        #   read_version == 2    ditto, but for reading
        #   reserved_space != 0  every page is shorter than you think it is
        #   text_encoding != 1   UTF-16; your codec assumes UTF-8
        #   schema_format != 4   1..3 lack serial types 8 and 9
        #   largest_root_page    non-zero means auto-vacuum, with pointer maps
        #     / incremental_vacuum   interleaved among the pages you'd walk
        #
        # The rule to reason from: TOLERATE is safe only when misreading the
        # field cannot corrupt data or return wrong answers. Silently ignoring
        # reserved_space makes you read bytes that are not payload. Silently
        # ignoring user_version costs nothing.
        if self.page_size != PAGE_SIZE:
            raise InvalidHeaderError(f"PAGE SIZE must be {PAGE_SIZE}")
       
        if self.text_encoding != TEXT_ENCODING_UTF8:
            raise InvalidHeaderError(f"Text encoding should be UTF8 which value is 1")


        if self.read_version == 2 or self.write_version == 2:
            raise InvalidHeaderError("Not support reading combine the WAL mode")
       
        if self.reserved_space != 0:
            raise InvalidHeaderError("Reserved space should be zero")
       
        if self.largest_root_page or self.incremental_vacuum:
            raise InvalidHeaderError("Not supported yet")
       
        if self.schema_format != 4:
            raise InvalidHeaderError("Should be 4, where Sqlite version >= 3.3.0")
         
       
       
    @classmethod
    def from_bytes(cls, data: bytes | memoryview) -> "FileHeader":
        """Parse and validate.


        Raises:
            InvalidHeaderError: buffer too short, bad magic, page size not a
                power of two in 512..65536, page_count == 0, or a payload
                fraction that the spec fixes at a constant is wrong.
        """
        if len(data) < FILE_HEADER_SIZE:
            raise InvalidHeaderError("buffer shorter than the 100-byte header")


        if data[OFF_MAGIC:OFF_MAGIC + len(MAGIC)] != MAGIC:
            raise InvalidHeaderError("not a SQLite database (bad magic)")


        raw_page_size = int.from_bytes(data[OFF_PAGE_SIZE:OFF_PAGE_SIZE + 2], "big")
        page_size = 65536 if raw_page_size == 1 else raw_page_size
        if page_size < 512 or page_size & (page_size - 1):
            raise InvalidHeaderError(f"illegal page size {page_size}")


        page_count = int.from_bytes(data[OFF_PAGE_COUNT:OFF_PAGE_COUNT + 4], "big")
        if page_count == 0:
            raise InvalidHeaderError("in-header database size is zero")


        # The spec says these three "must be" 64, 32, 32. Anything else is a
        # file no version of SQLite ever wrote.
        if (
            data[OFF_MAX_PAYLOAD_FRACTION] != MAX_EMBEDDED_PAYLOAD_FRACTION
            or data[OFF_MIN_PAYLOAD_FRACTION] != MIN_EMBEDDED_PAYLOAD_FRACTION
            or data[OFF_LEAF_PAYLOAD_FRACTION] != LEAF_PAYLOAD_FRACTION
        ):
            raise InvalidHeaderError("payload fractions are not 64/32/32")


        def u32(offset: int) -> int:
            return int.from_bytes(data[offset:offset + 4], "big")


        header = cls(
            page_size=page_size,
            write_version=data[OFF_WRITE_VERSION],
            read_version=data[OFF_READ_VERSION],
            reserved_space=data[OFF_RESERVED_SPACE],
            change_counter=u32(OFF_CHANGE_COUNTER),
            page_count=page_count,
            freelist_trunk=u32(OFF_FREELIST_TRUNK),
            freelist_count=u32(OFF_FREELIST_COUNT),
            schema_cookie=u32(OFF_SCHEMA_COOKIE),
            schema_format=u32(OFF_SCHEMA_FORMAT),
            default_cache_size=u32(OFF_DEFAULT_CACHE_SIZE),
            largest_root_page=u32(OFF_LARGEST_ROOT_PAGE),
            text_encoding=u32(OFF_TEXT_ENCODING),
            user_version=u32(OFF_USER_VERSION),
            incremental_vacuum=u32(OFF_INCREMENTAL_VACUUM),
            application_id=u32(OFF_APPLICATION_ID),
            version_valid_for=u32(OFF_VERSION_VALID_FOR),
            sqlite_version=u32(OFF_SQLITE_VERSION),
        )
        if header.usable_size < 480:
            raise InvalidHeaderError("usable size below the 480-byte minimum")
        header.check_supported()
        return header




