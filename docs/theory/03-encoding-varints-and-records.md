# 03 — Encoding: Varints and Records


> **Read before week 1 sessions 2 and 7** (`codec/varint.py`, `codec/record.py`).
>
> **Time:** ~30 minutes. **Prerequisite:** [chapter 02](02-slotted-page.md).
>
> ⚠️ **This chapter settled two questions that used to be open in your repo, and both are now decided in `constants.py`.** `MAX_VARINT_BYTES` is **9**, not 10 — §3.4 derives why. And there is **no zigzag encoding** — §3.5 explains what SQLite does instead, and why zigzag is a good idea that doesn't belong in this format. Read both before you write `codec/varint.py`; changing either later means regenerating every test fixture you own.


---


## 3.0 The problem: how do you write the number 5?


You have a row: `(1, "ada", 36)`. You have a page: 4096 bytes. You need to turn the first into bytes you can put in the second, and read it back identically.


Sounds trivial. It isn't, and the reason it isn't is the reason `codec/` is its own package: **there is no self-evident answer, and the answer you pick constrains everything.** `roadmap.md` week 1 says the record codec "is the one component you cannot refactor later without regenera ting every test fixture you own." That's true, so let's be careful.


Start with a single number. **How many bytes does the integer 5 take?**


---


## 3.1 Wrong answer 1: eight bytes, always


`struct.pack('>q', 5)` → `b'\x00\x00\x00\x00\x00\x00\x00\x05'`


Every integer gets 8 bytes. Simple, fast, and — crucially — **fixed-width**, which means you can jump to "the 5th integer" by arithmetic.


**Why it's wasteful, quantitatively:**


Real databases are full of small numbers. Row IDs start at 1 and count up. String lengths are 3, 11, 40. Ages are under 120. Prices are a few thousand pennies. Type codes are single digits.


Store the number 5 in 8 bytes and **7 of those bytes are zero.** For a row like `(1, "ada", 36)`, you'd spend 8 bytes on the rowid, 8 on the string length, 8 on the age = 24 bytes of integer to express three values whose combined information content is about 2 bytes.


Per chapter 00 §0.3 and §0.4, the currency you're spending is **page touches**. Bytes matter only insofar as they determine *how many rows fit on a page*, and that determines how many pages a table occupies, and that determines how many reads a scan costs. Concretely: if a compact encoding fits 80 rows per page where a fixed-width one fits 50, then a full scan of a million rows costs 12,500 page reads instead of 20,000. **A 37% reduction in the only resource that's genuinely scarce.**


That's the entire argument for compact encoding: bytes-per-row is a *proxy for page reads*, and page reads are the thing.


---


## 3.2 The answer: varints — spend bytes proportional to magnitude


> **Use 7 bits of each byte for data. Use the top bit as a flag meaning "there's more coming."**


```
  5   →  0000_0101
          ▲
          └── top bit 0: this is the last byte. Value = 5.  ONE BYTE.




 300  →  1000_0010   0010_1100
          ▲           ▲
          │           └── top bit 0: last byte. Low 7 bits = 010_1100 = 44
          └── top bit 1: more coming. Low 7 bits = 000_0010 = 2


         Value = (2 << 7) | 44 = 256 + 44 = 300.  TWO BYTES.
```


Decoding is a loop:


```python
def decode(buf, pos):
    value = 0
    while True:
        byte = buf[pos]; pos += 1
        value = (value << 7) | (byte & 0x7F)   # accumulate 7 bits
        if not (byte & 0x80):                  # top bit clear → done
            return value, pos
```


Capacity per length:


| Bytes | Bits | Max value | Covers |
|---|---|---|---|
| 1 | 7 | 127 | type codes, small lengths, ages |
| 2 | 14 | 16,383 | most string lengths |
| 3 | 21 | 2,097,151 | rowids in a 2M-row table |
| 4 | 28 | 268,435,455 | rowids in a 268M-row table |
| 5 | 35 | 34 billion | |
| … | | | |
| 9 (SQLite) | 64 | full 64-bit range | see §3.4 |


**The practical upshot:** virtually every number in a real database fits in 1–3 bytes. Rowids stay small for a long time, lengths are small, type codes are tiny.


### The tradeoff you're accepting — and this is the important part


> **You lose random access.** You cannot jump to "the 5th varint" because you don't know how long the first four are until you've read them.


This is a genuine loss, not a technicality. It means the "manifest" design in §3.7 becomes *necessary* rather than merely nice: if you couldn't skip over the fields, you'd have to decode everything.


But notice *why* it's acceptable here: you read a record's fields front-to-back anyway. **The access pattern you already have makes the cost you're paying free.** That's what a good tradeoff looks like — not "the downside is small" but "the downside falls in a direction we never go."


`guide.md` §1.3 puts this well: "Space saved, random access given up. Noticing tradeoffs like this is most of what database design *is*." That's exactly right, and it's worth being able to articulate the general form: *every encoding choice trades some combination of space, decode speed, and random access, and you pick based on your access pattern.*


### Where else you've seen this


Varints are not a database thing. They're everywhere data crosses a boundary and size matters:


- **Protocol Buffers** — every integer field, using LEB128 (little-endian, see §3.3)
- **UTF-8** — the exact same continuation-bit idea applied to Unicode code points, which is why ASCII is 1 byte and emoji are 4
- **MIDI**, **DWARF** debug info, **Git** packfiles, **WebAssembly** — all use LEB128
- **Your SSD's internal metadata**, quite likely


Being able to say "this is the same technique as UTF-8's continuation bytes" makes it click for an interviewer immediately, because they already know UTF-8.


> **Say this out loud:** "Varints use 7 bits per byte with the top bit as a continuation flag, so small numbers cost 1 byte instead of 8. That matters because bytes-per-row determines rows-per-page, which determines page reads — the actual scarce resource. The cost is losing random access into a sequence of them, which is free here because records are read front-to-back anyway. Same technique as UTF-8's continuation bytes."


---


## 3.3 The endianness trap — decide now, write it down


Two mutually incompatible conventions exist, and they differ in **which end holds the most significant bits**.


### Big-endian (SQLite): most significant group first


```
300 = 0b100101100  (9 bits: 10 | 0101100)


Split high-to-low, emit high group first:
   byte 0: 1_0000010   (continuation set, holds high bits  10)
   byte 1: 0_0101100   (last byte,        holds low bits 0101100)
```


Decode: `value = (value << 7) | (byte & 0x7F)` — shift left, accumulate.


### Little-endian / LEB128 (Protobuf, DWARF, WebAssembly): least significant group first


```
300 = 0b100101100


Emit low group first:
   byte 0: 1_0101100   (continuation set, holds low bits 0101100)
   byte 1: 0_0000010   (last byte,        holds high bits    10)
```


Decode: `value |= (byte & 0x7F) << shift; shift += 7` — accumulate with increasing shift.


### Why this will hurt if you get it wrong


Note that the *same value* produces `[0x82, 0x2C]` under one scheme and `[0xAC, 0x02]` under the other. Neither is malformed. Both decode to *a* number. **Mixing them gives you numbers that are wrong but plausible** — 300 becomes 5,634, or 44 becomes 5,632. Not a crash, not an exception: a number that's the right shape and the wrong value, and it will show up 200 operations later as a key that can't be found.


`guide.md` §1.3 calls this "a genuinely miserable afternoon," and it's right. The reason it's miserable is that the failure has no locality — the bug is in the codec, the symptom is in the B+tree.


### Which to pick


**Big-endian, to match SQLite.** Three reasons:


1. **Consistency.** Everything else in your format is big-endian: `constants.py` says "all big-endian," the u16 cell count, the u32 page numbers. One convention, one mental model, one set of helpers in `codec/ints.py`.
2. **Big-endian varints sort correctly as bytes.** This is the real technical reason and it's not obvious. If you compare two big-endian varints with `memcmp` — plain byte-wise comparison — the result matches numeric order. That's because the most significant bits come first, so the first differing byte decides, exactly as in decimal. Little-endian varints do *not* have this property.


   This matters directly in week 4. Index keys are compared lexicographically as byte strings (`roadmap.md` week 4: "Sorting encoded keys as bytes matches sorting the tuples logically"). Big-endian encoding makes that comparison *free* — no decoding, just `bytes_a < bytes_b`. It's an example of the general and very useful principle: **an encoding whose byte order matches its value order lets you compare without decoding.**
3. **You can cite the spec.** "Varints are big-endian: bits taken from the earlier byte of the varint are more significant than bits taken from the later bytes." — [fileformat2.html](https://www.sqlite.org/fileformat2.html)


**Write it in `docs/file-format.md` before you write `codec/varint.py`.** Not after. The whole point of a format doc is that it's the thing you check when the encoder and decoder disagree, and it's worthless if it was written by copying the code.


> **Say this out loud:** "Big-endian, matching SQLite — and not just for consistency: big-endian varints compare correctly under plain byte comparison, because the most significant bits come first. That makes index key comparison a `memcmp` with no decoding, which is exactly what week 4 needs."


---


## 3.4 Why SQLite's maximum is 9 bytes, not 10


### The problem


You need to encode a full 64-bit integer. At 7 bits per byte, how many bytes?


⌈64 / 7⌉ = **10 bytes.** Nine bytes gives you 63 bits — one short. So a naive scheme needs 10, and the 10th byte carries exactly one meaningful bit. That number is correct *for a pure 7-bits-per-byte scheme*, and it's the value `MAX_VARINT_BYTES` used to hold.


SQLite spends 9. **How?**


### The trick


By making the ninth byte special: it has no continuation bit, and all 8 of its bits are data.


> "A varint is between 1 and 9 bytes in length. The varint consists of either zero or more bytes which have the high-order bit set followed by a single byte with the high-order bit clear, **or nine bytes, whichever is shorter**. The lower seven bits of each of the first eight bytes and **all 8 bits of the ninth byte** are used to reconstruct the 64-bit twos-complement integer."
> — [fileformat2.html](https://www.sqlite.org/fileformat2.html)


The arithmetic: 8 bytes × 7 bits + 1 byte × 8 bits = 56 + 8 = **exactly 64 bits.**


Why is this safe? Because the continuation bit's only job is to say "keep reading," and **after eight bytes there is nothing left to say** — the format guarantees the varint ends there. The flag becomes redundant, so the bit is repurposed as data. You get 64 bits in 9 bytes with zero waste.


The decoder needs one special case:


```python
def decode_varint(buf, pos):
    value = 0
    for i in range(8):                       # bytes 1..8: 7 bits each
        byte = buf[pos]; pos += 1
        if not (byte & 0x80):
            return (value << 7) | byte, pos  # terminated early
        value = (value << 7) | (byte & 0x7F)
    value = (value << 8) | buf[pos]          # byte 9: ALL 8 bits, no flag
    return value, pos + 1
```


### Why this is a genuinely good design detail to know


It's small, it's clever, and it demonstrates a way of thinking worth having: **a flag whose answer is already implied by the format is a wasted bit, so reclaim it.**


It's also a good illustration of *bounded* cleverness. The special case costs one branch in the decoder and buys 10% off the worst case, which is a fine trade. Being able to explain *why* the ninth byte can safely drop its flag — because after eight bytes the length is already determined — shows you understand the mechanism rather than having memorised "SQLite varints are 1 to 9 bytes."


> **Say this out loud:** "Nine bytes, not ten, because the ninth byte drops its continuation flag and contributes all 8 bits — 8×7 + 8 = exactly 64. The flag is redundant there since the format guarantees the varint can't be longer, so the bit gets reclaimed as data."


---


## 3.5 Negative numbers: two's complement vs zigzag


### The problem


Encode `-1`.


### Why the naive answer is terrible


As a 64-bit two's complement integer, `-1` is `0xFFFFFFFFFFFFFFFF` — **all 64 bits set.** Varint-encode that and you get the maximum length, 9 bytes. And it's not just -1: **every negative number costs 9 bytes**, because two's complement sets all the high bits for any negative value. The number -5 costs 9 bytes; the number 5 costs 1.


SQLite accepts this. Its varints encode "64-bit twos-complement integers," so negatives are always maximal length. That's fine for SQLite because in its actual usage — rowids, payload lengths, serial type codes, header sizes — the values are *never negative*. The signed type is there for generality, not because negatives are expected.


### One possible fix: zigzag encoding


Hold this one at arm's length — it's the answer you'd reach for, it's a real technique used by real systems, and it is *not* what SQLite does. Work through it anyway, because the reason it's wrong here is the interesting part.


Map signed integers onto unsigned ones so that **numbers near zero stay near zero**, alternating sign:


| Signed | Zigzag | Bytes |
|---|---|---|
| 0 | 0 | 1 |
| −1 | 1 | 1 |
| 1 | 2 | 1 |
| −2 | 3 | 1 |
| 2 | 4 | 1 |
| −64 | 127 | 1 |
| 63 | 126 | 1 |


```python
def zigzag_encode(n: int) -> int:
    return (n << 1) ^ (n >> 63)      # for 64-bit; in Python use (n << 1) ^ (-1 if n < 0 else 0)


def zigzag_decode(u: int) -> int:
    return (u >> 1) ^ -(u & 1)
```


The mechanism: shift left by one (freeing the low bit), then put the sign in that low bit, inverting the magnitude bits for negatives. The name is the picture — the mapping zigzags across zero: 0, −1, 1, −2, 2, …


Cost: **−5 goes from 9 bytes to 1.**


This is Protocol Buffers' `sint32`/`sint64`. Protobuf notably has *both* — plain `int64` (two's complement, so negatives cost 10 bytes in LEB128) and `sint64` (zigzag) — and makes you choose, precisely because it's a real tradeoff and only you know whether your field goes negative.


### What SQLite actually does — and it's better than zigzag, for a reason worth getting


SQLite has **no zigzag varint at all.** Instead, `INSERT INTO t VALUES (-5)` stores the value like this:


```
serial type 1  →  "1-byte signed integer"
body           →  0xFB          (-5 in 8-bit two's complement)
```


**One byte of body.** Same as zigzag would cost. And look at the serial type table in §3.8: codes 1–6 mean widths of 1, 2, 3, 4, 6, and 8 bytes, each holding a two's-complement integer at that width. The encoder picks the narrowest bucket the value fits in. `-5` fits in 8 bits, so it costs one byte; `-1000000` needs 24 bits, so it costs three.


Now the argument for why this beats zigzag *here*, which is the part actually worth understanding:


> **A varint's continuation bits are a length prefix that you pay for inline. A serial type code is a length prefix you were already paying for anyway.**


Every value in a record already has a type code in the header — it must, or you couldn't tell text from integers or find where each value ends (§3.6). Once you're spending that code regardless, **making it also encode the width is free.** A zigzag varint in the body would be paying a second time for length information the header already carries.


That's why Protocol Buffers *does* want zigzag and SQLite doesn't: protobuf fields are self-delimiting and have no separate width table, so the varint must carry its own length. SQLite records have a manifest. **Same problem, different surrounding structure, opposite right answer** — which is a much better thing to be able to say than "SQLite uses two's complement and protobuf uses zigzag."


### Where the 9-byte negative actually still bites


Not everywhere is a record. **Rowids** are bare varints in a table b-tree cell (chapter 06 §6.0) with no type code in front of them. So a negative rowid really does cost the full 9 bytes, and SQLite simply accepts that — negative rowids are legal but pathological, and the format doesn't optimise for them.


Same for the other bare varints: payload lengths, header sizes, serial type codes. All are non-negative by construction, so the question never arises. **The signed type is there for generality, not because negatives are expected.**


### What to build


Not what `roadmap.md` week 1 currently says. **Build one unsigned varint codec, not two:**


- `encode_varint` / `decode_varint` over `0 <= n < 2**64` — used for rowids, payload lengths, header sizes, serial type codes. Everything.
- Two tiny helpers, `to_twos_complement` / `from_twos_complement`, to reinterpret a signed rowid as the u64 bit pattern the varint holds. Two lines each, no new encoding.
- Signed *record* integers are handled entirely in `codec/record.py`, by choosing a serial type and packing to that width with `int.to_bytes(w, "big", signed=True)`.


That deletes `encode_svarint`/`decode_svarint` from the plan, and with them a whole class of bug: **there is now exactly one varint encoding in the system.** Two encodings that differ only in interpretation, applied to the same byte stream, is precisely the endianness trap of §3.3 wearing a different hat — and the failure mode is identical, a plausible wrong number surfacing far from its cause.


**Zigzag is still worth knowing and worth naming as a road not taken:** *"Zigzag would make small negatives one byte in a bare varint, and it's what protobuf's `sint64` does. It's unnecessary here because record integers already carry a serial type that names their width, so the header pays for the length information and the body doesn't have to. It would only help for bare varints like rowids, where negatives are pathological anyway."*


> ⚠️ **Test the boundaries explicitly, not just randomly.** Hypothesis will find most bugs, but you should hand-write assertions at: 0, 1, 127, 128 (the 1→2 byte boundary), 16383, 16384, every subsequent 7-bit boundary, **2⁵⁶−1 and 2⁵⁶ (the 8→9 byte boundary, where the ninth-byte special case kicks in)**, and 2⁶⁴−1. Every length transition is a place where an off-by-one in a shift lives, and the 8→9 transition is the one your own code makes uniquely dangerous. `implementation.md` week 1 session 2 requires "every length boundary explicitly" — that's why.


---


## 3.6 The problem with a row: where does each value end?


Now the real thing. You have `(1, "ada", 36)`. You've got varints. Just write them out?


```
[varint 1][varint ???]["ada"][varint 36]
```


Immediately stuck. When you read this back, how do you know:


- that field 2 is a string and not an integer?
- how many bytes of string to read?
- that field 3 is an integer again?


Bytes have no types. `0x61` is the letter `a`, the integer 97, and part of a float, and nothing in the byte says which. **Type information must be stored, or agreed in advance.**


### Wrong answer 1: agree in advance, from the schema


"The catalog says column 2 is TEXT, so read a string."


Reasonable, and it does save space. But it breaks on details that matter:


- **You still don't know the length.** TEXT is variable-length. You need a length regardless.
- **NULL.** Any column can be NULL. If the schema says INTEGER and the value is NULL, what's in the body? You need a per-value marker anyway.
- **Schema evolution.** `ALTER TABLE ADD COLUMN` means old rows have fewer values than the schema has columns. If the record is schema-dependent, every old row becomes unreadable the moment you change the schema — or you rewrite the whole table.
- **The record codec would depend on the catalog**, inverting your layering. `roadmap.md` §3: "layers only call downward." The codec is below the catalog. A record must be decodable knowing nothing but its own bytes.


That last point is architectural and it's the strongest. **A record should be self-describing.**


### Wrong answer 2: tag every value inline


```
[type=INT][1][type=TEXT][len=3]["ada"][type=INT][36]
```


Self-describing. Works. And it's essentially what MessagePack and CBOR do.


**Why it's not ideal here:** to reach column 3 you must walk through columns 1 and 2 completely, including their *data*. For a wide row where you want one late column — `SELECT age FROM users` on a table with 30 columns and a large TEXT blob in column 4 — you decode everything to get to it.


**But it's close.** The insight is one step away: the *types and sizes* are what you need to navigate. The *data* is what you need to return. **Separate them.**


---


## 3.7 The answer: a manifest, then the crate


> **Write a header listing what types are coming. Then write the raw values, with no interleaved metadata.**


```
┌──────────────────────────────────┬───────────────────────────────┐
│           HEADER                 │            BODY               │
│  [hdr size][type][type][type]    │  [value][value][value]        │
│      4       1     17     1      │    01    "ada"    24          │
│           ↑int8 ↑text,3ch ↑int8  │                               │
└──────────────────────────────────┴───────────────────────────────┘
```


To read column 3: read the header (three small varints), see that column 1 is 1 byte and column 2 is 3 bytes, **skip 4 bytes**, read column 3. You never touched the string.


The analogy `guide.md` §1.4 uses is exactly right: a **shipping manifest taped to the outside of a crate.** You read the manifest to learn what's inside and how big each item is, without unpacking anything.


### Why the header's first varint is its own total size


> "The header begins with a single varint which determines the total number of bytes in the header. The varint value is the size of the header in bytes **including the size varint itself**."
> — [fileformat2.html](https://www.sqlite.org/fileformat2.html)


Two things this buys:


1. **The header is self-delimiting.** You know where the body starts before you've parsed a single type code — `body_start = record_start + header_size`. So you can compute a value's absolute position from the type codes alone. Without this you'd have to count type codes and know the column count from elsewhere (which would reintroduce the catalog dependency from §3.6).
2. **Including itself is the subtle part.** It makes the number *self-consistent* — the header is exactly `header_size` bytes, full stop, with no "plus the size of the size field" adjustment at every use. It's mildly awkward to *compute* when encoding (the size affects its own encoded length, so if adding a byte pushes the varint from 1 to 2 bytes you have to recompute) and it eliminates a whole class of off-by-one at every read site. **Trading a fiddly encoder for a foolproof decoder is almost always correct**, because you write the encoder once and read records billions of times.


⚠️ That recursion is a real trap: `header_size = 1 + sum(len(varint(t)) for t in types)` is wrong when the sum pushes the total past 127, because then the size varint itself becomes 2 bytes. Compute it in a small fixed-point loop, and write a test with exactly enough columns to straddle 127.


---


## 3.8 Serial types: SQLite's encoding, and the trick in it


The type codes, from the spec:


| Code | Content size | Meaning |
|---|---|---|
| 0 | **0** | NULL |
| 1 | 1 | 8-bit signed int |
| 2 | 2 | 16-bit signed int |
| 3 | 3 | 24-bit signed int |
| 4 | 4 | 32-bit signed int |
| 5 | 6 | 48-bit signed int |
| 6 | 8 | 64-bit signed int |
| 7 | 8 | IEEE-754 64-bit float |
| 8 | **0** | **the integer 0** |
| 9 | **0** | **the integer 1** |
| 10, 11 | — | reserved for internal use; never in a well-formed file |
| **N ≥ 12, even** | (N−12)/2 | BLOB of (N−12)/2 bytes |
| **N ≥ 13, odd** | (N−13)/2 | TEXT of (N−13)/2 bytes |


### Four things to actually notice


**1. NULL costs zero bytes.** The type code *is* the value. There's nothing in the body. So a table with many NULLs is genuinely cheaper to store — a 30-column row with 25 NULLs costs 25 type-code bytes and no body bytes for them.


Compare Postgres, which uses a **null bitmap** in the row header: one bit per column, so 30 columns cost 4 bytes of bitmap regardless of how many are null. Both make NULL nearly free; SQLite's costs 1 byte per null column, Postgres's costs a fixed 1 bit per column whether null or not. SQLite's wins for wide sparse rows; Postgres's wins for wide dense ones. **A real, small, opposite-direction tradeoff — a nice thing to be able to describe.**


**2. Codes 8 and 9 are integers 0 and 1 with zero body bytes.** Booleans, flags, counters, and `IS_ACTIVE` columns are overwhelmingly 0 or 1. So the two most common integers in any real database were given their own type codes and cost **nothing** in the body — one varint of header and that's the entire storage cost of a boolean column.


This is a **static Huffman code**, and the spec says so explicitly: a varint is "a static Huffman encoding of 64-bit twos-complement integers that uses less space for small positive values." Huffman coding gives short representations to frequent symbols; SQLite looked at what real databases contain, found 0 and 1 dominating, and gave them zero-length representations. That's a *measurement-driven* format decision, and being able to point at it and say "they profiled real data and encoded the result into the format" is a good observation.


Note also this is gated on schema format 4+ — meaning they added it later and versioned it, because old libraries would choke. Format evolution in practice.


**3. Odd means text, even means blob — from one formula.** `(n*2)+13` is always odd; `(n*2)+12` is always even. So the low bit of the type code tells you text-or-blob, and `(N - 12) >> 1` gives the length either way. One arithmetic expression handles both, and the parity carries a whole bit of type information for free.


Why does text vs blob even matter, when both are just bytes? **Comparison.** TEXT compares using a collation (case sensitivity, locale rules); BLOB compares with `memcmp`. Same bytes, different sort order. So the distinction has to survive to the comparison function, and the parity of the type code is where it's stored.


**4. There's no dedicated boolean, no date, no decimal.** SQLite has exactly five storage classes: NULL, INTEGER, REAL, TEXT, BLOB. Everything else is a convention on top — booleans are 0/1 integers, dates are strings or Julian day numbers, decimals are usually TEXT or scaled integers.


That's a *deliberate* minimalism worth understanding rather than criticising. Every type in a storage format is a permanent commitment: an encoding, a comparison function, a set of coercion rules, and a migration path forever. Five types you can implement perfectly beat fifteen you implement approximately. The cost is real — SQLite has no proper decimal type, which is a genuine problem for money, and the standard workaround is integer pennies.


Your `roadmap.md` follows this exactly (NULL/INT/REAL/TEXT/BLOB) and cuts SQLite's type-affinity rules in favour of "simple declared-type coercion." Both are right calls.


### The detail that makes `ALTER TABLE ADD COLUMN` free


> "A record might have fewer values than the number of columns in the corresponding table. This can happen, for example, after an `ALTER TABLE ... ADD COLUMN` SQL statement has increased the number of columns in the table schema without modifying preexisting rows in the table. Missing values at the end of the record are filled in using the default value for the corresponding columns."
> — [fileformat2.html](https://www.sqlite.org/fileformat2.html)


Think about what this buys. `ALTER TABLE users ADD COLUMN country TEXT DEFAULT 'UK'` on a 100-million-row table is **an O(1) metadata change.** Not a rewrite. Old records simply have fewer values, and the reader fills the tail from column defaults.


**Add this to your format from day one**, because it costs nothing (you already know the column count from the catalog; you just decode as many values as the record has and default the rest) and it converts a catastrophic operation into an instant one. It's also a concrete, impressive thing to say: *"adding a column is O(1) in my engine, because a record may hold fewer values than the schema has columns and missing trailing values come from the column defaults."*


Postgres, notably, could *not* do this for `ADD COLUMN` with a non-null default until version 11 (2018) — before that it rewrote the entire table, which took production systems down. A format decision made in 2003 avoided a problem that a much bigger database was still fighting fifteen years later. **That's the value of getting the record format right, and it's why `roadmap.md` warns you can't refactor it later.**


### Record sort order: why a total ordering across types matters


For index keys (week 4) you need to compare records of possibly different types. SQLite defines a total order:


1. **NULL** first
2. **Numeric** (integers and floats, compared numerically — 5 and 5.0 compare equal)
3. **Text**, by the column's collation
4. **BLOB** last, by `memcmp`


Note that "numeric" spans integers and floats *as one class*, so `WHERE x > 5` behaves sanely when the column contains both 4 and 4.5. And note NULL sorts *first* — which is the answer to `roadmap.md` week 7's question about "`NULL` sorts at one specific end in `ORDER BY` — find out which." NULLs first for `ASC`. Now you know, and you know where it's specified.


The design requirement here is worth stating generally: **an index needs a total order over all possible values, including values of different types and including NULL, or else two records can be incomparable and the tree has no defined shape.** Any dynamically-typed database has to make this decision explicitly. Statically-typed ones get it from the column type.


---


## 3.9 Roads not taken


### Fixed-width everything


Every column its declared width, no varints, no type codes. Column *k* is at a computable offset.


**Wins:** blazing random access to any column; trivial code; SIMD-friendly.


**Losses:** you must bound every string; NULL needs a separate mechanism; wasted space is the gap between max and actual, on every row.


**Used by:** column stores for fixed-width columns, embedded formats, and — partially — Postgres, which uses fixed offsets *until* the first variable-length column and then has to walk.


### Postgres's approach — the instructive contrast


Postgres rows have a fixed 23-byte header (transaction IDs for MVCC, a null bitmap, flags), then values laid out with **alignment padding**: an `int32` starts on a 4-byte boundary, an `int64` on an 8-byte boundary, with padding bytes inserted as needed. Large values are moved out to a side table by **TOAST** (The Oversized-Attribute Storage Technique) — Postgres's equivalent of your overflow pages.


**Why alignment?** So the CPU can load a value with a single aligned instruction. Unaligned loads are slower on x86 and were historically *illegal* on some architectures.


**So the tradeoff is explicit:** Postgres pays space (padding, plus a 23-byte header on every row) to buy CPU efficiency. SQLite pays CPU (varint decoding, no alignment guarantees) to buy space. Neither is wrong — **they're optimising different resources because they run in different places.** Postgres runs on a server where RAM is plentiful and CPU cycles per row matter; SQLite runs on phones and embedded devices where the database file's size on flash is a headline number.


That comparison is one of the best available answers to "how would you evaluate a storage format?" The answer is *"against which resource is scarce in the deployment,"* and here are two mature systems that chose oppositely for defensible reasons.


### Protocol Buffers / MessagePack / CBOR


General-purpose self-describing binary formats with varints and type tags.


**Why not:** they're designed for *messages in transit*, not *records at rest*. Field-number tags for schema evolution cost bytes you don't need (you have a catalog). No total ordering for index comparison. No control over layout. And `roadmap.md` §2 explicitly prohibits serialization frameworks in place of the record codec — correctly, since writing the codec *is* the exercise.


Worth knowing the boundary though: for an RPC payload, reach for protobuf. For rows in a B+tree, write the codec.


### Compression (LZ4, Snappy, zstd) per page or per row


Compress a page before writing, decompress after reading.


**Wins:** genuinely large. Real database pages compress 2–4× because they're full of repeated structure, common prefixes, and zeros.


**Losses, and they're structural:** a compressed page isn't 4096 bytes, so it no longer maps to one sector (chapter 00 §0.3) — you need an indirection layer mapping page numbers to variable-length physical locations, which is another allocator and another structure to keep crash-consistent. You also can't modify part of a page in place; you must decompress, modify, recompress, and the result may not fit where it was.


**Used by:** InnoDB page compression, RocksDB block compression (much easier there, because LSM files are immutable — nothing is ever modified in place, so the whole problem evaporates).


**Notice the pattern:** compression is easy in a log-structured design and hard in an update-in-place one. That's not a coincidence; it's the same structural fact that made copy-on-write attractive in chapter 02 §2.7. **Immutability makes a lot of problems disappear, at the cost of needing compaction.**


---


## 3.10 What you're building


`roadmap.md` week 1, sessions 2 and 7:


| File | Contents |
|---|---|
| `codec/ints.py` | Big-endian u16/u32 helpers over `struct` |
| `codec/varint.py` | `encode_varint` / `decode_varint` (unsigned, 1–9 bytes), plus `to_twos_complement` / `from_twos_complement` for signed rowids |
| `codec/record.py` | `encode_record(values) -> bytes`, `decode_record(buf) -> tuple`, and ideally `decode_column(buf, k)` |


Build `decode_column(buf, k)` even though nothing needs it in week 1. It's ~10 lines given the header, and it's the thing that makes §3.7's whole argument *true* rather than theoretical — plus `Project` in week 3 and covering indexes in week 4 both want it. If you never build it, the manifest design is unexercised and you can't honestly claim the benefit.


### The format, and none of it is negotiable


This table used to be a list of decisions. It isn't any more — every row is fixed by the format you're writing, and `sqlite3` will tell you if you get one wrong. Write it into `docs/file-format.md` **before** you type, because the point of a format doc is to be the thing you check when the encoder and decoder disagree, and it's worthless if it was written by copying the code.


| Rule | Value | Why |
|---|---|---|
| Varint byte order | **Big-endian** | §3.3 — and it's why byte comparison matches numeric order, which week 4 needs |
| Max varint length | **9** — the 9th byte drops its flag and gives all 8 bits | §3.4 |
| Signed integers | **Two's complement**, width chosen by serial type. No zigzag anywhere | §3.5 — the type code already pays for the length |
| Header size varint | **Includes itself** | §3.7 — foolproof decoder, and watch the 127-byte recursion |
| Serial types | SQLite's table, including **0 and 1 as zero-byte codes 8 and 9** | §3.8 — booleans free, and it's a Huffman argument you can explain. Requires schema format 4 in the file header |
| Short records | **Allowed**, missing trailing values take column defaults | §3.8 — makes `ADD COLUMN` O(1) |
| Total order | NULL < numeric < text < blob | §3.8 — required for index keys in week 4 |
| Text encoding | **UTF-8**, and the file header must say so (offset 56 = 1) | §3.8 — TEXT length in the serial type counts *bytes*, not characters |


### Tests that matter


1. **Round-trip property (Hypothesis):** `decode(encode(n)) == n` for all `n` in `0 .. 2**64-1`. Separately, `from_twos_complement(to_twos_complement(n)) == n` across `−2**63 .. 2**63−1`.
2. **Every length boundary, hand-written:** 0, 1, 127, 128, 16383, 16384, 2097151, 2097152, … and especially **2⁵⁶−1 and 2⁵⁶**, where the ninth-byte special case starts, plus 2⁶⁴−1.
3. **Record round-trip over mixed tuples** including NULLs, empty strings, empty blobs, multi-byte UTF-8, ±2⁶³, 0.0, −0.0, and NaN if you support it. *(−0.0 and NaN are worth deciding about deliberately: does `−0.0 == 0.0` for you? What does NaN sort as? SQLite says NULLs first and treats NaN as NULL. Write down your answer.)*
4. **`decode_column(buf, k) == decode_record(buf)[k]`** for every `k`. This is the test that proves the manifest actually works.
5. **A header straddling 127 bytes** — enough columns that the header-size varint goes from 1 byte to 2. §3.7's trap.
6. **Truncated input raises a typed error**, never `IndexError` or `struct.error`. A varint with the continuation bit set at the end of the buffer; a record header claiming more bytes than exist; a text type code claiming a length past the buffer. Per `roadmap.md` §7: "never handle malformed external input with `assert`."


Test 6 is the one that gets skipped and it's the one that matters in week 8's corruption suite.


---


## 3.11 Check yourself


1. Why does encoding size matter, when the real currency is page reads? Give the chain of reasoning.
2. What do you give up by using varints, and why is it free in this context?
3. Why big-endian rather than LEB128? Give the reason that isn't "consistency."
4. Why is SQLite's max varint 9 bytes when 64/7 rounds to 10?
5. `-5` as a bare two's complement varint costs how many bytes? As a record value with a serial type? Why is the second one one byte?
6. Zigzag would fix the bare-varint case. Give the reason SQLite doesn't use it, in terms of what the serial type already pays for. Why does protobuf's answer differ?
7. Where in this format does a negative number still cost the full 9 bytes, and why is that acceptable?
8. Why can't a record's meaning depend on the catalog?
9. Why does the header's size varint include itself, and what's the encoding trap that creates?
10. Why do NULL, 0, and 1 all cost zero body bytes? What's the information-theoretic name for that idea, and which schema format is it gated on?
11. `(n*2)+13` vs `(n*2)+12` — what does the parity encode, and why does the text/blob distinction matter at all?
12. Why is `ALTER TABLE ADD COLUMN` O(1) in this format? What did Postgres have to do before v11?
13. SQLite uses varints and no alignment; Postgres uses fixed offsets with alignment padding. Which resource is each optimising, and why does the deployment explain the difference?


---


## 3.12 Sources


- [SQLite Database File Format](https://www.sqlite.org/fileformat2.html) — the varint definition including the ninth-byte rule and "static Huffman encoding" characterisation; big-endian statement; the complete serial type table including codes 8 and 9 and the `(N−12)/2` / `(N−13)/2` formulas; "the nul terminator is not stored"; header size varint "including the size varint itself"; records with fewer values than columns and default-filling; the NULL < numeric < text < BLOB sort order and the BINARY/NOCASE/RTRIM collations.
- [Protocol Buffers encoding](https://protobuf.dev/programming-guides/encoding/) — LEB128 as the little-endian counterpoint; the `int64` vs `sint64` zigzag choice.
- Postgres row layout (`HeapTupleHeader`, null bitmap, alignment, TOAST) — cited from knowledge; see the Postgres docs on "Database Page Layout" and "TOAST" to confirm. The `ADD COLUMN` improvement landed in **PostgreSQL 11** (2018).


---


**Next:** [04 — The buffer pool](04-buffer-pool.md) — caching from first principles, and why the textbook eviction policy is actively wrong for the most common query you'll run.



