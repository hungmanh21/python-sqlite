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
        # The wait-for graph, plus victims chosen by SOME OTHER waiter's
        # detection pass (txn_id -> the cycle that condemned it). Both are
        # guarded by _latch, and _detect_deadlock only ever runs under it,
        # so a walk sees one consistent snapshot of who waits for what.
        self._waiting_for: dict[int, str] = {}  # txn_id -> resource it wants
        self._doomed: dict[int, list[tuple[int, str, int]]] = {}
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
        request). A held SHARED can be upgraded to EXCLUSIVE -- grantable()
        ignores the requester's own hold -- and the single global
        "__writer__" lock a writer takes before any table EXCLUSIVE
        (docs/concurrency.md, "Why a single writer") guarantees two
        DIFFERENT transactions never race to upgrade the same table.

        FIFO: a request queues behind any earlier waiter on this resource
        even when it would be compatible with the current holders. Without
        this, a steady stream of SHARED requests can keep a waiting
        EXCLUSIVE request from ever being granted -- writer starvation
        (chapter 16 SS16.5), a liveness bug no correctness test catches.

        Raises:
            DeadlockError: this txn is the youngest in a wait-for cycle --
                found either by its own detection pass or by another
                waiter's (which then wakes it; NOTES.md B6-5).
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
                    victim_resource: str | None = None
                    with self._latch:
                        doomed_cycle = self._doomed.pop(txn_id, None)
                        if doomed_cycle is not None:
                            raise DeadlockError(txn_id, doomed_cycle)
                        self._waiting_for[txn_id] = resource
                        detected = self._detect_deadlock(txn_id)
                        if detected is not None:
                            victim, cycle = detected
                            if victim == txn_id:
                                raise DeadlockError(victim, cycle)
                            # The cycle is real but its youngest member is
                            # someone else -- possibly txn_id closed it, and
                            # the victim has been asleep in its own wait()
                            # since BEFORE the cycle existed. Nothing else
                            # will ever wake it (no lock in the cycle can be
                            # released), so doom it and wake it here; left
                            # alone, it only noticed at its own timeout, and
                            # whichever side's timer fired first lost --
                            # sometimes the OLDER txn, with LockTimeoutError
                            # on a genuine cycle (NOTES.md B6-5).
                            if victim not in self._doomed:
                                self._doomed[victim] = cycle
                                victim_resource = self._waiting_for.get(victim)
                    if victim_resource is not None:
                        self._wake(victim_resource, entry)
                        continue  # _wake may have dropped our condition -- re-check grantable()
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        raise LockTimeoutError(
                            f"txn {txn_id} timed out waiting for {mode.name} "
                            f"on {resource!r}"
                        )
                    # Re-check deadline next loop, don't trust wait()'s return (NOTES.md B6-1).
                    entry.condition.wait(timeout=remaining)
                entry.holders[txn_id] = mode
            finally:
                entry.waiters.remove((txn_id, mode))
                # Leaving the queue -- granted, timed out, or doomed --
                # changes what's grantable for whoever is now at its front,
                # and nothing else will tell them. Granted: only
                # release_all() used to notify, so N readers queued behind
                # each other with no intervening release woke one at a time
                # and the rest slept forever (NOTES.md B6-1). Timed out or
                # doomed: the leaver may have been the FIFO head blocking
                # everyone behind it (NOTES.md B6-6). notify_all() is free
                # when nobody is waiting.
                entry.condition.notify_all()
                with self._latch:
                    self._waiting_for.pop(txn_id, None)
                    self._doomed.pop(txn_id, None)

    def _wake(self, resource: str, held: LockEntry) -> None:
        """notify_all() on `resource`'s condition, so a doomed victim asleep
        there re-runs its wait loop and finds itself in self._doomed.

        The caller holds `held.condition`. Taking a SECOND entry's condition
        while holding one is a lock-order inversion waiting to happen (two
        detectors each waking the other's resource), so drop ours first and
        take it back afterwards -- the same thing wait() does anyway.
        """
        target = self._entry_for(resource)
        if target is held:
            held.condition.notify_all()
            return
        held.condition.release()
        try:
            with target.condition:
                target.condition.notify_all()
        finally:
            held.condition.acquire()

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

        with self._latch:
            self._waiting_for.pop(txn_id, None)
            self._doomed.pop(txn_id, None)

    def _detect_deadlock(
        self, waiting_txn: int
    ) -> tuple[int, list[tuple[int, str, int]]] | None:
        """Follow wait-for edges from `waiting_txn`. If the walk returns to
        `waiting_txn`, that's a cycle; return (victim, cycle) where victim is
        the YOUNGEST txn in the cycle -- highest id, since ids increase,
        which also guarantees the oldest transaction in the system can never
        be perpetually aborted (chapter 16 SS16.4) -- and cycle is the edges
        walked to find it, for DeadlockError's message. Otherwise None.

        Called only at the moment a transaction blocks (acquire()'s wait
        loop), because that's the only moment a new edge appears -- a
        background scanner would do the same work later, never earlier.

        Caller must hold self._latch: every read of _waiting_for below, and
        every snapshot of another resource's holders, has to come from one
        consistent moment, or the walk can iterate a holders dict that
        another thread is resizing ("dictionary changed size during
        iteration") or chase edges that no longer exist.
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
            # tuple(): a snapshot -- this entry's holders are mutated under
            # its own condition, which the walk deliberately doesn't take.
            for holder in tuple(entry.holders):
                # A txn upgrading SHARED -> EXCLUSIVE is its own holder here.
                # That's not a wait-for edge (nobody waits behind their own
                # hold), and following it reports a self-deadlock
                # (NOTES.md, week 6 session 6).
                if holder == txn_id:
                    continue
                path_edges.append((txn_id, resource, holder))
                found = _walk(holder, path, path_edges)
                if found is not None:
                    return found
                path_edges.pop()  # dead end -- backtrack, try the next holder
            path.pop()
            return None

        return _walk(waiting_txn, [], [])