"""The crash matrix (week5-transactions.md SS33) -- one scenario, every
write boundary. The full six-scenario product and the sync-boundary /
crash-during-recovery variants are session 7; this session earns ONE
scenario end to end, which is already most of the plumbing.

The claim under test: after a crash at ANY write boundary, mid-transaction,
the database that connect() hands back is either entirely pre-transaction
or entirely post-transaction -- never a mixture. "I implemented a journal"
is a claim; this loop, plus the five assertions inside it, is the evidence.
"""

import pathlib
import subprocess
import tempfile
from collections.abc import Callable
from typing import BinaryIO

import pytest
from conftest import FaultyFile, patch_fsync

import quilldb
from quilldb.btree.validate import validate_btree, validate_index_btree
from quilldb.errors import SimulatedCrash

# One row per scenario name -> a callable that performs that scenario's
# mutation(s) against an already-open `db` with an explicit transaction in
# progress. Session 7 fills the rest of this list in (insert_causing_split,
# delete_freeing_page, update_with_indexes, analyze_refresh, multi_page_txn
# -- see the plan's SS33 for what each one is meant to exercise).
SCENARIOS: dict[str, Callable[[quilldb.Connection], object]] = {
    "single_insert": lambda db: db.execute("INSERT INTO t VALUES (9999, 'x')"),
}


def _setup_known_state(path: pathlib.Path) -> None:
    """A committed, closed database with one row already in it -- the
    STATE_BEFORE every scenario's crash must be able to fall back to.
    """
    db = quilldb.connect(str(path))
    db.execute("CREATE TABLE t (id INTEGER, v TEXT)")
    db.execute("INSERT INTO t VALUES (1, 'ada')")
    db.close()


def _run_scenario(
    path: pathlib.Path,
    scenario: str,
    *,
    target: str,
    crash_at: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Open a transaction, wire a fault into either the database file or
    the journal file (`target`), then run the scenario body and let
    SimulatedCrash propagate -- the caller wraps this in pytest.raises.

    Faulting is wired AFTER `BEGIN`, since the journal doesn't exist until
    then, and the fault has to be live before the scenario's own writes
    start -- which is also why this can't reuse Connection.transaction():
    that helper's own commit() would run to completion before this
    function got a chance to swap in the FaultyFile.
    """
    db = quilldb.connect(str(path))
    db.execute("BEGIN")
    txn = db._txn
    assert txn is not None

    if target == "db":
        real_file: BinaryIO = db.pager._file
        db.pager._file = FaultyFile(real_file, fail_at_write=crash_at)  # type: ignore[assignment]
    elif target == "journal":
        assert txn._journal._file is not None
        real_file = txn._journal._file
        txn._journal._file = FaultyFile(real_file, fail_at_write=crash_at)  # type: ignore[assignment]
    else:
        raise ValueError(f"unknown target {target!r}")
    patch_fsync(monkeypatch, target_fd=real_file.fileno(), fail_at_sync=None)

    SCENARIOS[scenario](db)
    db.execute("COMMIT")


def _measure_real_write_count(path: pathlib.Path, scenario: str, target: str) -> int:
    """The honest source for how many write boundaries a (scenario, target)
    pair actually has (SS32's "third design note"): run it to completion
    with `fail_at_write=None` -- nothing ever crashes -- and read off how
    many real `.write()` calls FaultyFile counted.

    `db` and `journal` are different files with very different write
    counts for the same scenario (see the docstring below), so this is
    called once per target rather than assumed to be one shared number.
    """
    db = quilldb.connect(str(path))
    db.execute("BEGIN")
    txn = db._txn
    assert txn is not None

    if target == "db":
        faulty = FaultyFile(db.pager._file, fail_at_write=None)
        db.pager._file = faulty  # type: ignore[assignment]
    elif target == "journal":
        assert txn._journal._file is not None
        faulty = FaultyFile(txn._journal._file, fail_at_write=None)
        txn._journal._file = faulty  # type: ignore[assignment]
    else:
        raise ValueError(f"unknown target {target!r}")

    SCENARIOS[scenario](db)
    db.execute("COMMIT")
    # No db.close() here: _run_scenario never calls it either (a real crash
    # never gets a clean close), and close() does its own extra header
    # write -- counting it would fabricate a crash_at that can't happen.
    return faulty.writes


def _write_boundaries() -> list[tuple[str, str, int]]:
    """Every real (scenario, target, crash_at) triple worth crashing at --
    built from measured counts, not a shared guess. `db` writes only
    happen after commit_barrier() (Pager.write_page's assertion forbids
    it earlier), while `journal` writes happen three times per journalled
    page (page_id, image, checksum -- Journal.record_original()) plus the
    header and the magic/nRec stamp, so the two counts are never equal and
    a single MAX_WRITES shared between them would either miss real `db`
    boundaries or generate impossible `journal` ones (or the reverse).
    """
    triples: list[tuple[str, str, int]] = []
    with tempfile.TemporaryDirectory() as tmp:
        for scenario in SCENARIOS:
            for target in ("db", "journal"):
                path = pathlib.Path(tmp) / f"{scenario}-{target}.db"
                _setup_known_state(path)
                count = _measure_real_write_count(path, scenario, target)
                triples.extend((scenario, target, n) for n in range(1, count + 1))
    return triples


@pytest.mark.parametrize("scenario,target,crash_at", _write_boundaries())
def test_atomic_at_every_write_boundary(
    tmp_path: pathlib.Path,
    scenario: str,
    target: str,
    crash_at: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "t.db"
    _setup_known_state(path)

    with pytest.raises(SimulatedCrash):
        _run_scenario(path, scenario, target=target, crash_at=crash_at, monkeypatch=monkeypatch)

    # Recovery runs here, inside connect() -- nobody calls it directly.
    db = quilldb.connect(str(path))

    # check 1: state is unmixed -- this scenario never reaches STATE_AFTER
    # (see the module for why: db writes only ever happen post-barrier, and
    # journal.delete() has no write() call for FaultyFile to fault, so
    # every reachable crash point rolls all the way back).
    assert sorted(db.execute("SELECT * FROM t").fetchall()) == [
            (1, "ada"),
        ]

    # check 2 + 3: every b-tree is structurally valid, and (once a scenario
    # actually has one) no index disagrees with its table -- the loop below
    # validates both the same way regardless of how many indexes exist.
    for table in db.catalog.list_tables():
        validate_btree(pager = db.pager, pool = db.pool, root=table.root_page)
        for index in db.catalog.indexes_for(table=table.name):
            validate_index_btree(pager = db.pager, pool = db.pool, root=index.root_page, n_key_columns=len(index.columns))
    # check 4
    db.close()
    result = subprocess.run(
        ["sqlite3", str(path), "PRAGMA integrity_check;"], capture_output=True, text=True, check=False
    )
    assert result.stdout.strip() == "ok"

    # check 5
    assert not (tmp_path / "t.db-journal").exists()


def test_no_fault_commits_cleanly_and_leaves_no_journal(tmp_path: pathlib.Path) -> None:
    """The negative the plan insists on (SS33, 'also assert the negative'):
    a matrix that only ever crashes proves nothing if the no-crash case was
    never actually exercised.
    """
    path = tmp_path / "t.db"
    _setup_known_state(path)

    db = quilldb.connect(str(path))
    db.execute("BEGIN")
    SCENARIOS["single_insert"](db)
    db.execute("COMMIT")
    assert sorted(db.execute("SELECT * FROM t").fetchall()) == [
        (1, "ada"),
        (9999, "x"),
    ]
    db.close()
    assert not (tmp_path / "t.db-journal").exists()
