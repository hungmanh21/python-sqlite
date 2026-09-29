"""PlanCache tests (plan/cache.py, week7-query-processing.md §43's "The
plan cache", session 7).

The unit-level section exercises PlanCache directly: get/put and
schema_cookie-based invalidation, independent of Connection/build_operator.
The end-to-end section is the two claims the roadmap's own "done when"
names explicitly: a cached plan re-run with new parameters returns the
NEW parameters' rows (never the first call's, baked in), and the cache
dies on DDL/ANALYZE through the schema cookie.
"""

from pathlib import Path

import pytest

import quilldb
from quilldb.plan.cache import PlanCache
from quilldb.plan.planner import PlanShape

# =====================================================================
# PlanCache, directly
# =====================================================================


def test_get_returns_none_when_nothing_cached() -> None:
    cache = PlanCache()
    assert cache.get("SELECT * FROM t", schema_cookie=0) is None


def test_put_then_get_with_the_same_cookie_returns_the_shape() -> None:
    cache = PlanCache()
    shape = PlanShape("index_scan", "ix_t_a", False)
    cache.put("SELECT * FROM t WHERE a = ?", schema_cookie=1, shape=shape)
    assert cache.get("SELECT * FROM t WHERE a = ?", schema_cookie=1) == shape


def test_get_with_a_different_cookie_returns_none() -> None:
    """The whole invalidation mechanism: a DDL/ANALYZE bumps the schema
    cookie, and a stale entry just stops matching -- nothing has to sweep
    it out proactively."""
    cache = PlanCache()
    cache.put("SELECT * FROM t", schema_cookie=1, shape=PlanShape("seq_scan", None, False))
    assert cache.get("SELECT * FROM t", schema_cookie=2) is None


def test_put_overwrites_the_previous_entry_for_the_same_sql() -> None:
    cache = PlanCache()
    cache.put("SELECT * FROM t", schema_cookie=1, shape=PlanShape("seq_scan", None, False))
    cache.put("SELECT * FROM t", schema_cookie=2, shape=PlanShape("index_scan", "ix_t_a", False))
    assert cache.get("SELECT * FROM t", schema_cookie=2) == PlanShape("index_scan", "ix_t_a", False)


def test_different_sql_text_is_a_different_key() -> None:
    cache = PlanCache()
    cache.put("SELECT a FROM t", schema_cookie=1, shape=PlanShape("seq_scan", None, False))
    assert cache.get("SELECT b FROM t", schema_cookie=1) is None


# =====================================================================
# End to end, through quilldb.connect()
# =====================================================================


def _seed(db: quilldb.Connection) -> None:
    db.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    db.execute("CREATE INDEX ix_t_id ON t (id)")
    for i in range(20):
        db.execute("INSERT INTO t VALUES (?, ?)", (i, f"row{i}"))
    db.execute("ANALYZE")


_BIG_N = 2000
_PADDING = "x" * 500


def _seed_big(db: quilldb.Connection, *, with_index: bool) -> None:
    """A table large enough that assign_cost's own honest arithmetic
    (chapter 12 SS12.6) actually prefers an index seek over a SeqScan --
    the same reasoning test_join_planner.py's flagship page-read test
    uses a wide padding column and a few thousand rows for.
    """
    db.execute("CREATE TABLE t (id INTEGER, name TEXT, padding TEXT)")
    if with_index:
        db.execute("CREATE INDEX ix_t_id ON t (id)")
    for i in range(_BIG_N):
        db.execute("INSERT INTO t VALUES (?, ?, ?)", (i, f"row{i}", _PADDING))
    db.execute("ANALYZE")


def test_repeated_query_reuses_the_cached_plan_with_fresh_parameter_values(tmp_path: Path) -> None:
    """The trap the roadmap's own plan-cache section names explicitly:
    caching the bound VALUES would answer every later call with the
    FIRST call's parameter. Run the same SQL text twice with different
    `?` values and confirm each call gets its own correct row.
    """
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        _seed(db)
        sql = "SELECT name FROM t WHERE id = ?"
        first = db.execute(sql, (3,)).fetchall()
        second = db.execute(sql, (7,)).fetchall()
        assert first == [("row3",)]
        assert second == [("row7",)]
        # Prove it was actually a cache HIT the second time, not two
        # independent fresh plans that coincidentally agree.
        assert db.db.plan_cache.get(sql, db.pager.schema_cookie) is not None


@pytest.mark.slow
def test_plan_cache_records_the_chosen_shape_after_the_first_call(tmp_path: Path) -> None:
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        _seed_big(db, with_index=True)
        sql = "SELECT name FROM t WHERE id = ?"
        assert db.db.plan_cache.get(sql, db.pager.schema_cookie) is None
        db.execute(sql, (3,)).fetchall()
        shape = db.db.plan_cache.get(sql, db.pager.schema_cookie)
        assert shape is not None
        assert shape.kind == "index_scan"
        assert shape.index_name == "ix_t_id"


@pytest.mark.slow
def test_plan_cache_dies_on_ddl(tmp_path: Path) -> None:
    """A CREATE INDEX bumps the schema cookie (catalog/catalog.py) --
    the cached shape from before the index existed must not survive it.
    """
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        _seed_big(db, with_index=False)

        sql = "SELECT name FROM t WHERE id = ?"
        before = db.execute(sql, (3,)).fetchall()
        assert before == [("row3",)]
        cookie_before = db.pager.schema_cookie
        shape_before = db.db.plan_cache.get(sql, cookie_before)
        assert shape_before is not None
        assert shape_before.kind == "seq_scan"  # no index exists yet -- nothing else to choose

        db.execute("CREATE INDEX ix_t_id ON t (id)")
        assert db.pager.schema_cookie != cookie_before
        assert db.db.plan_cache.get(sql, cookie_before) is not None  # the OLD entry is still there...
        assert db.db.plan_cache.get(sql, db.pager.schema_cookie) is None  # ...but unreachable at the NEW cookie

        after = db.execute(sql, (3,)).fetchall()
        assert after == [("row3",)]  # still correct, replanned from scratch
        shape = db.db.plan_cache.get(sql, db.pager.schema_cookie)
        assert shape is not None
        assert shape.index_name == "ix_t_id"  # picked up the index this query never saw before


def test_plan_cache_dies_on_analyze(tmp_path: Path) -> None:
    with quilldb.connect(tmp_path / "db.sqlite") as db:
        _seed(db)
        sql = "SELECT name FROM t WHERE id = ?"
        db.execute(sql, (3,)).fetchall()
        cookie_before = db.pager.schema_cookie

        db.execute("ANALYZE")
        assert db.pager.schema_cookie != cookie_before
        assert db.db.plan_cache.get(sql, db.pager.schema_cookie) is None

        rows = db.execute(sql, (3,)).fetchall()
        assert rows == [("row3",)]
