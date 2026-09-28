"""Syntax tree for the Week 3 SQL subset.

The AST records syntax, not catalog facts: a Column node still carries a
bare name string. Resolving that name to an integer column slot is the
binder's job (sql/binder.py, a later task) -- keeping that resolution out of
here is what lets the parser be tested with zero knowledge of any table.

Every node is a frozen dataclass so tests can compare whole trees with `==`
instead of walking them field by field.
"""

from dataclasses import dataclass
from enum import Enum

from quilldb.codec.record import Value


class DataType(Enum):
    INTEGER = "INTEGER"
    REAL = "REAL"
    TEXT = "TEXT"
    BLOB = "BLOB"

@dataclass(frozen=True)
class ColumnDef:
    name: str
    data_type: DataType


@dataclass(frozen=True)
class Literal:
    value: Value


@dataclass(frozen=True)
class Column:
    name: str
    table: str | None = None
    """The raw qualifier before the dot in `u.id` -- an alias or a table
    name, whichever the query wrote. Unresolved: deciding which scope it
    names is the binder's job (sql/binder.py's resolve_column), exactly
    like `name` itself is an unresolved column reference.
    """


@dataclass(frozen=True)
class Parameter:
    index: int
    """Zero-based, assigned left-to-right by Parser across the whole
    statement -- the tokenizer's PARAMETER token carries no index itself
    (see tokens.py), since counting `?` occurrences is inherently a parsing
    concern, not a scanning one.
    """


@dataclass(frozen=True)
class UnaryOp:
    operator: str
    operand: "Expression"


@dataclass(frozen=True)
class BinaryOp:
    left: "Expression"
    operator: str
    right: "Expression"


@dataclass(frozen=True)
class IsNull:
    """`x IS [NOT] NULL`. Needs its own node rather than reusing BinaryOp
    with operator "=": SQL's NULL is not a value comparisons can produce
    TRUE against (§6.5's evaluator gives NULL = NULL -> NULL, never TRUE),
    so `x = NULL` and `x IS NULL` are different operators with different
    three-valued results, not two spellings of the same thing.
    """

    operand: "Expression"
    negated: bool = False


@dataclass(frozen=True)
class FunctionCall:
    """`COUNT(*)`, `SUM(total)`, ... -- syntax only, same policy as every
    other node here: whether `name` is a function quilldb actually knows,
    and whether the argument count/shape makes sense for it, is the
    binder's job (sql/binder.py's _bind_aggregate_call), not the parser's.

    `star` is True only for `COUNT(*)`: `*` is not a value-producing
    expression the way every other argument is (there is no AST node for
    a bare `*` outside a SELECT list), so it is recorded as its own flag
    with `args` left empty, rather than inventing a Star() expression
    node used nowhere else.
    """

    name: str
    args: tuple["Expression", ...] = ()
    star: bool = False


type Expression = Literal | Column | Parameter | UnaryOp | BinaryOp | IsNull | FunctionCall


@dataclass(frozen=True)
class OrderKey:
    """One `ORDER BY` key: an expression (an ordinary Expression, OR an
    integer Literal meaning "the Nth output column" -- deciding which is
    the binder's job, same policy as every other node here) plus its
    direction. `descending=False` covers both a bare key and an explicit
    `ASC` -- the parser doesn't distinguish them, since neither SQL nor
    quilldb gives them different meaning.
    """

    expression: "Expression"
    descending: bool = False


@dataclass(frozen=True)
class CreateTable:
    name: str
    columns: tuple[ColumnDef, ...]


@dataclass(frozen=True)
class Insert:
    table: str
    values: tuple[Expression, ...]


@dataclass(frozen=True)
class TableRef:
    """One table named in a FROM/JOIN clause, alias and all.

    `alias` is None when the query didn't give one -- the binder then keys
    that table's scope on `name` itself, so `FROM orders o` and `FROM
    orders` both produce a scope, just under a different key.
    """

    name: str
    alias: str | None = None


@dataclass(frozen=True)
class JoinClause:
    """One `JOIN ... ON ...` or comma-join step, chained onto the table
    before it in `Select.joins`.

    A comma join (`FROM a, b`) parses to `JoinClause("INNER", b, on=None)`:
    it's an inner join with no ON condition, which is exactly what a comma
    join means -- any filtering it implies lives in WHERE, same as real SQL.
    `on` is only ever None for that case; `JOIN ... ON` always supplies one.
    """

    join_type: str  # "INNER" | "LEFT"
    table: TableRef
    on: "Expression | None" = None


@dataclass(frozen=True)
class Select:
    expressions: tuple[Expression, ...] | None
    """None means `SELECT *`; expanding that into one Column per table
    column is the binder's job, since the parser has no catalog to expand
    it against.
    """
    table: TableRef
    joins: tuple[JoinClause, ...] = ()
    where: Expression | None = None
    group_by: tuple[Expression, ...] = ()
    having: Expression | None = None
    distinct: bool = False
    order_by: tuple[OrderKey, ...] = ()
    limit: Expression | None = None
    offset: Expression | None = None
    """Whether GROUP BY keys are functionally determined, whether a bare
    column outside GROUP BY is legal, and whether DISTINCT may combine
    with an aggregate -- none of that is decided here. The parser only
    records what the query wrote; sql/binder.py's bind_aggregate_select
    is where those questions get answered (week7-query-processing.md §40).

    `limit`/`offset` are Expression rather than a bare int so `LIMIT ?`
    parses -- folding either down to a concrete non-negative int (and
    rejecting anything else) is the binder's job, same as every other
    Expression here. An ORDER BY key naming a column the SELECT list
    didn't ask for, or an ordinal referring to one of those columns by
    position, is also unresolved at this level -- see BoundSelect's
    `hidden_order_by` in sql/binder.py.
    """


@dataclass(frozen=True)
class Delete:
    table: str
    where: Expression | None = None


@dataclass(frozen=True)
class Assignment:
    column: str
    value: Expression


@dataclass(frozen=True)
class Update:
    table: str
    assignments: tuple[Assignment, ...]
    where: Expression | None = None

@dataclass(frozen=True)
class CreateIndex:
    name: str
    table: str
    columns: tuple[str, ...]
    unique: bool = False


@dataclass(frozen=True)
class Analyze:
    target: str | None = None
    """A table name, or None to analyze every table. Unlike real SQLite,
    an index name here is not resolved to its owning table -- StatisticsCatalog.analyze()
    treats `target` strictly as a table name and raises TableNotFoundError
    otherwise."""



@dataclass(frozen=True)
class Explain:
    statement: Select
    analyze: bool = False


@dataclass(frozen=True)
class Begin:
    """`immediate=True` for `BEGIN IMMEDIATE` (week6-concurrency.md,
    "declare write intent up front") -- claims the global writer lock
    right away instead of leaving it to the transaction's first real
    write. EXCLUSIVE (SQLite's third variant) has no meaning under
    quilldb's table-level locking -- there's no separate "block new
    readers too" mode to name -- so it isn't parsed.
    """
    immediate: bool = False


@dataclass(frozen=True)
class Commit:
    pass


@dataclass(frozen=True)
class Rollback:
    pass


type Statement = (
    CreateTable | Insert | Select | Delete | Update | CreateIndex | Analyze | Explain
    | Begin | Commit | Rollback
)