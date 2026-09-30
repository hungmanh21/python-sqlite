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







# =====================================================================
# `pages`, `btree`, `validate`, `bench`, `shell`
# =====================================================================


@pytest.fixture
def populated_path(tmp_path):
    import quilldb

    path = tmp_path / "populated.db"
    db = quilldb.connect(path)
    db.execute("CREATE TABLE users (id INTEGER, email TEXT)")
    db.execute("CREATE INDEX ix_email ON users (email)")
    db.execute("BEGIN")
    for i in range(1, 601):
        db.execute("INSERT INTO users VALUES (?, ?)", (i, f"u{i}@example.com"))
    db.execute("COMMIT")
    db.close()
    return path


def test_pages_lists_every_page_with_its_type(populated_path, capsys) -> None:
    assert main(["pages", str(populated_path)]) == 0
    out = capsys.readouterr().out
    assert "LEAF_TABLE" in out and "INTERIOR_TABLE" in out and "LEAF_INDEX" in out


def test_pages_range_limits_the_output(populated_path, capsys) -> None:
    main(["pages", str(populated_path), "--range", "1-2"])
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 3  # header + two pages


def test_pages_rejects_a_backwards_range(populated_path, capsys) -> None:
    assert main(["pages", str(populated_path), "--range", "5-2"]) == 1
    assert "backwards" in capsys.readouterr().err


def test_pages_on_a_missing_file_fails_cleanly(tmp_path, capsys) -> None:
    assert main(["pages", str(tmp_path / "nope.db")]) == 1
    assert "no such file" in capsys.readouterr().err


def test_btree_renders_a_multi_level_tree(populated_path, capsys) -> None:
    from quilldb.cli import render_btree

    # Page 2 is quill_stat1's root, page 3 the users table's.
    lines = render_btree(populated_path, 3, max_depth=3)
    assert lines[0].startswith("page 3 INTERIOR_TABLE")
    assert any("LEAF_TABLE" in line and "rowids 1.." in line for line in lines)


def test_btree_depth_cuts_the_tree_off(populated_path) -> None:
    from quilldb.cli import render_btree

    lines = render_btree(populated_path, 3, max_depth=1)
    assert len(lines) == 2 and "children below" in lines[1]


def test_btree_reports_a_root_outside_the_file(populated_path) -> None:
    from quilldb.cli import render_btree

    assert "outside the file" in render_btree(populated_path, 9999, max_depth=2)[0]


def test_validate_accepts_a_good_file(populated_path, capsys) -> None:
    assert main(["validate", str(populated_path)]) == 0
    out = capsys.readouterr().out
    assert "Both agree" in out and "ix_email: ok (600 entries)" in out


def test_validate_rejects_a_zero_cell_interior_page(populated_path, capsys) -> None:
    """The B4-4 corruption: real SQLite calls the whole file malformed."""
    data = bytearray(populated_path.read_bytes())
    interior = 2 * PAGE_SIZE  # page 3, the users table's root
    data[interior + 3 : interior + 5] = b"\x00\x00"
    populated_path.write_bytes(data)

    assert main(["validate", str(populated_path)]) == 1
    assert "malformed" in capsys.readouterr().out


def test_validate_warns_about_a_leftover_journal(populated_path, capsys) -> None:
    populated_path.with_name(populated_path.name + "-journal").write_bytes(b"")
    main(["validate", str(populated_path)])
    assert "journal" in capsys.readouterr().out


def test_bench_list_names_the_benchmarks(capsys) -> None:
    assert main(["bench", "--list"]) == 0
    assert "hit_rate" in capsys.readouterr().out


def _shell(path, lines, capsys):
    from quilldb.shell import run_shell

    feed = iter(lines)

    def fake_input(_prompt: str) -> str:
        try:
            return next(feed)
        except StopIteration:
            raise EOFError from None

    assert run_shell(path, input_fn=fake_input) == 0
    return capsys.readouterr().out


def test_shell_runs_statements_and_prints_a_table(tmp_path, capsys) -> None:
    out = _shell(
        tmp_path / "s.db",
        [
            "CREATE TABLE t (a INTEGER, b TEXT);",
            "INSERT INTO t VALUES (1, NULL);",
            "INSERT INTO t",
            "  VALUES (2, 'two');",  # a statement across two lines
            "SELECT * FROM t;",
        ],
        capsys,
    )
    assert "a | b" in out and "1 | NULL" in out and "2 | two" in out and "(2 rows)" in out


def test_shell_survives_an_error_and_keeps_going(tmp_path, capsys) -> None:
    out = _shell(tmp_path / "s.db", ["SELECT * FROM missing;", "CREATE TABLE t (a INTEGER);", ".tables"], capsys)
    assert "Error:" in out and "\nt\n" in out


def test_shell_prints_explain_as_plain_lines(tmp_path, capsys) -> None:
    out = _shell(
        tmp_path / "s.db", ["CREATE TABLE t (a INTEGER);", "EXPLAIN SELECT * FROM t;"], capsys
    )
    assert "SeqScan t" in out and "(1 row)" not in out


def test_shell_dot_commands_and_quit(tmp_path, capsys) -> None:
    out = _shell(tmp_path / "s.db", ["CREATE TABLE t (a INTEGER);", ".schema t", ".quit", "SELECT 1;"], capsys)
    assert "CREATE TABLE t (a INTEGER);" in out


def test_shell_bang_runs_a_system_command(tmp_path, capfd) -> None:
    from quilldb.shell import run_shell

    lines = iter(["!echo from-the-system-shell"])

    def fake_input(_prompt: str) -> str:
        try:
            return next(lines)
        except StopIteration:
            raise EOFError from None

    run_shell(tmp_path / "s.db", input_fn=fake_input)
    assert "from-the-system-shell" in capfd.readouterr().out


def test_format_table_aligns_columns_and_marks_nulls() -> None:
    from quilldb.shell import format_table

    text = format_table(["id", "name"], [(1, "ada"), (22, None)])
    assert text.splitlines()[0] == "id | name"
    assert "22 | NULL" in text and text.endswith("(2 rows)")


@pytest.mark.parametrize(
    ("text", "complete"),
    [
        ("SELECT 1;", True),
        ("SELECT 1;   \n", True),
        ("SELECT 1", False),
        ("", False),
        ("INSERT INTO t VALUES ('a;b')", False),  # the ';' is inside the string
        ("INSERT INTO t VALUES ('a;b');", True),
        ("INSERT INTO t VALUES ('abc;", False),  # string still open
        ("INSERT INTO t VALUES ('it''s;');", True),  # doubled quote is an escape
        ("INSERT INTO t VALUES ('it''s;')", False),
        ('SELECT "a;b" FROM t', False),
        ("SELECT 1 -- not done;", False),  # ';' in a comment does not count
        ("SELECT 1; -- done", True),  # a trailing comment after ';' does
        ("SELECT 1 /* ; */", False),
        ("SELECT 1; /* note */", True),
        ("SELECT 1 /* unterminated;", False),
        ("SELECT 1\n-- c;\n;", True),
    ],
)
def test_statement_complete(text, complete) -> None:
    from quilldb.shell import statement_complete

    assert statement_complete(text) is complete


def test_shell_keeps_reading_while_a_string_is_open(tmp_path, capsys) -> None:
    out = _shell(
        tmp_path / "s.db",
        [
            "CREATE TABLE t (a TEXT);",
            "INSERT INTO t VALUES ('one;",  # would have ended here before
            "two');",
            "SELECT a FROM t;",
        ],
        capsys,
    )
    # One row whose text contains the newline; the table shows it as \n.
    assert "one;\\ntwo" in out and "(1 row)" in out
