import hashlib

import pytest

from quilldb.constants import PAGE_SIZE
from quilldb.storage.bufferpool import BufferPool
from quilldb.storage.pager import Pager
from quilldb.txn.journal import Journal
from quilldb.txn.transaction import Transaction


def open_txn(pager: Pager, pool: BufferPool, journal: Journal) -> Transaction:
    """Wire a Transaction into both halves of the hook the way session 4's
    connect() eventually will: Pager.write_page's assertion reads
    pager._txn, BufferPool.get_page_for_write reads pool._txn.
    """
    txn = Transaction(pager, pool, journal)
    pager._txn = txn
    pool._txn = txn
    journal.begin(pager.page_count)
    return txn


def test_will_modify_journals_the_pre_transaction_bytes_once(tmp_path) -> None:
    pager = Pager.create(tmp_path / "t.db")
    pool = BufferPool(pager)
    page_id = pool.allocate_page()  # growth path: already unpinned, already dirty
    pool.flush_page(page_id)
    original = pager.read_page(page_id)
    pager.sync()

    journal = Journal(tmp_path / "t.db")
    txn = open_txn(pager, pool, journal)

    with pool.pinned_for_write(page_id) as data:
        data[:] = b"\xaa" * PAGE_SIZE
    with pool.pinned_for_write(page_id) as data:
        data[:] = b"\xbb" * PAGE_SIZE  # second write must NOT re-journal

    assert txn._journalled == {page_id}
    assert journal._nrec == 1
    raw = journal.path.read_bytes()
    record = raw[512:]  # SECTOR_SIZE
    assert record[4:4 + PAGE_SIZE] == bytes(original)

    pager.close()


def test_will_modify_skips_a_page_allocated_this_transaction(tmp_path) -> None:
    pager = Pager.create(tmp_path / "t.db")
    pool = BufferPool(pager)
    journal = Journal(tmp_path / "t.db")
    txn = open_txn(pager, pool, journal)

    new_page_id = pool.allocate_page()  # growth path: bypasses get_page_for_write entirely
    # allocate_page's own contract: every call site re-pins separately afterward
    # to write the page's real content -- THAT second pin is what calls will_modify.
    with pool.pinned_for_write(new_page_id) as data:
        data[:] = b"\x99" * PAGE_SIZE

    assert new_page_id in txn._journalled
    assert journal._nrec == 0  # nothing written -- rollback's truncate() erases it for free

    pager.close()


def test_commit_flushes_pages_deletes_journal_and_never_writes_early(tmp_path) -> None:
    pager = Pager.create(tmp_path / "t.db")
    pool = BufferPool(pager)
    page_id = pool.allocate_page()  # growth path: already unpinned, already dirty
    pool.flush_page(page_id)
    pager.sync()

    journal = Journal(tmp_path / "t.db")
    txn = open_txn(pager, pool, journal)

    write_calls = []
    real_write_page = pager.write_page
    pager.write_page = lambda pid, data: (write_calls.append(pid), real_write_page(pid, data))[-1]  # type: ignore[method-assign]

    with pool.pinned_for_write(page_id) as data:
        data[:] = b"\xcc" * PAGE_SIZE
    assert write_calls == []  # no database write before commit()

    txn.commit()

    assert write_calls != []  # flush_all() during commit() did write
    assert txn.barrier_passed is True
    assert journal.path is not None
    assert not journal.path.exists()
    assert pager.read_page(page_id) == bytearray(b"\xcc" * PAGE_SIZE)

    pager.close()


def test_rollback_restores_a_byte_identical_file(tmp_path) -> None:
    db_path = tmp_path / "t.db"
    pager = Pager.create(db_path)
    pool = BufferPool(pager)
    for _ in range(5):
        pid = pool.allocate_page()
        with pool.pinned_for_write(pid):
            pass
    pool.flush_all()
    pager.sync()
    pager.close()

    before = hashlib.sha256(db_path.read_bytes()).digest()

    pager = Pager.open(db_path)
    pool = BufferPool(pager)
    journal = Journal(db_path)
    txn = open_txn(pager, pool, journal)

    # Mutate existing pages and grow the file, all inside the transaction.
    with pool.pinned_for_write(2) as data:
        data[:] = b"\x11" * PAGE_SIZE
    for _ in range(3):
        new_pid = pool.allocate_page()
        with pool.pinned_for_write(new_pid) as data:
            data[:] = b"\x22" * PAGE_SIZE

    txn.rollback()
    pager.close()

    after = hashlib.sha256(db_path.read_bytes()).digest()
    assert after == before


def test_no_steal_grows_the_pool_instead_of_raising(tmp_path) -> None:
    pager = Pager.create(tmp_path / "t.db")
    pool = BufferPool(pager, capacity=2)
    page_ids = []
    for _ in range(3):
        pid = pool.allocate_page()
        with pool.pinned_for_write(pid):
            pass
        page_ids.append(pid)
    pool.flush_all()
    pager.sync()
    pool.clear()  # drop the cache so the transaction below re-reads from disk (real misses)

    journal = Journal(tmp_path / "t.db")
    txn = open_txn(pager, pool, journal)

    # Dirty more pages than capacity -- each fetch past the first two is a
    # miss at full capacity, which must grow the cache rather than raise or
    # evict (every candidate is dirty under an active transaction).
    for pid in page_ids:
        with pool.pinned_for_write(pid) as data:
            data[:] = bytes([pid]) * PAGE_SIZE

    assert len(pool._cache) > pool._capacity

    txn.commit()
    pager.close()


def test_write_page_before_barrier_raises(tmp_path) -> None:
    pager = Pager.create(tmp_path / "t.db")
    pool = BufferPool(pager)
    journal = Journal(tmp_path / "t.db")
    open_txn(pager, pool, journal)

    with pytest.raises(AssertionError):
        pager.write_page(1, bytearray(PAGE_SIZE))

    pager.close()
