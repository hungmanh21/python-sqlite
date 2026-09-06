import subprocess

import pytest

from quilldb.constants import OFF_RESERVED, PAGE_SIZE, RESERVED_LEN
from quilldb.errors import PageOutOfRangeError
from quilldb.storage.pager import Pager, page_header_offset


def test_create_then_open_roundtrip(tmp_path) -> None:
    path = tmp_path / "test.db"
    Pager.create(path).close()


    pager = Pager.open(path)
    assert pager.page_count == 1  # page 1 alone is a valid empty database
    pager.close()




def test_create_refuses_existing_file(tmp_path) -> None:
    path = tmp_path / "test.db"
    Pager.create(path).close()
    with pytest.raises(FileExistsError):
        Pager.create(path)




def test_open_missing_file_raises(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        Pager.open(tmp_path / "missing.db")




def test_write_then_read_is_faithful(tmp_path) -> None:
    pager = Pager.create(tmp_path / "test.db")
    page_id = pager.allocate_page()
    payload = bytearray(PAGE_SIZE)
    payload[:5] = b"hello"
    pager.write_page(page_id, payload)
    assert pager.read_page(page_id) == payload
    pager.close()




def test_read_out_of_range_rejected(tmp_path) -> None:
    pager = Pager.create(tmp_path / "test.db")
    with pytest.raises(PageOutOfRangeError):
        pager.read_page(2)  # page_count is 1
    with pytest.raises(PageOutOfRangeError):
        pager.read_page(0)
    pager.close()




def test_write_wrong_size_rejected(tmp_path) -> None:
    pager = Pager.create(tmp_path / "test.db")
    with pytest.raises(ValueError):
        pager.write_page(1, b"too short")
    pager.close()




def test_reserved_header_bytes_zero_on_create(tmp_path) -> None:
    """Only bytes 72..91 are reserved. The rest of the header is real fields."""
    path = tmp_path / "test.db"
    Pager.create(path).close()
    data = path.read_bytes()
    assert data[OFF_RESERVED:OFF_RESERVED + RESERVED_LEN] == b"\x00" * RESERVED_LEN




def test_page_count_matches_file_size(tmp_path) -> None:
    path = tmp_path / "test.db"
    Pager.create(path).close()
    assert path.stat().st_size == 1 * PAGE_SIZE




def test_page_header_offset_only_special_cases_page_one() -> None:
    assert page_header_offset(1) == 100
    assert page_header_offset(2) == 0
    assert page_header_offset(9999) == 0




def test_create_makes_page_one_an_empty_leaf_table(tmp_path) -> None:
    """Page 1 is the sqlite_schema root: a leaf table b-tree at byte 100."""
    path = tmp_path / "test.db"
    Pager.create(path).close()
    data = path.read_bytes()
    hdr = page_header_offset(1)
    assert data[hdr] == 13                                        # LEAF_TABLE
    assert int.from_bytes(data[hdr + 3:hdr + 5], "big") == 0       # no cells
    assert int.from_bytes(data[hdr + 5:hdr + 7], "big") == PAGE_SIZE




def test_sqlite3_accepts_a_freshly_created_database(tmp_path) -> None:
    """The acceptance test for the whole format. Keep this green from day one."""
    path = tmp_path / "test.db"
    Pager.create(path).close()
    result = subprocess.run(
        ["sqlite3", str(path), "PRAGMA integrity_check;"],
        capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == "ok"




def test_sqlite3_sees_no_tables_in_a_fresh_database(tmp_path) -> None:
    path = tmp_path / "test.db"
    Pager.create(path).close()
    result = subprocess.run(
        ["sqlite3", str(path), "SELECT count(*) FROM sqlite_schema;"],
        capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == "0"




def test_allocate_extends_file_when_freelist_empty(tmp_path) -> None:
    pager = Pager.create(tmp_path / "test.db")
    assert pager.page_count == 1
    new_id = pager.allocate_page()
    assert new_id == 2
    assert pager.page_count == 2
    pager.close()




def test_free_then_allocate_reuses_page(tmp_path) -> None:
    pager = Pager.create(tmp_path / "test.db")
    page_id = pager.allocate_page()
    pager.free_page(page_id)


    reused = pager.allocate_page()
    assert reused == page_id
    assert pager.page_count == 2  # reuse must not grow the file again
    pager.close()




def test_free_schema_root_page_rejected(tmp_path) -> None:
    """Page 1 holds the file header and the schema b-tree. It can never be freed."""
    pager = Pager.create(tmp_path / "test.db")
    with pytest.raises(ValueError):
        pager.free_page(1)
    pager.close()




def test_free_out_of_range_rejected(tmp_path) -> None:
    pager = Pager.create(tmp_path / "test.db")
    with pytest.raises(PageOutOfRangeError):
        pager.free_page(99)
    pager.close()




def test_freelist_persists_across_reopen(tmp_path) -> None:
    path = tmp_path / "test.db"
    pager = Pager.create(path)
    page_id = pager.allocate_page()
    pager.free_page(page_id)
    pager.close()


    reopened = Pager.open(path)
    reused = reopened.allocate_page()
    assert reused == page_id
    reopened.close()




def test_freed_pages_are_fully_zeroed(tmp_path) -> None:
    """The freelist correctness trap, and it is not the one you'd guess.


    A one-level freelist is *legal* — sqlite3 reads it as a chain of trunk pages
    each holding zero leaves, and integrity_check says ok. Trunk pages are a
    performance win (N page touches -> N/120), not a validity requirement.


    What is NOT legal is leaving stale data in a freed page. Bytes 4..7 of a
    trunk page are L, the leaf count. Leave old row data there and sqlite3
    reports "freelist leaf count too big" — so free_page must zero the whole
    page, not just write the next pointer.
    """
    path = tmp_path / "test.db"
    pager = Pager.create(path)


    pages = [pager.allocate_page() for _ in range(4)]
    for page_id in pages:                      # dirty them like real rows would
        payload = bytearray(b"\xab" * PAGE_SIZE)
        pager.write_page(page_id, payload)
    for page_id in pages:
        pager.free_page(page_id)
    pager.sync()


    for page_id in pages:
        assert pager.read_page(page_id)[4:8] == b"\x00\x00\x00\x00", (
            f"page {page_id} kept stale bytes where the leaf count L lives"
        )
    pager.close()


    result = subprocess.run(
        ["sqlite3", str(path), "PRAGMA integrity_check;"],
        capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == "ok"




