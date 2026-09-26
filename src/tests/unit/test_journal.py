import pytest

from quilldb.constants import JOURNAL_MAGIC, PAGE_SIZE, SECTOR_SIZE
from quilldb.storage.pager import Pager
from quilldb.txn.journal import Journal, journal_checksum

RECORD_SIZE = 4 + PAGE_SIZE + 4  # page_id u32 + one page + checksum u32


def test_header_is_padded_to_sector_size(tmp_path) -> None:
    journal = Journal(tmp_path / "t.db")
    journal.begin(page_count_before=29)

    assert journal.path is not None
    raw = journal.path.read_bytes()
    assert len(raw) == SECTOR_SIZE


def test_magic_and_nrec_are_withheld_after_begin(tmp_path) -> None:
    """The other half of test_magic_and_nrec_are_zero_until_the_barrier (see
    docs/implementation/week5-transactions.md §29) — the post-commit_barrier
    half moves to test_journal.py once session 2 implements commit_barrier().
    """
    journal = Journal(tmp_path / "t.db")
    journal.begin(page_count_before=29)
    journal.record_original(7, b"\xab" * PAGE_SIZE)

    assert journal.path is not None
    raw = journal.path.read_bytes()
    assert raw[0:12] == b"\x00" * 12  # magic AND nRec both withheld — §13.6


def test_begin_refuses_to_overwrite_a_hot_journal(tmp_path) -> None:
    journal = Journal(tmp_path / "t.db")
    journal.begin(page_count_before=1)

    other = Journal(tmp_path / "t.db")
    with pytest.raises(FileExistsError):
        other.begin(page_count_before=1)


def test_record_original_appends_page_id_data_and_checksum(tmp_path) -> None:
    journal = Journal(tmp_path / "t.db")
    journal.begin(page_count_before=1)
    payload = b"\xcd" * PAGE_SIZE

    journal.record_original(5, payload)

    assert journal.path is not None
    raw = journal.path.read_bytes()
    record = raw[SECTOR_SIZE:]
    assert int.from_bytes(record[0:4], "big") == 5
    assert record[4:4 + PAGE_SIZE] == payload
    expected_checksum = journal_checksum(payload, journal._nonce)
    assert int.from_bytes(record[4 + PAGE_SIZE:4 + PAGE_SIZE + 4], "big") == expected_checksum


@pytest.mark.parametrize("nonce", [0, 1, 0x7B6057E4, 0xFFFFFFFF])
def test_checksum_is_deterministic_and_nonce_dependent(nonce: int) -> None:
    page = (bytes(range(256)) * 16)[:PAGE_SIZE]
    assert journal_checksum(page, nonce) == journal_checksum(page, nonce)
    assert journal_checksum(page, nonce) != journal_checksum(page, nonce + 1)


def test_checksum_only_samples_every_200th_byte() -> None:
    """Documents the tradeoff rather than pretending it's a real checksum."""
    a = bytearray(PAGE_SIZE)
    b = bytearray(PAGE_SIZE)
    b[97] = 0xFF  # 97 is NOT a sampled offset
    assert journal_checksum(bytes(a), 0) == journal_checksum(bytes(b), 0)

    b2 = bytearray(PAGE_SIZE)
    b2[96] = 0xFF  # 96 IS sampled
    assert journal_checksum(bytes(a), 0) != journal_checksum(bytes(b2), 0)


# --- session 2: commit_barrier() / replay() / delete() ---


def test_commit_barrier_flips_magic_and_nrec(tmp_path) -> None:
    journal = Journal(tmp_path / "t.db")
    journal.begin(page_count_before=29)
    journal.record_original(7, b"\xab" * PAGE_SIZE)

    journal.commit_barrier()

    assert journal.path is not None
    raw = journal.path.read_bytes()
    assert raw[0:8] == JOURNAL_MAGIC
    assert int.from_bytes(raw[8:12], "big") == 1


def test_replay_returns_zero_before_commit_barrier(tmp_path) -> None:
    journal = Journal(tmp_path / "t.db")
    journal.begin(page_count_before=1)
    journal.record_original(2, b"\xab" * PAGE_SIZE)
    pager = Pager.create(tmp_path / "target.db")

    assert journal.replay(pager) == 0
    pager.close()


def test_replay_ignores_a_journal_with_magic_zeroed(tmp_path) -> None:
    """Magic and nRec are checked independently -- a torn write that flips
    one but not the other must still be rejected (§13.6).
    """
    journal = Journal(tmp_path / "t.db")
    journal.begin(page_count_before=1)
    journal.record_original(2, b"\xab" * PAGE_SIZE)
    journal.commit_barrier()

    assert journal._file is not None
    journal._file.seek(0)
    journal._file.write(b"\x00" * 8)  # zero the magic only; nRec stays 1
    journal._file.flush()

    pager = Pager.create(tmp_path / "target.db")
    assert journal.replay(pager) == 0
    pager.close()


def test_replay_ignores_a_journal_with_nrec_zeroed(tmp_path) -> None:
    journal = Journal(tmp_path / "t.db")
    journal.begin(page_count_before=1)
    journal.record_original(2, b"\xab" * PAGE_SIZE)
    journal.commit_barrier()

    assert journal._file is not None
    journal._file.seek(8)
    journal._file.write(b"\x00" * 4)  # zero nRec only; magic stays intact
    journal._file.flush()

    pager = Pager.create(tmp_path / "target.db")
    assert journal.replay(pager) == 0
    pager.close()


def test_replay_restores_pages_through_pager(tmp_path) -> None:
    journal = Journal(tmp_path / "t.db")
    journal.begin(page_count_before=1)
    journal.record_original(2, b"\xaa" * PAGE_SIZE)
    journal.record_original(3, b"\xbb" * PAGE_SIZE)
    journal.commit_barrier()

    target_path = tmp_path / "target.db"
    pager = Pager.create(target_path)
    restored = journal.replay(pager)
    pager.sync()
    pager.close()

    assert restored == 2
    # read through a raw handle, not pager.read_page() -- restore_page()
    # deliberately doesn't bump page_count (that's recovery's job, session 2
    # of recovery.py), so pages 2/3 are legitimately "out of range" to the
    # Pager's own bounds check right now.
    with target_path.open("rb") as f:
        f.seek(1 * PAGE_SIZE)
        assert f.read(PAGE_SIZE) == b"\xaa" * PAGE_SIZE
        f.seek(2 * PAGE_SIZE)
        assert f.read(PAGE_SIZE) == b"\xbb" * PAGE_SIZE


def test_replay_stops_at_a_bad_checksum(tmp_path) -> None:
    journal = Journal(tmp_path / "t.db")
    journal.begin(page_count_before=1)
    journal.record_original(2, b"\xaa" * PAGE_SIZE)
    journal.record_original(3, b"\xbb" * PAGE_SIZE)
    journal.commit_barrier()

    # Flip a byte inside record 2's page data, at offset 96 -- one of the
    # checksum's sampled offsets (see test_checksum_only_samples_every_200th_byte).
    second_record_offset = SECTOR_SIZE + RECORD_SIZE
    assert journal._file is not None
    journal._file.seek(second_record_offset + 4 + 96)  # +4 skips record 2's page_id field
    journal._file.write(b"\xff")
    journal._file.flush()

    target_path = tmp_path / "target.db"
    pager = Pager.create(target_path)
    restored = journal.replay(pager)
    pager.sync()
    pager.close()

    assert restored == 1  # record 1 only -- record 2 failed its checksum
    with target_path.open("rb") as f:
        f.seek(1 * PAGE_SIZE)
        assert f.read(PAGE_SIZE) == b"\xaa" * PAGE_SIZE


def test_delete_removes_the_journal_file(tmp_path) -> None:
    journal = Journal(tmp_path / "t.db")
    journal.begin(page_count_before=1)
    journal.commit_barrier()
    assert journal.path is not None
    assert journal.path.exists()

    journal.delete()

    assert not journal.path.exists()


def test_in_memory_journal_commit_and_delete_do_not_touch_disk(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    journal = Journal(None)
    journal.begin(page_count_before=1)
    journal.record_original(2, b"\xaa" * PAGE_SIZE)

    journal.commit_barrier()  # must not raise despite no filesystem path
    journal.delete()

    assert list(tmp_path.iterdir()) == []
