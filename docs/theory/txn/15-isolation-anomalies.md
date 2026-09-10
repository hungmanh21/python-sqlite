# 15 — Isolation and the Anomalies


> **Read at the START of week 6, before you write a single lock.** This chapter is about *what* you're
> trying to guarantee; [chapter 16](16-locking-and-deadlock.md) is about the machinery. Building the
> machinery first is how you end up unable to say what your database guarantees.
>
> **Time:** ~40 minutes. **Prerequisite:** [chapter 13](13-atomic-commit-and-the-journal.md) (what a
> transaction is).


---


## 15.0 The problem: two threads, and $50 that stops existing


Here is a bank transfer, written correctly:


```python
def transfer(db, src, dst, amount):
    with db.transaction():
        bal = db.execute("SELECT balance FROM acct WHERE id=?", (src,)).fetchone()[0]
        db.execute("UPDATE acct SET balance=? WHERE id=?", (bal - amount, src))
        bal = db.execute("SELECT balance FROM acct WHERE id=?", (dst,)).fetchone()[0]
        db.execute("UPDATE acct SET balance=? WHERE id=?", (bal + amount, dst))
```


Account 1 has $100. Two threads each transfer $50 out of it, to different accounts.


```
        Thread A                          Thread B
  ──────────────────────────────    ──────────────────────────────
  read acct1.balance -> 100
                                    read acct1.balance -> 100
  write acct1.balance = 50
                                    write acct1.balance = 50
  write acct2 += 50
                                    write acct3 += 50
  COMMIT                            COMMIT
```


Final state: account 1 has **$50**. Accounts 2 and 3 each gained $50. **$50 appeared out of nothing.**


Every individual transaction was atomic and durable. Week 5's journal worked perfectly — neither
transaction was half-applied. The file passes `integrity_check`. **Atomicity and durability do not give
you correctness under concurrency**, and that gap is what isolation is.


Note what's *not* the problem: there's no crash, no corruption, no bug in your B-tree. The bug is that
B's read happened before A's write and B's write happened after it. Nothing local to either transaction
is wrong.


---


## 15.1 The four anomalies, as interleavings rather than vocabulary


The standard four have terrible names that obscure very simple pictures. Learn the pictures; the names
follow.


### Lost update


The one above. Two transactions read the same value, both compute from it, and the second write
obliterates the first.


```
A: read x=100    B: read x=100    A: write x=50    B: write x=50
                                                   └─ A's update is lost
```


**The tell:** any read-modify-write cycle. `x = x - 50` in application code is always a candidate.


### Dirty read — reading uncommitted data


```
A: write x=50
                 B: read x -> 50        ← reads A's uncommitted value
A: ROLLBACK
                 B: acts on a number that never existed
```


B saw a value that, after A's rollback, was never true at any point in the database's history. **This is
the only one of the four that week 5 already prevents for you** — a writer's changes live in its own
memory and journal until commit, so there is nothing for B to read.


### Non-repeatable read — the same row changes under you


```
A: read x -> 100
                 B: write x=50; COMMIT
A: read x -> 50        ← same query, same transaction, different answer
```


Sounds harmless until the transaction is `SELECT balance` … *validate* … `SELECT balance` … *act*. Any
check-then-act across two reads is broken by this.


### Phantom read — the same *query* returns different rows


```
A: SELECT count(*) FROM acct WHERE balance > 100   -> 5
                 B: INSERT INTO acct VALUES (99, 500); COMMIT
A: SELECT count(*) FROM acct WHERE balance > 100   -> 6      ← a row appeared
```


**Why this is genuinely harder than a non-repeatable read, and worth understanding:** to prevent a
non-repeatable read you lock the rows you read — there are finitely many and you know which. To prevent a
phantom you must lock **rows that do not exist yet**. You can't lock a row that isn't there, so you have
to lock the *predicate*, or the range, or the whole table. That's why phantoms are the last anomaly every
system fixes and the first one it documents as permitted.


> **This is why table-level locking is not the embarrassing choice it looks like.** A table lock prevents
> phantoms trivially, because the space where a phantom could appear is exactly what you locked.
> Row-level locking needs gap locks or predicate locks or MVCC to get the same property back.


---


## 15.2 What isolation actually means


The definition is stricter and simpler than most explanations make it:


> **A concurrent execution is serializable if its outcome is identical to *some* order of running those
> same transactions one at a time.**


Three things people get wrong about that:


1. **"Some" order, not the order they started in.** If A and B run concurrently and the result matches
   "all of B, then all of A," that is a correct serializable execution even if A started first. The
   system is not obliged to preserve arrival order — only to be *equivalent to* some order.
2. **It's a property of the outcome, not of the schedule.** Two transactions can interleave freely, and
   as long as no interleaving is *observable* in the result, it's serializable.
3. **It says nothing about which order you get.** Both serial orders are correct. If your application
   needs a specific one, that's sequencing, and isolation won't give it to you.


Serializability is the gold standard because it lets you reason about a transaction **as if it ran
alone** — which is the only way anyone can actually reason about concurrent code. The `transfer()`
function above is correct under that assumption. Its bug isn't in the function, it's in the assumption
being false.


---


## 15.3 The isolation levels, and why the ladder is a bit of a fraud


ANSI SQL defines four levels, defined by *which anomalies they permit*:


| Level | Dirty read | Lost update | Non-repeatable read | Phantom |
|---|---|---|---|---|
| Read uncommitted | ✅ allowed | ✅ | ✅ | ✅ |
| Read committed | ❌ prevented | ✅ | ✅ | ✅ |
| Repeatable read | ❌ | ❌ | ❌ | ✅ allowed |
| Serializable | ❌ | ❌ | ❌ | ❌ |


**Two criticisms worth being able to make**, because they show you've thought about it rather than
memorized the table:


**1. Defining a guarantee by a list of forbidden phenomena is a weak definition.** The list was derived
from the anomalies that lock-based implementations happened to exhibit, so it describes *implementation
artifacts* rather than a coherent property. The famous 1995 critique by Berenson et al. showed the
definitions are ambiguous and that they omit anomalies that real systems have — including **write skew**
(§15.5), which none of the four levels names.


**2. The level names don't mean the same thing across engines.** PostgreSQL's "repeatable read" is
actually snapshot isolation. Oracle's "serializable" is snapshot isolation. MySQL's default is repeatable
read implemented with gap locks that prevent some phantoms but not all. **So "we use repeatable read" is
not a portable statement about behaviour**, which is a genuinely useful thing to know when you're
debugging an application that moved databases.


The practical upshot for you: **name the anomalies your system permits, not the level.** A sentence like
"reads are not repeatable within a transaction because I release read locks after each statement" is
checkable. "We're read-committed" is a claim someone has to go and verify.


---


## 15.4 What SQLite provides, in its own words


> "Except in the case of shared cache database connections with `PRAGMA read_uncommitted` turned on, all
> transactions in SQLite show **"serializable"** isolation. SQLite implements serializable transactions
> by **actually serializing the writes**. There can only be a single writer at a time to an SQLite
> database. There can be multiple database connections open at the same time, and all of those database
> connections can write to the database file, but they have to **take turns**."


Note the scare quotes around "serializable" — they're SQLite's, not mine. And note the mechanism: not a
validation protocol, not multi-version timestamps. **It just doesn't let two writers run at once.** If
writes never interleave, no write-write anomaly can exist, so serializability is achieved by removing the
concurrency rather than by managing it.


That's a legitimate engineering answer and it's worth saying admiringly rather than dismissively: the
cheapest way to satisfy a hard property is often to eliminate the situation in which it's hard. SQLite is
a library embedded in one application; a single writer is usually plenty.


In WAL mode, the guarantee changes: *"In WAL mode, SQLite exhibits **"snapshot isolation"**."* Which is a
different thing, and §15.5 is about why.


### The caveat that catches everyone


Isolation is between **connections**, not between statements on one connection:


> "there is no isolation between operations that occur within the same database connection."


A query *"sees all changes that are completed on the same database connection prior to the start of the
query"* — **committed or not**. And for changes landing mid-statement, *"it is undefined whether or not
the query will see those changes,"* with the specific warnings that *"the query might return a changed row
more than once, or it might return a row that was previously deleted."*


Verified, and this is exactly the trap:


```
A: BEGIN; UPDATE t SET v='A' WHERE id=1        (not committed)
A: SELECT v FROM t WHERE id=1   ->  ('A',)     ← sees its own uncommitted write
B: SELECT v FROM t WHERE id=1   ->  ('v1',)    ← sees the pre-image
```


**The practical rule this produces:** never mutate a table while iterating a cursor over it on the same
connection. Chapter 10's `DeleteOp` warning — collect the rowids first, then delete — is this rule, and
now you know it isn't a quirk of your implementation. It's inherent, and SQLite documents it because
it's inherent there too.


---


## 15.5 Snapshot isolation, and the anomaly that isn't on the list


**Snapshot isolation** is what you get from MVCC: every transaction reads the database as it existed at
its start instant, and writes are checked at commit for conflicts with concurrent writes to the *same
rows*. It's what PostgreSQL calls "repeatable read," what Oracle calls "serializable," and what SQLite
provides in WAL mode.


It's a wonderful property. Readers never block writers and writers never block readers, because a reader
is looking at old versions nobody is modifying. Verified in WAL mode: a `SELECT` succeeded against a
4 MB in-flight write transaction that would have hard-blocked it in rollback-journal mode.


**And it is not serializable.** Here is the counterexample, and it's the best single piece of interview
content in this chapter, because almost everyone who says "we use snapshot isolation, so we're fine" has
not seen it.


### Write skew


A hospital rule: **at least one doctor must be on call at all times.** Two doctors, Alice and Bob, both
currently on call. Each independently decides to go home.


```
        Transaction A (Alice)                Transaction B (Bob)
  ─────────────────────────────────    ─────────────────────────────────
  SELECT count(*) WHERE on_call
      -> 2  ("fine, 2 > 1")
                                       SELECT count(*) WHERE on_call
                                           -> 2  ("fine, 2 > 1")
  UPDATE doctors SET on_call=0
      WHERE name='alice'
                                       UPDATE doctors SET on_call=0
                                           WHERE name='bob'
  COMMIT  ✅                             COMMIT  ✅
```


Both commit successfully. **Zero doctors are on call.**


Snapshot isolation's commit check looks for two transactions writing the *same row*. A wrote Alice's row,
B wrote Bob's row — no conflict. But each made its decision based on data the other invalidated, and
there is no serial order that produces this outcome: run A then B and B reads count=1 and refuses; run B
then A and A refuses.


**Why it slips through:** the transactions conflict on a *read*, not a write. A read a row B wrote and B
read a row A wrote. That's a read-write conflict cycle, and snapshot isolation only tracks write-write.


**None of the four ANSI anomalies describes this.** Not a dirty read, not a lost update (different rows),
not non-repeatable (each transaction read once), not a phantom (no row appeared). It's the concrete
demonstration that §15.3's ladder is incomplete — and the reason the Berenson critique exists.


> **Say this out loud:** "Snapshot isolation gives every transaction a consistent view as of its start
> and checks write-write conflicts at commit, so readers and writers never block each other. It isn't
> serializable, though — write skew slips through, where two transactions each read what the other
> writes and modify *different* rows, so there's no write conflict to detect. The on-call-doctor case is
> the canonical example. Getting true serializability on top of MVCC needs read tracking, which is what
> PostgreSQL's Serializable Snapshot Isolation adds."


---


## 15.6 What quilldb provides, and how to say it honestly


quilldb uses **table-level two-phase locking with a single writer** (chapter 16 builds it). Here is the
resulting guarantee, and the reasoning is worth following because it's a pleasant surprise:


- Shared (read) and exclusive (write) locks, **at table granularity**.
- Both held **until commit** — strict 2PL, no early release.
- One writer at a time overall.


**That is serializable, genuinely, and the phantom row is why.** Holding a shared lock on the *whole
table* means no other transaction can insert into it, so a repeated query cannot see a new row. The
anomaly that usually requires gap locks or predicate locks or MVCC to eliminate is eliminated for free by
coarse granularity. **Coarse locking buys the strongest isolation level most cheaply** — it just buys it
by allowing less concurrency.


So the claim to make is:


> *"Table-level strict two-phase locking with a single writer. That's serializable, including phantom
> protection, because a shared lock on the whole table means nobody can insert into the range I read. The
> price is concurrency: two transactions writing different rows of the same table serialize even though
> they don't conflict. Row-level locking would allow that, but then I'd need gap locks to get phantom
> protection back."*


And the comparison to SQLite, which is in your favour and should be stated that way:


> *"This is actually **finer**-grained than SQLite in rollback-journal mode, which locks the whole
> database file. I can afford finer granularity because I'm single-process with shared memory, so a lock
> is a mutex and a dict entry. SQLite coordinates between separate OS processes through filesystem locks,
> where per-table locking would mean many more syscalls and a much harder recovery story when a process
> dies holding one. Their coarser choice is correct for their constraint."*


**Demonstrating that you understand why someone else's design is coarser than yours is worth more than
claiming you beat them**, and it's the difference between sounding like you read a comparison table and
sounding like you understand the tradeoff.


### Do not skip writing this down


`docs/concurrency.md` must name the level **and list what it permits**. Two reasons: it's the artifact
that proves you thought about it, and — more usefully — writing it forces you to check. It is very common
to discover while writing this file that you release a read lock somewhere you shouldn't, which
downgrades you from serializable to read-committed without anyone noticing.


Include the same-connection caveat from §15.4. Your `Connection` sees its own uncommitted writes, exactly
as SQLite's does, and that's a documented property rather than a bug.


---


## 15.7 Roads not taken


### MVCC (multi-version concurrency control)


Keep multiple versions of each row, tagged with the transaction that created and deleted them. Readers
pick the versions visible as of their start. **Used by:** PostgreSQL, Oracle, InnoDB, SQL Server
snapshot mode, and SQLite's WAL mode in a limited form.


**Why it's the dominant design:** readers never block, writers never block readers, and long analytic
reads stop being a concurrency disaster. **What it costs:** old versions must be stored (chapter 10 §10.7's
bloat), garbage-collected (`VACUUM`), and every read now filters by visibility. And, per §15.5, it gives
snapshot isolation rather than serializability unless you add more.


**Why not here:** it changes the storage format — every row needs version metadata — which conflicts
directly with writing SQLite's format byte-for-byte. That's the cleanest possible reason to decline
something: not "too hard," but "incompatible with a locked constraint."


### Serializable Snapshot Isolation (SSI)


Snapshot isolation *plus* tracking read-write dependencies, aborting transactions that would form a
cycle. **Used by:** PostgreSQL's `SERIALIZABLE` since 9.1, CockroachDB. **Why it's remarkable:** it
delivers true serializability with no read locks at all — readers still never block. **The cost:**
tracking, and false-positive aborts, so applications must be prepared to retry. Worth knowing as the
answer to "how would you fix write skew without giving up MVCC's concurrency."


### Optimistic concurrency control


Don't lock. Execute, record what you read and wrote, and at commit check whether anything you read
changed; if so, abort and retry. **Used by:** most distributed and document databases, and every
`version` column in an ORM you've ever seen.


**When it wins:** low contention. No lock bookkeeping, no deadlocks, no waiting. **When it loses badly:**
high contention, where the same transaction is repeatedly redone and thrown away — work is wasted rather
than merely delayed. **The one-line comparison worth remembering:** pessimistic locking pays a fixed cost
to avoid conflict; optimistic pays nothing unless there *is* conflict, and then pays a lot.


### Timestamp ordering


Give each transaction a timestamp and force conflicting operations to occur in timestamp order,
aborting anything that arrives out of order. **Why it's mostly of theoretical interest for you:** it needs
per-object read and write timestamps, and it aborts a lot. But it's the conceptual root of both MVCC and
SSI, so it's the right thing to name if someone asks what else exists besides locking.


---


## 15.8 What you're building this week


No code in this chapter — it's the specification for chapter 16's code. What you should have written down
before session 1:


```markdown
# docs/concurrency.md  (skeleton — fill it in before you write locks, not after)


## Isolation level
Serializable, via table-level strict two-phase locking with a single writer.


## What that means concretely
- A transaction's reads are repeatable: ...
- Phantoms cannot occur because: ...
- Two transactions writing different rows of the same table: ...


## What it permits
- No isolation between statements on the SAME connection: ...
- ...


## Why table granularity
...


## Why this differs from SQLite
...
```


**And the test that defines the week**, because it's the one that would have caught §15.0:


```python
def test_no_money_is_created_or_destroyed(db):
    """8 threads, 10k random transfers. The invariant is not 'each transfer
    worked' — it's that the SUM never changes. That's the property that a
    lost update violates and that atomicity alone does not give you."""
    total_before = sum_balances(db)
    run_concurrent_transfers(db, threads=8, transfers=10_000)
    assert sum_balances(db) == total_before
```


Run it a hundred times in CI. A concurrency test that passes once has told you almost nothing; the value
is in it never failing across many runs, and in it failing *reproducibly* when you seed the randomness.


---


## 15.9 Check yourself


1. Two transfers of $50 from a $100 account leave $50 and create money. Every transaction was atomic and
   durable — so what property was missing, and why don't atomicity and durability imply it?
2. Draw the interleaving for each of the four anomalies.
3. Which of the four does week 5's journal already prevent, and why?
4. Why is a phantom read harder to prevent than a non-repeatable read? What do you have to lock?
5. Why does table-level locking make phantom protection nearly free?
6. Define serializable. Why is "some serial order" rather than "the order they arrived" the right
   definition?
7. Give two legitimate criticisms of the ANSI isolation-level ladder.
8. What does "we use repeatable read" fail to tell you?
9. How does SQLite achieve serializable isolation — what's the mechanism, in one sentence?
10. What does SQLite guarantee between two statements on the *same* connection? What practical coding
    rule follows?
11. What is snapshot isolation, and what does it buy that locking doesn't?
12. Walk through write skew with the on-call doctors. Why does snapshot isolation permit it?
13. Which of the four ANSI anomalies is write skew? (Careful.)
14. What isolation level does quilldb provide, and what's the argument that it includes phantom
    protection?
15. quilldb's locking is finer-grained than SQLite's rollback-journal mode. Why can you afford that, and
    why is SQLite's choice right for SQLite?
16. Why does MVCC conflict with quilldb's format-fidelity constraint?
17. When does optimistic concurrency control beat locking, and when is it much worse?
18. What does SSI add to snapshot isolation, and what does an application using it have to be prepared to
    do?


---


## 15.10 Sources


- [Isolation In SQLite](https://www.sqlite.org/isolation.html) — *"all transactions in SQLite show
  "serializable" isolation. SQLite implements serializable transactions by actually serializing the
  writes… they have to take turns."*; *"In WAL mode, SQLite exhibits "snapshot isolation.""*; the section
  "No Isolation Between Operations On The Same Database Connection", including *"the query might return a
  changed row more than once, or it might return a row that was previously deleted."*; and *"all readers
  must be (temporarily) expelled"* for the rollback-journal reader/writer relationship.
- [Write-Ahead Logging](https://www.sqlite.org/wal.html) — *"WAL provides more concurrency as readers do
  not block writers and a writer does not block readers"*, followed in §9 by the honest hedge *"This is
  mostly true."*
- Berenson, Bernstein, Gray, Melton, O'Neil & O'Neil, **"A Critique of ANSI SQL Isolation Levels"**
  (SIGMOD 1995) — the ambiguity of the ANSI definitions and the anomalies they omit, write skew among
  them.
- Ports & Grittner, **"Serializable Snapshot Isolation in PostgreSQL"** (VLDB 2012) — SSI as the fix for
  write skew without read locks.
- **The same-connection visibility experiment in §15.4** and the WAL reader-during-write result in §15.5
  were run against `sqlite3` 3.37.2 / Python 3.50.4 with two live connections; the WAL reader succeeded
  against a 4 MB in-flight write transaction.


---


**Next:** [16 — Locking, 2PL, and deadlock](16-locking-and-deadlock.md) — the machinery, why the *second*
phase is the load-bearing half, and why SQLite needs a fifth lock state.