"""`quilldb inspect FILE` -- print the 100-byte file header of a database
file: the same fields `sqlite3 FILE ".dbinfo"` reads from the same bytes.


Deliberately reads the header directly, not through Pager -- inspection
should work (and say why it can't) even on a file quilldb can't fully open,
e.g. one with an unsupported page size.
"""


import argparse
import sys
from dataclasses import fields
from pathlib import Path


from quilldb.constants import FILE_HEADER_SIZE
from quilldb.errors import QuillDBError
from quilldb.storage.header import FileHeader




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




def build_parser() -> argparse.ArgumentParser:
    """The `inspect` subcommand only, for now -- more subcommands are
    plausible future work, not required by week 1.
    """
    parser = argparse.ArgumentParser(prog="quilldb")
    subparsers = parser.add_subparsers(dest="command", required=True)


    inspect_parser = subparsers.add_parser("inspect", help="print a database file's header")
    inspect_parser.add_argument("path", type=Path)


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


    if args.command == "inspect":
        try:
            header = read_header(args.path)
        except (FileNotFoundError, QuillDBError) as exc:
            print(f"quilldb: {exc}", file=sys.stderr)
            return 1


        print(format_header(header))
        return 0


    # Unreachable while "inspect" is the only subparser -- argparse itself
    # rejects anything else before main() is ever called.
    raise AssertionError(f"unhandled command: {args.command!r}")




if __name__ == "__main__":
    sys.exit(main())



