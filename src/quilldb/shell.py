"""`quilldb shell FILE` -- an interactive SQL prompt.

    quilldb> SELECT id, email FROM users WHERE id < 3;
    id | email
    ---+----------------
    1  | ada@example.com
    2  | bob@example.com
    (2 rows)

A statement ends at `;`, so it can span lines (the prompt changes to `...>`
until it does). A line starting with `.` is a shell command, and a line
starting with `!` runs the rest in the system shell:

    .tables            list tables and indexes
    .schema [TABLE]    the CREATE statements
    .help              this text
    .quit              leave (so does Ctrl-D)
    !sqlite3 f.db "PRAGMA integrity_check"

`EXPLAIN` output is printed as-is, one plan line per line. Errors print and
the prompt carries on; a bad statement never ends the session.

`run_shell` takes its input and output as arguments so the tests can drive it
without a terminal.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO

import quilldb
from quilldb.codec.record import Value
from quilldb.errors import QuillDBError

PROMPT = "quilldb> "
CONTINUATION = "   ...> "
MAX_CELL_WIDTH = 40

HELP = """\
.tables            list tables and indexes
.schema [TABLE]    show CREATE statements
.help              show this text
.quit              leave the shell (Ctrl-D also works)
!COMMAND           run COMMAND in the system shell
A statement ends with ';' and may span several lines."""


def statement_complete(buffer: str) -> bool:
    """Has the user finished typing a statement?

    Called after every line with everything typed so far for the current
    statement. `True` sends it to the engine; `False` shows the `...>` prompt
    and keeps reading.

    A statement is complete when the last thing in the text that is not
    whitespace or a comment is a `;` sitting outside every quoted region. So
    a `;` inside `'a;b'`, inside a `"quoted identifier"`, or inside a comment
    does not end anything, and a string left open (`'abc`) keeps the prompt
    waiting for its closing quote, even if the line ends in `;`.
    """
    quote = ""  # the quote character we are inside, or ""
    in_line_comment = in_block_comment = False
    last_significant = ""
    i = 0
    while i < len(buffer):
        char, pair = buffer[i], buffer[i : i + 2]
        if in_line_comment:
            in_line_comment = char != "\n"
        elif in_block_comment:
            if pair == "*/":
                in_block_comment = False
                i += 1
        elif quote:
            if char == quote:
                if buffer[i + 1 : i + 2] == quote:  # a doubled quote is an escaped quote
                    i += 1
                else:
                    quote = ""
        elif pair == "--":
            in_line_comment = True
            i += 1
        elif pair == "/*":
            in_block_comment = True
            i += 1
        elif char in ("'", '"'):
            quote = char
            last_significant = char
        elif not char.isspace():
            last_significant = char
        i += 1
    return not (quote or in_block_comment) and last_significant == ";"


def format_value(value: Value) -> str:
    """One cell as text: NULL, hex for blobs, and long text cut with an ellipsis."""
    if value is None:
        return "NULL"
    if isinstance(value, bytes):
        text = "x'" + value.hex() + "'"
    else:
        text = str(value).replace("\n", "\\n")
    if len(text) > MAX_CELL_WIDTH:
        text = text[: MAX_CELL_WIDTH - 3] + "..."
    return text


def format_table(headers: Sequence[str], rows: Sequence[Sequence[Value]]) -> str:
    """Rows as an aligned table under their column names, plus a row count."""
    cells = [[format_value(v) for v in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in cells:
        widths = [max(w, len(c)) for w, c in zip(widths, row, strict=True)]
    lines = [" | ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True)).rstrip()]
    lines.append("-+-".join("-" * w for w in widths))
    lines.extend(" | ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)).rstrip() for row in cells)
    noun = "row" if len(rows) == 1 else "rows"
    lines.append(f"({len(rows)} {noun})")
    return "\n".join(lines)


def execute_and_format(conn: quilldb.Connection, sql: str) -> str:
    """Run one statement and return what the shell should print for it."""
    cursor = conn.execute(sql)
    if cursor.description is None:
        if cursor.rowcount <= 0:
            return "OK"
        return f"OK ({cursor.rowcount} row{'s' if cursor.rowcount != 1 else ''} affected)"
    rows = cursor.fetchall()
    if sql.lstrip()[:7].upper() == "EXPLAIN":
        # A plan is text, not a table: print the lines untouched.
        return "\n".join(str(row[0]) for row in rows)
    return format_table([column[0] for column in cursor.description], rows)


def dot_command(conn: quilldb.Connection, line: str, out: TextIO) -> bool:
    """Handle a `.command`. Returns False when the shell should exit."""
    parts = line.split()
    name, args = parts[0], parts[1:]
    if name in (".quit", ".exit"):
        return False
    if name == ".help":
        print(HELP, file=out)
    elif name == ".tables":
        for table in conn.catalog.list_tables():
            print(table.name, file=out)
            for index in conn.catalog.indexes_for(table.name):
                print(f"  {index.name} (index)", file=out)
    elif name == ".schema":
        shown = False
        for table in conn.catalog.list_tables():
            if args and table.name.casefold() != args[0].casefold():
                continue
            print(table.sql.rstrip(";") + ";", file=out)
            for index in conn.catalog.indexes_for(table.name):
                print(index.sql.rstrip(";") + ";", file=out)
            shown = True
        if args and not shown:
            print(f"no such table: {args[0]}", file=out)
    else:
        print(f"unknown command {name!r}; try .help", file=out)
    return True


def run_shell(
    path: Path,
    input_fn: Callable[[str], str] = input,
    out: TextIO | None = None,
) -> int:
    """The read-eval-print loop. Returns a process exit code."""
    out = out if out is not None else sys.stdout
    try:
        conn = quilldb.connect(path)
    except (OSError, QuillDBError) as exc:
        print(f"quilldb: {exc}", file=sys.stderr)
        return 1

    print(f"quilldb shell on {path}. Type .help for commands, .quit to leave.", file=out)
    buffer = ""
    try:
        while True:
            try:
                line = input_fn(CONTINUATION if buffer else PROMPT)
            except EOFError:
                print(file=out)
                break
            except KeyboardInterrupt:
                print("^C", file=out)
                buffer = ""
                continue

            if not buffer and line.strip().startswith("."):
                if not dot_command(conn, line.strip(), out):
                    break
                continue
            if not buffer and line.startswith("!"):
                subprocess.run(line[1:], shell=True, check=False)
                continue
            if not buffer and not line.strip():
                continue

            buffer = f"{buffer}\n{line}" if buffer else line
            if not statement_complete(buffer):
                continue
            sql, buffer = buffer.strip(), ""
            try:
                print(execute_and_format(conn, sql), file=out)
            except QuillDBError as exc:
                print(f"Error: {exc}", file=out)
    finally:
        conn.close()
    return 0
