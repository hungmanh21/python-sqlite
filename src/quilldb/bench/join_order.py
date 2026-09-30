"""Join order: the same join written small-table-first and big-table-first.

A nested-loop join re-runs its inner side once per outer row. If the inner
table fits the buffer pool, that costs almost nothing after the first pass;
if it does not, every outer row re-reads it from disk. So the order decides
the page reads even when the rows examined are identical:

  big outer, small inner    scan `readings` once; `sensors` stays cached
  small outer, big inner    for each of the sensors, scan all of `readings`

For a pure INNER join the planner tries every order and keeps the cheapest
(plan/search.py), so writing the tables the other way round changes nothing
-- that is the planner earning its keep, and it means INNER JOIN cannot be
used to compare the orders. A LEFT JOIN is never reordered, so it pins the
order the SQL is written in. The data is built so every left row matches,
which makes all three statements return the same rows.

No index is involved on purpose: this is the join-order effect alone. (The
planner would not use one here anyway -- its cost model is in pages, and at
this size a scan of the inner table is priced below the seeks.)

    python -m quilldb.bench.join_order
"""

from __future__ import annotations

import pathlib
import shutil
import tempfile

import quilldb
from quilldb.bench._util import measure_cold

SENSORS = 20
READINGS = 5_000

CASES = (
    (
        "planner's choice (INNER)",
        "SELECT r.id, s.name FROM sensors s JOIN readings r ON r.sensor_id = s.id",
    ),
    (
        "written big-first (LEFT)",
        "SELECT r.id, s.name FROM readings r LEFT JOIN sensors s ON s.id = r.sensor_id",
    ),
    (
        "written small-first (LEFT)",
        "SELECT r.id, s.name FROM sensors s LEFT JOIN readings r ON r.sensor_id = s.id",
    ),
)


def build(path: pathlib.Path) -> None:
    db = quilldb.connect(str(path))
    db.execute("CREATE TABLE sensors (id INTEGER, name TEXT)")
    db.execute("CREATE TABLE readings (id INTEGER, sensor_id INTEGER, note TEXT)")
    db.execute("BEGIN")
    for i in range(SENSORS):
        db.execute("INSERT INTO sensors VALUES (?, ?)", (i, f"sensor {i}"))
    for i in range(READINGS):
        # ~300 bytes a row: about 13 rows a page, so ~380 pages -- well past
        # the 128-page default pool, which is the whole point.
        db.execute("INSERT INTO readings VALUES (?, ?, ?)", (i, i % SENSORS, f"reading {i} " + "x" * 280))
    db.execute("COMMIT")
    db.execute("ANALYZE")
    db.close()


def main() -> None:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="quilldb-join-"))
    try:
        path = tmp / "join.db"
        build(path)
        print(f"Join order, {SENSORS} sensors x {READINGS:,} readings, cold pool of 128 pages")
        print(f"{'order':<28}{'page reads':>12}{'rows examined':>15}{'returned':>10}{'ms':>10}")
        for label, sql in CASES:
            reads, examined, returned, ms = measure_cold(path, sql)
            print(f"{label:<28}{reads:>12,}{examined:>15,}{returned:>10,}{ms:>10.0f}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
