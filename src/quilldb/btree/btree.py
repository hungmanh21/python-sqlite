"""BTree: the root-to-leaf descent, on top of search.py's per-page decisions.


search.py answers "where in THIS page" -- pure, no I/O. This module answers "which page is
the root, and which pages do I visit on the way down", which is the part that actually touches
the Pager and BufferPool. See docs/theory/06-b-tree-mechanics.md §6.1's `search()` sketch; this
is that sketch made to match this codebase's actual page-parsing and pinning API.


Only `search()` exists so far -- no insert, no cursor. Week 2 builds those on top of this same
descent shape (a cursor keeps the path stack this function throws away after each hop).
"""


from quilldb.btree.search import binary_search_leaf, child_for_key
from quilldb.constants import PageType
from quilldb.storage.bufferpool import BufferPool
from quilldb.storage.page import parse_page
from quilldb.storage.pager import Pager




class BTree:
    """A table b-tree rooted at a fixed page. Read-only for now."""


    def __init__(self, pager: Pager, pool: BufferPool, root: int) -> None:
        self.pager = pager
        self.pool = pool
        self.root = root


    def search(self, key: int) -> tuple[int, int] | None:
        """Find `key`'s cell, descending from the root.


        Args:
            key: the rowid to find.
        Returns:
            (page_id, slot) of the matching cell on a LEAF_TABLE page, where
            slot is the index binary_search_leaf found it at -- or None if no
            leaf in the tree contains `key`.
        Raises:
            InvalidPageTypeError, MalformedCellError: propagated unchanged
                from parse_page/decode_* if a page is corrupt.
        """
        page_id = self.root


        while True:
            raw = self.pool.get_page(page_id)
            body = parse_page(raw)
            self.pool.unpin(page_id)


            if body.page_type == PageType.LEAF_TABLE:
                slot = binary_search_leaf(body, key)
                return (page_id, slot) if slot is not None else None


            page_id = child_for_key(body, key)
       



