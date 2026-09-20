# quilldb — The Explained Guide


A companion to `roadmap.md` for someone who has never built a database before. The roadmap tells you
*what* to build. This tells you *what those words mean*, *why the thing exists*, and *what to type on
each of the ~58 working sessions*.


Assumed background: comfortable Python. No database internals, no C, no systems background.


> **Going deeper.** This guide gives you the working mental model. `docs/theory/` gives you the
> theory underneath it — how storage hardware actually behaves, why each design is shaped the way it
> is, what the alternatives were, and why SQLite chose as it did, all sourced to primary documents.
> Read [`docs/theory/foundations/00-foundations.md`](docs/theory/foundations/00-foundations.md) before week 1; it's the
> chapter every other decision follows from. Chapters 01–06 cover weeks 1–2 in depth.
>
> Two places where the theory chapters **correct** this guide: the RAM-vs-disk ratio in §1.5 (see
> theory §0.2) and the varint maximum length in §1.3 (see theory §3.4).
>
> **One project-wide fact this guide now assumes:** quilldb writes the **real SQLite on-disk format**,
> byte for byte, and the acceptance test is `sqlite3 yourfile.db "PRAGMA integrity_check"` returning
> `ok`. So where this guide used to say "your format, yours to design," the format is now given and the
> interesting question is *why it's shaped that way*. Feature scope is unchanged — no WAL, no page
> merging, no bytecode VM — because none of those change the bytes. See `roadmap.md` §1.1.


---


## Part 0: The One Analogy That Carries Everything


Almost every concept in this project fits one picture. Learn it once and the rest gets much easier.


> **Your database is a giant three-ring binder of numbered sheets of paper.**
>
> Every sheet is exactly the same size: 4096 bytes. The sheets are numbered 1, 2, 3, … and the whole
> binder is one file on disk.


That's it. That's a database file.


**Why fixed-size sheets?** Because disks don't hand you bytes, they hand you blocks — typically 4KB.
Asking for 100 bytes costs the same as asking for 4096. So you always work in whole sheets, and you
try hard to make each sheet you fetch worth fetching.


**Why numbered?** Because sheet N lives at byte offset `N × 4096`. No searching. You want sheet 47,
you jump straight to byte 192,512. This is the single most important property of the whole design:
*page number → exact disk location, by arithmetic.*


Everything you build over eight weeks is a role in this binder metaphor:


| You build         | In the binder                                                         | Week |
| ----------------- | --------------------------------------------------------------------- | ---- |
| `Pager`           | The assistant who fetches and files sheet N                           | 1    |
| `BufferPool`      | Your desk — a few sheets kept out so you stop walking to the shelf    | 1    |
| Record codec      | The compact shorthand you write a row in                              | 1    |
| B+tree            | Signpost sheets that tell you which sheet holds which rows            | 2    |
| SQL frontend      | Someone who understands spoken requests and writes order slips        | 3    |
| Index             | The back-of-book index — a second sorted list pointing into the first | 4    |
| Journal           | Photocopies of pages you're about to scribble on, so you can undo     | 5    |
| Lock manager      | A sign-out sheet so two people don't scribble the same sheet          | 6    |
| Joins/aggregation | Cross-referencing two binders; tallying results                       | 7    |


Keep coming back to this table when a week feels abstract.


---


## Part 1: Why Not Just Use a File?


Worth answering before you spend 120 hours, because the answer *is* the project.


Say you store users as JSON lines:


```python
with open("users.jsonl", "a") as f:
    f.write(json.dumps({"id": 1, "name": "ada", "age": 36}) + "\n")
```


Five real problems, and each one maps to a week:


1. **Finding one user means reading the whole file.** A million users, you read a million lines to
   find one. → *B+tree, week 2. Turns "read everything" into "read 3 sheets."*
2. **You can't ask questions.** You want "average age of users over 30" — now you're writing Python.
   → *SQL, week 3.*
3. **Sorting by anything but insertion order means reading everything.** → *Indexes, week 4.*
4. **A crash mid-write corrupts it.** Power cut halfway through a line and you have half a JSON
   object. → *Transactions, week 5.*
5. **Two programs writing at once interleave into garbage.** → *Locking, week 6.*


A database is precisely the thing that solves these five problems. You're going to solve them in the
order they're hardest to retrofit.


---


## Part 2: How To Work


**Session length: 2 hours.** ~7 sessions per week — except week 4, which needs **9** once the cost
model is included — so ~58 total. Each session below has a goal small
enough to finish in one sitting, which matters more than it sounds — finishing is what keeps you
going for eight weeks.


**Every session ends with a passing test and a commit.** Not "it seems to work." A test.


**The debugging rule.** If you're stuck for 45 minutes, stop trying to fix it and instead build
something that makes the problem *visible*: a function that prints a page's raw bytes with offsets
labeled, or dumps the tree structure as indented text. In this project, 80% of "I'm stuck" is
actually "I can't see what's happening." Debug tooling pays for itself within the hour.


**Write down every bug in `NOTES.md`.** Symptom → what you thought → what it actually was → the fix.
Four lines. This is where your interview stories come from, and you will not remember them in October.


**Sessions that will overflow, honestly:** week 2 sessions 6–7 (splits), week 5 session 7 (crash
matrix), week 6 session 7 (debugging the stress test). Expect those to take double. That's not
failure, it's where the real work is.


---


# Week 1 — The Binder Itself


**One sentence:** teach Python to read and write numbered 4KB sheets, and to write a row onto a sheet
compactly.


## 1.1 The Pager — your filing assistant


You never touch the file directly after this week. Instead you have an assistant:


```python
page = pager.read_page(47)      # "bring me sheet 47"
pager.write_page(47, page)      # "file sheet 47 back"
```


Under the hood that's `f.seek(46 * 4096); f.read(4096)`. (Sheet 47 is the 47th sheet, so 46 sheets
come before it.)


**Why bother wrapping something so simple?** Because everything above it — the B+tree, in week 2 —
will only ever say "give me sheet 47." It will never know a file exists. That ignorance is what lets
you, in week 5, make the pager secretly photocopy pages before handing them over, and in week 6 make
it coordinate between threads — without changing a single line of B+tree code.


This is the most valuable architectural idea in the project, and it's available to you in session 4
of 56. Real SQLite does exactly this: `btree.c` never calls `read()`.


## 1.2 The slotted page — two lists growing toward each other


Now: how do you fit several variable-length rows onto one 4096-byte sheet?


Naive answer: write them one after another from the top. Problem: rows must be kept in *sorted* order
so you can binary-search them, and inserting a row in the middle would mean physically shifting every
byte after it.


The trick — used by essentially every real database — is to split the sheet into two regions growing
toward each other from opposite ends:


```
byte 0                                                          byte 4095
┌──────────┬────────────────┬─────────────────┬────────────────────────┐
│  header  │ slot array  →  │   free space    │  ←  row data           │
│ 8-12 B   │ 2 bytes each   │                 │     (variable length)  │
└──────────┴────────────────┴─────────────────┴────────────────────────┘
```


- **Row data** is written from the *bottom* upward. Once written, a row never moves.
- **The slot array** is written from the *top* downward. It's just a list of offsets:
  `[3900, 3840, 3790, ...]` meaning "row 1 starts at byte 3900, row 2 at 3840, …"
- The slot array is kept in **sorted key order**. The row data is in whatever order it was written.


So to insert a row in the middle of the sort order, you append its bytes at the bottom (cheap) and
insert its 2-byte offset into the middle of the slot array (also cheap — you're shifting a handful of
2-byte entries, not kilobytes of row data).


**The page is full when the two regions meet.** That's the entire condition, and it's what triggers a
split in week 2.


Analogy: a notebook where you write your main list from the front and your footnotes from the back.
You know you're out of room when the two collide.


## 1.3 Varints — shorthand for numbers


Most numbers in a database are small. Row IDs like 1, 2, 3. String lengths like 4, 11, 20. If you
store every number as a fixed 8 bytes, you waste 7 bytes storing the number 5.


A **varint** (variable-length integer) uses as many bytes as the number needs. The scheme: 7 bits of
actual number per byte, and the top bit means "keep reading."


```
5     →  0000_0101                  (1 byte, top bit 0 = done)
300   →  1000_0010  0010_1100       (2 bytes; first has top bit 1 = more coming)
```


To decode: read a byte, take its low 7 bits, check the top bit; if set, read another byte and shift.


**The tradeoff you're accepting:** you can no longer jump to "the 5th number" — you must read them in
order, because you don't know how long each one is until you read it. That's fine here, because you
read a row's fields front-to-back anyway. Space saved, random access given up. Noticing tradeoffs
like this is most of what database design *is*.


⚠️ **Byte order is decided for you: big-endian**, because you're writing SQLite's format. Little-endian
(protobuf's LEB128) is the other convention, they are incompatible, and mixing them up between your
encoder and your decoder produces numbers that are *almost* right. That's a genuinely miserable
afternoon. Write it into `docs/file-format.md` before you code anyway — the format doc is what you
check when encoder and decoder disagree.


⚠️ **And the maximum length is 9 bytes, not 10.** `ceil(64/7)` says 10, but SQLite's ninth byte drops
its continuation flag and contributes all 8 of its bits: `8×7 + 8 = 64` exactly. The flag is redundant
there, because after eight bytes the format guarantees the varint ends. **Everything below 2⁵⁶ works
with the naive loop above**, so a test suite that only checks small numbers passes while your encoder
is wrong for large rowids and long payloads. Test the 2⁵⁶ boundary explicitly. Theory §3.4 derives it.


**There is no zigzag encoding.** Negative values are 64-bit two's complement, so a negative varint
costs the full 9 bytes — which SQLite accepts, because the only bare varints are rowids and lengths.
Record integers avoid the cost differently: the serial type names the width (1/2/3/4/6/8 bytes) and the
value is stored two's complement at that width, so `-5` costs one body byte. Theory §3.5 explains why
that beats zigzag *here* and why protobuf's opposite answer is also right.


## 1.4 Records — a manifest, then the crate


A row is `(1, "ada", 36)`. How do you write that as bytes so you can read it back?


You need to know, when reading, where each value ends. So a record is written in two parts:


```
[ header: what types are coming ]  [ body: the raw values ]
    varint  varint  varint            varint  bytes   varint
      1       17      1                  1     "ada"    36
    (int8)  (text,2ch) (int8)
```


The header is a list of **serial type codes** — small numbers meaning "an 8-bit int comes next," "a
6-byte string comes next," "a NULL comes next (occupying zero bytes in the body)." SQLite's actual
encoding: `0` = NULL, `1`–`6` = integers of 1–6 bytes, `7` = float, and for variable-length data,
`(n*2)+13` = a text value of n bytes, `(n*2)+12` = a blob of n bytes. Odd means text, even means
blob — a nice trick.


Analogy: a shipping manifest taped to the outside of a crate. You read the manifest to know what's
inside and how big each item is, without unpacking. That's why you can extract column 3 without
decoding columns 1 and 2 — read the header, sum the lengths, jump.


**Why NULL costs zero bytes** is worth noticing: the type code itself says "NULL," so the body
contains nothing for that column. A table with many NULLs is genuinely cheaper to store.


## 1.5 The buffer pool — your desk


The binder is on a shelf across the room. Your desk is right here. Going to the shelf takes roughly
10,000 times longer than glancing at your desk (RAM vs disk, and that ratio is real).


So: keep the sheets you're using on your desk. That's the buffer pool — a bounded dict from page
number to page contents.


Three rules make it correct rather than just fast:


1. **Dirty tracking.** If you scribbled on a sheet, you *must* file it back before removing it from
   your desk. A page you modified but haven't written is "dirty." Losing one silently loses data.
2. **Pin counts.** If someone is actively reading a sheet, you can't file it away — their reference
   would point at nothing. So each page has a count of active users; you can only evict when it's
   zero. In week 2, a cursor walking the tree holds pins.
3. **Eviction policy.** The desk is finite. When it's full and you need a new sheet, which one goes
   back? **LRU** — least recently used — on the guess that the sheet you haven't touched longest is
   least likely to be needed next.


**A depth point worth knowing, because it comes up in interviews:** LRU is actively *wrong* for full
table scans. A scan touches every page exactly once, in order. So LRU dutifully evicts all your
genuinely useful pages to make room for pages it will never look at again — the scan poisons the
cache. Real systems detect sequential scans and route them through a small separate ring buffer.
You don't have to implement that. Knowing it is the point.


## 1.6 Your seven sessions


| #   | Do this                                                                                                                              | Done when                                                                                                                                                       |
| --- | ------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | `pyproject.toml`, `src/quilldb/`, pytest + ruff + mypy config, GitHub Actions CI, `errors.py` with the exception hierarchy           | CI badge is green on a project that does nothing. Psychologically worth more than it sounds.                                                                    |
| 2   | `codec/ints.py` (big-endian u16/u32 helpers) and `codec/varint.py`                                                                   | Hypothesis test: `decode(encode(n)) == n` for all `n` in `0 .. 2**64-1`. Test every length boundary explicitly.                                                 |
| 3   | `storage/header.py` — define all 100 bytes, plus `create_database(path)` and `read_header(path)`                                     | You create a file and read back its page size, page count, and freelist head                                                                                    |
| 4   | `storage/pager.py` — `read_page`, `write_page`, bounds checks                                                                        | Write page 5, read page 5, get identical bytes. Reading page 999 of a 3-page file raises `PageOutOfRange`.                                                      |
| 5   | Freelist: `allocate_page`, `free_page` (a linked list — each free page stores the number of the next free page in its first 4 bytes) | Allocate 3, free the middle one, allocate again → you get the freed page back, and the file didn't grow                                                         |
| 6   | `storage/page.py` — `SlottedPage` with `insert_cell(i, bytes)`, `get_cell(i)`, `delete_cell(i)`, `free_space()`                      | Fill a page to within 10 bytes, verify the 11th insert is refused cleanly                                                                                       |
| 7   | `storage/bufferpool.py` + `codec/record.py` + `cli.py inspect`                                                                       | `quilldb inspect demo.db` prints header fields. Capacity-8 pool over a 200-page workload evicts correctly, never loses a dirty page, never evicts a pinned one. |


**Success state for the week:** you can write `(1, "ada", 36)` into page 4, close the file, reopen it,
and read `(1, "ada", 36)` back. Everything else in the project is built on that sentence being
reliably true.


---


# Week 2 — Signposts (The B+Tree)


**One sentence:** organize sheets so that finding any row takes 3 sheet-reads instead of a million.


## 2.1 Start with the physical phone book


You want "Nguyen" in a 1000-page phone book. You do not read page 1, then page 2. You open near the
middle, see "M," go right, open again, see "P," go back a bit. Six or seven opens and you're there.


That works for exactly one reason: **it's sorted.** Sorting is what buys you the ability to skip.


A B+tree is that idea, made persistent and — crucially — *updatable*. A sorted array in a file would
give you fast lookup, but inserting one row in the middle would mean rewriting the entire file. The
B+tree's whole purpose is to keep things sorted while making insertion cheap.


## 2.2 The structure: two kinds of sheet


```
                    ┌─────────────────────────────────┐
     interior       │  <100 → p5 │ <500 → p6 │ → p7   │   page 2 (the root)
     (signposts)    └─────────────────────────────────┘
                        ↓            ↓          ↓
        ┌───────────┐ ┌───────────┐ ┌───────────┐
 leaves │ 1:ada     │ │ 100:bo    │ │ 500:cy    │
 (data) │ 7:ben     │ │ 220:kit   │ │ 890:dee   │
        │ 42:cal    │ │ 431:sam   │ │ 991:eve   │
        └───────────┘ └───────────┘ └───────────┘
            page 5        page 6        page 7
```


- **Leaf pages** hold the actual rows, sorted by key (the row ID).
- **Interior pages** hold *no rows at all* — only signposts: "keys below 100 are on sheet 5; keys
  below 500 are on sheet 6; everything else is on sheet 7."


To find key 431: start at the root, see 431 is ≥100 and <500 → go to page 6 → binary search within
page 6 → found. Two sheet reads.


**Why this is fast, concretely.** An interior page is 4096 bytes and a signpost is about 8 bytes, so
one interior page holds ~500 signposts. Three levels of interior pages: 500 × 500 × 500 = 125 million
leaves. **You can reach any row in a 125-million-row table in four sheet reads.** That's the entire
value proposition of a database, and it's this data structure.


**Why not a hash table?** Hash tables give you single-key lookup and nothing else. You couldn't do
`WHERE age BETWEEN 20 AND 30`, or `ORDER BY`, or "give me the next row" — all of which need *order*.
A B+tree gives you fast lookup *and* ordered iteration from the same structure. That's why it won.


**The "+" in B+tree** means data lives only in leaves. Interior pages are pure navigation. This makes
scanning easy: walk the leaves left to right and you've read the whole table in sorted order.


## 2.3 Splitting — the part that will fight you


A page holds keys 1–50 and is completely full. You insert key 25. There's no room. So:


1. Get a fresh sheet from the pager.
2. Move roughly half the rows — say keys 26–50 — onto the new sheet.
3. Insert key 25 into the old sheet, which now has room.
4. **Tell the parent:** "there's a new sheet now; keys ≥ 26 live on sheet 88."


Step 4 is called **promoting a separator**, and it's the step people get wrong. The new signpost has
to go into the parent, in the right sorted position.


**Now the nasty part.** What if the parent is *also* full? Then the parent splits too, and *its*
parent gets a new signpost. This can cascade all the way up. And when the root itself splits, you
create a brand-new root above it — **the tree grows one level taller.**


That's worth sitting with, because it's counterintuitive: **a B+tree grows from the root upward, not
from the leaves downward.** Every leaf is always at the same depth, and the only way depth increases
is a root split. This is why B+trees stay balanced without any explicit rebalancing pass.


## 2.4 Why you write the validator first


A split writes three sheets: the old leaf, the new leaf, the parent. Suppose you get the separator
key off by one — you write `keys ≥ 27 → sheet 88` when it should be `≥ 26`.


Nothing crashes. Every page is structurally valid. But key 26 is now unreachable: lookups follow the
signpost to the old sheet, which no longer has it. You will discover this 200 inserts later, as a
scan that silently returns 9,997 of 10,000 rows.


So write a function that walks the entire tree and asserts the things that must be true:


```python
def validate_btree(pager, root_page_id) -> ValidationReport:
    # - every key on page P is inside the range its parent's signposts claim
    # - keys within each page are strictly increasing
    # - every page is reachable exactly once (no page owned by two parents, none orphaned)
    # - all leaves are at the same depth
    # - every cell offset lies inside its page
    # - no overflow chain contains a cycle
```


Run it after every mutation in tests. It converts "silently wrong 200 inserts later" into "raised
immediately, on the operation that caused it." This single function will save you more hours than
anything else you write, and it's also a genuine interview asset — it says you think in invariants.


## 2.5 Overflow pages — rows too big for a sheet


What if a row is 10KB and a sheet is 4KB? You store as much as fits on the leaf, then a page number
pointing to a continuation sheet, which holds more data plus a pointer to the next, and so on — a
linked list. Like a letter continued on additional pages, each saying "continued on sheet 91."


The one hazard: if sheet 91 points back to sheet 88, a reader loops forever. So the validator checks
for cycles, and the reader caps its hops.


## 2.6 Your seven (realistically eight) sessions


| #   | Do this                                                                                                                                                                                                                                                         | Done when                                                                                                                                                |
| --- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | The four cell formats. Table leaf = `[payload len varint][rowid varint][payload]`, table interior = `[left child u32][rowid varint]`, index leaf = `[payload len][payload]`, index interior = `[left child u32][payload len][payload]`. Encode/decode all four. | Round-trip tests pass. **Watch the order in the table leaf cell: length before rowid.**                                                                  |
| 2   | Parse a B+tree page header; binary search *within* one page for a key                                                                                                                                                                                           | Given a hand-built page of 20 keys, find each one, and correctly report misses                                                                           |
| 3   | `search(key)` — descend root → leaf. **Hand-build a 2-level tree in a test fixture** rather than waiting for insert to work.                                                                                                                                    | Lookups succeed on a manually constructed 3-leaf tree                                                                                                    |
| 4   | `TableCursor` with a path stack: `first()`, `next()`, crossing from one leaf to the next                                                                                                                                                                        | Full scan of the hand-built tree yields all keys in order                                                                                                |
| 5   | `insert()` for the easy case only — the target leaf has room                                                                                                                                                                                                    | Insert into empty page, at start, middle, end. Keys stay sorted.                                                                                         |
| 6   | **Leaf split + separator promotion.** Expect this to take the full session and then some.                                                                                                                                                                       | A page that overflows by one cell splits into two, the parent gains a signpost, and lookups still find every key                                         |
| 7   | **Recursive split + root split.** Parent full → parent splits → cascade → new root.                                                                                                                                                                             | Insert 1000 sequential keys; tree height grows; every key still findable                                                                                 |
| 8   | `validate_btree()`, then the stress test                                                                                                                                                                                                                        | 100k random-order inserts, validator clean. Scan == `sorted(set(keys))`. Overflow: a 10KB value round-trips. Reopening the file mid-run changes nothing. |


**When you get stuck on session 6 or 7** — and you will — the fix is almost always a visualizer:


```python
def dump_tree(pager, root, depth=0):
    """Print the tree as indented text. Write this before you need it."""
```


Then read [db_tutorial parts 10, 13, 14](https://cstack.github.io/db_tutorial/) — splitting a leaf,
updating the parent, splitting internal nodes. It's C, but it's the same algorithm step by step, and
it's the best walkthrough of exactly this problem that exists.


---


# Week 3 — Teaching It to Understand English ⭐


**One sentence:** turn the string `"SELECT name FROM users WHERE age > 30"` into actual B+tree cursor
calls — so that for the first time, `import quilldb` does something.


## 3.1 The restaurant


Four stages, and the restaurant analogy maps cleanly onto all of them:


```
"SELECT name FROM users WHERE age > 30"
        │
        ▼  TOKENIZER — hear the words
   [SELECT] [name] [FROM] [users] [WHERE] [age] [>] [30]
        │
        ▼  PARSER — understand the grammar, write an order slip
   Select(columns=[name], from=users, where=GreaterThan(age, 30))
        │
        ▼  BINDER — check it's possible
   "table users exists; 'name' is column 1; 'age' is column 2, an INTEGER"
        │
        ▼  PLANNER — decide how to make it
   Project(cols=[1]) ← Filter(col2 > 30) ← SeqScan(root_page=4)
        │
        ▼  EXECUTOR — the assembly line does the work
   [("ada",)]
```


- **Tokenizer**: hearing spoken words and splitting them. Purely mechanical — no understanding.
- **Parser**: understanding grammar. Output is a *tree*, not a list, because SQL nests:
  `WHERE (age > 30 AND name = 'ada') OR id = 1` has real structure.
- **Binder**: checking the order is possible. Is there a `users` table? Does it have `name`? Which
  column position is it? This turns names into integers, which is what the executor wants.
- **Planner**: the kitchen deciding on an approach. Trivial this week (always scan); it gets
  interesting in week 4.
- **Executor**: the assembly line.


## 3.2 The assembly line (this is the important one)


Your executor is a chain of workers, each knowing one job, each with an upstream worker to ask.


```
Project(name)              "give me only the name column"
  └─ Filter(age > 30)      "only pass rows where age > 30"
       └─ SeqScan(users)   "read rows off the sheets, one at a time"
```


Every worker has one method: `next()`, returning one row or `None` when exhausted.


You ask `Project.next()`. Project asks `Filter.next()`. Filter asks `SeqScan.next()` repeatedly,
throwing away rows until one passes `age > 30`, then hands it up. Project strips it to one column.
You get `("ada",)`.


```python
class Filter:
    def __init__(self, child, predicate):
        self.child, self.predicate = child, predicate


    def next(self):
        while (row := self.child.next()) is not None:
            if self.predicate(row):
                return row
        return None
```


That's a complete, real query operator. They're all about that size.


**Two properties make this design worth using.** It *composes*: a new operation is a new class with a
`next()`, and it plugs in anywhere. And it's *memory-bounded*: rows stream through one at a time, so
you can query a 10-million-row table with a handful of rows in memory. Nothing materializes.


This is called the **Volcano** or **iterator** model, and it's how Postgres works. (SQLite instead
compiles queries to bytecode for a little virtual machine — a valid alternative, more code, and one
you deliberately skipped in the roadmap.)


## 3.3 The catalog — a table that describes tables


When you reopen the file, how does `quilldb` know that `users` exists, lives at page 4, and has three
columns of particular types?


You store that information. Where? In a table. A table whose rows describe other tables.


The chicken-and-egg problem — how do you find the table that tells you where tables are? — is solved
by fiat: **the catalog always lives at page 1.** Hardcoded, and not even recorded in the file header,
because a constant can't be corrupt. Everything else is discoverable from there.


It's called `sqlite_schema`, and it has exactly five columns: `type`, `name`, `tbl_name`, `rootpage`,
`sql`. That last one stores the literal `CREATE TABLE users (...)` **text**, which gets re-parsed
every time you open the file. That sounds wasteful, and it is — but it means the text is the source of
truth, so a 2024 SQLite can correctly interpret a schema written in 2004. Structured metadata would be
simpler and faster to open, and would freeze your interpretation at write time.


You store the text, for two reasons: it's the more future-proof design, and `sqlite3` reads that
column to learn your schema — so storing anything else would make your files unreadable by the tool
your acceptance test depends on. Being able to explain why the awkward option is the right one is a
genuinely good interview moment.


> ⚠️ Page 1 is doing three jobs at once: the first 100 bytes are the **file header**, byte 100 begins
> page 1's own **b-tree page header**, and the b-tree rooted there is the **catalog**. That's why page
> 1 has 100 fewer usable bytes than every other page. `docs/theory/storage/01-pages-and-the-pager.md` §1.6
> covers why SQLite accepted that awkwardness, and what it costs.


## 3.4 Why this week is the most important one


Right now you have a data structure. Nobody can look at a B+tree and be impressed, and *you* can't
tell whether your layering is right.


After this week you have a database you can hand someone. And every remaining week improves a working
system instead of inching toward a first runnable state. If something goes wrong in week 6, you still
have a real project.


Protect this week. If week 2 runs long, cut validator polish — not this.


## 3.5 Your seven sessions


| #   | Do this                                                                                                   | Done when                                                                                                                                                                                              |
| --- | --------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 1   | `sql/tokenizer.py` — keywords, identifiers, string and number literals, operators, `?`, comments          | `tokenize("SELECT * FROM t WHERE a>=1")` gives the right token list; unterminated string raises cleanly                                                                                                |
| 2   | `sql/ast.py` dataclasses, and parse `CREATE TABLE` only                                                   | `CREATE TABLE users (id INTEGER, name TEXT)` → correct AST                                                                                                                                             |
| 3   | Parse `INSERT` and `SELECT` with no `WHERE`                                                               | Both produce correct ASTs; `SELECT *` and explicit column lists both work                                                                                                                              |
| 4   | Expression parsing via Pratt/precedence climbing — comparisons, `AND`/`OR`/`NOT`, arithmetic, `IS NULL`   | `a > 1 AND b = 2 OR c = 3` parses with correct precedence. Read [matklad's post](https://matklad.github.io/2020/04/13/simple-but-powerful-pratt-parsing.html) first — 40 minutes, saves you two hours. |
| 5   | `catalog/` — store table metadata as rows in the page-1 `sqlite_schema` B+tree; create and look up tables | `CREATE TABLE`, close the file, reopen, and the table is still known                                                                                                                                   |
| 6   | Binder (names → column indexes, type checks) + `exec/expressions.py` evaluator                            | Unknown column raises a typed error *before* execution starts                                                                                                                                          |
| 7   | `SeqScan`, `Filter`, `Project`, `Insert` operators + `api/connection.py`                                  | **The four-line snippet below runs.**                                                                                                                                                                  |


```python
db = quilldb.connect("demo.db")
db.execute("CREATE TABLE users (id INTEGER, name TEXT, age INTEGER)")
db.execute("INSERT INTO users VALUES (?, ?, ?)", (1, "ada", 36))
assert db.execute("SELECT name FROM users WHERE age > 30").fetchall() == [("ada",)]
```


Also this week: write the differential test harness. ~40 lines that run a SQL script against both
`sqlite3` and `quilldb` and diff the results. From now on every feature gets tested against a real
database for free — this is the highest-leverage 40 lines in your test suite.


---


# Week 4 — The Back-of-Book Index


**One sentence:** make `WHERE email = ?` stop reading the whole table, and be able to *show* that it
did.


## 4.1 The analogy is literally an index


A textbook's pages are in page order. But to find "photosynthesis" you don't read 400 pages — you
flip to the index at the back: *photosynthesis … 231*. Then you turn to page 231.


That's exactly a database index, including the part people miss:


**An index lookup is two lookups.** The index gets you a page number (a row ID). Then you do a second
search — in the table's B+tree — to fetch the actual row.


```
Index B+tree, sorted by email          Table B+tree, sorted by rowid
┌────────────────────────┐             ┌──────────────────────┐
│ "a@b.c"  → rowid 42    │──────┐      │ 42: (42,"a@b.c",36)  │
│ "d@e.f"  → rowid 7     │      └─────▶│  7: (7,"d@e.f",22)   │
└────────────────────────┘             └──────────────────────┘
     search #1                              search #2
```


Which immediately explains **covering indexes**: if your query only needs `email`, and `email` is
already in the index, you can skip search #2 entirely. Roughly twice as fast, for free. That's a
real optimization you can implement and demo.


An index is *just another B+tree* — the same code from week 2. The only differences are the key
(the indexed column values plus the rowid, rather than the rowid alone) and the comparison function
(lexicographic over a tuple, rather than integer).


**The cost, which matters:** every `INSERT`, `UPDATE`, and `DELETE` must now update every index too.
Indexes make reads fast and writes slower. Negotiating that tradeoff is what backend engineers
actually do with real databases, which is why this week is the most *transferable* of the eight.


## 4.2 The leading-column rule — the single most useful thing here


An index on `(last_name, first_name)` is a list sorted by last name, then first name. Like a phone
book.


- `WHERE last_name = 'Nguyen'` → **works.** All the Nguyens are adjacent.
- `WHERE last_name = 'Nguyen' AND first_name = 'An'` → **works.** Even more precise.
- `WHERE first_name = 'An'` → **useless.** The Ans are scattered throughout. You cannot use a phone
  book to find everyone named An.


So: the *leading* columns must be constrained by equality, and you can't skip one. The rightmost
column you use may take an inequality (`>`, `<`, `BETWEEN`) because that's still a contiguous range.


Your planner is a function implementing exactly this rule. SQLite documents it in
[optoverview §2](https://www.sqlite.org/optoverview.html) with a worked table of which `WHERE` clauses
can use an index on `(a,b,c,d,…)` — read that table and copy its logic.


This is also the thing that makes you useful on day one of a backend job. Most production database
performance problems are "there's no index on that column" or "there is one but the query can't use
it."


But notice what this rule *cannot* do, because it's the whole subject of the next section: it tells you
which indexes are **allowed**. It does not tell you which one is **best**, and it never tells you
whether using an index at all beats just reading the table.


## 4.3 Two indexes, both legal — now what?


Here's the situation the rule can't touch. Table of 10,000 people. Two indexes:


```
idx_active  on active   -- values: 0 or 1.          ~5,000 rows per value
idx_email   on email    -- values: all different.   ~1 row per value
```


Now: `WHERE active = 1 AND email = 'a@b.c'`.


The leading-column rule says **both indexes are usable**. Both are a bare column with an equality on
it. Identical shape. And yet:


- via `idx_email`: find 1 entry, do 1 table lookup. **~4 page reads.**
- via `idx_active`: find 5,000 entries, do **5,000** table lookups. Far worse than just reading the
  whole table.


Same rule, same shapes, ~1000× apart in real work. **No rule can tell these apart, because the
difference isn't in the query — it's in the data.** That's the entire argument for what comes next.


### The two philosophies, and why they aren't rivals


**A rule-based planner** decides from the *shape* of the query. "There's an equality on an indexed
leading column, so use that index." Cheap to build, and — this is the part people undersell —
completely predictable. You can read a query and know what it will do.


**A cost-based planner** decides from the shape *plus measurements of the data*. It generates every
legal plan, estimates how many rows each will touch, converts that to an estimated number of page
reads, and picks the smallest.


The trap is thinking you must choose one. You don't, and the reason is worth memorising:


> **Rules decide which plans are *legal*. Costs decide which legal plan *wins*.**


They're two stages of one pipeline, not two schools of thought. A cost model cannot rescue an illegal
seek — no number makes `WHERE lower(email)=?` usable against an index on `email`. And no rule can
separate `idx_active` from `idx_email` above. You need both, and quilldb builds both:


```text
your WHERE clause
    │
    ├─ stage 1: legality rules  ──────▶ candidate plans   ← §4.2, the rule-based part
    │                                   (SeqScan, IndexScan(idx_active), IndexScan(idx_email))
    ├─ stage 2: estimate rows   ──────▶ "this one touches ~5,000, that one ~1"
    ├─ stage 3: cost the rows   ──────▶ "≈5,003 page reads vs ≈4"
    └─ stage 4: pick the cheapest ────▶ IndexScan(idx_email)
```


Stage 1 alone is a rule-based planner. Stages 1–4 are a cost-based one. Note that stage 1 doesn't get
deleted when you add the others — that's why the phone-book rule in §4.2 is still the most useful thing
in this week.


### Where the numbers come from: `ANALYZE`


Stage 2 needs to know that `active` has 2 distinct values and `email` has 10,000. The database doesn't
know that unless someone counts. That's the `ANALYZE` command: it walks each index, counts, and stores
a tiny summary in a table called `quill_stat1`.


The summary is startlingly small — a string of integers per index:


```text
tbl  idx         stat
t    idx_active  "10000 5000"      -- 10,000 rows; ~5,000 rows per distinct value
t    idx_email   "10000 1"         -- 10,000 rows; ~1 row per distinct value
```


For a three-column index on `(a,b,c)` you get four numbers — `"10000 100 10 2"` — meaning 10,000 rows,
~100 rows share any given `a`, ~10 share any `(a,b)`, ~2 share any `(a,b,c)`. That's it. No histograms,
no per-value counts. Four integers, and they're enough to tell "1 row per value" from "5,000 rows per
value", which is the distinction that actually decides the plan.


This is copied deliberately: SQLite's own `sqlite_stat1` uses exactly this encoding.


### Two consequences you should be able to state


**1. Statistics are estimates, and averages hide skew.** If 90% of your rows have `status='open'`, the
*average* rows-per-status looks moderate and the planner will happily choose that index for
`status='open'` — and touch 90% of the table. This is a real limitation, it's the one SQLite has too,
and "averages can't see skew or correlation" is the right one-sentence answer for it.


**2. Statistics go stale, and that must never change your answers.** You insert a million rows; the
stats still describe the old table. The plan may now be slow. **It must still be correct.** That's the
hard line: bad statistics are allowed to pick a slow legal plan, never a different result. Your week-4
property test is the guard — run every query with and without each index and assert identical output.


And the operational cost of all this, which is the honest counterweight to "cost-based is better":
statistics are *derived state*. One more thing that can be stale, one more thing to invalidate, one more
reason a query got slow overnight when nobody deployed anything. A rule-based planner has none of those
failure modes. It's just reliably mediocre instead.


### Why `SeqScan` must always stay on the list


The non-obvious one. Even when a perfectly legal index exists, *just reading the whole table* is
sometimes cheapest — because a non-covering index pays a separate table lookup **per matching row**, and
those are scattered random reads. Reading 2,417 pages in order can beat 5,000 random lookups.


So the planner always includes "scan the whole thing" as a candidate and lets it compete. An
optimizer that never considers the dumbest plan will lose to it.


## 4.4 EXPLAIN — the kitchen showing you the plan


`EXPLAIN` prints the operator tree instead of executing it:


```
>>> print(db.execute("EXPLAIN SELECT * FROM users WHERE email='a@b.c'").explain())
Project  [id, email, age]
└─ IndexScan  idx_email  (email = 'a@b.c')   est_rows=1  startup=3.00  cost=7.01
```


Note what's printed: **estimates only**. Plain `EXPLAIN` does not run the query, so it cannot know how
many pages were really read — `est_rows` and `cost` are the planner's predictions. To get real numbers
you run `EXPLAIN ANALYZE`, which executes the plan once and adds measured counters:


```
>>> print(db.execute("EXPLAIN ANALYZE SELECT * FROM users WHERE email='a@b.c'").explain())
Project  [id, email, age]
└─ IndexScan  idx_email  (email = 'a@b.c')   est_rows=1  cost=7.01  actual_rows=1  pages_read=4
```


Keeping those two commands separate is a small discipline with a real payoff: it makes it impossible to
accidentally present an estimate as a measurement. And putting them side by side is the best demo in the
project — "the planner predicted 1 row, it read 1 row, here's the page count" is a claim nobody can wave
away.


It costs about two hours because you already have the tree — you're just printing it with
indentation. Nothing else in the project makes it look this much like a real database in a demo.


## 4.5 About deletion


Removing a row means removing a cell from a page and repacking it. Simple.


What real databases *also* do is notice when a page is nearly empty and merge it with a sibling, to
keep the tree compact. You're deliberately skipping that (see the roadmap's cut list) — it's ~10
hours for behavior you can't demo.


The honest consequence: after many deletes, pages sit half-empty. It's not a *leak* — you return
fully-empty pages to the freelist so the space is reused — but the tree is less compact than it could
be. Write that down in the README's limitations section. A specific, understood limitation reads as
competence; interviewers find "I know exactly what I didn't do and why" far more convincing than an
implied claim of completeness.


## 4.6 Your nine sessions


**This week needs nine, not seven** — the cost model is two extra sessions and pretending otherwise just
means running over. It's the heaviest week in the project. If you have to borrow time, borrow it from
week 8's presentation polish, not from here.


| #   | Do this                                                                                       | Done when                                                                                                                  |
| --- | --------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| 1   | Parse `DELETE`; `Delete` operator; B+tree cell removal + repack                               | Delete by rowid works, remaining rows intact, validator clean                                                              |
| 2   | Return emptied pages to the freelist; parse and implement `UPDATE`                            | Delete 1000 rows then insert 1000 — file size doesn't grow                                                                 |
| 3   | Index key encoding (composite: column values + rowid) and comparison                          | Sorting encoded keys as bytes matches sorting the tuples logically. Test with mixed types and NULLs.                       |
| 4   | `CREATE INDEX` — build the tree by scanning existing rows; `UNIQUE` enforcement               | Index built on a populated table; duplicate insert into a unique index raises *before* writing anything                    |
| 5   | **Index maintenance on insert/update/delete.** The correctness-critical session.              | Property test: full index scan and full table scan + filter return identical row sets, after any sequence of mutations     |
| 6   | `IndexScan` operator + **stage 1 only**: candidate generation and the legality rule from §4.2 | The 12-case legality table is green — including that the four *ineligible* cases produce **no** IndexScan candidate at all |
| 7   | `ANALYZE`, `quill_stat1`, and row estimation (**stages 2**)                                   | A known distribution produces the expected prefix numbers; missing statistics fall back to defaults instead of failing     |
| 8   | The cost model and access-path choice (**stages 3–4**)                                        | The selective index wins; editing *only* the statistics flips the choice; a low-selectivity index loses to SeqScan         |
| 9   | `EXPLAIN` + `EXPLAIN ANALYZE` + the benchmark                                                 | **Indexed lookup ~4 page reads vs ~2,417 for the scan. This number goes in your README.**                                  |


**Do not merge sessions 6 and 8.** It's the tempting cut when you're behind, and it quietly destroys the
thing you're building: if candidate generation and costing live in one function, the implementation may
still pick good plans but you can no longer *test* that choice was cost-based rather than rule-based.
The two tests that prove this project has a real optimizer — statistics flip the plan, and a legal index
loses to a scan — both need the stages separable. Cut presentation polish instead.


One property test worth writing this week, because it catches a whole bug class: run every query in
your test suite both with and without the index, and assert identical results. An index must change
*speed*, never *answers*. This is also your guard against the entire class of planner bugs — a planner
is an *optimization*, and an optimization that changes answers is just a bug with a nicer name.


---


# Week 5 — Photocopies (Crash Safety) ⭐⭐


**One sentence:** make it impossible for a power cut to leave your database half-modified.


This is the most impressive week in the project. It's also the one with the best available reading,
so lean on it.


## 5.1 Why a half-written database is worse than you think


Text file, crash mid-write: you lose the end of the file. Annoying, recoverable.


**Your B+tree, crash mid-split: you lose the *structure*.**


Remember a split writes three sheets — old leaf, new leaf, parent signpost. Suppose the first two land
and the power cuts before the parent. Now there's a sheet full of rows that *nothing points to*. Those
rows are gone. Worse, depending on the order, you can get a parent pointing at a sheet that was never
written — so opening the file reads garbage as a page header.


And week 4 made it worse: one `INSERT` now writes a table page *plus* every index page. Crash between
them and your index disagrees with your table — it claims a row exists that doesn't, or misses one
that does. There is no way to repair that after the fact, because nothing recorded what was intended.


So atomicity isn't a feature you add on top of the B+tree. **It's what makes the B+tree safe to use
at all.** That's the framing to use when someone asks why you built it.


## 5.2 The photocopy protocol


You're about to edit 5 pages of an important binder. If you're interrupted, you want *all five edits*
or *none* — never three.


Here's the protocol, and every step is load-bearing:


```
1. Photocopy the 5 original pages into a separate folder     ← "write the journal"
2. Make sure the photocopies are physically IN the folder    ← fsync the journal   ⚠️ FENCE
3. Now edit the real pages
4. Make sure the edits are physically on the real pages      ← fsync the database
5. THROW AWAY the photocopy folder                           ← ✅ THE COMMIT POINT
```


**Why is throwing away the folder the commit point?**


Because the folder's *existence* is the flag meaning "an edit was in progress." Every time you open
the binder, you check: is there a photocopy folder?


- **Folder exists** → you were interrupted → restore every original page from its photocopy. The
  transaction never happened.
- **No folder** → all done. Proceed normally.


And here's the elegant part: *"does this file exist?"* is a question that cannot be half-answered.
File deletion is atomic from your process's point of view. That is essentially the only atomic
primitive the filesystem gives you for free — and the entire design is arranged to lean on that one
primitive. Once you see that, the whole scheme clicks.


**Why must step 2 come before step 3?** Because if you edit the real pages first and crash before the
photocopies are safely stored, you've destroyed the originals with no way back. The fsync is a fence:
nothing after it may start until everything before it is durable.


## 5.3 Why `write()` isn't enough


`f.write(data)` does *not* put data on the disk. It hands bytes to the operating system, which parks
them in memory and writes them out whenever convenient — **possibly in a different order than you
wrote them.**


`os.fsync(fd)` means "actually put it on the physical disk, and don't return until it's there."


Without fsync, your careful ordering is a polite suggestion the OS ignores. This is the single most
common way real applications get crash-safety wrong.


Two things worth knowing beyond that (both make excellent interview material):


- On macOS, `fsync` doesn't fully flush the drive's own write cache; you need
  `fcntl(F_FULLFSYNC)`. And some consumer drives ignore flush commands outright, to score better on
  benchmarks. So "durable" has a hardware floor you cannot reach from Python.
- Creating a file isn't durable until you fsync its *parent directory* too. Otherwise you can crash
  and find the journal you carefully wrote doesn't exist.


Read [Dan Luu's "Files are hard"](https://danluu.com/file-consistency/) — 30 minutes, and it's why
you'll take this seriously. Note his warning about the obvious shortcut: "just write a temp file and
rename it" is atomic in normal operation but **not across crashes.** You'll be tempted; don't be.


## 5.4 The crash matrix — proving it instead of hoping


Anyone can write a journal. What almost nobody does is *prove* it works.


Wrap the file object so that the Nth write raises an exception:


```python
class FaultyFile:
    def __init__(self, real, fail_at_write):
        self.real, self.n, self.fail_at = real, 0, fail_at_write


    def write(self, data):
        self.n += 1
        if self.n == self.fail_at:
            raise SimulatedCrash(f"crash at write #{self.n}")
        return self.real.write(data)
```


Then loop over every possible crash point:


```python
@pytest.mark.parametrize("crash_at", range(1, 200))
def test_atomic_at_every_crash_point(tmp_path, crash_at):
    setup_known_state(tmp_path)
    with pytest.raises(SimulatedCrash):
        run_transaction(tmp_path, fail_at_write=crash_at)


    db = quilldb.connect(tmp_path / "db")        # opening triggers recovery
    assert read_state(db) in (STATE_BEFORE, STATE_AFTER)   # never in between
    assert validate_all_btrees(db).is_valid
```


That's ~200 generated tests from about 30 lines, and it proves atomicity holds at **every physical
write boundary that exists.** Very few portfolio projects have anything like it, and it's the kind of
thing an interviewer will happily spend ten minutes digging into — which is exactly what you want.


One extra case worth adding, because SQLite tests it and it's genuinely nasty: **crash during
recovery from a previous crash.** Recovery must itself be crash-safe.


## 5.5 Your seven sessions


Read [Atomic Commit In SQLite](https://www.sqlite.org/atomiccommit.html) before session 1 — it is
essentially the spec for this week, and §3 lists the exact ordering.


| #   | Do this                                                                                                          | Done when                                                                                                        |
| --- | ---------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| 1   | `Transaction` object: tracks dirty pages, all `get_page` routes through it. **No journal yet** — just the shape. | All existing tests pass with mutations flowing through a transaction                                             |
| 2   | Journal file format: header (original page count) + records (page number, original bytes, 32-bit checksum)       | You can write and read back a journal file                                                                       |
| 3   | Capture-before-modify, plus the fsync ordering from §5.2                                                         | `strace`/logging confirms the order: journal write → fsync → db write → fsync → unlink                           |
| 4   | `commit()` and `rollback()`, `BEGIN`/`COMMIT`/`ROLLBACK`, `with db.transaction():`                               | Hash the file, do work, rollback, hash again — byte-identical                                                    |
| 5   | Hot-journal detection and replay on open                                                                         | Kill the process mid-transaction (for real, `os._exit`), reopen, and the DB is back to its pre-transaction state |
| 6   | `FaultyFile` + your first handful of crash tests                                                                 | Crash at write 3 and write 17 both recover correctly                                                             |
| 7   | Full parametrized crash matrix + `docs/durability.md`                                                            | ~200 crash points green, including one crash-during-recovery case                                                |


Two details worth copying from SQLite exactly: the journal's page count starts at **zero** and is
only filled in after the body is synced (so a torn journal rolls back nothing, harmlessly); and each
journaled page carries a checksum, so a corrupted journal is *refused* rather than replayed into your
database.


In `docs/durability.md`, state your commit point and — just as importantly — your
**non-guarantees**: lying `fsync`, torn sectors, whatever you decided about directory fsync. Naming
your limits precisely is what separates an engineer from someone repeating the word ACID.


---


# Week 6 — The Sign-Out Sheet (Threads) ⭐


**One sentence:** let 8 threads use the database at once without corrupting it or hanging.


## 6.1 Shared and exclusive — the whiteboard


Picture a shared whiteboard.


- **Many people can read it simultaneously.** Reading disturbs nobody.
- **Only one person may write at a time**, and while they're writing, nobody may read — you'd catch a
  half-erased sentence and misread it.


That's the whole locking model:


- **Shared lock (S)** — "I'm reading." Many can hold it at once.
- **Exclusive lock (X)** — "I'm writing." Only one, and it excludes all readers.


You'll lock at **table** granularity: one lock per table. Coarse, simple, correct.


**Two-phase locking (2PL)** just means: acquire locks as you need them during the transaction, and
release them all *at the end* (commit or rollback) — never in the middle. Releasing early would let
another thread see your half-finished work, which is precisely what week 5 worked so hard to prevent.


## 6.2 Deadlock — two people, two pens


Thread A holds `users` and wants `orders`. Thread B holds `orders` and wants `users`. Neither can
proceed. Neither will let go. They wait forever.


Two ways out:


**Prevention** — a rule making deadlock impossible. E.g. *always lock tables in alphabetical order.*
Then B would have taken `users` before `orders`, so this situation can't arise. Cheap, but it
constrains how you write everything.


**Detection** — let it happen, then notice. Keep a "wait-for" graph:


```
A ──waiting for──▶ B
▲                  │
└──waiting for─────┘        ← a cycle. That's a deadlock.
```


Find a cycle, pick a victim, abort it. Aborting means rolling back its transaction — **which you
already built in week 5.** The pieces compose, which is the nice part.


You're doing detection, because it's a better conversation and it reuses your rollback machinery.
Real SQLite does prevention (its strict lock ordering) — knowing both, and why each fits its context,
is the actual goal.


## 6.3 What breaks in the buffer pool


Your week-1 desk assumed one person. Now:


- Two threads asking for page 47 must get **the same object**, not two copies — otherwise their edits
  silently clobber each other.
- Eviction must not remove a page a thread is currently reading. **This is where pin counts start
  doing real work** rather than being a nicety.
- The dict itself needs a lock, or two simultaneous inserts can corrupt it.


Simplest correct approach: one lock around the page table, plus per-page state. Not the fastest.
Correct, and honest about being coarse.


## 6.4 The GIL, and being straight about it


Python runs one thread of bytecode at a time. You will not get 8× write throughput from 8 threads.


Don't pretend otherwise. What you demonstrate — and what actually matters — is **correctness under
contention**: no lost updates, no corruption, no hangs, deadlocks detected and resolved. Present it
that way, note the GIL as the reason write throughput is flat, and an interviewer will trust
everything else you tell them more. A benchmark that overclaims does the opposite.


## 6.5 The one test that matters


The classic, and it's classic because it's a real invariant:


```python
# 8 threads, 10,000 random transfers between 100 accounts.
# Each transfer: subtract N from account X, add N to account Y, in one transaction.
# INVARIANT: sum of all balances never changes. Ever.
```


If locking is broken, this fails — two threads read the same balance, both write back, one update
vanishes. It's the cleanest possible demonstration that your concurrency control actually works.


## 6.6 Your seven sessions


| #   | Do this                                                                                                               | Done when                                                                                            |
| --- | --------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| 1   | Split `Database` (shared: file, pool, lock manager) from `Connection` (per-thread: transaction, cursors). A refactor. | All existing tests pass; two connections can be opened to one database                               |
| 2   | Make the buffer pool thread-safe: lock the page table, per-page state, safe eviction                                  | Two threads hammering `get_page` on overlapping pages don't corrupt the pool                         |
| 3   | `LockManager`: S/X lock table, `threading.Condition` for waiting, compatibility matrix                                | Unit tests: S+S compatible, S+X blocks, X+X blocks, waiter wakes on release                          |
| 4   | Wire 2PL into execution: acquire on first table touch, release all at commit/rollback                                 | Two sequential transactions in different threads serialize correctly                                 |
| 5   | Wait-for graph + cycle detection + abort the youngest victim                                                          | A deliberately constructed deadlock resolves: one gets `DeadlockError`, the other commits            |
| 6   | `busy_timeout`, lock wait timeouts, the transfer stress test                                                          | The stress test runs (it may well fail — that's session 7)                                           |
| 7   | **Debug the stress test.** Then `benchmarks/concurrent.py`.                                                           | `sum(balances)` invariant holds over 8 threads × 10k transactions, repeatedly, in CI without flaking |


Session 7 will take longer than two hours. Concurrency bugs are intermittent by nature. Two things
help: seed your random number generator so failures reproduce, and log every lock
acquire/release/wait with thread IDs so you can reconstruct what happened.


Also: name your isolation level in `docs/concurrency.md`, and say which anomalies it permits. Read
[Berenson et al.](https://arxiv.org/abs/cs/0701157) — after it you can say "I implemented X, it
permits Y, here's the tradeoff" instead of vaguely claiming ACID. Interviewers notice that
difference instantly.


---


# Week 7 — Cross-Referencing Two Binders


**One sentence:** `JOIN`, `GROUP BY`, `ORDER BY` — the things that make it feel like a real database
to someone who isn't a storage engineer.


## 7.1 Joins: for each X, find the matching Y


You have a list of people and a list of orders, each order noting whose it is. You want every person
paired with each of their orders.


**Nested loop join** is exactly what it sounds like:


```python
for person in people:            # outer
    for order in orders:         # inner — the WHOLE list, every time
        if order.user_id == person.id:
            yield (person, order)
```


Correct, and O(n × m). 1,000 people × 10,000 orders = 10 million comparisons.


**Index nested loop join** is the same thing with one change: if `orders` has an index on `user_id`,
then for each person you *jump straight* to their orders instead of walking the whole list.
1,000 × log(10,000) ≈ 13,000 operations instead of 10 million.


That's the entire idea. And note it's the same insight as week 4 — an index turns "look at
everything" into "jump to the right place" — just applied inside a loop.


**Join order matters, sometimes enormously.** Joining A to B is logically the same as B to A, but the
*cost* isn't: you want the smaller relation as the outer loop and the indexed one as the inner.
SQLite's docs work an example where one order beats the other by roughly **2000×**.


Now, "smaller table outer" is a *heuristic* — and this is a good moment to notice why week 4's cost
model was worth building, because the heuristic is wrong often enough to matter. It ignores whether the
inner side has a usable index (a big table with an index on the join key makes a *better* inner than a
small one without), and it ignores how many rows survive each table's `WHERE` clause before the join
even starts. A 1M-row table filtered to 3 rows is the smaller relation, and only the estimates know it.


So instead of a heuristic, week 7 reuses the four stages from §4.3:


```text
stage 1: which orders are legal?      -- inner joins reorder freely; LEFT JOIN doesn't
stage 2: how many rows does each      -- |R| × |S| / max(distinct values on either key)
         join produce?
stage 3: what does each order cost?   -- outer cost + outer rows × one inner lookup
stage 4: pick the cheapest            -- and with ≤3 tables there are at most 3! = 6 orders,
                                         so just try them all
```


That last point is the nice one to be able to say out loud: **because the language caps joins at three
tables, you can afford to be exhaustive, and exhaustive beats clever.** Six orders is nothing. SQLite
can't do this — it supports joins of dozens of tables, so it uses a polynomial heuristic search ("N3",
keeping the N best partial plans at each step) that is fast but not guaranteed optimal. Your search *is*
guaranteed optimal under your own cost model, purely because your problem is smaller. Knowing which
constraint bought you that is the interesting half of the answer.


`EXPLAIN` then shows what it chose, which is a great demo moment — especially next to the page-read
count for the order it *rejected*.


## 7.2 Aggregation: a tally sheet


You have a pile of ballots and want counts per candidate. You don't sort the pile — you keep a tally
sheet and go through the pile once:


```python
tally = {}
for row in child:                      # one pass
    key = row[group_by_column]
    tally.setdefault(key, Accumulator())
    tally[key].add(row)
for key, acc in tally.items():
    yield (key, acc.result())
```


That's a **hash aggregate**, and it's how `GROUP BY` works. `COUNT` keeps a counter, `SUM` keeps a
running total, `AVG` keeps both.


Note this operator is different in kind from `Filter`: it must consume its *entire* input before it
can emit anything, because the last row might belong to the first group. Operators that block like
this (aggregation, sorting) are called pipeline breakers, and knowing the distinction is worth a
sentence in an interview.


## 7.3 This week also tests your week-3 design


If the iterator abstraction is right, a join is just an operator with *two* children, and aggregation
is one that drains its child. They should slot in cleanly.


If adding a join turns out to be painful, that's real information: something leaked into your
executor interface that shouldn't have. Either outcome teaches you something — that's why this week
is placed after the concurrency work rather than before.


## 7.4 Your seven sessions


| #   | Do this                                                                                        | Done when                                                                                                                                                 |
| --- | ---------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | Parse `JOIN ... ON`, comma joins, table aliases, qualified names (`u.id`)                      | ASTs correct; ambiguous unqualified column raises a clear error                                                                                           |
| 2   | Binder handles multiple tables — column resolution across a join                               | `u.id` and `o.user_id` resolve to the right (table, index) pairs                                                                                          |
| 3   | `NestedLoopJoin` operator                                                                      | Two-table join returns correct rows; verified against `sqlite3`                                                                                           |
| 4   | `IndexNestedLoopJoin` + the planner **costing** it against a plain scan inner side             | `EXPLAIN` shows the index join when it's cheaper, a scan when it isn't; page-read count drops sharply, and you record the number for the losing order too |
| 5   | `HashAggregate`: `COUNT`, `SUM`, `AVG`, `MIN`, `MAX`, with and without `GROUP BY`              | Matches `sqlite3` on all five, grouped and ungrouped                                                                                                      |
| 6   | `Sort` (in-memory, documented limit), multi-key `ORDER BY` with `ASC`/`DESC`, `LIMIT`/`OFFSET` | `LIMIT 1` over a million rows doesn't scan a million rows — prove it with the page counter                                                                |
| 7   | `HAVING`, `DISTINCT`, and heavy differential testing on `NULL`                                 | The full example query below works                                                                                                                        |


```sql
SELECT u.age, COUNT(*), AVG(o.total)
FROM users u JOIN orders o ON o.user_id = u.id
WHERE u.age > 25
GROUP BY u.age
HAVING COUNT(*) > 5
ORDER BY 2 DESC
LIMIT 10;
```


⚠️ **`NULL` will be your bug source this week.** SQL uses three-valued logic: `NULL = NULL` is not
true, it's `NULL`. Consequences worth testing explicitly against `sqlite3`:


- `COUNT(*)` counts rows; `COUNT(col)` skips NULLs
- `AVG` ignores NULLs (so it isn't `SUM/COUNT(*)`)
- `SUM` over zero rows is `NULL`, but `COUNT` over zero rows is `0`
- `NULL` sorts at one specific end in `ORDER BY` — find out which
- `NULL` is a valid `GROUP BY` key, and all NULLs group together (even though `NULL != NULL`)


Twenty minutes of deliberate thought about three-valued logic before you implement comparison will
save you a session of confusion.


---


# Week 8 — Making Someone Care


**One sentence:** convert 100 hours of engineering into something a stranger values in 40 seconds.


## 8.1 Why this is a whole week, not an afternoon


A recruiter gives your GitHub 40 seconds. A hiring manager gives it three minutes. Neither will open
`btree.py`.


So 100 hours of engineering nobody can evaluate is worth less than 80 hours a stranger understands
immediately. This week isn't polish — it's the conversion step, and for your actual goal it's the
highest-ROI week in the plan.


Here's the parallel worth internalizing: **SQLite's documentation and test suite are as responsible
for its ubiquity as its code is.** It ships 590× more test code than library code, across four
independent harnesses, and it publishes [a document](https://www.sqlite.org/testing.html) explaining
exactly how it earns trust. Nobody adopts an embedded database because its B-tree is elegant. They
adopt it because the project makes its reliability *legible*.


Your README does that same job at small scale.


## 8.2 The README, in order


Most people read only the first screen. So the order is the design:


1. **One sentence** saying what it is, then badges
2. **The GIF** — a 20-second asciinema of the demo. This does more work than everything below it.
3. `pip install -e .` and a 15-line usage example someone can paste
4. Architecture diagram
5. Feature table, ✅/❌
6. Benchmark table with real numbers
7. **"Not implemented / known limitations"**
8. Testing story — counts by category, crash matrix called out
9. Design decisions, linking your ADRs


**On #7, which feels wrong and isn't:** listing what you *didn't* build is one of the highest-signal
sections in the repo. It proves you know the difference between a subset and a product, and it
preempts "did you realize you don't handle X?" — a question that's damaging when it surprises you and
completely harmless when your README already answered it. Be specific: "no page merging after delete,
so pages can sit half-empty; space is reused via the freelist but the tree isn't compacted" beats
"some limitations exist."


## 8.3 Your seven sessions


| #   | Do this                                                                                                                                                                                                                                 |
| --- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | README sections 1, 3, 4, 5                                                                                                                                                                                                              |
| 2   | Benchmark harness: point lookup, scan, insert throughput (sorted vs random), index vs no index, buffer-pool hit rate, tree height vs row count, threads vs throughput                                                                   |
| 3   | `docs/architecture.md` with a diagram matching the real module layout; `docs/file-format.md`                                                                                                                                            |
| 4   | 6–8 ADRs. One page each: the decision, the alternatives, why. (Undo journal not WAL; iterators not bytecode; no page merging; SQLite's format exactly but only one direction; table-level locking; deadlock detection over prevention.) |
| 5   | `quilldb shell` — a REPL. Worth its 90 minutes on its own: "let me just show you" beats any explanation.                                                                                                                                |
| 6   | Record the GIF. CI matrix across 3.11/3.12/3.13 with coverage. Test a clean-checkout install in a fresh virtualenv.                                                                                                                     |
| 7   | Write the 5-minute demo script. Deliver it out loud, twice, from memory.                                                                                                                                                                |


**Success state:** a stranger clones it, pastes the example, and it works on the first try. And you
can talk through the whole project for five minutes without notes.


---


# Glossary


Terms in the roadmap that this guide explains, with where.


| Term                         | Plain meaning                                                                              | Where |
| ---------------------------- | ------------------------------------------------------------------------------------------ | ----- |
| **Page / sheet**             | A fixed 4096-byte chunk of the file, addressed by number                                   | §0    |
| **Pager**                    | The layer that fetches and writes pages, hiding the file                                   | §1.1  |
| **Buffer pool / page cache** | A bounded in-memory dict of recently used pages                                            | §1.5  |
| **Dirty page**               | A cached page modified but not yet written to disk                                         | §1.5  |
| **Pin count**                | How many things are currently using a page; can't evict above zero                         | §1.5  |
| **LRU**                      | Least Recently Used — evict whatever you touched longest ago                               | §1.5  |
| **Slotted page**             | Page layout with an offset array from the top, data from the bottom                        | §1.2  |
| **Cell**                     | One entry on a page — a row, or a signpost                                                 | §1.2  |
| **Varint**                   | Variable-length integer; small numbers take fewer bytes                                    | §1.3  |
| **Serial type**              | A code in a record header saying what type/size comes next                                 | §1.4  |
| **Record**                   | One row encoded as bytes: type header, then values                                         | §1.4  |
| **Overflow page**            | Continuation page for a row too big for one page                                           | §2.5  |
| **B+tree**                   | Sorted tree; interior pages navigate, leaves hold data                                     | §2.2  |
| **Leaf / interior page**     | Holds rows / holds only signposts to child pages                                           | §2.2  |
| **Split**                    | Full page → two pages + a new signpost in the parent                                       | §2.3  |
| **Separator / promotion**    | The signpost key handed up to the parent after a split                                     | §2.3  |
| **Root split**               | The only way a B+tree gets taller                                                          | §2.3  |
| **Cursor**                   | A position in the tree that can move forward and back                                      | §2.6  |
| **Freelist**                 | Linked list of pages available for reuse                                                   | §1.6  |
| **AST**                      | Abstract Syntax Tree — the parsed structure of a query                                     | §3.1  |
| **Tokenizer / lexer**        | Splits SQL text into words and symbols                                                     | §3.1  |
| **Binder**                   | Resolves names to positions and type-checks                                                | §3.1  |
| **Volcano / iterator model** | Query operators chained by `next()`, one row at a time                                     | §3.2  |
| **Operator**                 | One node in the assembly line: `SeqScan`, `Filter`, `Project`…                             | §3.2  |
| **Pipeline breaker**         | An operator that must consume all input before emitting (sort, aggregate)                  | §7.2  |
| **Catalog**                  | The table that describes all other tables                                                  | §3.3  |
| **Secondary index**          | A second B+tree sorted by a different column                                               | §4.1  |
| **Covering index**           | An index containing every column the query needs — skips the table lookup                  | §4.1  |
| **Leading column rule**      | An index only helps if you constrain its columns left to right, no gaps                    | §4.2  |
| **Planner**                  | Generates every *legal* access path, then costs them and picks the cheapest                | §4.3  |
| **Rule- vs cost-based**      | Rules decide which plans are legal; costs decide which legal plan wins. Stages, not rivals | §4.3  |
| **`ANALYZE` / statistics**   | Counts distinct values per index prefix and stores them in `quill_stat1`                   | §4.3  |
| **Selectivity**              | What fraction of rows a predicate keeps — 1 row per value vs 5,000                         | §4.3  |
| **Cardinality estimate**     | The planner's guess at how many rows an operator will emit                                 | §4.3  |
| **Cost model**               | Converts estimated rows into estimated page reads, so plans are comparable                 | §4.3  |
| **`fsync`**                  | "Actually put it on the physical disk, and don't return until it's there"                  | §5.3  |
| **Journal**                  | File holding original page contents so a transaction can be undone                         | §5.2  |
| **Commit point**             | The instant a transaction becomes durable — for you, deleting the journal                  | §5.2  |
| **Hot journal**              | A journal found on open, meaning the last run was interrupted                              | §5.2  |
| **Atomicity**                | All of a transaction happens, or none of it                                                | §5.1  |
| **Fault injection**          | Deliberately failing writes to test recovery                                               | §5.4  |
| **Shared / exclusive lock**  | Many readers / one writer                                                                  | §6.1  |
| **2PL**                      | Acquire locks during the transaction, release all at the end                               | §6.1  |
| **Deadlock**                 | Two transactions each waiting on what the other holds                                      | §6.2  |
| **Wait-for graph**           | Who-waits-on-whom; a cycle means deadlock                                                  | §6.2  |
| **Isolation level**          | Precisely what one transaction can see of another's work                                   | §6.6  |
| **Nested loop join**         | For each row of A, look through B                                                          | §7.1  |
| **Hash aggregate**           | One pass, keeping a tally dict keyed by the `GROUP BY` value                               | §7.2  |
| **ADR**                      | Architecture Decision Record — one page on a choice and its alternatives                   | §8.3  |


---


# If You Read Only One Thing Per Week


| Week | Read this                                                                                                                                           | Time   |
| ---- | --------------------------------------------------------------------------------------------------------------------------------------------------- | ------ |
| 1    | [SQLite Internals: Pages & B-trees](https://fly.io/blog/sqlite-internals-btree/)                                                                    | 20 min |
| 2    | [db_tutorial parts 10, 13, 14](https://cstack.github.io/db_tutorial/)                                                                               | 90 min |
| 3    | [Simple but Powerful Pratt Parsing](https://matklad.github.io/2020/04/13/simple-but-powerful-pratt-parsing.html)                                    | 40 min |
| 4    | [SQLite optoverview §2](https://www.sqlite.org/optoverview.html) (legality) + [queryplanner-ng](https://www.sqlite.org/queryplanner-ng.html) (cost) | 50 min |
| 5    | [Atomic Commit In SQLite](https://www.sqlite.org/atomiccommit.html) — **twice**                                                                     | 2 h    |
| 6    | [A Critique of ANSI SQL Isolation Levels](https://arxiv.org/abs/cs/0701157)                                                                         | 1 h    |
| 7    | CMU 15-445 "Joins" + "Sorting & Aggregations" slides                                                                                                | 40 min |
| 8    | [toydb](https://github.com/erikgrinaker/toydb)'s README and architecture doc, as a template                                                         | 30 min |


Full annotated list, including what to skip and why, is in `references.md`.