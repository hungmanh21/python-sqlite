"""Week 1 smoke test: a row through every storage-layer piece and back.


encode a row -> put it on a page -> cache the page, mark it dirty -> flush to
disk -> close -> reopen -> read the same page -> parse it -> decode the row.


If this prints "round-trip through disk: OK", every Week 1 piece (Pager,
BufferPool, PageBody, the record codec) is composing correctly end to end,
not just passing in isolation.
"""


from pathlib import Path

from quilldb.codec.record import decode_record, encode_record
from quilldb.constants import PageType
from quilldb.storage.bufferpool import BufferPool
from quilldb.storage.page import PageBody, parse_page, serialize_page
from quilldb.storage.pager import Pager

path = Path("demo.db")
path.unlink(missing_ok=True)  # rerunning this script shouldn't hit FileExistsError


pager = Pager.create(path)
pool = BufferPool(pager, capacity=16)


# Pager.create() only writes page 1 (header + empty schema root) -- a fresh
# page for our own data has to be allocated explicitly, same as any caller
# outside the storage layer would have to.
page_id = pager.allocate_page()


body = PageBody(PageType.LEAF_TABLE)
body.insert_cell(0, encode_record((1, "ada", 36)))
with pool.pinned(page_id, dirty=True) as page:
    page[:] = serialize_page(body)
pool.flush_all()
pager.close()


pager = Pager.open(path)
reloaded = parse_page(pager.read_page(page_id))
assert decode_record(reloaded.cells[0]) == (1, "ada", 36)
pager.close()
print("round-trip through disk: OK")





