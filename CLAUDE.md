# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

quilldb: a relational database engine written from scratch in pure Python, built as an 8-week
portfolio project. It writes the **real SQLite on-disk file format, byte for byte** (not a format
"inspired by" SQLite — the actual `"SQLite format 3\000"` header, page layout, varints, cell
encoding, type codes). The acceptance test for storage/codec/btree work is literally
`sqlite3 file.db "PRAGMA integrity_check"` returning `ok`, and `src/tests/differential/` runs the
same SQL against quilldb and real `sqlite3` and diffs the results.

The project is organized as eight weekly slices (storage → B+tree → SQL/executor → mutation &
indexes → transactions → concurrency → joins/aggregation → presentation). Current branch
(`week4`) is mutation, indexes, and the planner. Check `docs/implementation/README.md`'s table to
see which files/weeks exist yet — later-week packages (`plan/`, `txn/`) may not exist in the tree
until that week lands.

There are four docs at the repo root, each answering a different question — check the doc's own
job before consulting it:

| Doc | Answers |
|---|---|
| `roadmap.md` | *What* to build this week, in how many hours |
| `guide.md` | What *is* this thing, in plain language (concept primer, session-by-session) |
| `docs/theory/` | *Why* it's shaped that way — mirrors `src/quilldb/` package-for-package |
| `docs/implementation/weekN-*.md` | Exact signatures/stubs and the tests that define "done" for that week |

`docs/decisions/ADR-*.md` records deliberate departures from "obvious" designs (e.g. ADR-001: the
buffer pool caches raw page bytes, not parsed page objects, matching SQLite's own pcache/btree
split).

## Commands

Activate the venv first (`source .venv/bin/activate`) or prefix with `.venv/bin/`.

```bash
# Run the full test suite (slow/scale tests excluded by default via addopts)
pytest

# Run one file / one test
pytest src/tests/unit/test_btree.py
pytest src/tests/unit/test_btree.py::test_split_cascades_to_root -v

# Run the differential harness against real sqlite3
pytest src/tests/differential/

# Include slow tests (100k+ inserts at real scale)
pytest -m slow

# Lint / type-check
ruff check .
mypy

# CLI: inspect a database file's header without going through Pager
quilldb inspect path/to/file.db
```

Note: `pyproject.toml`'s `testpaths = ["tests"]` doesn't match the actual `src/tests/` layout —
pytest falls back to recursive discovery from cwd and still finds everything, just with a
`PytestConfigWarning`. Run pytest from the repo root.

`mypy` is configured `strict = true` over `files = ["src"]` — new code is expected to pass strict
mode, not incrementally typed.

## Architecture

Layers, bottom to top, each one a package under `src/quilldb/`, and each a client of the layer
below with no upward dependencies:

1. **`storage/`** — `pager.py` (fixed-size numbered pages, the freelist), `page.py` (slotted page
   parse/serialize), `bufferpool.py` (pin-counted LRU cache of **raw page bytes**, per ADR-001 —
   callers decode), `overflow.py` (overflow chains for payloads that don't fit one page),
   `header.py` (the 100-byte file header).
2. **`codec/`** — `varint.py` (SQLite's variable-length integer encoding, up to 9 bytes),
   `record.py` (the manifest-then-body record format: a header of serial types followed by the
   column values, so reading column 5 doesn't require decoding 1–4).
3. **`btree/`** — `cells.py`, `btree.py` (the B+tree itself: search, insert, delete), `split.py`
   (the split cascade), `cursor.py` (ordered traversal), `validate.py` (structural invariant
   checker used by tests, not by the running engine). Table B-trees key on rowid; index B-trees
   (week 4) key on the indexed column(s) with the rowid glued on as the last column of every
   entry — not appended after the record — see chapter 11.
4. **`sql/`** — `tokenizer.py` → `parser.py` (hand-written, not generated) → `ast.py`. Produces an
   AST with unresolved names; nothing here touches the catalog.
5. **`catalog/`** — `schema.py` + `catalog.py`. Page 1 doubles as both the file header's home *and*
   the root of the `sqlite_schema` table-B-tree, which is how a quilldb file is self-describing.
   `sql/binder.py` sits here conceptually (resolves AST names against the catalog into a `Bound*`
   tree) even though the file lives under `sql/`.
6. **`plan/`** (week 4+) — `planner.py` picks an access path (seq scan vs. index) per query,
   `explain.py` renders it as text for `EXPLAIN`.
7. **`exec/`** — `operators.py` (pull-based/iterator operators: SeqScan, Filter, Project, Insert,
   ... — `open`/`next`/`close`, and nothing materializes a full result set), `expressions.py`
   (expression evaluation, including three-valued NULL logic and short-circuiting AND/OR).
8. **`api/connection.py`** — the DB-API-style public surface (`connect`, `Connection`, `Cursor`).
   This is the seam that turns "know about Pager, BufferPool, and Catalog" into one `execute()`
   call. Three invariants worth knowing before touching this file: `execute()` always returns a
   `Cursor` regardless of statement kind (count vs. rows show up in `rowcount`/`description`, not
   the return type); a `SELECT`'s operator stream is *not* drained into a list at the boundary —
   `fetchone()` pulls lazily from the same open operator; and starting a new `execute()` on a
   connection closes whatever cursor was still open on it, so pins can't accumulate silently
   (concurrent result sets require separate connections).

`errors.py` has one hard rule that classifies every exception in the file: anything deriving from
`CorruptDatabaseError` means the **file on disk** is bad; everything else means the **caller** (bad
SQL, wrong param count, ...) or internal control flow (`PageFullError` triggers a split,
`DuplicateRowIDError` is caught by callers that need it) is at fault. Preserve that split when
adding new error types.

`constants.py` documents itself as non-negotiable: every offset/magic value/type code is copied
from `sqlite.org/fileformat2.html`, not invented, because the acceptance test is a real SQLite
binary reading the file back.

## Working conventions specific to this project

- **Tests are the spec.** Earlier-week specs in `docs/implementation/` ship stubs raising
  `NotImplementedError` plus tests already written; the job is to make the tests pass, not to
  redesign the signature. Later weeks (4+) give contracts and the tests that matter but not full
  transcribed stubs — see `docs/implementation/README.md`.
- Read the matching `docs/theory/<package>/NN-*.md` chapter before writing in that package if
  you're unsure *why* something is shaped the way it is — it's more likely to have the answer than
  guessing from the code alone.
- Commit message convention used in this repo: `<package>: <what>` (e.g. `btree: fix separator key
  off-by-one in leaf split`, `storage: add slotted page parse/serialize`).
- Format fidelity and feature completeness are treated as different claims. Missing features
  (WAL, page merging, `VACUUM`, three-way rebalancing) are implementation gaps; anything that
  touches on-disk bytes must match the real format exactly — there is no "close enough" here.
