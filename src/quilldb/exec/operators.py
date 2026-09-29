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
from collections.abc import Callable, Iterator, Sequence
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
from quilldb.plan.planner import (
    AccessPath,
    PlanShape,
    SortKey,
    access_path_shape,
    enumerate_access_paths,
    rebuild_access_path,
)
from quilldb.plan.predicates import (
    Predicate,
    classify_predicate,
    extract_conjuncts,
    referenced_tables,
)
from quilldb.plan.search import (
    PlanCandidate,
    choose_access_path,
    choose_join_plan,
    enumerate_join_plans,
    sort_cost,
)
from quilldb.plan.statistics import (
    IndexStats,
    TableStats,
    default_index_stats,
    default_table_stats,
    estimate_row_counts,
)
from quilldb.sql.ast import DataType
from quilldb.sql.binder import (
    BoundAggregateSelect,
    BoundAssignment,
    BoundBinaryOp,
    BoundColumn,
    BoundDelete,
    BoundExpression,
    BoundInsert,
    BoundJoinSelect,
    BoundOrderKey,
    BoundSelect,
    BoundUpdate,
    JoinOrderKey,
    resolve_layout,
)
from quilldb.storage.bufferpool import BufferPool
from quilldb.storage.pager import Pager
from quilldb.txn.transaction import Transaction


class Operator(ABC):
    rows_affected: int = 0  # meaningful only for Insert/Delete/Update; SELECT streams instead


    @abstractmethod
    def open(self, outer: Row = ()) -> None:
        """Acquire whatever this operator needs to produce rows.


        Calling open() on an already-open operator resets it: any resources
        from the previous open are released first, so re-opening can't leak.


        `outer` is the current row of whatever ENCLOSES this operator --
        empty for a top-level SELECT, and the outer side's current row for
        an operator re-opened once per outer row inside a NestedLoopJoin
        (week7-query-processing.md session 0.4/§41). Only IndexScan reads
        it (a correlated seek value is an expression over the outer row's
        columns); every other operator either ignores it or threads it
        straight to its child. The default `()` is what keeps every
        pre-week-7 call site (`operator.open()`) working unchanged.
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


    def __init__(
        self, pager: Pager, pool: BufferPool, table: TableSchema, txn: Transaction | None = None
    ) -> None:
        self.pager = pager
        self.pool = pool
        self.table = table
        self.txn = txn
        self._cursor: TableCursor | None = None
        self._positioned = False


    def open(self, outer: Row = ()) -> None:
        # SHARED on the table before either cursor touches a page (SS38) --
        # txn is None only for tests that build this operator directly,
        # bypassing Connection/locking entirely (build_operator's own
        # `stats=None` default follows the same convention). SeqScan has
        # no correlated seek to evaluate, so `outer` is accepted and
        # otherwise unused -- it exists on every open() only to keep the
        # Operator contract uniform.
        if self.txn is not None:
            self.txn.lock_for_read(self.table.name)
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
            self.pool.rows_examined += 1
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




def _seek_value(predicate: Predicate, outer: Row = ()) -> Value:
    """Fold a seek_term's value side down to the constant to probe with.


    NOT always a BoundLiteral, which is what an earlier version asserted.
    plan/predicates.py's sargability test is `_is_column_free`, and it
    deliberately admits any expression computable without reading a row --
    so `age >= -1` arrives as a BoundUnaryOp (SQL has no negative literal;
    the minus is an operator) and `age = 2 + 0` as a BoundBinaryOp. Both
    are legal search arguments, and real sqlite3 plans the first as
    `SEARCH t USING INDEX ix_age (age>?)`, so the fix is to evaluate them,
    not to refuse the index.


    `outer` (week7-query-processing.md session 0.4) is the enclosing
    NestedLoopJoin's current outer row -- what makes an index-join's
    correlated seek (`o.user_id = u.id`, planned as a seek on `orders`
    parameterized by `u.id`) possible: the seek VALUE is an expression
    over the OUTER row's columns, bound against the outer row's own
    layout, never the combined one (§41 trap #3). Every non-correlated
    seek keeps working unchanged because `outer` defaults to `()`, same
    empty row `evaluate` already treated as unreachable input for a
    column-free expression.


    Should a BoundColumn ever leak through classify_predicate for a
    non-correlated seek, it surfaces as ColumnNotFoundError ("the row is
    shorter than a BoundColumn's index"), which is errors.py's existing
    name for a binder/executor disagreement -- the right classification
    for this, and the reason no bare assert is needed to guard it.
    """
    return evaluate(predicate.value, outer)




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


    def __init__(
        self,
        pager: Pager,
        pool: BufferPool,
        table: TableSchema,
        path: AccessPath,
        txn: Transaction | None = None,
    ) -> None:
        self.pager = pager
        self.pool = pool
        self.table = table
        self.path = path
        self.txn = txn
        self._rowids: Iterator[int] | None = None
        self._cursor: TableCursor | None = None


    def open(self, outer: Row = ()) -> None:
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


        A seek_term's probe value comes out of a `Predicate` through
        `_seek_value`, which evaluates it: `predicate.value` is any
        column-free expression, not necessarily a `BoundLiteral`.


        This operator does its own per-row table lookups in next() through
        a single reused TableCursor -- build it here as
        `self._cursor = TableCursor(self.pager, self.pool, self.table.root_page)`,
        matching how SeqScan.open() builds its cursor. Re-opening must reset
        rather than leak, same rule as SeqScan.open().
        """
        # SHARED on the TABLE, not the index -- the index is derived data
        # belonging to the table, and locking them separately would invent
        # a second lock order for no benefit (week6-concurrency.md SS38).
        if self.txn is not None:
            self.txn.lock_for_read(self.table.name)
        index = self.path.index
        self.close()  # re-opening resets rather than leaking the old cursor
        if not index:
            return
        btree = IndexBTree(self.pager, self.pool, index.root_page, n_key_columns=len(index.columns), unique=index.unique)
        self._cursor = TableCursor(self.pager, self.pool, self.table.root_page)


        # A seek bound that folds to NULL makes the conjunct unsatisfiable,
        # and it has to be caught here rather than handed to the b-tree.
        # compare_keys orders NULL below every other value -- correct for
        # STORING NULLs in an index, and exactly the wrong semantics for a
        # comparison BOUND, because `age > NULL` would then seek "everything
        # above the lowest possible key" and return the whole table.
        # Measured before this guard existed, same rows indexed vs. not:
        #
        #   WHERE age >  NULL    SeqScan []    IndexScan [0,1,2,3,4]   WRONG
        #   WHERE age =  NULL    SeqScan []    IndexScan [5]           WRONG
        #   WHERE age IS NULL    SeqScan [5]   IndexScan [5]           right
        #
        # Real sqlite3 returns nothing for the first two: `=`, `<`, `>`
        # against NULL evaluate to NULL, never true. `IS` is the one
        # operator that DOES match NULLs, which is why classify_predicate
        # gives it a name of its own and why it must be excluded here --
        # including it would break `WHERE age IS NULL`, a legitimate seek.
        #
        # This is the worst class of bug the planner can have: an index is
        # only allowed to change a query's SPEED, never its RESULTS, and
        # getting it wrong returns wrong rows silently rather than raising.
        for term in self.path.seek_terms:
            if _seek_value(term, outer) is None and term.operator != "IS":
                self._rowids = iter(())
                return


        # check if seek terms is all equal comparison
        all_eq = True


        for predicate in self.path.seek_terms:
            if predicate.operator not in ["=", "IS"]:
                all_eq = False


        if all_eq:
            if not self.path.seek_terms and self.path.reverse:
                # No seek at all -- session 7's sort-avoidance candidate for
                # `ORDER BY indexed_col DESC` (plan/planner.py's
                # _index_order_path, output_order tagged reverse=True): walk
                # the WHOLE index back to front instead of front to back, a
                # B+tree's own free direction (chapter 18 SS18.3). scan_reverse
                # yields (key values, rowid) pairs, unlike seek_eq's bare
                # rowids -- drop the key half, the same shape every other
                # branch here already produces.
                self._rowids = (rowid for _, rowid in btree.scan_reverse())
            else:
                # get all the predicates
                values = [_seek_value(predicate, outer) for predicate in self.path.seek_terms]
                self._rowids = btree.seek_eq(values)
        else:
            equality_prefix = [
                _seek_value(predicate, outer)
                for predicate in self.path.seek_terms
                if predicate.operator in ["=", "IS"]
            ]
            low_bound: list[Value] = []
            high_bound: list[Value] = []
            low_inclusive = high_inclusive = True


            for predicate in self.path.seek_terms:
                value = _seek_value(predicate, outer)
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
                self.pool.rows_examined += 1
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
        if self.path.reverse:
            label += " REVERSE"
        if not verbose:
            return _explain_line(depth, label)


        if self.path.seek_terms:
            predicate_text = " AND ".join(
                f"{p.column} {p.operator} {_literal_sql(_seek_value(p))}" for p in self.path.seek_terms
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


    def open(self, outer: Row = ()) -> None:
        self.child.open(outer)


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


    def open(self, outer: Row = ()) -> None:
        self.child.open(outer)


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




class Distinct(Operator):
    """Drop duplicate rows from the child, streaming rather than
    materializing the whole result: only the set of rows already returned
    needs to be held, not the child's entire output. Every Value the codec
    produces (int, float, str, bytes, bool, None) is hashable, and a Row
    is a tuple of them, so a child row is a legal set member with nothing
    to convert.

    Sits ABOVE Project in build_operator() (`SELECT DISTINCT` dedups the
    OUTPUT columns, not the underlying table row) -- so two rows differing
    only in a column that wasn't selected still collapse to one.
    """

    def __init__(self, child: Operator) -> None:
        self.child = child
        self._seen: set[Row] = set()

    def open(self, outer: Row = ()) -> None:
        self.child.open(outer)
        self._seen = set()

    def next(self) -> Row | None:
        try:
            while True:
                row = self.child.next()
                if row is None:
                    return None
                if row not in self._seen:
                    self._seen.add(row)
                    return row
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        self._seen = set()
        self.child.close()

    def explain(self, depth: int = 0, verbose: bool = False) -> str:
        return _explain_line(depth, "Distinct") + "\n" + self.child.explain(depth + 1, verbose)


class Limit(Operator):
    """Skip `offset` rows, then yield at most `limit` more (None = every
    remaining row).

    Pulls from `child` lazily, one row at a time, exactly like every other
    operator here -- which is what makes `LIMIT 1` over a million-row
    SeqScan/IndexScan read only a handful of pages instead of the whole
    table (the pull model's whole justification, chapter 09 -- and
    week7-query-processing.md §44's short-circuit tests exist to prove it
    stays true through this operator too).
    """

    def __init__(self, child: Operator, limit: int | None, offset: int = 0) -> None:
        self.child = child
        self.limit = limit
        self.offset = offset
        self._skipped = 0
        self._returned = 0

    def open(self, outer: Row = ()) -> None:
        self.child.open(outer)
        self._skipped = 0
        self._returned = 0

    def next(self) -> Row | None:
        try:
            while self._skipped < self.offset:
                row = self.child.next()
                if row is None:
                    return None
                self._skipped += 1
            if self.limit is not None and self._returned >= self.limit:
                return None
            row = self.child.next()
            if row is None:
                return None
            self._returned += 1
            return row
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        self.child.close()

    def explain(self, depth: int = 0, verbose: bool = False) -> str:
        label = "Limit"
        if self.limit is not None:
            label += f" {self.limit}"
        if self.offset:
            label += f" OFFSET {self.offset}"
        return _explain_line(depth, label) + "\n" + self.child.explain(depth + 1, verbose)


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
        txn: Transaction | None = None,
    ) -> None:
        self.pager = pager
        self.pool = pool
        self.statement = statement
        self.indexes = indexes
        self.txn = txn
        self._attempted = False


    def open(self, outer: Row = ()) -> None:
        if self.txn is not None:
            self.txn.lock_for_write(self.statement.table.name)
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
        txn: Transaction | None = None,
    ) -> None:
        self.pager = pager
        self.pool = pool
        self.table = table
        self.predicate = predicate
        self.indexes = indexes
        self.txn = txn
        self._attempted = False


    def open(self, outer: Row = ()) -> None:
        if self.txn is not None:
            self.txn.lock_for_write(self.table.name)
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
        txn: Transaction | None = None,
    ) -> None:
        self.pager = pager
        self.pool = pool
        self.table = table
        self.assignments = assignments
        self.predicate = predicate
        self.indexes = indexes
        self.txn = txn
        self._attempted = False


    def open(self, outer: Row = ()) -> None:
        if self.txn is not None:
            self.txn.lock_for_write(self.table.name)
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


    def open(self, outer: Row = ()) -> None:
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




class _DefaultStats:
    """The `stats=None` fallback for join planning, matching the single-
    table path's own convention (see build_operator's `stats` docstring
    below): every candidate costs against the flat documented default
    rather than real ANALYZE numbers. Satisfies plan/search.py's StatsSource
    Protocol structurally -- no inheritance from StatisticsCatalog needed,
    the same reasoning as sql/binder.py's SchemaSource.
    """

    def table_stats(self, name: str) -> TableStats:
        return default_table_stats()

    def index_stats(self, index: IndexSchema) -> IndexStats:
        return default_index_stats(index, default_table_stats())


def _and_all(expressions: list[BoundExpression]) -> BoundExpression | None:
    if not expressions:
        return None
    result = expressions[0]
    for expression in expressions[1:]:
        result = BoundBinaryOp(result, "AND", expression)
    return result


def _build_join_operator(
    statement: BoundJoinSelect,
    pager: Pager,
    pool: BufferPool,
    catalog: Catalog,
    stats: StatisticsCatalog | None,
    txn: Transaction | None,
) -> Operator:
    """The join equivalent of build_operator()'s single-table plan: pick a
    PlanCandidate (plan/search.py's enumerate_join_plans/choose_join_plan),
    then build the same SeqScan/IndexScan/Filter shape per table, glued
    together with exec/join.py's NestedLoopJoin in the candidate's chosen
    order.


        Project
        └─ Filter                    # only if `candidate.residual` is non-empty
           └─ NestedLoopJoin          # one per join step, left-deep
              ├─ ... (outer side, recursively the same shape)
              └─ Filter               # only if this table has its OWN residual
                 └─ SeqScan | IndexScan


    Every BoundColumn the binder produced is still (table_ordinal, index) --
    sql/binder.py's resolve_layout is what turns that into the flat index
    each operator actually reads, using `offsets`, built up here exactly as
    NestedLoopJoin will concatenate rows: table_ordinal -> its starting
    position once every table up to and including it is in hand.


    ORDER BY/LIMIT/OFFSET (session 7) sit on top, same tail as a plain
    SELECT's own (`_apply_order_limit`) -- but `statement.order_by` arrives
    as JoinOrderKeys (expression-shaped, table-scope: see that type's own
    docstring), which can only become a row-position-shaped BoundOrderKey
    once `offsets` exists, i.e. AFTER a candidate is chosen -- so, unlike
    the single-table/aggregate paths, that resolution happens here rather
    than at bind time.
    """
    # Deferred: exec/join.py imports Operator from this module at its own
    # import time, so a module-level import here would cycle.
    from quilldb.exec.join import NestedLoopJoin

    stats_source = stats if stats is not None else _DefaultStats()
    candidates = enumerate_join_plans(
        list(statement.scopes), list(statement.joins), statement.where, catalog, stats_source
    )
    sort_keys = tuple(SortKey(key.expression, key.descending) for key in statement.order_by)
    candidate: PlanCandidate = choose_join_plan(candidates, sort_keys)

    source: Operator | None = None
    offsets: dict[int, int] = {}
    offset = 0

    for position, table_ordinal in enumerate(candidate.order):
        scope = statement.scopes[table_ordinal]
        path = candidate.access_paths[position]

        scan: Operator
        if path.kind == "index_scan":
            scan = IndexScan(pager, pool, scope.table, path, txn)
        else:
            scan = SeqScan(pager, pool, scope.table, txn)

        # Only a residual predicate sourced ENTIRELY from this table can be
        # checked right after its own scan -- one that also names an
        # earlier table can't be evaluated until that table's columns are
        # in hand too, which is exactly what the `else` branch below does
        # once this step's NestedLoopJoin has produced a combined row.
        own_residual = [
            predicate.source
            for predicate in path.residual
            if referenced_tables(predicate.source) == {table_ordinal}
        ]
        own_filter = _and_all(own_residual)
        if own_filter is not None:
            scan = Filter(scan, resolve_layout(own_filter, {table_ordinal: 0}))

        if source is None:
            source = scan
        else:
            match = candidate.match_expressions[position]
            match_offsets = dict(offsets)
            match_offsets[table_ordinal] = offset
            resolved_match = None if match is None else resolve_layout(match, match_offsets)
            source = NestedLoopJoin(
                source, scan, resolved_match, len(scope.table.columns), candidate.join_types[position]
            )

        offsets[table_ordinal] = offset
        offset += len(scope.table.columns)

    assert source is not None, "a join always has at least two tables, so at least one iteration ran"
    if candidate.residual is not None:
        source = Filter(source, resolve_layout(candidate.residual, offsets))

    expressions = tuple(resolve_layout(expression, offsets) for expression in statement.expressions)
    order_by, hidden_order_by = _resolve_join_order_by(statement.order_by, expressions, offsets)
    projected: Operator = Project(source, expressions + hidden_order_by)
    skip_sort = bool(sort_keys) and bool(candidate.output_order) and sort_cost(candidate, sort_keys) == 0.0
    return _apply_order_limit(
        projected,
        len(expressions),
        order_by,
        hidden_order_by,
        statement.limit,
        statement.offset,
        skip_sort=skip_sort,
    )


def _resolve_join_order_by(
    items: tuple[JoinOrderKey, ...],
    expressions: tuple[BoundExpression, ...],
    offsets: dict[int, int],
) -> tuple[tuple[BoundOrderKey, ...], tuple[BoundExpression, ...]]:
    """_build_join_operator's own version of sql/binder.py's
    _resolve_order_by_key/_bind_order_by, run here instead of at bind time
    because a join's row layout doesn't exist until `offsets` does (see
    _build_join_operator's own docstring). Every `items[i].expression` is
    already a concrete BoundExpression -- JoinOrderKey has no ordinal case
    left to resolve, sql/binder.py's _bind_join_order_by settled that
    eagerly against the SELECT list, before a join order was even chosen.
    """
    hidden: list[BoundExpression] = []
    keys = []
    for item in items:
        resolved = resolve_layout(item.expression, offsets)
        position = None
        for i, expr in enumerate(expressions):
            if expr == resolved:
                position = i
                break
        if position is None:
            hidden.append(resolved)
            position = len(expressions) + len(hidden) - 1
        keys.append(BoundOrderKey(position, item.descending))
    return tuple(keys), tuple(hidden)


def _build_single_table_source(
    table: TableSchema,
    where: BoundExpression | None,
    pager: Pager,
    pool: BufferPool,
    catalog: Catalog,
    stats: StatisticsCatalog | None,
    txn: Transaction | None,
    order_by: tuple[SortKey, ...] = (),
    cached_shape: PlanShape | None = None,
) -> tuple[Operator, AccessPath]:
    """The cost-based SeqScan/IndexScan (+ Filter) pipeline shared by a
    plain SELECT and a no-GROUP-BY aggregate SELECT alike -- WHERE
    placement and access-path choice don't depend on what sits on top of
    this (a Project or a HashAggregate), only on `table` and `where`.
    Factored out of build_operator()'s single-table branch so
    _build_aggregate_operator() doesn't duplicate stages 1-4 of the
    cost-based pipeline.

    Returns the chosen AccessPath alongside the operator (session 7): a
    plain SELECT's build_operator() needs it to decide whether the row
    stream already satisfies its own ORDER BY (path.output_order, via
    plan/search.py's sort_cost) and can skip adding a Sort.
    `order_by=()` (every pre-session-7 caller, and every call from
    _build_aggregate_operator below) keeps choose_access_path ranking on
    `cost.total` alone -- HashAggregate doesn't promise to preserve
    whatever order its own child scan happened to produce, so a raw
    table's physical order is not a property _build_aggregate_operator's
    OWN ORDER BY (over the grouped output) could safely rely on; that's a
    documented gap, not something this function tries to solve.

    `cached_shape` (plan/cache.py, session 7): when given, skips stages
    2-4 (row estimates, costs, and every candidate but the winning one)
    entirely -- `plan/planner.py`'s `rebuild_access_path` re-derives the
    SAME AccessPath a fresh enumeration+choose would have picked, against
    THIS call's own fresh `predicates`, with no quill_stat1 read at all.
    """
    indexes = list(catalog.indexes_for(table.name))
    predicates, non_sargable = _extract_predicates(where)

    path: AccessPath
    if cached_shape is not None:
        path = rebuild_access_path(cached_shape, table, indexes, predicates)
    else:
        table_stats = stats.table_stats(table.name) if stats is not None else default_table_stats()
        candidates = enumerate_access_paths(table, indexes, predicates)
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
        path = choose_access_path(costed_candidates, order_by)

    source: Operator
    if path.kind == "index_scan":
        source = IndexScan(pager, pool, table, path, txn)
    else:
        source = SeqScan(pager, pool, table, txn)

    filter_expression = _residual_filter_expression(non_sargable, path.residual)
    if filter_expression is not None:
        source = Filter(source, filter_expression)
    return source, path


def _strip_hidden_columns(source: Operator, visible_count: int) -> Operator:
    """Project a row back down to just its first `visible_count` columns --
    the final step for a query whose ORDER BY needed a hidden trailing
    column (BoundSelect.hidden_order_by / BoundAggregateSelect's own field)
    Sort could see but the caller never asked for. An ordinary Project
    works unchanged here: a BoundColumn's `index` is just a position in
    whatever row it's handed, and Sort's output row has the exact same
    shape as the Project below it -- only the ROW ORDER changed, not which
    column lives where.
    """
    expressions = tuple(BoundColumn(i, "", DataType.INTEGER) for i in range(visible_count))
    return Project(source, expressions)


def _order_by_sort_keys(
    expressions: tuple[BoundExpression, ...],
    hidden_order_by: tuple[BoundExpression, ...],
    order_by: tuple[BoundOrderKey, ...],
) -> tuple[SortKey, ...]:
    """Recover the EXPRESSION each already-resolved BoundOrderKey.index
    refers to (session 7): `_resolve_order_by_key` (sql/binder.py) threw
    that expression away once it settled on a row position, but
    plan/search.py's sort_cost needs the expression back, to `==`-compare
    against an AccessPath's own output_order. `(*expressions,
    *hidden_order_by)` is exactly the row BoundOrderKey.index already
    indexes into (BoundSelect.hidden_order_by's own docstring), so this is
    a pure lookup, not a re-resolution.
    """
    produced = expressions + hidden_order_by
    return tuple(SortKey(produced[key.index], key.descending) for key in order_by)


def _apply_order_limit(
    source: Operator,
    visible_count: int,
    order_by: tuple[BoundOrderKey, ...],
    hidden_order_by: tuple[BoundExpression, ...],
    limit: int | None,
    offset: int,
    *,
    skip_sort: bool = False,
) -> Operator:
    """The tail every SELECT shares once its own rows are ready: sort (if
    ORDER BY was given and no cheaper access path already produced that
    order -- `skip_sort`, session 7), strip any hidden columns Sort/the
    caller's own ORDER BY needed but the caller didn't ask for, then apply
    LIMIT/OFFSET. Shared between build_operator()'s plain-SELECT path,
    _build_aggregate_operator(), and _build_join_operator() so none of the
    three duplicate this sequencing.

    `skip_sort=True` never means "ORDER BY was ignored" -- it means the
    rows already arrived in that order (see build_operator()'s own
    sort_cost-gated computation of it), so adding a Sort node would only
    re-sort an already-sorted stream. The hidden-column strip still runs
    whenever `hidden_order_by` is non-empty regardless of `skip_sort`: an
    ORDER BY on a column outside the select list still had to widen the
    row upstream (build_operator()'s Project) to be compared against
    AT ALL, satisfied by the access path or not, so it still needs
    removing before the caller sees it.

    No bounded top-K heap: Sort always fully materializes and sorts, and
    LIMIT/OFFSET are applied strictly on top by Limit -- see exec/sort.py's
    module docstring for why that's a documented gap rather than what the
    roadmap's stretch goal describes.
    """
    from quilldb.exec.sort import Sort

    result = source
    if order_by and not skip_sort:
        result = Sort(result, order_by)
    if hidden_order_by:
        result = _strip_hidden_columns(result, visible_count)
    if limit is not None or offset:
        result = Limit(result, limit, offset)
    return result


def _build_aggregate_operator(
    statement: BoundAggregateSelect,
    pager: Pager,
    pool: BufferPool,
    catalog: Catalog,
    stats: StatisticsCatalog | None,
    txn: Transaction | None,
    cached_shape: PlanShape | None = None,
    on_planned: Callable[[PlanShape], None] | None = None,
) -> Operator:
    """
        Limit / Offset              # only when LIMIT or OFFSET was given
        └─ Project                  # strips hidden_order_by, if any were added
           └─ Sort                  # statement.order_by -- only if ORDER BY was given
              └─ Project                    # statement.select_items + hidden_order_by, over the flat grouped row
                 └─ Filter                  # statement.having -- only if HAVING was given
                    └─ HashAggregate        # statement.group_by / statement.aggregates
                       └─ SeqScan | IndexScan (+ Filter)   # statement.where, same as a plain SELECT

    select_items, having, and hidden_order_by are already BoundExpression
    trees over HashAggregate's flat output row (sql/binder.py's
    _bind_group_output), so the same Filter/Project operators a plain
    SELECT uses finish the pre-sort half of the query -- no special-cased
    evaluator needed for the post-grouping half.

    `cached_shape`/`on_planned` (session 7): see build_operator's own
    docstring -- this is just where they reach _build_single_table_source
    for the aggregate path.
    """
    # Deferred: exec/aggregate.py imports Operator from this module at its
    # own import time, so a module-level import here would cycle -- same
    # reasoning as _build_join_operator's deferred NestedLoopJoin import.
    from quilldb.exec.aggregate import HashAggregate

    source, path = _build_single_table_source(
        statement.table, statement.where, pager, pool, catalog, stats, txn, cached_shape=cached_shape
    )
    if cached_shape is None and on_planned is not None:
        on_planned(access_path_shape(path))
    grouped: Operator = HashAggregate(source, statement.group_by, statement.aggregates)
    if statement.having is not None:
        grouped = Filter(grouped, statement.having)
    projected: Operator = Project(grouped, statement.select_items + statement.hidden_order_by)
    return _apply_order_limit(
        projected,
        len(statement.select_items),
        statement.order_by,
        statement.hidden_order_by,
        statement.limit,
        statement.offset,
    )


def build_operator(
    statement: BoundSelect | BoundJoinSelect | BoundAggregateSelect | BoundInsert | BoundDelete | BoundUpdate,
    pager: Pager,
    pool: BufferPool,
    catalog: Catalog,
    stats: StatisticsCatalog | None = None,
    txn: Transaction | None = None,
    cached_shape: PlanShape | None = None,
    on_planned: Callable[[PlanShape], None] | None = None,
) -> Operator:
    """Translate a bound statement into an executable operator tree.


    The SELECT plan is cost-based (chapter 12 §12.6, stages 1-4 from
    plan/predicates.py, plan/planner.py, plan/statistics.py, plan/cost.py,
    plan/search.py):


        Limit / Offset  # only when LIMIT or OFFSET was given
        └─ Project       # strips hidden_order_by, if any were added
           └─ Sort          # only if ORDER BY was given
              └─ Distinct      # only for SELECT DISTINCT
                 └─ Project
                    └─ Filter       # omitted when nothing is left over to check
                       └─ SeqScan | IndexScan

    A BoundAggregateSelect (GROUP BY and/or an aggregate call) builds a
    different shape -- see _build_aggregate_operator's own docstring.


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

    `txn=None` (the default) follows the same convention for locking
    (week6-concurrency.md SS38): SeqScan/IndexScan/Insert/Delete/Update
    only call lock_for_read()/lock_for_write() when a real Transaction is
    supplied, so every pre-week-6 test calling build_operator() directly
    keeps working unlocked.


    `Filter` is built ONLY from what the chosen path doesn't already
    guarantee -- the seek_terms a chosen IndexScan consumes are satisfied
    by construction, so re-checking them would be redundant work. Getting
    this residual set right is exactly chapter 12 §12.6 trap #3: "the
    planner must never change results" -- a WHERE clause must return the
    same rows whether or not a matching index exists.


    `catalog` resolves which indexes need maintaining -- Catalog.indexes_for()
    -- for INSERT, DELETE, and UPDATE alike, and now also which indexes are
    available to seek for SELECT.


    `cached_shape`/`on_planned` (plan/cache.py, session 7) are the plan
    cache's own hooks, meaningful only for a single-table `BoundSelect`/
    `BoundAggregateSelect` (a joined SELECT always plans fresh -- see
    plan/cache.py's own docstring for why). `cached_shape`, when given,
    skips straight to rebuilding the named AccessPath, no enumeration or
    quill_stat1 reads. `on_planned`, when given AND `cached_shape` was
    NOT, is called once with the freshly chosen AccessPath's shape -- the
    plan cache's own way to learn what to store, without build_operator()
    itself gaining a cache dependency or a different return type: every
    pre-session-7 caller passes neither and sees no change at all.
    """
    if isinstance(statement, BoundJoinSelect):
        return _build_join_operator(statement, pager, pool, catalog, stats, txn)


    if isinstance(statement, BoundAggregateSelect):
        return _build_aggregate_operator(statement, pager, pool, catalog, stats, txn, cached_shape, on_planned)


    if isinstance(statement, BoundInsert):
        indexes = catalog.indexes_for(statement.table.name)
        return Insert(pager, pool, statement, indexes, txn)


    if isinstance(statement, BoundDelete):
        indexes = catalog.indexes_for(statement.table.name)
        return Delete(pager, pool, statement.table, statement.where, indexes, txn)


    if isinstance(statement, BoundUpdate):
        indexes = catalog.indexes_for(statement.table.name)
        return Update(pager, pool, statement.table, statement.assignments, statement.where, indexes, txn)


    sort_keys = _order_by_sort_keys(statement.expressions, statement.hidden_order_by, statement.order_by)
    source, path = _build_single_table_source(
        statement.table, statement.where, pager, pool, catalog, stats, txn, sort_keys, cached_shape
    )
    if cached_shape is None and on_planned is not None:
        on_planned(access_path_shape(path))
    projected: Operator = Project(source, statement.expressions + statement.hidden_order_by)
    if statement.distinct:
        projected = Distinct(projected)
    # Only ever ask sort_cost when there's a real chance of an answer other
    # than "not satisfied" -- session 7's own guard (see choose_access_path's
    # docstring): with no candidate advertising ANY output_order, no Sort
    # could ever be avoided, so there's nothing to gain from asking.
    skip_sort = bool(sort_keys) and bool(path.output_order) and sort_cost(path, sort_keys) == 0.0
    return _apply_order_limit(
        projected,
        len(statement.expressions),
        statement.order_by,
        statement.hidden_order_by,
        statement.limit,
        statement.offset,
        skip_sort=skip_sort,
    )