"""Shared by every differential test file: the `Case` shape and the actual
real-sqlite3-vs-quilldb comparison, factored out of test_sqlite.py in
session 8 so test_null_matrix.py's generated cases run through the exact
same comparison as test_sqlite.py's hand-written ones.


Not itself a test file (no `test_` prefix) -- pytest won't try to collect
it, and it can be imported normally as `tests.differential._util`.
"""


import sqlite3
from typing import NamedTuple

import quilldb
from quilldb.codec.record import Value


class Case(NamedTuple):
    name: str
    script: list[tuple[str, tuple[Value, ...]]]
    query: tuple[str, tuple[Value, ...]]


def assert_matches_sqlite(case: Case) -> None:
    """Run `case.script` then `case.query` against both a real sqlite3
    :memory: connection and a quilldb :memory: connection, and assert they
    agree on column names and on every row, in order.


    Row order is compared literally (plain `==` on `fetchall()`) -- see
    test_sqlite.py's own module docstring for why a case's query must be
    unambiguously ordered (a single row, or an explicit `ORDER BY` with a
    deterministic tiebreak) for that comparison to mean anything.
    """
    sqlite_conn = sqlite3.connect(":memory:")
    quill_conn = quilldb.connect(":memory:")
    try:
        for sql, params in case.script:
            sqlite_conn.execute(sql, params)
            quill_conn.execute(sql, params)

        query_sql, query_params = case.query
        sqlite_cursor = sqlite_conn.execute(query_sql, query_params)
        quill_cursor = quill_conn.execute(query_sql, query_params)

        sqlite_description = sqlite_cursor.description
        quill_description = quill_cursor.description
        assert sqlite_description is not None
        assert quill_description is not None
        assert [c[0] for c in sqlite_description] == [c[0] for c in quill_description]

        assert sqlite_cursor.fetchall() == quill_cursor.fetchall()
    finally:
        sqlite_conn.close()
        quill_conn.close()
