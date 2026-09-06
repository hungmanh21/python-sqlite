import pytest


from quilldb.constants import PAGE_SIZE
from quilldb.errors import PoolExhaustedError
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



