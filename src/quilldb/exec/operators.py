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
from collections.abc import Sequence
from typing import Self


from quilldb.btree.btree import BTree
from quilldb.btree.cursor import TableCursor
from quilldb.btree.index import IndexBTree
from quilldb.catalog.catalog import Catalog
from quilldb.catalog.schema import IndexSchema, TableSchema
from quilldb.codec.record import decode_record, encode_record
from quilldb.errors import UniqueViolationError
from quilldb.exec.expressions import Row, evaluate, where_passes
from quilldb.sql.binder import BoundExpression, BoundInsert, BoundSelect
from quilldb.storage.bufferpool import BufferPool
from quilldb.storage.pager import Pager




class Operator(ABC):
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
    def explain(self, depth: int = 0) -> str:
        """A one-operator-per-line plan tree, deepest last."""


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


    def explain(self, depth: int = 0) -> str:
        return _explain_line(depth, f"SeqScan {self.table.name}")




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


    def explain(self, depth: int = 0) -> str:
        return _explain_line(depth, "Filter") + "\n" + self.child.explain(depth + 1)




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


    def explain(self, depth: int = 0) -> str:
        return _explain_line(depth, "Project") + "\n" + self.child.explain(depth + 1)




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


    def explain(self, depth: int = 0) -> str:
        return _explain_line(depth, f"Insert {self.statement.table.name}")




def build_operator(
    statement: BoundSelect | BoundInsert,
    pager: Pager,
    pool: BufferPool,
    catalog: Catalog,
) -> Operator:
    """Translate a bound statement into the Week 3 fixed plan.


    The SELECT plan is always:


        Project
        └─ Filter       # omitted when there is no WHERE
           └─ SeqScan


    That IS a planner -- just one with a single possible access path. Week 4
    moves this function into plan/planner.py and teaches it to choose
    IndexScan, without changing any operator contract above; that's the
    payoff for keeping the choice here rather than inlining it into
    Connection.execute().

    `catalog` isn't used by the SELECT plan yet -- it exists so INSERT can
    resolve which indexes need maintaining (Catalog.indexes_for()), and so
    this signature doesn't have to change again once IndexScan needs it too.
    """
    if isinstance(statement, BoundInsert):
        indexes = catalog.indexes_for(statement.table.name)
        return Insert(pager, pool, statement, indexes)


    source: Operator = SeqScan(pager, pool, statement.table)
    if statement.where is not None:
        source = Filter(source, statement.where)
    return Project(source, statement.expressions)