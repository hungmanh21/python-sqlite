"""Page-level file access.


Page N occupies bytes [(N-1) * PAGE_SIZE, N * PAGE_SIZE). Page numbering starts
at 1 so that 0 can mean "no page" in child and freelist pointers.


Page 1 is special in exactly one way: the 100-byte file header sits in front of
its b-tree page header, so page 1's page header begins at byte 100 and page 1 has
100 fewer usable bytes than every other page. Use page_header_offset() and never
inline that comparison.


Freed pages form a TWO-level structure: header.freelist_trunk points at a trunk
page holding an array of free leaf page numbers, and the leaf pages themselves are
never read or written. See allocate_page/free_page and chapter 01 §1.8.
"""


import os
from pathlib import Path

from quilldb.constants import FILE_HEADER_SIZE, PAGE_SIZE, SCHEMA_ROOT_PAGE, PageType
from quilldb.errors import CorruptDatabaseError, PageOutOfRangeError
from quilldb.storage.header import FileHeader


def page_header_offset(page_id: int) -> int:
    """Where page `page_id`'s b-tree page header starts within its own page.


    100 for page 1, 0 for everything else. SQLite's `hdrOffset = pgno==1 ? 100 : 0`
    from btree.c, in one place so no call site can forget it.
    """
    return FILE_HEADER_SIZE if page_id == SCHEMA_ROOT_PAGE else 0




class Pager:
    def __init__(self, path: Path) -> None:
        """Prefer Pager.create() or Pager.open()."""
        Pager.create(path = path)


    @classmethod
    def create(cls, path: Path) -> "Pager":
        """Create a new database file: one page, holding the file header and an
        empty `sqlite_schema` leaf b-tree.


        Page 1 does double duty (chapter 01 §1.6): bytes 0..99 are the file
        header, and byte 100 begins page 1's own b-tree page header. A one-page
        file with an empty leaf table b-tree there is a valid, complete SQLite
        database — `sqlite3` will open it and `PRAGMA integrity_check` says ok.


        Raises:
            FileExistsError: path already exists.
        """
        file = path.open("x+b")  # "x" is exclusive create: atomic vs. a check-then-open race


        header = FileHeader()  # defaults: page_count=1, schema root is page 1 by definition


        page1 = bytearray(PAGE_SIZE)
        page1[:FILE_HEADER_SIZE] = header.to_bytes()


        # The sqlite_schema root: an empty leaf table b-tree, header at byte 100.
        hdr = FILE_HEADER_SIZE
        page1[hdr] = PageType.LEAF_TABLE          # offset 0: type
        # offset 1 first freeblock = 0, offset 3 cell count = 0 — already zeroed
        page1[hdr + 5:hdr + 7] = PAGE_SIZE.to_bytes(2, "big")   # content start: end of page
        # offset 7 fragment count = 0 — already zeroed


        file.write(page1)


        self = object.__new__(cls)
        self._path = path
        self._file = file
        self._header = header
        return self


    @classmethod
    def open(cls, path: Path) -> "Pager":
        """Open an existing file and validate its header.


        Raises:
            FileNotFoundError, InvalidHeaderError.
            CorruptDatabaseError: file length is not a whole number of pages,
                or is shorter than the header's page_count claims.
        """
        file = path.open("r+b")  # raises FileNotFoundError if missing


        header = FileHeader.from_bytes(file.read(FILE_HEADER_SIZE))  # raises InvalidHeaderError


        file_size = path.stat().st_size
        if file_size % PAGE_SIZE != 0:
            raise CorruptDatabaseError(f"{path}: length {file_size} is not a whole number of pages")
        if file_size < header.page_count * PAGE_SIZE:
            raise CorruptDatabaseError(
                f"{path}: header claims {header.page_count} pages, file only holds {file_size // PAGE_SIZE}"
            )


        self = object.__new__(cls)
        self._path = path
        self._file = file
        self._header = header
        return self


    @property
    def page_count(self) -> int:
        return self._header.page_count


    def read_page(self, page_id: int) -> bytearray:
        """Read one page.


        Returns:
            A fresh mutable 4096-byte bytearray. Mutating it does NOT write to
            disk — call write_page.
        Raises:
            PageOutOfRangeError: page_id < 1 or > page_count.
        """
        # TODO(human): validate page_id, then seek to its byte offset and read
        # exactly PAGE_SIZE bytes.
        #
        # - page_id must satisfy 1 <= page_id <= self.page_count, else raise
        #   PageOutOfRangeError (see quilldb.errors).
        # - Page N starts at byte (N - 1) * PAGE_SIZE — this is the off-by-one
        #   the module docstring warns about; get it into one expression you
        #   trust rather than re-deriving it elsewhere.
        # - self._file.seek(offset) then self._file.read(PAGE_SIZE); wrap the
        #   result in bytearray(...) since read() returns immutable bytes.
        if page_id < 1 or page_id > self.page_count:
            raise PageOutOfRangeError
        offset = (page_id - 1) * PAGE_SIZE
        self._file.seek(offset)
        data = self._file.read(PAGE_SIZE)
       
        return bytearray(data)


    def write_page(self, page_id: int, data: bytes | bytearray) -> None:
        """Write one page.


        Raises:
            PageOutOfRangeError, ValueError: len(data) != PAGE_SIZE.
        """
        # TODO(human): validate page_id and len(data), then seek and write.
        #
        # - Same page_id bounds check as read_page.
        # - len(data) must be exactly PAGE_SIZE, else raise ValueError.
        # - self._file.seek(offset) then self._file.write(data).
        if page_id < 1 or page_id > self.page_count:
            raise  PageOutOfRangeError(f"page {page_id} out of range (1..{self.page_count})")


        if len(data) != PAGE_SIZE:
            raise ValueError


        offset = (page_id - 1) * PAGE_SIZE
        self._file.seek(offset)
       
        self._file.write(data)


    def allocate_page(self) -> int:
        """Get a page for new data — reusing a freed one if any.


        Returns:
            The page number. Contents are undefined; the caller must initialize.
        """
        # ⚠️ PERFORMANCE, NOT VALIDITY — the code below implements a ONE-LEVEL
        # freelist. That is legal: sqlite3 reads it as a chain of trunk pages
        # each holding zero leaves, and integrity_check says ok (verified). But
        # it touches every page on free and on allocate, which is exactly the
        # cost the two-level design exists to avoid. See chapter 01 §1.8.
        #
        # SQLite's freelist is two levels: header.freelist_trunk points at a
        # TRUNK page, which is an array of 4-byte big-endian integers:
        #
        #     offset 0:  next trunk page number (0 = last trunk)
        #     offset 4:  L = how many leaf page numbers follow
        #     offset 8:  L x 4-byte page numbers of free LEAF pages
        #
        # Leaf pages are never read or written — that is the whole point, it turns
        # N page touches into N/120.
        #
        # Allocation order, matching the spec:
        #   1. If the first trunk has L > 0, take its LAST leaf entry, decrement
        #      L in place, write the trunk back. One page write, no read of the
        #      page you're returning.
        #   2. If L == 0, the trunk page itself becomes the allocation, and
        #      header.freelist_trunk moves to its `next`.
        #   3. If freelist_trunk == 0, grow the file: page_count += 1 (bump it
        #      BEFORE write_page, which bounds-checks against it).
        #
        # Keep `header.freelist_count` as the total of trunks + leaves, and leave
        # the last SIX entries of a trunk array unused — modern SQLite still does,
        # so files stay readable by pre-3.6.0 libraries.
        if self._header.freelist_trunk != 0:
            page_id = self._header.freelist_trunk
            data = self.read_page(page_id)
            nxt_freehead = int.from_bytes(data[:4], "big")
            self._header.freelist_trunk = nxt_freehead
            self._header.freelist_count -= 1


            return page_id


        else:
            self._header.page_count += 1
            # extend on disk
            page_id = self._header.page_count
            buffer = bytearray(PAGE_SIZE)
            self.write_page(page_id, buffer)
            return self._header.page_count




    def free_page(self, page_id: int) -> None:
        """Return a page to the freelist.


        Raises:
            PageOutOfRangeError.
            ValueError: page_id is 1 (the header page).
        """
        # ⚠️ See allocate_page above. Freeing should append to a trunk page's leaf
        # array rather than overwriting this page with a `next` pointer.
        #
        # AND THE ONE HARD REQUIREMENT: zero the WHOLE page. Bytes 4..7 are L, the
        # leaf count. Leave stale row data there and sqlite3 reports "freelist leaf
        # count too big" — the bytes you don't write are load-bearing.
        #
        #   1. Reject page_id == 1 (the schema root can never be freed) and
        #      bounds-check like read_page/write_page.
        #   2. If the first trunk has room (L < capacity, where capacity is
        #      (usable_size // 4) - 2 - 6 to leave the last six slots alone),
        #      append page_id to its leaf array, bump L, write the trunk.
        #      NOTE: the freed page itself is never touched. That is the win.
        #   3. Otherwise page_id BECOMES a new trunk: write it with
        #      next = old freelist_trunk, L = 0, and the rest zeroed, then
        #      freelist_trunk = page_id.
        #   4. freelist_count += 1 either way.
        if page_id == 1:
            raise ValueError("Cannot delete the page 1")
        else:
            if page_id < 1 or page_id > self.page_count:
                raise  PageOutOfRangeError(f"page {page_id} out of range (1..{self.page_count})")


        buffer = bytearray(PAGE_SIZE)
        buffer[:4] = self._header.freelist_trunk.to_bytes(4, "big") # point the current page to the head of free pointer


        self.write_page(page_id, buffer)
        self._header.freelist_trunk = page_id
        self._header.freelist_count += 1


    def sync(self) -> None:
        """os.fsync. Becomes load-bearing in Week 5."""
        self._file.flush()
        os.fsync(self._file.fileno())


    def close(self) -> None:
        """Flush the header and close the file."""
        self._file.seek(0)
        self._file.write(self._header.to_bytes())
        self.sync()
        self._file.close()



