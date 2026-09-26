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


import threading
import time
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Self

from quilldb.codec.record import Value
from quilldb.errors import ThreadingError, TransactionError, UnsupportedFeatureError
from quilldb.exec.operators import ExplainResult, Operator, build_operator
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
from quilldb.txn.transaction import Transaction

if TYPE_CHECKING:
    from quilldb.api.database import Database




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
        *,
        implicit_txn: Transaction | None = None,
    ) -> None:
        self._operator = operator
        self._description = description
        self._rowcount = rowcount
        self._closed = False
        # Set only for an autocommit SELECT (SS38: "autocommit still gets a
        # transaction, for the locks, not the journal"). Its SHARED lock
        # must outlive execute()'s return -- the operator streams lazily --
        # so it's released here, whenever this cursor's stream actually
        # ends, rather than by whoever called execute().
        self._implicit_txn = implicit_txn

    def _finish_implicit_txn(self) -> None:
        if self._implicit_txn is not None:
            self._implicit_txn.commit()
            self._implicit_txn = None


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
            self._finish_implicit_txn()
            raise
        if row is None:
            # Nothing left to stream: release the pins now rather than
            # waiting for an explicit close() that may never come.
            self._operator.close()
            self._operator = None
            self._finish_implicit_txn()
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
        self._finish_implicit_txn()
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
    """ONE per thread. NOT thread-safe -- never share one across threads.
    `_check_thread()` is what turns that mistake into an immediate
    `ThreadingError` instead of interleaved cursor state and half-applied
    transactions that look like a B-tree bug (week6-concurrency.md SS36).

    Owns: the current transaction, open cursors, autocommit state, and the
    id of the thread that created it. Pager/pool/catalog/lock-manager belong
    to the shared `Database` and are only referenced here.
    """


    def __init__(self, db: "Database") -> None:
        self.db = db
        self.pager = db.pager
        self.pool = db.pool
        self.catalog = db.catalog
        self.stats = db.stats
        self._owner_thread = threading.get_ident()
        self._last_seen_cookie = db.pager.schema_cookie
        self._open_cursor: Cursor | None = None
        self._closed = False
        self._txn: Transaction | None = None  # set only by an EXPLICIT BEGIN -- see _begin()
        self.busy_timeout: float = 5.0  # connection attribute, not an execute() kwarg (SS38)


    def _check_thread(self) -> None:
        """Raise ThreadingError if called from a thread other than the one
        that created this Connection.

        TODO(human): call this at the top of every public method below
        (execute, close, transaction, and the pages_read/pages_cached/
        rows_examined/reset_counters measurement surface). Deciding whether
        that's N explicit calls or one decorator over the class IS the
        session-3 exercise -- see week6-concurrency.md SS36's note on why
        this is "the highest-value ten lines in the week."
        """
        if threading.get_ident() != self._owner_thread:
            raise ThreadingError(
                f"Connection created in thread {self._owner_thread} used from "
                f"{threading.get_ident()}. Create one Connection per thread."
            )




    # ---- measurement surface (chapter 19 SS19.2) -------------------------
    #
    # Page reads, not seconds. A page read here is exactly a buffer-pool
    # MISS, so the number is decided by the access path rather than by how
    # warm the cache happened to be -- which is what makes it reproducible
    # and checkable against the arithmetic by anyone reading the README.


    @property
    def pages_read(self) -> int:
        """Buffer-pool misses since the last reset_counters()."""
        self._check_thread()
        return self.pool.misses


    @property
    def pages_cached(self) -> int:
        """Buffer-pool hits since the last reset_counters(). These cost no
        I/O and are deliberately NOT part of pages_read.
        """
        self._check_thread()
        return self.pool.hits


    @property
    def rows_examined(self) -> int:
        """Rows pulled out of a scan operator since the last
        reset_counters() -- rows LOOKED AT, which for a SeqScan is the
        whole table however few rows come back.
        """
        self._check_thread()
        return self.pool.rows_examined


    def reset_counters(self) -> None:
        """Zero the measurement counters, immediately before the statement
        being measured.
        """
        self._check_thread()
        self.pool.reset_counters()


    # ---- transactions (week 5, session 4) ---------------------------
    #
    # self._txn is set ONLY by an explicit BEGIN and cleared ONLY by the
    # matching COMMIT/ROLLBACK -- an implicit (autocommit) transaction is
    # begun and finished entirely inside _run_mutation() and never touches
    # this attribute, so there is nothing here to distinguish "no
    # transaction" from "mid-autocommit": from self._txn's point of view
    # they're the same state, None.


    def _begin(self, *, immediate: bool = False) -> None:
        """Open an explicit transaction. The BoundBegin dispatch in
        execute() calls this directly.

        Constructs a Transaction and, by default, nothing else -- it starts
        read-only (week6-concurrency.md SS37.2): no Journal, no
        pager._txn/pool._txn. Its first real write promotes it lazily,
        inside will_modify().

        `immediate=True` (BEGIN IMMEDIATE) claims the global writer lock
        right here instead of leaving that to the first write -- see
        Transaction.lock_immediate().
        """
        if self._txn is not None:
            raise TransactionError("a transaction is already open")
        txn_id = self.db.next_txn_id()
        self._txn = Transaction(
            self.pager, self.pool, self.db.lock_manager, txn_id, timeout=self.busy_timeout
        )
        if immediate:
            self._txn.lock_immediate()


    def _commit(self) -> None:
        """Commit the open explicit transaction. The BoundCommit dispatch in
        execute() calls this directly.

        Unwiring the hook afterward is not optional: self._txn.commit()
        deletes the journal, so a write that slipped through the hook after
        this point would try to append to a file that no longer exists.

        self._txn.commit() releases this transaction's locks as one of its
        last steps (Transaction.commit() step 6) -- including "__writer__" --
        before returning here. That reopens the writer queue: another
        connection's lock_for_write() can be unblocked, run, and wire
        pager._txn/pool._txn to ITSELF in the gap between that release and
        this method's own unwiring below. Only clear the hook if it's still
        pointing at this transaction; otherwise it's already someone else's
        and clearing it would unjournal their writes out from under them
        (week6-concurrency.md SS38 -- caught via a forced-interleaving repro,
        not by any timing-dependent test).
        """
        if self._txn is None:
            raise TransactionError("no transaction is open")
        self._txn.commit()
        if self.pager._txn is self._txn:
            self.pager._txn = None
        if self.pool._txn is self._txn:
            self.pool._txn = None
        self._txn = None


    def _rollback(self) -> None:
        """Roll back the open explicit transaction. The BoundRollback
        dispatch in execute() calls this directly. Same unwiring reasoning
        as _commit().

        self._txn.rollback() restores page 1 on disk and reloads the
        pager's in-memory header (its own docstring's steps 5-6), but a
        CREATE TABLE/CREATE INDEX earlier in this transaction already
        mutated the shared Catalog's in-memory _tables/_indexes directly,
        outside any journal -- that mutation is a third stale cache
        rollback() has no way to know about. Reload it here rather than
        leaving it to execute()'s lazy schema_cookie check on whatever
        statement happens to run next: anything that reads self.catalog
        (or db.catalog, shared by every other connection) before then would
        see a table that no longer exists on disk.

        Same race as _commit() (see there): self._txn.rollback() releases
        "__writer__" near its own end, which can let another connection's
        transaction claim pager._txn/pool._txn before this method reaches
        its unwiring below. Only clear the hook if it's still this
        transaction's.
        """
        if self._txn is None:
            raise TransactionError("no transaction is open")
        self._txn.rollback()
        if self.pager._txn is self._txn:
            self.pager._txn = None
        if self.pool._txn is self._txn:
            self.pool._txn = None
        self._txn = None
        self.catalog.load()
        self._last_seen_cookie = self.db.pager.schema_cookie


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


    def _read_txn(self) -> tuple[Transaction, bool]:
        """The transaction a read should lock through: the open explicit
        one, or a fresh implicit one this statement alone uses (SS38:
        "autocommit still gets a transaction -- for the locks, not the
        journal"). Cheap now that a Transaction starts read-only and only
        promotes to a real Journal on its first write (SS37.2) -- a pure
        read never reaches that promotion at all.

        Second element is True when the caller, not an explicit
        COMMIT/ROLLBACK, owns finishing it.
        """
        if self._txn is not None:
            return self._txn, False
        txn_id = self.db.next_txn_id()
        return (
            Transaction(self.pager, self.pool, self.db.lock_manager, txn_id, timeout=self.busy_timeout),
            True,
        )

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
        self._check_thread()
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
        self._check_thread()
        if self._closed:
            raise ValueError("connection is closed")
        if self._open_cursor is not None:
            self._open_cursor.close()
            self._open_cursor = None

        # TODO(human): if self.db.pager.schema_cookie has moved past
        # self._last_seen_cookie, another Connection's DDL changed the
        # shared Catalog underneath this one -- reload it
        # (self.db.catalog.load()) before binding, and update
        # self._last_seen_cookie. See week6-concurrency.md SS37.4.
        if self.db.pager.schema_cookie != self._last_seen_cookie:
            self.catalog.load()
            self._last_seen_cookie = self.db.pager.schema_cookie

        bound = bind(parse(sql), self.catalog, tuple(parameters))


        if isinstance(bound, BoundBegin):
            self._begin(immediate=bound.statement.immediate)
            return Cursor(None, None, 0)


        if isinstance(bound, BoundCommit):
            self._commit()
            return Cursor(None, None, 0)


        if isinstance(bound, BoundRollback):
            self._rollback()
            return Cursor(None, None, 0)


        if isinstance(bound, BoundCreateTable):
            # CREATE TABLE writes a new sqlite_schema row into page 1, which
            # always exists (page_id 1 <= any transaction's page_count_before)
            # -- an EXISTING page, so it needs the same lock_for_write() that
            # promotes this transaction to journalling (SS37.2) that
            # SeqScan/Insert/etc. get from inside their own open(). Nothing
            # analogous to an operator's open() exists for DDL, so the call
            # goes here instead, locking the table's own (not-yet-existing)
            # name -- a second CREATE TABLE of the same name from another
            # connection correctly queues behind this one.
            def _run_create_table() -> None:
                assert self._txn is not None
                self._txn.lock_for_write(bound.statement.name)
                self.catalog.create_table(bound.statement, sql)
            self._run_mutation(_run_create_table)
            return Cursor(None, None, 0)


        if isinstance(bound, BoundCreateIndex):
            def _run_create_index() -> None:
                assert self._txn is not None
                self._txn.lock_for_write(bound.statement.table)
                self.catalog.create_index(bound.statement, sql)
            self._run_mutation(_run_create_index)
            return Cursor(None, None, 0)


        if isinstance(bound, BoundAnalyze):
            self.stats.analyze(bound.statement.target)
            return Cursor(None, None, 0)


        if isinstance(bound, BoundInsert):
            def _run_insert() -> None:
                with build_operator(bound, self.pager, self.pool, self.catalog, txn=self._txn) as operator:
                    operator.next()
            self._run_mutation(_run_insert)
            return Cursor(None, None, 1)


        if isinstance(bound, (BoundDelete, BoundUpdate)):
            rows_affected = 0

            def _run_delete_or_update() -> None:
                nonlocal rows_affected
                with build_operator(bound, self.pager, self.pool, self.catalog, txn=self._txn) as operator:
                    operator.next()
                    rows_affected = operator.rows_affected
            self._run_mutation(_run_delete_or_update)
            return Cursor(None, None, rows_affected)


        if isinstance(bound, BoundExplain):
            read_txn, owns_txn = self._read_txn()
            try:
                plan = build_operator(bound.select, self.pager, self.pool, self.catalog, self.stats, read_txn)
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
            finally:
                if owns_txn:
                    read_txn.commit()
            result = ExplainResult((text,))
            result.open()
            cursor = Cursor(result, (("QUERY PLAN",),), -1)
            self._open_cursor = cursor
            return cursor


        read_txn, owns_txn = self._read_txn()
        operator = build_operator(bound, self.pager, self.pool, self.catalog, self.stats, read_txn)
        try:
            operator.open()
        except BaseException:
            if owns_txn:
                read_txn.rollback()
            raise
        description = tuple((_display_name(e),) for e in bound.expressions)
        cursor = Cursor(operator, description, -1, implicit_txn=read_txn if owns_txn else None)
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
        self._check_thread()
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
    """Open an existing database or create a new one, returning a single
    Connection bound to the calling thread.

    A thin wrapper over Database (api/database.py), which is where the
    pager/pool/catalog/lock-manager actually live as of week 6 session 3 --
    this keeps every earlier week's single-Connection call sites unchanged.
    A second thread wanting its own Connection on the same file should call
    `.db.connect()` on one already open, not this function again.
    """
    from quilldb.api.database import open_database

    return open_database(path).connect()