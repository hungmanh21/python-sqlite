"""The rollback journal: original page images, so a transaction can be undone.

Format (chapter 13 §13.5 — SQLite's layout, quilldb's magic):

    HEADER, padded to SECTOR_SIZE:
      0   8  magic            JOURNAL_MAGIC, but written as ZEROS initially
      8   4  nRec             record count, written as ZERO initially
      12  4  checksum nonce   random per journal
      16  4  page_count_before  truncate back to this on rollback
      20  4  sector size
      24  4  page size

    RECORDS, immediately after the header, no padding:
      [page_id u32][original page image, PAGE_SIZE bytes][checksum u32]

The magic and nRec are withheld until the body is synced, so a torn journal is
rejected two independent ways. See §13.6 — this is the single most important
property of the format.

ONE SEGMENT ONLY, deliberately. Real SQLite journals hold many headers, one
per mid-transaction spill (chapter 13 §13.5). quilldb forbids spilling
outright — see docs/implementation/week5-transactions.md §0.1 — so there is
exactly one header at offset 0 and `commit_barrier()` is called exactly once
per transaction. If you ever relax no-steal, this is the first thing that has
to change, and it changes the format.

Session map (docs/implementation/week5-transactions.md, "Week 5 sessions"):
  session 1: header layout, record layout, journal_checksum, begin(),
    record_original().
  session 2: commit_barrier(), replay(), delete().
  session 3+: nothing here yet calls begin()/record_original()/commit_barrier()
    for a real transaction -- that's Transaction, in txn/transaction.py.
"""

import io
import os
import random
from pathlib import Path
from typing import BinaryIO

from quilldb.constants import JOURNAL_MAGIC, PAGE_SIZE, SECTOR_SIZE, SyncMode
from quilldb.storage.pager import Pager


def journal_checksum(page: bytes, nonce: int) -> int:
    """SQLite's sampled additive sum (chapter 13 §13.7).

    Not a CRC: it reads every 200th byte from the end, 20 bytes out of 4096.
    Designed to catch torn writes cheaply, not adversarial corruption.

    Contract (see test_journal.py for the exact cases this must satisfy):
      - Start an accumulator at `nonce`.
      - Walk backward from PAGE_SIZE - 200 in steps of -200 (i.e. indices
        PAGE_SIZE-200, PAGE_SIZE-400, ... down to but not below 0), adding
        `page[i]` at each stop.
      - Return the accumulator masked to 32 bits (`& 0xFFFFFFFF`).

    This is deliberately a weak checksum — it's documented as a known
    weakness, not a bug, by test_checksum_only_samples_every_200th_byte.
    """
    accumulator = nonce
    for i in range(PAGE_SIZE - 200, -1, -200):
        accumulator += page[i]
    return accumulator & 0xFFFFFFFF


class Journal:
    def __init__(self, db_path: Path | None, sync_mode: SyncMode = SyncMode.FULL) -> None:
        """The journal lives at db_path with '-journal' appended.

        db_path is None for an in-memory database (Pager.memory() sets
        _path = None): self.path stays None and begin() backs the journal
        with an io.BytesIO instead of a real file. Every fsync is then a
        no-op and delete() just drops the buffer — rollback still works
        exactly as it does on disk, only recovery is meaningless, since the
        database dies with the process. (Session 0 §0.2 task 6.)
        """
        self.path: Path | None = (
            None if db_path is None else db_path.with_name(db_path.name + "-journal")
        )
        self.sync_mode = sync_mode
        self._file: BinaryIO | None = None  # opened by begin()
        self._nonce = 0
        self._nrec = 0  # counts record_original() calls; commit_barrier() (session 2) writes this

    def begin(self, page_count_before: int) -> None:
        """Create the file and write the header with magic and nRec BOTH ZERO.

        Opens self.path with "x+b" (the same exclusive-create idiom as
        Pager.create() — "does a journal already exist" becomes atomic
        instead of a check-then-open race), or an io.BytesIO() for the
        in-memory case. Picks this journal's nonce once, here —
        record_original() reuses it for every checksum. Bytes beyond offset
        28 stay zero padding out to SECTOR_SIZE.

        Raises:
            FileExistsError: a journal already exists — that's a hot journal or
                a bug, and either way this must not overwrite it.
        """
        self._file = self.path.open("x+b") if self.path else io.BytesIO()
        self._nonce = random.getrandbits(32)
        header = bytearray(SECTOR_SIZE)
        header[12:16] = self._nonce.to_bytes(4, "big")
        header[16:20] = page_count_before.to_bytes(4, "big")   # offset 16, page_count_before
        header[20:24] = SECTOR_SIZE.to_bytes(4, "big")   # offset 20, SECTOR_SIZE
        header[24:28] = PAGE_SIZE.to_bytes(4, "big")   # offset 24, PAGE_SIZE
        self._file.write(header)
        self._file.flush()

    def record_original(self, page_id: int, data: bytes) -> None:
        """Append one record. Data must be the page as it was BEFORE any
        modification in this transaction.

        Must be idempotent per page_id within a transaction: recording the same
        page twice wastes I/O. The caller (Transaction, session 3) tracks that
        — this method does not need to check.

        Raises:
            ValueError: len(data) != PAGE_SIZE.
        """
        assert self._file is not None, "record_original() called before begin()"
        if len(data) != PAGE_SIZE:
            raise ValueError(f"data length {len(data)} != PAGE_SIZE {PAGE_SIZE}")

        self._file.seek(0, io.SEEK_END)
        self._file.write(page_id.to_bytes(4, "big"))
        self._file.write(data)
        self._file.write(journal_checksum(data, self._nonce).to_bytes(4, "big"))
        self._file.flush()
        self._nrec += 1

    def commit_barrier(self) -> None:
        """Make the journal valid. Nothing may touch the database before this.

            fsync(journal)                    # body durable
            fsync(directory)                  # the journal's NAME durable  (§13.4)
            write magic + nRec, 12 bytes @ 0  # one write, inside one sector
            fsync(journal)                    # NOW it is valid

        At SyncMode.NORMAL, both fsyncs before the header write still happen —
        only the SECOND journal fsync is skipped, since the per-record
        checksums cover for it (§13.8). At OFF, all three fsyncs are skipped,
        but the header write itself still happens: replay()'s magic/nRec
        check is what makes a journal "committed" at all, independent of
        sync level.

        In-memory (self.path is None): there is no OS file or directory to
        fsync, so every fsync step is a no-op regardless of sync_mode — but
        the header write still happens, so replay() behaves identically to
        the on-disk case.

        The magic and nRec are written together in one write() call — that's
        the "inside one sector" property: a torn write can't leave magic
        right and nRec wrong.
        """
        assert self._file is not None, "commit_barrier() called before begin()"
        self._file.flush()
        if self.sync_mode != SyncMode.OFF and self.path is not None:
            os.fsync(self._file.fileno())

            # fsync the parent directory, not the journal file itself (§13.4)
            dir_fd = os.open(str(self.path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)

        self._file.seek(0)
        self._file.write(JOURNAL_MAGIC + self._nrec.to_bytes(4, "big"))
        self._file.flush()

        if self.sync_mode == SyncMode.FULL and self.path is not None:
            os.fsync(self._file.fileno())

    def replay(self, pager: Pager) -> int:
        """Roll back. Returns the number of pages restored.

        Stops at the first record whose checksum fails — that's the boundary
        between what reached disk and what didn't, not an error (§14.3).
        Returns 0 if the magic is absent or nRec is 0.

        Restores through pager.restore_page(), NOT pager.write_page() — a
        page number here can be above the pager's current page_count (chapter
        14 §14.5 bug 2), and restore_page() is the one that tolerates that.
        """
        assert self._file is not None, "replay() called before begin()"
        self._file.seek(0)
        header = self._file.read(SECTOR_SIZE)
        if header[0:8] != JOURNAL_MAGIC:
            return 0
        nrec = int.from_bytes(header[8:12], "big")
        if nrec == 0:
            return 0
        nonce = int.from_bytes(header[12:16], "big")

        restored_count = 0
        record_size = 4 + PAGE_SIZE + 4
        for i in range(nrec):
            record_offset = SECTOR_SIZE + i * record_size
            self._file.seek(record_offset)
            record_data = self._file.read(record_size)
            page_id = int.from_bytes(record_data[0:4], "big")
            data = record_data[4:4 + PAGE_SIZE]
            stored_checksum = int.from_bytes(record_data[4 + PAGE_SIZE:4 + PAGE_SIZE + 4], "big")
            if journal_checksum(data, nonce) != stored_checksum:
                break
            pager.restore_page(page_id, data)
            restored_count += 1

        return restored_count

    def delete(self) -> None:
        """Unlink. THE COMMIT POINT. The caller must have fsynced the database
        already.

        In-memory (self.path is None): just drops the buffer — there was
        never anything on disk to unlink.
        """
        assert self._file is not None, "delete() called before begin()"
        if self.path is None:
            self._file = None
        else:
            self._file.close()
            self.path.unlink()
