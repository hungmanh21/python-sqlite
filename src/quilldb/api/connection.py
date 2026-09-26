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


CREATE TABLE, INSERT, DELETE, and UPDATE are executed to completion before
execute() returns -- there is nothing left to stream. SELECT is the one
case that leaves an operator open past the call that created it.
"""


import time
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Self

from quilldb.catalog.catalog import Catalog
from quilldb.codec.record import Value
from quilldb.errors import TransactionError, UnsupportedFeatureError
from quilldb.exec.operators import ExplainResult, Operator, build_operator
from quilldb.plan.analyze import StatisticsCatalog
from quilldb.sql.binder import (
    BoundAnalyze,
    BoundBegin,
    BoundBinaryOp,
    BoundColumn,
    BoundCommit,
    BoundCreateIndex,
    BoundCreateTable,
    BoundDelete,
    BoundExplain,
    BoundExpression,
    BoundInsert,
    BoundIsNull,
    BoundLiteral,
    BoundRollback,
    BoundUnaryOp,
    BoundUpdate,
    bind,
)
from quilldb.sql.parser import parse
from quilldb.storage.bufferpool import BufferPool
from quilldb.storage.pager import Pager
from quilldb.txn.journal import Journal
from quilldb.txn.recovery import recover_if_needed
from quilldb.txn.transaction import Transaction

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


    def __init__(self, pager: Pager, pool: BufferPool, catalog: Catalog, stats: StatisticsCatalog) -> None:
        self.pager = pager
        self.pool = pool
        self.catalog = catalog
        self.stats = stats
        self._open_cursor: Cursor | None = None
        self._closed = False
        self._txn: Transaction | None = None  # set only by an EXPLICIT BEGIN -- see _begin()


    # ---- measurement surface (chapter 19 SS19.2) -------------------------
    #
    # Page reads, not seconds. A page read here is exactly a buffer-pool
    # MISS, so the number is decided by the access path rather than by how
    # warm the cache happened to be -- which is what makes it reproducible
    # and checkable against the arithmetic by anyone reading the README.


    @property
    def pages_read(self) -> int:
        """Buffer-pool misses since the last reset_counters()."""
        return self.pool.misses


    @property
    def pages_cached(self) -> int:
        """Buffer-pool hits since the last reset_counters(). These cost no
        I/O and are deliberately NOT part of pages_read.
        """
        return self.pool.hits


    @property
    def rows_examined(self) -> int:
        """Rows pulled out of a scan operator since the last
        reset_counters() -- rows LOOKED AT, which for a SeqScan is the
        whole table however few rows come back.
        """
        return self.pool.rows_examined


    def reset_counters(self) -> None:
        """Zero the measurement counters, immediately before the statement
        being measured.
        """
        self.pool.reset_counters()


    # ---- transactions (week 5, session 4) ---------------------------
    #
    # self._txn is set ONLY by an explicit BEGIN and cleared ONLY by the
    # matching COMMIT/ROLLBACK -- an implicit (autocommit) transaction is
    # begun and finished entirely inside _run_mutation() and never touches
    # this attribute, so there is nothing here to distinguish "no
    # transaction" from "mid-autocommit": from self._txn's point of view
    # they're the same state, None.


    def _begin(self) -> None:
        """Open an explicit transaction. The BoundBegin dispatch in
        execute() calls this directly.

        Constructs the Journal and Transaction, calls journal.begin(), and
        wires the hook onto both self.pager._txn and self.pool._txn last --
        so a half-constructed Transaction is never visible through the hook
        if something above it raises.
        """
        if self._txn is not None:
            raise TransactionError("a transaction is already open")
        journal = Journal(self.pager.path)
        txn = Transaction(self.pager, self.pool, journal)
        journal.begin(self.pager.page_count)
        self.pager._txn = txn
        self.pool._txn = txn
        self._txn = txn


    def _commit(self) -> None:
        """Commit the open explicit transaction. The BoundCommit dispatch in
        execute() calls this directly.

        Unwiring the hook afterward is not optional: self._txn.commit()
        deletes the journal, so a write that slipped through the hook after
        this point would try to append to a file that no longer exists.
        """
        if self._txn is None:
            raise TransactionError("no transaction is open")
        self._txn.commit()
        self.pager._txn = None
        self.pool._txn = None
        self._txn = None


    def _rollback(self) -> None:
        """Roll back the open explicit transaction. The BoundRollback
        dispatch in execute() calls this directly. Same unwiring reasoning
        as _commit().
        """
        if self._txn is None:
            raise TransactionError("no transaction is open")
        self._txn.rollback()
        self.pager._txn = None
        self.pool._txn = None
        self._txn = None


    def _run_mutation(self, body: Callable[[], object]) -> None:
        """Run `body` (a CREATE TABLE / CREATE INDEX / INSERT / DELETE /
        UPDATE's actual work) under a transaction, autocommitting if the
        caller didn't open one explicitly.

        If self._txn is already set, an explicit transaction is open: body's
        writes ride along it, and COMMIT/ROLLBACK is the caller's job, not
        this method's. Otherwise this statement gets its own implicit
        transaction -- begin, run body, commit on success or roll back and
        re-raise on any exception.

        This is the one place that decides "does this statement get its own
        transaction, or ride an existing one" -- every mutating call site in
        execute() goes through it instead of deciding for itself, the same
        reason get_page_for_write is the one place that decides write intent.
        """
        if self._txn is not None:
            body()
            return
        self._begin()
        try:
            body()
        except BaseException:
            self._rollback()
            raise
        else:
            self._commit()


    @contextmanager
    def transaction(self) -> Generator[None]:
        """`with db.transaction():` -- commits on clean exit, rolls back on
        any exception escaping the block.

        Not built on top of _run_mutation(): the caller's block can contain
        many statements (the Week 5 contract's 10,000-row example), each of
        which will see self._txn already set and ride this same transaction
        via _run_mutation's first branch -- this method owns the
        begin/commit/rollback calls directly instead.
        """
        self._begin()
        try:
            yield
        except BaseException:
            self._rollback()
            raise
        else:
            self._commit()


    def execute(self, sql: str, parameters: Sequence[Value] = ()) -> Cursor:
        """Parse, bind, and execute one statement.


        CREATE TABLE, CREATE INDEX, INSERT, DELETE, and UPDATE complete
        before this method returns. SELECT leaves its operator open and
        streams rows through the returned Cursor. Starting another
        execute() closes any still-open result cursor on this connection;
        multiple active cursors arrive with multiple connections.
        """
        if self._closed:
            raise ValueError("connection is closed")
        if self._open_cursor is not None:
            self._open_cursor.close()
            self._open_cursor = None


        bound = bind(parse(sql), self.catalog, tuple(parameters))


        if isinstance(bound, BoundBegin):
            self._begin()
            return Cursor(None, None, 0)


        if isinstance(bound, BoundCommit):
            self._commit()
            return Cursor(None, None, 0)


        if isinstance(bound, BoundRollback):
            self._rollback()
            return Cursor(None, None, 0)


        if isinstance(bound, BoundCreateTable):
            self._run_mutation(lambda: self.catalog.create_table(bound.statement, sql))
            return Cursor(None, None, 0)


        if isinstance(bound, BoundCreateIndex):
            self._run_mutation(lambda: self.catalog.create_index(bound.statement, sql))
            return Cursor(None, None, 0)


        if isinstance(bound, BoundAnalyze):
            self.stats.analyze(bound.statement.target)
            return Cursor(None, None, 0)


        if isinstance(bound, BoundInsert):
            def _run_insert() -> None:
                with build_operator(bound, self.pager, self.pool, self.catalog) as operator:
                    operator.next()
            self._run_mutation(_run_insert)
            return Cursor(None, None, 1)


        if isinstance(bound, (BoundDelete, BoundUpdate)):
            rows_affected = 0

            def _run_delete_or_update() -> None:
                nonlocal rows_affected
                with build_operator(bound, self.pager, self.pool, self.catalog) as operator:
                    operator.next()
                    rows_affected = operator.rows_affected
            self._run_mutation(_run_delete_or_update)
            return Cursor(None, None, rows_affected)


        if isinstance(bound, BoundExplain):
            plan = build_operator(bound.select, self.pager, self.pool, self.catalog, self.stats)
            text = plan.explain(verbose=True)
            if bound.analyze:
                # Drain for real, discarding rows -- EXPLAIN ANALYZE trades
                # "free to run" for "the numbers are measured, not guessed",
                # the same tradeoff chapter 12 makes for pages_read.
                started = time.perf_counter()
                actual_rows = 0
                plan.open()
                try:
                    while plan.next() is not None:
                        actual_rows += 1
                finally:
                    plan.close()
                elapsed = time.perf_counter() - started
                text += f"\nactual_rows={actual_rows} elapsed={elapsed:.6f}s"
            result = ExplainResult((text,))
            result.open()
            cursor = Cursor(result, (("QUERY PLAN",),), -1)
            self._open_cursor = cursor
            return cursor


        operator = build_operator(bound, self.pager, self.pool, self.catalog, self.stats)
        operator.open()
        description = tuple((_display_name(e),) for e in bound.expressions)
        cursor = Cursor(operator, description, -1)
        self._open_cursor = cursor
        return cursor


    def close(self) -> None:
        """Flush every dirty page and close the file. Idempotent.

        An explicit transaction still open at this point never committed,
        so it never happened -- close() rolls it back rather than flushing
        its dirty pages, which would both violate the write barrier
        (Pager.write_page's assertion) and, if the barrier weren't there,
        silently persist uncommitted data.
        """
        if self._closed:
            return
        if self._txn is not None:
            self._rollback()
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


    # Recovery completes before the pool exists -- there is no cache to
    # invalidate because there is no cache yet (week5-transactions.md
    # SS"Where it goes in connect()").
    recover_if_needed(pager.path, pager)
    pool = BufferPool(pager)
    catalog = Catalog(pager, pool)
    catalog.load()
    stats = StatisticsCatalog(pager, pool, catalog)
    return Connection(pager, pool, catalog, stats)