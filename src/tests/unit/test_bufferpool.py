import pytest

from quilldb.constants import PAGE_SIZE
from quilldb.errors import PageOutOfRangeError, PoolExhaustedError
from quilldb.storage.bufferpool import BufferPool
from quilldb.storage.pager import Pager


@pytest.fixture
def pager(tmp_path):
    p = Pager.create(tmp_path / "test.db")
    yield p
    p.close()




# =====================================================================
# Basic hit/miss and identity
# =====================================================================


def test_get_page_returns_page_sized_bytearray(pager) -> None:
    pool = BufferPool(pager, capacity=4)
    page_id = pager.allocate_page()


    page = pool.get_page(page_id)


    assert isinstance(page, bytearray)
    assert len(page) == PAGE_SIZE
    pool.unpin(page_id)




def test_hit_returns_identical_object(pager) -> None:
    pool = BufferPool(pager, capacity=4)
    page_id = pager.allocate_page()


    first = pool.get_page(page_id)
    second = pool.get_page(page_id)


    assert first is second
    pool.unpin(page_id)
    pool.unpin(page_id)




def test_mutation_is_visible_through_every_reference(pager) -> None:
    pool = BufferPool(pager, capacity=4)
    page_id = pager.allocate_page()


    a = pool.get_page(page_id)
    b = pool.get_page(page_id)
    a[:5] = b"hello"


    assert bytes(b[:5]) == b"hello"
    pool.unpin(page_id)
    pool.unpin(page_id)




# =====================================================================
# LRU eviction
# =====================================================================


def test_eviction_targets_least_recently_used(pager) -> None:
    pool = BufferPool(pager, capacity=2)
    a, b, c = pager.allocate_page(), pager.allocate_page(), pager.allocate_page()


    a1 = pool.get_page(a)
    pool.unpin(a)
    b1 = pool.get_page(b)
    pool.unpin(b)
    pool.get_page(c)  # pool full at {a, b}; a is LRU and gets evicted
    pool.unpin(c)


    b2 = pool.get_page(b)
    assert b2 is b1  # b survived
    pool.unpin(b)


    a2 = pool.get_page(a)
    assert a2 is not a1  # a was evicted and freshly re-read
    pool.unpin(a)




def test_get_page_refreshes_recency(pager) -> None:
    pool = BufferPool(pager, capacity=2)
    a, b, c = pager.allocate_page(), pager.allocate_page(), pager.allocate_page()


    a1 = pool.get_page(a)
    pool.unpin(a)
    b1 = pool.get_page(b)
    pool.unpin(b)


    pool.get_page(a)  # touch a again -- b is now the LRU one
    pool.unpin(a)


    pool.get_page(c)  # must evict b, not a
    pool.unpin(c)


    a2 = pool.get_page(a)
    assert a2 is a1
    pool.unpin(a)


    b2 = pool.get_page(b)
    assert b2 is not b1
    pool.unpin(b)




# =====================================================================
# Pin counts: correctness, not performance
# =====================================================================


def test_pinned_page_is_never_evicted(pager) -> None:
    pool = BufferPool(pager, capacity=2)
    a, b, c = pager.allocate_page(), pager.allocate_page(), pager.allocate_page()


    a1 = pool.get_page(a)  # left pinned deliberately
    b1 = pool.get_page(b)
    pool.unpin(b)


    pool.get_page(c)  # pool full at {a, b}; a is pinned, so b must be evicted
    pool.unpin(c)


    a2 = pool.get_page(a)
    assert a2 is a1  # a survived despite being LRU, because it was pinned
    pool.unpin(a)
    pool.unpin(a)


    b2 = pool.get_page(b)
    assert b2 is not b1
    pool.unpin(b)




def test_pool_exhausted_when_every_page_is_pinned(pager) -> None:
    pool = BufferPool(pager, capacity=2)
    a, b, c = pager.allocate_page(), pager.allocate_page(), pager.allocate_page()


    pool.get_page(a)
    pool.get_page(b)


    with pytest.raises(PoolExhaustedError):
        pool.get_page(c)


    pool.unpin(a)
    pool.unpin(b)




def test_unpin_without_outstanding_pin_raises(pager) -> None:
    pool = BufferPool(pager, capacity=2)
    page_id = pager.allocate_page()
    pool.get_page(page_id)
    pool.unpin(page_id)


    with pytest.raises(ValueError):
        pool.unpin(page_id)  # no pin left to release




def test_unpin_unknown_page_raises(pager) -> None:
    pool = BufferPool(pager, capacity=2)
    with pytest.raises(ValueError):
        pool.unpin(999)




# =====================================================================
# Dirty pages: write-back correctness
# =====================================================================


def test_dirty_page_is_written_back_before_eviction(pager) -> None:
    pool = BufferPool(pager, capacity=1)
    a, b = pager.allocate_page(), pager.allocate_page()


    page = pool.get_page(a)
    page[:5] = b"hello"
    pool.unpin(a, dirty=True)


    pool.get_page(b)  # capacity=1 forces a's eviction -- must flush first
    pool.unpin(b)


    assert bytes(pager.read_page(a)[:5]) == b"hello"




def test_clean_page_evicted_without_corrupting_disk(pager) -> None:
    pool = BufferPool(pager, capacity=1)
    a, b = pager.allocate_page(), pager.allocate_page()


    page = pool.get_page(a)
    page[:5] = b"dirty"  # mutate in memory, but never mark it dirty
    pool.unpin(a, dirty=False)


    pool.get_page(b)
    pool.unpin(b)


    # a was never marked dirty, so eviction must not have written it back --
    # disk still holds whatever allocate_page originally put there (zeros).
    assert bytes(pager.read_page(a)[:5]) == bytes(5)




def test_dirty_flag_is_sticky_across_unpins(pager) -> None:
    pool = BufferPool(pager, capacity=1)
    a, b = pager.allocate_page(), pager.allocate_page()


    page = pool.get_page(a)
    page[:5] = b"first"
    pool.unpin(a, dirty=True)


    pool.get_page(a)
    pool.unpin(a, dirty=False)  # does NOT clear the earlier dirty=True


    pool.get_page(b)  # evicts a -- must still flush, since it's still dirty
    pool.unpin(b)


    assert bytes(pager.read_page(a)[:5]) == b"first"




# =====================================================================
# Explicit flush
# =====================================================================


def test_flush_page_writes_back_without_evicting(pager) -> None:
    pool = BufferPool(pager, capacity=4)
    page_id = pager.allocate_page()


    page = pool.get_page(page_id)
    page[:5] = b"world"
    pool.unpin(page_id, dirty=True)


    pool.flush_page(page_id)
    assert bytes(pager.read_page(page_id)[:5]) == b"world"


    # still cached -- flush is not eviction
    same = pool.get_page(page_id)
    assert same is page
    pool.unpin(page_id)




def test_flush_page_on_uncached_page_is_a_noop(pager) -> None:
    pool = BufferPool(pager, capacity=4)
    pool.flush_page(999)  # nothing cached under 999 -- must not raise




def test_flush_all_writes_every_dirty_page(pager) -> None:
    pool = BufferPool(pager, capacity=4)
    ids = [pager.allocate_page() for _ in range(3)]


    for i, page_id in enumerate(ids):
        page = pool.get_page(page_id)
        page[0] = i + 1
        pool.unpin(page_id, dirty=True)


    pool.flush_all()


    for i, page_id in enumerate(ids):
        assert pager.read_page(page_id)[0] == i + 1




# =====================================================================
# pinned() context manager
# =====================================================================


def test_pinned_context_manager_yields_the_page(pager) -> None:
    pool = BufferPool(pager, capacity=4)
    page_id = pager.allocate_page()


    with pool.pinned(page_id) as page:
        page[:3] = b"abc"


    assert bytes(pager.read_page(page_id)[:3]) != b"abc"  # dirty defaulted False




def test_pinned_context_manager_releases_the_pin_on_normal_exit(pager) -> None:
    pool = BufferPool(pager, capacity=1)
    a, b = pager.allocate_page(), pager.allocate_page()


    with pool.pinned(a) as page:
        page[:3] = b"abc"


    pool.get_page(b)  # only possible if `a`'s pin was released
    pool.unpin(b)




def test_pinned_context_manager_releases_the_pin_even_on_exception(pager) -> None:
    pool = BufferPool(pager, capacity=1)
    a, b = pager.allocate_page(), pager.allocate_page()


    with pytest.raises(RuntimeError), pool.pinned(a):
        raise RuntimeError("boom")


    pool.get_page(b)  # still only possible if the pin was released
    pool.unpin(b)




def test_pinned_context_manager_marks_dirty(pager) -> None:
    pool = BufferPool(pager, capacity=1)
    a, b = pager.allocate_page(), pager.allocate_page()


    with pool.pinned(a, dirty=True) as page:
        page[:3] = b"xyz"


    pool.get_page(b)  # evicts a -- must flush it first since dirty=True
    pool.unpin(b)


    assert bytes(pager.read_page(a)[:3]) == b"xyz"




# =====================================================================
# Write intent: get_page_for_write / pinned_for_write  (week 5, session 0)
# =====================================================================


def test_get_page_for_write_marks_dirty_at_acquisition(pager) -> None:
    pool = BufferPool(pager, capacity=4)
    page_id = pager.allocate_page()


    pool.get_page_for_write(page_id)


    # Dirty from the moment it's handed out -- no unpin(dirty=True) needed.
    assert pool._cache[page_id].dirty is True
    pool.unpin(page_id)




def test_get_page_for_write_returns_same_object_as_get_page(pager) -> None:
    pool = BufferPool(pager, capacity=4)
    page_id = pager.allocate_page()


    a = pool.get_page(page_id)
    pool.unpin(page_id)


    b = pool.get_page_for_write(page_id)


    assert b is a
    pool.unpin(page_id)




def test_get_page_for_write_dirty_survives_a_clean_unpin(pager) -> None:
    pool = BufferPool(pager, capacity=1)
    a, b = pager.allocate_page(), pager.allocate_page()


    page = pool.get_page_for_write(a)
    page[:5] = b"abcde"
    pool.unpin(a)  # NOT unpin(a, dirty=True) -- already dirty at acquisition


    pool.get_page(b)  # capacity=1 forces a's eviction
    pool.unpin(b)


    assert bytes(pager.read_page(a)[:5]) == b"abcde"




def test_pinned_for_write_marks_dirty_and_releases_pin(pager) -> None:
    pool = BufferPool(pager, capacity=1)
    a, b = pager.allocate_page(), pager.allocate_page()


    with pool.pinned_for_write(a) as page:
        page[:3] = b"xyz"


    pool.get_page(b)  # only possible if pinned_for_write released its pin
    pool.unpin(b)


    assert bytes(pager.read_page(a)[:3]) == b"xyz"  # dirty, so flushed on evict




def test_pinned_for_write_releases_pin_even_on_exception(pager) -> None:
    pool = BufferPool(pager, capacity=1)
    a, b = pager.allocate_page(), pager.allocate_page()


    with pytest.raises(RuntimeError), pool.pinned_for_write(a):
        raise RuntimeError("boom")


    pool.get_page(b)  # still only possible if the pin was released
    pool.unpin(b)




def test_get_page_for_write_is_an_inert_hook_this_session(pager) -> None:
    """Session 0 wires the hook, but nothing sets self._txn yet -- it must
    stay None and get_page_for_write must not raise or require one. The
    hook goes live in session 3, not here.
    """
    pool = BufferPool(pager, capacity=4)
    assert pool._txn is None


    page_id = pager.allocate_page()
    pool.get_page_for_write(page_id)  # must not raise
    pool.unpin(page_id)




# =====================================================================
# Allocation: pool.allocate_page / pool.free_page  (week 5, session 0, task 3)
#
# Pager.allocate_page/free_page still exist and are what `pager` (the
# fixture) uses to seed pages for tests above -- these exercise the NEW
# pool-mediated versions directly, in isolation, before any call site
# switches over.
# =====================================================================


def test_pool_allocate_extends_file_when_freelist_empty(pager) -> None:
    pool = BufferPool(pager, capacity=8)
    assert pager.page_count == 1


    new_id = pool.allocate_page()


    assert new_id == 2
    assert pager.page_count == 2




def test_pool_free_then_allocate_reuses_page(pager) -> None:
    pool = BufferPool(pager, capacity=8)
    page_id = pool.allocate_page()


    pool.free_page(page_id)
    reused = pool.allocate_page()


    assert reused == page_id
    assert pager.page_count == 2  # reuse must not grow the file again




def test_pool_free_schema_root_page_rejected(pager) -> None:
    pool = BufferPool(pager, capacity=8)
    with pytest.raises(ValueError):
        pool.free_page(1)




def test_pool_free_out_of_range_rejected(pager) -> None:
    pool = BufferPool(pager, capacity=8)
    with pytest.raises(PageOutOfRangeError):
        pool.free_page(99)




def test_pool_freed_page_is_fully_zeroed_once_flushed(pager) -> None:
    """Same trap as Pager's own test_freed_pages_are_fully_zeroed: stale
    bytes at offset 4 (where a trunk page's leaf count L would live, in
    the two-level format real sqlite3 expects) get misread as corruption.
    free_page must zero the whole page, not just the next-pointer bytes --
    checked against the PAGER directly, so this also confirms free_page
    writes THROUGH the pool rather than bypassing it.
    """
    pool = BufferPool(pager, capacity=8)
    pages = [pool.allocate_page() for _ in range(4)]
    for page_id in pages:
        with pool.pinned_for_write(page_id) as raw:
            raw[:] = bytearray(b"\xab" * PAGE_SIZE)


    for page_id in pages:
        pool.free_page(page_id)
    pool.flush_all()


    for page_id in pages:
        assert pager.read_page(page_id)[4:8] == b"\x00\x00\x00\x00", (
            f"page {page_id} kept stale bytes where the leaf count L lives"
        )




def test_pool_free_discards_stale_cache_before_reuse(pager) -> None:
    """The exact hazard free_page's old docstring warned callers about by
    hand: a page freed while still cached DIRTY must not let a later
    flush clobber whatever allocate_page() hands out next for that same
    page number. free_page must handle this itself now -- no caller-side
    pool.discard() dance required (see btree.py's delete(), which still
    does that dance manually until this lands and the workaround is
    deleted).
    """
    pool = BufferPool(pager, capacity=8)
    page_id = pool.allocate_page()


    with pool.pinned_for_write(page_id) as raw:
        raw[:5] = b"stale"  # dirty, cached, never flushed


    pool.free_page(page_id)
    reused = pool.allocate_page()
    assert reused == page_id  # LIFO: the only free page comes right back


    with pool.pinned_for_write(reused) as raw:
        raw[:5] = b"fresh"
    pool.flush_all()


    assert bytes(pager.read_page(reused)[:5]) == b"fresh"  # not clobbered by "stale"




def test_pool_allocate_page_leaves_no_outstanding_pin(pager) -> None:
    pool = BufferPool(pager, capacity=8)
    page_id = pool.allocate_page()


    with pytest.raises(ValueError):
        pool.unpin(page_id)  # nothing pinned -- allocate_page must not leave one




def test_pool_reused_page_leaves_no_outstanding_pin(pager) -> None:
    pool = BufferPool(pager, capacity=8)
    page_id = pool.allocate_page()
    pool.free_page(page_id)
    reused = pool.allocate_page()


    with pytest.raises(ValueError):
        pool.unpin(reused)



