"""Build the database the demo starts from: 5,000 users, no indexes yet.

    python examples/build_demo_db.py demo.db
    quilldb shell demo.db

Half the users are active, so an index on `active` is nearly useless; every
email is distinct, so an index on `email` is decisive. The demo creates both
indexes and shows the planner choosing between them.
"""

from __future__ import annotations

import sys
from pathlib import Path

import quilldb

ROWS = 5_000


def build(path: Path) -> None:
    if path.exists():
        path.unlink()
    conn = quilldb.connect(path)
    conn.execute("CREATE TABLE users (id INTEGER, email TEXT, active INTEGER, name TEXT)")
    conn.execute("BEGIN")
    for i in range(1, ROWS + 1):
        conn.execute(
            "INSERT INTO users VALUES (?, ?, ?, ?)",
            (i, f"user{i}@example.com", i % 2, f"User number {i}"),
        )
    conn.execute("COMMIT")
    conn.close()


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "demo.db")
    build(target)
    print(f"wrote {target} ({ROWS:,} users, no indexes)")
