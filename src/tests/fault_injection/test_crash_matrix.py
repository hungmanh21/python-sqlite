"""The crash matrix (week5-transactions.md §33) -- every scenario, every
write boundary, every sync boundary, and a second crash during recovery
itself.

The claim under test: after a crash at ANY write or sync boundary,
mid-transaction, the database that connect() hands back is either entirely
pre-transaction or entirely post-transaction -- never a mixture. "I
implemented a journal" is a claim; this loop, plus the checks inside it, is
the evidence.
"""

import pathlib
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from typing import BinaryIO

import pytest
from conftest import FaultyFile, patch_fsync

import quilldb
from quilldb.btree.validate import validate_btree, validate_index_btree
from quilldb.errors import SimulatedCrash
from quilldb.storage.pager import Pager
from quilldb.txn.recovery import recover_if_needed


def _multi_page_txn(db: quilldb.Connection) -> None:
    """35,000 inserts, all inside ONE transaction: enough to push page_count
    past the pool's default capacity (128 -- see BufferPool.__init__), so
    every one of those pages stays pinned-dirty and uncommitted at once.
    No-steal (BufferPool.get_page_for_write's "saw_no_steal_candidate"
    branch) forbids evicting any of them before the barrier, so the pool's
    cache is allowed to exceed capacity rather than write early -- this
    scenario is the one that proves that path is actually exercised, not
    just theoretically reachable.
    """
    for i in range(2, 35_002):
        db.execute("INSERT INTO t VALUES (?, ?)", (i, f"v{i}"))


# One row per scenario name -> a callable that performs that scenario's
# mutation(s) against an already-open `db` with an explicit transaction in
# progress. Each name matches a branch in _setup_known_state below, which
# builds whatever precondition that mutation needs (existing rows to force
# a split, an index to keep in sync, a prior ANALYZE to replace, ...).
SCENARIOS: dict[str, Callable[[quilldb.Connection], object]] = {
    "single_insert": lambda db: db.execute("INSERT INTO t VALUES (9999, 'x')"),
    "insert_causing_split": lambda db: db.execute("INSERT INTO t VALUES (318, 'v318')"),
    "delete_freeing_page": lambda db: db.execute("DELETE FROM t WHERE id >= 318"),
    "update_with_indexes": lambda db: db.execute("UPDATE t SET v = 'zzz' WHERE id = 1"),
    "analyze_refresh": lambda db: db.execute("ANALYZE t"),
    "multi_page_txn": _multi_page_txn,
}

# single_insert is cheap enough to run on every commit (week5-transactions.md
# §33: "the write matrix for one cheap scenario runs on every commit"). The
# other five are real but not free -- multi_page_txn alone measures in the
# tens of seconds -- so they're gated behind -m slow, same as the full
# product and crash-during-recovery below.
_FAST_SCENARIOS = ("single_insert",)
_SLOW_SCENARIOS = tuple(s for s in SCENARIOS if s not in _FAST_SCENARIOS)


def _setup_known_state(path: pathlib.Path, scenario: str = "single_insert") -> None:
    """A committed, closed database holding whatever precondition `scenario`
    needs -- the STATE_BEFORE every crash in that scenario must be able to
    fall back to.

    Row counts below are measured, not guessed (the same lesson session 6
    learned the hard way about MAX_WRITES): inserting rows 1..317 one at a
    time into a fresh (id INTEGER, v TEXT) table leaves the very next
    insert (318) as the one that overflows the leaf and forces a split --
    confirmed by watching Pager.page_count change across that boundary.
    Rows 1..400 leave enough leaves that deleting id >= 318 empties one (or
    more) of them completely, which is what actually frees a page
    (btree.py's `parent_is_empty` branch), not merely shrinks it.
    """
    db = quilldb.connect(str(path))
    db.execute("CREATE TABLE t (id INTEGER, v TEXT)")

    if scenario == "update_with_indexes":
        # Built before the row exists, same as any real schema would be.
        db.execute("CREATE INDEX idx_v ON t(v)")

    db.execute("INSERT INTO t VALUES (1, 'ada')")

    if scenario == "insert_causing_split":
        for i in range(2, 318):
            db.execute("INSERT INTO t VALUES (?, ?)", (i, f"v{i}"))
    elif scenario == "delete_freeing_page":
        for i in range(2, 401):
            db.execute("INSERT INTO t VALUES (?, ?)", (i, f"v{i}"))
    elif scenario == "analyze_refresh":
        db.execute("ANALYZE t")
    # single_insert, update_with_indexes, multi_page_txn: the base row is
    # precondition enough -- the scenario itself does the rest.
    db.close()


def _run_scenario(
    path: pathlib.Path,
    scenario: str,
    *,
    target: str,
    crash_at: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Open a transaction, wire a WRITE fault into either the database file
    or the journal file (`target`), then run the scenario body and let
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


def _run_scenario_sync_fault(
    path: pathlib.Path,
    scenario: str,
    *,
    target: str,
    sync_at: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same shape as _run_scenario, but faults the sync_at-th os.fsync call
    on `target`'s file descriptor instead of a write -- writes themselves
    are left alone, so this exercises "the write succeeded but the fsync
    that was supposed to make it durable didn't."
    """
    db = quilldb.connect(str(path))
    db.execute("BEGIN")
    txn = db._txn
    assert txn is not None

    if target == "db":
        target_fd = db.pager._file.fileno()
    elif target == "journal":
        assert txn._journal._file is not None
        target_fd = txn._journal._file.fileno()
    else:
        raise ValueError(f"unknown target {target!r}")

    patch_fsync(monkeypatch, target_fd=target_fd, fail_at_sync=sync_at)
    SCENARIOS[scenario](db)
    db.execute("COMMIT")


def _measure_real_write_count(path: pathlib.Path, scenario: str, target: str) -> int:
    """The honest source for how many write boundaries a (scenario, target)
    pair actually has: run it to completion with `fail_at_write=None` --
    nothing ever crashes -- and read off how many real `.write()` calls
    FaultyFile counted.

    `db` and `journal` are different files with very different write
    counts for the same scenario, so this is called once per target rather
    than assumed to be one shared number.
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


def _measure_real_sync_count(path: pathlib.Path, scenario: str, target: str, monkeypatch: pytest.MonkeyPatch) -> int:
    """The sync-boundary equivalent of _measure_real_write_count: run to
    completion with fail_at_sync=None and read off patch_fsync's counter.
    """
    db = quilldb.connect(str(path))
    db.execute("BEGIN")
    txn = db._txn
    assert txn is not None

    if target == "db":
        target_fd = db.pager._file.fileno()
    elif target == "journal":
        assert txn._journal._file is not None
        target_fd = txn._journal._file.fileno()
    else:
        raise ValueError(f"unknown target {target!r}")

    counter = patch_fsync(monkeypatch, target_fd=target_fd, fail_at_sync=None)
    SCENARIOS[scenario](db)
    db.execute("COMMIT")
    return counter.syncs


def _write_boundaries(scenarios: tuple[str, ...]) -> list[tuple[str, str, int]]:
    """Every real (scenario, target, crash_at) triple worth crashing at --
    built from measured counts, not a shared guess (a single MAX_WRITES
    shared across targets is structurally broken: session 6 found that
    `db` and `journal` never have the same real write count for the same
    scenario).
    """
    triples: list[tuple[str, str, int]] = []
    with tempfile.TemporaryDirectory() as tmp:
        for scenario in scenarios:
            for target in ("db", "journal"):
                path = pathlib.Path(tmp) / f"{scenario}-{target}.db"
                _setup_known_state(path, scenario)
                count = _measure_real_write_count(path, scenario, target)
                if scenario == "multi_page_txn":
                    # Exhausting every boundary here means every one of the
                    # ~140 cases re-runs the full 35,000-insert loop (no-steal
                    # means nothing writes until COMMIT, regardless of where
                    # crash_at falls) -- sample instead, the same trade-off
                    # the plan itself makes for test_crash_during_recovery.
                    n_samples = 8
                    step = max(1, count // n_samples)
                    sampled = sorted(set(range(1, count + 1, step)) | {1, count})
                    triples.extend((scenario, target, n) for n in sampled)
                else:
                    triples.extend((scenario, target, n) for n in range(1, count + 1))
    return triples


def _sync_boundaries(scenarios: tuple[str, ...]) -> list[tuple[str, str, int]]:
    """The sync-boundary equivalent of _write_boundaries."""
    triples: list[tuple[str, str, int]] = []
    with tempfile.TemporaryDirectory() as tmp, pytest.MonkeyPatch.context() as mp:
        for scenario in scenarios:
            for target in ("db", "journal"):
                path = pathlib.Path(tmp) / f"{scenario}-{target}-sync.db"
                _setup_known_state(path, scenario)
                count = _measure_real_sync_count(path, scenario, target, mp)
                triples.extend((scenario, target, n) for n in range(1, count + 1))
    return triples


def _assert_recovered_state_is_consistent(
    db_path: pathlib.Path, before_rows: Sequence[tuple[object, ...]]
) -> None:
    """The five checks every crash point must survive (week5-transactions.md
    §33): state is unmixed, every b-tree is structurally valid, no index
    disagrees with its table, a real sqlite3 binary agrees the file is
    sound, and no journal is left behind.
    """
    db = quilldb.connect(str(db_path))

    # check 1: state is unmixed -- this harness never reaches STATE_AFTER
    # (db writes only ever happen post-barrier, and journal.delete() has no
    # write() call for FaultyFile to fault, so every reachable crash point
    # rolls all the way back to STATE_BEFORE).
    assert sorted(db.execute("SELECT * FROM t").fetchall()) == before_rows

    # check 2 + 3: every b-tree is structurally valid, and no index
    # disagrees with its table.
    for table in db.catalog.list_tables():
        validate_btree(pager=db.pager, pool=db.pool, root=table.root_page)
        for index in db.catalog.indexes_for(table=table.name):
            validate_index_btree(
                pager=db.pager, pool=db.pool, root=index.root_page, n_key_columns=len(index.columns)
            )

    # check 4
    db.close()
    result = subprocess.run(
        ["sqlite3", str(db_path), "PRAGMA integrity_check;"], capture_output=True, text=True, check=False
    )
    assert result.stdout.strip() == "ok"

    # check 5
    assert not db_path.with_name(db_path.name + "-journal").exists()


def _run_write_boundary_case(
    tmp_path: pathlib.Path, scenario: str, target: str, crash_at: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "t.db"
    _setup_known_state(path, scenario)

    db = quilldb.connect(str(path))
    before_rows = sorted(db.execute("SELECT * FROM t").fetchall())
    db.close()

    with pytest.raises(SimulatedCrash):
        _run_scenario(path, scenario, target=target, crash_at=crash_at, monkeypatch=monkeypatch)

    # Recovery runs here, inside connect() -- nobody calls it directly.
    _assert_recovered_state_is_consistent(path, before_rows)


@pytest.mark.parametrize("scenario,target,crash_at", _write_boundaries(_FAST_SCENARIOS))
def test_atomic_at_every_write_boundary(
    tmp_path: pathlib.Path, scenario: str, target: str, crash_at: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run_write_boundary_case(tmp_path, scenario, target, crash_at, monkeypatch)


@pytest.mark.slow
@pytest.mark.parametrize("scenario,target,crash_at", _write_boundaries(_SLOW_SCENARIOS))
def test_atomic_at_every_write_boundary_full_matrix(
    tmp_path: pathlib.Path, scenario: str, target: str, crash_at: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run_write_boundary_case(tmp_path, scenario, target, crash_at, monkeypatch)


@pytest.mark.parametrize("scenario,target,sync_at", _sync_boundaries(_FAST_SCENARIOS))
def test_atomic_at_every_sync_boundary(
    tmp_path: pathlib.Path, scenario: str, target: str, sync_at: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "t.db"
    _setup_known_state(path, scenario)

    db = quilldb.connect(str(path))
    before_rows = sorted(db.execute("SELECT * FROM t").fetchall())
    db.close()

    with pytest.raises(SimulatedCrash):
        _run_scenario_sync_fault(path, scenario, target=target, sync_at=sync_at, monkeypatch=monkeypatch)

    _assert_recovered_state_is_consistent(path, before_rows)


@pytest.mark.parametrize("scenario", list(SCENARIOS))
def test_no_fault_commits_cleanly_and_leaves_no_journal(tmp_path: pathlib.Path, scenario: str) -> None:
    """The negative the plan insists on (§33, 'also assert the negative'):
    a matrix that only ever crashes proves nothing if the no-crash case was
    never actually exercised, for ANY of the six scenarios.
    """
    path = tmp_path / "t.db"
    _setup_known_state(path, scenario)

    db = quilldb.connect(str(path))
    db.execute("BEGIN")
    SCENARIOS[scenario](db)
    db.execute("COMMIT")
    db.close()
    assert not (tmp_path / "t.db-journal").exists()

    db = quilldb.connect(str(path))
    for table in db.catalog.list_tables():
        validate_btree(pager=db.pager, pool=db.pool, root=table.root_page)
    db.close()


def _crash_mid_transaction(path: pathlib.Path, *, crash_at: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """Get a hot journal onto disk and then die -- the FIRST of chapter 14
    §14.5's two crashes. Faults the db file specifically: every db-target
    write happens after commit_barrier() (Pager.write_page's assertion
    forbids it earlier), so any crash_at here is guaranteed to leave the
    journal already hot on disk, not merely begun.
    """
    with pytest.raises(SimulatedCrash):
        _run_scenario(path, "single_insert", target="db", crash_at=crash_at, monkeypatch=monkeypatch)


def _connect_with_faulty_recovery(path: pathlib.Path, *, fail_at_write: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mirror connect()'s own construction order (Pager.open(), then
    recover_if_needed() -- see api/connection.py) but with a FaultyFile
    already wired into the pager BEFORE recovery runs: the window a SECOND
    crash, mid-recovery, would land in. Never reaches BufferPool
    construction, same as recover_if_needed()'s own real call site.
    """
    pager = Pager.open(path)
    real_file = pager._file
    pager._file = FaultyFile(real_file, fail_at_write=fail_at_write)  # type: ignore[assignment]
    patch_fsync(monkeypatch, target_fd=real_file.fileno(), fail_at_sync=None)
    recover_if_needed(path, pager)


def _measure_recovery_write_count() -> int:
    """How many .write() calls recover_if_needed() itself makes against the
    db file, replaying a hot single_insert journal -- the honest source for
    `second`'s range below, same reasoning as _measure_real_write_count.
    Independent of exactly which db-write crash_at produced the hot
    journal: by the time commit_barrier() has passed, the journal's own
    contents are already complete, so replay() has the same work to do
    regardless of how far the db-write phase got before the first crash.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "measure-recovery.db"
        _setup_known_state(path, "single_insert")
        with pytest.MonkeyPatch.context() as mp:
            _crash_mid_transaction(path, crash_at=1, monkeypatch=mp)

        pager = Pager.open(path)
        faulty = FaultyFile(pager._file, fail_at_write=None)
        pager._file = faulty  # type: ignore[assignment]
        recover_if_needed(path, pager)
        return faulty.writes


_RECOVERY_WRITE_COUNT = _measure_recovery_write_count()


@pytest.mark.slow
@pytest.mark.parametrize("first", [1, 2])  # single_insert's db target has exactly 2 real writes (measured).
@pytest.mark.parametrize("second", range(1, _RECOVERY_WRITE_COUNT + 1))
def test_crash_during_recovery(
    tmp_path: pathlib.Path, first: int, second: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chapter 14 §14.5 -- the case that finds the two worst ordering bugs:
    crash once mid-transaction (leaving a hot journal), then crash AGAIN
    while recovery is itself replaying/truncating/syncing/deleting that
    same journal. If recovery's own steps aren't in the right order, or
    aren't themselves safe to run twice, this is where it shows up.

    TODO(human): implement the test body below. Two helpers are ready to
    use:
      - `_crash_mid_transaction(path, crash_at=first, monkeypatch=monkeypatch)`
        gets a hot journal onto disk (raises SimulatedCrash itself, already
        wrapped in pytest.raises internally -- just call it).
      - `_connect_with_faulty_recovery(path, fail_at_write=second,
        monkeypatch=monkeypatch)` runs recover_if_needed() with a SECOND
        fault wired into the (still-hot) db file. This one raises
        SimulatedCrash too, but YOU need to wrap that call in
        `pytest.raises(SimulatedCrash)` yourself.

    After both crashes, do one final CLEAN `quilldb.connect(str(path))` (no
    fault this time -- recovery runs a third time, for real) and check the
    database converged anyway: query `t` and compare against `before_rows`,
    and run `validate_btree` over every table in `db.catalog.list_tables()`
    (both already used the same way in _assert_recovered_state_is_consistent
    above, which you're welcome to call directly instead of repeating the
    checks by hand).

    What should the rows in `t` actually be? This scenario is
    single_insert, and STATE_AFTER is unreachable in this harness (see
    _assert_recovered_state_is_consistent's check-1 comment) -- so what
    does that imply here, even with a SECOND crash in the middle of undoing
    the first one?
    """
    path = tmp_path / "t.db"
    _setup_known_state(path, "single_insert")
    db = quilldb.connect(str(path))
    before_rows = sorted(db.execute("SELECT * FROM t").fetchall())
    db.close()

    _crash_mid_transaction(path, crash_at=first, monkeypatch=monkeypatch)

    with pytest.raises(SimulatedCrash):
        _connect_with_faulty_recovery(path, fail_at_write=second, monkeypatch=monkeypatch)

    # Recovery runs a third time here, for real -- no fault this time.
    _assert_recovered_state_is_consistent(path, before_rows)

    db = quilldb.connect(str(path))  # sanity: can open and close without crashing

    after_rows = sorted(db.execute("SELECT * FROM t").fetchall())

    assert after_rows == before_rows

    for tbl in db.catalog.list_tables():
        validate_btree(pager=db.pager, pool=db.pool, root=tbl.root_page)

    db.close()
