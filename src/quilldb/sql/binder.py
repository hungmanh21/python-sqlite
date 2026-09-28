"""Name resolution, parameter substitution, and declared-type checks.


Parsing answers "is this grammatical?"; binding answers "does this mean
anything in this database?" (docs/implementation/week-3-sql.md §17). By the
time a BoundStatement reaches exec/, every column name is an integer index
and every `?` is a concrete value -- the executor never compares a name or
counts a parameter, because those answers can't change mid-scan and so are
hoisted out of the per-row loop entirely.


Two properties this module exists to guarantee:


1. NOTHING TOUCHES DISK HERE. bind() takes a SchemaSource (just
   get_table()), not a Pager or BufferPool -- so an unknown table or
   column provably cannot open a cursor or dirty a page, because this
   module has no way to do either. That's why the interface is a Protocol
   and not Catalog: the narrow type IS the guarantee.


2. The Bound* tree is a SEPARATE type hierarchy from ast.py's, not the
   same nodes mutated. evaluate() will be typed to accept BoundExpression,
   so handing it an unresolved ast.Column is a type error at the call site
   rather than an AttributeError on row 40,000.
"""


from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from quilldb.catalog.schema import TableSchema
from quilldb.codec.record import Value
from quilldb.errors import (
    AggregateError,
    AmbiguousColumnError,
    ColumnCountError,
    ColumnNotFoundError,
    ParameterCountError,
    TypeMismatchError,
    UnsupportedFeatureError,
)
from quilldb.sql.ast import (
    Analyze,
    Begin,
    BinaryOp,
    Column,
    Commit,
    CreateIndex,
    CreateTable,
    DataType,
    Delete,
    Explain,
    Expression,
    FunctionCall,
    Insert,
    IsNull,
    Literal,
    OrderKey,
    Parameter,
    Rollback,
    Select,
    Statement,
    UnaryOp,
    Update,
)

# The set of function names bind_aggregate_select recognizes as aggregates.
# Deliberately NOT imported from exec/aggregate.py's AGGREGATES dict (minus
# "count_star", which is never a source-level name -- COUNT(*) reaches it
# through FunctionCall.star, not through a function named "count_star"):
# sql/ sits below exec/ in the layering (CLAUDE.md's architecture table),
# so binder.py cannot depend on the executor package without inverting it.
_AGGREGATE_FUNCTIONS = frozenset({"count", "sum", "avg", "min", "max"})


class SchemaSource(Protocol):
    """The only thing binding needs from a catalog: name -> TableSchema.


    Catalog satisfies this structurally. Declaring the dependency this
    narrowly is what lets binder tests run with no Pager, no BufferPool,
    and no temp file -- and what makes "binding cannot touch storage" a
    fact about the types rather than a convention.
    """


    def get_table(self, name: str) -> TableSchema: ...




@dataclass(frozen=True)
class BoundLiteral:
    value: Value




@dataclass(frozen=True)
class BoundColumn:
    index: int
    name: str
    data_type: DataType
    table_ordinal: int = 0
    """Which table in the FROM clause this column came from -- 0 for every
    single-table query (the only kind that existed before week 7), and the
    table's position in `Select.joins`-plus-the-base-table for a join.

    Defaulting to 0 is what keeps every pre-week-7 call site (single-table
    SELECT/DELETE/UPDATE, and every test that builds a BoundColumn by hand)
    working unchanged: they never had a second table to distinguish, so
    they never have to spell this field out.

    `index` is meaningful only *within* that table's own row until
    resolve_layout() rewrites it to a flat offset for a chosen join order --
    see the module-level resolve_layout() docstring for why that has to be
    a separate pass rather than something the binder computes up front.
    """




@dataclass(frozen=True)
class BoundUnaryOp:
    operator: str
    operand: "BoundExpression"




@dataclass(frozen=True)
class BoundBinaryOp:
    left: "BoundExpression"
    operator: str
    right: "BoundExpression"




@dataclass(frozen=True)
class BoundIsNull:
    operand: "BoundExpression"
    negated: bool = False




type BoundExpression = BoundLiteral | BoundColumn | BoundUnaryOp | BoundBinaryOp | BoundIsNull




@dataclass(frozen=True)
class BoundOrderKey:
    """One resolved `ORDER BY` key: a position in the row exec/sort.py's
    Sort will receive, plus direction. `index` is NOT a BoundColumn --
    Sort reads a row that's already been projected (BoundSelect.expressions
    or BoundAggregateSelect.select_items, PLUS `hidden_order_by`), so all
    it ever needs is "column N of that flat row", never a fresh name
    lookup against a table.
    """

    index: int
    descending: bool = False




@dataclass(frozen=True)
class BoundCreateTable:
    statement: CreateTable
    """CREATE TABLE needs no resolution -- it INTRODUCES names rather than
    referring to them, so there is nothing to look up and the statement
    passes through unchanged. The duplicate-column check already happened
    in the parser; the reserved-prefix and already-exists checks belong to
    Catalog.create_table(), which is the thing that can actually see what
    exists on disk.
    """




@dataclass(frozen=True)
class BoundCreateIndex:
    statement: CreateIndex
    """A passthrough wrapper too, for the same underlying reason as
    BoundCreateTable, even though CREATE INDEX's `table`/`columns` DO refer
    to something that must already exist: Catalog.create_index() is what
    can see the table's actual schema on disk, and it needs the raw names
    anyway to run the backfill, so resolving them here first would just be
    duplicated work with nowhere to put the result (there's no per-row
    executor downstream the way BoundInsert/BoundSelect have -- the
    backfill loop lives entirely inside create_index()).
    """




@dataclass(frozen=True)
class BoundInsert:
    table: TableSchema
    values: tuple[Value, ...]




@dataclass(frozen=True)
class BoundSelect:
    table: TableSchema
    expressions: tuple[BoundExpression, ...]
    where: BoundExpression | None
    distinct: bool = False
    order_by: tuple[BoundOrderKey, ...] = ()
    hidden_order_by: tuple[BoundExpression, ...] = ()
    """Extra columns Project must compute so Sort can read them, beyond
    what the query actually selected -- `SELECT name FROM t ORDER BY age`
    needs `age` in the row Sort sorts, but not in the row the caller gets
    back. `hidden_order_by` entries are appended AFTER `expressions` in
    the row build_operator()'s Project produces; a BoundOrderKey.index
    into that combined row may therefore land past `len(expressions)`,
    at which point a final strip (another Project, over just the first
    len(expressions) positions) removes them again -- see
    _resolve_order_by_key for how one ORDER BY item decides which case
    it's in.
    """
    limit: int | None = None
    offset: int = 0




@dataclass(frozen=True)
class TableScope:
    """One table visible to name resolution inside a joined SELECT: its
    position in the FROM clause, the name column references key on (the
    alias if the query gave one, else the table name itself), and its
    schema.

    A plain list[TableScope] rather than a dict keyed by alias: aliases
    aren't required to be unique here (real SQL rejects `FROM t, t`, this
    binder doesn't yet), and resolve_column needs to walk every scope
    anyway to detect ambiguity, not just do one dict lookup.
    """

    ordinal: int
    alias: str
    table: TableSchema


@dataclass(frozen=True)
class BoundJoin:
    join_type: str  # "INNER" | "LEFT"
    table_ordinal: int
    on: "BoundExpression | None"
    """None only for a comma join (JoinClause.on was None) -- an inner join
    with no condition, same as real SQL.
    """


@dataclass(frozen=True)
class BoundJoinSelect:
    """A SELECT with at least one JOIN or comma join.

    Deliberately a separate type from BoundSelect rather than BoundSelect
    growing an optional `joins` field: NestedLoopJoin (exec/join.py) doesn't
    exist until session 2, so nothing downstream can execute this yet.
    Keeping it a distinct type means a single-table SELECT's bind path -- and
    everything the week 3-6 executor and planner already do with it -- is
    untouched by this session, instead of silently gaining an unused field.
    """

    scopes: tuple[TableScope, ...]
    joins: tuple[BoundJoin, ...]
    expressions: tuple[BoundExpression, ...]
    where: BoundExpression | None


@dataclass(frozen=True)
class BoundAggregate:
    """One aggregate call from a SELECT list: which fold to run
    (exec/aggregate.py's AGGREGATES key) and the already-bound expression
    to feed it each row, or None only for `count_star` -- COUNT(*) counts
    rows, not values, so it has nothing to evaluate() per row.
    """

    func: str  # "count_star" | "count" | "sum" | "avg" | "min" | "max"
    arg: BoundExpression | None


@dataclass(frozen=True)
class BoundAggregateSelect:
    """A SELECT that groups: either it has a GROUP BY clause, or its
    select list contains at least one aggregate call. Deliberately a
    separate type from BoundSelect, for the same reason BoundJoinSelect
    is one: a BoundAggregate cannot be evaluate()d per row the way every
    BoundExpression can -- it needs the whole stream of rows in a group,
    not one row at a time -- so it isn't a BoundExpression, and folding
    it into BoundSelect.expressions would make every existing
    evaluate()/resolve_layout() caller handle a shape it fundamentally
    can't.

    `where` filters rows BEFORE grouping, against `table`'s own row shape
    -- exactly like a plain SELECT's where. `group_by`, `select_items`,
    and `having` all operate AFTER grouping, against the FLAT row one
    finished group produces: `(*group_by values in order, *aggregates
    values in order)`. That flat row is exec/aggregate.py's HashAggregate
    output shape, session 4's "hidden columns" pipeline: `select_items`
    and `having` are ordinary BoundExpression trees over it, with a
    BoundColumn's `index` pointing at a position in THAT row rather than
    `table`'s -- which is what lets build_operator() finish this query
    with the same Project/Filter operators a plain SELECT already uses,
    instead of a special-cased evaluator (see _bind_group_output).

    `aggregates` is every aggregate call reachable from `select_items` or
    `having`, collected in the order first seen, with no deduplication --
    `SELECT COUNT(*) FROM t HAVING COUNT(*) > 1` binds two BoundAggregates
    even though they're the same call, one for the slot select_items[0]
    reads and one for the slot having reads. Simpler than deduplicating,
    and correctness doesn't depend on it: HashAggregate folds every entry
    in `aggregates` regardless of whether two entries happen to compute
    the same thing.

    `labels` is Cursor.description's column names, computed once here
    from the ORIGINAL (unbound) select-list expressions -- by the time an
    aggregate call is rewritten into a flat-row BoundColumn slot, its
    function name and argument are gone, so there is nowhere left to
    recover "COUNT(*)" or "MIN(id)" from downstream.
    """

    table: TableSchema
    group_by: tuple[BoundExpression, ...]
    aggregates: tuple[BoundAggregate, ...]
    select_items: tuple[BoundExpression, ...]
    having: BoundExpression | None
    where: BoundExpression | None
    labels: tuple[str, ...]
    order_by: tuple[BoundOrderKey, ...] = ()
    hidden_order_by: tuple[BoundExpression, ...] = ()
    """BoundSelect.hidden_order_by's twin for the grouped case: an ORDER
    BY item naming a fresh aggregate call nothing else selected (`ORDER BY
    SUM(x)` with no SUM(x) in the select list or HAVING) still has to be
    folded by HashAggregate, so it's bound through _bind_group_output --
    exactly like `having` is -- and appended here rather than to
    `select_items`, so it never reaches the caller's row.
    """
    limit: int | None = None
    offset: int = 0


@dataclass(frozen=True)
class BoundDelete:
    table: TableSchema
    where: BoundExpression | None
    """Unlike BoundCreateTable/BoundCreateIndex, this one DOES resolve:
    `where` is bound against `table` through the exact same _expression()
    call BoundSelect.where uses, since a DELETE's predicate is evaluated
    per row exactly the way a SELECT's is -- the only difference is what
    happens to a row that passes it.
    """




@dataclass(frozen=True)
class BoundAssignment:
    column_index: int
    value: BoundExpression
    """`value` is bound with the general _expression() resolver, not
    INSERT's narrower _constant() -- UPDATE's SET clause is allowed to
    reference the row being updated (`SET age = age + 1`), which only
    makes sense once there is a row to evaluate against at exec time.
    INSERT has no row yet, which is exactly why it's restricted to
    constants.
    """




@dataclass(frozen=True)
class BoundUpdate:
    table: TableSchema
    assignments: tuple[BoundAssignment, ...]
    where: BoundExpression | None




@dataclass(frozen=True)
class BoundAnalyze:
    statement: Analyze
    """A passthrough wrapper, same reasoning as BoundCreateTable/
    BoundCreateIndex: `target` names a table (or is None, for "every
    table"), and whether that name actually resolves is a question only
    StatisticsCatalog.analyze() can answer -- it already has to walk
    catalog.list_tables()/get_table() itself, so resolving `target` here
    first would just be duplicated work with nowhere to put the result.
    """




@dataclass(frozen=True)
class BoundExplain:
    select: BoundSelect
    analyze: bool
    """Unlike BoundCreateTable/BoundAnalyze, this DOES resolve: the inner
    SELECT is bound through the same bind_select() a bare SELECT uses, so
    an EXPLAIN of an unknown table/column fails at bind time exactly like
    the SELECT it wraps would -- there's no reason EXPLAIN should be more
    forgiving about names than the query it's explaining.
    """




@dataclass(frozen=True)
class BoundBegin:
    statement: Begin


@dataclass(frozen=True)
class BoundCommit:
    statement: Commit


@dataclass(frozen=True)
class BoundRollback:
    statement: Rollback


type BoundStatement = (
    BoundCreateTable
    | BoundCreateIndex
    | BoundInsert
    | BoundSelect
    | BoundJoinSelect
    | BoundAggregateSelect
    | BoundDelete
    | BoundUpdate
    | BoundAnalyze
    | BoundExplain
    | BoundBegin
    | BoundCommit
    | BoundRollback
)


def resolve_layout(expr: BoundExpression, offsets: dict[int, int]) -> BoundExpression:
    """Rewrite every (table_ordinal, column) reference in `expr` to a flat
    row index, for the join order the planner actually chose.

    `offsets[table_ordinal]` is where that table's columns start in the
    concatenated row a NestedLoopJoin produces; `expr` came out of
    bind_join_select with `index` still meaning "position within its own
    table's row" (table_ordinal names which table). This can't happen at
    bind time -- the planner (week7-query-processing.md §43) may reorder the
    joined tables *after* binding to get a cheaper plan, so the binder
    cannot yet know which offset any given table will land at.

    One rewrite pass handles every intermediate layout a multi-table plan
    produces; a canonical layout chosen up front doesn't, because an ON
    predicate evaluated after only some tables are assembled sees a row
    with just those tables in it, at whatever offsets *that* partial plan
    uses.
    """
    if isinstance(expr, BoundColumn):
        return BoundColumn(offsets[expr.table_ordinal] + expr.index, expr.name, expr.data_type)

    if isinstance(expr, BoundUnaryOp):
        return BoundUnaryOp(expr.operator, resolve_layout(expr.operand, offsets))

    if isinstance(expr, BoundBinaryOp):
        return BoundBinaryOp(
            resolve_layout(expr.left, offsets), expr.operator, resolve_layout(expr.right, offsets)
        )

    if isinstance(expr, BoundIsNull):
        return BoundIsNull(resolve_layout(expr.operand, offsets), expr.negated)

    return expr  # BoundLiteral: nothing to rewrite


def _select_needs_aggregation(statement: Select) -> bool:
    """True when this Select must route through bind_aggregate_select()
    rather than the plain single-table bind_select(): either it has a
    GROUP BY clause, or (session 3's original check) a top-level
    select-list expression is a call to a known aggregate function.

    GROUP BY alone routes here even with no aggregate call anywhere --
    `SELECT region FROM t GROUP BY region` has nothing to fold, but it
    still has to deduplicate by key instead of running through the plain
    Project every non-aggregate SELECT uses.

    Only checks the TOP level of the select list for the aggregate-call
    case: `SUM(a) + 1` (an aggregate nested inside a larger expression) is
    not detected here and falls through to bind_select(), where a bare
    FunctionCall node has no handler and raises UnsupportedFeatureError --
    not yet supported, and a session-3 scope cut rather than a silent
    wrong answer.
    """
    if statement.group_by:
        return True
    if statement.expressions is None:
        return False  # SELECT * can never be an aggregate select
    return any(
        isinstance(e, FunctionCall) and e.name.casefold() in _AGGREGATE_FUNCTIONS
        for e in statement.expressions
    )


def _select_item_label(expression: Expression) -> str:
    """The Cursor.description label for one top-level aggregate-select-list
    or HAVING expression, computed from the UNBOUND AST -- mirrors
    api/connection.py's _display_name closely enough that a plain SELECT
    and an aggregate SELECT produce the same-shaped labels, but has to
    live here rather than there: by the time _bind_group_output rewrites
    an aggregate call into a flat-row BoundColumn slot, its function name
    and argument are gone (BoundAggregateSelect's own docstring).
    """
    if isinstance(expression, FunctionCall):
        if expression.star:
            return f"{expression.name.upper()}(*)"
        args = ", ".join(_select_item_label(arg) for arg in expression.args)
        return f"{expression.name.upper()}({args})"
    if isinstance(expression, Column):
        return expression.name
    if isinstance(expression, Literal):
        return repr(expression.value)
    if isinstance(expression, UnaryOp):
        return f"{expression.operator}{_select_item_label(expression.operand)}"
    if isinstance(expression, BinaryOp):
        return f"{_select_item_label(expression.left)} {expression.operator} {_select_item_label(expression.right)}"
    if isinstance(expression, IsNull):
        suffix = "IS NOT NULL" if expression.negated else "IS NULL"
        return f"{_select_item_label(expression.operand)} {suffix}"
    return type(expression).__name__


def _describe(expression: Expression) -> str:
    """A short, human-readable label for an unbound AST expression, for
    AggregateError's message -- runs before any name resolution, so this
    can't reuse api/connection.py's _display_name (built for BoundExpression).
    """
    if isinstance(expression, Column):
        return f"{expression.table}.{expression.name}" if expression.table else expression.name
    if isinstance(expression, FunctionCall):
        return f"{expression.name}(*)" if expression.star else f"{expression.name}(...)"
    return type(expression).__name__


def bind(
    statement: Statement,
    catalog: SchemaSource,
    parameters: tuple[Value, ...] = (),
) -> BoundStatement:
    """Resolve all names and replace every Parameter with a concrete value.


    Args:
        statement: the parsed statement, straight from parse().
        catalog: anything that can resolve a table name (see SchemaSource).
        parameters: the caller's positional `?` values, in order.
    Returns:
        A BoundStatement whose every column reference is an index and whose
        every parameter has been substituted.
    Raises:
        TableNotFoundError: propagated from catalog.get_table().
        ColumnNotFoundError: propagated from TableSchema.column_index(), or
            from resolve_column() for a joined SELECT.
        AmbiguousColumnError: a joined SELECT's unqualified column name
            matches more than one table in scope.
        AggregateError: an aggregate/GROUP BY SELECT's select list or
            HAVING clause references a column that is neither an aggregate
            call nor one of the GROUP BY keys (bind_aggregate_select's
            _bind_group_output), `SELECT *` is combined with GROUP BY or an
            aggregate call, or an aggregate call is malformed (COUNT(*)-only
            star usage on a non-COUNT function, wrong argument count).
        ColumnCountError: an INSERT supplied a different number of values
            than the table has columns.
        ParameterCountError: the statement's `?` count and len(parameters)
            disagree -- in either direction. Every supplied parameter must
            be consumed, so passing extras is an error rather than being
            silently ignored.
        TypeMismatchError: a supplied parameter is not a storable Value, or
            an INSERT value is incompatible with its column's declared type
            (see _require_bindable and _coerce_to_declared_type).
        UnsupportedFeatureError: an INSERT value is not a constant
            expression, or a statement type this binder doesn't handle.
    """
    _require_bindable(parameters)
    binder = _Binder(catalog, parameters)


    if isinstance(statement, CreateTable):
        bound: BoundStatement = BoundCreateTable(statement)
    elif isinstance(statement, CreateIndex):
        bound = BoundCreateIndex(statement)
    elif isinstance(statement, Insert):
        bound = binder.bind_insert(statement)
    elif isinstance(statement, Select):
        if statement.joins:
            bound = binder.bind_join_select(statement)
        elif _select_needs_aggregation(statement):
            bound = binder.bind_aggregate_select(statement)
        else:
            bound = binder.bind_select(statement)
    elif isinstance(statement, Delete):
        bound = binder.bind_delete(statement)
    elif isinstance(statement, Update):
        bound = binder.bind_update(statement)
    elif isinstance(statement, Analyze):
        bound = BoundAnalyze(statement)
    elif isinstance(statement, Explain):
        if statement.statement.joins:
            raise UnsupportedFeatureError("EXPLAIN of a joined SELECT is not supported yet")
        bound = BoundExplain(binder.bind_select(statement.statement), statement.analyze)
    elif isinstance(statement, Begin):
        bound = BoundBegin(statement)
    elif isinstance(statement, Commit):
        bound = BoundCommit(statement)
    elif isinstance(statement, Rollback):
        bound = BoundRollback(statement)
    else:
        raise UnsupportedFeatureError(f"cannot bind a {type(statement).__name__} statement")


    binder.require_all_parameters_consumed()
    return bound




class _Binder:
    """Carries the two things every recursive step needs: where to resolve
    names, and which parameters have been consumed so far.
    """


    def __init__(self, catalog: SchemaSource, parameters: tuple[Value, ...]) -> None:
        self.catalog = catalog
        self.parameters = parameters
        self.consumed: set[int] = set()


    def bind_insert(self, statement: Insert) -> BoundInsert:
        table = self.catalog.get_table(statement.table)


        if len(statement.values) != len(table.columns):
            raise ColumnCountError(
                f"table {table.name!r} has {len(table.columns)} columns "
                f"but {len(statement.values)} values were supplied"
            )


        values = tuple(
            _coerce_to_declared_type(self._constant(value), column.data_type, table.name, column.name)
            for value, column in zip(statement.values, table.columns, strict=True)
        )
        return BoundInsert(table, values)


    def bind_select(self, statement: Select) -> BoundSelect:
        """The single-table path, unchanged since week 3. A qualifier on a
        column reference (`t.id` rather than `id`) is not checked against
        `statement.table`'s alias here -- with exactly one table in scope
        there's nothing for it to disambiguate, so it's accepted the same as
        the bare name. bind_join_select's resolve_column is where a
        qualifier actually has to mean something.
        """
        table = self.catalog.get_table(statement.table.name)


        if statement.expressions is None:
            # SELECT * -- only resolvable here, because only here is there a
            # catalog to say how many columns there are.
            expressions: tuple[BoundExpression, ...] = tuple(
                BoundColumn(index, column.name, column.data_type)
                for index, column in enumerate(table.columns)
            )
        else:
            expressions = tuple(self._expression(e, table) for e in statement.expressions)


        where = None if statement.where is None else self._expression(statement.where, table)

        order_by, hidden_order_by = self._bind_order_by(
            statement.order_by, expressions, lambda e: self._expression(e, table)
        )
        if statement.distinct and hidden_order_by:
            raise UnsupportedFeatureError(
                "DISTINCT combined with an ORDER BY expression outside the select list is not supported yet"
            )
        limit = self._bind_limit(statement.limit)
        offset = self._bind_offset(statement.offset)

        return BoundSelect(table, expressions, where, statement.distinct, order_by, hidden_order_by, limit, offset)


    def bind_join_select(self, statement: Select) -> BoundJoinSelect:
        """The multi-table path: at least one JOIN or comma join.

        Every column reference resolves through `resolve_column` against
        `scopes` rather than through a single TableSchema.column_index() --
        the whole reason TableScope/BoundColumn.table_ordinal exist. The ON
        expression of a later join is bound against every *earlier* scope
        plus its own table, matching what a real join actually has in hand
        when it evaluates that condition: by the time join k runs, tables
        0..k are already assembled into one row, and it's only checking
        that its own new table fits.

        `statement.distinct`/`group_by`/`having`/`order_by`/`limit`/`offset`
        are rejected rather than silently dropped: BoundJoinSelect has none
        of those fields, and this method never looks at them below, so an
        unguarded `SELECT DISTINCT ... FROM a JOIN b` or `... FROM a JOIN b
        GROUP BY ...` would parse and bind cleanly but quietly return every
        unaggregated joined row -- the exact "binder changes results"
        failure this codebase's error policy exists to avoid. A select
        list with a top-level aggregate call still gets caught downstream
        (`_join_expression` has no FunctionCall case), but GROUP BY/HAVING
        with no such call, or a bare DISTINCT/ORDER BY/LIMIT, would
        otherwise sail through unnoticed (same reasoning as bind_aggregate_select's own
        DISTINCT check).
        """
        if statement.distinct:
            raise UnsupportedFeatureError("DISTINCT combined with a JOIN is not supported yet")
        if statement.group_by:
            raise UnsupportedFeatureError("GROUP BY combined with a JOIN is not supported yet")
        if statement.having is not None:
            raise UnsupportedFeatureError("HAVING combined with a JOIN is not supported yet")
        if statement.order_by:
            raise UnsupportedFeatureError("ORDER BY combined with a JOIN is not supported yet")
        if statement.limit is not None:
            raise UnsupportedFeatureError("LIMIT combined with a JOIN is not supported yet")
        if statement.offset is not None:
            raise UnsupportedFeatureError("OFFSET combined with a JOIN is not supported yet")

        scopes = self._build_scopes(statement)

        joins = []
        for join, scope in zip(statement.joins, scopes[1:], strict=True):
            visible = scopes[: scope.ordinal + 1]
            on = None if join.on is None else self._join_expression(join.on, visible)
            joins.append(BoundJoin(join.join_type, scope.ordinal, on))

        if statement.expressions is None:
            expressions: tuple[BoundExpression, ...] = tuple(
                BoundColumn(index, column.name, column.data_type, scope.ordinal)
                for scope in scopes
                for index, column in enumerate(scope.table.columns)
            )
        else:
            expressions = tuple(self._join_expression(e, scopes) for e in statement.expressions)

        where = None if statement.where is None else self._join_expression(statement.where, scopes)
        return BoundJoinSelect(tuple(scopes), tuple(joins), expressions, where)

    def _build_scopes(self, statement: Select) -> list[TableScope]:
        refs = (statement.table, *(join.table for join in statement.joins))
        return [
            TableScope(ordinal, ref.alias or ref.name, self.catalog.get_table(ref.name))
            for ordinal, ref in enumerate(refs)
        ]

    def _join_expression(self, expression: Expression, scopes: list[TableScope]) -> BoundExpression:
        """`_expression`'s twin for the multi-table case: same tree shape,
        the only difference is how a bare Column resolves -- through
        `resolve_column` against every scope in play instead of a single
        table's column_index().
        """
        if isinstance(expression, Literal):
            return BoundLiteral(expression.value)

        if isinstance(expression, Parameter):
            return BoundLiteral(self._parameter(expression.index))

        if isinstance(expression, Column):
            return self.resolve_column(expression.name, expression.table, scopes)

        if isinstance(expression, UnaryOp):
            return BoundUnaryOp(expression.operator, self._join_expression(expression.operand, scopes))

        if isinstance(expression, BinaryOp):
            return BoundBinaryOp(
                self._join_expression(expression.left, scopes),
                expression.operator,
                self._join_expression(expression.right, scopes),
            )

        if isinstance(expression, IsNull):
            return BoundIsNull(self._join_expression(expression.operand, scopes), expression.negated)

        raise UnsupportedFeatureError(f"cannot bind a {type(expression).__name__} expression")

    def resolve_column(self, name: str, qualifier: str | None, scopes: list[TableScope]) -> BoundColumn:
        """Resolve a (possibly qualified) column reference against the
        tables in scope.

        `u.id`  -> qualifier="u": find the scope whose alias/name is "u"
                   (case-insensitively, matching TableSchema.column_index's
                   own convention), then look up "id" only in that table.
        `id`    -> qualifier=None: search every scope. Exactly one table may
                   have a column named "id" -- more than one is an error,
                   not a silent pick of the first match. Silently picking
                   one is how a join returns a plausible-looking wrong
                   answer instead of failing loudly (week7-query-processing.md
                   §40, and errors.AmbiguousColumnError's docstring).

        Raises:
            ColumnNotFoundError: qualifier names no scope, or the resolved
                table(s) have no such column.
            AmbiguousColumnError: an unqualified name matches columns in
                more than one scope.
        """
        if qualifier is not None:
            folded = qualifier.casefold()
            scope = next((s for s in scopes if s.alias.casefold() == folded), None)
            if scope is None:
                raise ColumnNotFoundError(f"no such table: {qualifier!r}")
            index = scope.table.column_index(name)  # raises ColumnNotFoundError
            column = scope.table.columns[index]
            return BoundColumn(index, column.name, column.data_type, scope.ordinal)

        matches: list[tuple[TableScope, int]] = []
        for scope in scopes:
            try:
                matches.append((scope, scope.table.column_index(name)))
            except ColumnNotFoundError:
                continue

        if not matches:
            raise ColumnNotFoundError(f"no such column: {name!r}")
        if len(matches) > 1:
            tables = ", ".join(scope.alias for scope, _ in matches)
            raise AmbiguousColumnError(f"column {name!r} is ambiguous: present in {tables}")

        scope, index = matches[0]
        column = scope.table.columns[index]
        return BoundColumn(index, column.name, column.data_type, scope.ordinal)

    def bind_aggregate_select(self, statement: Select) -> BoundAggregateSelect:
        """The grouping path: a GROUP BY clause, at least one aggregate
        call in the select list, or both.

        `where` binds against `table`'s own row, exactly like bind_select's
        -- filtering happens before any grouping, same as real SQL. Every
        GROUP BY key expression binds the same way (a key is not allowed to
        contain an aggregate call: `_expression` has no FunctionCall case,
        so `GROUP BY COUNT(*)` surfaces as UnsupportedFeatureError, which is
        the right rejection even if not the most specific message).

        `select_items` and `having` are rewritten by _bind_group_output
        into BoundExpression trees over the FLAT post-grouping row
        (BoundAggregateSelect's own docstring) -- that's where every
        aggregate call actually gets collected into `aggregates`, and
        where a bare column is checked against `group_by`
        (_group_by_key_index).
        """
        if statement.expressions is None:
            raise AggregateError("SELECT * cannot be combined with GROUP BY or an aggregate function")
        if statement.distinct:
            raise UnsupportedFeatureError("DISTINCT combined with GROUP BY or an aggregate is not supported yet")

        table = self.catalog.get_table(statement.table.name)
        where = None if statement.where is None else self._expression(statement.where, table)
        group_by = tuple(self._expression(e, table) for e in statement.group_by)

        aggregates: list[BoundAggregate] = []
        select_items = tuple(
            self._bind_group_output(e, table, group_by, aggregates) for e in statement.expressions
        )
        having = (
            None
            if statement.having is None
            else self._bind_group_output(statement.having, table, group_by, aggregates)
        )
        labels = tuple(_select_item_label(e) for e in statement.expressions)

        order_by, hidden_order_by = self._bind_order_by(
            statement.order_by, select_items, lambda e: self._bind_group_output(e, table, group_by, aggregates)
        )
        limit = self._bind_limit(statement.limit)
        offset = self._bind_offset(statement.offset)

        return BoundAggregateSelect(
            table,
            group_by,
            tuple(aggregates),
            select_items,
            having,
            where,
            labels,
            order_by,
            hidden_order_by,
            limit,
            offset,
        )

    def _bind_group_output(
        self,
        expression: Expression,
        table: TableSchema,
        group_by: tuple[BoundExpression, ...],
        aggregates: list[BoundAggregate],
    ) -> BoundExpression:
        """Rewrite one select-list or HAVING expression into a
        BoundExpression over the flat post-grouping row
        `(*group_by values, *aggregates values)`, collecting every
        aggregate call it contains into `aggregates` as it goes.

        An aggregate call becomes a BoundColumn pointing at the slot its
        own position in `aggregates` will land at once HashAggregate has
        run -- `len(group_by) + <its index>`, since every group key comes
        first in the flat row. A bare column has to be one of the GROUP BY
        keys (_group_by_key_index); anything else -- SUM(a)+1's outer `+`,
        for instance -- recurses structurally, same shape as _expression.
        """
        if isinstance(expression, FunctionCall) and expression.name.casefold() in _AGGREGATE_FUNCTIONS:
            aggregates.append(self._bind_aggregate_call(expression, table))
            slot = len(group_by) + len(aggregates) - 1
            return BoundColumn(slot, _select_item_label(expression), DataType.INTEGER)

        if isinstance(expression, Literal):
            return BoundLiteral(expression.value)

        if isinstance(expression, Parameter):
            return BoundLiteral(self._parameter(expression.index))

        if isinstance(expression, Column):
            # With zero GROUP BY keys there is nothing an unmatched column
            # could ever match -- session 3's original rule, unconditional
            # and independent of _group_by_key_index below, which is only
            # ever reached once group_by is non-empty.
            if not group_by:
                raise AggregateError(
                    f"'{_describe(expression)}' is neither an aggregate function nor part of a GROUP BY"
                )
            index = self._group_by_key_index(expression, table, group_by)
            if index is None:
                raise AggregateError(
                    f"'{_describe(expression)}' is neither an aggregate function nor part of a GROUP BY"
                )
            column = table.columns[table.column_index(expression.name)]
            return BoundColumn(index, column.name, column.data_type)

        if isinstance(expression, UnaryOp):
            return BoundUnaryOp(
                expression.operator, self._bind_group_output(expression.operand, table, group_by, aggregates)
            )

        if isinstance(expression, BinaryOp):
            return BoundBinaryOp(
                self._bind_group_output(expression.left, table, group_by, aggregates),
                expression.operator,
                self._bind_group_output(expression.right, table, group_by, aggregates),
            )

        if isinstance(expression, IsNull):
            return BoundIsNull(
                self._bind_group_output(expression.operand, table, group_by, aggregates), expression.negated
            )

        raise UnsupportedFeatureError(f"cannot bind a {type(expression).__name__} expression in an aggregate SELECT")

    def _group_by_key_index(
        self, expression: Column, table: TableSchema, group_by: tuple[BoundExpression, ...]
    ) -> int | None:
        """Return the position of `expression`, bound against `table`,
        within `group_by` -- or None if it isn't one of the GROUP BY keys
        at all.

        This is validate_aggregates's real rule (week7-query-processing.md
        §40): every non-aggregate item in the select list or HAVING clause
        must be "functionally determined by GROUP BY", which -- with no
        primary-key/functional-dependency analysis in this binder -- means
        exactly "appears in GROUP BY".

        `expression` is resolved against `table` the same way any other
        single-table column reference is (table.column_index -- case
        insensitive, qualifier ignored, matching _expression's own
        convention), then matched against `group_by` by that resolved
        index rather than by name or by a full BoundExpression `==`: a
        GROUP BY key that isn't itself a bare column (`GROUP BY a + 1`)
        can never match a bare column reference here, since only a
        BoundColumn in `group_by` has an `.index` to compare against --
        which is correct, not a gap, given _bind_group_output only ever
        calls this for a bare Column.
        """
        tbl_index = table.column_index(expression.name)
        for i, key in enumerate(group_by):
            if isinstance(key, BoundColumn) and key.index == tbl_index:
                return i
        return None

    def _bind_order_by(
        self,
        items: tuple[OrderKey, ...],
        produced: tuple[BoundExpression, ...],
        bind: Callable[[Expression], BoundExpression],
    ) -> tuple[tuple[BoundOrderKey, ...], tuple[BoundExpression, ...]]:
        """Resolve every `ORDER BY` item against `produced` -- the select
        list (bind_select) or select_items (bind_aggregate_select), in
        that order -- collecting any item that isn't already one of those
        columns into a fresh `hidden` list as it goes.

        `bind` is how a NOT-already-produced expression gets resolved:
        bind_select passes `self._expression(e, table)` (an ordinary
        column reference), bind_aggregate_select passes
        `self._bind_group_output(e, table, group_by, aggregates)` (so
        `ORDER BY SUM(x)` with no SUM(x) elsewhere in the query still
        folds through HashAggregate, appending to the SAME `aggregates`
        list `having` and `select_items` already share). Keeping that
        difference in the caller rather than here is what lets one
        function serve both binding paths.
        """
        hidden: list[BoundExpression] = []
        keys = tuple(self._resolve_order_by_key(item, produced, hidden, bind) for item in items)
        return keys, tuple(hidden)

    def _resolve_order_by_key(
        self,
        item: OrderKey,
        produced: tuple[BoundExpression, ...],
        hidden: list[BoundExpression],
        bind: Callable[[Expression], BoundExpression],
    ) -> BoundOrderKey:
        """Resolve one `ORDER BY` item into a BoundOrderKey pointing at a
        position in the row exec/sort.py's Sort will actually receive:
        `(*produced, *hidden)` -- `produced` is whatever the caller already
        computed (the select list itself), `hidden` is the running list of
        extra columns this SELECT needs only so Sort can see them (see
        `_bind_order_by`'s docstring and BoundSelect.hidden_order_by).

        Two cases, and this is week7-query-processing.md §40's
        `resolve_ordinal` stub plus its natural extension to a full
        expression:

          - `item.expression` is an integer Literal: an ORDINAL. SQL's
            `ORDER BY 2` means the SECOND OUTPUT column -- 1-based, and
            out of range (< 1 or > len(produced)) must be a clear error,
            not silently clamped to the nearest valid position.
          - Anything else: bind it with `bind(item.expression)`, then
            decide whether the result is something `produced` already
            computes (`ORDER BY name` when `name` is already selected
            shouldn't add a second, redundant copy of the same column --
            reuse that position) or whether it's genuinely new and has to
            be appended to `hidden` instead.

        Raises:
            Whatever `bind` raises for an unresolvable expression,
            propagated as-is. An out-of-range ordinal raises
            ColumnNotFoundError -- "no such output column", the same
            error family a name-based column lookup already uses.
        """
        if isinstance(item.expression, Literal) and isinstance(item.expression.value, int):
            ordinal = item.expression.value
            if ordinal < 1 or ordinal > len(produced):
                raise ColumnNotFoundError(f"ORDER BY position {ordinal} is out of range")
            return BoundOrderKey(ordinal - 1, item.descending)

        bound_expr = bind(item.expression)
        for i, expr in enumerate(produced):
            if expr == bound_expr:
                return BoundOrderKey(i, item.descending)

        hidden.append(bound_expr)
        return BoundOrderKey(len(produced) + len(hidden) - 1, item.descending)

    def _bind_limit(self, expression: Expression | None) -> int | None:
        """Fold a `LIMIT` clause down to a concrete count, or None for "no
        LIMIT was given" -- reuses `_constant` (the same narrow constant
        folder INSERT values go through) so `LIMIT ?` and `LIMIT 1 + 1`
        both work without a second evaluator.
        """
        if expression is None:
            return None
        value = self._constant(expression)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise TypeMismatchError("LIMIT must be a non-negative integer")
        return value

    def _bind_offset(self, expression: Expression | None) -> int:
        """`_bind_limit`'s twin for `OFFSET`, defaulting to 0 -- "no OFFSET
        clause" and "OFFSET 0" mean the same thing, so there's no reason
        for callers to carry a None case OFFSET never actually needs.
        """
        if expression is None:
            return 0
        value = self._constant(expression)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise TypeMismatchError("OFFSET must be a non-negative integer")
        return value

    def _bind_aggregate_call(self, call: FunctionCall, table: TableSchema) -> BoundAggregate:
        name = call.name.casefold()
        if call.star:
            if name != "count":
                raise AggregateError(f"{call.name.upper()}(*) is only valid for COUNT")
            return BoundAggregate("count_star", None)

        if len(call.args) != 1:
            raise AggregateError(f"{call.name.upper()} takes exactly one argument")
        return BoundAggregate(name, self._expression(call.args[0], table))

    def bind_delete(self, statement: Delete) -> BoundDelete:
        table = self.catalog.get_table(statement.table)
        where = None if statement.where is None else self._expression(statement.where, table)
        return BoundDelete(table, where)


    def bind_update(self, statement: Update) -> BoundUpdate:
        table = self.catalog.get_table(statement.table)
        assignments = tuple(
            BoundAssignment(table.column_index(assignment.column), self._expression(assignment.value, table))
            for assignment in statement.assignments
        )
        where = None if statement.where is None else self._expression(statement.where, table)
        return BoundUpdate(table, assignments, where)


    def _expression(self, expression: Expression, table: TableSchema) -> BoundExpression:
        if isinstance(expression, Literal):
            return BoundLiteral(expression.value)


        if isinstance(expression, Parameter):
            return BoundLiteral(self._parameter(expression.index))


        if isinstance(expression, Column):
            index = table.column_index(expression.name)  # raises ColumnNotFoundError
            column = table.columns[index]
            return BoundColumn(index, column.name, column.data_type)


        if isinstance(expression, UnaryOp):
            return BoundUnaryOp(expression.operator, self._expression(expression.operand, table))


        if isinstance(expression, BinaryOp):
            return BoundBinaryOp(
                self._expression(expression.left, table),
                expression.operator,
                self._expression(expression.right, table),
            )


        if isinstance(expression, IsNull):
            return BoundIsNull(self._expression(expression.operand, table), expression.negated)


        raise UnsupportedFeatureError(f"cannot bind a {type(expression).__name__} expression")


    def _constant(self, expression: Expression) -> Value:
        """Fold an INSERT value down to a Value at bind time.


        Deliberately narrow: a literal, a `?`, or a unary sign over either.
        Arithmetic like `VALUES (1 + 1)` is rejected rather than folded here
        -- constant-folding `+ - * / %` means reimplementing SQL's NULL
        propagation and divide-by-zero rules, which exec/expressions.py is
        about to own. Once evaluate() exists, this method becomes
        `evaluate(self._expression(...), ())` and the restriction lifts.


        Raises:
            UnsupportedFeatureError: anything else, including a column
                reference (there is no row to read it from).
        """
        if isinstance(expression, Literal):
            return expression.value


        if isinstance(expression, Parameter):
            return self._parameter(expression.index)


        if isinstance(expression, UnaryOp) and expression.operator in ("+", "-"):
            inner = self._constant(expression.operand)
            if inner is None:
                return None
            if isinstance(inner, bool) or not isinstance(inner, (int, float)):
                raise TypeMismatchError(f"unary {expression.operator!r} needs a number, got {type(inner).__name__}")
            return inner if expression.operator == "+" else -inner


        raise UnsupportedFeatureError(
            f"INSERT values must be constant; {type(expression).__name__} is not supported yet"
        )


    def _parameter(self, index: int) -> Value:
        if index >= len(self.parameters):
            raise ParameterCountError(
                f"statement uses at least {index + 1} parameters but {len(self.parameters)} were supplied"
            )
        self.consumed.add(index)
        return self.parameters[index]


    def require_all_parameters_consumed(self) -> None:
        if len(self.consumed) != len(self.parameters):
            raise ParameterCountError(
                f"{len(self.parameters)} parameters were supplied but the statement uses {len(self.consumed)}"
            )




def _require_bindable(parameters: tuple[Value, ...]) -> None:
    """Reject supplied parameters that aren't storable Values, before any
    name resolution happens.


    `parameters` is annotated tuple[Value, ...], but callers hand it in from
    outside quilldb, so at runtime it is whatever they passed. Only the
    INSERT path would otherwise notice: those values get checked against a
    declared column type, while a parameter landing in a WHERE clause has no
    declared type to be checked against and so was checked by nothing at all.


    That asymmetry is worth closing here rather than in the evaluator,
    because it is the same argument as resolving names once instead of per
    row: an unusable parameter is a fact about the caller's arguments, known
    before the first page is read, and a check hoisted here reports it as
    such. Left to execution, `object()` surfaces as a TypeError from inside
    a comparison on some arbitrary row, and `True` doesn't surface at all --
    bool being an int subclass, `True > 30` quietly evaluates to False and
    the query returns a wrong answer with no error anywhere.


    Raises:
        TypeMismatchError: a parameter is not None, int, float, str, or
            bytes. bool is rejected for the reason above, matching
            _coerce_to_declared_type's treatment of it.
    """
    for index, value in enumerate(parameters):
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float, str, bytes)):
            raise TypeMismatchError(
                f"parameter {index} is a {type(value).__name__}; "
                "parameters must be None, int, float, str, or bytes"
            )




def _coerce_to_declared_type(
    value: Value, data_type: DataType, table_name: str, column_name: str
) -> Value:
    """Check `value` against a declared column type, coercing int -> float
    for REAL.


    quilldb's rules are deliberately SMALLER and STRICTER than SQLite's type
    affinity (§17): SQLite would accept the string '42' into an INTEGER
    column and convert it, while this rejects it. That is a documented
    supported-subset policy, not an attempt at affinity parity -- so a
    differential test that disagrees here is expected behaviour, not a bug.


    bool is rejected everywhere numeric even though bool IS an int in
    Python: `INSERT INTO t VALUES (True)` almost always means the caller
    confused a flag for a number, and SQLite has no boolean storage class
    to map it onto.


    Raises:
        TypeMismatchError: value is not acceptable for data_type.
    """
    if value is None:  # NULL is acceptable in every declared type
        return None


    where = f"{table_name}.{column_name}"


    if data_type is DataType.INTEGER:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeMismatchError(f"{where} is INTEGER; cannot store {type(value).__name__}")
        return value


    if data_type is DataType.REAL:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeMismatchError(f"{where} is REAL; cannot store {type(value).__name__}")
        return float(value)


    if data_type is DataType.TEXT:
        if not isinstance(value, str):
            raise TypeMismatchError(f"{where} is TEXT; cannot store {type(value).__name__}")
        return value


    if data_type is DataType.BLOB:
        if not isinstance(value, bytes):
            raise TypeMismatchError(f"{where} is BLOB; cannot store {type(value).__name__}")
        return value


    raise UnsupportedFeatureError(f"unknown declared type {data_type!r} on {where}")