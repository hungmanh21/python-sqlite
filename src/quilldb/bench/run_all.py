"""Run the benchmarks and emit one paste-able markdown document.

    python -m quilldb.bench                    # all of them
    python -m quilldb.bench hit_rate join_order  # just these
    python -m quilldb.bench --list

Each benchmark builds its own database in a temp directory and prints its
own table. This wraps every one under a heading, in a fenced block, with a
one-line caveat -- the thing a reader must know to read the numbers right.
The tables are not re-parsed into markdown tables on purpose: they stay
correct however a benchmark's columns change.

Page reads (buffer-pool misses) are the headline throughout; seconds are
context (chapter 19). The whole run takes a few minutes.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import platform
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass

from quilldb.bench import (
    btree_height,
    concurrent,
    covering_index,
    hit_rate,
    index_lookup,
    inserts,
    join_order,
    limit_short_circuit,
)


@dataclass(frozen=True)
class Benchmark:
    name: str
    title: str
    run: Callable[[], None]
    caveat: str


BENCHMARKS = (
    Benchmark(
        "index_lookup",
        "Point lookup: index vs scan, and what an index costs to write",
        index_lookup.main,
        "Page reads are on a cold pool; milliseconds run against a warm OS cache and are context only.",
    ),
    Benchmark(
        "btree_height",
        "B+tree height vs rows",
        btree_height.main,
        "Rows above 100,000 are computed from the measured fanout, not built.",
    ),
    Benchmark(
        "inserts",
        "B+tree insert: sequential vs random keys",
        inserts.main,
        "Measures BTree.insert, not the SQL INSERT: the parser cannot choose a rowid.",
    ),
    Benchmark(
        "hit_rate",
        "Buffer pool hit rate vs pool size",
        hit_rate.main,
        "The sweep uses Zipf-skewed lookups (exponent 1.2); uniform draws give a sharper knee.",
    ),
    Benchmark(
        "covering_index",
        "What a covering index would save",
        covering_index.main,
        "quilldb has no index-only path; the second column is a direct index walk, not a feature.",
    ),
    Benchmark(
        "join_order",
        "Join order",
        join_order.main,
        "A LEFT JOIN pins the written order (INNER is always reordered); all statements return the same rows.",
    ),
    Benchmark(
        "limit_short_circuit",
        "LIMIT short-circuits the scan",
        limit_short_circuit.main,
        "ORDER BY ... LIMIT 1 still reads everything: a sort has to see every row first.",
    ),
    Benchmark(
        "concurrent",
        "Throughput vs threads",
        concurrent.main,
        "Reads are flat under the GIL; writes are flat because there is a single writer.",
    ),
)


def _capture(run: Callable[[], None]) -> tuple[str, float]:
    buffer = io.StringIO()
    started = time.perf_counter()
    with contextlib.redirect_stdout(buffer):
        run()
    return buffer.getvalue().rstrip(), time.perf_counter() - started


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m quilldb.bench", description=__doc__.splitlines()[0])
    parser.add_argument("names", nargs="*", help="benchmarks to run (default: all)")
    parser.add_argument("--list", action="store_true", help="list benchmark names and exit")
    args = parser.parse_args(argv)

    by_name = {b.name: b for b in BENCHMARKS}
    if args.list:
        for b in BENCHMARKS:
            print(f"{b.name:<22}{b.title}")
        return 0
    unknown = [n for n in args.names if n not in by_name]
    if unknown:
        print(f"unknown benchmark(s): {', '.join(unknown)}; try --list", file=sys.stderr)
        return 2
    chosen = [by_name[n] for n in args.names] if args.names else list(BENCHMARKS)

    print("# quilldb benchmarks\n")
    print(f"Python {platform.python_version()} on {platform.system()} {platform.machine()}. "
          "Page reads are the headline; times are context.\n")
    for b in chosen:
        print(f"## {b.title}\n", flush=True)
        print(f"*{b.caveat}*\n")
        output, seconds = _capture(b.run)
        print(f"```text\n{output}\n```\n")
        print(f"<sub>ran in {seconds:.0f}s</sub>\n", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
