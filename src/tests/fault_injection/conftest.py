"""The fault injector (week5-transactions.md SS32) and the plumbing that
wires it onto a live Connection's actual file objects.

Two design notes from the plan that separate a useful injector from a
decorative one:

  1. Fail BEFORE performing the operation, not after. "Crash after the
     write succeeded" is a much weaker test -- the interesting states are
     the ones where a write you believed happened didn't.
  2. Count writes and syncs SEPARATELY, and inject into BOTH files. The
     journal and the database are different file objects; a crash matrix
     that only faults the database never tests journal-write failures.

A third wrinkle this codebase's own shape adds, that the plan's toy
FaultyFile glosses over: Pager.sync() and Journal.commit_barrier() call
`os.fsync(fd)` directly on a bare integer descriptor, not through a method
on the file object -- so wrapping `.write()` (which IS a method call
through `self._file`) catches every write fault, but catching a SYNC fault
needs `os.fsync` itself monkeypatched, matched back to the right file by
comparing file descriptors. `patch_fsync` below does that.
"""

from __future__ import annotations

import os
from typing import BinaryIO

import pytest

from quilldb.errors import SimulatedCrash


class FaultyFile:
    """Wraps a real, already-open file object. Every write is counted from
    1; when the count reaches `fail_at_write`, the write raises
    SimulatedCrash instead of happening -- the wrapped file never sees it.

    Every other method (`flush`, `fileno`, `seek`, `read`, `truncate`,
    `close`) passes straight through: Pager and Journal only ever dirty a
    page or append a journal record through `.write()`, so that is the only
    method that needs to know about faults.
    """

    def __init__(self, real_file: BinaryIO, *, fail_at_write: int | None = None) -> None:
        self._real = real_file
        self.writes = 0
        self.fail_at_write = fail_at_write

    def write(self, data: bytes) -> int:
        self.writes += 1
        if self.writes == self.fail_at_write:
            raise SimulatedCrash(f"write #{self.writes}")
        return self._real.write(data)

    def flush(self) -> None:
        self._real.flush()

    def fileno(self) -> int:
        return self._real.fileno()

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._real.seek(offset, whence)

    def read(self, size: int = -1) -> bytes:
        return self._real.read(size)

    def truncate(self, size: int | None = None) -> int:
        return self._real.truncate(size)

    def close(self) -> None:
        self._real.close()


def patch_fsync(
    monkeypatch: pytest.MonkeyPatch,
    *,
    target_fd: int,
    fail_at_sync: int | None,
) -> None:
    """Make os.fsync raise SimulatedCrash the `fail_at_sync`-th time it is
    called on `target_fd` specifically -- fsyncs on any other descriptor
    (the OTHER file, or anything pytest itself does) pass through untouched.

    Scoped to this one test via monkeypatch: os.fsync is a single shared
    module attribute, so this patches it process-wide for the duration of
    the test and monkeypatch restores the real function on teardown.
    """
    real_fsync = os.fsync
    counter = {"syncs": 0}

    def fake_fsync(fd: int) -> None:
        if fd == target_fd:
            counter["syncs"] += 1
            if counter["syncs"] == fail_at_sync:
                raise SimulatedCrash(f"sync #{counter['syncs']} on fd {fd}")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fake_fsync)
