# 05 — Why B-Trees


> **Read at the start of week 2.** The theory behind everything in `btree/`.
>
> **Time:** ~40 minutes. **Prerequisite:** [chapter 04](../storage/04-the-buffer-pool.md). The arithmetic in §5.3 is the centre of the chapter.


---


## 5.0 The problem: one row in a billion


A table with 1,000,000,000 rows, stored as pages (chapter 01). Each page holds ~50 rows, so ~20,000,000 pages, ~80 GB.


```sql
SELECT * FROM users WHERE id = 736429185;
```


**How many pages must you read?**


The honest starting point: with no organisation at all, you read pages until you find it — on average 10,000,000 pages, 40 GB, at ~50 µs each = **8 minutes**. For one row.


You need to get that to single digits. Let's build up to how, because each wrong answer teaches something the final design needs.


---


## 5.1 Wrong answer 1: keep the rows sorted, binary search


Sort by `id`. Binary search: check the middle page, decide which half, repeat.


log₂(20,000,000) ≈ **25 page reads.** From 10 million to 25. Enormous win, and the mechanism is worth stating precisely: **sorting lets you eliminate half the search space per probe.** That's the fundamental capability, and every structure in this chapter is a variation on buying it.


**Why it breaks: insertion.**


Insert a row with `id = 500`, and it belongs between 499 and 501, which are in the middle of the file. To keep the file sorted you must shift **every byte after it** — 40 GB of rewriting, to add one row.


So a sorted array is:
- **Perfect for reading** (25 reads, and range scans are free — just walk forward)
- **Completely unusable for writing** (O(n) per insert, where n is 80 GB)


**The insight:** you want sortedness *without* physical contiguity. Sortedness is what buys the search; contiguity is what makes insertion impossible. **Separate them.** (This is the same move as chapter 02 §2.1 — sorted *findability* without sorted *storage* — one level up.)


---


## 5.2 Wrong answer 2: a binary search tree


Classic answer. Each node holds one key and two child pointers. Insertion is O(log n) — no shifting, just a pointer update. Sortedness is maintained by structure, not by layout.


```
              [736429185]
             /            \
      [400000000]      [900000000]
        /      \          /     \
      ...      ...      ...     ...
```


A balanced BST over a billion keys has depth log₂(1,000,000,000) ≈ **30**.


**So: 30 page reads.** Worse than the sorted array's 25, and here's the thing — *this is the answer most people give*, and it's the answer that reveals whether someone has thought about disk.


### Why a BST is a disaster on disk


Look at one node. A key (8 bytes) plus two child pointers (4 bytes each) = **16 bytes.**


Your unit of I/O is **4096 bytes** (chapter 00 §0.3 — you cannot read less).


So every node access reads 4096 bytes to use 16. **You waste 99.6% of every read.** You pay for a page and consume a sliver of it.


And it compounds: 30 levels means 30 separate page reads, each of which wastes 4,080 bytes. You've been charged 122,880 bytes of I/O to answer a query, and used 480 bytes of it.


### The reframe that fixes everything


Here's the move, and it's the whole chapter:


> **In-memory algorithms count *comparisons*. On-disk algorithms count *page reads*. These are different objective functions, and they favour different structures.**


A BST minimises comparisons — ~30 of them, which is optimal for a comparison-based search. But comparisons are free (~1 ns) and page reads cost 50,000 ns. Optimising the free thing while ignoring the expensive one by a factor of 50,000 is how you get a structure that's theoretically elegant and practically terrible.


The right question isn't "how few comparisons?" It's:


> **"Given that I'm paying for 4096 bytes anyway, how much search progress can I extract from one page?"**


A BST extracts one bit — one comparison, one branch, half the space eliminated. **That's the cheapest possible use of 4096 bytes.**


`roadmap.md` §9 lists this among the things you should be able to discuss cold: "how an index turns O(n) into O(log n) in **page reads**, not just comparisons." That emphasis is the point of this section.


> **Say this out loud:** "A BST is depth-30 over a billion keys, so 30 page reads — and each node is 16 bytes on a 4096-byte page, so you waste 99.6% of every read to extract one bit of search progress. On disk you're optimising page touches, not comparisons, and those favour completely different structures."


---


## 5.3 The answer: make each page a wide decision


Since you're paying for 4096 bytes, **fill them with keys.**


Instead of one key and two children per node, put ~500 keys and ~501 children on one page:


```
 one interior page (4096 bytes):
 ┌────────────────────────────────────────────────────────────────┐
 │ <100→p5 │ <500→p6 │ <900→p7 │ <1400→p8 │ ... ~500 signposts ...│
 └────────────────────────────────────────────────────────────────┘
```


One page read now narrows the search by a factor of **500**, not 2.


The number of children per node is called the **fanout** (or *branching factor*), and it is the single number that determines everything.


### The arithmetic — do this by hand once


An interior cell is a key plus a child page number. Roughly 8 bytes for a varint key plus 4 for a `u32` page number ≈ **8–12 bytes**, plus 2 for its slot pointer (chapter 02). Call it ~14 bytes, and 4096 / 14 ≈ **290**. Round to a conservative ~300, or ~500 for smaller keys.


Now, with fanout *f*, a tree of height *h* addresses *f^h* leaves:


| Height | Interior pages traversed | Leaves reachable (f=500) | Rows (50/leaf) |
|---|---|---|---|
| 1 | root only | 500 | 25,000 |
| 2 | root + 1 | 250,000 | 12,500,000 |
| **3** | **root + 2** | **125,000,000** | **6,250,000,000** |
| 4 | root + 3 | 62,500,000,000 | 3 trillion |


Read the bolded row. **Three levels of interior pages plus one leaf read = 4 page reads to find any row in a table of six billion.**


Compare:


| Approach | Page reads for 1 row in 10⁹ |
|---|---|
| No organisation | ~10,000,000 |
| Sorted array + binary search | ~25 |
| Balanced BST | ~30 |
| **B+tree, fanout 500** | **~4** |


### Why fanout matters so much: it's in the base of the logarithm


Height is `log_f(N)`. Fanout isn't a constant factor — **it's the base of the log**, so increasing it *shrinks the exponent*:


```
log₂(1,000,000,000)   ≈ 30
log₅₀₀(1,000,000,000) ≈ 3.3
```


A ~250× increase in fanout gives a ~9× reduction in height, and each unit of height is a 50 µs disk read. **This is why databases use wide, shallow trees and why "B-tree" and "on-disk" are nearly synonymous.**


It also explains why the tree is so *insensitive* to size. Going from a million rows to a billion — a 1000× increase — adds only `log₅₀₀(1000) ≈ 1.1` levels. **One extra page read for a thousand times the data.** That robustness is a large part of why databases feel like they scale.


### And in practice it's better than 4 reads


Per chapter 04, the top of the tree lives in the buffer pool permanently — the root is on every lookup path, so LRU can never evict it, and the ~500 second-level pages are also hot. So a "4 page read" lookup is typically:


- root: **cache hit** (~100 ns)
- level 2: **cache hit** (~100 ns)
- level 3: probably a miss (~50 µs)
- leaf: miss (~50 µs)


**Two actual disk reads.** And notice how little memory buys this: the root plus all 500 second-level pages is 501 × 4 KB ≈ **2 MB**. Two megabytes of cache turns a six-billion-row table into a two-read lookup.


That's a great number to put in your README, and it's the concrete link between chapters 04 and 05: **the buffer pool and the B+tree are not two optimisations, they're one.** The tree is shaped so that its hot part is small enough to cache, and the cache is what makes the tree's height nearly free.


> **Say this out loud:** "Fanout is the base of the logarithm, so it doesn't reduce height by a constant — it changes the exponent. log₂ of a billion is 30; log₅₀₀ is 3.3. Four page reads for a six-billion-row table, and in practice two, because the top two levels are only ~2 MB and stay cached permanently."


---


## 5.4 Why not a hash table?


Hash tables give **O(1)** lookup. That's better than O(log n). Case closed?


### What a hash index actually gives you


`hash(key) → bucket number → read that page`. **One page read.** Genuinely better than four for exact-match lookup, and hash indexes are real — Postgres has them, and MySQL's MEMORY engine uses them.


### What it costs you: all of order


Hashing is designed to **destroy** order. A good hash function scatters similar keys to maximally different buckets — that's what makes it uniform, and uniformity is the whole point. Keys 100 and 101 land in unrelated places.


So every one of these becomes impossible, not slow — *impossible*:


| Query | Why a hash index can't help |
|---|---|
| `WHERE age BETWEEN 20 AND 30` | The ages in range are scattered across every bucket. You'd hash all 11 values individually, and for a `float` range you can't even enumerate them |
| `ORDER BY age` | Bucket order is meaningless. You must read everything and sort |
| `WHERE name LIKE 'ada%'` | Prefix matching needs adjacency; hashing has none |
| `WHERE id > 1000` | Unbounded range. Nothing to enumerate |
| "give me the next row after this one" | **There is no next.** No successor relation exists |
| `MIN(age)` / `MAX(age)` | Full scan. A B+tree answers both in one descent |


That last-but-one row is the deepest. A **cursor** — a position you can advance — is what makes range scans, `ORDER BY`, merge joins, and `LIMIT` short-circuiting all work. `roadmap.md` week 2 builds `TableCursor` with `first`, `last`, `seek`, `next`, `prev`. **A hash index cannot support any of those operations**, because "next" requires an ordering and hashing has deliberately discarded it.


### The other problems


- **Resizing is brutal.** The bucket count is fixed at build time. Too many rows and every bucket overflows into chains, degrading toward O(n). Growing means rehashing *everything* — reading and rewriting the entire index. There are clever incremental schemes (extendible hashing, linear hashing) which spread the cost out, and they're genuinely elegant, but they add real complexity.
- **No efficient sorted bulk load.** Building a B+tree from sorted input is a sequential write. Building a hash index scatters writes randomly across the whole file.
- **Unpredictable clustering.** A bad hash or adversarial keys produce hot buckets.


### The honest verdict


> **A B+tree gives you O(log n) lookup *and* ordered iteration from one structure. A hash table gives you O(1) lookup and nothing else.**


Databases chose the B+tree because SQL is full of order — `ORDER BY`, `BETWEEN`, `>`, `<`, `LIMIT`, `MIN`, `MAX`, merge joins, and every index range scan. Trading a 4× lookup improvement for the loss of *all* ordered access is a terrible trade for a general-purpose engine.


Which is why hash indexes exist as a **specialist** option: Postgres's are useful when you have exact-match-only queries on a large column and want a smaller index than a B-tree. A niche, correctly served, and not the default.


> **Say this out loud:** "Hashing deliberately destroys locality — that's what makes it uniform — so a hash index can do exact match in one read and literally nothing else. No ranges, no ORDER BY, no MIN/MAX, and no cursor, because 'the next key' doesn't exist without an ordering. SQL is saturated with order, so giving up all of it to make point lookups 4× faster is a bad trade for a general engine."


---


## 5.5 What the "+" means, and why every real database has it


A **B-tree** stores data in *every* node — interior nodes hold keys, payloads, *and* child pointers.


A **B+tree** stores data **only in leaves**. Interior nodes hold keys and child pointers, nothing else. They're pure navigation.


```
 B-tree:                           B+tree:
   [10:"ada" | 20:"bo"]              [ 10 | 20 ]          ← keys only, no data
    /       |        \                /    |    \
  ...      ...       ...          [..]  [..]  [..]        ← ALL data down here
                                    ↔     ↔     ↔         ← leaves often linked
```


Three consequences, and each is load-bearing:


### 1. Much higher fanout — the reason it matters most


An interior B-tree cell must hold the row payload. If rows average 80 bytes, an interior cell is ~92 bytes, so **fanout ≈ 44**.


An interior B+tree cell holds only a key and a child pointer, ~14 bytes, so **fanout ≈ 290–500**.


Height over a billion rows: `log₄₄(10⁹) ≈ 5.7` versus `log₅₀₀(10⁹) ≈ 3.3`. **Roughly two extra page reads on every single lookup**, for every query, forever.


This is the same insight as §5.3 applied one level deeper: **keep payload out of the navigation structure, so the navigation structure stays dense.** Removing data from interior nodes is *why* fanout can be 500.


### 2. Uniform depth, so predictable cost


In a B-tree, a key stored in the root is found in 1 read; a key in a leaf takes 5. Query cost varies by where the key happens to live.


In a B+tree, **every** key is in a leaf, so every lookup costs exactly the height. Uniform, predictable, and — this matters for week 4 — much easier for a query planner to estimate. A planner that can't estimate cost can't choose between plans.


### 3. Cheap full scans, and cheap range scans


All the data is in the leaves, at the bottom, in sorted order. So:


```sql
SELECT * FROM users;                    -- walk every leaf, left to right
SELECT * FROM users WHERE id > 1000;    -- descend once, then walk forward
```


A range query is **one descent plus a sequential walk.** In a B-tree you'd have to do a full in-order traversal, bouncing up and down between interior and leaf nodes, revisiting interior pages repeatedly.


Many B+tree implementations add **sibling pointers** — each leaf storing the page number of the next leaf — so the walk is a pointer-follow rather than a re-descent through the parent. Your `TableCursor` keeps a **path stack** instead (`roadmap.md` week 2), which achieves the same thing by remembering the ancestors and stepping to the parent's next child when a leaf is exhausted.


**The tradeoff between those two is worth knowing**, because it's a real one:
- **Sibling pointers**: scanning is O(1) per leaf with no stack. But every split must update the neighbour's pointer, which is a *third* page to modify (and in a concurrent setting, a notorious source of subtle locking bugs — the classic solution is Lehman & Yao's B-link tree).
- **Path stack**: nothing extra to maintain on split; the cursor holds ancestors. Slightly more work to cross a leaf boundary, and the cursor must hold pins on its whole path (chapter 04 §4.6).


`roadmap.md` chooses the path stack, which is right: **it moves complexity from the write path (which must be crash-safe in week 5 and lock-safe in week 6) into the read path (which doesn't).** That's a good general instinct — prefer complexity where the consequences of getting it wrong are smallest.


SQLite's table B-trees are B+trees by exactly this definition: keyed by 64-bit rowid with payload only in the leaves. Note the asymmetry in the format spec: a **table interior cell** is just `[4-byte child page][varint key]` — no payload, and "interior pages of table b-trees have no payload and so there is never any payload to spill." But an **index interior cell** *does* carry key-payload, because an index's key *is* its data. So SQLite is a B+tree for tables and closer to a B-tree for indexes, in the same file, with the same code.


> **Say this out loud:** "The '+' means data lives only in leaves, so interior cells are just a key plus a child pointer — which is what lets fanout be ~500 instead of ~44, saving about two page reads on every lookup. It also makes every lookup cost the same (all keys are at the same depth, so the planner can estimate) and makes range scans one descent plus a walk along the bottom."


---


## 5.6 How does it stay balanced without a rebalancing pass?


### The problem


A BST degenerates. Insert 1, 2, 3, …, 1000 in order and you get a linked list of depth 1000. Red-black trees and AVL trees fix this with rotations — explicit rebalancing operations triggered when the tree gets lopsided.


**A B+tree has no rotations and no rebalancing pass.** Yet every leaf is always at exactly the same depth, no matter what order you insert in. **How?**


Genuinely think about this. The answer is one of the most elegant things in data structures.


### The answer: it grows from the root, upward


A B+tree never adds depth at the leaves. Depth increases in **exactly one** situation: the root itself splits.


Follow the mechanism:


1. A leaf fills up. It splits into two leaves, and a separator key is promoted into the parent. **Depth unchanged** — there were leaves at depth *d*, now there are more leaves at depth *d*.
2. The parent might now be full. It splits, promoting a separator to *its* parent. **Depth unchanged.**
3. This cascades upward. Every split so far leaves depth alone.
4. **Unless it reaches the root.** The root has no parent to promote into. So you create a **brand-new root** above it, containing one key and two children — the two halves of the old root.


Only step 4 changes the height, and it changes it **for the entire tree at once**, because every path from the new root to any leaf gained exactly one level.


> **Every leaf is always at the same depth, because the only operation that changes depth changes it for all leaves simultaneously.**


That's the whole proof, and it's why balance is *structural* rather than *maintained*. There's nothing to get wrong, no imbalance metric to monitor, no rotation to trigger. It's a property of the shape of the algorithm.


`guide.md` §2.3 flags this as counterintuitive and it is: **trees you've studied grow downward; a B+tree grows upward.** Say it that way in an interview — it's memorable and it shows you understand the mechanism rather than the fact.


SQLite's function for this is named exactly what it does: `balance_deeper()`, alongside `balance_nonroot()` for the ordinary case ([`btree.c`](https://github.com/sqlite/sqlite/blob/master/src/btree.c)).


### The corollary that makes trees so shallow in practice


Height only ever increases on a root split, and a root split needs the root to fill, which needs ~500 children, which needs the level below to have filled ~500 times… Height grows *logarithmically in the number of rows*, which is why real B+trees are almost always 3–4 levels deep. **A depth-5 B+tree with fanout 500 addresses 31 billion leaves.** You will essentially never see a deeper one.


---


## 5.7 Occupancy: how full are the pages, really?


A detail that's easy to skip and comes up in interviews because it's a real operational concern.


### The invariant


After a split, each half holds about half the cells. So pages sit somewhere between **~50% and 100%** full. The minimum-occupancy invariant is what bounds the tree's height in terms of *n*: if pages could be arbitrarily empty, a tree with *n* keys could be arbitrarily deep.


### The number: ~69%


Under **random** insertion order, the steady-state average page occupancy of a B-tree is approximately **ln 2 ≈ 69%**.


The intuition: pages are constantly being filled toward 100% and then halved to 50%, so at any instant you're sampling a population uniformly distributed across that range — and integrating over the fill/split cycle gives ln 2 rather than the naive 75% midpoint.


**So a 1 GB database holds about 690 MB of data.** That ~31% overhead is the price of cheap insertion, and it's worth being able to state, because "why is my database file bigger than my data?" is a question real engineers ask.


### The sequential-insertion trap, and why it's the opposite of what you'd guess


Now insert keys in **ascending** order — which is exactly what `INSERT INTO users VALUES (1,…), (2,…), (3,…)` with an autoincrementing rowid does, i.e. *the most common insert pattern in existence*.


Every insert goes to the rightmost leaf. It fills. A naive split moves half its cells to a new page — and then **the left half is never touched again**, because all future keys are larger.


Result: **every page ends up exactly 50% full.** Your database is twice the size it needs to be, and every scan reads twice as many pages.


**The fix is a special case: on a rightmost-page split, don't split 50/50 — put almost everything in the left page and start the new right page nearly empty.** The left page is sealed at ~100% (correctly, since nothing will ever be inserted into it again) and the new page becomes the fill target.


SQLite implements exactly this and calls it **`balance_quick()`** — the append-optimised fast path ([`btree.c`](https://github.com/sqlite/sqlite/blob/master/src/btree.c), and noted in `roadmap.md`'s week 2 SQLite-counterpart section).


**This is very possibly the highest value-per-line optimisation in your entire project.** It is maybe five lines — "if the splitting page is the rightmost child of its parent, split at the last cell instead of the middle" — and it takes sequential-insert occupancy from 50% to ~100%. Every benchmark in week 8 involves inserting sequential rowids, so it directly halves your file size and your scan cost.


**And it makes a great before/after benchmark**, which is exactly the sort of artifact `roadmap.md` week 8 wants: *"sequential insert of 100k rows: 2,040 pages naive, 1,050 with rightmost-split optimisation."* That's a measured number, from a five-line change, with a clear explanation. Interviewers love those.


### The road not taken: 3-way balance


SQLite doesn't split one page into two. It **rebalances across three sibling pages**, redistributing cells among them (`balance_nonroot()`).


**Why:** it splits less often (three pages' worth of slack absorbs more inserts before anyone overflows) and achieves higher average occupancy. Fewer splits means fewer page allocations and, importantly for week 5, **fewer journalled pages per insert**.


**Why you're not doing it:** it's substantially more complex — you're redistributing a variable number of variable-length cells across three pages while maintaining sorted order and recomputing two separator keys, and every off-by-one is a silently corrupted tree.


**And here's the important thing to be clear about: this is not a format difference.** Nothing in the file format records *how* a page came to hold the cells it holds. A tree built by two-way splitting and a tree built by `balance_nonroot()` are both perfectly valid SQLite databases — `PRAGMA integrity_check` passes on either, and neither the spec nor `sqlite3` has any way to tell them apart. Yours will simply be a bit less dense.


That distinction is worth holding onto generally, because it applies to most of what you're not building:


> **The format constrains what your bytes must look like. It says almost nothing about which algorithm produced them.**


So "I don't do three-way balance" is an **implementation gap** — a thing not yet built — rather than a divergence from the format. The honest framing is *"my trees are valid and slightly less dense than SQLite's; three-way rebalancing is the fix and it's the first thing I'd add with more time,"* which is a much better sentence than one about deliberately departing from the spec. Same for page merging on delete (chapter 01 §1.8) and for `VACUUM`: legal files, lower density.


> **Say this out loud:** "Random insertion gives about ln 2 ≈ 69% average page occupancy — pages cycle between 50% and 100% full. But *sequential* insertion is pathological with naive splitting: every split abandons a half-full page that will never be touched again, so you get exactly 50% and a file twice the necessary size. The fix is to split rightmost pages asymmetrically, which is SQLite's `balance_quick()`. Five lines, and it halved my file size on sequential inserts."


---


## 5.8 The road not taken that matters most: LSM-trees


This is the comparison you're most likely to be asked about, because it's the defining storage-engine debate of the last fifteen years. Worth doing properly.


### The idea


A **Log-Structured Merge-tree** inverts the B-tree's priorities: make writes as fast as possible by never doing random I/O, and pay for it on reads.


```
   writes ──► [ memtable ]           in RAM, sorted (skip list or red-black tree)
                   │ when full, flush sequentially to disk
                   ▼
              [ SSTable L0 ]  [ SSTable L0 ]  [ SSTable L0 ]   immutable, sorted
                   │ background compaction merges them
                   ▼
              [ ───── SSTable L1 ───── ]
                   │
                   ▼
              [ ────────── SSTable L2 ────────── ]   each level ~10× larger
```


- A write goes to an in-memory sorted structure. **No disk I/O at all** (plus one sequential append to a write-ahead log for durability).
- When the memtable is full, it's written out as an immutable sorted file (an **SSTable**) in **one sequential write**.
- Background **compaction** merges SSTables, discarding overwritten and deleted keys.
- A read checks the memtable, then each level, newest first, until it finds the key.


### Why writes are so much faster


A B+tree insert does a **random write** to the leaf page (plus its journal write, plus any index pages). An LSM insert does an **in-memory** insert plus an amortised share of one **sequential** write.


Per chapter 00 §0.5, sequential beats random by 2–10× on SSD and up to 200× on spinning disk. And per chapter 00 §0.3, **the SSD's own FTL is already log-structured**, so a log-structured database is working *with* the hardware rather than against it — sequential writes cause dramatically less write amplification inside the drive.


Write throughput differences of 10× or more are routine, which is why LSM engines dominate write-heavy workloads: time series, metrics, event logs, message queues, and anything ingesting a firehose.


### What it costs


| | B+tree | LSM-tree |
|---|---|---|
| **Point read** | ~4 page reads, predictable | Check memtable + up to *L* levels; mitigated by **Bloom filters** (a small probabilistic structure that says "definitely not here" cheaply) but still more than 1 |
| **Range scan** | One descent, then walk leaves | Must **merge** iterators from every level. More work, more CPU, more pages |
| **Write** | Random write per leaf | Sequential, batched — **much faster** |
| **Space** | ~31% slack from ~69% occupancy | Obsolete versions until compaction; typically 10–30% and *variable* |
| **Read latency predictability** | Consistent | **Compaction causes latency spikes** — a background merge competes for I/O |
| **Deletes** | Remove the cell | Write a **tombstone**; space isn't reclaimed until compaction reaches it |
| **Operational complexity** | Low | **High** — compaction strategy, level sizing, write stalls, space amplification all need tuning |


That "operational complexity" row is underrated. Compaction is a whole scheduling subsystem: get it wrong and you get **write stalls** (ingestion blocked because compaction can't keep up) or **space amplification** (the database several times larger than the data). RocksDB has dozens of tuning knobs, and companies employ people to tune them.


### Who chose what, and why


| System | Structure | Why |
|---|---|---|
| SQLite, Postgres, MySQL/InnoDB, Oracle | **B+tree** | General-purpose OLTP: mixed read/write, ranges everywhere, predictable latency required |
| RocksDB, LevelDB, Cassandra, ScyllaDB, HBase | **LSM** | Write-heavy ingestion, often with a key-value API where ranges matter less |
| MongoDB (WiredTiger) | **Both** | You choose per collection |
| SQLite4 (abandoned) | **LSM** | An experiment; the design notes are public and interesting |
| InfluxDB, Prometheus | **LSM-ish** | Time series = append-mostly, the ideal LSM workload |


### The framing to actually use: the RUM conjecture


There's a clean way to express why neither wins. The **RUM conjecture** (Athanassoulis et al., EDBT 2016) holds that any access method must trade off:


- **R**ead overhead
- **U**pdate overhead
- **M**emory (space) overhead


— and you can optimise for at most two, at the cost of the third.


- **B+tree**: good reads, good space, worse updates.
- **LSM-tree**: good updates, good space, worse reads.
- **Hash index**: good reads, good updates, terrible space (and no ranges at all).


That's a genuinely useful mental tool and citing it by name is a strong move, because it reframes the question from "which is better" (no answer) to "which axis can your workload afford to lose" (an engineering question with an answer).


### The answer to give


> *"I chose a B+tree because quilldb targets general OLTP: mixed reads and writes, range queries, ORDER BY, and predictable latency. An LSM-tree would give me roughly an order of magnitude more write throughput by turning random writes into sequential ones — which also plays better with an SSD's own log-structured FTL — but it costs read amplification across levels, tombstone-delayed space reclamation, merge-based range scans, and a compaction subsystem with its own scheduling and tuning problems. That's the right trade for time-series ingestion and the wrong one for a general SQL engine. The RUM conjecture is the clean framing: B+tree optimises read and space at the cost of update; LSM optimises update and space at the cost of read."*


That's about 90 seconds, it's accurate, it names the mechanism in both directions, and it ends with a framework rather than a preference.


---


## 5.9 Other roads not taken


### Skip lists


A probabilistic alternative: a sorted linked list with randomly-assigned "express lanes" over it. O(log n) expected, much simpler to implement than a balanced tree, and famously easier to make lock-free.


**Why not:** it's a pointer-chasing structure, so it has the same problem as a BST — poor node density, and each level traversal is potentially a separate page. Skip lists shine **in memory**, which is exactly why they're the standard choice for an LSM **memtable** (RocksDB uses one) and for Redis sorted sets. Not for on-disk indexes.


The lesson generalises: **in-memory structures optimise pointer hops; on-disk structures optimise page density.** Same asymptotics, different constants, and the constants decide.


### Tries / radix trees / ART


Key-prefix-based trees. Lookup is O(key length) rather than O(log n) — independent of how many keys there are — and they share prefixes so common-prefix keys are compact.


**ART** (Adaptive Radix Tree, Leis et al. 2013) is the modern in-memory form and is genuinely excellent — used in HyPer and DuckDB.


**Why not:** poor page density again, awkward variable-length key handling on disk, and range scans are more complex. Mostly an in-memory story.


### Bitmap indexes


For low-cardinality columns (`status`, `country`, `is_active`), store a bitmap per distinct value: one bit per row. `WHERE status='active' AND country='UK'` becomes a bitwise AND of two bitmaps — extraordinarily fast, and highly compressible.


**Why not:** terrible for high cardinality (a bitmap per distinct value of a unique column is nonsense), and updates are expensive since a single row change touches multiple bitmaps. Standard in data warehouses (Oracle, and column stores generally); rare in OLTP.


Worth knowing as a *complement* rather than a competitor: it's the right index for exactly the columns a B-tree serves worst.


### Learned indexes


A 2018-onward research direction (Kraska et al., "The Case for Learned Index Structures"): train a model to predict a key's position, then do a local search to correct the prediction. Since the index is *approximating a cumulative distribution function*, and CDFs are often smooth, a small model can replace a large tree.


**Why not:** updates are the hard part (the model goes stale), worst-case guarantees are weak, and it's still largely research. But it's a genuinely interesting answer to "what's new in this space?" and shows you read beyond the textbook.


### Sorted-array-plus-overflow (ISAM)


The historical predecessor: a sorted file plus overflow chains for inserts. Fast until the overflow chains grow, then progressively worse, and it needs periodic full reorganisation. The B-tree's *actual historical contribution* was making the structure self-maintaining — no reorg pass. Worth knowing as the thing B-trees replaced, and the reason they mattered in 1972 (Bayer & McCreight).


---


## 5.10 Where the name comes from (30 seconds, and it's a fair question)


Nobody knows what the "B" stands for. Bayer and McCreight introduced it in 1972 and never said. Candidates: **B**alanced, **B**ayer, **B**oeing (where they worked), **B**ushy, **B**road. Ed McCreight later joked that "the more you think about what the B in B-trees means, the better you understand B-trees."


The "+" in B+tree *does* have a meaning: data only in leaves (§5.5).


Not important, but if someone asks and you say "nobody knows, the authors never said," that's the correct answer and it's a more interesting one than a confident guess.


---


## 5.11 What you're building


`roadmap.md` week 2, `btree/`. This chapter is the *why*; [chapter 06](06-b-tree-mechanics.md) is the *how*.


The design decisions this chapter justifies:


| Decision | Justified by |
|---|---|
| B+tree, not B-tree | §5.5 — fanout ~500 vs ~44, ≈2 fewer reads per lookup, uniform cost, cheap scans |
| B+tree, not hash | §5.4 — SQL needs order; a hash index has no cursor |
| B+tree, not LSM | §5.8 — general OLTP wants read performance and predictable latency |
| Table interior cells are `[child u32][rowid varint]` with no payload | §5.5 — payload in interior cells is what destroys fanout |
| Path-stack cursor, not sibling pointers | §5.5 — keeps complexity out of the write path, which must be crash-safe and lock-safe |
| Two-way split, not SQLite's three-way | §5.7 — simpler, and *not* a format difference: the files are valid, just less dense |
| **Add the rightmost-split optimisation** | §5.7 — five lines, halves file size on sequential inserts, makes a great benchmark |


### One thing to add to your plan


The rightmost-split special case (§5.7) isn't in `roadmap.md` or `docs/implementation/`. **Add it to week 2 session 6**, right after the basic split works. It's five lines, it's the highest value-per-line change available to you, and it produces a measurable before/after number for week 8.


Do it *after* the plain 50/50 split passes its tests, not instead of it — you want both code paths and a test that each is taken when expected.


---


## 5.12 Check yourself


1. A billion rows, no organisation. How many page reads, and how long?
2. Why is a sorted array perfect for reading and unusable for writing? What's the general insight?
3. A balanced BST over a billion keys is depth 30. Give the two reasons that's bad on disk.
4. State the reframe: in-memory algorithms optimise X, on-disk algorithms optimise Y.
5. Why does fanout matter more than a constant factor would?
6. How much cache does it take to make a six-billion-row lookup cost two disk reads? Show the arithmetic.
7. Name five queries a hash index cannot answer, and the one *operation* whose absence explains all five.
8. What does the "+" mean, and give the fanout numbers with and without it.
9. Why is a B+tree always perfectly balanced with no rotations? Give the one-sentence proof.
10. Which direction does a B+tree grow, and what's the only operation that changes its height?
11. What's the average page occupancy under random inserts? Under sequential inserts with naive splitting? What's the five-line fix and what does SQLite call it?
12. Why is an LSM-tree ~10× faster at writes? Name three things it gives up.
13. State the RUM conjecture and place B+tree, LSM, and hash index on it.
14. Why are skip lists standard for LSM memtables but not for on-disk indexes?


If 11 of 14 come out fluently you're ready for week 2. Question 11 is the one that separates people who read about B-trees from people who've built one.


---


## 5.13 Sources


- [SQLite Database File Format](https://www.sqlite.org/fileformat2.html) — table interior cells carry no payload ("there is never any payload to spill") while index cells do; interior pages hold K keys and K+1 children; the minimum-fanout-of-4 design goal for index b-trees; "all children of an interior page have the same depth in a well-formed database"; `sqlite_schema` rooted at page 1.
- [`btree.c`](https://github.com/sqlite/sqlite/blob/master/src/btree.c) — `balance()`, `balance_nonroot()` (three-sibling redistribution), `balance_deeper()` (height growth), `balance_quick()` (append fast path).
- [`btreeInt.h`](https://github.com/sqlite/sqlite/blob/master/src/btreeInt.h) — the max-embedded-payload-fraction of 64 chosen so "at least 4 cells will fit on one page"; lookup characterised as O(log(M)) page reads.
- [SQLite Query Optimizer Overview](https://www.sqlite.org/optoverview.html) — index usability rules (week 4) and the all-nested-loops join design (week 7).
- Cited by title/author/year: **Bayer & McCreight**, "Organization and Maintenance of Large Ordered Indices" (1972) — the original B-tree. **Comer**, "The Ubiquitous B-Tree" (ACM Computing Surveys, 1979) — the survey, and the source of the ln 2 ≈ 69% occupancy result. **Lehman & Yao**, "Efficient Locking for Concurrent Operations on B-Trees" (1981) — B-link trees, relevant to sibling pointers under concurrency. **Graefe**, "Modern B-Tree Techniques" (2011) — the definitive survey; skim for vocabulary only. **O'Neil et al.**, "The Log-Structured Merge-Tree" (1996) — the LSM paper. **Athanassoulis et al.**, "Designing Access Methods: The RUM Conjecture" (EDBT 2016). **Leis et al.**, "The Adaptive Radix Tree" (ICDE 2013). **Kraska et al.**, "The Case for Learned Index Structures" (SIGMOD 2018).


---


**Next:** [06 — B-tree mechanics](06-b-tree-mechanics.md) — cells, cursors, the split cascade, overflow chains, and the invariants your validator exists to enforce.