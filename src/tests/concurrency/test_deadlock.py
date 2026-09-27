"""Session 2 (week6-concurrency.md SS36): the wait-for graph and
_detect_deadlock's cycle walk. A crossed lock order must produce exactly one
DeadlockError and one survivor -- never a hang, never both aborted, never
both granted. See docs/concurrency.md, "Deadlocks are detected, not
prevented."

The week-6 plan sketches these tests against a `db` fixture (a full
Connection, taking locks implicitly per statement) -- that wiring is session
5's job. Until then, these drive LockManager directly, the same way
test_lock_manager.py does.
"""

import threading
import time

import pytest

from quilldb.errors import DeadlockError, LockTimeoutError
from quilldb.txn.locks import LockManager, LockMode


@pytest.fixture
def lm() -> LockManager:
    return LockManager()


def _wait_until_waiting_for(lm: LockManager, txn_id: int, resource: str, timeout: float = 2.0) -> None:
    """Block until `txn_id` has recorded a wait-for edge onto `resource`.

    Needed (not just _wait_until_queued) because _detect_deadlock reads
    `_waiting_for` directly: a crossing acquire() must not run its cycle
    check until the other side's edge actually exists, or detection would
    depend on a timing race instead of the graph itself.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if lm._waiting_for.get(txn_id) == resource:
            return
        time.sleep(0.005)
    raise AssertionError(f"txn {txn_id} never recorded a wait-for edge onto {resource!r} within {timeout}s")


def _wait_until_queued(lm: LockManager, resource: str, txn_id: int, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        entry = lm._locks.get(resource)
        if entry is not None:
            with entry.condition:
                if any(waiter == txn_id for waiter, _ in entry.waiters):
                    return
        time.sleep(0.005)
    raise AssertionError(f"txn {txn_id} never queued for {resource!r} within {timeout}s")


def _provoke_crossed_deadlock(
    lm: LockManager, txn_a: int, txn_b: int, resource_a: str, resource_b: str, timeout: float = 5.0
) -> dict[int, BaseException | None]:
    """A holds `resource_a`, B holds `resource_b`; A then wants B's resource
    and B then wants A's resource -- the classic crossed lock order (chapter
    16 SS16.4). Returns {txn_id: None-on-success-or-the-raised-exception},
    with the victim's locks already released so the survivor could proceed.
    """
    lm.acquire(txn_a, resource_a, LockMode.EXCLUSIVE)
    lm.acquire(txn_b, resource_b, LockMode.EXCLUSIVE)

    results: dict[int, BaseException | None] = {}

    def run(txn_id: int, resource: str) -> None:
        try:
            lm.acquire(txn_id, resource, LockMode.EXCLUSIVE, timeout=timeout)
            results[txn_id] = None
        except (DeadlockError, LockTimeoutError) as exc:
            results[txn_id] = exc

    t_a = threading.Thread(target=run, args=(txn_a, resource_b), daemon=True)
    t_a.start()
    _wait_until_waiting_for(lm, txn_a, resource_b, timeout=timeout)

    # By now A's wait-for edge exists, so B's acquire below finds the cycle
    # on its very first check -- deterministic, not a race.
    t_b = threading.Thread(target=run, args=(txn_b, resource_a), daemon=True)
    t_b.start()
    t_b.join(timeout=timeout + 1)

    victim = next(txn for txn, result in results.items() if isinstance(result, DeadlockError))
    lm.release_all(victim)  # simulates the rollback a real caller does on DeadlockError

    t_a.join(timeout=timeout + 1)
    t_b.join(timeout=timeout + 1)
    return results


def test_deadlock_is_detected_and_one_side_commits(lm: LockManager) -> None:
    """The important assertion is the second one."""
    results = _provoke_crossed_deadlock(lm, 1, 2, "users", "orders")
    assert sum(isinstance(r, DeadlockError) for r in results.values()) == 1
    assert sum(r is None for r in results.values()) == 1  # the other MUST succeed


def test_deadlock_error_names_the_cycle(lm: LockManager) -> None:
    results = _provoke_crossed_deadlock(lm, 1, 2, "users", "orders")
    error = next(r for r in results.values() if isinstance(r, DeadlockError))
    assert len(error.cycle) == 2
    assert {resource for _, resource, _ in error.cycle} == {"users", "orders"}


def test_deadlock_through_shared_holders_is_still_detected(lm: LockManager) -> None:
    """Regression: _detect_deadlock must explore every SHARED holder of a
    resource, not stop at whichever one a dict happens to yield first.
    """
    lm.acquire(10, "table", LockMode.SHARED)  # dead end -- tried first, waits on nothing
    lm.acquire(5, "table", LockMode.SHARED)  # this one closes the cycle

    results: dict[int, BaseException | None] = {}

    def run() -> None:
        try:
            lm.acquire(5, "other", LockMode.EXCLUSIVE, timeout=5.0)
            results[5] = None
        except (DeadlockError, LockTimeoutError) as exc:
            results[5] = exc

    lm.acquire(99, "other", LockMode.EXCLUSIVE)
    t5 = threading.Thread(target=run, daemon=True)
    t5.start()
    _wait_until_waiting_for(lm, 5, "other")

    # txn 99 wants "table" EXCLUSIVE, incompatible with BOTH holders. The
    # walk must not give up after the dead-end holder (10) -- it has to
    # reach holder 5, whose own wait on "other" (held by 99) closes the loop.
    with pytest.raises(DeadlockError) as excinfo:
        lm.acquire(99, "table", LockMode.EXCLUSIVE, timeout=5.0)
    assert excinfo.value.victim == 99

    lm.release_all(99)
    t5.join(timeout=5.0)
    assert results[5] is None


def test_bystander_queued_behind_a_cycle_is_not_raised_as_its_victim(lm: LockManager) -> None:
    """Regression: a transaction merely queued behind one participant of an
    unrelated cycle must not have DeadlockError raised in ITS OWN thread just
    because its own re-check of the wait-for graph happens to observe that
    cycle. _detect_deadlock(root) can return a victim other than `root` --
    the walk closes wherever the cycle actually is, not necessarily back at
    the node it started from. Only a cycle's actual members may raise for
    themselves; acquire() guards this with `if victim == txn_id` before
    raising (locks.py).

    B holds X and wants Y; C holds Y and wants X -- B<->C is genuinely
    deadlocked. Bystander A only wants X (queued behind B) and has no part
    in that cycle -- A must time out normally, never receive someone else's
    DeadlockError.
    """
    lm._entry_for("X").holders[2] = LockMode.EXCLUSIVE  # B holds X
    lm._entry_for("Y").holders[3] = LockMode.EXCLUSIVE  # C holds Y
    lm._waiting_for[2] = "Y"  # B wants Y (held by C)
    lm._waiting_for[3] = "X"  # C wants X (held by B) -- closes the B<->C cycle
    lm._waiting_for[1] = "X"  # A wants X too -- same edge acquire() records

    # Sanity: the manufactured cycle genuinely doesn't route through the
    # bystander -- otherwise this test would not exercise the guard at all.
    detected = lm._detect_deadlock(1)
    assert detected is not None
    victim, _ = detected
    assert victim != 1

    with pytest.raises(LockTimeoutError):
        lm.acquire(1, "X", LockMode.EXCLUSIVE, timeout=0.3)


def test_upgrade_blocked_by_another_reader_is_not_a_deadlock(lm: LockManager) -> None:
    """The upgrade problem (week6-concurrency.md SS35, "the upgrade problem,
    which the roadmap doesn't mention and which will bite you"): txn 1 reads
    "accounts" (SHARED), txn 2 also reads it (SHARED) -- an unrelated,
    perfectly ordinary concurrent reader -- then txn 1 wants to write it
    (EXCLUSIVE). Txn 1 is legitimately blocked by txn 2's SHARED hold; txn 2
    isn't waiting on anything and will simply finish and release. That's
    ordinary contention, not a cycle -- the "__writer__" global lock
    (week6-concurrency.md's recommended sidestep) only rules out two
    DIFFERENT transactions racing to upgrade the same table; it says nothing
    about a transaction's own prior SHARED hold on the table it's now
    escalating on.

    Regression: txn 1's own SHARED hold on "accounts" must not create a
    wait-for edge back to itself. _detect_deadlock's walk, given a resource
    whose holders include the waiting transaction itself (exactly what an
    upgrade looks like), must skip that self-entry rather than recursing
    into it -- recursing finds txn_id already in `path` one level down and
    reports a deadlock against itself, even though the real (and only)
    blocker, txn 2, isn't part of any cycle at all.
    """
    lm.acquire(1, "accounts", LockMode.SHARED)
    lm.acquire(2, "accounts", LockMode.SHARED)

    results: dict[int, BaseException | None] = {}

    def upgrade() -> None:
        try:
            lm.acquire(1, "accounts", LockMode.EXCLUSIVE, timeout=2.0)
            results[1] = None
        except (DeadlockError, LockTimeoutError) as exc:
            results[1] = exc

    t = threading.Thread(target=upgrade, daemon=True)
    t.start()

    # Not _wait_until_waiting_for: under the bug, _detect_deadlock raises
    # within the very acquire() call that sets the wait-for edge, and
    # acquire()'s `finally` clears that edge again before this could ever
    # observe it "set" -- there is no window where it reliably persists.
    # A short sleep is the honest wait here: give the (buggy) detector every
    # opportunity to have already run and raised.
    time.sleep(0.2)
    assert 1 not in results  # still legitimately waiting on txn 2, not deadlocked

    lm.release_all(2)  # the real reader finishes
    t.join(timeout=2.0)
    assert results.get(1) is None


def test_blocked_wait_without_a_cycle_is_not_a_deadlock(lm: LockManager) -> None:
    """A plain block (no reverse edge) must not be mistaken for a cycle --
    it should just wait, then get granted once the holder releases.
    """
    lm.acquire(1, "table", LockMode.EXCLUSIVE)

    granted = threading.Event()

    def run() -> None:
        lm.acquire(2, "table", LockMode.EXCLUSIVE, timeout=5.0)
        granted.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    _wait_until_queued(lm, "table", 2)

    assert not granted.wait(0.2)  # blocked, not raised, not granted
    lm.release_all(1)
    assert granted.wait(2.0)
