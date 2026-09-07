import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st


from quilldb.errors import OverflowCycleError
from quilldb.storage.bufferpool import BufferPool
from quilldb.storage.overflow import CONTENT_PER_PAGE, read_overflow_chain, write_overflow_chain
from quilldb.storage.pager import Pager




@pytest.fixture
def pager(tmp_path):
    p = Pager.create(tmp_path / "test.db")
    yield p
    p.close()




@pytest.fixture
def pool(pager):
    return BufferPool(pager, capacity=16)




# =====================================================================
# Round trips -- the headline property. Sizes chosen to hit the page
# boundary from both sides: below it (single page), exactly on it
# (next == 0 with a full page of content), and past it (multi-page).
# =====================================================================




def test_round_trip_single_short_page(pager, pool) -> None:
    data = b"hello overflow"
    first_page = write_overflow_chain(pager, pool, data)
    assert read_overflow_chain(pager, pool, first_page, len(data)) == data




def test_round_trip_exactly_one_full_page(pager, pool) -> None:
    data = b"x" * CONTENT_PER_PAGE
    first_page = write_overflow_chain(pager, pool, data)
    assert read_overflow_chain(pager, pool, first_page, len(data)) == data




def test_round_trip_spills_into_second_page(pager, pool) -> None:
    data = b"y" * (CONTENT_PER_PAGE + 1)
    first_page = write_overflow_chain(pager, pool, data)
    assert read_overflow_chain(pager, pool, first_page, len(data)) == data




def test_round_trip_ten_kilobytes(pager, pool) -> None:
    """Success-state bullet from roadmap.md: a 10KB value round-trips."""
    data = bytes((i * 7) % 256 for i in range(10_000))
    first_page = write_overflow_chain(pager, pool, data)
    assert read_overflow_chain(pager, pool, first_page, len(data)) == data




@given(st.binary(min_size=1, max_size=3 * CONTENT_PER_PAGE + 50))
@settings(max_examples=25, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_round_trip_property(pager, pool, data: bytes) -> None:
    first_page = write_overflow_chain(pager, pool, data)
    assert read_overflow_chain(pager, pool, first_page, len(data)) == data




# =====================================================================
# Chain shape -- verify the on-disk structure directly, not just the
# round trip, so a bug that happens to cancel out on read/write can't hide.
# =====================================================================




def test_write_allocates_exactly_ceil_len_over_capacity_pages(pager, pool) -> None:
    before = pager.page_count
    data = b"z" * (2 * CONTENT_PER_PAGE + 1)  # needs 3 pages
    write_overflow_chain(pager, pool, data)
    assert pager.page_count - before == 3




def test_single_page_chain_has_next_pointer_zero(pager, pool) -> None:
    first_page = write_overflow_chain(pager, pool, b"short")
    raw = pool.get_page(first_page)
    assert raw[0:4] == (0).to_bytes(4, "big")
    pool.unpin(first_page)




def test_multi_page_chain_links_are_followable_by_hand(pager, pool) -> None:
    data = b"w" * (2 * CONTENT_PER_PAGE + 5)  # 3 pages
    first_page = write_overflow_chain(pager, pool, data)


    visited = []
    page_id = first_page
    for _ in range(10):  # generous cap; a real loop would still be caught below
        raw = pool.get_page(page_id)
        next_page = int.from_bytes(raw[0:4], "big")
        pool.unpin(page_id)
        visited.append(page_id)
        if next_page == 0:
            break
        page_id = next_page


    assert len(visited) == 3
    assert len(set(visited)) == 3  # no page reused




def test_last_page_content_past_total_len_is_ignored(pager, pool) -> None:
    """The last page may be padded past total_len -- read must not include it."""
    data = b"q" * (CONTENT_PER_PAGE + 3)
    first_page = write_overflow_chain(pager, pool, data)
    result = read_overflow_chain(pager, pool, first_page, len(data))
    assert result == data
    assert len(result) == len(data)




# =====================================================================
# Errors
# =====================================================================




def test_write_rejects_empty_data(pager, pool) -> None:
    with pytest.raises(ValueError):
        write_overflow_chain(pager, pool, b"")




def test_read_detects_a_cycle(pager, pool) -> None:
    """Hand-corrupt a two-page chain so it loops back on itself, matching
    the hand-built-fixture technique from §6.2 -- this can't be produced by
    write_overflow_chain itself, only by a corrupt file.
    """
    page_a = pager.allocate_page()
    page_b = pager.allocate_page()


    buf_a = pool.get_page(page_a)
    buf_a[0:4] = page_b.to_bytes(4, "big")
    pool.unpin(page_a, dirty=True)


    buf_b = pool.get_page(page_b)
    buf_b[0:4] = page_a.to_bytes(4, "big")  # loops back to A instead of ending
    pool.unpin(page_b, dirty=True)


    # Ask for more bytes than two pages hold, so the walk must follow the
    # loop at least once more instead of stopping exactly at page B.
    with pytest.raises(OverflowCycleError):
        read_overflow_chain(pager, pool, page_a, 2 * CONTENT_PER_PAGE + 10)



