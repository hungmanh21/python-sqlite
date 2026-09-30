"""B+tree height vs row count: measured up to 100k rows, computed beyond.

A table B+tree's height is what a point lookup pays in page reads, so how
slowly it grows is the whole reason the structure exists. Height is measured
by walking the leftmost path of a tree built with `BTree.insert`. Every row
count above the largest one built is COMPUTED from the fanout measured on
that largest tree -- the leaf and interior page counts are counted, so
`fanout = interior children per page` and `rows per leaf` are observed, not
assumed. The computed rows are labelled; the arithmetic is one line, so
check it: height = 1 + ceil(log_fanout(ceil(rows / rows_per_leaf))).

Building 10M rows takes tens of minutes at this engine's ~0.15 ms an insert,
which is why it is computed rather than built.

    python -m quilldb.bench.btree_height
"""

from __future__ import annotations

import math

from quilldb.btree.btree import BTree
from quilldb.codec.record import encode_record
from quilldb.constants import PageType
from quilldb.storage.bufferpool import BufferPool
from quilldb.storage.page import PageBody, parse_page, serialize_page
from quilldb.storage.pager import Pager

BUILT = (1_000, 10_000, 100_000)
COMPUTED = (1_000_000, 10_000_000, 100_000_000)


def _payload(key: int) -> bytes:
    return encode_record((key, f"user{key}@example.com", "padding" * 6))


def build(rows: int) -> tuple[Pager, BufferPool, int]:
    # Capacity is generous: this measures shape, not caching.
    pager = Pager.memory()
    pool = BufferPool(pager, 4096)
    root = pager._allocate_page()
    with pool.pinned(root, dirty=True) as raw:
        raw[:] = serialize_page(PageBody(PageType.LEAF_TABLE, cells=[]))
    tree = BTree(pager, pool, root)
    for key in range(1, rows + 1):
        tree.insert(key, _payload(key))
    return pager, pool, root


def shape(pool: BufferPool, root: int) -> tuple[int, int, int, int]:
    """(height, leaf pages, interior pages, interior children) by walking every page."""
    leaves = interiors = children = 0
    height = 0
    stack = [(root, 1)]
    while stack:
        page_id, depth = stack.pop()
        raw = pool.get_page(page_id)
        try:
            body = parse_page(raw)
        finally:
            pool.unpin(page_id)
        if body.page_type is PageType.LEAF_TABLE:
            leaves += 1
            height = max(height, depth)
            continue
        interiors += 1
        kids = [int.from_bytes(cell[:4], "big") for cell in body.cells]
        if body.right_child:
            kids.append(body.right_child)
        children += len(kids)
        stack.extend((kid, depth + 1) for kid in kids)
    return height, leaves, interiors, children


def computed_height(rows: int, rows_per_leaf: float, fanout: float) -> int:
    leaves = math.ceil(rows / rows_per_leaf)
    return 1 + max(1, math.ceil(math.log(leaves, fanout))) if leaves > 1 else 1


def main() -> None:
    print("Table B+tree height vs rows")
    print(f"{'rows':>13}{'height':>8}{'leaf pages':>12}{'how':>12}")
    fanout = rows_per_leaf = 0.0
    for rows in BUILT:
        pager, pool, root = build(rows)
        height, leaves, interiors, children = shape(pool, root)
        rows_per_leaf = rows / leaves
        fanout = children / interiors if interiors else 0.0
        print(f"{rows:>13,}{height:>8}{leaves:>12,}{'measured':>12}")
        pager.close()
    print(f"\nmeasured on the {BUILT[-1]:,}-row tree: {rows_per_leaf:.1f} rows per leaf, "
          f"{fanout:.0f} children per interior page")
    for rows in COMPUTED:
        leaves = math.ceil(rows / rows_per_leaf)
        print(f"{rows:>13,}{computed_height(rows, rows_per_leaf, fanout):>8}{leaves:>12,}{'computed':>12}")


if __name__ == "__main__":
    main()
