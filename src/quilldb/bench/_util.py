"""Helpers shared by the benchmarks. Not a benchmark itself."""

from __future__ import annotations

import pathlib
import time
from collections.abc import Sequence

import quilldb
from quilldb.codec.record import Value


def measure_cold(
    path: pathlib.Path, sql: str, params: Sequence[Value] = (), *, pool_capacity: int = 128
) -> tuple[int, int, int, float]:
    """Run one statement on a FRESH connection, so the pool starts cold.

    Returns (page reads, rows examined, rows returned, milliseconds). A page
    read is exactly a buffer-pool miss, so a cold pool is what makes the
    count mean "pages this statement needed". The OS page cache is not
    dropped, so the milliseconds are context, not the headline.
    """
    db = quilldb.connect(str(path), pool_capacity=pool_capacity)
    misses, examined = db.pool.misses, db.pool.rows_examined
    started = time.perf_counter()
    rows = db.execute(sql, tuple(params)).fetchall()
    elapsed = (time.perf_counter() - started) * 1000
    result = (db.pool.misses - misses, db.pool.rows_examined - examined, len(rows), elapsed)
    db.close()
    return result
