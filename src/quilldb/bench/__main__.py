"""`python -m quilldb.bench` -- run every benchmark that exists, in order.

Each benchmark builds its own database in a temp directory, so this takes no
path argument. The individual modules can also be run alone:

    python -m quilldb.bench.index_lookup
    python -m quilldb.bench.concurrent
"""

from quilldb.bench import concurrent, index_lookup


def main() -> None:
    index_lookup.main()
    print()
    concurrent.main()


if __name__ == "__main__":
    main()
