"""Pull-based relational operators for the Week 3 vertical slice.


The iterator (Volcano) model: every operator answers next() with one row or
None, pulling from its child. Nothing materialises a result set, so a
million-row scan through a Filter costs one row of memory -- and the same
next() protocol works whether the child is a b-tree scan or, in Week 4, an
index scan.


RESOURCES, NOT JUST ROWS. This is the layer that owns pins, so the
discipline is explicit and uniform:


    - close() is idempotent. It gets called on the success path, from an
      exception handler, and possibly again by a `with` block; all three
      must be safe.
    - An operator closes ITSELF AND ITS CHILD when next() raises. A
      TypeMismatchError from evaluate() on row 40,000 must not strand the
      TableCursor's pins -- a leaked pin is worse than a crash, because the
      buffer pool then refuses to evict that page for the rest of the
      process (PoolExhaustedError somewhere unrelated, much later).


All the value semantics live in exec/expressions.py; Filter and Project are
deliberately dull because everything subtle about NULL already happened
there.
"""


from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from typing import Self

from quilldb.btree.btree import BTree
from quilldb.btree.cursor import TableCursor
from quilldb.btree.index import IndexBTree, compare_keys
from quilldb.catalog.catalog import Catalog
from quilldb.catalog.schema import IndexSchema, TableSchema
from quilldb.codec.record import Value, decode_record, encode_record
from quilldb.errors import PageFullError, UniqueViolationError
from quilldb.exec.expressions import Row, evaluate, where_passes
from quilldb.plan.analyze import StatisticsCatalog
from quilldb.plan.cost import assign_cost
from quilldb.plan.planner import AccessPath, enumerate_access_paths
from quilldb.plan.predicates import Predicate, classify_predicate, extract_conjuncts
from quilldb.plan.search import choose_access_path
from quilldb.plan.statistics import default_index_stats, default_table_stats, estimate_row_counts
from quilldb.sql.binder import (
    BoundAssignment,
    BoundBinaryOp,
    BoundDelete,
    BoundExpression,
    BoundInsert,
    BoundLiteral,
    BoundSelect,
    BoundUpdate,
)
from quilldb.storage.bufferpool import BufferPool
from quilldb.storage.pager import Pager


class Operator(ABC):
    rows_affected: int = 0  # meaningful only for Insert/Delete/Update; SELECT streams instead


    @abstractmethod
    def open(self) -> None:
        """Acquire whatever this operator needs to produce rows.


        Calling open() on an already-open operator resets it: any resources
        from the previous open are released first, so re-opening can't leak.
        """


    @abstractmethod
    def next(self) -> Row | None:
        """Return one row, or None when exhausted.


        Once None has been returned, further calls keep returning None.
        On any exception, this operator and its child are closed before the
        exception propagates.
        """


    @abstractmethod
    def close(self) -> None:
        """Release every resource held, including the child's. Idempotent."""


    @abstractmethod
    def explain(self, depth: int = 0, verbose: bool = False) -> str:
        """A one-operator-per-line plan tree, deepest last.


        `verbose=True` is EXPLAIN's own request (stage-5 Step 7) for the
        cost/row-estimate annotations chapter 12 §12.6 shows on an
        IndexScan line -- every other operator's plain label is unaffected,
        so verbose is a no-op everywhere except IndexScan.explain(). Kept
        False by default so every operator's ordinary `explain()` (used for
        debugging and by every existing test predating EXPLAIN) keeps
        producing today's terse text unchanged.
        """


    def __enter__(self) -> Self:
        self.open()
        return self


    def __exit__(self, *exc_info: object) -> None:
        self.close()




def _explain_line(depth: int, label: str) -> str:
    if depth == 0:
        return label
    return "   " * (depth - 1) + f"└─ {label}"




class SeqScan(Operator):
    """Stream decoded records from a table's TableCursor in rowid order."""


    def __init__(self, pager: Pager, pool: BufferPool, table: TableSchema) -> None:
        self.pager = pager
        self.pool = pool
        self.table = table
        self._cursor: TableCursor | None = None
        self._positioned = False


    def open(self) -> None:
        self.close()  # re-opening resets rather than leaking the old cursor
        cursor = TableCursor(self.pager, self.pool, self.table.root_page)
        cursor.first()
        self._cursor = cursor
        # An empty table leaves the cursor unpositioned -- next() must then
        # return None rather than raising, so `SELECT` on an empty table
        # yields [] instead of an error.
        self._positioned = cursor.valid


    def next(self) -> Row | None:
        if self._cursor is None or not self._positioned:
            return None
        try:
            # record() reassembles an overflow chain if the payload spilled,
            # so a 10KB value scans the same as a 10-byte one from here.
            row = decode_record(self._cursor.record())
            if not self._cursor.next():
                self._positioned = False
        except Exception:
            self.close()
            raise
        return row


    def close(self) -> None:
        if self._cursor is not None:
            self._cursor.close()  # unpins every page still on the cursor's path
            self._cursor = None
        self._positioned = False


    def explain(self, depth: int = 0, verbose: bool = False) -> str:
        return _explain_line(depth, f"SeqScan {self.table.name}")




def _literal_value(predicate: Predicate) -> Value:
    """A seek_term's literal value -- always a BoundLiteral once a Predicate
    is sargable (classify_predicate's contract, plan/predicates.py), so this
    narrows what mypy still sees as the general BoundExpression union.
    """
    assert isinstance(predicate.value, BoundLiteral), (
        f"seek_term {predicate.column!r} has a non-literal value: {type(predicate.value).__name__}"
    )
    return predicate.value.value




def _literal_sql(value: Value) -> str:
    """Render a Value as SQL literal text for EXPLAIN's predicate display
    (e.g. `email = 'a@b.c'`, chapter 12 §12.6's own format) -- quoting and
    doubling embedded quotes the way SQL text literals require, rather
    than Python's `repr()` (right for a simple string, wrong the moment a
    value contains a `'`).
    """
    if value is None:
        return "NULL"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return str(value)




class IndexScan(Operator):
    """Stream rows by seeking an index, then point-looking-up each matching
    rowid in the table.


    `IndexBTree.seek_eq`/`seek_range` yield rowids only -- an index entry's
    payload is (key values, rowid), never the table's other columns. So
    every IndexScan does two lookups per row: seek the index for the rowids
    that satisfy `path.seek_terms`, then `TableCursor.seek(rowid)` to fetch
    the actual row. That two-step shape is why `assign_cost` (plan/cost.py)
    prices every IndexScan with a per-row `RANDOM_PAGE_COST * table height`
    term -- there is no covering-index case yet where that second lookup
    could be skipped.


    `path.residual` is NOT applied here -- it becomes a Filter above this
    operator in build_operator(), same as SeqScan never filters its own
    output. This operator's only job is "produce exactly the rows this
    index seek matches," nothing more.
    """


    def __init__(self, pager: Pager, pool: BufferPool, table: TableSchema, path: AccessPath) -> None:
        self.pager = pager
        self.pool = pool
        self.table = table
        self.path = path
        self._rowids: Iterator[int] | None = None
        self._cursor: TableCursor | None = None


    def open(self) -> None:
        """Position this scan at the start of its rowid stream.


        Build the IndexBTree for `self.path.index` (same constructor shape
        Insert/Delete/Update already use elsewhere in this file:
        `IndexBTree(self.pager, self.pool, index.root_page,
        n_key_columns=len(index.columns), unique=index.unique)`), then call
        either `seek_eq` or `seek_range` on it, and store the resulting
        rowid iterator on `self._rowids`.


        Which method to call depends on the SHAPE of `self.path.seek_terms`
        (chapter 12 §12.3, the same leading-column rule `_match_index_prefix`
        in plan/planner.py already used to build these seek_terms):


          - If every seek_term is an equality (`operator in ("=", "IS")`),
            the seek is a single POINT: call `seek_eq(values)` where `values`
            is the seek_terms' literal values, in index-column order. (A
            future `IN`-as-equality case would mean more than one point --
            not reachable yet, since nothing upstream produces two equality
            predicates on the same column today.)
          - If the LAST seek_term is an inequality (`<`, `<=`, `>`, `>=`) --
            the sandwich case -- everything before it is still a leading
            equality prefix. Call `seek_range(low, high, low_inclusive=...,
            high_inclusive=...)` where `low`/`high` are each the leading
            equality values PLUS that column's lower/upper bound (only
            whichever bounds are actually present -- `low`/`high` each
            default to None for "unbounded" on that side). `compare_keys`
            (btree/index.py) already handles a probe shorter than a stored
            key by comparing only the shared prefix, so a `low`/`high` probe
            that's just the equality prefix plus one bound is exactly what
            seek_range expects.


        A literal value comes out of a `Predicate` via `predicate.value` --
        always a `BoundLiteral` once this predicate reached here, so
        `predicate.value.value` is the actual Value to pass to IndexBTree.


        This operator does its own per-row table lookups in next() through
        a single reused TableCursor -- build it here as
        `self._cursor = TableCursor(self.pager, self.pool, self.table.root_page)`,
        matching how SeqScan.open() builds its cursor. Re-opening must reset
        rather than leak, same rule as SeqScan.open().
        """
        index = self.path.index
        self.close()  # re-opening resets rather than leaking the old cursor
        if not index:
            return
        btree = IndexBTree(self.pager, self.pool, index.root_page, n_key_columns=len(index.columns), unique=index.unique)
        self._cursor = TableCursor(self.pager, self.pool, self.table.root_page)


        # check if seek terms is all equal comparison
        all_eq = True


        for predicate in self.path.seek_terms:
            if predicate.operator not in ["=", "IS"]:
                all_eq = False


        if all_eq:
            # get all the predicates
            values = [_literal_value(predicate) for predicate in self.path.seek_terms]
            self._rowids = btree.seek_eq(values)
        else:
            equality_prefix = [_literal_value(predicate) for predicate in self.path.seek_terms if predicate.operator in ["=", "IS"]]
            low_bound: list[Value] = []
            high_bound: list[Value] = []
            low_inclusive = high_inclusive = True


            for predicate in self.path.seek_terms:
                value = _literal_value(predicate)
                if predicate.operator in ["<", "<="]:
                    # keep the TIGHTEST (smallest) upper bound seen -- two
                    # same-direction inequalities on one column (e.g. a
                    # redundant `age<10 AND age<7`) must narrow, not just
                    # take whichever appears last in seek_terms.
                    if not high_bound or compare_keys([value], high_bound) < 0:
                        high_bound = [value]
                        high_inclusive = predicate.operator == "<="
                    elif compare_keys([value], high_bound) == 0 and predicate.operator == "<":
                        high_inclusive = False
                elif predicate.operator in [">", ">="]:
                    # keep the TIGHTEST (largest) lower bound seen, same
                    # reasoning as above but for the other direction.
                    if not low_bound or compare_keys([value], low_bound) > 0:
                        low_bound = [value]
                        low_inclusive = predicate.operator == ">="
                    elif compare_keys([value], low_bound) == 0 and predicate.operator == ">":
                        low_inclusive = False


            # An equality prefix confines the seek to that prefix even with
            # no explicit inequality on this side -- None here would mean
            # "fully unbounded", which spills past the prefix into the next
            # distinct value of the leading column(s) (e.g. name='ada' AND
            # age>20 must not also match name='bob').
            lower_bounds = equality_prefix + low_bound if (equality_prefix or low_bound) else None
            higher_bounds = equality_prefix + high_bound if (equality_prefix or high_bound) else None
            self._rowids = btree.seek_range(
                low=lower_bounds,
                high=higher_bounds,
                low_inclusive=low_inclusive,
                high_inclusive=high_inclusive
            )




    def next(self) -> Row | None:
        if self._rowids is None or self._cursor is None:
            return None
        try:
            for rowid in self._rowids:
                # seek() closes and repositions the SAME cursor -- each
                # rowid is an independent point lookup, so there is nothing
                # to carry over between them the way SeqScan carries a
                # standing position forward.
                found = self._cursor.seek(rowid)
                if not found:
                    # The index entry exists but the table row is gone --
                    # can't happen within one statement (no concurrent
                    # writers yet), but skipping rather than raising keeps
                    # this scan's own resource discipline self-contained.
                    continue
                return decode_record(self._cursor.record())
            self._rowids = None
            return None
        except Exception:
            self.close()
            raise


    def close(self) -> None:
        if self._cursor is not None:
            self._cursor.close()
            self._cursor = None
        self._rowids = None


    def explain(self, depth: int = 0, verbose: bool = False) -> str:
        index_name = self.path.index.name if self.path.index is not None else "?"
        label = f"IndexScan {index_name}"
        if not verbose:
            return _explain_line(depth, label)


        if self.path.seek_terms:
            predicate_text = " AND ".join(
                f"{p.column} {p.operator} {_literal_sql(_literal_value(p))}" for p in self.path.seek_terms
            )
            label += f" ({predicate_text})"


        label += f" est_rows={self.path.est_rows}"
        if self.path.cost is not None:
            label += f" startup={self.path.cost.startup:.2f} cost={self.path.cost.total:.2f}"
        return _explain_line(depth, label)




class Filter(Operator):
    """Discard child rows unless `where_passes(evaluate(predicate, row))`."""


    def __init__(self, child: Operator, predicate: BoundExpression) -> None:
        self.child = child
        self.predicate = predicate


    def open(self) -> None:
        self.child.open()


    def next(self) -> Row | None:
        try:
            while True:
                row = self.child.next()
                if row is None:
                    return None
                # NULL and FALSE both reject -- see where_passes' docstring
                # for why that makes a predicate and its negation both drop
                # a NULL row.
                if where_passes(evaluate(self.predicate, row)):
                    return row
        except Exception:
            self.close()
            raise


    def close(self) -> None:
        self.child.close()


    def explain(self, depth: int = 0, verbose: bool = False) -> str:
        return _explain_line(depth, "Filter") + "\n" + self.child.explain(depth + 1, verbose)




class Project(Operator):
    """Evaluate output expressions for every child row."""


    def __init__(self, child: Operator, expressions: tuple[BoundExpression, ...]) -> None:
        self.child = child
        self.expressions = expressions


    def open(self) -> None:
        self.child.open()


    def next(self) -> Row | None:
        try:
            row = self.child.next()
            if row is None:
                return None
            return tuple(evaluate(expression, row) for expression in self.expressions)
        except Exception:
            self.close()
            raise


    def close(self) -> None:
        self.child.close()


    def explain(self, depth: int = 0, verbose: bool = False) -> str:
        return _explain_line(depth, "Project") + "\n" + self.child.explain(depth + 1, verbose)




class Insert(Operator):
    """Insert one encoded row on the first next(); then return None forever.


    Returns no rows at all -- an INSERT has no result set, so next() answers
    None even on the call that does the write. The API layer reports the
    row count instead (Cursor.rowcount).


    `indexes` is every index on this table (Catalog.indexes_for(), resolved
    once by build_operator() -- there's no per-row loop here to hoist it
    out of, since one Insert instance ever writes exactly one row). Every
    UNIQUE index among them is checked for a conflict before the table row
    -- or anything else -- is written, mirroring create_index()'s backfill
    (week-4 doc rule 2: "a UNIQUE violation raises before any page is
    written"). The table row is written before any index entry: absent
    transactions until week 5, a row that exists but is momentarily
    missing from an index is recoverable (DROP INDEX and recreate); an
    index entry pointing at a rowid that was never actually written would
    not be.
    """


    def __init__(
        self,
        pager: Pager,
        pool: BufferPool,
        statement: BoundInsert,
        indexes: Sequence[IndexSchema] = (),
    ) -> None:
        self.pager = pager
        self.pool = pool
        self.statement = statement
        self.indexes = indexes
        self._attempted = False


    def open(self) -> None:
        self._attempted = False


    def next(self) -> Row | None:
        if self._attempted:
            return None
        # Set BEFORE writing, not after: if BTree.insert() raises partway
        # through a split, a caller who keeps calling next() must not get a
        # second attempt at the same row.
        self._attempted = True


        table = self.statement.table
        values = self.statement.values
        keyed_indexes = [
            (index, [values[table.column_index(column)] for column in index.columns])
            for index in self.indexes
        ]


        for index, key in keyed_indexes:
            if not index.unique:
                continue
            probe = IndexBTree(self.pager, self.pool, index.root_page, n_key_columns=len(index.columns), unique=True)
            if probe.find_conflict(key) is not None:
                raise UniqueViolationError(index.name, tuple(key))


        rowid = self._next_rowid()
        payload = encode_record(values)
        BTree(self.pager, self.pool, table.root_page).insert(rowid, payload)


        for index, key in keyed_indexes:
            ibt = IndexBTree(
                self.pager, self.pool, index.root_page, n_key_columns=len(index.columns), unique=index.unique
            )
            ibt.insert(key, rowid)


        return None


    def _next_rowid(self) -> int:
        """1 for an empty table, otherwise the largest rowid plus one.


        The implicit rowid is deliberately NOT part of any row this week --
        it's the b-tree key, not a column, and SQL has no way to name it yet.
        """
        with TableCursor(self.pager, self.pool, self.statement.table.root_page) as cursor:
            cursor.last()
            return cursor.rowid() + 1 if cursor.valid else 1


    def close(self) -> None:
        # Nothing held between calls: _next_rowid()'s cursor is closed by its
        # own `with`, and BTree.insert()/IndexBTree.insert() unpin everything
        # they touch.
        pass


    def explain(self, depth: int = 0, verbose: bool = False) -> str:
        return _explain_line(depth, f"Insert {self.statement.table.name}")




def _scan_matching_rows(
    pager: Pager, pool: BufferPool, table: TableSchema, predicate: BoundExpression | None
) -> list[tuple[int, Row]]:
    """Every (rowid, row) pair passing `predicate`, fully materialized.


    Delete and Update both need this instead of composing through SeqScan:
    they mutate the same b-tree they are scanning, and TableCursor makes no
    promise about staying valid across a mutation to its own tree
    (btree/cursor.py's documented policy) -- so every match has to be
    collected before the first write happens, not discovered one row at a
    time while writes to that same tree are already underway.
    """
    matches: list[tuple[int, Row]] = []
    with TableCursor(pager, pool, table.root_page) as cursor:
        cursor.first()
        positioned = cursor.valid
        while positioned:
            row = decode_record(cursor.record())
            if predicate is None or where_passes(evaluate(predicate, row)):
                matches.append((cursor.rowid(), row))
            positioned = cursor.next()
    return matches




class Delete(Operator):
    """Delete every row matching an optional predicate, and every index
    entry it owned.


    Every match comes from _scan_matching_rows() -- collected before this
    operator mutates anything, for the reason given in that function's
    docstring. Index entries for a row are removed before the row itself:
    absent transactions until week 5, "row exists, stale index entry" is
    recoverable (DROP INDEX and recreate); "index entry pointing at a
    rowid that no longer exists" is not. Same ordering rule as Insert's
    UNIQUE-check-then-table-row-then-index-entries, read back to front.
    """


    def __init__(
        self,
        pager: Pager,
        pool: BufferPool,
        table: TableSchema,
        predicate: BoundExpression | None,
        indexes: Sequence[IndexSchema] = (),
    ) -> None:
        self.pager = pager
        self.pool = pool
        self.table = table
        self.predicate = predicate
        self.indexes = indexes
        self._attempted = False


    def open(self) -> None:
        self._attempted = False
        self.rows_affected = 0


    def next(self) -> Row | None:
        if self._attempted:
            return None
        self._attempted = True


        matches = _scan_matching_rows(self.pager, self.pool, self.table, self.predicate)
        for rowid, values in matches:
            for index in self.indexes:
                key = [values[self.table.column_index(column)] for column in index.columns]
                IndexBTree(
                    self.pager, self.pool, index.root_page, n_key_columns=len(index.columns), unique=index.unique
                ).delete(key, rowid)
            BTree(self.pager, self.pool, self.table.root_page).delete(rowid)


        self.rows_affected = len(matches)
        return None


    def close(self) -> None:
        pass


    def explain(self, depth: int = 0, verbose: bool = False) -> str:
        return _explain_line(depth, f"Delete {self.table.name}")




class Update(Operator):
    """Apply assignments to every row matching an optional predicate, and
    keep every index on the table current.


    Same collect-before-mutating shape as Delete, for the same TableCursor
    reason. There is no in-place "rewrite this rowid's payload" primitive
    on BTree -- only insert() and delete() -- so a row is updated as
    delete(rowid) followed by insert(rowid, new_payload): same rowid, new
    bytes. Matches are applied one at a time, each one fully finished --
    table row AND every index entry -- before the next one starts; see
    _apply()'s docstring for why that ordering matters once a UNIQUE index
    is involved.
    """


    def __init__(
        self,
        pager: Pager,
        pool: BufferPool,
        table: TableSchema,
        assignments: tuple[BoundAssignment, ...],
        predicate: BoundExpression | None,
        indexes: Sequence[IndexSchema] = (),
    ) -> None:
        self.pager = pager
        self.pool = pool
        self.table = table
        self.assignments = assignments
        self.predicate = predicate
        self.indexes = indexes
        self._attempted = False


    def open(self) -> None:
        self._attempted = False
        self.rows_affected = 0


    def next(self) -> Row | None:
        if self._attempted:
            return None
        self._attempted = True


        matches = _scan_matching_rows(self.pager, self.pool, self.table, self.predicate)
        for rowid, old_values in matches:
            self._apply(rowid, old_values)


        self.rows_affected = len(matches)
        return None


    def _apply(self, rowid: int, old_values: Row) -> None:
        """Apply this row's assignments and keep every index on the table
        current -- or raise UniqueViolationError before touching anything.


        `old_values` is the row exactly as it was before this UPDATE
        reached it. Every assignment's expression must be evaluate()d
        against `old_values`, never against a value another assignment on
        this same row just computed -- that is what makes
        `SET a = b, b = a` a swap instead of clobbering `b` with the new
        `a` before `b`'s own assignment gets to read the old one.


        Steps, in order -- stitching together Insert's "check everything
        before writing anything" rule with Delete's "index entries before
        the table row" rule:


        1. Build `new_values`: a mutable copy of `old_values`, with each
           assignment's evaluate()d result written to its column_index.
        2. For every UNIQUE index on the table, compute the OLD key and the
           NEW key (index.columns -> self.table.column_index() -> values).
           If they're equal, this index has nothing at stake for this row
           -- skip it. Otherwise probe with an IndexBTree built the same
           way Insert.next() builds one, via find_conflict(new_key). A
           conflict is only real if the conflicting rowid is NOT this row's
           own rowid: find_conflict has no way to know this row's old entry
           is about to be replaced, so the caller has to exclude self. On a
           real conflict, raise UniqueViolationError(index.name,
           tuple(new_key)) before step 3 runs for ANY index.
        3. Now that every index has passed its check: for every index
           whose key actually changed, delete its OLD (key, rowid) entry.
        4. BTree.delete(rowid), then BTree.insert(rowid,
           encode_record(new_values)) -- same rowid, new payload. If that
           insert raises PageFullError (the same rare "needs a three-way
           split" case already accepted for a plain INSERT -- see
           test_btree_splits.py's test for that), the row must not stay
           deleted (docs/theory/btree/10-deletion-and-space-reuse.md
           §10.5 rule 3): delete() only ever frees space, never costs it,
           so re-inserting the OLD payload for this same rowid is
           guaranteed to fit where it fit before, and restores the table
           row before re-raising. Index entries already deleted in step 3
           are NOT restored -- a row with a stale/missing index entry is
           the same "recoverable via DROP INDEX/CREATE INDEX" gap Insert
           and Delete already live with.
        5. For every index whose key changed, insert its NEW (key, rowid)
           entry.


        Only steps 3-5 touch a page. Checking every UNIQUE index (step 2)
        before mutating any of them is what stops a conflict on this row
        from leaving one index updated and another not.
        """
        new_values = list(old_values)
        for assignment in self.assignments:
            new_values[assignment.column_index] = evaluate(assignment.value, old_values)
       
        for index in self.indexes:
            if not index.unique:
                continue


            old_key = [old_values[self.table.column_index(column)] for column in index.columns]
            new_key = [new_values[self.table.column_index(column)] for column in index.columns]


            if old_key == new_key:
                continue


            tree = IndexBTree(
                self.pager,
                self.pool,
                index.root_page,
                n_key_columns=len(index.columns),
                unique=True,
            )
            conflicting_rowid = tree.find_conflict(new_key)


            if conflicting_rowid is not None and conflicting_rowid != rowid:
                raise UniqueViolationError(index.name, tuple(new_key))
       


        for index in self.indexes:
            old_key = [old_values[self.table.column_index(column)] for column in index.columns]
            new_key = [new_values[self.table.column_index(column)] for column in index.columns]


            if old_key != new_key:
                tree = IndexBTree(
                        self.pager,
                        self.pool,
                        index.root_page,
                        n_key_columns=len(index.columns),
                        unique=index.unique,
                    )


                tree.delete(old_key, rowid)
       
       
        BTree(self.pager, self.pool, self.table.root_page).delete(rowid)
        try:
            BTree(self.pager, self.pool, self.table.root_page).insert(
                rowid,
                encode_record(new_values),
            )
        except PageFullError:
            BTree(self.pager, self.pool, self.table.root_page).insert(rowid, encode_record(old_values))
            raise


        for index in self.indexes:
            old_key = [old_values[self.table.column_index(column)] for column in index.columns]
            new_key = [new_values[self.table.column_index(column)] for column in index.columns]


            if old_key != new_key:
                tree = IndexBTree(
                        self.pager,
                        self.pool,
                        index.root_page,
                        n_key_columns=len(index.columns),
                        unique=index.unique,
                    )


                tree.insert(new_key, rowid)
       
       


    def close(self) -> None:
        pass


    def explain(self, depth: int = 0, verbose: bool = False) -> str:
        return _explain_line(depth, f"Update {self.table.name}")


class ExplainResult(Operator):
    """Wraps one precomputed row (EXPLAIN's rendered plan text) as a
    one-shot Operator, so Cursor's fetch machinery -- built to pull from
    an Operator, for the "one result shape for every statement kind" rule
    api/connection.py documents -- doesn't need a special case for a
    result that was never backed by a B-tree scan to begin with.
    """


    def __init__(self, row: Row) -> None:
        self._original_row = row
        self._row: Row | None = None


    def open(self) -> None:
        self._row = self._original_row


    def next(self) -> Row | None:
        row, self._row = self._row, None
        return row


    def close(self) -> None:
        self._row = None


    def explain(self, depth: int = 0, verbose: bool = False) -> str:
        return _explain_line(depth, "Result")


def _extract_predicates(where: BoundExpression | None) -> tuple[list[Predicate], list[BoundExpression]]:
    """Split a WHERE clause into sargable Predicates (candidates for an
    index seek) and the non-sargable conjuncts classify_predicate() rejected
    (an OR, a computed comparison, `NOT ...`, etc). Non-sargable conjuncts
    can never be consumed by any AccessPath's seek_terms -- no index will
    ever make them go away -- so they always end up in the final Filter,
    alongside whatever a chosen path's own `residual` leaves behind.
    """
    predicates: list[Predicate] = []
    non_sargable: list[BoundExpression] = []
    for conjunct in extract_conjuncts(where):
        predicate = classify_predicate(conjunct)
        if predicate is None:
            non_sargable.append(conjunct)
        else:
            predicates.append(predicate)
    return predicates, non_sargable




def _residual_filter_expression(
    non_sargable: list[BoundExpression], residual: tuple[Predicate, ...]
) -> BoundExpression | None:
    """AND together everything the chosen AccessPath doesn't already
    guarantee: every non-sargable conjunct, plus each residual Predicate's
    original `.source` -- `source` already IS the bound expression
    classify_predicate started from, so there's nothing to reconstruct.
    None when nothing is left over, so build_operator() can skip Filter
    entirely, matching the Week 3 "Filter omitted when there is no WHERE"
    behavior exactly (a full-seek equality plan with no other conjuncts
    needs no Filter at all).
    """
    parts = list(non_sargable) + [predicate.source for predicate in residual]
    if not parts:
        return None
    expression = parts[0]
    for part in parts[1:]:
        expression = BoundBinaryOp(expression, "AND", part)
    return expression




def build_operator(
    statement: BoundSelect | BoundInsert | BoundDelete | BoundUpdate,
    pager: Pager,
    pool: BufferPool,
    catalog: Catalog,
    stats: StatisticsCatalog | None = None,
) -> Operator:
    """Translate a bound statement into an executable operator tree.


    The SELECT plan is cost-based (chapter 12 §12.6, stages 1-4 from
    plan/predicates.py, plan/planner.py, plan/statistics.py, plan/cost.py,
    plan/search.py):


        Project
        └─ Filter       # omitted when nothing is left over to check
           └─ SeqScan | IndexScan


    `stats` is where real ANALYZE numbers enter the planner (stage-5 Step
    6): when a StatisticsCatalog is supplied, every candidate is costed
    against its `table_stats()`/`index_stats()` -- real measurements when
    the table/index has been ANALYZEd, the same flat documented fallback
    as before when it hasn't (StatisticsCatalog itself owns that
    fallback, so there's nothing left for this function to fall back to).
    `stats=None` (the default) skips StatisticsCatalog entirely and costs
    every candidate against the flat fallback directly -- this is what
    lets Step 2's own tests keep calling build_operator() without a live
    Catalog-backed StatisticsCatalog of their own, exactly the isolation
    those tests were written under before ANALYZE existed.


    `Filter` is built ONLY from what the chosen path doesn't already
    guarantee -- the seek_terms a chosen IndexScan consumes are satisfied
    by construction, so re-checking them would be redundant work. Getting
    this residual set right is exactly chapter 12 §12.6 trap #3: "the
    planner must never change results" -- a WHERE clause must return the
    same rows whether or not a matching index exists.


    `catalog` resolves which indexes need maintaining -- Catalog.indexes_for()
    -- for INSERT, DELETE, and UPDATE alike, and now also which indexes are
    available to seek for SELECT.
    """
    if isinstance(statement, BoundInsert):
        indexes = catalog.indexes_for(statement.table.name)
        return Insert(pager, pool, statement, indexes)


    if isinstance(statement, BoundDelete):
        indexes = catalog.indexes_for(statement.table.name)
        return Delete(pager, pool, statement.table, statement.where, indexes)


    if isinstance(statement, BoundUpdate):
        indexes = catalog.indexes_for(statement.table.name)
        return Update(pager, pool, statement.table, statement.assignments, statement.where, indexes)


    indexes = catalog.indexes_for(statement.table.name)
    predicates, non_sargable = _extract_predicates(statement.where)


    table_stats = stats.table_stats(statement.table.name) if stats is not None else default_table_stats()
    candidates = enumerate_access_paths(statement.table, list(indexes), predicates)
    costed_candidates = []
    for candidate in candidates:
        if candidate.index is None:
            index_stats = None
        elif stats is not None:
            index_stats = stats.index_stats(candidate.index)
        else:
            index_stats = default_index_stats(candidate.index, table_stats)
        candidate = estimate_row_counts(candidate, index_stats, table_stats)
        candidate = assign_cost(candidate, index_stats, table_stats)
        costed_candidates.append(candidate)
    path = choose_access_path(costed_candidates)


    source: Operator
    if path.kind == "index_scan":
        source = IndexScan(pager, pool, statement.table, path)
    else:
        source = SeqScan(pager, pool, statement.table)


    filter_expression = _residual_filter_expression(non_sargable, path.residual)
    if filter_expression is not None:
        source = Filter(source, filter_expression)
    return Project(source, statement.expressions)