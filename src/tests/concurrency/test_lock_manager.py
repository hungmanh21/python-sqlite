"""Session 1 (week6-concurrency.md SS35): compatibility, FIFO acquire/
release, per-resource conditions. No deadlock cases here -- those need
_detect_deadlock, which is session 2 -- see test_deadlock.py once it exists.
"""

import threading
import time

import pytest

from quilldb.errors import LockTimeoutError
from quilldb.txn.locks import LockManager, LockMode


@pytest.fixture
def lm() -> LockManager:
    return LockManager()


def _wait_until_queued(lm: LockManager, resource: str, txn_id: int, timeout: float = 2.0) -> None:
    """Block until `txn_id` shows up in `resource`'s waiter list.

    Thread *creation* isn't synchronous -- `Thread.start()` returns before the
    new thread has necessarily run a single line of Python, so a fixed sleep
    (e.g. `event.wait(0.1)`) to mean "the other thread has definitely reached
    its acquire() call" is a race, not a guarantee. Poll the manager's actual
    state instead of guessing a delay long enough to usually work.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        entry = lm._locks.get(resource)
        if entry is not None:
            with entry.condition:
                if any(waiter == txn_id for waiter, _ in entry.waiters):
                    return
        time.sleep(0.005)
    raise AssertionError(f"txn {txn_id} never queued for {resource!r} within {timeout}s")


def _wait_until_holder(lm: LockManager, resource: str, txn_id: int, timeout: float = 2.0) -> None:
    """Block until `txn_id` shows up in `resource`'s holders.

    Same reasoning as `_wait_until_queued`: poll actual state rather than
    guess a delay. This one also sidesteps NOTES.md's B6-1 -- a test that
    proved "granted" by racing a short `acquire(timeout=...)` against wall
    clock is exactly the pattern that turned out to be flaky on this
    environment, independent of anything `LockManager` does.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        entry = lm._locks.get(resource)
        if entry is not None:
            with entry.condition:
                if txn_id in entry.holders:
                    return
        time.sleep(0.005)
    raise AssertionError(f"txn {txn_id} never became a holder of {resource!r} within {timeout}s")


def test_shared_locks_coexist(lm: LockManager) -> None:
    lm.acquire(1, "users", LockMode.SHARED)
    lm.acquire(2, "users", LockMode.SHARED)  # must not block


def test_exclusive_excludes(lm: LockManager) -> None:
    lm.acquire(1, "users", LockMode.EXCLUSIVE)
    with pytest.raises(LockTimeoutError):
        lm.acquire(2, "users", LockMode.SHARED, timeout=0.1)


def test_reentrant_acquire_does_not_block(lm: LockManager) -> None:
    lm.acquire(1, "users", LockMode.EXCLUSIVE)
    lm.acquire(1, "users", LockMode.EXCLUSIVE)  # same txn, must not deadlock on itself
    lm.acquire(1, "users", LockMode.SHARED)  # held EXCLUSIVE already covers SHARED


def test_release_all_wakes_waiters(lm: LockManager) -> None:
    lm.acquire(1, "users", LockMode.EXCLUSIVE)
    granted = threading.Event()

    def acquire_and_signal() -> None:
        lm.acquire(2, "users", LockMode.SHARED)
        granted.set()

    threading.Thread(target=acquire_and_signal, daemon=True).start()
    assert not granted.wait(0.1)
    lm.release_all(1)
    assert granted.wait(2.0)  # generous: this asserts progress, not latency


def test_waiters_are_fifo_so_nobody_starves(lm: LockManager) -> None:
    """A stream of readers must not indefinitely postpone a waiting writer
    (chapter 16 SS16.5's writer starvation, in our own lock manager).

    Reader 1 holds SHARED. Writer 2 queues for EXCLUSIVE and must block
    behind reader 1. A flood of later readers (3..7) must then queue behind
    the *writer* rather than jumping ahead of it just because SHARED is
    compatible with SHARED -- otherwise the writer never gets a turn.

    Every blocking `acquire()` here uses `timeout=None` (block until granted,
    never on a clock) and progress is proven by polling `LockManager`'s own
    internal state with a generous bound, not by racing a short timeout
    against wall-clock threading -- see NOTES.md B6-1. A finite timeout on
    the acquire() calls themselves ties correctness to this environment's
    clock behaving, which it demonstrably doesn't always do here; `None`
    removes that dependency for the property this test actually cares about
    (grant *order*), leaving only the polling bounds below exposed to it.
    """
    lm.acquire(1, "accounts", LockMode.SHARED)

    errors: list[LockTimeoutError] = []

    def run(txn_id: int, mode: LockMode) -> None:
        try:
            lm.acquire(txn_id, "accounts", mode, timeout=None)
        except LockTimeoutError as exc:  # surface it via `errors`, not lost in the thread
            errors.append(exc)

    poll_timeout = 15.0  # generous: see NOTES.md B6-1 on this environment's clock

    writer = threading.Thread(target=run, args=(2, LockMode.EXCLUSIVE), daemon=True)
    writer.start()
    _wait_until_queued(lm, "accounts", 2, timeout=poll_timeout)

    late_reader_ids = list(range(3, 8))
    readers = [
        threading.Thread(target=run, args=(txn_id, LockMode.SHARED), daemon=True)
        for txn_id in late_reader_ids
    ]
    for reader, txn_id in zip(readers, late_reader_ids):
        reader.start()
        _wait_until_queued(lm, "accounts", txn_id, timeout=poll_timeout)

    entry = lm._locks["accounts"]
    with entry.condition:
        assert entry.waiters[0][0] == 2, "the writer must stay at the front of the queue"
        assert 2 not in entry.holders  # reader 1 still holds -- writer not granted yet

    lm.release_all(1)
    _wait_until_holder(lm, "accounts", 2, timeout=poll_timeout)  # writer goes next, not the flood

    with entry.condition:
        assert not (set(late_reader_ids) & entry.holders.keys()), (
            "a later reader was granted before the queued writer -- FIFO is broken"
        )

    lm.release_all(2)
    for txn_id in late_reader_ids:
        _wait_until_holder(lm, "accounts", txn_id, timeout=poll_timeout)  # no one starves forever

    for reader in readers:
        reader.join(timeout=poll_timeout)
    writer.join(timeout=poll_timeout)
    assert not errors, errors
