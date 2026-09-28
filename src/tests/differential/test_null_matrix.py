"""Session 8 (week7-query-processing.md row 795, "Differential test
generators over the NULL matrix"): instead of hand-writing one Case at a
time like test_sqlite.py, GENERATE a combinatorial set of join/aggregate/
ORDER BY queries over rows that name every NULL-vs-non-NULL shape sessions
2-7 (joins, aggregates, GROUP BY, ORDER BY) need to agree with real sqlite3
about, and run every one of them through the exact same comparison
test_sqlite.py's hand-written cases use.


"Session 8 will find bugs in sessions 2-7. That's the point" (the
roadmap's own words) -- this file's job is producing enough DIFFERENT
combinations of NULL placement x join type x aggregate x ordering that an
accidental disagreement introduced anywhere in weeks 2-7 has somewhere to
surface, not inventing new semantics of its own.


Two families, not one: `bind_join_select` still rejects GROUP BY/HAVING/
aggregates combined with a JOIN (sql/binder.py's own documented, on-purpose
gap -- not attempted here, since generating that combination would just
fail on quilldb by design and prove nothing about an ACCIDENTAL
disagreement). So the "join" family below varies WHERE/ORDER BY/LIMIT over
a two-table NULL-key matrix, and the "aggregate" family separately varies
GROUP BY/HAVING/aggregate functions over a single table's own NULL values
-- together, "join/aggregate queries" the week's definition of done asks
for, just never a query that is both at once.
"""


import itertools

import pytest
from _util import Case, assert_matches_sqlite

from quilldb.codec.record import Value

# =====================================================================
# Shared fixture data -- one seed script every generated case reuses.
#
# `left_t`/`right_t` are joined on `key`; the rows below name every
# NULL-key shape a join needs to agree with real sqlite3 about:
#   id=1/id=2  -- a normal match (id=2's PARTNER row has a NULL `val`,
#                 covering a matched-but-NULL-payload row)
#   id=3       -- left.key IS NULL: must match NOTHING, including
#                 right's own NULL-key row (two NULLs never match)
#   id=4       -- left.val IS NULL, but its key (4) matches nothing on
#                 the right at all (right has no key=4)
#   id=5       -- key=5 matches nothing on the right either (right's
#                 remaining row uses key=6)
# right_t's own id=3 (key NULL) and id=4 (key=6) are the mirror image:
# a NULL key and a key with no left-side match.
# =====================================================================

_LEFT_T = "CREATE TABLE left_t (id INTEGER, key INTEGER, val INTEGER)"
_RIGHT_T = "CREATE TABLE right_t (id INTEGER, key INTEGER, val INTEGER)"

_LEFT_ROWS: list[tuple[Value, ...]] = [
    (1, 1, 10),
    (2, 2, 20),
    (3, None, 30),
    (4, 4, None),
    (5, 5, 50),
]

_RIGHT_ROWS: list[tuple[Value, ...]] = [
    (1, 1, 100),
    (2, 2, None),
    (3, None, 300),
    (4, 6, 400),
]


def _seed_script() -> list[tuple[str, tuple[Value, ...]]]:
    script: list[tuple[str, tuple[Value, ...]]] = [(_LEFT_T, ()), (_RIGHT_T, ())]
    script += [("INSERT INTO left_t VALUES (?, ?, ?)", row) for row in _LEFT_ROWS]
    script += [("INSERT INTO right_t VALUES (?, ?, ?)", row) for row in _RIGHT_ROWS]
    return script


# =====================================================================
# Family 1: JOIN x WHERE-predicate x ORDER BY x LIMIT/OFFSET
# =====================================================================

_JOIN_TYPES = ["INNER", "LEFT"]

# Each predicate exercises three-valued NULL logic somewhere different:
# a plain equality, IS [NOT] NULL on either side of the join, a numeric
# comparison against a possibly-NULL value, and one compound predicate
# combining two of those. `right_key_is_null`/`right_val_is_null` are the
# ones that only ever match anything once NULL-extension (LEFT JOIN) is in
# play -- an INNER JOIN can never produce a NULL from the "required" side.
_JOIN_PREDICATES: list[tuple[str, str | None]] = [
    ("no_predicate", None),
    ("left_key_eq_1", "left_t.key = 1"),
    ("left_key_eq_5", "left_t.key = 5"),
    ("left_key_is_null", "left_t.key IS NULL"),
    ("right_key_is_null", "right_t.key IS NULL"),
    ("right_key_is_not_null", "right_t.key IS NOT NULL"),
    ("left_val_gt_15", "left_t.val > 15"),
    ("left_val_is_null", "left_t.val IS NULL"),
    ("right_val_is_null", "right_t.val IS NULL"),
    ("left_val_gt_15_and_right_val_not_null", "left_t.val > 15 AND right_t.val IS NOT NULL"),
]

_ORDER_DIRS = ["ASC", "DESC"]

_LIMIT_VARIANTS: list[tuple[str, str]] = [
    ("no_limit", ""),
    ("limit_2", "LIMIT 2"),
    ("limit_2_offset_1", "LIMIT 2 OFFSET 1"),
]


def _join_cases() -> list[Case]:
    script = _seed_script()
    cases = []
    combinations = itertools.product(
        _JOIN_TYPES, _JOIN_PREDICATES, _ORDER_DIRS, _ORDER_DIRS, _LIMIT_VARIANTS
    )
    for join_type, (predicate_name, predicate_sql), order1, order2, (limit_name, limit_sql) in combinations:
        where_clause = f" WHERE {predicate_sql}" if predicate_sql else ""
        # `left_t.id` alone already pins every row uniquely (each left row
        # matches at most one right row in this fixture), but a second
        # ORDER BY key still exercises multi-key ASC/DESC binding for free.
        query = (
            f"SELECT left_t.id, right_t.id, right_t.val "
            f"FROM left_t {join_type} JOIN right_t ON left_t.key = right_t.key"
            f"{where_clause} "
            f"ORDER BY left_t.id {order1}, right_t.id {order2}"
            f"{' ' + limit_sql if limit_sql else ''}"
        )
        name = (
            f"join__{join_type.lower()}__{predicate_name}"
            f"__order_{order1.lower()}_{order2.lower()}__{limit_name}"
        )
        cases.append(Case(name, script, (query, ())))
    return cases


# =====================================================================
# Family 2: single-table aggregates x GROUP BY x HAVING x ORDER BY x LIMIT
# =====================================================================

_AGG_TABLES = ["left_t", "right_t"]

_HAVING_VARIANTS: list[tuple[str, str]] = [
    ("no_having", ""),
    ("having_count_gt_1", "HAVING COUNT(*) > 1"),
]


def _aggregate_no_group_by_cases() -> list[Case]:
    """No GROUP BY collapses to exactly one output row -- no ORDER BY
    tiebreak needed, per assert_matches_sqlite's own single-row exception.
    """
    script = _seed_script()
    return [
        Case(
            f"aggregate_no_group_by__{table}",
            script,
            (f"SELECT COUNT(*), COUNT(val), SUM(val), AVG(val) FROM {table}", ()),
        )
        for table in _AGG_TABLES
    ]


def _aggregate_group_by_cases() -> list[Case]:
    """GROUP BY key: `key` itself is unique per output row (NULL included,
    per "all NULL rows land in one group"), so `ORDER BY key` alone is
    already a full tiebreak.
    """
    script = _seed_script()
    cases = []
    combinations = itertools.product(_AGG_TABLES, _ORDER_DIRS, _HAVING_VARIANTS, _LIMIT_VARIANTS)
    for table, order_dir, (having_name, having_sql), (limit_name, limit_sql) in combinations:
        query = (
            f"SELECT key, COUNT(*), COUNT(val), SUM(val), AVG(val) FROM {table} GROUP BY key"
            f"{' ' + having_sql if having_sql else ''} "
            f"ORDER BY key {order_dir}"
            f"{' ' + limit_sql if limit_sql else ''}"
        )
        name = f"aggregate_group_by__{table}__order_{order_dir.lower()}__{having_name}__{limit_name}"
        cases.append(Case(name, script, (query, ())))
    return cases


def generate_null_matrix_cases() -> list[Case]:
    """Every case from both families above, combined -- see this module's
    own docstring for why they stay two separate families rather than one
    join-and-aggregate cross product.
    """
    return _join_cases() + _aggregate_no_group_by_cases() + _aggregate_group_by_cases()


def test_generate_null_matrix_cases_reaches_a_few_hundred_unique_cases() -> None:
    """The roadmap's own "done when": "green across a few hundred
    generated queries". Checked here as a lower bound plus a uniqueness
    check on `Case.name` (pytest's `ids=` silently disambiguates
    duplicates by appending a counter, which would hide a generator bug
    that reuses the same name for two different combinations).
    """
    cases = generate_null_matrix_cases()
    assert len(cases) >= 200
    names = [case.name for case in cases]
    assert len(names) == len(set(names))


_CASES = generate_null_matrix_cases()


@pytest.mark.parametrize("case", _CASES, ids=[c.name for c in _CASES])
def test_matches_sqlite_over_the_null_matrix(case: Case) -> None:
    assert_matches_sqlite(case)
