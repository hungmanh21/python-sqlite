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
Rows are compared with plain equality: safe for a hand-written case as
long as its query result is unambiguously ordered (either exactly one row,
or an explicit `ORDER BY` with a deterministic tiebreak) -- with a
multi-access-path planner (week 7+), a query with NO `ORDER BY` has no
guaranteed row order in EITHER engine, so a case without one is only safe
here by accident. test_null_matrix.py's generator (session 8) is why this
matters starting week 7: every generated query pins its own order rather
than relying on either engine's incidental scan order.


`Case` and the actual comparison (`assert_matches_sqlite`) live in
`_util.py` (imported bare, `from _util import ...`, not dotted -- this
directory has no `__init__.py`, matching the rest of `src/tests/`, and
pytest/mypy both resolve a same-directory import that way) so
test_null_matrix.py's generated cases run through the exact same
real-sqlite3-vs-quilldb comparison as these hand-written ones, instead of
a second reimplementation that could quietly drift from this one.


Meant to grow, not stay at ~20: every week from here adds cases as new
syntax lands, so every feature gets checked against a real database
for free.
"""


import pytest
from _util import Case, assert_matches_sqlite

_USERS = "CREATE TABLE users (id INTEGER, name TEXT, age INTEGER)"




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
    assert_matches_sqlite(case)