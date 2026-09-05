# 00 — Foundations: The Machine You're Actually Writing To


> **Read this before you write a line of code.** Every design decision in chapters 01–06 is a response to something in this chapter. If a later design looks arbitrary, the reason is almost always here.
>
> **Time:** 60–75 minutes. **Prerequisites:** none.


---


## 0.0 What is a database, really?


Let's start by throwing out the definition you've probably absorbed.


A database is *not* "a place to store data." A Python dictionary stores data. A text file stores data. Neither is a database.


Here is the honest definition, and it's worth reading twice:


> **A database is a program that maintains a useful data structure on a device that is slow, addressable only in large chunks, and capable of losing power in the middle of your write — while several other programs are trying to use it at the same time.**


Every hard part of this project comes from one of those four clauses:


| Clause | Consequence | Where you deal with it |
|---|---|---|
| **slow** | You must cache, and you must minimise the number of trips | Buffer pool (week 1), B+tree (week 2) |
| **addressable only in large chunks** | You must work in fixed-size pages, not bytes | Pager, slotted pages (week 1) |
| **can lose power mid-write** | You must be able to undo a half-finished change | Journal (week 5) |
| **several programs at once** | You must coordinate access | Lock manager (week 6) |


Notice what's *not* in that list: SQL. Parsing, planning, joins — all of week 3, 4 and 7 — is a *user interface* on top of the real problem. It's the most visible part and the least fundamental. That's not a reason to skip it (a database nobody can query is a data structure), but it's why the storage layer comes first.


So: to understand why databases look the way they do, you have to understand the device. Let's go all the way down.


---


## 0.1 Where does data live? (The problem of forgetting)


### The problem


You write this:


```python
users = {}
users[1] = ("ada", 36)
```


Then the program exits. Where did `users` go?


### The answer nobody tells you


It's gone, and the reason is physical, not logical.


Your computer has two fundamentally different kinds of memory, and the difference is not speed — speed is a *consequence*. The difference is **what happens when the power stops**.


**RAM (Random Access Memory)** stores each bit as a tiny electrical charge in a capacitor. A capacitor is a bucket that holds charge, and it leaks. RAM leaks so badly that the memory controller has to re-read and re-write *every bit in your entire RAM* dozens of times per second just to keep the values from decaying — this is called **refresh**, and it's why the "D" in DDR-DRAM stands for *Dynamic*. Cut the power and the refresh stops. Within a fraction of a second the charges drain and every bit becomes noise.


RAM is **volatile**. It is a whiteboard: instant to write, instant to read, and wiped clean the moment you leave the room.


**Storage (an SSD or a hard disk)** stores each bit as something that doesn't need power to persist. On a spinning hard disk it's the magnetic orientation of a microscopic patch of metal — a tiny permanent magnet pointing north or south. On an SSD it's electrons trapped inside an insulated pocket called a *floating gate*, held there by physics with nowhere to leak to. Neither needs electricity to stay put. A magnet doesn't forget. An SSD holds data for years unpowered.


Storage is **non-volatile**. It is a stone tablet: slow to carve, permanent once carved.


### Why this is the whole ballgame


Everything you build exists because of this one asymmetry:


> **The fast memory forgets. The memory that remembers is slow.**


If RAM were non-volatile, this project would not exist. You'd keep a Python dict in memory forever and be done. Databases are, in their entirety, an elaborate technology for coping with the fact that the only place your data is *safe* is a place that is *painfully slow to visit*.


Hold that thought. It explains the buffer pool, the B+tree, the journal, and why `fsync` is the most important function call in week 5.


> **Say this out loud:** "RAM is volatile because it stores charge, which leaks; storage is non-volatile because it stores magnetic orientation or trapped electrons, which don't. Databases exist to bridge that gap — fast-but-forgetful versus slow-but-permanent."


---


## 0.2 How slow is slow? (Make it physical)


### The problem


Everyone says "disk is slow." That's useless. *How* slow, and slow compared to what? Without numbers you can't reason about design at all.


### Your first instinct


"Disk is maybe 10 or 100 times slower than memory. Annoying but manageable."


### The real numbers


Here they are, with the third column being the thing that actually makes it click. The trick: **scale every duration by one billion**, so that 1 nanosecond becomes 1 second. Now the numbers land in human time.


| Operation | Real time | If 1 ns = 1 second |
|---|---|---|
| Read from L1 CPU cache | ~1 ns | **1 second** |
| Read from L2 CPU cache | ~4 ns | 4 seconds |
| Read from RAM | ~80–100 ns | **~1.5 minutes** |
| Read 4 KB at random from an NVMe SSD | ~50–100 µs | **~14–28 hours** |
| Read 4 KB at random from a SATA SSD | ~150 µs | ~1.7 days |
| `fsync()` on an SSD (force to physical media) | ~0.5–10 ms | **~6 days to 4 months** |
| Seek + read 4 KB from a spinning hard disk | ~5–10 ms | **~2–4 months** |


Read the third column again. Fetching a value from RAM takes about as long, relatively speaking, as getting up and making a cup of tea. Fetching a page from an SSD takes as long as *going on holiday*. Fetching from a spinning disk is *a season of the year*.


### The ratios you should memorise


- **RAM → NVMe SSD: roughly 500× to 1,000×.**
- **RAM → spinning disk: roughly 50,000× to 100,000×.**


`guide.md` §1.5 says "roughly 10,000 times longer," which sits between those and is a reasonable single number to carry around — but know that it's ~20× pessimistic for the SSD in your laptop and ~5× optimistic for a hard disk. If you quote "10,000× slower than RAM" about NVMe in an interview, someone who works on storage will correct you. Say "two to three orders of magnitude for SSD, four to five for spinning rust" and you'll sound like you've measured something.


### The design consequence


If one disk trip costs 500 RAM trips, then a design that does 3 disk reads beats a design that does 300, *even if the 3-read design burns ten times more CPU getting there*. You can afford to be almost arbitrarily clever in memory if it saves you a trip to the device.


This single ratio is the justification for:


- **the buffer pool** — don't go to disk twice for the same page (chapter 04)
- **the B+tree** — 4 disk reads instead of 2,400 (chapter 05)
- **indexes** — turn a 2,400-read scan into a 3-read lookup (week 4)
- **wide, shallow trees instead of binary trees** — trade many in-memory comparisons for few disk reads (chapter 05)


Every one of those is the same trade: **spend CPU and RAM freely; hoard disk trips like they're money.**


> **Say this out loud:** "A random 4 KB read from NVMe is ~50–100 µs against ~100 ns for RAM — call it 500–1000×. So the entire discipline is minimising the *number of page touches*, not the number of CPU instructions. That's why B-trees are wide and shallow rather than binary."


---


## 0.3 Why can't you write just one byte?


### The problem


You have a 1 GB file. You want to change byte number 5,000 from `A` to `B`. One byte. How much work is that for the device?


### Your first instinct


"One byte. Obviously. Maybe there's some overhead, but fundamentally it's one byte of work."


### Why that's wrong


The device physically cannot do it.


Storage devices are **block devices**. They do not expose individual bytes. The smallest unit a device can write in one operation is called a **sector**, and SQLite's own documentation defines it exactly this way:


> "A sector is the minimum amount of data that can be written to mass storage in a single go... It is not possible to modify any part of the disk smaller than a sector."
> — [atomiccommit.html §2](https://www.sqlite.org/atomiccommit.html)


Historically a sector was **512 bytes**. On modern drives it is **4096 bytes** (this transition is called *Advanced Format*; drives that internally use 4096 but pretend to be 512 for compatibility are called *512e*, and ones that admit it are *4Kn*).


So changing one byte actually means:


1. **Read** the whole 4096-byte sector containing byte 5,000 into memory.
2. **Modify** the one byte in memory.
3. **Write** all 4096 bytes back.


This is called **read-modify-write**, and it means:


> **Writing 1 byte costs the same as writing 4096 bytes. Sometimes more, because of the extra read.**


### Why devices are built this way


Two reasons worth knowing, because they're different for the two device types:


**On a spinning disk,** the platter is physically divided into concentric tracks, and each track into arcs. Each arc carries not just your data but a *preamble* to let the read head synchronise its timing, and *error-correcting codes* to recover from the inevitable bit flips. That overhead is fixed per arc. If sectors were 1 byte, the overhead would dwarf the data by orders of magnitude. Large sectors amortise it. The 512→4096 transition happened precisely because bigger sectors mean proportionally less ECC overhead and therefore more usable capacity on the same platter.


**On an SSD,** it's stranger and more severe. Flash memory has an asymmetry: you can *read* a small unit (a flash page, typically 4–16 KB) but you can only *erase* a much larger unit (a flash block, typically 128 KB–4 MB, i.e. hundreds of pages). And flash cells can only be written after being erased — you can't overwrite in place at all. So "modify these 4 KB" internally becomes something like: write the new 4 KB to a fresh pre-erased page somewhere else, update an internal mapping table so the old address now points at the new location, and mark the old page as garbage to be erased later in bulk.


That translation layer is called the **FTL (Flash Translation Layer)**, and it means an SSD is *already* a log-structured database with garbage collection, hidden inside your hard drive, lying to you about being a simple array of sectors. The extra physical writing it does is called **write amplification**.


Two things follow that are worth carrying:


- Nothing you do in Python controls this. You issue a 4 KB write; the FTL decides what actually happens to the flash. This is one reason "durability" has a hardware floor you cannot reach from userspace (§0.6).
- The idea you'll implement in weeks 1–2 — fixed-size pages, indirection from logical to physical location, free space reclaimed in bulk — is the *same idea* the SSD firmware uses. You're not learning an archaic technique; you're learning the technique, at a different layer.


### The design consequence


Since the device charges you per sector regardless, the only sane strategy is:


> **Choose a unit of work that is a whole number of sectors, always read and write exactly that unit, and make every unit you fetch worth fetching.**


That unit is called a **page**. This is the single most important structural decision in the project, and it's forced on you by hardware, not chosen for elegance. Chapter 01 is entirely about it.


> **Say this out loud:** "Devices are block-addressed — you can't write less than a sector, historically 512 bytes, now 4096. So a one-byte update is a read-modify-write of the whole sector. That's why databases work in fixed-size pages: the device already charges per block, so you may as well make the block your unit of accounting."


---


## 0.4 Why 4096 bytes specifically?


### The problem


Your `constants.py` says `PAGE_SIZE = 4096`. Where does that number come from? Why not 1024, or 65536?


### The layered answer


Four different things in your stack are 4 KB, and that's not a coincidence:


| Layer | Unit | Typical size |
|---|---|---|
| Physical drive sector (Advanced Format) | sector | **4096 B** |
| Filesystem block (ext4, APFS, NTFS defaults) | block | **4096 B** |
| OS virtual memory page (x86-64, ARM64) | page | **4096 B** |
| Your database page | page | **4096 B** |


When all four align, a single database page read is exactly one filesystem block, exactly one device sector, exactly one memory page. Nothing straddles a boundary, so nothing costs double. Pick 4097 and every single page read touches two filesystem blocks and two sectors — you'd roughly double your I/O for no gain. Pick 1024 and four of your pages share one sector, so writing one of them read-modify-writes the other three.


**Why not larger, like 65536?** Because of *read amplification*. If you need one 100-byte row and your page is 64 KB, you fetched 64 KB to use 100 bytes. Bigger pages help sequential scans (fewer, larger requests) and hurt point lookups (more waste per lookup). 4 KB is the compromise the whole industry has landed on, and it's why the alignment above exists at all.


**Why not smaller, like 512?** Fanout. A B+tree's height depends on how many keys fit on an interior page (chapter 05). Smaller pages mean fewer keys per page, more levels, more disk reads per lookup. Also, on a 4096-byte-sector drive, a 512-byte page is a read-modify-write every time.


### ⚠️ Be honest about what SQLite actually claims


Here's where a lot of blog posts overstate things, and you should not.


SQLite's default page size **was 1024 bytes** from the format's design in 2003 until **version 3.12.0 (2016-03-29)**, when it changed to **4096**. The reason SQLite gives, in full, is:


> "This was a reasonable choice in 2003. But on modern hardware, a 4096 byte page is a faster and better choice."
> — [pgszchng2016.html](https://www.sqlite.org/pgszchng2016.html)


That's it. That page makes **no mention** of sector size, filesystem block size, or MMU pages. It offers **no benchmarks**. The alignment argument in the table above is the correct general engineering reason, and it's certainly what "on modern hardware" is gesturing at — but it is *my* reasoning and the industry's, not a quote from SQLite. Don't attribute it to them.


Two genuinely interesting details that page *does* give, both good interview colour:


- They simultaneously changed the default cache size from `2000` (meaning 2000 pages) to `-2000` (meaning 2000 × 1024 bytes ≈ 2 MB). Otherwise quadrupling the page size would have quadrupled page-cache memory for every application in the world overnight. **A negative number meaning "bytes instead of pages" is a real API in a real system** — that's what backwards compatibility looks like when you can't change a parameter's type.
- An *empty* database got 4× larger, because the minimum size is one page per table and per index. But: "Due to relaxed bin-packing constraints, the 4096-byte page size might actually result in a *smaller* file, once substantial content is added." Bigger boxes waste proportionally less space at the end — the same reason a bigger suitcase packs more efficiently.


### The road not taken


`roadmap.md` §1.3 fixes 4096 as a constant and cuts configurable page sizes. That's correct for your hours, and the reasoning is worth having ready: configurability here is a one-line claim you cannot demo, and it costs you a variable in every arithmetic expression in the storage layer. SQLite supports 512 through 65536 because it ships on wristwatches and on servers and must serve both. You ship on one machine.


> **Say this out loud:** "4096 because it's the alignment point where the device sector, the filesystem block, and the OS memory page all agree — so one page read is exactly one of each, with nothing straddling a boundary. SQLite's own default moved from 1024 to 4096 in 3.12.0, though their stated reason is just 'modern hardware.'"


---


## 0.5 Sequential vs random: the access pattern that changes everything


### The problem


You need to read 1,000 pages. Does it matter *which* 1,000, or in what order?


### Your first instinct


"1,000 pages is 1,000 pages. 4 MB of reading either way."


### Why that's wrong, and by how much


Reading 1,000 pages that happen to be adjacent — pages 100 through 1,099 — is dramatically faster than reading 1,000 pages scattered across the file.


**On a spinning disk the reason is mechanical and enormous.** There is a physical arm holding a read head, and a platter spinning at 5,400 or 7,200 RPM. To read a given sector you must (a) move the arm to the right track — a **seek**, ~3–10 ms, an actual motor moving actual metal — and (b) wait for the sector you want to rotate underneath the head — **rotational latency**, averaging half a revolution, ~4 ms at 7,200 RPM. That's ~8 ms of pure waiting before a single byte transfers. But once the head is parked on a track, the data streams past at 100–200 MB/s with no further waiting.


So: 1,000 random reads ≈ 1,000 × 8 ms ≈ **8 seconds**. 1,000 sequential reads ≈ one seek plus 4 MB of streaming ≈ **~30 ms**. Same amount of data. **Two hundred times** the difference. This is why every classic database paper is obsessed with sequential access — the authors were writing for machines where random I/O was catastrophic.


**On an SSD the gap shrinks but doesn't close.** There's no arm and no platter, so no seek and no rotation — random reads are genuinely fast, which is the main reason SSDs felt revolutionary. But sequential still wins, for three reasons: large sequential requests can be split across multiple flash chips and served in parallel; the drive and the OS both *read ahead*, speculatively fetching the next blocks because you'll probably want them; and per-request overhead (the command, the queue entry, the interrupt) is fixed, so fewer bigger requests beat more smaller ones. Typical modern gap: **2× to 10×**, not 200×.


### The design consequence, and it cuts both ways


This is the tension that produces most of the interesting variety in storage engines:


- **Point lookups want random access.** "Give me the user with id 5,000" ideally touches 3 pages, wherever they are.
- **Scans and analytics want sequential access.** "Average age of all users" wants to stream the whole table with zero seeks.
- **A B+tree gives you a decent version of both**, which is exactly why it won: point lookup in O(log n) page reads, *and* ordered iteration by walking the leaves. Chapter 05 develops this.


But note the honest caveat, and it's a good one to have ready: a B+tree's leaves are only *logically* sequential. After thousands of splits, leaf 5 might live at page 900 and leaf 6 at page 41. So a "sequential scan" of an old, heavily-updated B+tree can degenerate into random I/O. This is called **fragmentation**, and it's what `VACUUM` fixes by rewriting the file in logical order. `roadmap.md` §11 correctly defers `VACUUM` — but knowing *why* it exists is free.


> **Say this out loud:** "Sequential beats random by ~200× on spinning disk (seek plus rotational latency ~8 ms versus streaming at 150 MB/s) and still by 2–10× on SSD (parallelism across flash chips, readahead, and fixed per-request overhead). B+trees are a compromise that serves both patterns, though their leaves drift out of physical order as the tree ages — which is what VACUUM exists to fix."


---


## 0.6 The lie in `write()` — buffering, ordering, and `fsync`


This is the most important section in the chapter. Week 5 is entirely a consequence of it, and it's the single most common thing real applications get wrong.


### The problem


```python
f.write(b"important data")
```


The call returns. No exception. Is the data on the disk?


### Your first instinct


"Yes? The function is called `write`. It returned successfully. If it hadn't worked it would have raised."


### Why that's wrong


**No.** The data is almost certainly still in RAM, and there is no guarantee about when it will reach the device — or in what order relative to your other writes.


Here's what actually happens. Between you and the platter there are three or four layers of buffering, and each one is happy to hold your bytes:


```
    your Python code
         │  f.write(b"...")
         ▼
 ┌───────────────────────┐
 │  Python's own buffer  │   io.BufferedWriter — bytes may not have even
 │  (userspace)          │   reached the OS yet. Cleared by f.flush().
 └───────────────────────┘
         │  write() syscall
         ▼
 ┌───────────────────────┐
 │  OS page cache        │   The big one. The kernel keeps your bytes in RAM,
 │  (kernel RAM)         │   marks the page "dirty," and returns success
 │                       │   immediately. A background thread writes it out
 │                       │   "eventually" — often 5–30 seconds later, in
 │                       │   whatever order it finds convenient.
 └───────────────────────┘
         │  eventually, reordered
         ▼
 ┌───────────────────────┐
 │  Drive's own cache    │   The physical device has RAM too (tens of MB).
 │  (volatile, on-drive) │   It reports "done" when bytes land here, not
 │                       │   when they reach flash or magnetic media.
 └───────────────────────┘
         │  eventually
         ▼
   ✅ actually durable
```


So `write()` returning success means precisely one thing: *"the kernel has accepted responsibility for your bytes."* If the power fails in the next 30 seconds, the kernel takes that responsibility to the grave with it.


### Why the OS does this to you


Not malice — it's the right default for almost every program. Buffering lets the kernel:


- **Return instantly** instead of blocking your program for milliseconds.
- **Batch** many small writes into one big sequential write (see §0.5 — this is a huge win).
- **Coalesce** repeated writes to the same block: if you write byte 5 a hundred times, the disk sees one write.
- **Reorder** writes into the order the device prefers, minimising seeks.


For a text editor, this is free performance. For a database, **that fourth bullet is a loaded gun**. Your entire crash-safety scheme in week 5 depends on "the journal must reach disk *before* the data pages." The kernel does not know or care about your ordering requirement and will cheerfully do it backwards.


### The one tool you get: `fsync`


```python
f.flush()        # Python's buffer → the kernel.  NOT ENOUGH.
os.fsync(f.fileno())   # kernel → the physical device.  This is the real one.
```


`os.fsync(fd)` means: *"do not return until every pending write for this file is on non-volatile media."* It blocks. It is slow — 0.5 to 10 milliseconds, which per §0.2 is a **week to four months** in human-scaled time. It is the most expensive thing your database will do, and the reason `PRAGMA synchronous` exists at all.


Note the two-step: `f.flush()` alone only moves bytes from Python's buffer to the kernel. It does *not* touch the disk. `os.fsync()` without a preceding `flush()` may sync a file the kernel doesn't yet have all your bytes for. **You need both, in that order.** This is a real bug people ship.


`fsync` is what turns ordering from a suggestion into a guarantee. It's a **fence**: nothing after it starts until everything before it is durable. That's why week 5's protocol looks like this, and why the fsyncs aren't decoration:


```
write journal → fsync → write data pages → fsync → delete journal
                  ▲                          ▲
                  └── fence ─────────────────┘
```


### What SQLite assumes about your hardware — the honest contract


This is the best material in the whole chapter and almost nobody reads it. [atomiccommit.html §2](https://www.sqlite.org/atomiccommit.html) lists, explicitly, every assumption SQLite makes about the machine underneath it. Being able to recite even three of these puts you ahead of most working engineers:


| # | SQLite assumes | Why it matters to you |
|---|---|---|
| 1 | A sector is the minimum writable unit; historically assumed 512 bytes, and as of 3.5.0 the VFS *still* returned a hardcoded 512 because "there is no standard way of discovering the true sector size on either Unix or Windows" | You cannot reliably ask the OS how big a sector is. Assume the worst. |
| 2 | **Sector writes are NOT atomic.** A power cut mid-sector can leave it partially updated | This is the *torn write*, and it's the thing your journal defends against |
| 3 | **Sector writes ARE linear** — "if any part of the sector gets changed, then either the first or the last bytes will be changed. So the hardware will never start writing a sector in the middle and work towards the ends. *We do not know if this assumption is always true but it seems reasonable.*" | Note the honesty of that last sentence. This assumption is what makes a checksum or a sentinel value at a known position meaningful |
| 4 | The OS buffers writes and **will reorder them**; fsync doesn't return until pending writes complete. But: "we are told that the flush and fsync primitives are broken on some versions of Windows and Linux... there is nothing that SQLite can do to test for or remedy the situation... hopefully you will not lose power too often" | Your ordering is only as good as `fsync`, and `fsync` is only as good as the OS and drive |
| 5 | **File size is updated before file content.** Growing a file may briefly expose garbage where the new bytes will be | This is why week 5's journal page count starts at zero — a torn journal must roll back *nothing* |
| 6 | **File deletion appears atomic** from a user process's view: after a crash the file either exists entirely with original content, or isn't in the filesystem at all | **This is the one atomic primitive the whole design rests on.** See §0.7 |
| 7 | Bit-error detection is the hardware's job. "SQLite does not add any redundancy to the database file for the purpose of detecting corruption" | The database file has no checksums. The *journal* does. Asymmetric, and deliberate |
| 8 | **Powersafe overwrite**: writing a byte range won't damage bytes outside that range, even on power loss. Assumed by default *only since 3.7.9 (2011)* — "with the standard sector size increasing from 512 to 4096 bytes on most disk drives, it has become necessary to assume powersafe overwrite in order to maintain historical performance levels" | A performance-driven weakening of a safety assumption, documented in public. That's what engineering honesty looks like |


Assumption 8 is worth pausing on: the 512→4096 sector transition (§0.3) made SQLite's old pessimism too expensive, so they adopted a *less* safe assumption to keep performance. They wrote it down, explained it, and made it configurable. That's a much better story than "we made it safe."


### And when `fsync` lies anyway


Three ways durability fails below the level you can control, all from [atomiccommit.html §9.2](https://www.sqlite.org/atomiccommit.html):


- **The drive lies.** "Often the IDE disk control lies and says that data has reached oxide while it is still held only in the volatile control cache." Consumer drives have historically ignored flush commands because it makes their benchmark numbers better.
- **macOS `fsync` is not enough.** You need `fcntl(F_FULLFSYNC)`, exposed as `PRAGMA fullfsync=ON`. SQLite's own verdict: "the implementation of fullfsync involves resetting the disk controller. And so not only is it profoundly slow, it also slows down other unrelated disk I/O. So its use is not recommended." A correctness feature they recommend against, on performance grounds.
- **`FlushFileBuffers()` on Windows can be disabled by registry setting.**


And one that catches people even when the hardware is honest: **creating a file isn't durable until you fsync the file's parent directory too.** The file's *contents* are covered by fsyncing the file; the *directory entry* saying the file exists is a different piece of metadata in a different place. Crash at the wrong moment and you find the journal you carefully wrote and synced does not exist. SQLite does this — §9.5 mentions "opening and syncing the directory containing the rollback journal at the same time it syncs the journal file itself."


### The takeaway


> **Durability is not a boolean. It's a stack of assumptions, and you can only be as durable as the weakest layer you can't see.**


This is why `docs/durability.md` (roadmap week 5) must state your **non-guarantees**. "I implemented an fsync-ordered undo journal; it is atomic against process crash and OS crash; it is atomic against power loss *only if* the drive honours flush commands, which some don't, and *only if* sector writes are linear, which SQLite assumes and cannot verify" is a sentence that will make an interviewer sit up. "It's ACID" is a sentence that invites them to find out whether you know what that means.


> **Say this out loud:** "`write()` only means the kernel took your bytes — they sit in the page cache for seconds and may be reordered. `fsync` is the fence that makes ordering real, and it costs 0.5–10 ms. Below that there's a hardware floor you can't reach from userspace: drives that ignore flush, macOS needing F_FULLFSYNC, and non-atomic sector writes. So durability claims have to name their assumptions."


---


## 0.7 What does "atomic" mean when the hardware isn't?


### The problem


You want this to be all-or-nothing:


```
account A: 100 → 50
account B:  20 → 70
```


If the power fails between those two writes, £50 has evaporated. You need both or neither. But §0.6 just established that you don't control write ordering, sector writes aren't atomic, and the drive might be lying. **How do you build an all-or-nothing operation out of parts that are individually not all-or-nothing?**


Genuinely stop and think about this one. It's the deepest idea in the project.


### Wrong answer 1: "Write carefully, in the right order"


You can't. Ordering is only enforceable with `fsync`, and fsync is slow, and even *with* correct ordering a crash still lands you somewhere in the middle. Ordering reduces which intermediate states are possible; it never eliminates intermediate states.


### Wrong answer 2: "Write to a temp file, then rename it over the original"


This is the single most popular wrong answer, and it's popular because it's *nearly* right. `rename()` is atomic in normal operation — POSIX guarantees a reader sees either the old file or the new one, never a mixture.


But it is **not atomic across a crash.** The rename is a metadata change that itself sits in the page cache and can be reordered relative to your data writes. You can crash and find the rename landed but the file contents didn't. Dan Luu's ["Files are hard"](https://danluu.com/file-consistency/) has a whole update section on this, added because it was the most common reader suggestion. You will be tempted by it in week 5. Don't be.


It also doesn't scale to your actual problem: you're modifying 5 pages of a 1 GB file. Rewriting a gigabyte to change 20 KB is not a plan.


### Wrong answer 3: "Checksum everything, detect corruption on read"


Detection is not recovery. Knowing the file is broken doesn't tell you what it said before. You'd need the old data, which means... keeping a copy of the old data, which is the right answer, arrived at the long way round.


### The actual answer: find one atomic thing and lever everything off it


Here's the move, and it's beautiful.


You can't make writes atomic. But there is *one* operation the filesystem gives you that a process can only ever see two outcomes of. From [atomiccommit.html §2](https://www.sqlite.org/atomiccommit.html), assumption 6:


> "If SQLite requests that a file be deleted and the power is lost during the delete operation, once power is restored either the file will exist completely with all of its original content unaltered, or else the file will not be seen in the filesystem at all."


**"Does this file exist?"** is a question with no third answer. Not because deletion is truly atomic — it isn't, internally — but because a user process has no way to observe a half-deleted file. SQLite is explicit about this being an appearance rather than a reality:


> "Deleting a file is not really an atomic operation, but it appears to be from the point of view of a user process."


So: take the one binary question you can trust, and make it *mean* "did the transaction happen?"


```
1. Copy the ORIGINAL contents of every page you're about to change
   into a separate file — the journal.
2. fsync the journal.                        ← the copies are safe
3. Now modify the real pages.
4. fsync the database.                       ← the changes are safe
5. DELETE the journal.                       ← ⭐ THE TRANSACTION COMMITS HERE
```


And on every open, ask the one trustworthy question:


- **Journal exists?** → someone was interrupted → copy the originals back → the transaction never happened.
- **No journal?** → nothing was in flight → proceed.


Every possible crash point maps to exactly one of those two branches:


| Crash during | Journal on disk? | Recovery does | Result |
|---|---|---|---|
| Step 1 (writing journal) | partial | rolls back nothing useful — the DB was never touched | **before** state |
| Step 3 (writing DB pages) | complete | copies originals back over the half-written pages | **before** state |
| Step 5 (during the delete) | either yes or no, never half | rolls back, or doesn't | **before** or **after** |
| After step 5 | no | nothing | **after** state |


There is no crash point that yields a mixture. That's atomicity, constructed entirely out of non-atomic parts plus one trustworthy question.


SQLite states the argument as a syllogism, and it's worth memorising verbatim-ish:


> "The existence of a transaction depends on whether or not the rollback journal file exists and the deletion of a file appears to be an atomic operation from the point of view of a user-space process. Therefore, a transaction appears to be an atomic operation."


### The generalisable lesson


This pattern — **find the one primitive your substrate gives you atomically, then encode your whole notion of "committed" into that primitive** — recurs everywhere in systems. It's `rename()` in build tools, a single-row insert in a distributed saga's ledger, a compare-and-swap on one word in a lock-free data structure, one atomic pointer swap in a copy-on-write B-tree. The specific primitive changes; the move doesn't.


If you internalise one thing from this chapter, make it this. It's the answer to a very large family of "how do you make X safe?" questions.


### Two refinements SQLite adds, both cheap and both worth copying


- **The journal's page count starts at zero**, and is only written after the journal body is synced. Because of assumption 5 (file size updates before content, §0.6), a journal that was still being written may contain garbage. A journal claiming "zero pages" rolls back nothing — harmlessly. Then the count is written and synced separately. This is why `synchronous=FULL` does **two** fsyncs on the journal: "once to write the page content and a second time to write the page count in the header." And note this detail: "The rollback journal header is always kept in a separate sector from any page data so that it can be overwritten and flushed without risking damage to a data page."
- **Every journaled page carries a 32-bit checksum.** If a checksum is wrong, "the rollback is abandoned" — a corrupt journal is refused rather than smeared into your database. SQLite is candid that this is a probabilistic defence: "the checksum does not guarantee that the page data is correct since there is a small but finite probability that the checksum might be right even if the data is corrupt." Strictly it's only *needed* at `synchronous=NORMAL`, where only one fsync happens, but they include it always because "the checksums never hurt."


You'll build both in week 5. They're maybe 15 lines together and they're the difference between a journal and a *trustworthy* journal.


> **Say this out loud:** "You can't make writes atomic, so you find the one thing that is — from a user process's view, a file either exists or it doesn't — and you make that the commit point. Write the original pages to a journal, fsync, write the real pages, fsync, then delete the journal. The journal's existence *is* the transaction's existence, so recovery is just 'does this file exist?', which has no third answer."


---


## 0.8 The three eternal problems


Step back. Everything in every database ever built is an answer to one of three questions.


### Problem 1: Find it fast


**How do you locate one record among a billion without reading a billion records?**


The only general answer is **organise before you search**. Sorting, hashing, and partitioning are the three families, and each buys a different capability:


| Structure | Point lookup | Range scan | Ordered iteration | Insert cost |
|---|---|---|---|---|
| Unsorted heap | O(n) — read everything | O(n) | ✗ needs a sort | O(1), append |
| Sorted array | O(log n) compares, but **O(n) page rewrites to insert** | ✓ excellent | ✓ | **unusable** |
| Hash table | O(1) | ✗ **impossible** | ✗ **impossible** | O(1) |
| **B+tree** | **O(log n) page reads** | **✓** | **✓** | **O(log n)** |
| LSM-tree | O(log n) × levels | ✓ (merged) | ✓ | **O(1) amortised, very fast** |


The B+tree wins not by being best at anything but by being *good at everything and terrible at nothing*. Chapter 05 derives this properly.


### Problem 2: Change it safely


**How do you modify data such that a crash at any instant leaves a coherent state?**


Answered in §0.7. The design space:


| Approach | Idea | Used by |
|---|---|---|
| **Undo journal** (your choice) | Save the OLD data, then overwrite in place. Crash → restore old | SQLite rollback mode |
| **Redo log / WAL** | Save the NEW data to a log first, apply to the real file later. Crash → replay the log | Postgres, MySQL, SQLite WAL mode |
| **Copy-on-write / shadow paging** | Never overwrite. Write new versions elsewhere, then atomically swap one root pointer | LMDB, BoltDB, ZFS, btrfs |
| **Log-structured** | Only ever append. The log *is* the database; compact in the background | LSM engines (RocksDB, Cassandra), and your SSD's own FTL |


You're building undo. Chapter 05's roads-not-taken section and the (future) week-5 chapter cover why.


### Problem 3: Share it safely


**How do several readers and writers use the same data without seeing each other's half-finished work?**


| Approach | Idea | Cost |
|---|---|---|
| **Nothing** — one user at a time | A global lock | Correct, useless |
| **Locking (2PL)** — your choice | Take shared locks to read, exclusive to write, hold until commit | Readers block writers and vice versa |
| **MVCC** | Keep multiple versions; each reader sees a consistent snapshot; readers never block writers | Storage overhead, garbage collection, and *write skew* if you're not careful |


You're building table-level two-phase locking with deadlock detection (week 6). Postgres, Oracle, and modern SQL Server use MVCC. SQLite locks the whole file, which is *coarser* than what you're building — and the reason is genuinely interesting: SQLite coordinates between separate OS processes through the filesystem, where fine-grained locking would be prohibitively expensive. You're single-process with shared memory, so coordination is cheap and you can afford finer locks. **Being finer-grained than SQLite here isn't you being cleverer; it's you having an easier problem.** Say it that way.


---


## 0.9 Where every week comes from


The whole project, as a chain of consequences from physics:


```
Storage is non-volatile but slow, and block-addressed
        │
        ├──► so work in fixed-size numbered pages ─────────► WEEK 1  pager, slotted pages
        │
        ├──► so cache pages in RAM, bounded ──────────────► WEEK 1  buffer pool
        │
        ├──► so make bytes-per-page count ────────────────► WEEK 1  varints, records
        │
        ├──► so minimise page touches per lookup ─────────► WEEK 2  B+tree
        │                                                   WEEK 4  indexes
        │
        ├──► and writes are buffered, reordered,
        │    and non-atomic ─────────────────────────────► WEEK 5  journal + fsync ordering
        │
        └──► and several threads want it at once ─────────► WEEK 6  lock manager


                    ... and humans want to ask questions in English
                                        └────────────────► WEEK 3  SQL frontend
                                                            WEEK 7  joins, aggregation


                    ... and a stranger has to believe you
                                        └────────────────► WEEK 8  README, docs, benchmarks
```


Weeks 1, 2, 5, 6 are physics. Weeks 3 and 7 are ergonomics. Week 8 is communication. All three kinds of work are real; knowing which kind you're doing keeps you from over-engineering the ergonomics or under-investing in the communication.


---


## 0.10 Vocabulary


Everything introduced above, in one place. If you can't define one of these from memory, reread that section.


| Term | Definition | §|
|---|---|---|
| **Volatile / non-volatile** | Loses contents without power / doesn't | 0.1 |
| **DRAM refresh** | Periodic re-reading and re-writing of every bit to stop charge decay | 0.1 |
| **Block device** | A device addressable only in fixed-size chunks, not bytes | 0.3 |
| **Sector** | The smallest unit a device can write in one operation (512 B historically, 4096 B now) | 0.3 |
| **Advanced Format / 4Kn / 512e** | Drives with 4096-byte physical sectors; 512e ones pretend to be 512 | 0.3 |
| **Read-modify-write** | Changing part of a sector requires reading, editing, and rewriting all of it | 0.3 |
| **FTL (Flash Translation Layer)** | SSD firmware mapping logical addresses to physical flash, because flash can't be overwritten in place | 0.3 |
| **Write amplification** | Physical bytes written exceeding logical bytes requested | 0.3 |
| **Page** | The database's fixed-size unit of I/O and accounting; here, 4096 bytes | 0.3–0.4 |
| **Seek / rotational latency** | Moving a disk arm / waiting for the platter to bring your sector around | 0.5 |
| **Readahead** | Speculatively fetching subsequent blocks on the guess you'll want them | 0.5 |
| **Fragmentation** | Logically adjacent data living far apart physically, turning scans into random I/O | 0.5 |
| **OS page cache** | Kernel RAM holding file data; why `write()` returns before anything is durable | 0.6 |
| **Dirty page** | A cached page modified in memory but not yet written to storage | 0.6 |
| **`fsync`** | "Don't return until this file's pending writes are on non-volatile media" | 0.6 |
| **Write barrier / fence** | An ordering guarantee: nothing after starts until everything before is durable | 0.6 |
| **Torn write** | A sector left partially updated by a crash mid-write | 0.6 |
| **Powersafe overwrite** | The assumption that writing a byte range can't damage bytes outside it | 0.6 |
| **Atomicity** | All of an operation happens, or none of it | 0.7 |
| **Journal** | A file holding data needed to undo (or redo) an in-flight change | 0.7 |
| **Commit point** | The exact instant a transaction becomes durable; here, deleting the journal | 0.7 |
| **Hot journal** | A journal found at open time, proving the previous run was interrupted | 0.7 |
| **Undo vs redo logging** | Save the old data and restore it / save the new data and replay it | 0.8 |
| **Copy-on-write / shadow paging** | Never overwrite; write new versions and swap a root pointer | 0.8 |
| **MVCC** | Multi-version concurrency control: readers see snapshots and never block writers | 0.8 |


---


## 0.11 Check yourself


Answer out loud, in full sentences, without looking. These are all real interview questions.


1. Why is RAM fast and volatile while storage is slow and permanent? Name the physical mechanism for each.
2. How much slower is a random 4 KB NVMe read than a RAM read? Than a spinning-disk read?
3. What does it cost to change one byte in the middle of a 1 GB file, and why?
4. Why is 4096 a better page size than 1024 or 65536? Which part of that argument is SQLite's own and which is general engineering reasoning?
5. Sequential vs random: what's the gap on a hard disk, and *why* is it smaller on SSD?
6. `f.write(b"x")` returns without error. Name three places those bytes could be sitting, none of them the disk.
7. Why must the journal fsync happen *before* the data pages are written, rather than after?
8. Why is *deleting* the journal the commit point rather than *writing* the data pages?
9. Why does the journal's page count start at zero?
10. Name three ways your durability guarantee can fail for reasons entirely outside your code.
11. Why does a hash index make `WHERE age BETWEEN 20 AND 30` impossible rather than merely slow?
12. SQLite locks the whole database file; you're locking individual tables. Why is that not you being better than SQLite?


If 8 of 12 come out fluently, you're ready for week 1.


---


## 0.12 Sources


Primary sources, fetched and verified. Where I've reasoned beyond what a source states, it's marked in the text.


- [Atomic Commit In SQLite](https://www.sqlite.org/atomiccommit.html) — §2 hardware assumptions (the table in §0.6 is drawn directly from it), §3 commit sequence, §3.11 the commit-point syllogism, §4.2 hot-journal conditions, §6.2 zero page count and checksums, §9 failure modes. **The most valuable document in this project.**
- [SQLite Database File Format](https://www.sqlite.org/fileformat2.html) — sector and page size constraints, header layout.
- [Default Page Size Change in SQLite 3.12.0](https://www.sqlite.org/pgszchng2016.html) — the 1024→4096 change, the `-2000` cache-size trick, the bin-packing note. Also the source of the caveat in §0.4 that SQLite does *not* give the sector-alignment argument.
- [SQLite Architecture](https://www.sqlite.org/arch.html) — layer responsibilities; "The page cache is responsible for reading, writing, and caching these pages... also provides the rollback and atomic commit abstraction and takes care of locking of the database file."
- [Files are hard](https://danluu.com/file-consistency/) — Dan Luu. The temp-file-and-rename rebuttal in §0.7, the incremental hardening of a single overwrite, drives that ignore flush.
- [Ensuring data reaches disk](https://lwn.net/Articles/457667/) — LWN. What `write`, `fsync`, `fdatasync`, `sync_file_range` each actually guarantee.


Latency figures in §0.2 are conventional industry numbers (the "latency numbers every programmer should know" lineage, updated for NVMe) — treat them as correct orders of magnitude, not measurements of your machine. Measure your own in week 8's benchmark harness; that's a better README line than a quoted table anyway.


---


**Next:** [01 — Pages and the pager](01-pages-and-pager.md) — why fixed-size numbered pages beat every alternative, and why the pager indirection is the most valuable boundary you'll draw all project.



