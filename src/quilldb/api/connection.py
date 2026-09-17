"""Small DB-API-inspired public surface: `connect`, `Connection`, `Cursor`.


Every layer built so far (catalog, binder, evaluator, operators) is correct
but unusable on its own -- a caller would need to know about Pager,
BufferPool, and Catalog just to run one query. This module is the seam: it
owns the three storage handles for the lifetime of one open database, and
turns `execute()` into the one call a caller needs, whatever kind of
statement it was.


Three things this layer alone is responsible for, that no layer below it
can be:


1. UNIFORM RESULT SHAPE ACROSS STATEMENT KINDS. CREATE TABLE mutates the
   catalog and produces no rows; INSERT writes one row and produces no rows
   either, just a count; SELECT produces a lazy stream. execute() always
   returns a Cursor -- description and rowcount are where the difference
   surfaces, not the return type.
2. THE STREAM SURVIVES THE HANDOFF. SeqScan/Filter/Project already refuse
   to materialize a result set (exec/operators.py). If execute() drained an
   operator into a list before returning, that guarantee would be undone
   right at the boundary a caller actually touches. Cursor.fetchone() pulls
   from the same still-open operator, one row at a time.
3. ONE RULE FOR RESULT LIFETIME. A Cursor holds an open Operator, which
   holds b-tree pins. Starting a new execute() on a connection closes
   whatever cursor that connection still had open, so pins can't accumulate
   silently across queries -- multiple *simultaneous* result sets require
   multiple connections, matching every DB-API's convention.


CREATE TABLE and INSERT are executed to completion before execute() returns
-- there is nothing left to stream. SELECT is the one case that leaves an
operator open past the call that created it.
"""


from collections.abc import Sequence
from pathlib import Path
from typing import Self


from quilldb.catalog.catalog import Catalog
from quilldb.codec.record import Value
from quilldb.errors import UnsupportedFeatureError
from quilldb.exec.operators import Operator, build_operator
from quilldb.sql.binder import (
    BoundBinaryOp,
    BoundColumn,
    BoundCreateTable,
    BoundExpression,
    BoundInsert,
    BoundIsNull,
    BoundLiteral,
    BoundUnaryOp,
    bind,
)
from quilldb.sql.parser import parse
from quilldb.storage.bufferpool import BufferPool
from quilldb.storage.pager import Pager


_MEMORY_PATH = ":memory:"




class Cursor:
    """One statement's result. `description`/`rowcount` are fixed at
    creation; `fetch*` pulls from the operator underneath, one row at a
    time, until it's exhausted or closed.
    """


    def __init__(
        self,
        operator: Operator | None,
        description: tuple[tuple[str, ...], ...] | None,
        rowcount: int,
    ) -> None:
        self._operator = operator
        self._description = description
        self._rowcount = rowcount
        self._closed = False


    @property
    def description(self) -> tuple[tuple[str, ...], ...] | None:
        """One tuple per output expression, or None for CREATE TABLE/INSERT.


        Only element 0 (the display name) is guaranteed this week -- the
        real DB-API's other six positions (type, size, precision...) have
        no meaning quilldb can supply yet.
        """
        return self._description


    @property
    def rowcount(self) -> int:
        """1 for a successful INSERT, 0 for CREATE TABLE, -1 for SELECT.


        -1 rather than a real count: SELECT is a lazy stream, so "how many
        rows" isn't known until fetch* has drained it, same as sqlite3's
        own Cursor.
        """
        return self._rowcount


    def fetchone(self) -> tuple[Value, ...] | None:
        """One row, or None once exhausted -- and every call after that.


        A row that raises (a TypeMismatchError from evaluate(), say) leaves
        this cursor closed rather than merely exhausted: the operator chain
        underneath already closed itself on the way out (exec/operators.py's
        every-operator-closes-on-exception rule), so letting a later
        fetchone() call reach it would just see "no cursor, return None" --
        indistinguishable from a query that finished cleanly. A caller that
        catches the error and keeps pulling deserves "cursor is closed", not
        a silent lie that there were simply no more rows.
        """
        if self._closed:
            raise ValueError("cursor is closed")
        if self._operator is None:
            return None
        try:
            row = self._operator.next()
        except Exception:
            self._operator = None
            self._closed = True
            raise
        if row is None:
            # Nothing left to stream: release the pins now rather than
            # waiting for an explicit close() that may never come.
            self._operator.close()
            self._operator = None
        return row


    def fetchmany(self, size: int = 1) -> list[tuple[Value, ...]]:
        if size < 0:
            raise ValueError(f"fetchmany size must not be negative, got {size}")
        rows: list[tuple[Value, ...]] = []
        for _ in range(size):
            row = self.fetchone()
            if row is None:
                break
            rows.append(row)
        return rows


    def fetchall(self) -> list[tuple[Value, ...]]:
        rows: list[tuple[Value, ...]] = []
        while (row := self.fetchone()) is not None:
            rows.append(row)
        return rows


    def close(self) -> None:
        if self._operator is not None:
            self._operator.close()
            self._operator = None
        self._closed = True




def _display_name(expression: BoundExpression) -> str:
    """The source column name for a plain column, or SQL-ish reconstructed
    text otherwise -- exactly what Cursor.description's element 0 needs.
    """
    if isinstance(expression, BoundColumn):
        return expression.name
    if isinstance(expression, BoundLiteral):
        return repr(expression.value)
    if isinstance(expression, BoundUnaryOp):
        return f"{expression.operator}{_display_name(expression.operand)}"
    if isinstance(expression, BoundBinaryOp):
        return f"{_display_name(expression.left)} {expression.operator} {_display_name(expression.right)}"
    if isinstance(expression, BoundIsNull):
        suffix = "IS NOT NULL" if expression.negated else "IS NULL"
        return f"{_display_name(expression.operand)} {suffix}"
    raise UnsupportedFeatureError(f"cannot describe a {type(expression).__name__}")




class Connection:
    """Owns one Pager/BufferPool/Catalog for as long as the database is
    open. `execute()` is the only entry point that touches them.
    """


    def __init__(self, pager: Pager, pool: BufferPool, catalog: Catalog) -> None:
        self.pager = pager
        self.pool = pool
        self.catalog = catalog
        self._open_cursor: Cursor | None = None
        self._closed = False


    def execute(self, sql: str, parameters: Sequence[Value] = ()) -> Cursor:
        """Parse, bind, and execute one statement.


        CREATE TABLE and INSERT complete before this method returns. SELECT
        leaves its operator open and streams rows through the returned
        Cursor. Starting another execute() closes any still-open result
        cursor on this connection; multiple active cursors arrive with
        multiple connections.
        """
        if self._closed:
            raise ValueError("connection is closed")
        if self._open_cursor is not None:
            self._open_cursor.close()
            self._open_cursor = None


        bound = bind(parse(sql), self.catalog, tuple(parameters))


        if isinstance(bound, BoundCreateTable):
            self.catalog.create_table(bound.statement, sql)
            return Cursor(None, None, 0)


        if isinstance(bound, BoundInsert):
            with build_operator(bound, self.pager, self.pool) as operator:
                operator.next()
            return Cursor(None, None, 1)


        operator = build_operator(bound, self.pager, self.pool)
        operator.open()
        description = tuple((_display_name(e),) for e in bound.expressions)
        cursor = Cursor(operator, description, -1)
        self._open_cursor = cursor
        return cursor


    def close(self) -> None:
        """Flush every dirty page and close the file. Idempotent."""
        if self._closed:
            return
        if self._open_cursor is not None:
            self._open_cursor.close()
            self._open_cursor = None
        self.pool.flush_all()
        self.pager.close()
        self._closed = True


    def __enter__(self) -> Self:
        return self


    def __exit__(self, *exc_info: object) -> None:
        self.close()




def connect(path: str | Path) -> Connection:
    """Open an existing database or create a new one.


    The exact string ":memory:" selects Pager.memory() and never creates a
    file -- anything else, including a Path spelled ":memory:", is a real
    path on disk.
    """
    if path == _MEMORY_PATH:
        pager = Pager.memory()
    else:
        path = Path(path)
        pager = Pager.open(path) if path.exists() else Pager.create(path)


    pool = BufferPool(pager)
    catalog = Catalog(pager, pool)
    catalog.load()
    return Connection(pager, pool, catalog)