"""What a covering index WOULD save. quilldb does not have one.

An index entry holds the key columns and the rowid, so a query that asks only
for indexed columns could be answered from the index alone. quilldb never
does that: every IndexScan follows each rowid into the table
(exec/operators.py, IndexScan; plan/cost.py prices it that way). So this is
not "covering vs not covering" in the engine -- it is the engine's real cost
next to the cost of an index-only walk over the same key range, measured by
reading the index B+tree directly. The second column is what an
implementation would have to beat, not something quilldb does today.

    real:        SELECT email FROM users WHERE email >= lo AND email < hi
    index-only:  IndexBTree.seek_range(lo, hi), no table lookups

Page reads are on a cold pool (a fresh connection per row). The engine's
count includes what any statement pays that the direct index walk does not
-- the schema page it resolves `users` and `ix_email` from -- so the ratio
slightly flatters the index-only side at 1 row and matters less as the
range grows.

    python -m quilldb.bench.covering_index
"""

from __future__ import annotations

import pathlib
import shutil
import tempfile

import quilldb
from quilldb.bench._util import measure_cold
from quilldb.btree.index import IndexBTree

ROWS = 20_000
RANGE_SIZES = (1, 20, 200)


def _email(i: int) -> str:
    return f"u{i:06d}@example.com"


def build(path: pathlib.Path) -> None:
    db = quilldb.connect(str(path))
    db.execute("CREATE TABLE users (id INTEGER, email TEXT, name TEXT)")
    db.execute("CREATE INDEX ix_email ON users (email)")
    db.execute("BEGIN")
    for i in range(ROWS):
        db.execute(
            "INSERT INTO users VALUES (?, ?, ?)", (i, _email(i), f"user number {i} " + "padding" * 8)
        )
    db.execute("COMMIT")
    db.close()


def index_only_reads(path: pathlib.Path, low: str, high: str) -> int:
    """Page reads to walk the index over [low, high) and touch no table page."""
    db = quilldb.connect(str(path))
    index = db.catalog.indexes_for("users")[0]
    tree = IndexBTree(
        db.db.pager, db.pool, index.root_page, n_key_columns=len(index.columns), unique=index.unique
    )
    before = db.pool.misses
    for _ in tree.seek_range((low,), (high,), high_inclusive=False):
        pass
    reads = db.pool.misses - before
    db.close()
    return reads


def main() -> None:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="quilldb-covering-"))
    try:
        path = tmp / "covering.db"
        build(path)
        print(f"Index-only vs the engine's index scan, {ROWS:,} rows, cold pool")
        print(f"{'rows in range':>14}{'engine reads':>14}{'index-only reads':>18}{'ratio':>8}")
        start = ROWS // 2
        for k in RANGE_SIZES:
            low, high = _email(start), _email(start + k)
            reads, _examined, returned, _ms = measure_cold(
                path, "SELECT email FROM users WHERE email >= ? AND email < ?", (low, high)
            )
            assert returned == k, (returned, k)
            only = index_only_reads(path, low, high)
            print(f"{k:>14,}{reads:>14,}{only:>18,}{reads / only:>7.1f}x")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
