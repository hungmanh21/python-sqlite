# 08 — The Catalog and Binding


> **Read in week 3 before building `catalog/` and `sql/binder.py`.**
>
> **Time:** ~40 minutes. **Prerequisite:** [chapter 07](../sql/07-from-sql-text-to-a-tree.md) for the AST, and [chapter 06](../btree/06-b-tree-mechanics.md) §6.4 for the stable-root invariant.


---


## 8.0 The problem: reopen the file tomorrow


Today the user runs:


```sql
CREATE TABLE users (id INTEGER, name TEXT, age INTEGER);
```


You allocate a B+tree root at page 4 and insert rows. Then the process exits.


Tomorrow the file is only bytes. How does a new process know:


- that a table named `users` exists?
- that its root is page 4?
- that record column 1 is `name`, not `email`?
- that column 2 was declared `INTEGER`?
- which parser rules should reinterpret the schema if the engine has been upgraded?


The data pages cannot answer those questions. A table B+tree says "rowid 7 has this record"; it does
not say what the record's columns mean. The database needs durable data *about its data*.


That data is **metadata**, and the durable collection of it is the **catalog** (or system catalog).


The catalog creates an apparent recursion: if table locations are found by consulting a table,
where is the catalog table?


SQLite ends the recursion with a convention:


> **The schema table is a table b-tree rooted at page 1.**


No lookup is needed to find it. Page 1 is known by definition; everything else becomes discoverable
from rows stored there.


```text
known constant
page 1: sqlite_schema
    │
    ├── users  → root page 4
    ├── posts  → root page 9
    └── idx_email → root page 15
```


This is **bootstrapping**: begin with the smallest hardcoded fact that lets the system discover all
remaining facts.


> **Say this out loud:** "The catalog solves database bootstrapping. Page 1 is the one hardcoded
> root; scanning its ordinary table rows tells me every other schema object's root page and DDL.
> The database is self-describing without needing a second metadata file format."


---


## 8.1 Wrong answer 1: keep a Python dictionary


```python
self.tables["users"] = TableSchema(...)
```


You need this dictionary as a cache. It makes name lookup cheap and gives the binder immutable schema
objects to work with.


It cannot be the source of truth. It disappears when the process exits. Reconstructing it from page
contents by guessing record shapes is impossible: the schema is precisely the information needed to
interpret those records.


The rule is:


> **The catalog on disk is authoritative; the dictionary is a derived cache.**


That distinction becomes critical in week 6. Multiple connections can each cache metadata, so a
schema change needs a durable version number that tells them their caches are stale (§8.6).


---


## 8.2 Wrong answer 2: put metadata in a sidecar JSON file


```text
app.db
app.db.schema.json
```


It is tempting. JSON is readable, Python parses it for free, and structured columns require no SQL
re-parsing.


It creates harder problems than it solves:


1. **Atomicity now spans two files.** A crash can persist the new B+tree root but not the JSON entry,
   or the entry but not the page. Week 5's journal would have to cover both.
2. **Files can be separated.** Copying, renaming, uploading, or backing up one without the other
   silently destroys meaning.
3. **There are two formats to version.** The database format and the catalog format can drift.
4. **SQLite cannot see it.** `sqlite3` reads `sqlite_schema`; an external JSON file makes the database
   structurally valid but semantically empty to your independent oracle.


Putting metadata inside the database means the same pager, B+tree, cache, journal, backup, and
integrity machinery protect it. **Metadata is important data; storing it through a weaker path is
backwards.**


---


## 8.3 The five columns SQLite chose


Every `sqlite_schema` row has exactly five fields:


| Column | Meaning |
|---|---|
| `type` | `table`, `index`, `view`, or `trigger` |
| `name` | schema object's name |
| `tbl_name` | table the object belongs to; same as `name` for a table |
| `rootpage` | root b-tree page, or 0 for objects with no b-tree |
| `sql` | SQL text that created the object |


A Week 3 table row looks like:


```text
("table", "users", "users", 4,
 "CREATE TABLE users (id INTEGER, name TEXT, age INTEGER)")
```


It is encoded with the same record codec as user rows and stored in the same table-cell format.
There is no catalog-only encoding. This reuse is more than aesthetic: the code path exercised by
every schema load also exercises ordinary B+tree scans, overflow handling, and record decoding.


### Why both `name` and `tbl_name`?


For a table they repeat. For an index, `name` is the index name and `tbl_name` is the table it indexes.
Views and triggers also belong to a named table context. The repetition buys one uniform row shape
for all schema objects.


### Why `rootpage` is an integer rather than a pointer object


Page numbers are the durable addresses of this storage engine (chapter 01). A process-local object
reference would be meaningless after reopen. More subtly, the number must remain stable for the
life of the table, which is why chapter 06's root-split algorithm copies the old root into a new
child and rebuilds the existing root page in place.


If a root split changed the table's root number, every height increase would also be a catalog
update. That expands a local B+tree operation into a cross-tree operation and makes crash atomicity
harder. **Stable identity removes coordination.**


---


## 8.4 Why store the original CREATE text?


Your first instinct is probably a structured record:


```text
TableSchema(name="users", columns=[("id", INTEGER), ...])
```


That is easier to load. It is already parsed and type-checked. Why does SQLite store SQL text and
parse it again every time a connection loads the schema?


### Structured metadata freezes yesterday's interpretation


Imagine engine version 1 sees:


```sql
CREATE TABLE events (created_at TIMESTAMP);
```


It converts that declaration into an internal enum and stores the enum. Five years later, version 5
has richer rules for generated columns, constraints, collations, or type affinity. It opens the file
and sees version 1's *conclusion*, not the user's declaration. Information discarded by the old
interpretation cannot be recovered.


Store the declaration text instead and the newest parser interprets the original intent using the
newest rules. The schema representation survives version skew because it remains at the language
boundary.


This is a general serialization principle:


> **When future readers may know more than current writers, preserve the source representation rather
> than only today's derived interpretation.**


The cost is a schema parse on open. Schema text is tiny compared with table data, and parsing tens or
hundreds of statements takes microseconds to milliseconds. SQLite accepts that cost for evolution
and inspectability.


### "Verbatim" has limits in SQLite, but is the right Week 3 rule


SQLite's documentation describes normalizations it may make to stored CREATE text, such as keyword
capitalization and removal of some temporary/schema qualifiers. quilldb does not need to reproduce
those transformations. Store the submitted statement text verbatim and require that re-parsing it
produces metadata consistent with the other four columns.


That consistency check matters. If the row says name `users` but the SQL says `CREATE TABLE orders`,
the file is not merely using an unsupported feature; it contradicts itself and is corrupt.


> **Say this out loud:** "SQLite stores CREATE text instead of only structured metadata so newer
> engines can reinterpret older schemas with newer language rules. It pays a tiny re-parse cost on
> open to avoid freezing the writer's old interpretation into the file format."


---


## 8.5 Creating a table: order matters before transactions exist


`CREATE TABLE` changes at least three pieces of durable state:


1. allocate a page for the new table root
2. initialize that page as an empty leaf table b-tree
3. insert the catalog row that publishes its existence


Week 5 will make these atomic. In Week 3, you still choose an order that minimizes dangerous states.


**Initialize before publishing:**


```text
allocate page 4
write valid empty b-tree to page 4
insert sqlite_schema row pointing to page 4
```


If the process fails before step 3, you have an unreachable allocated page — a leak, but no schema
entry points to garbage. If you publish first and fail before initialization, every future open finds
a table whose root is invalid. One order degrades to lost space; the other degrades to corruption.


This is not atomicity. It does not make the operation safe against every crash. It is **failure-
aware ordering**, the same habit chapter 00 develops before journaling is available.


### Do not update the cache first


The in-memory dictionary is the final step. If you cache the table before the catalog insert
succeeds, the current connection can use a table that no future connection can discover.


```text
durable objects first → authoritative catalog row → derived cache last
```


### What about the leaked page?


Without transactions, cleanup after a failed catalog insert is best-effort: return the allocated
page to the freelist if doing so is safe. But do not let cleanup failure hide the original exception.
Week 5's rollback journal is the real answer; Week 3's ordering simply makes the temporary gap
honest and bounded.


---


## 8.6 The schema cookie: cache invalidation made durable


The file header contains a **schema cookie** (often called the schema version): a 32-bit integer that
changes whenever the schema changes.


Why is it needed if every connection can scan `sqlite_schema`?


Because scanning and parsing the catalog before every statement wastes work. A connection wants to
cache the result:


```text
cached cookie = 7
cached tables = {users, posts}
```


Before reusing it, compare the current file cookie:


```text
file cookie still 7 → cache is usable
file cookie now 8   → discard and reload
```


This is version-based invalidation. The version does not describe *what* changed. It answers the
only question the cache needs: "could my derived state be stale?"


Week 3 has one connection and could survive without it. Bump it anyway when `CREATE TABLE` succeeds,
because the on-disk format requires schema changes to announce themselves and Week 6's multiple
connections will need the signal.


The schema cookie illustrates a recurring database pattern:


> **Derived state may be cached freely if authoritative state carries a cheap generation number.**


You will see the same pattern in plan-cache invalidation and optimistic concurrency systems.


---


## 8.7 Parsing is not binding


Chapter 07's parser accepts:


```sql
SELECT nmae FROM users WHERE age > ?;
```


It is grammatically valid. `nmae` has the shape of an identifier. The parser cannot know it is a
misspelling because the parser should work without an open database.


The **binder** combines syntax with catalog facts:


```text
AST                              bound tree
Column("name")          →        BoundColumn(index=1, type=TEXT)
Column("age")           →        BoundColumn(index=2, type=INTEGER)
Parameter(0)            →        BoundLiteral(30)
table="users"           →        TableSchema(root_page=4, ...)
```


After binding, names are gone from the hot path. The executor reads `row[2]`; it does not search a
column-name list for every row.


For a million-row scan, resolving `age` by string comparison a million times is wasteful. More
importantly, delaying resolution means an unknown column might be discovered after the scan has
opened or a mutation has begun. Binding makes semantic failure happen before execution touches data.


### The binder's jobs


1. **Resolve tables.** `users` becomes a `TableSchema` with a root page.
2. **Resolve columns.** Names become zero-based slots and declared types.
3. **Expand `*`.** `SELECT *` becomes an explicit tuple of every bound column.
4. **Bind parameters.** Each `?` becomes the corresponding supplied value.
5. **Check arity.** `INSERT` supplies exactly one value per column.
6. **Check the supported type policy.** Reject values quilldb cannot store under the declared type.


This phase is sometimes called **semantic analysis**. Compilers do the same thing: parsing decides
that `x + 1` is shaped correctly; name resolution decides which `x` it means; type checking decides
whether addition is legal.


> **Say this out loud:** "Parsing proves grammatical structure without consulting the database.
> Binding combines the AST with the catalog, turns table names into root pages and column names into
> integer slots, substitutes parameters, and rejects semantic errors before any operator opens."


---


## 8.8 Parameters: data must not become syntax


Consider the unsafe construction:


```python
sql = f"SELECT * FROM users WHERE name = '{user_input}'"
```


If `user_input` contains a quote, it changes the token stream and therefore the syntax tree. This is
SQL injection in its most basic form: untrusted data is allowed to become program text.


A parameter marker prevents that category error:


```python
db.execute("SELECT * FROM users WHERE name = ?", (user_input,))
```


The parser sees a `Parameter(0)` node regardless of the value. The binder replaces that node with a
literal value *after parsing is complete*. A string containing quotes remains one string value; it
is never tokenized as SQL.


The security property comes from phase separation, not escaping cleverness:


```text
SQL text → parse fixed syntax
parameters → bind data into existing holes
```


Too few parameters leave holes. Too many usually indicate a caller bug. Reject both with
`ParameterCountError`; silently ignoring extras hides mismatched application code.


---


## 8.9 Declared types: SQLite compatibility versus a teachable subset


SQLite uses dynamic typing and **type affinity**. A column declaration influences conversion and
comparison but generally does not forbid storing another storage class. Reproducing all affinity
rules is explicitly outside quilldb's scope.


Week 3 chooses a stricter surface:


```text
INTEGER → int
REAL    → int or float, stored as float
TEXT    → str
BLOB    → bytes
NULL    → accepted everywhere
```


This is an implementation policy, not an on-disk format change. The record codec still writes
SQLite serial types. SQLite can read the rows; quilldb simply rejects some writes SQLite would
coerce or accept.


Why enforce anything? Because the binder then catches application mistakes at a clear boundary:


```python
db.execute("INSERT INTO users VALUES (?, ?)", ("not-an-int", "ada"))
```


The honest project description is: "SQLite-compatible file encoding with a deliberately narrower,
stricter SQL type policy." Do not say "SQLite type semantics" until affinity and comparison
coercions are actually implemented.


### Why `bool` needs an explicit decision in Python


`bool` is a subclass of `int`:


```python
isinstance(True, int)  # True
```


If the API claims INTEGER accepts Python integers, decide whether booleans count. The Week 3 spec
rejects them to avoid an accidental language-lawyer result. An explicit `type(value) is int` is more
honest than inheriting Python's historical choice unintentionally.


---


## 8.10 The road not taken


### A binary structured catalog


Faster to load and easier to validate locally. Many systems use structured system tables. The cost
for this project is losing SQLite schema interoperability and freezing language interpretation at
write time. Because exact SQLite bytes and `sqlite3` validation are project goals, this road is
closed by a higher-level constraint.


### Store the catalog at page 2


It makes page 1 simpler and was suggested in older drafts of this project's guide. It is not the
SQLite format. Page 1's awkward triple role — file header, b-tree page, schema root — is already
handled by `page_header_offset()`. Moving the catalog would trade one special offset for permanent
format incompatibility.


### Resolve names during parsing


It appears to save a phase. It makes the parser require an open database, prevents parser-only unit
tests, entangles syntax errors with catalog errors, and makes parsed statements impossible to cache
or rebind. Compilers separate parsing and semantic analysis for the same reasons databases do.


### Full SQLite affinity now


More compatible, much more surface area: declared-type-to-affinity rules, conversions before insert,
numeric/text comparison rules, collation, and edge cases. It adds hours without strengthening the
Week 3 vertical-slice story. Document the stricter subset and expand only when a later feature needs
it.


---


## 8.11 What you're building


| Component | Persistent input | Derived output |
|---|---|---|
| `Catalog.load()` | page-1 `sqlite_schema` rows | immutable `TableSchema` cache |
| `Catalog.create_table()` | `CreateTable` AST + original SQL | table root + catalog row + cookie bump |
| `bind()` | AST + schema cache + parameters | bound statement with root pages, slots, and values |


The invariants worth writing next to the code:


- catalog key comparison is case-insensitive for unquoted names
- original spelling and original CREATE text are preserved
- every table catalog row has `type == "table"`, `name == tbl_name`, and a valid nonzero root page
- reparsing `sql` yields a `CreateTable` agreeing with the row
- no unresolved `Column` or `Parameter` reaches execution
- binding failure opens no cursor and modifies no page


These are more useful than broad "catalog works" tests because each names the layer responsible for
a future failure.


---


## 8.12 Check yourself


1. What bootstrapping problem does page 1 solve?
2. Why is the in-memory schema dictionary a cache rather than the source of truth?
3. Name four failure modes introduced by a sidecar schema file.
4. List the five `sqlite_schema` columns and explain why `name` and `tbl_name` both exist.
5. Why must a table root page number remain stable across root splits?
6. What does storing CREATE text preserve that storing only today's parsed structure loses?
7. In what order should `CREATE TABLE` initialize the root, publish the catalog row, and update the cache? Why?
8. What does the schema cookie tell a connection, and what does it deliberately not tell it?
9. Give one query that parses successfully but fails during binding.
10. Why do column names become integer indexes before execution?
11. Explain parameter safety without using the phrase "escape the string."
12. How does quilldb's Week 3 type policy differ from SQLite affinity, and why is that not a file-format divergence?


If 10 of 12 are fluent, build the catalog and binder.


---


## 8.13 Sources


- [SQLite Database File Format — Storage of the SQL database schema](https://www.sqlite.org/fileformat2.html#storage_of_the_sql_database_schema) — page 1 as the schema root, the five columns, rootpage meaning, and stored SQL normalization.
- [SQLite `CREATE TABLE`](https://www.sqlite.org/lang_createtable.html) — table declaration syntax and the relationship between declared types and SQLite behavior.
- [Datatypes In SQLite](https://www.sqlite.org/datatype3.html) — storage classes, dynamic typing, and type affinity; the baseline quilldb deliberately narrows.
- [SQLite PRAGMA `schema_version`](https://www.sqlite.org/pragma.html#pragma_schema_version) — the schema-cookie field and prepared-statement invalidation role.
- [SQLite Architecture](https://www.sqlite.org/arch.html) — parser/code-generator boundaries and schema loading in the broader compilation pipeline.
- [SQLite C API `sqlite3_prepare`](https://www.sqlite.org/c3ref/prepare.html) — preparation as the point where SQL text is compiled against a schema before stepping execution.


---


**Next:** [09 — Iterator execution](../exec/09-iterator-execution.md) — how a tree of tiny `next()` methods becomes a streaming query engine, and why SQL NULL cannot use Python's boolean rules unchanged.