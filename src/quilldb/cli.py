"""The `quilldb` command line.

    quilldb shell FILE                 an interactive SQL prompt
    quilldb inspect FILE               the 100-byte file header
    quilldb pages FILE [--range A-B]   page types, cell counts, free space
    quilldb btree FILE --root N        the shape of one b-tree
    quilldb validate FILE              quilldb's validator, then real SQLite's
    quilldb bench [NAME ...]           the benchmarks, as markdown

`inspect`, `pages` and `btree` read the file's bytes directly, not through
Pager -- a diagnostic tool that refuses to open the broken file you wanted
to diagnose is worse than none, so they work (and say what is wrong) even on
a file quilldb can't fully open, e.g. one with an unsupported page size.
"""


import argparse
import sqlite3
import sys
from dataclasses import fields
from pathlib import Path

from quilldb.btree.cells import decode_interior_table_cell
from quilldb.btree.validate import validate_btree, validate_index_btree
from quilldb.catalog.catalog import Catalog
from quilldb.constants import FILE_HEADER_SIZE, PAGE_SIZE, PageType
from quilldb.errors import QuillDBError
from quilldb.storage.bufferpool import BufferPool
from quilldb.storage.header import FileHeader
from quilldb.storage.page import PageBody, parse_page
from quilldb.storage.pager import Pager


def read_header(path: Path) -> FileHeader:
    """Read and parse just the header -- no Pager, no page cache involved.


    Raises:
        FileNotFoundError: path doesn't exist.
        InvalidHeaderError / CorruptDatabaseError: not a file quilldb
            recognizes or supports.
    """
    with path.open("rb") as f:
        return FileHeader.from_bytes(f.read(FILE_HEADER_SIZE))




def format_header(header: FileHeader) -> str:
    """Render every field of `header` as human-readable text.


    Contract the tests rely on: one "name: value" line per field, using
    FileHeader's own attribute names (page_size, page_count, ...) as the
    names. That keeps this checkable without pinning exact wording.
    """
    # TODO: decide the labels/order and build the string. `dataclasses.fields(header)`
    # gives you every field name in declaration order if you want to loop instead of
    # listing them by hand. Look at `sqlite3 file.db ".dbinfo"` for a real precedent
    # on grouping/labeling, but you're not required to match it exactly.
    res = []
    for field in fields(header):
        res.append(f"{field.name}: {getattr(header, field.name)}")
    return "\n".join(res)




def read_page_bytes(path: Path, page_number: int) -> bytes:
    """One raw page, straight from the file. Page numbers start at 1."""
    with path.open("rb") as f:
        f.seek((page_number - 1) * PAGE_SIZE)
        return f.read(PAGE_SIZE)


def file_page_count(path: Path) -> int:
    """How many whole pages the file holds, by size (not by what the header claims)."""
    return path.stat().st_size // PAGE_SIZE


def parse_range(text: str, last: int) -> range:
    """`"3-7"` -> pages 3..7, `"5"` -> just page 5, clipped to the file."""
    low, _, high = text.partition("-")
    first, final = int(low), int(high or low)
    if first < 1 or final < first:
        raise ValueError(f"bad page range {text!r}: pages start at 1 and the range must not run backwards")
    return range(first, min(final, last) + 1)


def describe_page(raw: bytes, page_number: int) -> str:
    """One line: page number, type, cells, free bytes -- or why it isn't a b-tree page."""
    try:
        body = parse_page(raw, 100 if page_number == 1 else 0)
    except QuillDBError as exc:
        return f"{page_number:>6}  not a b-tree page ({type(exc).__name__}: a freelist, overflow or corrupt page)"
    extra = f"  right_child={body.right_child}" if body.page_type in (
        PageType.INTERIOR_TABLE, PageType.INTERIOR_INDEX
    ) else ""
    line = f"{page_number:>6}  {body.page_type.name:<15} cells={body.cell_count:<5} free={body.free_bytes():<5}{extra}"
    return line.rstrip()


def cmd_pages(path: Path, page_range: str | None) -> int:
    total = file_page_count(path)
    if total == 0:
        print(f"quilldb: {path} holds no whole {PAGE_SIZE}-byte page", file=sys.stderr)
        return 1
    try:
        pages = parse_range(page_range, total) if page_range else range(1, total + 1)
    except ValueError as exc:
        print(f"quilldb: {exc}", file=sys.stderr)
        return 1
    print(f"{'page':>6}  {'type':<15} cells / free")
    for number in pages:
        print(describe_page(read_page_bytes(path, number), number))
    return 0


def _child_pages(body: PageBody) -> list[int]:
    """Child page numbers of an interior page, left to right, right_child last."""
    children = [int.from_bytes(cell[:4], "big") for cell in body.cells]
    children.append(body.right_child)
    return children


def render_btree(path: Path, root: int, max_depth: int, max_children: int = 8) -> list[str]:
    """The tree under `root` as indented lines, cut off below `max_depth`."""
    total = file_page_count(path)
    lines: list[str] = []
    seen: set[int] = set()

    def walk(page_number: int, depth: int) -> None:
        indent = "  " * depth
        if not 1 <= page_number <= total:
            lines.append(f"{indent}page {page_number}: outside the file ({total} pages)")
            return
        if page_number in seen:
            lines.append(f"{indent}page {page_number}: reached twice (a cycle or shared child)")
            return
        seen.add(page_number)
        try:
            body = parse_page(read_page_bytes(path, page_number), 100 if page_number == 1 else 0)
        except QuillDBError as exc:
            lines.append(f"{indent}page {page_number}: not a b-tree page ({type(exc).__name__})")
            return
        label = f"{indent}page {page_number} {body.page_type.name} cells={body.cell_count}"
        if body.page_type is PageType.LEAF_TABLE and body.cells:
            from quilldb.btree.cells import decode_leaf_table_cell

            first = decode_leaf_table_cell(body.cells[0])[0]
            last = decode_leaf_table_cell(body.cells[-1])[0]
            label += f" rowids {first}..{last}"
        elif body.page_type is PageType.INTERIOR_TABLE and body.cells:
            keys = [decode_interior_table_cell(cell)[1] for cell in body.cells]
            label += f" separators {keys[0]}..{keys[-1]}"
        lines.append(label)
        if body.page_type not in (PageType.INTERIOR_TABLE, PageType.INTERIOR_INDEX):
            return
        children = _child_pages(body)
        if depth + 1 >= max_depth:
            lines.append(f"{indent}  ... {len(children)} children below (raise --depth to see them)")
            return
        for child in children[:max_children]:
            walk(child, depth + 1)
        if len(children) > max_children:
            lines.append(f"{indent}  ... and {len(children) - max_children} more children")

    walk(root, 0)
    return lines


def cmd_btree(path: Path, root: int, depth: int) -> int:
    for line in render_btree(path, root, depth):
        print(line)
    return 0


def validate_with_quilldb(path: Path) -> list[str]:
    """Run quilldb's own structural validator over every table and index. Returns problems."""
    problems: list[str] = []
    pager = Pager.open(path)
    try:
        pool = BufferPool(pager)
        catalog = Catalog(pager, pool)
        catalog.load()
        for table in catalog.list_tables():
            try:
                validate_btree(pager, pool, table.root_page)
                print(f"  table {table.name}: ok")
            except QuillDBError as exc:
                problems.append(f"table {table.name}: {exc}")
                print(f"  table {table.name}: {exc}")
            for index in catalog.indexes_for(table.name):
                try:
                    entries = validate_index_btree(pager, pool, index.root_page, len(index.columns))
                    print(f"  index {index.name}: ok ({entries} entries)")
                except QuillDBError as exc:
                    problems.append(f"index {index.name}: {exc}")
                    print(f"  index {index.name}: {exc}")
    finally:
        pager.close()
    return problems


def validate_with_sqlite(path: Path) -> list[str]:
    """Ask real SQLite (the standard library's `sqlite3`) to check the file. Returns problems."""
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = [row[0] for row in connection.execute("PRAGMA integrity_check")]
    except sqlite3.DatabaseError as exc:
        print(f"  {exc}")
        return [f"sqlite3 could not read the file: {exc}"]
    finally:
        connection.close()
    print(f"  {rows[0] if rows == ['ok'] else '; '.join(rows)}")
    return [] if rows == ["ok"] else rows


def cmd_validate(path: Path) -> int:
    journal = path.with_name(path.name + "-journal")
    if journal.exists():
        print(f"warning: {journal.name} exists, so the last commit may not have finished; "
              "opening the file with quilldb recovers it, and this check does not.")
    print("quilldb's validator:")
    try:
        problems = validate_with_quilldb(path)
    except QuillDBError as exc:
        problems = [f"quilldb could not open the file: {exc}"]
        print(f"  {problems[0]}")
    print("real SQLite: PRAGMA integrity_check")
    problems += validate_with_sqlite(path)
    print("\nBoth agree: the file is valid." if not problems else f"\n{len(problems)} problem(s) found.")
    return 0 if not problems else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quilldb")
    subparsers = parser.add_subparsers(dest="command", required=True)

    shell_parser = subparsers.add_parser("shell", help="an interactive SQL prompt")
    shell_parser.add_argument("path", type=Path)

    inspect_parser = subparsers.add_parser("inspect", help="print a database file's header")
    inspect_parser.add_argument("path", type=Path)

    pages_parser = subparsers.add_parser("pages", help="page types, cell counts and free space")
    pages_parser.add_argument("path", type=Path)
    pages_parser.add_argument("--range", dest="page_range", metavar="A-B", help="pages to show, e.g. 1-10 (default: all)")

    btree_parser = subparsers.add_parser("btree", help="render the structure of one b-tree")
    btree_parser.add_argument("path", type=Path)
    btree_parser.add_argument("--root", type=int, required=True, help="the root page (see `quilldb pages`)")
    btree_parser.add_argument("--depth", type=int, default=3, help="levels to show (default 3)")

    validate_parser = subparsers.add_parser("validate", help="check the file with quilldb's validator and real SQLite")
    validate_parser.add_argument("path", type=Path)

    bench_parser = subparsers.add_parser("bench", help="run the benchmarks and print markdown")
    bench_parser.add_argument("names", nargs="*", help="benchmarks to run (default: all)")
    bench_parser.add_argument("--list", action="store_true", help="list benchmark names and exit")

    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. `argv=None` means "read sys.argv", same as argparse's
    own default -- that's what lets the installed `quilldb` console script
    call this with no arguments at all.

    Returns:
        Process exit code: 0 on success, 1 if the file couldn't be read or
        parsed. Never lets FileNotFoundError/QuillDBError escape as a raw
        traceback -- both become a one-line message on stderr instead.
    """
    args = build_parser().parse_args(argv)

    if args.command == "bench":
        from quilldb.bench.run_all import main as bench_main

        return bench_main([*args.names] + (["--list"] if args.list else []))

    if args.command == "shell":
        from quilldb.shell import run_shell

        return run_shell(args.path)

    if not args.path.exists():
        print(f"quilldb: {args.path}: no such file", file=sys.stderr)
        return 1

    try:
        if args.command == "inspect":
            print(format_header(read_header(args.path)))
            return 0
        if args.command == "pages":
            return cmd_pages(args.path, args.page_range)
        if args.command == "btree":
            return cmd_btree(args.path, args.root, args.depth)
        if args.command == "validate":
            return cmd_validate(args.path)
    except (OSError, QuillDBError) as exc:
        print(f"quilldb: {exc}", file=sys.stderr)
        return 1

    raise AssertionError(f"unhandled command: {args.command!r}")


if __name__ == "__main__":
    sys.exit(main())
