"""Differential harness: does quilldb agree with real sqlite3?


Not a replacement for the unit tests -- when the two disagree here, a
focused unit test in exec/ or sql/ is what says WHICH layer is wrong; this
file only says THAT something disagrees. It deliberately excludes
quilldb's documented divergences (arithmetic on text, `WHERE '1'`, ...):
those are pinned on purpose by test_expressions.py/test_binder.py, and
differentially testing them would just fail on purpose, forever. This
harness is for catching ACCIDENTAL disagreement on paths both engines are
supposed to agree on.


Both engines run entirely in :memory: -- this is about answer agreement,
not durability (tests/integration/test_vertical_slice.py owns durability).
Rows are compared with plain equality, which is only safe because Week 3
has exactly one access path (SeqScan, rowid order, no ORDER BY); revisit
if a later week's planner can return rows in a different order.


Meant to grow, not stay at ~20: every week from here adds cases as new
syntax lands, so every feature gets checked against a real database
for free.
"""


import sqlite3
from typing import NamedTuple

import pytest

import quilldb
from quilldb.codec.record import Value

_USERS = "CREATE TABLE users (id INTEGER, name TEXT, age INTEGER)"




class Case(NamedTuple):
    name: str
    script: list[tuple[str, tuple[Value, ...]]]
    query: tuple[str, tuple[Value, ...]]




CASES: list[Case] = [
    Case(
        "empty_table",
        [(_USERS, ())],
        ("SELECT * FROM users", ()),
    ),
    Case(
        "integer_value",
        [(_USERS, ()), ("INSERT INTO users VALUES (?, ?, ?)", (1, "ada", 36))],
        ("SELECT id FROM users", ()),
    ),
    Case(
        "text_value",
        [(_USERS, ()), ("INSERT INTO users VALUES (?, ?, ?)", (1, "ada", 36))],
        ("SELECT name FROM users", ()),
    ),
    Case(
        "real_value",
        [
            ("CREATE TABLE t (x REAL)", ()),
            ("INSERT INTO t VALUES (?)", (2.5,)),
        ],
        ("SELECT x FROM t", ()),
    ),
    Case(
        "blob_value",
        [
            ("CREATE TABLE t (x BLOB)", ()),
            ("INSERT INTO t VALUES (?)", (b"\x00\x01\xff",)),
        ],
        ("SELECT x FROM t", ()),
    ),
    Case(
        "null_value",
        [(_USERS, ()), ("INSERT INTO users VALUES (?, ?, ?)", (1, "ada", None))],
        ("SELECT age FROM users", ()),
    ),
    Case(
        "null_in_where_predicate_rejects_the_row",
        [
            (_USERS, ()),
            ("INSERT INTO users VALUES (?, ?, ?)", (1, "ada", 36)),
            ("INSERT INTO users VALUES (?, ?, ?)", (2, "linus", None)),
        ],
        ("SELECT name FROM users WHERE age > 30", ()),
    ),
    Case(
        "not_of_a_null_predicate_also_rejects_the_row",
        [
            (_USERS, ()),
            ("INSERT INTO users VALUES (?, ?, ?)", (1, "ada", 36)),
            ("INSERT INTO users VALUES (?, ?, ?)", (2, "linus", None)),
        ],
        ("SELECT name FROM users WHERE NOT (age > 30)", ()),
    ),
    Case(
        "and_binds_tighter_than_or",
        [
            ("CREATE TABLE t (a INTEGER, b INTEGER, c INTEGER)", ()),
            ("INSERT INTO t VALUES (?, ?, ?)", (1, 0, 0)),
            ("INSERT INTO t VALUES (?, ?, ?)", (0, 1, 1)),
            ("INSERT INTO t VALUES (?, ?, ?)", (0, 0, 0)),
        ],
        ("SELECT a FROM t WHERE a = 1 OR b = 1 AND c = 0", ()),
    ),
    Case(
        "arithmetic_binds_tighter_than_comparison",
        [(_USERS, ()), ("INSERT INTO users VALUES (?, ?, ?)", (1, "ada", 36))],
        ("SELECT name FROM users WHERE age + 1 > 36", ()),
    ),
    Case(
        "escaped_single_quote_in_a_string_literal",
        [
            (_USERS, ()),
            ("INSERT INTO users VALUES (1, 'O''Brien', 40)", ()),
        ],
        ("SELECT name FROM users WHERE name = 'O''Brien'", ()),
    ),
    Case(
        "positional_parameters_bind_in_order",
        [
            (_USERS, ()),
            ("INSERT INTO users VALUES (?, ?, ?)", (1, "ada", 20)),
            ("INSERT INTO users VALUES (?, ?, ?)", (2, "bob", 41)),
        ],
        ("SELECT name FROM users WHERE age > ? AND age < ?", (10, 30)),
    ),
    Case(
        "like_percent_wildcard",
        [
            (_USERS, ()),
            ("INSERT INTO users VALUES (?, ?, ?)", (1, "ada", 36)),
            ("INSERT INTO users VALUES (?, ?, ?)", (2, "bob", 41)),
        ],
        ("SELECT name FROM users WHERE name LIKE 'a%'", ()),
    ),
    Case(
        "like_underscore_wildcard",
        [
            (_USERS, ()),
            ("INSERT INTO users VALUES (?, ?, ?)", (1, "ada", 36)),
            ("INSERT INTO users VALUES (?, ?, ?)", (2, "amy", 20)),
        ],
        ("SELECT name FROM users WHERE name LIKE 'a__'", ()),
    ),
    Case(
        "like_is_ascii_case_insensitive",
        [(_USERS, ()), ("INSERT INTO users VALUES (?, ?, ?)", (1, "ADA", 36))],
        ("SELECT name FROM users WHERE name LIKE 'ada'", ()),
    ),
    Case(
        "projection_order_can_differ_from_table_order",
        [(_USERS, ()), ("INSERT INTO users VALUES (?, ?, ?)", (1, "ada", 36))],
        ("SELECT age, name, id FROM users", ()),
    ),
    Case(
        "projection_of_a_computed_expression",
        [(_USERS, ()), ("INSERT INTO users VALUES (?, ?, ?)", (1, "ada", 36))],
        ("SELECT age + 1 FROM users", ()),
    ),
    Case(
        "select_star_expands_every_column",
        [(_USERS, ()), ("INSERT INTO users VALUES (?, ?, ?)", (1, "ada", 36))],
        ("SELECT * FROM users", ()),
    ),
    Case(
        "multiple_rows_are_scanned_in_rowid_order",
        [
            (_USERS, ()),
            ("INSERT INTO users VALUES (?, ?, ?)", (1, "ada", 36)),
            ("INSERT INTO users VALUES (?, ?, ?)", (2, "bob", 41)),
            ("INSERT INTO users VALUES (?, ?, ?)", (3, "amy", 20)),
        ],
        ("SELECT name FROM users", ()),
    ),
    Case(
        "comparisons_return_an_integer_not_sqlites_bool",
        [(_USERS, ()), ("INSERT INTO users VALUES (?, ?, ?)", (1, "ada", 36))],
        ("SELECT id = 1 FROM users", ()),
    ),
]




@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_matches_sqlite(case: Case) -> None:
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