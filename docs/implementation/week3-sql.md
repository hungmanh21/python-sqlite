# quilldb — Implementation Plan: Week 3, SQL Frontend and Executor


← [Index](README.md)  ·  Prev: [Week 2](week-2-btree.md)  ·  Next: [Week 4 — Mutation and Indexes](week-4-mutation-and-indexes.md)


---


# Week 3 Spec — SQL Frontend, Catalog, and Iterator Executor


This is the vertical slice: SQL text enters at the top, rows come out at the bottom, and the schema
survives closing the file. Week 2's exact interfaces now exist, so this spec is deliberately concrete.


Before starting, finish the Week 2 definition of done. In particular, `BTree.insert()` must split
recursively while keeping the root page number stable, `TableCursor` must release all pins, and the
B-tree/page serializer must correctly handle page 1's 100-byte prefix. The catalog exercises all
three at once; carrying a partial Week 2 implementation forward will make SQL failures look random.


## Week 3 contract


Keep the SQL surface narrow enough to finish, but make the supported part internally complete:


```sql
CREATE TABLE users (id INTEGER, name TEXT, age INTEGER);
INSERT INTO users VALUES (?, ?, ?);
SELECT name, age + 1 FROM users
WHERE age >= 30 AND name LIKE 'a%';
```


Supported this week:


- one statement per `execute()` call, with an optional trailing semicolon
- `CREATE TABLE` with `INTEGER`, `REAL`, `TEXT`, and `BLOB` columns
- `INSERT INTO table VALUES (...)`; no explicit target-column list yet
- `SELECT *` or an expression list from exactly one table, with an optional `WHERE`
- literals, `?` parameters, column references, parentheses, unary `+ - NOT`, arithmetic,
  comparisons, `IS [NOT] NULL`, `LIKE`, `AND`, and `OR`
- `-- line comments` and `/* block comments */`
- unquoted identifiers only; compare them case-insensitively with `casefold()`


Explicitly unsupported until later: joins, aliases, quoted identifiers, `ORDER BY`, `GROUP BY`,
`LIMIT`, `UPDATE`, `DELETE`, indexes, constraints, defaults, and multiple statements. Reject these
with a typed `SQLSyntaxError` or `UnsupportedFeatureError`; never silently accept and misinterpret.


Two semantic rules are non-negotiable:


1. SQL `NULL` is not Python `None` with ordinary Python operators. Comparisons and arithmetic with
   `NULL` produce `NULL`; `WHERE` keeps only rows whose predicate is exactly `TRUE`.
2. Page 1 is the `sqlite_schema` table b-tree. Do not create a private page-2 catalog. Its rows have
   SQLite's five real columns: `(type, name, tbl_name, rootpage, sql)`.


## Week 3 file map


Build in this order. Parser tests do not need storage; catalog tests do not need the public API;
operator tests do not need the SQL parser. Preserve those seams.


| # | File | What it does |
|---|---|---|
| 1 | `errors.py` | SQL, catalog, binding, and constraint errors |
| 2 | `sql/tokens.py` | Token kinds and source positions |
| 3 | `sql/tokenizer.py` | SQL text → tokens |
| 4 | `sql/ast.py` | Immutable syntax tree nodes |
| 5 | `sql/parser.py` | Statements + Pratt expression parser |
| 6 | `catalog/schema.py` | Column and table metadata |
| 7 | `catalog/catalog.py` | Durable `sqlite_schema` access |
| 8 | `sql/binder.py` | Names/parameters → checked column indexes and values |
| 9 | `exec/expressions.py` | SQL value semantics and expression evaluation |
| 10 | `exec/operators.py` | `SeqScan`, `Filter`, `Project`, `Insert` |
| 11 | `api/connection.py` | `connect`, `Connection`, result `Cursor` |
| 12 | `quilldb/__init__.py` | Public exports |


Create `__init__.py` in each new package. The engine remains standard-library-only at runtime.


---


## 10. Week 3 additions to `errors.py`


These are caller/query errors, not corrupt-file errors, so none derives from
`CorruptDatabaseError`.


```python
class SQLError(QuillDBError):
    """Base class for SQL text, name-resolution, and execution errors."""




class SQLSyntaxError(SQLError):
    """The SQL text is not in the supported grammar."""




class CatalogError(SQLError):
    """Base class for schema lookup and schema-change errors."""




class TableNotFoundError(CatalogError):
    """A statement named a table that does not exist."""




class TableAlreadyExistsError(CatalogError):
    """CREATE TABLE named an existing table."""




class ColumnNotFoundError(SQLError):
    """A statement named a column that does not exist in its input row."""




class ParameterCountError(SQLError):
    """The number of supplied values does not match the number of `?` markers."""




class TypeMismatchError(SQLError):
    """A value is incompatible with a declared column type."""




class ConstraintError(SQLError):
    """Base class for data-integrity failures."""
```


Include the token position or object name in every message. Tests should assert the type first and
only stable, useful fragments of the message.


---


## 11. `sql/tokens.py`


Use token kinds for punctuation/operators and for the small keyword set. Keeping keywords distinct
makes parser branches readable; preserving the original lexeme makes errors useful.


```python
"""Tokens produced by the SQL scanner."""


from dataclasses import dataclass
from enum import Enum, auto




class TokenType(Enum):
    EOF = auto()
    IDENTIFIER = auto()
    INTEGER = auto()
    REAL = auto()
    STRING = auto()
    PARAMETER = auto()


    LEFT_PAREN = auto()
    RIGHT_PAREN = auto()
    COMMA = auto()
    SEMICOLON = auto()
    STAR = auto()
    PLUS = auto()
    MINUS = auto()
    SLASH = auto()
    PERCENT = auto()
    EQ = auto()
    NE = auto()       # both != and <>
    LT = auto()
    LE = auto()
    GT = auto()
    GE = auto()


    CREATE = auto()
    TABLE = auto()
    INSERT = auto()
    INTO = auto()
    VALUES = auto()
    SELECT = auto()
    FROM = auto()
    WHERE = auto()
    INTEGER_TYPE = auto()
    REAL_TYPE = auto()
    TEXT_TYPE = auto()
    BLOB_TYPE = auto()
    NULL = auto()
    TRUE = auto()
    FALSE = auto()
    AND = auto()
    OR = auto()
    NOT = auto()
    IS = auto()
    LIKE = auto()




@dataclass(frozen=True)
class Token:
    type: TokenType
    lexeme: str
    value: None | int | float | str
    position: int




KEYWORDS: dict[str, TokenType] = {
    "create": TokenType.CREATE,
    # ...every keyword above, keyed by casefolded spelling
}
```


`position` is the zero-based character offset in the original SQL string. Do not store only line
and column; deriving those for an error is easy, while recovering an exact byte/character offset is
not.


---


## 12. `sql/tokenizer.py`


```python
"""A deterministic, single-pass scanner for quilldb's SQL subset."""


from quilldb.sql.tokens import Token




def tokenize(sql: str) -> list[Token]:
    """Scan one SQL statement and append exactly one EOF token.


    Rules:
      - keywords are case-insensitive; identifiers retain original spelling
      - strings use single quotes and escape one quote as two: 'Ada''s'
      - integers and decimal/exponent forms are separate token kinds
      - `!=` and `<>` both become NE
      - whitespace and comments are discarded
      - a leading sign is an operator, not part of a numeric token


    Raises:
        SQLSyntaxError: unknown character, unterminated string/comment, malformed
            exponent, or a bare `!`.
    """
    raise NotImplementedError
```


Do not tokenize with one giant regular expression. A cursor over the string makes unterminated
constructs and exact error positions straightforward, and it lets comment/string rules remain
local. Helpers worth writing:


```python
def _scan_string(sql: str, start: int) -> tuple[str, int]: ...
def _scan_number(sql: str, start: int) -> tuple[int | float, int]: ...
def _line_and_column(sql: str, position: int) -> tuple[int, int]: ...
```


Tests in `tests/unit/sql/test_tokenizer.py`:


- keywords in mixed case; identifiers preserve spelling
- every one- and two-character operator
- `123`, `1.5`, `.5`, `5.`, `1e6`, `1.5E-2`
- `'Ada''s notebook'` becomes `Ada's notebook`
- comments disappear without joining the tokens around them
- `?` produces a parameter token; indexing happens in the parser
- unterminated string/block comment and `!` include the correct position
- exactly one EOF token, including for empty/whitespace-only input


---


## 13. `sql/ast.py`


The AST records syntax, not catalog facts. A `Column` still contains a name; the binder later turns
it into an integer slot. Keep every node immutable so tests can compare whole trees directly.


```python
"""Syntax tree for the Week 3 SQL subset."""


from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias


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
    index: int                 # zero-based, assigned left-to-right by Parser




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
    operand: "Expression"
    negated: bool = False




Expression: TypeAlias = Literal | Column | Parameter | UnaryOp | BinaryOp | IsNull




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
    expressions: tuple[Expression, ...] | None  # None means SELECT *
    table: str
    where: Expression | None = None




Statement: TypeAlias = CreateTable | Insert | Select
```


`LIKE` is a `BinaryOp` whose operator is `"LIKE"`; it needs no special AST shape because its
syntax and arity are ordinary infix. `IS NULL` does need a node: treating it as `= NULL` would give
the wrong three-valued result.


---


## 14. `sql/parser.py`


Statements use straightforward recursive descent. Expressions use Pratt parsing (binding powers),
so adding another operator is one table entry rather than another call-stack layer.


```python
"""Recursive-descent statements plus a Pratt expression parser."""


from quilldb.sql.ast import Expression, Statement
from quilldb.sql.tokens import Token, TokenType




def parse(sql: str) -> Statement:
    """Parse exactly one statement with an optional trailing semicolon.


    Raises:
        SQLSyntaxError: empty input, unsupported statement, unexpected token,
            trailing tokens, duplicate column name, or malformed expression.
    """
    return Parser(tokenize(sql)).parse_statement()




class Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self.tokens = tokens
        self.current = 0
        self.parameter_count = 0


    def parse_statement(self) -> Statement: ...
    def _create_table(self) -> Statement: ...
    def _insert(self) -> Statement: ...
    def _select(self) -> Statement: ...
    def _expression(self, min_binding_power: int = 0) -> Expression: ...
    def _prefix(self) -> Expression: ...
    def _expect(self, token_type: TokenType, message: str) -> Token: ...
```


Use this precedence, lowest to highest:


| Binding | Operators | Associativity |
|---|---|---|
| 10 | `OR` | left |
| 20 | `AND` | left |
| 30 | comparisons, `IS [NOT] NULL`, `LIKE` | non-chainable |
| 40 | `+ -` | left |
| 50 | `* / %` | left |
| 60 | prefix `NOT + -` | right |


For a left-associative infix operator with precedence `p`, parse its right operand at `p + 1`.
Reject chained comparisons (`a < b < c`) rather than inventing Python semantics SQL does not have.


The statement grammar is:


```text
statement      := create_table | insert | select
create_table   := CREATE TABLE identifier "(" column_def ("," column_def)* ")"
column_def     := identifier (INTEGER | REAL | TEXT | BLOB)
insert         := INSERT INTO identifier VALUES "(" expression ("," expression)* ")"
select         := SELECT ("*" | expression ("," expression)*)
                  FROM identifier (WHERE expression)?
```


Parser tests in `tests/unit/sql/test_parser.py` should compare AST values, not rendered strings.
The precedence cases that must exist:


```python
assert parse("SELECT a + b * c FROM t") == Select(
    (BinaryOp(Column("a"), "+", BinaryOp(Column("b"), "*", Column("c"))),),
    "t",
)


assert parse("SELECT * FROM t WHERE a = 1 OR b = 2 AND NOT c = 3") == ...
```


Also test duplicate column declarations case-insensitively, parameter indexes assigned left-to-right
across the whole statement, empty lists, missing commas/parentheses, multiple statements, and a
useful error position.


---


## 15. `catalog/schema.py`


```python
"""In-memory schema objects reconstructed from sqlite_schema."""


from dataclasses import dataclass


from quilldb.sql.ast import DataType




@dataclass(frozen=True)
class ColumnSchema:
    name: str
    data_type: DataType




@dataclass(frozen=True)
class TableSchema:
    name: str
    columns: tuple[ColumnSchema, ...]
    root_page: int
    sql: str


    def column_index(self, name: str) -> int:
        """Resolve an unquoted name case-insensitively.


        Raises:
            ColumnNotFoundError.
        """
        raise NotImplementedError
```


Do not put live `BTree`, `Pager`, or cursor objects in schema dataclasses. Metadata must remain safe
to cache after a scan's pins have been released.


---


## 16. `catalog/catalog.py`


The catalog is not a new file format. It is the ordinary table b-tree rooted at
`SCHEMA_ROOT_PAGE == 1`; every row payload is `encode_record((type, name, tbl_name, rootpage, sql))`.


```python
"""Durable schema metadata stored in SQLite's sqlite_schema table."""


from collections.abc import Iterable


from quilldb.btree.btree import BTree
from quilldb.catalog.schema import TableSchema
from quilldb.sql.ast import CreateTable
from quilldb.storage.bufferpool import BufferPool
from quilldb.storage.pager import Pager




class Catalog:
    def __init__(self, pager: Pager, pool: BufferPool) -> None:
        self.pager = pager
        self.pool = pool
        self._tables: dict[str, TableSchema] = {}


    def load(self) -> None:
        """Scan page 1, validate its five-column rows, and rebuild `_tables`.


        For every row with type == "table", parse the stored SQL and require a
        CreateTable whose name agrees with `name` and `tbl_name`.


        Raises:
            CorruptDatabaseError: malformed catalog record, duplicate name,
                impossible root page, or stored SQL inconsistent with the row.
            UnsupportedFeatureError: a schema object uses syntax quilldb cannot
                interpret safely.
        """
        raise NotImplementedError


    def create_table(self, statement: CreateTable, sql: str) -> TableSchema:
        """Allocate an empty table root and add one sqlite_schema row.


        Store `sql` verbatim. Initialize the allocated root as LEAF_TABLE before
        publishing the catalog row. Use the next positive catalog rowid, bump the
        file header's schema cookie, and update `_tables` only after disk objects
        have been created successfully.


        Raises:
            TableAlreadyExistsError: case-insensitive name collision.
            UnsupportedFeatureError: name starts with reserved `sqlite_`.
        """
        raise NotImplementedError


    def get_table(self, name: str) -> TableSchema:
        """Return a table by case-insensitive name or raise TableNotFoundError."""
        raise NotImplementedError


    def list_tables(self) -> tuple[TableSchema, ...]:
        """Return schemas sorted by casefolded table name."""
        raise NotImplementedError
```


Two small storage APIs are required here; add them rather than reaching into private fields:


```python
# storage/pager.py
@classmethod
def memory(cls) -> "Pager":
    """Create a valid one-page database backed by io.BytesIO."""


def bump_schema_cookie(self) -> int:
    """Increment the header schema cookie modulo 2**32 and return it."""
```


`Pager.close()` must work for both a real file and `BytesIO`. `BufferPool` should need no changes.


Catalog tests in `tests/unit/catalog/test_catalog.py`:


- a new file loads an empty catalog
- creating a table allocates an empty `LEAF_TABLE` root and writes the exact five-column row
- names are case-insensitive; original spelling is preserved
- duplicate and reserved names raise typed errors without allocating a leaked page
- close/reopen reconstructs identical `TableSchema` values from stored SQL
- malformed catalog rows raise `CorruptDatabaseError`, not `IndexError`
- `sqlite3 file.db ".schema users"` sees the created table and `PRAGMA integrity_check` is `ok`


That last test may skip when the `sqlite3` executable is unavailable, but it belongs in CI if the
binary is installed.


---


## 17. `sql/binder.py`


Parsing answers "is this grammatical?" Binding answers "does it mean anything in this database?"
The executor should never compare names or count parameters.


```python
"""Name resolution, parameter substitution, and declared-type checks."""


from dataclasses import dataclass
from typing import TypeAlias


from quilldb.catalog.catalog import Catalog
from quilldb.catalog.schema import TableSchema
from quilldb.codec.record import Value
from quilldb.sql.ast import CreateTable, DataType, Statement




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




BoundExpression: TypeAlias = BoundLiteral | BoundColumn | BoundUnaryOp | BoundBinaryOp | BoundIsNull




@dataclass(frozen=True)
class BoundCreateTable:
    statement: CreateTable




@dataclass(frozen=True)
class BoundInsert:
    table: TableSchema
    values: tuple[Value, ...]




@dataclass(frozen=True)
class BoundSelect:
    table: TableSchema
    expressions: tuple[BoundExpression, ...]  # SELECT * expanded here
    where: BoundExpression | None




BoundStatement: TypeAlias = BoundCreateTable | BoundInsert | BoundSelect




def bind(
    statement: Statement,
    catalog: Catalog,
    parameters: tuple[Value, ...] = (),
) -> BoundStatement:
    """Resolve all names and replace every Parameter with a BoundLiteral.


    INSERT values must be constant expressions, match the table's column count,
    and satisfy declared types. SELECT * expands to one BoundColumn per table
    column. Exactly all supplied parameters must be consumed.


    Raises:
        TableNotFoundError, ColumnNotFoundError, ParameterCountError,
        TypeMismatchError, UnsupportedFeatureError.
    """
    raise NotImplementedError
```


Declared-type rules for this project are intentionally smaller and stricter than SQLite affinity:


| Declared type | Accepted non-NULL Python values |
|---|---|
| `INTEGER` | `int`, but not `bool` |
| `REAL` | `int` or `float`, coercing `int` to `float` |
| `TEXT` | `str` |
| `BLOB` | `bytes` |


Every type accepts `None`. Explain in the README that this is a supported-subset policy, not a claim
to reproduce SQLite's full affinity/coercion rules.


Binding tests must prove errors happen before a scan or write begins. Use a fake catalog or monkey
patched operator constructor to assert an unknown column never opens a cursor.


---


## 18. `exec/expressions.py`


```python
"""Evaluate bound expressions using SQL NULL and boolean semantics."""


from quilldb.codec.record import Value
from quilldb.sql.binder import BoundExpression


Row = tuple[Value, ...]
TruthValue = bool | None




def evaluate(expression: BoundExpression, row: Row) -> Value:
    """Evaluate one expression against one row.


    `None` represents SQL NULL. Arithmetic/comparisons propagate NULL;
    AND/OR/NOT use SQL's three-valued truth tables; IS NULL never returns NULL;
    division or remainder by zero returns NULL, matching SQLite.


    Raises:
        TypeMismatchError: operator receives incompatible non-NULL operands.
    """
    raise NotImplementedError




def where_passes(value: Value) -> bool:
    """WHERE keeps only TRUE. FALSE and NULL both reject the row."""
    return value is True or (isinstance(value, (int, float)) and value != 0)




def sql_like(value: str, pattern: str) -> bool:
    """Implement `%` (zero or more) and `_` (exactly one), without regex."""
    raise NotImplementedError
```


Do not implement `LIKE` by interpolating an unescaped pattern into a regular expression. A small
dynamic-programming matcher is deterministic, handles `%` backtracking correctly, and stays free of
regex metacharacter bugs.


The expression tests need the full `AND`/`OR` truth tables, not three hand-picked examples:


| A | B | A AND B | A OR B |
|---|---|---|---|
| TRUE | NULL | NULL | TRUE |
| FALSE | NULL | FALSE | NULL |
| NULL | NULL | NULL | NULL |


Also test `NOT NULL`, every comparison with `NULL`, `IS NULL`, arithmetic precedence via parsed and
bound expressions, divide-by-zero, and `%`/`_` LIKE patterns.


---


## 19. `exec/operators.py`


Operators own resources between `open()` and `close()`. `close()` must be idempotent, and every
operator closes its child even when evaluation raises.


```python
"""Pull-based relational operators for the Week 3 vertical slice."""


from abc import ABC, abstractmethod


from quilldb.codec.record import Value


Row = tuple[Value, ...]




class Operator(ABC):
    @abstractmethod
    def open(self) -> None: ...


    @abstractmethod
    def next(self) -> Row | None:
        """Return one row, or None when exhausted."""


    @abstractmethod
    def close(self) -> None: ...


    @abstractmethod
    def explain(self, depth: int = 0) -> str: ...




class SeqScan(Operator):
    """Stream decoded records from a table's TableCursor in rowid order."""


    def __init__(self, pager: Pager, pool: BufferPool, table: TableSchema) -> None: ...




class Filter(Operator):
    """Discard child rows unless `where_passes(evaluate(predicate, row))`."""


    def __init__(self, child: Operator, predicate: BoundExpression) -> None: ...




class Project(Operator):
    """Evaluate output expressions for every child row."""


    def __init__(self, child: Operator, expressions: tuple[BoundExpression, ...]) -> None: ...




class Insert(Operator):
    """Insert one encoded row on the first next(); then return None forever."""


    def __init__(self, pager: Pager, pool: BufferPool, statement: BoundInsert) -> None: ...
```


`SeqScan.open()` creates a `TableCursor`, calls `first()`, and remembers whether it is positioned.
`next()` returns the current decoded record before advancing. `close()` releases every cursor pin.


`Insert` chooses `rowid = 1` for an empty table, otherwise `last_rowid + 1`. It encodes the bound
values with `encode_record` and calls `BTree.insert`. The implicit rowid is not part of the returned
tuple and is deliberately not exposed in SQL this week.


Build the operator tree with one small function, kept out of `Connection.execute`:


```python
def build_operator(
    statement: BoundSelect | BoundInsert,
    pager: Pager,
    pool: BufferPool,
) -> Operator:
    """Translate a bound statement into the Week 3 fixed plan."""
```


The select plan is always:


```text
Project
└─ Filter       # omitted when there is no WHERE
   └─ SeqScan
```


That is a planner, just a planner with one possible access path. Week 4 moves this decision into
`plan/planner.py` and adds `IndexScan` without changing these operator contracts.


Operator tests in `tests/unit/exec/test_operators.py`:


- scan of empty and multi-page tables, in rowid order
- filter skips many rows before returning one; NULL predicate rejects
- project reorders columns and computes expressions
- insert writes exactly once even if `next()` is called repeatedly
- every operator's `close()` is idempotent
- evaluator failure closes the scan and leaks no pins
- a capacity-2 buffer pool can scan a multi-level tree to completion


---


## 20. `api/connection.py` and public exports


```python
"""Small DB-API-inspired public surface."""


from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Self


from quilldb.codec.record import Value




class Cursor:
    @property
    def description(self) -> tuple[tuple[str, ...], ...] | None: ...


    @property
    def rowcount(self) -> int: ...


    def fetchone(self) -> tuple[Value, ...] | None: ...
    def fetchmany(self, size: int = 1) -> list[tuple[Value, ...]]: ...
    def fetchall(self) -> list[tuple[Value, ...]]: ...
    def close(self) -> None: ...




class Connection:
    def execute(
        self,
        sql: str,
        parameters: Sequence[Value] = (),
    ) -> Cursor:
        """Parse, bind, and execute one statement.


        CREATE TABLE and INSERT complete before this method returns. SELECT
        leaves its operator open and streams rows through the returned Cursor.
        Starting another execute() closes any still-open result cursor on this
        connection; multiple active cursors arrive with multiple connections.
        """
        raise NotImplementedError


    def close(self) -> None: ...
    def __enter__(self) -> Self: ...
    def __exit__(self, *exc_info: object) -> None: ...




def connect(path: str | Path) -> Connection:
    """Open an existing database or create a new one.


    The exact string `:memory:` selects Pager.memory(); it never creates a file.
    """
    raise NotImplementedError
```


For `description`, use one tuple per output expression and guarantee only element 0 (the display
name) this week. Return the source column name for a plain column and the expression's SQL-ish text
otherwise. `rowcount` is `1` for a successful insert, `0` for create, and `-1` for select.


`Cursor.fetchone()` after exhaustion returns `None`; `fetchmany(0)` returns `[]`; a negative size
raises `ValueError`. Fetching after explicit close raises `ValueError("cursor is closed")`.


Export only the intended surface from `src/quilldb/__init__.py`:


```python
from quilldb.api.connection import Connection, Cursor, connect
from quilldb.errors import QuillDBError


__all__ = ["Connection", "Cursor", "QuillDBError", "connect"]
```


---


## Week 3 integration and differential tests


`tests/integration/test_vertical_slice.py` is the milestone:


```python
def test_create_insert_select_survives_reopen(tmp_path) -> None:
    path = tmp_path / "demo.db"


    with quilldb.connect(path) as db:
        db.execute("CREATE TABLE users (id INTEGER, name TEXT, age INTEGER)")
        db.execute("INSERT INTO users VALUES (?, ?, ?)", (1, "ada", 36))
        db.execute("INSERT INTO users VALUES (?, ?, ?)", (2, "linus", None))


    with quilldb.connect(path) as db:
        cursor = db.execute("SELECT name FROM users WHERE age > ?", (30,))
        assert cursor.description[0][0] == "name"
        assert cursor.fetchall() == [("ada",)]
```


`tests/differential/test_sqlite.py` owns the second oracle. Keep the harness boring:


```python
@pytest.mark.parametrize("script, query", CASES)
def test_matches_sqlite(tmp_path, script: list[tuple[str, tuple]], query: tuple[str, tuple]) -> None:
    # Run the same supported statements and parameters through sqlite3 and quilldb.
    # Compare the final query's column names and rows. Do not compare exception text.
```


Start with about 20 deterministic cases: empty table, all value types, NULL predicates, precedence,
escaped strings, parameters, LIKE, projection order, and multiple inserted rows. Differential tests
do not replace focused unit tests: when both engines disagree, a small unit test tells you which
layer is wrong.


## Week 3 sessions


| Session | Build | Stop only when |
|---|---|---|
| 1 | tokens + tokenizer | scanner edge cases and source positions are green |
| 2 | AST + `CREATE TABLE` parser | whole-tree equality tests are green |
| 3 | `INSERT`, `SELECT`, Pratt expressions | precedence table and syntax failures are green |
| 4 | schema objects + durable page-1 catalog | create, close, reopen reconstructs the table |
| 5 | binder + expression evaluator | bad names/params fail before execution; NULL truth tables pass |
| 6 | `SeqScan`, `Filter`, `Project`, `Insert` | operator tests pass with a tiny buffer pool |
| 7 | connection/cursor API + `:memory:` | the four-line public example works |
| 8 | differential harness + cleanup | ~20 SQLite comparisons, mypy, ruff, and full pytest are green |


## Week 3 definition of done


```bash
pytest -q
mypy
ruff check src tests
```


And all of these are true:


- the public four-line example in `roadmap.md` runs unchanged
- `CREATE TABLE` survives close/reopen because it is really in `sqlite_schema`
- `sqlite3 demo.db ".schema"` sees quilldb's table and `PRAGMA integrity_check` returns `ok`
- unknown tables/columns and wrong parameter counts fail before a cursor opens or a page changes
- `SELECT` on an empty table returns `[]`
- SQL NULL truth tables have exhaustive unit tests
- `:memory:` creates no filesystem entry
- abandoned, exhausted, and failing result cursors leak no pins
- differential results match SQLite for the supported subset


Commit the milestone separately from cleanup. The commit message should say what now works from a
user's point of view, not list internal files: `Run CREATE TABLE, INSERT, and SELECT end to end`.


---