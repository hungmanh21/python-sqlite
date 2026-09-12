# 14 — Crash Recovery


> **Read during week 5, before the crash matrix.** Covers `txn/recovery.py` and the fault-injection
> framework.
>
> **Time:** ~40 minutes. **Prerequisite:** [chapter 13](13-atomic-commit-and-the-journal.md).


---


## 14.0 The problem: nobody is going to call `recover()`


Chapter 13 built a journal that makes the previous state restorable. Now the process is dead. The user
runs your program again tomorrow:


```python
db = quilldb.connect("app.db")
print(db.execute("SELECT count(*) FROM users").fetchone())
```


There is no `db.repair()` in that snippet, and there never will be — users don't run recovery tools,
and a database that requires one after every crash is a database nobody trusts. **Recovery has to be
automatic, and it has to happen before the first read, and the first read is `connect()`.**


Which raises the question this chapter answers: **when you open a file, how do you know whether it needs
recovering?** You have no memory of last time. All you have is the bytes on disk.


---


## 14.1 Hot journals: how you know something went wrong


A **hot journal** is a journal file that exists when no transaction is in progress. Its existence is the
evidence: §13.4's commit deletes the journal as its last act, so a surviving journal means the process
died between "started writing" and "committed."


That's the idea. The implementation has four wrinkles, and each is a real bug if you skip it.


**1. "The file exists" is necessary, not sufficient.** A journal can exist and be meaningless:


| What you find | What it means | Do |
|---|---|---|
| No journal file | clean shutdown | nothing |
| Journal, magic absent | never became valid (§13.6) | delete it, change nothing |
| Journal, magic present, `nRec = 0` | describes zero pages | delete it, change nothing |
| Journal, magic present, `nRec > 0` | **hot** | roll back |
| Zero-length journal | created, nothing written | delete it |


Three of those five rows say "delete it, change nothing." **The common case for a surviving journal is
that there is nothing to do** — which is exactly the outcome §13.6's guards were designed to produce.


**2. A journal is not hot if someone is actively using it.** A running process mid-transaction has a
journal on disk that must not be rolled back by anyone else. This is what the lock states of chapter 16
are for: a would-be recoverer checks whether any other connection holds a write lock. Without the
check, opening a second connection while the first is committing would corrupt the database that was
being written correctly.


**3. Two processes can both spot the same hot journal.** Both start rolling it back, interleaving page
writes. SQLite's answer is a lock protocol — and it contains a beautiful detail worth stealing:


> "Do not acquire a RESERVED lock because that would make other processes think the journal was no
> longer hot."


Recovery takes PENDING and EXCLUSIVE but deliberately **skips** RESERVED, because RESERVED is the signal
"a normal write transaction is in progress," and a second process seeing it would conclude the journal
belongs to a live writer and leave it alone — permanently. **The recoverer must not disguise itself as a
writer.** That's the kind of interaction between two mechanisms that only shows up when both exist, and
it's a good illustration of why lock states carry *meaning* rather than just exclusion.


**4. Recovery must itself be crash-safe**, which is §14.2.


> **Say this out loud:** "A hot journal is a journal that exists when nobody is writing. On open I check
> for one, validate its magic and record count, and roll it back if it's real. Most surviving journals
> turn out to have nothing to replay, because the format is designed to be born invalid — so the common
> crash case is 'delete the journal and carry on.' And recovery deliberately doesn't take the lock a
> normal writer takes, because that would make other connections think the journal was live."


---


## 14.2 Replay must be idempotent — and undo logging gets that for free


You are rolling back 25 pages. You crash after 12. Someone opens the database again. The journal is
still there, still hot, and recovery starts over from record 1 — re-writing the 12 pages it already
wrote.


**Is that safe?** It has to be, because you cannot bound how many times it happens.


For an undo journal the answer is yes, structurally, and the reason is worth stating precisely:


> **Every journal record is an assignment, not a modification.** "Page 7's contents are these 4096
> bytes." Applying an assignment twice is the same as applying it once. There is no state to be confused
> about.


Compare a hypothetical journal of *deltas* — "add 5 to the integer at offset 100," or "insert this cell
into page 7." Applying that twice gives the wrong answer, so replay would need to know what it had
already done, which means per-page bookkeeping surviving the crash. That's what a **log sequence number**
is: a version stamp on each page so redo can ask "has this page already seen this log record?" ARIES
needs LSNs precisely because its log records aren't idempotent.


**You get to skip all of it, because whole-page assignment is idempotent.** This is the second property
that chapter 13 §13.10 got for free by storing whole pages rather than deltas — the first was torn-page
immunity. One representational choice, two hard problems avoided.


**The design lesson generalizes well beyond databases:** *make your recovery operations idempotent and
you don't need to track what you've done.* It's the same reason HTTP `PUT` is easier to retry than
`POST`, and the reason "declare the desired state" beats "apply this change" in configuration
management. Idempotency converts an exactly-once problem into an at-least-once problem, and at-least-once
is dramatically cheaper.


---


## 14.3 The recovery algorithm


```
on open:
  1. Is there a journal file?              no  -> done
  2. Does another connection hold a write lock?   yes -> not hot, done
  3. Take PENDING, then EXCLUSIVE. (NOT RESERVED — §14.1)
  4. Read the journal header.
        magic absent, or nRec == 0         -> goto 8
  5. For each record, in order:
        verify the checksum                fails -> STOP replaying here
        write the original page image back into the database
  6. Truncate the database to the header's original page count.
  7. fsync the database.                   <-- do not skip this
  8. Delete the journal.
  9. fsync the directory (optional; see §13.8).
 10. Release the locks. Re-read the file header; the cache is suspect.
```


Four steps to look at twice.


**Step 5's "stop replaying here."** A checksum failure is not an error to report — it's the boundary
between the records that reached the disk and the records that didn't. Stop, and roll back what you
have. This is safe for a reason specific to undo journals: **a prefix of an undo log is itself a valid
undo log.** Restoring pages 1–12 to their old contents and leaving pages 13–25 with their new contents
would be inconsistent *if the transaction had committed*, but it didn't — and any page you failed to
restore is a page whose *new* content also never got written, because §13.4 forbids writing database
pages before the journal is fully synced. That ordering constraint is what makes partial replay
correct.


**Step 6 comes before step 7 and both come before step 8.** Truncating restores the original file
length (§13.4). Then fsync, for exactly the same reason commit needs one: deleting the journal while the
restored pages sit in the page cache destroys your only copy of them.


**Step 10's "the cache is suspect."** Any in-memory page cache you were holding describes a file that
just changed underneath you. Discard it. And re-read the 100-byte file header, since recovery may have
changed the page count and the freelist head.


### What it looks like when it works


A real run: a process was killed mid-transaction after a cache spill had already pushed uncommitted
pages into the database file.


```
BEFORE recovery:  the db file contains 4,286 occurrences of the uncommitted
                  string 'MODIFIED-'   |  34 pages  |  139,264 bytes


  $ sqlite3 hot.db "SELECT count(*) FROM t;"        <- just opening it recovers
  5000
  $ sqlite3 hot.db "SELECT v FROM t WHERE id=1;"
  orig-00000001                                     <- pre-image restored
  $ sqlite3 hot.db "SELECT count(*) FROM t WHERE v LIKE 'MODIFIED%';"
  0
  $ sqlite3 hot.db "SELECT count(*) FROM t WHERE id=999999;"
  0                                                 <- uncommitted INSERT gone


AFTER recovery:   0 occurrences  |  28 pages  |  114,688 bytes  |  journal gone
                  PRAGMA integrity_check -> ok
```


Two things to notice. **Uncommitted data really was in the database file** — 4,286 instances of it —
because a transaction bigger than the page cache must spill dirty pages to disk before it commits. The
file was genuinely wrong, and recovery genuinely fixed it. And the file was truncated to *exactly* 28
pages, the number in the journal header, which is step 6 doing its job: 28 × 4096 = 114,688.


---


## 14.4 Turning a durability claim into evidence


"I implemented a journal" is a claim. Anyone can say it. Here is how you make it a fact.


**Fault injection.** Wrap the file object so it can fail on demand:


```python
class FaultyFile:
    """Fails deterministically at write #N or fsync #N."""
    def __init__(self, real, fail_at_write=None, fail_at_sync=None): ...
    def write(self, data):  # raises SimulatedCrash when this is write `fail_at_write`
    def flush_and_sync(self):  # raises SimulatedCrash when this is sync `fail_at_sync`
```


**Then parametrize over every fault point:**


```python
@pytest.mark.parametrize("crash_at", range(1, MAX_WRITES))
def test_atomic_at_every_crash_point(tmp_path, crash_at):
    setup_known_state(tmp_path)
    with pytest.raises(SimulatedCrash):
        run_transaction(tmp_path, fail_at_write=crash_at)
    db = quilldb.connect(tmp_path / "db")               # triggers recovery
    assert read_state(db) in (STATE_BEFORE, STATE_AFTER)   # never in between
    assert validate_all_btrees(db).is_valid
    assert index_matches_table(db)                      # the week-4 property
```


A few hundred generated cases, each asserting that the database is **entirely** pre-commit or
**entirely** post-commit. That is the highest-value four hours in the roadmap, and it's what separates
this project from every other toy database on GitHub. It is also exactly the kind of thing an interviewer
will happily spend ten minutes digging into.


**Do the same for fsync boundaries, not just writes.** A missing fsync doesn't change *which* bytes get
written, only *when*, so `fail_at_write` can never detect it. `fail_at_sync` at least verifies that the
sync happens where you think it does.


### What the crash matrix does not prove


Be honest about this in `docs/durability.md`, because someone will ask and volunteering it is much
stronger than being caught by it:


| Your test does | Reality also does | Detectable by you? |
|---|---|---|
| stops at a write boundary | stops **mid-write**, tearing a sector | ✗ needs a block-level fault injector |
| leaves the page cache intact | loses the page cache entirely | ✗ needs real power loss or a VM snapshot |
| assumes `fsync` worked | some drives lie about `fsync` | ✗ needs hardware instrumentation |
| tests your journal logic | can corrupt the journal itself | partly — flip a byte and assert it's refused |


**The honest framing:** *"My crash matrix proves atomicity holds at every write and fsync boundary my
code controls. It does not simulate torn sectors or a lost page cache — those need block-level fault
injection or real power cuts. What it does catch is every ordering bug in my own commit sequence, which
is where the bugs actually were."*


That's a stronger sentence than "it's crash-safe," because it's checkable.


---


## 14.5 The case that finds real bugs: crashing during recovery


Recovery writes pages and syncs. It can be interrupted. SQLite tests this explicitly, and you should
too, because it's where §14.2's idempotency stops being theory:


```python
@pytest.mark.parametrize("first_crash", range(1, MAX_WRITES))
@pytest.mark.parametrize("recovery_crash", range(1, MAX_RECOVERY_WRITES))
def test_crash_during_recovery(tmp_path, first_crash, recovery_crash):
    crash_mid_transaction(tmp_path, at=first_crash)
    with pytest.raises(SimulatedCrash):
        connect_with_faulty_file(tmp_path, fail_at_write=recovery_crash)   # dies recovering
    db = quilldb.connect(tmp_path / "db")                                  # recovers again
    assert read_state(db) in (STATE_BEFORE, STATE_AFTER)
    assert validate_all_btrees(db).is_valid
```


**The two bugs this finds, and they're both easy to write:**


1. **Deleting the journal before the restored pages are durable** — i.e. steps 7 and 8 in the wrong
   order. Crash between them and you've discarded the only copy of the original pages while the database
   still holds new ones. Recovery is no longer possible *at all*. This is the single worst bug available
   in week 5, and only this test finds it.
2. **Truncating before restoring** — if you truncate to 28 pages first and a page you still need to
   restore was number 30, its journal record now writes past the end of a shorter file. Depending on your
   pager's bounds checks, that's either an exception during recovery or silent file growth.


The nested parametrization is quadratic, so cap it: a handful of first-crash points × all
recovery-crash points is enough, and mark it slow so it runs in CI rather than on every save.


---


## 14.6 What recovery cannot fix


Recovery restores the *previous committed state*. It is not a repair tool, and the distinction matters:


- **A lying `fsync`.** If the drive acknowledges a sync it hasn't performed, every guarantee here is
  void. Chapter 00 §0.7 covers this; the mitigation is hardware you trust, and the honest
  acknowledgement is a line in your docs.
- **Torn sectors on hardware that doesn't do atomic sector writes.** Your journal is immune for *its
  own* pages (§13.10), but a torn write to the journal *header* in a non-sector-aligned format wouldn't
  be — which is why §13.5's padding matters.
- **A bug in your journal writer.** If you journalled the *modified* page instead of the original, every
  crash test passes — because rollback faithfully restores the broken state. **Your tests verify that
  rollback restores what the journal says; they cannot verify the journal said the right thing.** The
  guard against this is the hash test: hash the file before `BEGIN`, hash after `ROLLBACK`, assert equal.
  That one is worth writing before anything else in week 5, because it's the only test that checks the
  journal's *contents* rather than its mechanics.
- **Data you never committed.** Rollback is working correctly when your uncommitted work vanishes.


---


## 14.7 Roads not taken


### `fsck`-style repair


Scan the whole file, find structural damage, patch it up. **Used by:** filesystem repair tools, and
`PRAGMA integrity_check` is the read-only half of the idea. **Why it isn't recovery:** repair guesses. It
can make a file *readable* without making it *correct* — a rebuilt tree missing rows is structurally
perfect and silently wrong. Journal-based recovery never guesses; it restores a state that provably
existed. **"Readable" and "correct" are different goals**, and conflating them is how people lose data
while believing they recovered it.


### Checksums on every database page


Store a checksum in each page and verify on read, so corruption is *detected* rather than interpreted.
**Used by:** PostgreSQL (optional), InnoDB, ZFS. **Why SQLite doesn't by default:** there's nowhere free
to put it — every byte of a page is defined by the format — so it needs a reserved-bytes-per-page region
(the header field you set to zero in week 1) and it costs CPU on every read. SQLite offers it as an
opt-in extension. **Worth knowing that the header field you refused to support is exactly the mechanism
this would need.**


### Replication instead of recovery


Keep a second copy on another machine and fail over. **Used by:** every production database above a
certain size. **Why it's the real answer to durability:** recovery protects against process and power
failure; nothing local protects against the disk itself, or the datacentre. **Why it's not in scope:**
SQLite is an embedded library — the application *is* the server — and replication is a different
product. But it's the right thing to say when asked "how would you make this production-ready": *"I'd
stop trying to make one copy safer and add a second copy."*


---


## 14.8 What you're building


```python
def recover_if_needed(path: Path, pager: Pager) -> bool:
    """Roll back a hot journal if one is present. Returns True if it recovered.


    Called from connect(), before the first read. Idempotent — calling it twice
    on the same file must be safe, and after a crash mid-recovery it WILL be.


    Steps in §14.3. The two orderings that matter: fsync the database BEFORE
    deleting the journal, and restore pages BEFORE truncating.
    """
```


**Write these tests in this order.** The first one is the one that validates your journal's *contents*
rather than your recovery's *mechanics*, and it's cheap:


1. `test_rollback_restores_byte_identical_file` — hash before `BEGIN`, hash after `ROLLBACK`.
2. `test_journal_is_absent_after_commit`.
3. `test_journal_with_zero_nrec_changes_nothing` — construct one by hand.
4. `test_journal_with_no_magic_changes_nothing`.
5. `test_flipped_byte_in_a_record_is_refused` — corrupt one byte, assert the record is not replayed.
6. `test_recovery_truncates_a_grown_file` — a transaction that split pages, then crash.
7. The crash matrix over writes.
8. The crash matrix over fsyncs.
9. Crash during recovery.
10. `sqlite3 ... PRAGMA integrity_check` after every one of the above.


Number 10 is nearly free and it is the reason the format decision paid off: after every single crash
point, a C implementation written by other people confirms your file is structurally sound.


---


## 14.9 Check yourself


1. Why must recovery happen inside `connect()` rather than in a repair tool?
2. Give the five things you might find where a journal file exists, and what each means. Which is the
   most common after a crash?
3. Why does hot-journal recovery deliberately *not* take the lock a normal writer takes?
4. You crash 12 pages into a 25-page rollback. Why is starting over safe?
5. What's the property that makes undo replay idempotent, and what would a delta-based journal need
   instead?
6. Why can recovery stop at the first bad checksum instead of failing? What ordering rule makes a
   partial rollback correct?
7. Why must the database be fsynced before the journal is deleted, in *recovery* as well as in commit?
8. Why truncate the file, and what would happen if you truncated before restoring?
9. After recovery, why is the in-memory page cache suspect?
10. Name two things the crash matrix proves and two it can't.
11. Which single test would catch "I journalled the modified page instead of the original," and why do
    none of the crash tests catch it?
12. What's the worst possible ordering bug in week 5, and which test finds it?
13. Why is `fsck`-style repair not a substitute for journal-based recovery?
14. Page checksums would detect corruption your journal can't prevent. Why doesn't SQLite have them on
    by default, and which header field would they need?


---


## 14.10 Sources


- [Atomic Commit In SQLite](https://www.sqlite.org/atomiccommit.html) §4 — hot journals, automatic
  detection on open, and §4.4 on truncating the database back to the size recorded in the journal
  header.
- [File Locking And Concurrency In SQLite Version 3](https://www.sqlite.org/lockingv3.html) — the
  recovery lock sequence, and "Do not acquire a RESERVED lock because that would make other processes
  think the journal was no longer hot."
- [`PRAGMA integrity_check`](https://www.sqlite.org/pragma.html#pragma_integrity_check) — the read-only
  structural validator, and what it does and doesn't examine.
- **The recovery transcript in §14.3** was produced on `sqlite3` 3.37.2 by killing a Python process
  mid-transaction with a deliberately small page cache (`PRAGMA cache_size=10`) so that uncommitted
  pages really did spill into the database file; the before/after counts, page counts and byte sizes are
  measured. The `MODIFIED-` string count is the direct evidence that a database file can contain
  uncommitted data.


---


**Next:** [15 — Isolation and the anomalies](15-isolation-and-anomalies.md) — what actually goes wrong
when two transactions overlap, before you write a single lock.