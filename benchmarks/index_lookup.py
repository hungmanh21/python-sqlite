"""Same query, same data, with and without an index. Report PAGE READS.


Chapter 19 SS19.2's argument, applied: page reads are what the access path
determines. They're deterministic, they don't depend on this laptop's
thermal state, and nobody has to wonder whether a warm cache was being
measured. Seconds are reported too, but only as context.


A page read is exactly a buffer-pool MISS, so every measurement runs
against a FRESH connection -- a cold pool. That is chapter 19's "cold-ish"
setting: it matches the page-read count, which is the number being
claimed. The OS page cache is NOT dropped (that needs root), so the
milliseconds reflect a warm OS cache and are not the headline.


    python benchmarks/index_lookup.py
"""

from __future__ import annotations

import pathlib
import shutil
import tempfile
import time

import quilldb

ROWS = 100_000
NEEDLE = ROWS // 2


def _row(i: int) -> tuple[int, str, str]:
    # ~100 bytes/row, so 100k rows is ~10MB -- about 2,400 pages of 4096
    # bytes, which is the arithmetic a reader should be able to check
    # against the scan number below.
    return (i, f"u{i}@example.com", f"user number {i} " + "padding" * 8)


def build(path: pathlib.Path, *, with_index: bool) -> None:
    db = quilldb.connect(str(path))
    db.execute("CREATE TABLE users (id INTEGER, email TEXT, name TEXT)")
    if with_index:
        db.execute("CREATE INDEX ix_email ON users (email)")
    for i in range(1, ROWS + 1):
        db.execute("INSERT INTO users VALUES (?, ?, ?)", _row(i))
    db.close()


def measure_lookup(path: pathlib.Path) -> tuple[int, int, float, int]:
    """Open cold, run one point lookup, report what it cost."""
    db = quilldb.connect(str(path))
    db.reset_counters()
    started = time.perf_counter()
    rows = db.execute(
        "SELECT id FROM users WHERE email = ?", (f"u{NEEDLE}@example.com",)
    ).fetchall()
    elapsed = time.perf_counter() - started
    result = (db.pages_read, db.rows_examined, elapsed * 1000, len(rows))
    db.close()
    return result


def measure_insert_cost(tmp: pathlib.Path, index_count: int, rows: int = 5_000) -> float:
    """The write-amplification tax. Every index is paid for on every
    INSERT, and quoting the lookup speedup without this number is the
    dishonest half of the story.
    """
    path = tmp / f"w{index_count}.db"
    db = quilldb.connect(str(path))
    db.execute("CREATE TABLE users (id INTEGER, email TEXT, name TEXT)")
    for column in ("email", "name", "id")[:index_count]:
        db.execute(f"CREATE INDEX ix_{column} ON users ({column})")
    started = time.perf_counter()
    for i in range(1, rows + 1):
        db.execute("INSERT INTO users VALUES (?, ?, ?)", _row(i))
    elapsed = time.perf_counter() - started
    db.close()
    return elapsed * 1000 / rows


def main() -> None:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="quilldb-bench-"))
    try:
        print(f"quilldb index lookup benchmark -- {ROWS:,} rows, 4096-byte pages")
        print("page reads are buffer-pool misses, cold pool (fresh connection per run)\n")

        print(f"{'access path':<24} {'page reads':>12} {'rows examined':>15} {'ms':>9}")
        print("-" * 63)

        results: dict[bool, tuple[int, int, float, int]] = {}
        for with_index in (False, True):
            path = tmp / f"bench_{with_index}.db"
            build(path, with_index=with_index)
            pages, rows_seen, ms, found = measure_lookup(path)
            results[with_index] = (pages, rows_seen, ms, found)
            label = "IndexScan ix_email" if with_index else "SeqScan + Filter"
            print(f"{label:<24} {pages:>12,} {rows_seen:>15,} {ms:>9.1f}")
            assert found == 1, f"expected exactly one row, got {found}"

        scan_pages = results[False][0]
        index_pages = results[True][0]
        if index_pages:
            print(f"\n{scan_pages / index_pages:.0f}x fewer page reads with the index")
        print(f"check the arithmetic: {ROWS:,} rows x ~100 bytes / 4096 = ~{ROWS * 100 // 4096:,} pages")

        print("\nwrite side -- the tax every index charges on INSERT:")
        print(f"{'indexes':<24} {'ms per insert':>15}")
        print("-" * 40)
        baseline = None
        for index_count in (0, 1, 3):
            per_insert = measure_insert_cost(tmp, index_count)
            if baseline is None:
                baseline = per_insert
            factor = f"  ({per_insert / baseline:.2f}x)" if baseline else ""
            print(f"{index_count:<24} {per_insert:>15.4f}{factor}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
