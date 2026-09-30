"""B+tree insert cost: sequential keys vs random keys. Report PAGE READS.


This measures `BTree.insert`, NOT the SQL `INSERT` statement. The parser has
no `INTEGER PRIMARY KEY`, so SQL can't choose a row's rowid -- the engine
always hands out max+1, which makes every SQL insert "sequential" and the
random case unreachable. Going straight to the tree is the only honest way
to compare the two, so the table is labelled "B+tree insert" and nothing
here should be quoted as the cost of an INSERT.


Why the two orders differ (chapter 19 SS19.2, chapter 6): sequential keys
keep landing on the right-most leaf, which stays hot in the pool. Random
keys land anywhere, so once the tree outgrows the pool most inserts have to
read their leaf back in first. The pool is a fixed, small size on purpose --
that is what makes the difference visible.


    python -m quilldb.bench.inserts
"""

from __future__ import annotations

import random
import time

from quilldb.btree.btree import BTree
from quilldb.codec.record import encode_record
from quilldb.constants import PageType
from quilldb.storage.bufferpool import BufferPool
from quilldb.storage.page import PageBody, serialize_page
from quilldb.storage.pager import Pager

ROWS = 20_000
POOL_CAPACITY = 64
SEED = 1


def make_keys(n: int, order: str) -> list[int]:
    """The rowids to insert, in insertion order: 1..n, sequential or shuffled."""
    keys = list(range(1, n + 1))
    if order == "sequential":
        return keys
    # Seeded, full shuffle of 1..n: every run inserts the same dense keys in the
    # same scattered order, so the reads are reproducible and comparable.
    random.Random(SEED).shuffle(keys)
    return keys


def _payload(key: int) -> bytes:
    # ~60 bytes/row, so a 4 KiB leaf holds ~60 of them and 20k rows is a few
    # hundred leaves -- several times the 64-page pool.
    return encode_record((key, f"user{key}@example.com", "padding" * 6))


def measure(order: str) -> tuple[int, int, int, float]:
    """Build a tree by inserting in `order`; return (misses, hits, pages, seconds)."""
    pager = Pager.memory()
    pool = BufferPool(pager, POOL_CAPACITY)
    root = pager._allocate_page()
    with pool.pinned(root, dirty=True) as raw:
        raw[:] = serialize_page(PageBody(PageType.LEAF_TABLE, cells=[]))
    tree = BTree(pager, pool, root)

    keys = make_keys(ROWS, order)
    started = time.perf_counter()
    for key in keys:
        tree.insert(key, _payload(key))
    elapsed = time.perf_counter() - started

    result = (pool.misses, pool.hits, pager.page_count, elapsed)
    pager.close()
    return result


def measure_lookups(order: str) -> tuple[int, int]:
    """Build once, flush, reopen cold, then search every key in `order`.

    Returns (misses, pages). Unlike the insert numbers this reads a tree
    that already exists through a brand-new pool, so it is a pure read-path
    measurement: every page's first touch is a miss whatever the pool does
    on the write path.
    """
    pager = Pager.memory()
    build_pool = BufferPool(pager, POOL_CAPACITY)
    root = pager._allocate_page()
    with build_pool.pinned(root, dirty=True) as raw:
        raw[:] = serialize_page(PageBody(PageType.LEAF_TABLE, cells=[]))
    build = BTree(pager, build_pool, root)
    for key in range(1, ROWS + 1):
        build.insert(key, _payload(key))
    build_pool.flush_all()

    pool = BufferPool(pager, POOL_CAPACITY)
    tree = BTree(pager, pool, root)
    for key in make_keys(ROWS, order):
        assert tree.search(key) is not None
    result = (pool.misses, pager.page_count)
    pager.close()
    return result


def main() -> None:
    print(f"B+tree insert, {ROWS:,} rows, pool of {POOL_CAPACITY} pages")
    print(f"{'order':<12}{'reads':>10}{'reads/insert':>14}{'pages':>8}{'seconds':>10}")
    for order in ("sequential", "random"):
        misses, _hits, pages, seconds = measure(order)
        print(f"{order:<12}{misses:>10,}{misses / ROWS:>14.2f}{pages:>8,}{seconds:>10.2f}")

    print(f"\nB+tree search of every key, cold pool of {POOL_CAPACITY} pages")
    print(f"{'order':<12}{'reads':>10}{'reads/search':>14}")
    for order in ("sequential", "random"):
        misses, _pages = measure_lookups(order)
        print(f"{order:<12}{misses:>10,}{misses / ROWS:>14.2f}")


if __name__ == "__main__":
    main()
