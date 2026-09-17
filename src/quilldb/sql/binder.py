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


from dataclasses import dataclass
from typing import Protocol


from quilldb.catalog.schema import TableSchema
from quilldb.codec.record import Value
from quilldb.errors import (
    ColumnCountError,
    ParameterCountError,
    TypeMismatchError,
    UnsupportedFeatureError,
)
from quilldb.sql.ast import (
    BinaryOp,
    Column,
    CreateTable,
    DataType,
    Expression,
    Insert,
    IsNull,
    Literal,
    Parameter,
    Select,
    Statement,
    UnaryOp,
)




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
class BoundInsert:
    table: TableSchema
    values: tuple[Value, ...]




@dataclass(frozen=True)
class BoundSelect:
    table: TableSchema
    expressions: tuple[BoundExpression, ...]
    where: BoundExpression | None




type BoundStatement = BoundCreateTable | BoundInsert | BoundSelect




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
        ColumnNotFoundError: propagated from TableSchema.column_index().
        ColumnCountError: an INSERT supplied a different number of values
            than the table has columns.
        ParameterCountError: the statement's `?` count and len(parameters)
            disagree -- in either direction. Every supplied parameter must
            be consumed, so passing extras is an error rather than being
            silently ignored.
        TypeMismatchError: an INSERT value is incompatible with its
            column's declared type (see _coerce_to_declared_type).
        UnsupportedFeatureError: an INSERT value is not a constant
            expression, or a statement type this binder doesn't handle.
    """
    binder = _Binder(catalog, parameters)


    if isinstance(statement, CreateTable):
        bound: BoundStatement = BoundCreateTable(statement)
    elif isinstance(statement, Insert):
        bound = binder.bind_insert(statement)
    elif isinstance(statement, Select):
        bound = binder.bind_select(statement)
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
        table = self.catalog.get_table(statement.table)


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
        return BoundSelect(table, expressions, where)


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






