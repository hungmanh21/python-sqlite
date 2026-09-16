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

from quilldb.constants import PAGE_SIZE, PageType
from quilldb.errors import InvalidPageTypeError, MalformedCellError, PageFullError


@dataclass
class PageBody:
    """A page as Python objects. `cells` is in key order."""

    page_type: PageType
    cells: list[bytes] = field(default_factory=list)
    right_child: int = 0             # INTERIOR only
    header_offset: int = 0
    """Where this page's b-tree header starts within its own page: 100 for
    page 1 (whose first 100 bytes are the file header), 0 for every other
    page -- SQLite's `hdrOffset = pgno==1 ? 100 : 0`. Use
    pager.page_header_offset() to compute it rather than inlining the
    comparison.

    Note what this does NOT shift: cell content offsets stay absolute,
    measured from byte 0 of the page, exactly as SQLite writes them. Only
    the header and the pointer array that follows it move.
    """

    @property
    def cell_count(self) -> int:
        return len(self.cells)

    def used_bytes(self) -> int:
        """Bytes needed to serialize: reserved prefix + header + pointers + cell data."""
        header_bytes = self.page_type.header_size
        pointer_bytes = 2 * self.cell_count

        cell_bytes = sum(len(cell) for cell in self.cells)

        return self.header_offset + header_bytes + pointer_bytes + cell_bytes

    def free_bytes(self) -> int:
        """PAGE_SIZE - used_bytes()."""
        return PAGE_SIZE - self.used_bytes()

    def fits(self, payload_len: int) -> bool:
        """Would one more cell of `payload_len` bytes fit?

        Remember the 2-byte pointer, not just the payload.
        """
        return self.free_bytes() >= payload_len + 2

    def insert_cell(self, index: int, payload: bytes) -> None:
        """Insert at `index`, shifting later cells right.

        Raises:
            PageFullError: does not fit. Caller splits.
            IndexError: index out of range.
        """
        payload_len = len(payload)
        if not self.fits(payload_len):
            raise PageFullError("Cannot insert anymore cells")
        self.cells.insert(index, payload)

    def delete_cell(self, index: int) -> None:
        del self.cells[index]


def parse_page(data: bytes | memoryview, header_offset: int = 0) -> PageBody:
    """Decode a raw page.

    Args:
        data: the full PAGE_SIZE bytes of one page.
        header_offset: where this page's b-tree header starts -- 100 for
            page 1, 0 otherwise (see PageBody.header_offset). Cell content
            offsets inside `data` remain absolute either way.
    Raises:
        InvalidPageTypeError: unknown type byte.
        MalformedCellError: a cell pointer or length falls outside the page,
            or cells overlap.
    """

    page_type = data[header_offset]
    if page_type not in (PageType.LEAF_INDEX, PageType.LEAF_TABLE, PageType.INTERIOR_INDEX, PageType.INTERIOR_TABLE):
        raise InvalidPageTypeError("Page type must be ...")

    page_type = PageType(data[header_offset])

    ptr_array_start = header_offset + page_type.header_size
    cell_count = int.from_bytes(data[header_offset + 3:header_offset + 5], "big")

    if ptr_array_start + 2 * cell_count > PAGE_SIZE:
        raise MalformedCellError("Corrupted")

    cells = []
    offset = ptr_array_start

    offsets = []
    for _ in range(cell_count):
        decoded_offset = int.from_bytes(data[offset: offset + 2], "big")
        if not (ptr_array_start + 2 * cell_count <= decoded_offset <= PAGE_SIZE):
            raise MalformedCellError("Corrupted")
        offsets.append(decoded_offset)
        offset += 2

    for i in range(len(offsets)):
        if i == 0:
            cells.append(data[offsets[i]: PAGE_SIZE])
        else:
            cells.append(data[offsets[i]: offsets[i-1]])


    right_child = 0
    if not page_type.is_leaf:
        right_child = int.from_bytes(data[header_offset + 8:header_offset + 12], "big")

    return PageBody(
        page_type = page_type,
        cells = cells,
        right_child = right_child,
        header_offset = header_offset,
    )




def serialize_page(body: PageBody) -> bytearray:
    """Encode to exactly PAGE_SIZE bytes.

    Writes cell data packed tight against the end of the page, so a
    freshly-serialized page has zero fragmentation.

    Bytes before body.header_offset are left ZERO here, not preserved --
    on page 1 those are the file header, so use write_page_body() to splice
    into a live buffer rather than assigning `raw[:] = serialize_page(body)`.

    Raises:
        PageFullError: body.used_bytes() > PAGE_SIZE.
    """
    if body.used_bytes() > PAGE_SIZE:
        raise PageFullError("The current page body exceeded the limit")

    parsed_page = bytearray(PAGE_SIZE)
    hdr = body.header_offset

    parsed_page[hdr] = body.page_type
    # bytes 1-2 (first freeblock) and byte 7 (fragment count) stay 0 —
    # we never create freeblocks, so every page we serialize is defragmented.
    parsed_page[hdr + 3:hdr + 5] = body.cell_count.to_bytes(2, "big")

    if not body.page_type.is_leaf:
        parsed_page[hdr + 8:hdr + 12] = body.right_child.to_bytes(4, "big")

    cell_ptr_start = hdr + body.page_type.header_size

    # TODO(human): place each cell's bytes and write the pointer array.
    #
    # - Walk body.cells in order (index 0 first — key order, matching the
    #   order the pointer array must come out in). Keep a cursor starting
    #   at PAGE_SIZE.
    # - For each cell: move the cursor back by len(cell), write the cell's
    #   own bytes into `parsed_page` at the new cursor position, and
    #   remember that cursor value as this cell's offset.
    # - Once every cell is placed, write each remembered offset into the
    #   pointer array starting at cell_ptr_start, 2 bytes each (big-endian),
    #   in the same order as body.cells.
    # - Write the final cursor value into parsed_page[5:7] (content start)
    #   — this is PAGE_SIZE if body.cells is empty.
    cur_offset = PAGE_SIZE
    
    ptrs = []
    
    for i, cell in enumerate(body.cells):
        # write to ptrs list
        cur_offset -= len(cell)
        ptrs.append(cur_offset)
        
        # now write the end
        parsed_page[cur_offset: cur_offset + len(cell)] = cell
    
    for ptr in ptrs:
        parsed_page[cell_ptr_start: cell_ptr_start + 2] = ptr.to_bytes(2, "big")
        cell_ptr_start += 2
        
    parsed_page[hdr + 5:hdr + 7] = cur_offset.to_bytes(2, "big")

    return parsed_page


def write_page_body(raw: bytearray, body: PageBody) -> None:
    """Serialize `body` into `raw` in place, preserving whatever sits before
    body.header_offset.

    That prefix is page 1's 100-byte file header. `raw[:] =
    serialize_page(body)` would zero it -- taking the magic string, page
    count, and freelist pointers with it -- so every write to a page that
    might be page 1 goes through here instead.

    Raises:
        PageFullError: propagated from serialize_page.
        ValueError: len(raw) != PAGE_SIZE.
    """
    if len(raw) != PAGE_SIZE:
        raise ValueError(f"raw buffer is {len(raw)} bytes, expected {PAGE_SIZE}")
    serialized = serialize_page(body)
    raw[body.header_offset:] = serialized[body.header_offset:]
