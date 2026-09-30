"""Deleting rows must never leave a file real sqlite3 refuses to read.

NOTES.md B8-1: `DELETE ... WHERE id <= 59` on a two-level table left an
interior page with zero cells, which sqlite3 reports as "database disk image
is malformed" -- a hard parse error, so the check has to be a real sqlite3
open, not quilldb's own validator alone.
"""

import random
import sqlite3

import pytest

import quilldb


def _build(path, rows: int) -> None:
    conn = quilldb.connect(str(path))
    conn.execute("CREATE TABLE t (id INTEGER, pad TEXT)")
    conn.execute("BEGIN")
    for i in range(1, rows + 1):
        conn.execute("INSERT INTO t VALUES (?, ?)", (i, "x" * 60))
    conn.execute("COMMIT")
    conn.close()


def _assert_sqlite_reads(path, expected_ids: list[int]) -> None:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert [r[0] for r in conn.execute("SELECT id FROM t ORDER BY id")] == expected_ids
    finally:
        conn.close()


def test_b8_1_reproduction_leaves_a_file_sqlite_accepts(tmp_path) -> None:
    path = tmp_path / "b81.db"
    _build(path, 100)
    conn = quilldb.connect(str(path))
    conn.execute("DELETE FROM t WHERE id <= 59")
    conn.close()
    _assert_sqlite_reads(path, list(range(60, 101)))


@pytest.mark.parametrize("seed", range(6))
def test_random_deletes_keep_the_file_valid(tmp_path, seed: int) -> None:
    rng = random.Random(seed)
    rows = rng.choice([100, 400, 3000])
    path = tmp_path / f"r{seed}.db"
    _build(path, rows)

    survivors = set(range(1, rows + 1))
    conn = quilldb.connect(str(path))
    for _ in range(6):
        lo = rng.randint(1, rows)
        hi = lo + rng.randint(0, rows // 2)
        conn.execute("DELETE FROM t WHERE id >= ? AND id <= ?", (lo, hi))
        survivors -= set(range(lo, hi + 1))
    conn.close()

    _assert_sqlite_reads(path, sorted(survivors))
