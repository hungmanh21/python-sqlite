"""Buffer pool hit rate vs pool size, and what one big scan does to it.


Chapter 04's two claims, measured:

1. There is a working-set KNEE. Random point lookups touch some set of
   pages (index path + table leaf). Give the pool fewer pages than that set
   and the hit rate collapses; give it more and the rest is wasted memory.
2. Sequential flooding. LRU assumes recently used means soon used again; a
   scan touches every page once, so it evicts the hot pages to cache pages
   nobody will ask for again.


The hit rate is `hits / (hits + misses)` over a measured window that starts
AFTER a warm-up, so it describes a steady-state pool, not a cold one. Page
reads (misses) are shown beside it because they are the number that costs
I/O; the rate is only a ratio of them.


    python -m quilldb.bench.hit_rate
"""

from __future__ import annotations

import pathlib
import random
import shutil
import tempfile

import quilldb

ROWS = 20_000
CAPACITIES = (8, 16, 32, 64, 128, 256, 512, 1024)
WARMUP = 2_000
LOOKUPS = 5_000
HOT_KEYS = 20
SEED = 1


def _row(i: int) -> tuple[int, str, str]:
    return (i, f"u{i}@example.com", f"user number {i} " + "padding" * 8)


def build(path: pathlib.Path) -> None:
    db = quilldb.connect(str(path))
    db.execute("CREATE TABLE users (id INTEGER, email TEXT, name TEXT)")
    db.execute("CREATE INDEX ix_id ON users (id)")
    db.execute("BEGIN")
    for i in range(1, ROWS + 1):
        db.execute("INSERT INTO users VALUES (?, ?, ?)", _row(i))
    db.execute("COMMIT")
    db.close()


ZIPF_S = 1.2
_WEIGHTS = [1 / rank**ZIPF_S for rank in range(1, ROWS + 1)]
# Rank -> id, fixed by SEED. Without it the hottest ranks would be ids 1, 2,
# 3..., which sit on the same few leaf pages and flatter the cache; real hot
# rows are not neighbours.
_ID_OF_RANK = random.Random(SEED).sample(range(1, ROWS + 1), ROWS)


def pick_keys(n: int, rng: random.Random) -> list[int]:
    """`n` ids to look up, Zipf-skewed (exponent 1.2) over all ROWS ids.

    A few ids are asked for very often, most almost never. Which ids are hot
    is fixed and scattered across the table, so the hot set is spread over
    many pages. The knee in the table therefore describes THIS skew;
    uniform draws would push it right, to the whole working set.
    """
    ranks = rng.choices(range(ROWS), weights=_WEIGHTS, k=n)
    return [_ID_OF_RANK[rank] for rank in ranks]


def _lookup(db: quilldb.Connection, key: int) -> None:
    db.execute("SELECT name FROM users WHERE id = ?", (key,)).fetchall()


def _window(db: quilldb.Connection, keys: list[int]) -> tuple[int, int]:
    """Run `keys` and return (misses, hits) for just that window."""
    misses, hits = db.pool.misses, db.pool.hits
    for key in keys:
        _lookup(db, key)
    return db.pool.misses - misses, db.pool.hits - hits


def sweep(path: pathlib.Path) -> list[tuple[int, int, float]]:
    """(capacity, misses, hit rate) per pool size, on a warmed pool."""
    rows = []
    for capacity in CAPACITIES:
        rng = random.Random(SEED)
        db = quilldb.connect(str(path), pool_capacity=capacity)
        _window(db, pick_keys(WARMUP, rng))
        misses, hits = _window(db, pick_keys(LOOKUPS, rng))
        db.close()
        rows.append((capacity, misses, hits / (hits + misses)))
    return rows


def flood(path: pathlib.Path, capacity: int = 128) -> tuple[float, float, float]:
    """Hit rate on a small hot set: before a full scan, right after, recovered."""
    rng = random.Random(SEED)
    hot = [rng.randint(1, ROWS) for _ in range(HOT_KEYS)]
    probes = [rng.choice(hot) for _ in range(LOOKUPS)]

    def rate(db: quilldb.Connection) -> float:
        misses, hits = _window(db, probes)
        return hits / (hits + misses)

    db = quilldb.connect(str(path), pool_capacity=capacity)
    _window(db, probes)  # warm the hot set in
    before = rate(db)
    db.execute("SELECT COUNT(*) FROM users WHERE name = 'zzz'").fetchall()
    # ONE pass over the hot set, each id once: that is the cost of getting
    # back to steady state. A longer window would average the misses away
    # (the second visit to any id is a hit) and hide the flood.
    misses, hits = _window(db, list(dict.fromkeys(hot)))
    after = hits / (hits + misses)
    recovered = rate(db)
    db.close()
    return before, after, recovered


def main() -> None:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="quilldb-hitrate-"))
    try:
        path = tmp / "hit_rate.db"
        build(path)

        print(f"Random point lookups, {ROWS:,} rows, {LOOKUPS:,} lookups after a {WARMUP:,} warm-up")
        print(f"{'pool pages':>11}{'page reads':>12}{'hit rate':>10}")
        for capacity, misses, rate in sweep(path):
            print(f"{capacity:>11,}{misses:>12,}{rate:>10.1%}")

        before, after, recovered = flood(path)
        print(f"\nSequential flooding, {HOT_KEYS} hot ids, pool of 128 pages")
        print(f"{'hit rate before scan':<28}{before:>7.1%}")
        print(f"{'first pass after a full scan':<28}{after:>7.1%}")
        print(f"{'once the hot set is back':<28}{recovered:>7.1%}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
