import pytest


from quilldb.cli import main
from quilldb.constants import PAGE_SIZE
from quilldb.storage.pager import Pager




@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    Pager.create(path).close()
    return path




# =====================================================================
# `inspect` on a valid file
# =====================================================================


def test_inspect_returns_zero_and_prints_page_size(db_path, capsys) -> None:
    exit_code = main(["inspect", str(db_path)])


    captured = capsys.readouterr()
    assert exit_code == 0
    assert str(PAGE_SIZE) in captured.out




def test_inspect_prints_every_header_field(db_path, capsys) -> None:
    main(["inspect", str(db_path)])
    out = capsys.readouterr().out


    printed = {}
    for line in out.splitlines():
        if ":" not in line:
            continue
        name, _, value = line.partition(":")
        printed[name.strip()] = value.strip()


    for field in (
        "page_size",
        "write_version",
        "read_version",
        "reserved_space",
        "change_counter",
        "page_count",
        "freelist_trunk",
        "freelist_count",
        "schema_cookie",
        "schema_format",
        "text_encoding",
        "user_version",
        "application_id",
        "sqlite_version",
    ):
        assert field in printed, f"missing field {field!r} in `inspect` output"




def test_inspect_field_values_match_the_real_header(db_path, capsys) -> None:
    from quilldb.constants import FILE_HEADER_SIZE
    from quilldb.storage.header import FileHeader


    with db_path.open("rb") as f:
        header = FileHeader.from_bytes(f.read(FILE_HEADER_SIZE))


    main(["inspect", str(db_path)])
    out = capsys.readouterr().out


    printed = {}
    for line in out.splitlines():
        if ":" not in line:
            continue
        name, _, value = line.partition(":")
        printed[name.strip()] = value.strip()


    assert printed["page_size"] == str(header.page_size)
    assert printed["page_count"] == str(header.page_count)
    assert printed["schema_cookie"] == str(header.schema_cookie)




# =====================================================================
# Error paths -- never a raw traceback
# =====================================================================


def test_inspect_missing_file_is_a_clean_error(tmp_path, capsys) -> None:
    missing = tmp_path / "does-not-exist.db"


    exit_code = main(["inspect", str(missing)])


    captured = capsys.readouterr()
    assert exit_code != 0
    assert captured.err != ""




def test_inspect_non_database_file_is_a_clean_error(tmp_path, capsys) -> None:
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not a sqlite database" + b"\x00" * 100)


    exit_code = main(["inspect", str(junk)])


    captured = capsys.readouterr()
    assert exit_code != 0
    assert captured.err != ""




# =====================================================================
# Argument parsing
# =====================================================================


def test_main_with_no_subcommand_exits_nonzero(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main([])


    assert exc_info.value.code != 0




def test_main_with_unknown_subcommand_exits_nonzero(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["frobnicate", "whatever.db"])


    assert exc_info.value.code != 0





