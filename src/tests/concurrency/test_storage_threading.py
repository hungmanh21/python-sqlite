"""Session 4 (week6-concurrency.md SS37): Pager's seek-then-read/write races
and BufferPool's unlatched cache are the "five things week 5 left
single-threaded" that a stress test discovers one run in fifteen, not every
time -- these are written to fail reliably instead.

Pager's pread/pwrite split (SS37.1) and the private _allocate_page/_free_page
(SS37.3) are done; the tests below that exercise them should already pass.
BufferPool's own latch (SS37.6, the TODO(human) in bufferpool.py's __init__)
is not wired in yet -- test_a_pinned_page_is_never_evicted_under_contention
and test_concurrent_allocate_never_returns_the_same_page_twice are expected
to fail (or error) until it is.
"""

import sys
import threading

import pytest

from quilldb.constants import PAGE_SIZE
from quilldb.storage.bufferpool import BufferPool
from quilldb.storage.pager import Pager


@pytest.fixture
def tight_switching():
    """CPython's default GIL switch interval (5ms) is too coarse to reliably
    hit a race window inside a few thousand pure-Python attribute ops with no
    I/O to release the GIL early -- which is exactly BufferPool.allocate_page
    and _evict_one's situation before SS37.6's latch exists. Shortening the
    interval makes an already-real race surface within one test run instead
    of "one run in fifteen" (this file's docstring) -- it does not fabricate
    a race that isn't there.
    """
    original = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        yield
    finally:
        sys.setswitchinterval(original)


def test_pager_allocate_page_is_private() -> None:
    """SS37.3's guard: if Pager grows a public allocate_page again, the pool's
    latch (once it exists) only ever covers ITS copy, and the race comes back
    through the other one.
    """
    assert not hasattr(Pager, "allocate_page")
    assert not hasattr(Pager, "free_page")
    assert hasattr(Pager, "_allocate_page")
    assert hasattr(Pager, "_free_page")


@pytest.mark.parametrize("backend", ["file", "memory"])
def test_concurrent_reads_never_return_the_wrong_page(tmp_path, backend: str) -> None:
    """8 threads each read a DIFFERENT known page in a tight loop; every read
    must return the page it asked for. Fails reliably on seek-then-read,
    passes on pread -- and on :memory:, which takes Pager._io_lock instead.
    """
    pager = Pager.create(tmp_path / "test.db") if backend == "file" else Pager.memory()
    page_ids = [pager._allocate_page() for _ in range(8)]
    for marker, page_id in enumerate(page_ids):
        buf = bytearray(PAGE_SIZE)
        buf[0] = marker
        pager.write_page(page_id, buf)

    errors: list[tuple[int, int, int]] = []

    def worker(page_id: int, marker: int) -> None:
        for _ in range(200):
            data = pager.read_page(page_id)
            if data[0] != marker:
                errors.append((page_id, marker, data[0]))

    threads = [
        threading.Thread(target=worker, args=(page_id, marker))
        for marker, page_id in enumerate(page_ids)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    pager.close()


def test_a_pinned_page_is_never_evicted_under_contention(tmp_path, tight_switching) -> None:
    """A cursor holds a pinned page while 7 threads thrash the pool. The
    pinned page's contents must be unchanged and its frame must be the same
    frame -- not just "a page with the same id", the same object.
    """
    pager = Pager.create(tmp_path / "test.db")
    # capacity=10, not a tighter number: with 1 slot permanently held and 7
    # threads under tight_switching's artificially frequent interleaving,
    # a too-small pool can legitimately have more than a couple of the
    # "other" pages pinned at once -- that's real contention, not a bug,
    # and PoolExhaustedError from it would be a false failure here.
    pool = BufferPool(pager, capacity=10)

    held_id = pager._allocate_page()
    held_page = pool.get_page(held_id)  # pinned for the whole test, never unpinned below
    held_page[0] = 0xAB

    other_ids = [pager._allocate_page() for _ in range(20)]  # >> capacity, forces eviction
    errors: list[BaseException] = []

    def thrash() -> None:
        try:
            for _ in range(200):
                for page_id in other_ids:
                    pool.get_page(page_id)
                    pool.unpin(page_id)
        except BaseException as exc:  # noqa: BLE001 -- a race can throw almost
            # anything (ValueError, RuntimeError from OrderedDict mutation,
            # KeyError); a thread's own exception never reaches .join(), so
            # this is what makes it visible instead of a silently-swallowed
            # PytestUnhandledThreadExceptionWarning.
            errors.append(exc)

    threads = [threading.Thread(target=thrash) for _ in range(7)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert held_id in pool._cache
    assert pool._cache[held_id].data is held_page
    assert held_page[0] == 0xAB
    pool.unpin(held_id)
    pager.close()


def test_concurrent_allocate_never_returns_the_same_page_twice(tmp_path, tight_switching) -> None:
    """The freelist race (SS37.3). 8 threads x 200 allocations; assert 1600
    DISTINCT page numbers. This is the test that catches the bug that would
    otherwise show up as mysterious tree corruption a week later.
    """
    pager = Pager.create(tmp_path / "test.db")
    pool = BufferPool(pager, capacity=64)

    got: list[int] = []
    got_lock = threading.Lock()  # protects the TEST's own list, not the pool

    def worker() -> None:
        local = [pool.allocate_page() for _ in range(200)]
        with got_lock:
            got.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(set(got)) == len(got) == 1600
    pager.close()
