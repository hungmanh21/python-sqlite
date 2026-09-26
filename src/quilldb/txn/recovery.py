"""Automatic hot-journal recovery -- the reason nobody ever calls a repair
tool by hand (chapter 14 SS14.0).

A HOT journal is a journal file that exists when no transaction is in
progress: the previous process died between commit_barrier() and the final
journal.delete() of Transaction.commit()/rollback(). Its existence alone is
the crash signal -- there is no other bookkeeping, because the crashed
process never got a chance to write any.

quilldb is single-process/single-writer this week (week 6 adds real
locking), so the two-processes-race-to-recover and stale-lock hazards
chapter 14 SS14.2 describes don't apply yet: recover_if_needed() is simply
called once, synchronously, before anything else touches the file.
"""

from pathlib import Path

from quilldb.storage.pager import Pager
from quilldb.txn.journal import Journal


def recover_if_needed(db_path: Path | None, pager: Pager) -> bool:
    """Roll back a hot journal if one is present. Returns True if it
    recovered (replayed and/or deleted a journal), False if there was
    nothing to do.

    Called from connect(), after Pager.open()/create() and BEFORE the
    BufferPool is constructed -- see week5-transactions.md SS"Where it goes
    in connect()". Putting it there makes the ordering structural rather
    than a comment: there is no page cache to invalidate here because
    there is no cache yet.

    Returns False immediately for an in-memory database (db_path is None):
    nothing survives the process, so there is nothing to recover.

    Steps (chapter 14 SS14.3), once a journal file is confirmed present:
      1. magic absent, or nRec == 0 -> the journal never became valid, or
         validly describes zero pages -- delete it, change nothing.
      2. otherwise it's HOT: replay() every record it can (stopping at the
         first bad checksum is correct, not an error -- SS14.3 step 5,
         "a prefix of an undo log is itself a valid undo log"), THEN
         truncate the pager back to the journal's own recorded
         page_count_before, THEN fsync the database, THEN delete the
         journal -- in that exact order. Restore-before-truncate and
         fsync-before-unlink are rule 3 of the week-5 contract, and they
         bind recovery exactly as hard as they bind commit().
      3. Finish with pager.reload_header(): recovery just changed
         page_count and possibly the freelist head underneath the header
         object connect() already built (SS14.3 step 10, "the cache is
         suspect").

    Must be idempotent: a crash mid-recovery just leaves the journal hot
    again (or, past truncate()+sync(), leaves it with nothing left to
    restore), and the next call is safe either way -- every journal record
    is an assignment (SS14.2), so replaying the same prefix twice is
    harmless.
    """
    if db_path is None:
        return False
    journal = Journal(db_path)
    if not journal.exists():
        return False

    journal.open_for_recovery()

    if not journal.is_hot():
        journal.delete()
        return True
    journal.replay(pager=pager)
    pager.truncate(journal.page_count_before())
    pager.sync()
    journal.delete()
    pager.reload_header()
    return True