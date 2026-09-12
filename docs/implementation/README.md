# quilldb — Implementation Plan


Companion to `guide.md` (concepts) and `roadmap.md` (schedule). **This is the coding spec.**


How to use it: for each file you get the **signatures with docstrings** (stubbed with
`NotImplementedError`) and the **tests already written**. Your job is to make the tests pass. The
tests *are* the specification — when they're green, that file is done.


Copy the stubs and tests in verbatim. Write the bodies yourself.


---


---


## The eight files


One per week. Each is self-contained: stubs, tests, and a definition of done.


| Week | Spec | Builds | Theory to read first |
|---|---|---|---|
| 1 | [Storage and codec](week-1-storage.md) | `errors`, `constants`, `codec/`, `storage/` | [01, 02](../theory/storage/), [03](../theory/codec/), [04](../theory/storage/04-the-buffer-pool.md) |
| 2 | [The B+Tree](week-2-btree.md) | `btree/cells`, `btree/btree`, `btree/validate` | [05, 06](../theory/btree/) |
| 3 | [SQL frontend and executor](week-3-sql.md) ⭐ | `sql/`, `catalog/`, `exec/`, `api/` | [07](../theory/sql/), [08](../theory/catalog/), [09](../theory/exec/) |
| 4 | [Mutation and indexes](week-4-mutation-and-indexes.md) | `btree/index`, `plan/`, delete/update operators | [10, 11](../theory/btree/), [12](../theory/plan/) |
| 5 | [Transactions and recovery](week-5-transactions.md) ⭐⭐ | `txn/journal`, `txn/transaction`, `txn/recovery` | [13, 14](../theory/txn/) |
| 6 | [Concurrency](week-6-concurrency.md) ⭐ | `txn/locks`, thread-safe pool, `Database`/`Connection` | [15, 16](../theory/txn/) |
| 7 | [Query processing](week-7-query-processing.md) | `exec/join`, `exec/aggregate`, `exec/sort` | [17, 18](../theory/exec/) |
| 8 | [Presentation](week-8-presentation.md) | README, benchmarks, CI, docs | [19](../theory/benchmarks/) |


⭐ marks the weeks that carry the most interview signal. If the schedule slips, these are the last
things to cut — see the descope ladder in `../../roadmap.md` §5.


**Specs get more detailed the closer they are.** Weeks 1–3 give you every signature and every test.
Weeks 4–8 give you the contracts, the algorithms, the tests that matter, and the traps — but they
deliberately stop short of transcribing every stub, because signatures written before week 3's
operator contracts have been exercised by real queries would be fiction you'd have to rewrite
anyway.


---


## The four docs, and which one to open


| Doc | Answers |
|---|---|
| `../../roadmap.md` | *What* am I building this week, and in how many hours? |
| `../../guide.md` | What *is* this thing, in plain language? |
| `../theory/` | *Why* is it shaped like that, and what did SQLite decide? |
| **this directory** | What exactly do I *type*? |


---


## Session 0: The First 30 Minutes


Right now, before anything else:


```bash
cd /prj/corp/airesearch/lasvegas/vol11-scratch/users/hmanh/road_to_l6/sqlite_scratch
mkdir -p quilldb/{src/quilldb/{codec,storage,btree},tests/unit}
cd quilldb
git init
python -m venv .venv && source .venv/bin/activate
pip install pytest pytest-cov hypothesis mypy ruff
```


`pyproject.toml`:


```toml
[project]
name = "quilldb"
version = "0.1.0"
requires-python = ">=3.11"


[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"


[tool.setuptools.packages.find]
where = ["src"]


[tool.pytest.ini_options]
testpaths = ["tests"]


[tool.mypy]
strict = true
files = ["src"]


[tool.ruff]
line-length = 100
```


Then:


```bash
pip install -e .
touch src/quilldb/__init__.py src/quilldb/codec/__init__.py src/quilldb/storage/__init__.py
echo "def test_setup_works(): assert True" > tests/unit/test_smoke.py
pytest
git add -A && git commit -m "Project scaffolding"
```


One passing test and one commit. You've started.


---


---


## Working Rules


**Order within a session:** read the tests → copy the stub → run `pytest` and watch it fail for the
right reason → implement → green → commit. Never write implementation before you've seen the test
fail; a test that passes against `NotImplementedError` is testing nothing.


**When stuck 45 minutes:** stop and build visibility. `hexdump(page)` with labeled offsets;
`dump_tree()`; a `--verbose` flag that logs every page read. In this project most "I'm stuck" is
"I can't see what's happening."


**Seed your randomness.** `random.Random(42)`, not `random.shuffle`. A failure you can't reproduce
costs an hour.


**`NOTES.md` after every bug.** Symptom → what you assumed → what it was → fix. Four lines. This is
where your interview stories come from.


**Commit message convention** — keeps the graph readable, and it's free:


```
storage: add slotted page parse/serialize
btree: fix separator key off-by-one in leaf split
test: cover overflow chain cycle detection
```