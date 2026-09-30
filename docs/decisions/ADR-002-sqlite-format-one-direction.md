# ADR-002: Write SQLite's exact on-disk format, but only in one direction


## Status


Accepted (decided in week 1; recorded in week 8).


## Context


The project needed an acceptance test that quilldb could not grade itself on. A format "inspired by"
SQLite would be tested only against quilldb's own reader, so any misunderstanding of the format would be
invisible. Matching SQLite's real format makes the reference implementation the judge:
`sqlite3 file.db "PRAGMA integrity_check"` must print `ok`.

There are two ways to claim interoperability, and they differ enormously in cost. *Writing* files that
`sqlite3` can read needs the structures quilldb builds anyway (slotted pages, varints, a record codec,
overflow chains, a freelist, a 100-byte header) with SQLite's constants. *Reading* arbitrary files that
`sqlite3` wrote needs freeblock parsing, the rollback-journal byte format and hot-journal detection, WAL,
auto-vacuum pointer maps, other page sizes, UTF-16, old schema formats and exact type affinity.


## Decision


quilldb writes SQLite's format byte for byte, and **does not promise to read files it did not write.**
Where a real SQLite file uses a feature quilldb does not implement, `FileHeader.check_supported()`
refuses it (`InvalidHeaderError`) instead of misreading it: page sizes other than 4096, text encodings
other than UTF-8, WAL, non-zero reserved space per page, auto-vacuum / incremental vacuum.

The rule that sorts everything else: **anything that touches on-disk bytes of the database file must
match the real format exactly; anything that does not is free to differ.** The journal is the clearest
case. Its algorithm is copied from SQLite (commit point, fsync order, the two born-invalid guards, the
checksum), but its byte layout is quilldb's own, because `sqlite3` never has to read a quilldb journal.


## Consequences


- The acceptance test is external and objective. `src/tests/differential/` (294 tests) runs the same SQL
  through quilldb and real `sqlite3` and diffs the results, and the README example ends by asking the
  standard library's `sqlite3` to check the file it wrote; CI runs that example.
- A bug in the format is caught even when quilldb is self-consistent. Two of the bugs in `NOTES.md`
  (B4-2 `wrong # of entries in index`, B4-4 a zero-cell page that `sqlite3` rejects as malformed) were
  found because a real SQLite binary was reading the output.
- quilldb cannot open a database created by `sqlite3` in general. Opening one that uses an unsupported
  feature fails loudly at the header check; opening one that happens not to use any may work, but that is
  not a supported use.
- Format fidelity and feature completeness are different claims. A missing feature (WAL, `VACUUM`) is a
  gap; a byte that differs from the spec is a defect. Nothing in this project trades the second for the
  first.


## Alternatives considered


- **A format of quilldb's own design.** Far less to get wrong, and it would have allowed simpler choices
  (variable page sizes, no varints). It also removes the only external check on the storage layer, and
  removes the reason anyone would find the project interesting.
- **Full bidirectional interop.** The expensive half, estimated at 50 to 60 hours in the roadmap, mostly
  spent on features that add no new talking points. Cut, with the refusal in `check_supported()` making
  the boundary explicit.
- **Reading `sqlite3` files opportunistically, without refusing.** Rejected because it turns a missing
  feature into silent misreads (for example, interpreting an auto-vacuum pointer-map page as a b-tree
  page), which is the worst failure mode a storage engine has.
