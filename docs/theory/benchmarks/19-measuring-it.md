# 19 — Measuring It


> **Read during week 8, before you write `benchmarks/`.** Short chapter, high leverage: the benchmark
> table is one of the four things in this project that a stranger actually reads.
>
> **Time:** ~25 minutes. **Prerequisites:** [chapter 00](../foundations/00-foundations.md) (why storage is
> slow), [chapter 04](../storage/04-the-buffer-pool.md) (the cache that will fool you).


---


## 19.0 The problem: you have a number, and no reason to believe it


Week 4 produced this:


> *Indexed lookup: 3 page reads. Full scan: 2,400 page reads. 800× faster.*


It's a great line. Now imagine a hostile reader — which is exactly what an interviewer is, politely:


- "Faster at what? Was the cache warm?"
- "Was it 800× in *time*, or in something you counted?"
- "How many rows? How wide? What page size?"
- "Did you run it once?"


**If you can't answer all four, the number is decoration.** And a benchmark that doesn't survive
questioning is worse than no benchmark, because it converts "this person built a database" into "this
person doesn't know what they measured."


This chapter is about producing numbers that survive.


---


## 19.1 Why you should not report milliseconds


Time is the thing you care about, and it's the worst thing to measure. Run the identical benchmark twice
and you can see a 3× difference for reasons that have nothing to do with your code:


| Source of variance | What it does |
|---|---|
| **The OS page cache** | run 2 reads from RAM, not disk. This is the big one — often 100× |
| **Your own buffer pool** | run 2 starts with the tree already cached (chapter 04) |
| **CPU frequency scaling** | the first run boosts; the tenth is thermally throttled |
| **Python warmup** | bytecode caching, import costs, allocator state |
| **Noisy neighbours** | another process, a VM co-tenant, a background indexer |
| **Filesystem state** | fragmentation, and whether the file was just written |


None of these is about whether your B-tree is good. **All of them move the number more than your
algorithm does**, which means a timing comparison mostly measures your environment.


The trap that catches everyone: you benchmark the scan first and the index lookup second, and the index
lookup now runs against a warm cache. **Your measurement includes an advantage you accidentally granted
it.** The number is real, reproducible on your machine, and meaningless.


---


## 19.2 Count page reads instead


```python
db.reset_counters()
db.execute("SELECT * FROM users WHERE email = ?", ("u50000@x.com",)).fetchall()
print(db.pages_read, db.rows_examined)
```


Four properties, and the fourth is the one people miss:


1. **Deterministic.** Same data, same query, same count. Every time, on every machine.
2. **Determined by the algorithm**, which is the thing you're claiming credit for.
3. **Machine-independent**, so the number in your README means the same thing on the reader's laptop.
4. **Checkable from first principles**, which is what makes it *persuasive* rather than merely accurate.


Property 4 deserves the emphasis. A reader can verify your scan number without running anything:


```
100,000 rows × ~100 bytes/row  =  ~10 MB
10 MB / 4096 bytes per page    =  ~2,400 pages        ✓ matches the claim
```


And the index number, from chapter 05's fanout arithmetic:


```
fanout ~150  ->  log_150(100,000) ≈ 2.3  ->  3 levels + 1 table leaf  =  ~4 reads   ✓
```


> **A number the reader can derive is a number the reader believes.** A timing is a number they have to
> take on faith, and a sharp reader knows better than to. This is the single most useful idea in this
> chapter: **choose the metric that makes your claim checkable.**


**Report time too, as a secondary figure**, because "how long does it take" is a legitimate question and
refusing to answer looks evasive. Just present it with its variance (median of N runs, and the spread) and
say which caches were warm. Page reads are the claim; milliseconds are the context.


---


## 19.3 Name your three caches


There are three, and a benchmark that doesn't say which were warm is ambiguous:


```
your query
   ↓
[1] quilldb's buffer pool      you control this — reset it, or reopen the Database
   ↓
[2] the OS page cache          you mostly can't control it (needs root to drop)
   ↓
[3] the drive's own cache      you can't control it at all (chapter 00 §0.7)
```


**What you can honestly do:**


- **Cold-ish:** create a fresh `Database` per measurement, so the buffer pool is empty. This is the
  measurement that matches your page-read count, because a page read is exactly a buffer-pool miss.
- **Warm:** run the query twice and report the second. Legitimate, and closer to steady-state
  production — just label it.
- **Truly cold:** needs `echo 3 > /proc/sys/vm/drop_caches` (root) or `O_DIRECT`. Almost certainly out of
  scope. **Say so** rather than implying you did it.


**The honest sentence:** *"Page-read counts are from a cold buffer pool — a fresh `Database` per
measurement. The OS page cache was not dropped, so wall-clock times reflect a warm OS cache and are
reported as medians with spread. The page-read counts are unaffected by that, which is why they're the
headline number."*


That paragraph takes thirty seconds to write and it pre-empts every question in §19.0.


---


## 19.4 What's worth measuring in this project


Not everything. Each of these maps to a specific claim in the README, and anything that doesn't support a
claim is a benchmark you don't need:


| Measurement | Supports the claim | Expected shape |
|---|---|---|
| **Point lookup: index vs scan** | "indexes work" | ~4 vs ~2,400 page reads |
| **B+tree height vs row count** | "it's logarithmic" | 1k→2, 100k→3, 10M→4 |
| **Insert throughput: sequential vs random rowid** | "I understand splits" | sequential much faster (chapter 05's rightmost-split case) |
| **Insert cost per index** | honesty about write amplification | linear in index count |
| **Buffer-pool hit rate vs pool size** | "caching theory is real" | steep, then flat — the working-set knee |
| **Scan with and without a covering index** | chapter 11 §11.5 | ~2× |
| **Throughput vs thread count, reads and writes separately** | week 6, honestly | reads scale; writes don't |
| **`LIMIT 1` over a million rows** | "the pull model actually short-circuits" | <10 page reads |
| **Crash matrix case count** | "durability is tested, not claimed" | a few hundred |


**The B+tree height table is the most underrated one.** Three rows, trivially measured, and it demonstrates
the single most important property of the whole data structure — that 10 million rows are four reads away.
It also invites the reader to check `log_fanout(N)` themselves, which per §19.2 is exactly what you want.


**The buffer-pool hit-rate curve is the most interesting-looking.** Plot hit rate against pool size and you
get a knee where the working set fits. That's chapter 04's theory, visible, from your own instrumentation.


---


## 19.5 How to state a result


**Bad:**


> *quilldb does 50,000 inserts/sec and lookups are 800× faster with an index.*


Two unfalsifiable claims. No units on the comparison, no conditions, no idea what "faster" measures.


**Good:**


> **Point lookup, 100,000 rows** (`users(id INTEGER PRIMARY KEY, email TEXT, age INTEGER)`, ~100 B/row,
> 4096-byte pages, cold buffer pool, median of 20 runs):
>
> | Access path | Page reads | Rows examined | Median time |
> |---|---|---|---|
> | `SeqScan` + `Filter` | 2,417 | 100,000 | 412 ms |
> | `IndexScan` on `idx_email` | 4 | 1 | 0.9 ms |
>
> Page reads are buffer-pool misses with a fresh pool per run; the OS page cache was not dropped, so
> times reflect a warm OS cache. 2,417 ≈ 10 MB of rows / 4 KB per page. 4 ≈ a 3-level index plus one
> table leaf.
>
> **Not claimed:** this is a single-threaded, single-column equality lookup. It says nothing about range
> scans, joins, or concurrent throughput.


The second one is longer and it is the one that gets you hired. It states the setup, the metric, the
variance, the caches, the arithmetic, **and the limits of the claim**.


> **That last section — "not claimed" — is the highest-value part.** It demonstrates you know what your
> measurement doesn't cover, which is precisely the judgement that separates someone who ran a benchmark
> from someone who can be trusted with a performance question. It also removes the interviewer's best
> follow-up, because you've already made it.


---


## 19.6 Publish the numbers that make you look worse


Counterintuitive, and it's the strongest move available in week 8.


- **Insert cost with 3 indexes vs 0.** Every index is a write tax. Publishing it proves you understand
  indexes are a *tradeoff* rather than a free speedup — and it makes your lookup speedup credible, because
  you clearly weren't cherry-picking.
- **Write throughput vs thread count, flat.** The GIL plus your single-writer design caps it (chapter 16
  §16.7). State it, explain *why*, and show that read throughput does scale. Someone who discovers this
  themselves will discount everything else you claimed.
- **Random-order inserts vs sequential.** Random is slower, because splits happen mid-page rather than at
  the rightmost edge. That's chapter 05 §5.7, measured — a weakness that demonstrates understanding.
- **The sort row limit.** "`ORDER BY` sorts in memory and raises past N rows" is a limitation. Published,
  with the number, it reads as a scoped decision. Discovered by a reader, it reads as a bug.


**The general principle:** a benchmark table with no unflattering rows in it looks curated, and curated
looks untrustworthy. **One honest weakness makes the other rows believable**, which means volunteering it
is not just integrity, it's the more effective presentation.


---


## 19.7 Test counts are a benchmark too


SQLite's own answer to "why should I trust this" is not architectural — it's a number. As of 3.42.0 the
project ships roughly **590× more test code than library code**, across four independent harnesses, and
`testing.html` exists purely to explain that in public.


> **Nobody chooses an embedded database because its B-tree is elegant.** They choose it because the
> project makes its reliability *legible*.


Your README's table of tests by category is the same move at small scale, and one row does most of the
work:


```
| Category            | Tests | What it proves                                    |
|---------------------|-------|---------------------------------------------------|
| Unit                |   612 | each module against its spec                       |
| Property (Hypothesis)|   47 | invariants over generated input                    |
| Differential vs sqlite3| 180 | query results match a reference implementation     |
| **Crash injection** | **340** | **atomicity holds at every write and fsync boundary** |
| Concurrency stress  |    12 | no lost updates across 8 threads × 10k transactions |
| Corruption handling |    23 | malformed files raise typed errors, never crash    |
```


**The crash-injection row is the one an interviewer will stop on**, because almost nobody has it. Make sure
the count is real and you can explain how the cases are generated (chapter 14 §14.4).


---


## 19.8 What real benchmarking looks like


Worth knowing the landscape, mostly so you can say what you *didn't* do:


**Standard benchmarks.** **TPC-C** (OLTP, order entry, measures transactions/min), **TPC-H** (analytics,
22 fixed queries), **YCSB** (key-value, tunable read/write mixes), **sysbench** (MySQL-oriented). **Why
they exist:** so vendors can't pick their own favourable workload. **Why not here:** they'd take longer to
implement than the features they'd exercise, and quilldb doesn't support enough SQL to run TPC-H. Naming
them shows you know your own benchmarks are self-selected.


**Statistical rigour.** Report medians and interquartile ranges rather than means, discard warmup runs,
and if you compare two configurations, note that a difference smaller than the run-to-run spread is not a
difference. Page reads sidestep almost all of this, which is a fifth reason to prefer them.


**Profiling rather than benchmarking.** A benchmark tells you *how fast*; a profiler tells you *where the
time goes*. `cProfile` plus a flamegraph on your slowest query is 20 minutes and often finds something
embarrassing and easy — an accidental O(n²), a re-parse in a loop, a page decoded twice per access.
**Do this once before you write the README**, because a fixed hot spot is a better story than a measured
one.


**The honest limit of all of it:** every number here comes from one machine, one filesystem, one Python
version, one dataset shape. That's fine — just don't imply otherwise.


---


## 19.9 What you're building


```python
# benchmarks/harness.py
@dataclass
class Result:
    name: str
    page_reads: int          # deterministic — the headline
    rows_examined: int       # deterministic
    median_ms: float         # context, with spread
    p10_ms: float
    p90_ms: float
    setup: str               # row count, schema, page size, cache state




def measure(db_factory, query, params=(), runs=20) -> Result:
    """Fresh Database per run so the buffer pool is cold, which is what makes
    page_reads mean 'buffer-pool misses'. Discard the first run. Report the
    median and the 10th/90th percentiles, never the mean."""
```


**Make the output paste-able as a markdown table.** You will regenerate these numbers several times as you
polish, and hand-formatting them each time is how the README ends up with stale figures that contradict the
code — the specific failure this whole chapter is meant to prevent.


**And check the numbers against the arithmetic before publishing.** If your scan reports 4,800 page reads
where the row-size arithmetic predicts 2,400, you have a bug worth finding — probably reading each page
twice, or a buffer pool that isn't caching within a single scan. **The benchmark is also a test**, and this
is the moment it earns that twice over.


---


## 19.10 Check yourself


1. Four questions a hostile reader asks about "800× faster." Can you answer all four?
2. Name five reasons a wall-clock benchmark varies between runs, none of them your code.
3. What's the specific mistake in benchmarking the scan first and the index lookup second?
4. Give four properties of page-read counts as a metric. Which one makes a claim *persuasive* rather than
   merely accurate?
5. Show the arithmetic a reader would use to check "a full scan of 100,000 rows is ~2,400 page reads."
6. Name the three caches between your query and the platter. Which can you control?
7. What's the honest way to describe cache state in a README?
8. Why is the B+tree-height-vs-row-count table underrated?
9. What does the buffer-pool hit-rate curve demonstrate, and what shape should it be?
10. Rewrite "quilldb does 50,000 inserts/sec" as a defensible claim.
11. Why is a "not claimed" section the highest-value part of a benchmark write-up?
12. Give three numbers that make quilldb look worse, and explain why publishing them helps.
13. Why does SQLite's 590× test-to-code ratio belong in a chapter about benchmarks?
14. Which row of your test-count table will an interviewer stop on, and what must you be able to explain
    about it?
15. Why can't you run TPC-H against quilldb, and why is naming TPC-H still worth doing?
16. Your scan reports 4,800 page reads where the arithmetic says 2,400. What do you do?


---


## 19.11 Sources


- [How SQLite Is Tested](https://www.sqlite.org/testing.html) — the test-to-code ratio, the four
  independent harnesses, and the fact that the page exists at all: a public explanation of how the project
  earns trust.
- [Atomic Commit In SQLite](https://www.sqlite.org/atomiccommit.html) §9 — the hardware assumptions that
  bound what any measurement on your machine can tell you.
- [TPC-C](http://www.tpc.org/tpcc/) and [TPC-H](http://www.tpc.org/tpch/) — the standard OLTP and analytic
  benchmarks, for §19.8.
- Cooper et al., **"Benchmarking Cloud Serving Systems with YCSB"** (SoCC 2010).
- **Chapters 05 and 04** for the arithmetic a reader uses to check your fanout and hit-rate claims — the
  benchmark table and the theory should agree, and if they don't, one of them is wrong.


---


**This is the last chapter.** If you've read all twenty, you can explain why storage is slow, why databases
are shaped like databases, how a query becomes page reads, why a crash doesn't corrupt the file, what your
isolation level permits, and which numbers you'd trust. That's the whole point — not the code, which any
reader can see, but the reasoning behind it, which they can only get by asking you.