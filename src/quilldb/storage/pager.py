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

import io
import os
from pathlib import Path
from typing import BinaryIO

from quilldb.constants import FILE_HEADER_SIZE, PAGE_SIZE, SCHEMA_ROOT_PAGE, PageType
from quilldb.errors import CorruptDatabaseError, PageOutOfRangeError
from quilldb.storage.header import FileHeader


def page_header_offset(page_id: int) -> int:
    """Where page `page_id`'s b-tree page header starts within its own page.

    100 for page 1, 0 for everything else. SQLite's `hdrOffset = pgno==1 ? 100 : 0`
    from btree.c, in one place so no call site can forget it.
    """
    return FILE_HEADER_SIZE if page_id == SCHEMA_ROOT_PAGE else 0


def _bootstrap_page1() -> tuple[bytearray, FileHeader]:
    """The one-page bootstrap shared by create() and memory(): a file header
    plus an empty sqlite_schema leaf, its own page header at byte 100.
    """
    header = FileHeader()  # defaults: page_count=1, schema root is page 1 by definition

    page1 = bytearray(PAGE_SIZE)
    page1[:FILE_HEADER_SIZE] = header.to_bytes()

    # The sqlite_schema root: an empty leaf table b-tree, header at byte 100.
    hdr = FILE_HEADER_SIZE
    page1[hdr] = PageType.LEAF_TABLE          # offset 0: type
    # offset 1 first freeblock = 0, offset 3 cell count = 0 — already zeroed
    page1[hdr + 5:hdr + 7] = PAGE_SIZE.to_bytes(2, "big")   # content start: end of page
    # offset 7 fragment count = 0 — already zeroed

    return page1, header


class Pager:
    _path: Path | None
    _file: BinaryIO
    _header: FileHeader

    def __init__(self, path: Path) -> None:
        """Prefer Pager.create() or Pager.open()."""
        raise NotImplementedError("Use Pager.create() or Pager.open().")

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
        page1, header = _bootstrap_page1()
        file.write(page1)

        self = object.__new__(cls)
        self._path = path
        self._file = file
        self._header = header
        return self

    @classmethod
    def memory(cls) -> "Pager":
        """Create a valid one-page database backed by io.BytesIO, for `:memory:`.

        Same bootstrap as create(), but never touches disk: close() drops the
        buffer instead of flushing it to a path, and sync() is a no-op since
        there is no file descriptor to fsync.
        """
        page1, header = _bootstrap_page1()
        file = io.BytesIO()
        file.write(page1)

        self = object.__new__(cls)
        self._path = None
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

    def bump_schema_cookie(self) -> int:
        """Increment the header schema cookie modulo 2**32 and return it.

        Every schema change (CREATE TABLE, and later DROP/ALTER) bumps this
        so a second connection with this file already open notices its
        cached schema is stale -- the same signal SQLite itself uses.
        """
        self._header.schema_cookie = (self._header.schema_cookie + 1) % 2**32
        return self._header.schema_cookie

    def header_bytes(self) -> bytes:
        """The live in-memory header, serialized."""
        return self._header.to_bytes()

    def reload_header(self) -> None:
        """Re-read bytes 0..99 from the file, replacing the in-memory header
        wholesale. Chapter 14 §14.3 step 10: after journal replay restores
        page 1's bytes to their pre-transaction state, the in-memory header
        (freelist_trunk, freelist_count, change_counter, schema_cookie, ...)
        is still whatever this transaction last set it to -- this is what
        rollback calls to resync memory from the now-restored file.
        """
        self._file.seek(0)
        self._header = FileHeader.from_bytes(self._file.read(FILE_HEADER_SIZE))

    def read_page(self, page_id: int) -> bytearray:
        """Read one page.

        A page within page_count but past the physical end of the file reads
        as all zeros, rather than a short bytearray -- week 5, session 0
        needs this: BufferPool.allocate_page()'s growth path bumps
        page_count without writing anything (the whole point is that a page
        that only ever grew the file never touches disk until commit, so a
        rollback's truncate erases it for free). Before that page's first
        real write, it must still read back as a valid, full-size page --
        logically zero, exactly like a page that WAS written as zeros would.

        Returns:
            A fresh mutable 4096-byte bytearray. Mutating it does NOT write to
            disk — call write_page.
        Raises:
            PageOutOfRangeError: page_id < 1 or > page_count.
        """
        if page_id < 1 or page_id > self.page_count:
            raise PageOutOfRangeError
        offset = (page_id - 1) * PAGE_SIZE
        self._file.seek(offset)
        data = self._file.read(PAGE_SIZE)
        if len(data) < PAGE_SIZE:
            data = data.ljust(PAGE_SIZE, b"\x00")

        return bytearray(data)

    def write_page(self, page_id: int, data: bytes | bytearray) -> None:
        """Write one page.

        Raises:
            PageOutOfRangeError, ValueError: len(data) != PAGE_SIZE.
        """
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

        ⚠️ WRITES THROUGH THE POOL'S BACK: this writes the freelist trunk
        straight to the file, so a caller running above a BufferPool must be
        sure `page_id` has no dirty cached entry. If it does, the pool's later
        flush overwrites the trunk with the page's old contents, and its type
        byte is then read as the high byte of a next-trunk pointer -- sqlite3
        reports "freelist leaf count too big" and an absurd page number rather
        than anything that points at this function. BufferPool has no discard
        API yet; until it does, free only pages the pool has never cached (a
        page straight from allocate_page(), for instance, which this module
        also writes directly).

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
        """os.fsync. Becomes load-bearing in Week 5. A no-op for a memory()
        pager -- io.BytesIO has no file descriptor to fsync.
        """
        self._file.flush()
        try:
            fileno = self._file.fileno()
        except (OSError, io.UnsupportedOperation):
            return
        os.fsync(fileno)

    def close(self) -> None:
        """Flush the header and close the file."""
        self._file.seek(0)
        self._file.write(self._header.to_bytes())
        self.sync()
        self._file.close()
