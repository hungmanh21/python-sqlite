"""NestedLoopJoin tests, end to end through quilldb.connect() (chapter 17,
week7-query-processing.md §41).

Going through Connection rather than constructing NestedLoopJoin directly:
the interesting behavior here is the INTERACTION between the join, the
planner's choice of access path per table, and resolve_layout's rewrite of
every BoundColumn -- none of which a hand-built operator tree would
exercise honestly.
"""

import re
from pathlib import Path

import quilldb


def _setup(db: quilldb.Connection) -> None:
    db.execute("CREATE TABLE users (id INTEGER, name TEXT)")
    db.execute("CREATE TABLE orders (id INTEGER, user_id INTEGER, total INTEGER)")
    db.execute("CREATE INDEX ix_users_id ON users (id)")
    db.execute("CREATE INDEX ix_orders_user ON orders (user_id)")


def _outstanding_pins(connection: quilldb.Connection) -> int:
    return sum(entry.pin_count for entry in connection.pool._cache.values())


# =====================================================================
# INNER JOIN
# =====================================================================


def test_inner_join_matches_rows_on_the_join_key(tmp_path: Path) -> None:
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        _setup(db)
        db.execute("INSERT INTO users VALUES (1, 'ada')")
        db.execute("INSERT INTO users VALUES (2, 'bob')")
        db.execute("INSERT INTO orders VALUES (10, 1, 100)")
        db.execute("INSERT INTO orders VALUES (11, 1, 50)")
        db.execute("INSERT INTO orders VALUES (12, 2, 200)")
        db.execute("ANALYZE")

        rows = db.execute(
            "SELECT u.name, o.total FROM users u JOIN orders o ON u.id = o.user_id"
        ).fetchall()
        assert sorted(rows) == [("ada", 50), ("ada", 100), ("bob", 200)]


def test_inner_join_drops_a_user_with_no_orders(tmp_path: Path) -> None:
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        _setup(db)
        db.execute("INSERT INTO users VALUES (1, 'ada')")
        db.execute("INSERT INTO users VALUES (2, 'carol')")  # no orders
        db.execute("INSERT INTO orders VALUES (10, 1, 100)")
        db.execute("ANALYZE")

        rows = db.execute(
            "SELECT u.name FROM users u JOIN orders o ON u.id = o.user_id"
        ).fetchall()
        assert rows == [("ada",)]


def test_comma_join_is_the_full_cross_product(tmp_path: Path) -> None:
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        _setup(db)
        db.execute("INSERT INTO users VALUES (1, 'ada')")
        db.execute("INSERT INTO users VALUES (2, 'bob')")
        db.execute("INSERT INTO orders VALUES (10, 1, 100)")
        db.execute("INSERT INTO orders VALUES (11, 2, 200)")
        db.execute("INSERT INTO orders VALUES (12, 2, 300)")
        db.execute("ANALYZE")

        rows = db.execute("SELECT * FROM users, orders").fetchall()
        assert len(rows) == 2 * 3


def test_null_join_key_never_matches(tmp_path: Path) -> None:
    """Two rows both with a NULL join key must NOT join to each other --
    `on()` returning NULL means skip, never a match (§41 trap #4)."""
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        _setup(db)
        db.execute("INSERT INTO users VALUES (1, 'ada')")
        db.execute("INSERT INTO orders VALUES (10, NULL, 999)")
        db.execute("ANALYZE")

        rows = db.execute(
            "SELECT * FROM users u JOIN orders o ON u.id = o.user_id"
        ).fetchall()
        assert rows == []


# =====================================================================
# LEFT JOIN
# =====================================================================


def test_left_join_emits_unmatched_outer_rows(tmp_path: Path) -> None:
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        _setup(db)
        db.execute("INSERT INTO users VALUES (1, 'ada')")
        db.execute("INSERT INTO users VALUES (2, 'carol')")  # no orders
        db.execute("INSERT INTO orders VALUES (10, 1, 100)")
        db.execute("ANALYZE")

        rows = db.execute(
            "SELECT u.name, o.total FROM users u LEFT JOIN orders o ON u.id = o.user_id"
        ).fetchall()
        assert sorted(rows, key=str) == [("ada", 100), ("carol", None)]


def test_left_join_over_an_entirely_empty_inner_table_null_extends(tmp_path: Path) -> None:
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        _setup(db)
        db.execute("INSERT INTO users VALUES (1, 'ada')")
        db.execute("ANALYZE")

        rows = db.execute(
            "SELECT u.name, o.total FROM users u LEFT JOIN orders o ON u.id = o.user_id"
        ).fetchall()
        assert rows == [("ada", None)]


def test_where_on_inner_column_after_left_join_drops_null_rows(tmp_path: Path) -> None:
    """Documents chapter 17 §17.8's trap as intended behaviour: a WHERE
    clause naming the NULLABLE side runs AFTER NULL-extension, so it
    correctly drops an outer row that never matched (week7-query-
    processing.md §43's pushdown table -- this conjunct must NOT be pushed
    into the inner table's own scan, or it would never get the chance).
    """
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        _setup(db)
        db.execute("INSERT INTO users VALUES (1, 'ada')")
        db.execute("INSERT INTO users VALUES (2, 'carol')")  # no orders
        db.execute("INSERT INTO orders VALUES (10, 1, 100)")
        db.execute("ANALYZE")

        rows = db.execute(
            "SELECT u.name FROM users u LEFT JOIN orders o ON u.id = o.user_id WHERE o.total > 0"
        ).fetchall()
        assert rows == [("ada",)]


def test_where_on_outer_column_after_left_join_keeps_unmatched_rows(tmp_path: Path) -> None:
    """The mirror image: a WHERE clause on the PRESERVED side is always
    safe to apply, and must not accidentally suppress an outer row that
    never matched anything."""
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        _setup(db)
        db.execute("INSERT INTO users VALUES (1, 'ada')")
        db.execute("INSERT INTO users VALUES (2, 'carol')")  # no orders
        db.execute("INSERT INTO orders VALUES (10, 1, 100)")
        db.execute("ANALYZE")

        rows = db.execute(
            "SELECT u.name FROM users u LEFT JOIN orders o ON u.id = o.user_id WHERE u.name = 'carol'"
        ).fetchall()
        assert rows == [("carol",)]


# =====================================================================
# Resource discipline (§41 traps #1/#2)
# =====================================================================


def test_inner_cursor_is_closed_once_per_outer_row(tmp_path: Path) -> None:
    """No pin survives the join: the inner is re-opened |outer| times, and
    every open()/close() pair must fully release its pins, or a large join
    would make pages permanently unevictable (§41 trap #1).
    """
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        _setup(db)
        for i in range(5):
            db.execute("INSERT INTO users VALUES (?, ?)", (i, f"u{i}"))
            db.execute("INSERT INTO orders VALUES (?, ?, ?)", (i, i, i * 10))
        db.execute("ANALYZE")

        rows = db.execute(
            "SELECT u.name, o.total FROM users u JOIN orders o ON u.id = o.user_id"
        ).fetchall()
        assert len(rows) == 5
        assert _outstanding_pins(db) == 0


def test_matched_flag_resets_per_outer_row_not_per_open(tmp_path: Path) -> None:
    """A user with two orders, followed by a user with none: the second
    user's LEFT JOIN NULL-extension must not be suppressed by the FIRST
    user's match (§41 trap #2 -- `matched` belongs to the outer row, not to
    the whole operator).
    """
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        _setup(db)
        db.execute("INSERT INTO users VALUES (1, 'ada')")
        db.execute("INSERT INTO users VALUES (2, 'carol')")
        db.execute("INSERT INTO orders VALUES (10, 1, 100)")
        db.execute("INSERT INTO orders VALUES (11, 1, 50)")
        db.execute("ANALYZE")

        rows = db.execute(
            "SELECT u.name, o.total FROM users u LEFT JOIN orders o ON u.id = o.user_id"
        ).fetchall()
        assert sorted(rows, key=str) == [("ada", 100), ("ada", 50), ("carol", None)]


# =====================================================================
# ORDER BY / LIMIT / OFFSET on a joined SELECT (session 7, §43-44)
# =====================================================================


def _seed_orders(db: quilldb.Connection) -> None:
    _setup(db)
    db.execute("INSERT INTO users VALUES (1, 'ada')")
    db.execute("INSERT INTO users VALUES (2, 'bob')")
    db.execute("INSERT INTO orders VALUES (10, 1, 100)")
    db.execute("INSERT INTO orders VALUES (11, 1, 50)")
    db.execute("INSERT INTO orders VALUES (12, 2, 200)")
    db.execute("ANALYZE")


def test_order_by_on_a_join_sorts_the_joined_rows(tmp_path: Path) -> None:
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        _seed_orders(db)
        rows = db.execute(
            "SELECT u.name, o.total FROM users u JOIN orders o ON u.id = o.user_id ORDER BY o.total DESC"
        ).fetchall()
        assert rows == [("bob", 200), ("ada", 100), ("ada", 50)]


def test_order_by_ordinal_on_a_join(tmp_path: Path) -> None:
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        _seed_orders(db)
        rows = db.execute(
            "SELECT u.name, o.total FROM users u JOIN orders o ON u.id = o.user_id ORDER BY 2"
        ).fetchall()
        assert rows == [("ada", 50), ("ada", 100), ("bob", 200)]


def test_order_by_a_column_not_in_the_select_list_on_a_join(tmp_path: Path) -> None:
    """Exercises the hidden-column strip for a join: `u.name` never
    appears in the output row, only in the row Sort itself sorts on."""
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        _seed_orders(db)
        rows = db.execute(
            "SELECT o.total FROM users u JOIN orders o ON u.id = o.user_id ORDER BY u.name, o.total"
        ).fetchall()
        assert rows == [(50,), (100,), (200,)]


def test_limit_on_a_join(tmp_path: Path) -> None:
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        _seed_orders(db)
        rows = db.execute(
            "SELECT o.total FROM users u JOIN orders o ON u.id = o.user_id ORDER BY o.total LIMIT 2"
        ).fetchall()
        assert rows == [(50,), (100,)]


def test_offset_on_a_join(tmp_path: Path) -> None:
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        _seed_orders(db)
        rows = db.execute(
            "SELECT o.total FROM users u JOIN orders o ON u.id = o.user_id ORDER BY o.total LIMIT 2 OFFSET 1"
        ).fetchall()
        assert rows == [(100,), (200,)]


def test_left_join_order_by_still_never_reorders_the_join_itself(tmp_path: Path) -> None:
    """A LEFT JOIN's own table order is untouched by sort-cost-aware
    ranking, same guarantee test_join_planner.py already proves for the
    no-ORDER-BY case -- ORDER BY only ever adds a Sort on top, never
    reorders which side drives."""
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        _seed_orders(db)
        db.execute("INSERT INTO users VALUES (3, 'carol')")  # no orders
        rows = db.execute(
            "SELECT u.name, o.total FROM users u LEFT JOIN orders o ON u.id = o.user_id ORDER BY u.name"
        ).fetchall()
        assert rows == [("ada", 100), ("ada", 50), ("bob", 200), ("carol", None)]


# =====================================================================
# EXPLAIN of a joined SELECT (session 7)
# =====================================================================


def test_explain_of_a_join_no_longer_raises(tmp_path: Path) -> None:
    """On these few rows a SeqScan legitimately beats an index descent for
    both tables (assign_cost's own honest arithmetic, chapter 12 SS12.6) --
    EXPLAIN just has to report whatever build_operator() actually built,
    not force an index. test_join_planner.py's own page-read test is where
    "IndexScan wins on a bigger table" gets proven.
    """
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        _seed_orders(db)
        rows = db.execute(
            "EXPLAIN SELECT u.name, o.total FROM users u JOIN orders o ON u.id = o.user_id"
        ).fetchall()
        # Each SeqScan carries its planner annotation now (week 8); the shape is what this test pins.
        stripped = re.sub(r" est_rows=\d+ startup=[\d.]+ cost=[\d.]+", "", rows[0][0])
        assert stripped == "Project\n└─ NestedLoopJoin\n   └─ SeqScan users\n   └─ SeqScan orders"
