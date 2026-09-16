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


type Expression = Literal | Column | Parameter | UnaryOp | BinaryOp | IsNull


@dataclass(frozen=True)
class CreateTable:
    name: str
    columns: tuple[ColumnDef, ...]


@dataclass(frozen=True)
class Insert:
    table: str
    values: tuple[Expression, ...]


@dataclass(frozen=True)
class Select:
    expressions: tuple[Expression, ...] | None
    """None means `SELECT *`; expanding that into one Column per table
    column is the binder's job, since the parser has no catalog to expand
    it against.
    """
    table: str
    where: Expression | None = None


type Statement = CreateTable | Insert | Select
