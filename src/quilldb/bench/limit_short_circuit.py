"""LIMIT stops the scan: page reads for `LIMIT 1` vs the statements that can't stop.

`Limit` is a pull-based operator, so once it has its rows it stops pulling
and nothing below it runs further (exec/operators.py). That only helps when
nothing BLOCKING sits between it and the scan: `ORDER BY` has to see every
row before it can say which is first, so `ORDER BY ... LIMIT 1` reads the
whole table even though it returns one row. Both are shown, because the
second is the one people expect to be cheap.

Page reads are the headline, on a cold pool (a fresh connection per row).

    python -m quilldb.bench.limit_short_circuit
"""

from __future__ import annotations

import pathlib
import shutil
import tempfile

import quilldb
from quilldb.bench._util import measure_cold

ROWS = 100_000

QUERIES = (
    ("SELECT * FROM t LIMIT 1", "LIMIT 1"),
    ("SELECT * FROM t LIMIT 100", "LIMIT 100"),
    ("SELECT * FROM t ORDER BY grp LIMIT 1", "ORDER BY grp LIMIT 1"),
    ("SELECT COUNT(*) FROM t", "COUNT(*) (no LIMIT)"),
)


def build(path: pathlib.Path) -> None:
    db = quilldb.connect(str(path))
    db.execute("CREATE TABLE t (id INTEGER, grp INTEGER, payload TEXT)")
    db.execute("BEGIN")
    for i in range(1, ROWS + 1):
        db.execute("INSERT INTO t VALUES (?, ?, ?)", (i, i % 97, f"row {i} " + "padding" * 8))
    db.execute("COMMIT")
    db.close()


def main() -> None:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="quilldb-limit-"))
    try:
        path = tmp / "limit.db"
        build(path)
        print(f"LIMIT short-circuit, {ROWS:,} rows, cold pool")
        print(f"{'statement':<24}{'page reads':>12}{'rows examined':>15}{'returned':>10}{'ms':>9}")
        for sql, label in QUERIES:
            reads, examined, returned, ms = measure_cold(path, sql)
            print(f"{label:<24}{reads:>12,}{examined:>15,}{returned:>10,}{ms:>9.1f}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
