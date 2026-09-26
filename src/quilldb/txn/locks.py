"""Table-level shared/exclusive locks, strict two-phase locking, deadlock
detection via a wait-for graph.

Session map (docs/implementation/week6-concurrency.md, "Week 6 sessions"):
  session 1 (this file, first pass): LockMode, LockManager skeleton --
    compatibility, FIFO acquire/release, one Condition per resource. No
    deadlock detection yet -- a blocked acquire() can only time out.
  session 2: the wait-for graph and _detect_deadlock's cycle walk, wired
    into acquire()'s wait loop (marked below).

See docs/concurrency.md for the isolation argument this machinery exists to
make true, and docs/theory/txn/16-locking-and-deadlock.md for the design
reasoning -- in particular SS16.3 (locks vs. latches: the LockManager's own
_latch guards _locks the dict, never the resource-level wait) and SS16.5
(why a stream of readers must not indefinitely postpone a waiting writer,
which is what the FIFO "no waiters ahead" check below prevents).
"""

import threading
import time
from dataclasses import dataclass, field
from enum import Enum

from quilldb.errors import DeadlockError, LockTimeoutError


class LockMode(Enum):
    SHARED = 1
    EXCLUSIVE = 2


COMPATIBLE = {
    (LockMode.SHARED, LockMode.SHARED): True,
    (LockMode.SHARED, LockMode.EXCLUSIVE): False,
    (LockMode.EXCLUSIVE, LockMode.SHARED): False,
    (LockMode.EXCLUSIVE, LockMode.EXCLUSIVE): False,
}


@dataclass
class LockEntry:
    holders: dict[int, LockMode] = field(default_factory=dict)
    waiters: list[tuple[int, LockMode]] = field(default_factory=list)  # FIFO
    condition: threading.Condition = field(default_factory=threading.Condition)


class LockManager:
    """One per Database, shared by every Connection's transactions.

    `_latch` protects `_locks` itself (adding a new resource's entry) -- it
    is a latch, not a lock, per SS16.3: held for microseconds, never across a
    wait. Once an entry exists, its own `condition` (which carries its own
    internal lock) protects that entry's `holders`/`waiters`.
    """

    def __init__(self, default_timeout: float = 5.0) -> None:
        self._locks: dict[str, LockEntry] = {}
        self._latch = threading.Lock()
        self._waiting_for: dict[int, str] = {}  # txn_id -> resource it wants
        self._default_timeout = default_timeout

    def _entry_for(self, resource: str) -> LockEntry:
        with self._latch:
            entry = self._locks.get(resource)
            if entry is None:
                entry = LockEntry()
                self._locks[resource] = entry
            return entry

    def acquire(
        self,
        txn_id: int,
        resource: str,
        mode: LockMode,
        timeout: float | None = None,
    ) -> None:
        """Block until granted.

        Reentrant: a txn already holding a sufficient mode on `resource`
        returns immediately (holding EXCLUSIVE already satisfies a SHARED
        request). There is no lock upgrade path here on purpose -- quilldb
        sidesteps it with the single global "__writer__" lock a writer takes
        before any table EXCLUSIVE (docs/concurrency.md, "Why a single
        writer"), so acquire() never needs to turn a held SHARED into an
        EXCLUSIVE.

        FIFO: a request queues behind any earlier waiter on this resource
        even when it would be compatible with the current holders. Without
        this, a steady stream of SHARED requests can keep a waiting
        EXCLUSIVE request from ever being granted -- writer starvation
        (chapter 16 SS16.5), a liveness bug no correctness test catches.

        Raises:
            DeadlockError: session 2 wires this in -- see the TODO in
                _detect_deadlock and the call site marked below.
            LockTimeoutError: the busy_timeout backstop expired first.
        """
        timeout = self._default_timeout if timeout is None else timeout
        deadline = None if timeout is None else time.monotonic() + timeout
        entry = self._entry_for(resource)

        with entry.condition:
            held = entry.holders.get(txn_id)
            if held is mode or held is LockMode.EXCLUSIVE:
                return  # already sufficient

            def grantable() -> bool:
                if entry.waiters and entry.waiters[0][0] != txn_id:
                    return False  # someone queued ahead of us -- FIFO
                return all(
                    COMPATIBLE[(mode, other_mode)]
                    for holder, other_mode in entry.holders.items()
                    if holder != txn_id
                )

            if grantable():
                entry.holders[txn_id] = mode
                # No one else can be waiting here (grantable() only takes this
                # branch when entry.waiters is empty), but notify_all() is
                # free on an empty waiter set -- see the note below on why
                # every successful grant does this.
                entry.condition.notify_all()
                return

            entry.waiters.append((txn_id, mode))
            try:
                while not grantable():
                    self._waiting_for[txn_id] = resource
                    detected = self._detect_deadlock(txn_id)
                    if detected is not None:
                        victim, cycle = detected
                        if victim == txn_id:
                            raise DeadlockError(victim, cycle)
                        # The walk found a real cycle, but it closed on some
                        # node other than txn_id -- txn_id is merely queued
                        # behind one of the cycle's participants, not part of
                        # the cycle itself. Raising here would abort an
                        # innocent transaction while leaving the actual cycle
                        # untouched (its members hold no lock txn_id is
                        # waiting on). The real victim's own acquire() call
                        # walks the same graph starting from itself, finds
                        # itself at index 0, and raises for itself instead.
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        raise LockTimeoutError(
                            f"txn {txn_id} timed out waiting for {mode.name} "
                            f"on {resource!r}"
                        )
                    # Re-check deadline next loop, don't trust wait()'s return (NOTES.md B6-1).
                    entry.condition.wait(timeout=remaining)
                entry.holders[txn_id] = mode
                # A grant changes what's grantable for whoever is now next in
                # the FIFO queue (this txn just left entry.waiters' front, or
                # a SHARED grant just added a compatible holder). Only
                # release_all() used to call notify_all(); when N readers
                # queue behind each other with no intervening release, the
                # single notify_all() that woke this queue only lets the
                # front waiter through; without this, everyone behind them
                # sleeps forever waiting for a notify that never comes
                # (NOTES.md B6-1 -- caught once the FIFO test stopped using
                # short timeouts, which had been masking it by re-polling on
                # their own schedule regardless of notification).
                entry.condition.notify_all()
            finally:
                entry.waiters.remove((txn_id, mode))
                self._waiting_for.pop(txn_id, None)

    def release_all(self, txn_id: int) -> None:
        """Drop every lock held by this transaction and wake each resource's
        waiters. Called ONLY at commit/rollback -- there is deliberately no
        release_one(): releasing a lock mid-transaction is exactly what
        strict 2PL forbids (chapter 16 SS16.2), because it reopens the
        cascading-abort problem strict 2PL exists to close.
        """
        with self._latch:
            entries = list(self._locks.values())

        for entry in entries:
            with entry.condition:
                if entry.holders.pop(txn_id, None) is not None:
                    entry.condition.notify_all()

        self._waiting_for.pop(txn_id, None)

    def _detect_deadlock(
        self, waiting_txn: int
    ) -> tuple[int, list[tuple[int, str, int]]] | None:
        """Follow wait-for edges from `waiting_txn`. If the walk returns to
        `waiting_txn`, that's a cycle; return (victim, cycle) where victim is
        the YOUNGEST txn in the cycle -- highest id, since ids increase,
        which also guarantees the oldest transaction in the system can never
        be perpetually aborted (chapter 16 SS16.4) -- and cycle is the edges
        walked to find it, for DeadlockError's message. Otherwise None.

        Called only at the moment a transaction blocks (see the TODO in
        acquire()'s wait loop), because that's the only moment a new edge
        appears -- a background scanner would do the same work later, never
        earlier.
        """
        def _walk(
            txn_id: int, path: list[int], path_edges: list[tuple[int, str, int]]
        ) -> tuple[int, list[tuple[int, str, int]]] | None:
            if txn_id in path:
                i = path.index(txn_id)  # where the loop closes
                return max(path[i:]), path_edges[i:]  # youngest txn, full cycle

            resource = self._waiting_for.get(txn_id)
            if resource is None:
                return None  # no outgoing edge, so no cycle
            entry = self._locks.get(resource)
            if entry is None:
                return None  # no holders, so no cycle

            path.append(txn_id)
            for holder in entry.holders:
                path_edges.append((txn_id, resource, holder))
                found = _walk(holder, path, path_edges)
                if found is not None:
                    return found
                path_edges.pop()  # dead end -- backtrack, try the next holder
            path.pop()
            return None

        return _walk(waiting_txn, [], [])