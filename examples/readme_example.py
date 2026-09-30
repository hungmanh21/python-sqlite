"""The example from the README. CI runs this file, so it cannot rot.

    python examples/readme_example.py
"""

import pathlib
import sqlite3
import tempfile

import quilldb

path = pathlib.Path(tempfile.mkdtemp()) / "demo.db"
db = quilldb.connect(path)
db.execute("CREATE TABLE users (id INTEGER, email TEXT, age INTEGER)")
db.execute("CREATE INDEX ix_email ON users (email)")
db.execute("BEGIN")
for i in range(1, 1001):
    db.execute("INSERT INTO users VALUES (?, ?, ?)", (i, f"user{i}@example.com", 20 + i % 40))
db.execute("COMMIT")
db.execute("ANALYZE")

print(db.execute("EXPLAIN SELECT id FROM users WHERE email = ?", ("user7@example.com",)).fetchall()[0][0])
print(db.execute("SELECT age, COUNT(*) FROM users GROUP BY age ORDER BY age LIMIT 3").fetchall())
db.close()

# The file is a real SQLite database: the standard library's sqlite3 checks it.
# CI runs this script, so a corrupt file fails the build.
result = sqlite3.connect(path).execute("PRAGMA integrity_check").fetchone()[0]
print(result)
assert result == "ok", result
