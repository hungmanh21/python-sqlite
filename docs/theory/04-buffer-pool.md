 # 04 — The Buffer Pool


> **Read before week 1 session 7** (`storage/bufferpool.py`).
>
> **Time:** ~35 minutes. **Prerequisite:** [chapter 00](00-foundations.md) §0.2 (latency), [chapter 01](01-pages-and-pager.md) §1.7 (the pager boundary).


---


## 4.0 The problem: you keep reading the same page


Here's a B+tree lookup, week 2:


```
search(431):  read page 2 (root)  →  read page 6  →  read page 41 (leaf)
```


Three page reads. Now do a thousand lookups. **How many page reads?**


Naively 3,000. But look at *which* pages: **every single lookup reads page 2 first**, because page 2 is the root and there's only one root. You just read the same 4096 bytes from disk a thousand times.


Per chapter 00 §0.2, each of those is ~50 µs against ~100 ns from RAM. You're spending 50 ms on page 2 when you could spend 0.1 ms.


**What do you do?** The answer is obvious — keep it in memory — so the interesting question is the one after it: **keep *what* in memory, and for how long, given that memory is finite?** That question has a 50-year literature and a genuinely surprising answer for database workloads.


---


## 4.1 Wrong answer 1: keep everything in memory


```python
self.pages: dict[int, bytearray] = {}   # unbounded
```


Read a page once, keep it forever. Maximum hit rate, trivially correct.


**Why it breaks:** your database is 50 GB and your RAM is 16 GB. The process dies.


And it fails *unpredictably*, which is worse than failing. It works perfectly in all your tests (small databases) and then dies in production on a large one. **Unbounded caches are not caches; they're memory leaks with good intentions.** The bound is not a compromise on the design — the bound *is* the design.


This is worth being firm about because it's a real habit. Any time you write a dict that accumulates entries keyed by external input, ask what bounds it. If the answer is "nothing," you have a bug that hasn't happened yet.


---


## 4.2 Wrong answer 2: no cache, let the OS handle it


Chapter 00 §0.6 established that the kernel already caches file data in the OS page cache. So a second read of page 2 is served from kernel RAM, not the disk. Isn't your buffer pool redundant?


**Partly! This is a genuinely good question**, and "the OS cache is doing most of the work for you" is true. But four things make your own pool worth having, and they're worth knowing in this order:


1. **You still pay a syscall and a copy.** A `read()` hitting the OS cache still crosses into the kernel and copies 4096 bytes into your buffer. That's ~1–5 µs — better than 50 µs, but ~20× worse than the ~100 ns of already having the object.


2. **In Python, you pay parsing.** This is the big one, and it's specific to you. A cached `SlottedPage` object has its header already decoded and its cell offsets already available. A raw `bytes` from `read()` has to be re-wrapped and re-parsed every time. In C that's negligible; in Python, `struct.unpack` calls in a loop are *the* cost. **Your buffer pool caches parsed objects, not just bytes** — that's a bigger win for you than it is for SQLite.


3. **You need identity for correctness, not just speed.** This is the one that isn't about performance at all. In week 6, two threads asking for page 47 must receive **the same object**. If each gets its own copy, their modifications silently clobber each other — thread A writes its copy, thread B writes its copy over the top, and A's change vanishes. The OS cache cannot give you object identity; only a pool keyed by page number can. **Even a pool with a 100% miss rate would be necessary for this reason alone.**


4. **You need control for the journal.** In week 5, "this page is dirty and not yet journalled" is state that must live somewhere, and it must be consulted before the page can be written out. The OS cache has no notion of your journal.


Points 3 and 4 are the ones to lead with if challenged, because they're about correctness. Speed is the motivation; identity and control are the requirements.


> **Say this out loud:** "The OS page cache does help, but a buffer pool buys three things it can't: cached *parsed* page objects rather than raw bytes, which matters a lot in Python; object *identity*, so two threads asking for page 47 get the same object rather than two copies that clobber each other; and control, so the journal can enforce that a dirty page isn't written before its original is saved."


---


## 4.3 Caching theory: why this works at all


A cache only helps if the future resembles the past. The technical name for that resemblance is **locality**, and it comes in two flavours:


**Temporal locality** — *if you touched it recently, you'll probably touch it again soon.*


Databases have this in abundance, and not by luck — by *structure*:
- The B+tree **root** is on the path of every single lookup. It's the single hottest page in the database.
- **Interior pages** near the root are shared by huge numbers of lookups.
- The **catalog** (page 2, week 3) is read by every query that mentions a table.
- **Recently inserted rows** get read back immediately by real applications.


**Spatial locality** — *if you touched page N, you'll probably touch N+1.*


Also present: range scans walk leaves in order; sequential rowid inserts land on the same rightmost leaf repeatedly. This is what the OS's readahead exploits, and it's why sequential access is fast (chapter 00 §0.5).


### The arithmetic that shows why hit rate dominates everything


Effective average latency:


```
latency = hit_rate × RAM_latency + (1 − hit_rate) × disk_latency
```


With RAM ≈ 100 ns and NVMe ≈ 50 µs (a 500× gap):


| Hit rate | Effective latency | vs. no cache |
|---|---|---|
| 0% | 50,000 ns | 1× |
| 50% | 25,050 ns | 2× |
| 90% | 5,010 ns | 10× |
| 99% | 600 ns | **83×** |
| 99.9% | 150 ns | **333×** |


Stare at the bottom rows. **Going from 99% to 99.9% — eliminating 90% of the *remaining* misses — is a 4× speedup.** Going from 0% to 50% is only 2×.


This is deeply counterintuitive and it's the single most important thing in this chapter: **cache performance is dominated by the misses, so improvements at high hit rates are worth far more than they look.** A 1% miss rate isn't "1% of the work" — at a 500× latency gap, that 1% of accesses is **83% of your total time.**


Two consequences:


- **Report hit rate with enough precision to matter.** "97%" and "99.5%" sound similar and differ by 6× in effective latency. Your week 8 benchmark table should show hit rate to at least one decimal place, and ideally the miss *count*.
- **This is why eviction policy matters.** At a 60% hit rate, a smarter policy is a rounding error. At 99%, a policy that turns 1% misses into 0.3% misses is a 3× win. Which brings us to the interesting part.


> **Say this out loud:** "Effective latency is hit_rate × RAM + miss_rate × disk, and with a 500× gap the misses dominate completely — at a 99% hit rate, that 1% of accesses is 83% of your total time. So improvements at high hit rates are worth much more than they appear, and that's what makes eviction policy worth thinking about."


---


## 4.4 Eviction: the pool is full, something must go


### The problem


Capacity is 8 pages. All 8 are occupied. You need page 99. **Which of the 8 do you throw out?**


### The theoretically optimal answer, and why you can't have it


Evict **the page that will be needed furthest in the future.** This is *Belady's optimal algorithm* (sometimes MIN or OPT), and it's provably the best possible policy.


It requires knowing the future. It's useless in practice — but it's not useless as a *concept*: it's the yardstick. When people say a policy achieves "within 10% of optimal," this is what they mean. And it tells you what every real policy is doing: **guessing the future from the past.** Each policy is a different guess.


### The candidates


**FIFO** — evict whatever arrived first.


Simple, and *wrong in a specific way*: it evicts the root page as readily as anything else, purely because it arrived early. It ignores usage entirely. FIFO also exhibits **Bélády's anomaly** — a bigger cache can produce *more* misses, which is a delightful fact and a sign the policy isn't tracking anything meaningful.


**LFU (Least Frequently Used)** — evict the lowest access count.


Intuitively appealing: the root gets accessed constantly, so it's protected. But LFU has a fatal flaw called **cache pollution**: a page accessed 10,000 times last Tuesday and never since has a huge count and is unevictable forever. LFU has no notion of *recency*, so it can't adapt to a changing workload. Fixing it requires decaying counts over time, which means picking a decay rate, which is a tuning knob nobody knows how to set.


**LRU (Least Recently Used)** — evict whatever was touched longest ago.


The default answer, and a good one. It captures temporal locality directly: recently-used predicts soon-used. It adapts to workload changes automatically (a page that stops being used drifts down and out). And it's cheap — a hash map plus a doubly-linked list, or in Python an `OrderedDict` with `move_to_end()`, giving O(1) for every operation.


Crucially for you, **LRU protects exactly the pages you most need protected.** The root is touched on every lookup, so it can never be least-recently-used while any lookup is happening. Interior pages near the root are next. **LRU naturally keeps the top of the B+tree resident**, which is why a lookup in a big table costs ~1 disk read rather than 4 in steady state. You get the right behaviour without writing any B+tree-specific logic.


**Build LRU.** `roadmap.md` says LRU, `implementation.md` says LRU, and it's correct. But now the interesting part.


---


## 4.5 Why LRU is actively wrong for the most common query you'll run


### The problem


Your pool holds 100 pages. Right now it holds the hot working set — the B+tree root, interior pages, the catalog, the frequently-read leaves. Hit rate is 99%.


Someone runs:


```sql
SELECT COUNT(*) FROM users;     -- full table scan, 10,000 pages
```


**What does LRU do, and what's the state of your cache afterwards?**


Think it through before reading on. This is the best interview question in the chapter.


### What happens


The scan reads pages 1 through 10,000, **each exactly once, in order.** LRU dutifully:


1. Reads page 1, evicts the least-recently-used page — your root, or your catalog — to make room.
2. Reads page 2, evicts something else useful.
3. … continues for 10,000 pages.


At the end, your 100-page cache contains **pages 9,901 through 10,000** — the tail of a scan that will never look at them again. Everything genuinely useful has been evicted.


The next query has a **0% hit rate** and must re-read the root, the catalog, and its leaves from disk.


### Why LRU fails here, precisely


LRU's assumption is *"recently used implies soon-to-be-used again."* A sequential scan is a workload where that implication is **exactly false**: every page it touches is touched once and never again. LRU's prediction isn't merely imperfect, it's *anti-correlated with reality*.


The names for this: **sequential flooding**, **cache pollution**, or **scan-resistant** (of policies that avoid it). And note how the scan is doubly harmful — it gets no benefit from the cache *and* it destroys the cache for everyone else.


### What real systems do about it


Three distinct strategies, all worth knowing:


**PostgreSQL: ring buffers (buffer access strategies).** Postgres detects operations that will read a lot and touch each page once — sequential scans over large tables, `VACUUM`, bulk writes — and confines them to a **small dedicated ring** of buffers (a few hundred KB), reused circularly. The scan gets its own tiny sandbox; the main pool is untouched. Postgres's general replacement policy isn't strict LRU either but a **clock sweep** (see below).


**MySQL/InnoDB: midpoint insertion.** InnoDB splits its LRU list into a **young** sublist (5/8) and an **old** sublist (3/8). A newly read page is inserted *at the head of the old sublist*, not at the head of the whole list — so it's already most of the way to eviction. It's promoted to the young sublist only if it's accessed *again* after a configurable delay (`innodb_old_blocks_time`). A scan's pages are read once, never re-accessed, and cycle out of the old sublist without ever displacing anything young.


This is a lovely, cheap idea: **make a page prove it deserves to be cached before you let it push anything out.** One access is not proof; two accesses, separated in time, is.


**The academic answers.** Three policies from the literature, and the names are worth having:
- **LRU-K** (O'Neil, O'Neil & Weikum, 1993) — track the last *K* access times rather than just the last one, and evict by *K*-th-most-recent. With K=2, a page needs two accesses to look hot. This is InnoDB's idea, formalised.
- **2Q** (Johnson & Shasha, 1994) — two queues: a FIFO for first-time accesses and an LRU for pages accessed twice or more. Nearly LRU-K's quality at LRU's cost.
- **ARC** (Megiddo & Modha, 2003) — Adaptive Replacement Cache. Keeps four lists and *self-tunes* the balance between recency and frequency based on observed misses. Excellent, and famously patent-encumbered by IBM, which is why ZFS's use of it was contentious and why many systems avoided it.


**CLOCK / second-chance** deserves a mention as the *practical* workhorse: pages in a circular buffer, each with a reference bit set on access. A sweeping hand clears bits and evicts the first page whose bit is already clear. It approximates LRU at a fraction of the bookkeeping cost, and — importantly for chapter 06 and week 6 — it needs no list reordering, so it's far easier to make thread-safe. Postgres's clock sweep is a refinement using a small usage counter instead of a single bit.


### What SQLite does — and the honest answer


SQLite's page cache is `pcache.c` / `pcache1.c`, bounded by `PRAGMA cache_size` (default 2 MB of pages since 3.12.0 — see chapter 00 §0.4). It's an LRU over clean pages.


SQLite does *not* implement scan resistance, and the reason is a good example of scope discipline: it's an embedded engine where the whole database is often small enough to be fully cached, and where library size is a hard constraint. Adding ARC to SQLite would cost binary size and complexity for a benefit most of its users would never observe. **Not every system needs every optimisation, and knowing when a system correctly declined one is as useful as knowing the optimisation.**


### What you should do


**Implement LRU. Do not implement scan resistance.** It's not on `roadmap.md` and it shouldn't be.


But **do instrument it**, because that's nearly free and it converts this knowledge into a *demonstration*:


```python
class BufferPool:
    hits: int
    misses: int
    evictions: int
    dirty_writebacks: int
```


Then in week 8's benchmark, run a point-lookup workload, record the hit rate, run a full table scan, and record the hit rate of the *next* point-lookup workload. **You will measure the cache being destroyed.** That gives you:


- A concrete number for your README.
- A demonstrated understanding rather than a claimed one.
- The single best "here's something I learned that surprised me" story in the project.


`guide.md` §1.5 says "You don't have to implement that. Knowing it is the point." That's right, and measuring it is one better — it's the difference between "I read that LRU is bad for scans" and "I measured my hit rate dropping from 99.2% to 11% after a scan, which is sequential flooding; Postgres avoids it with ring buffers and InnoDB with midpoint insertion, and I'd add a ring buffer if scans were part of my workload."


> **Say this out loud:** "LRU assumes recently-used means soon-used-again, and a sequential scan is the workload where that's exactly false — every page is touched once, so the scan evicts the entire hot working set to cache pages it will never revisit. It's called sequential flooding. Postgres confines big scans to a small ring buffer; InnoDB inserts new pages at the midpoint of its LRU list so a page must be re-accessed to earn promotion. I measured it: my hit rate went from 99% to near zero after a scan."


---


## 4.6 Pin counts: the correctness half


Everything so far has been about speed. Pin counts are about **not crashing.**


### The problem


A B+tree cursor is in the middle of walking a leaf page. It holds a reference to that page's bytes and it's reading cell 5 of 40. Meanwhile another part of the code asks for a new page, the pool is full, and LRU decides the least-recently-used page is... the one the cursor is reading.


**What happens?**


### The failure


The pool evicts the page. In Python you won't segfault — the cursor holds a reference, so the object stays alive. What happens instead is *worse*, because it's silent:


- The cursor holds a **detached** page object. It's no longer in the pool.
- Someone else requests the same page number. It's a miss, so a **second, independent object** is created for the same page.
- Now two objects represent page 47. The cursor modifies one; the other gets written to disk. **One of the two sets of changes vanishes**, with no error and no log line.


In C this would be a use-after-free and you'd crash, which is honestly a kinder outcome — a crash has a stack trace. In Python it's a lost update, discovered weeks later as a row that isn't there.


### The fix


A **pin count** per page: how many things are currently using it.


```python
page = pool.get_page(47)      # pin_count += 1
...                            # cannot be evicted while pinned
pool.unpin(47)                 # pin_count -= 1, now eligible again
```


**Rule: a page with `pin_count > 0` is never evicted.** If the pool is full and every page is pinned, that's an error — you're out of buffers — and it should raise clearly rather than evicting something in use.


Note the resource-management shape: acquire, use, release, and *you must not forget the release*. That's a `try/finally` or a context manager, and the pool should be capable of telling you when you leaked:


```python
with pool.pinned(47) as page:
    ...    # unpinned automatically, even if this raises
```


**Write the context manager in week 1.** A leaked pin is invisible until the pool fills up and then everything mysteriously fails to evict — you'll have `PoolExhausted` raised in week 2 from code that has no bug, because week 1 leaked a pin. Add a debug assertion that all pins are zero between operations in tests; it's three lines and it localises the bug to the operation that caused it.


`guide.md` §1.5 says pin counts start "doing real work" in week 6. True — that's when concurrent access makes violations frequent. But they're *load-bearing from week 2*, because a cursor walking a tree holds pins the whole time.


---


## 4.7 Dirty pages: the rule that data loss hides behind


### The problem


You modify page 47 in the pool — it's now different from what's on disk. LRU decides to evict it.


**If you just drop it, what happens?**


### The answer


**Your data is gone.** Silently. No error. The `INSERT` returned successfully, the row existed in memory, and now it doesn't exist anywhere.


This is the most consequential single bug available in week 1, because it produces *silent data loss under memory pressure* — meaning it works in every small test and fails on large workloads, intermittently, in a way that's nearly impossible to reproduce.


### The fix


A **dirty flag** per page: has this been modified since it was read?


```
evict(page):
    if page.pin_count > 0:  refuse
    if page.is_dirty:       write it to disk FIRST
    remove from pool
```


That's it. Three lines, and skipping the middle one loses data.


### Why not just write immediately on every modification?


That's **write-through** caching, and it's a real design. Its virtue is simplicity: nothing is ever dirty, so eviction is trivial and there's no window in which memory and disk disagree.


**Why write-back (deferring) is worth the complexity:**


- **Write coalescing.** A B+tree split modifies a page, then the parent, then possibly its parent. A sequential insert workload hits the same rightmost leaf thousands of times. Write-through writes it thousands of times; write-back writes it once at eviction or commit. **This is often a 100× reduction in write volume.**
- **You need it for week 5 anyway.** Transactions require that modifications *not* reach disk until commit. A write-through pool would push uncommitted data to disk, which makes rollback impossible. So write-back isn't an optimisation you're choosing — it's a prerequisite for atomicity.


That second reason is the real one. State it that way: *"write-back isn't for speed, it's because atomicity requires that uncommitted changes not be on disk."*


### ⚠️ The interaction with the journal that will bite you in week 5


Here's a subtle one worth planting now, because it's a bug that's very hard to find later.


The pool can decide to evict a dirty page **at any time**, including in the middle of a transaction, purely because it needs space. That write goes to the database file. But week 5's protocol says: *the original content of a page must be in the journal, and the journal must be fsynced, before the page is overwritten in the database file.*


So an eviction can **violate the journal protocol** if the page's original wasn't journalled first. Nothing in the buffer pool knows about the journal, and nothing in the journal knows the pool is about to evict.


SQLite calls this a **cache spill** and handles it explicitly — a spill mid-transaction forces journal work, and (per [atomiccommit.html §7.5](https://www.sqlite.org/atomiccommit.html)) can require appending an additional journal header. It's one of the fiddlier parts of `pager.c`.


**What to do now, in week 1, in two lines of design:**


- Route **every** page acquisition-for-modification through the transaction object, from day one. `roadmap.md` §3 already mandates this: "Every mutation goes through a transaction object, from week 1 — even before the journal exists." **This is the reason.** If the transaction is always the thing that marks a page dirty, then the transaction always has the opportunity to journal it first.
- Give the pool a rule it can enforce without knowing what a journal is: **before writing a dirty page to disk, call a hook the transaction owns.** In week 1 the hook does nothing. In week 5 it journals. The pool never learns what a journal is — the same layering argument as chapter 01 §1.7.


Getting this seam right in week 1 costs you fifteen minutes. Getting it wrong costs you a red crash matrix in week 5 with a cause that looks like a B+tree bug.


---


## 4.8 Roads not taken


### No buffer pool — rely entirely on the OS page cache


Covered in §4.2. Viable for a read-mostly engine in C; not viable for you, because of object identity (week 6) and journal control (week 5). Worth knowing that "just use the OS cache" is the *right* answer for some systems — it's roughly what a memory-mapped design does.


### `mmap` — let the OS be the buffer pool


Chapter 01 §1.9 covers this. The relevant point here: with mmap, the *kernel's* replacement policy is your replacement policy, and you can't tell it that a scan is one-shot or that a cursor holds a pin. You trade control for zero-copy. LMDB and BoltDB make that trade because their copy-on-write design doesn't need the control.


### `O_DIRECT` — bypass the OS cache so yours is the only one


Chapter 01 §1.9. The motivation is **double caching**: without it, every page is cached twice (kernel + your pool), halving effective memory, and the two policies fight. InnoDB and Oracle do this. It only pays when your pool is large and your policy is *better* than the kernel's — which yours, being plain LRU, is not. That's a good, honest "how would you make this faster" answer with a precondition attached.


### Prefetching / readahead


If you notice sequential access, fetch pages *before* they're asked for, overlapping I/O with computation. Every real database does some version of this, and Postgres has explicit `effective_io_concurrency` tuning for it.


**Why not for you:** it needs asynchronous I/O or a background thread to be worth anything (a synchronous prefetch is just a read you did early), and the OS already does readahead on the file for you. `roadmap.md` doesn't include it and shouldn't.


### Multiple pools / partitioned pools


Big systems partition the pool into independent shards to reduce lock contention (InnoDB's `innodb_buffer_pool_instances`), or keep separate pools per page type. Purely a concurrency optimisation — relevant to week 6's discussion, not to week 1's design. Worth naming as the answer to "your single pool lock is a bottleneck, what would you do?": *"shard the pool by page number modulo N, so hot pages in different shards don't contend for the same lock."*


---


## 4.9 What you're building


`roadmap.md` week 1 session 7, `storage/bufferpool.py`, ~90 lines:


```python
class BufferPool:
    def __init__(self, pager: Pager, capacity: int = 128) -> None: ...
    def get_page(self, page_id: int) -> Page: ...      # pins
    def unpin(self, page_id: int, dirty: bool) -> None: ...
    def flush_page(self, page_id: int) -> None: ...
    def flush_all(self) -> None: ...
```


`OrderedDict` gives you LRU in about six lines: `move_to_end(k)` on access, `popitem(last=False)` to evict the oldest.


### The three rules, in priority order


1. **Never evict a pinned page.** Violation → lost updates and detached page objects (§4.6).
2. **Never drop a dirty page without writing it.** Violation → silent data loss (§4.7).
3. **Evict least-recently-used.** Violation → slower. *Only* slower.


Notice the asymmetry, and it's the thing to internalise: **rules 1 and 2 are correctness; rule 3 is performance.** If you get LRU wrong you have a slow database. If you get the first two wrong you have a database that loses data. Test them in that order and with that seriousness — the eviction *policy* deserves a unit test; the eviction *rules* deserve property tests.


### The tests


`roadmap.md`'s success criterion is "a capacity-8 pool over a 200-page workload evicts correctly, never loses a dirty page, never evicts a pinned one." Make that concrete:


1. **Capacity is respected.** Touch 200 distinct pages with capacity 8; `len(pool)` never exceeds 8.
2. **Dirty pages survive eviction.** Write to page 5, force 20 evictions, read page 5 back from disk — the modification is there. *(This is the data-loss test. Make it a property test over random access sequences.)*
3. **Pinned pages are never evicted.** Pin 8 pages with capacity 8, request a 9th → raises `PoolExhausted` (or whatever `errors.py` names it), and does **not** evict a pinned page.
4. **Identity.** Two `get_page(47)` calls return the *same object* (`is`, not `==`). This is week 6's foundation and it's free to test now.
5. **LRU order.** Access 1,2,3, then 1 again, then insert 4 with capacity 3 → page 2 is evicted, not page 1.
6. **Pins balance.** After any test operation, every pin count is zero. Assert it in a fixture teardown; it catches leaks at the operation that caused them.
7. **Hit rate accounting is correct.** `hits + misses == total get_page calls`. Trivial, and it makes the counters trustworthy enough to put in a README.


Test 2 is the one that matters most and it's the one most likely to be written weakly. Do it as a Hypothesis property over random sequences of (read page, write page, read page) with a small capacity, checking against a dict oracle. That's the shape of test that finds the eviction-of-dirty-page bug in a case you didn't think of.


---


## 4.10 Check yourself


1. The OS already caches file pages. Give the two *correctness* reasons you need your own pool anyway.
2. Why is an unbounded cache not a cache?
3. Effective latency with a 99% hit rate and a 500× RAM/disk gap: what fraction of total time is spent on misses? Why does that make high-hit-rate improvements disproportionately valuable?
4. Why does LRU automatically protect the B+tree root without any B+tree-specific code?
5. Belady's algorithm is optimal and useless. What's it useful *for*?
6. What's wrong with LFU? Name the failure mode.
7. Walk through what a 10,000-page sequential scan does to a 100-page LRU pool, and say why LRU's core assumption is *anti*-correlated here.
8. How do Postgres and InnoDB each avoid sequential flooding? State InnoDB's principle in one sentence.
9. In Python you won't segfault when a pinned page is evicted. Describe what happens instead, and why it's worse.
10. Write-through vs write-back: give the reason write-back is *required* rather than merely faster.
11. A cache spill mid-transaction can violate the week-5 journal protocol. How, and what seam do you put in during week 1 to prevent it?
12. Of the pool's three rules, which two are correctness and which is performance? Why does the distinction change how you test them?


---


## 4.11 Sources


- [SQLite Architecture](https://www.sqlite.org/arch.html) — `pcache.c` / `pcache1.c` as the in-memory caching implementation; the pager owning caching, rollback, atomic commit, and locking.
- [Atomic Commit In SQLite](https://www.sqlite.org/atomiccommit.html) — §3.12 on reusing the user-space cache across transactions via the change counter; §7.5 on additional journal headers when the cache spills mid-transaction.
- [Default Page Size Change](https://www.sqlite.org/pgszchng2016.html) — `SQLITE_DEFAULT_CACHE_SIZE` moving from `2000` (pages) to `-2000` (bytes) to hold cache memory constant across the page-size change.
- **CMU 15-445** "Buffer Pools" lecture — pin counts, dirty flags, replacement policies, and the sequential-flooding problem. `references.md` notes that Project #1 of that course is literally "build a buffer pool manager"; its public writeup is a free precise spec for this component.
- **PostgreSQL** buffer access strategies (ring buffers for seq scans, `VACUUM`, bulk writes) and clock-sweep replacement — see the Postgres source `src/backend/storage/buffer/README`, cited from knowledge.
- **MySQL/InnoDB** LRU with midpoint insertion, 5/8 young + 3/8 old sublists, `innodb_old_blocks_time` — see the InnoDB Buffer Pool section of the MySQL manual, cited from knowledge.
- Policy literature, cited by title/author/year: **LRU-K** (O'Neil, O'Neil & Weikum, SIGMOD 1993); **2Q** (Johnson & Shasha, VLDB 1994); **ARC** (Megiddo & Modha, USENIX FAST 2003); **Belady's MIN** (Belady, IBM Systems Journal 1966).


---


**Next:** [05 — Why B-trees](05-b-trees.md) — the arithmetic that makes 125 million rows reachable in four page reads, and the two data structures that beat B-trees at exactly one thing each.





