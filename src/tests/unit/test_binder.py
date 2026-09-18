"""Binder tests: name resolution, parameter substitution, declared-type checks.


Every test here runs against a fake SchemaSource -- no Pager, no BufferPool,
no temp file. That isn't a shortcut, it's the assertion: binding is
structurally incapable of opening a cursor or dirtying a page, so an unknown
table or column provably fails before execution (§17's requirement). If
bind() ever grew a storage dependency, this entire module would stop
importing.
"""


import datetime


import pytest


from quilldb.catalog.schema import ColumnSchema, TableSchema
from quilldb.errors import (
    ColumnCountError,
    ColumnNotFoundError,
    ParameterCountError,
    TableNotFoundError,
    TypeMismatchError,
    UnsupportedFeatureError,
)
from quilldb.sql.ast import CreateIndex, CreateTable, DataType
from quilldb.sql.binder import (
    BoundBinaryOp,
    BoundColumn,
    BoundCreateIndex,
    BoundCreateTable,
    BoundInsert,
    BoundIsNull,
    BoundLiteral,
    BoundSelect,
    BoundUnaryOp,
    bind,
)
from quilldb.sql.parser import parse


_USERS = TableSchema(
    "users",
    (
        ColumnSchema("id", DataType.INTEGER),
        ColumnSchema("name", DataType.TEXT),
        ColumnSchema("age", DataType.INTEGER),
    ),
    root_page=2,
    sql="CREATE TABLE users (id INTEGER, name TEXT, age INTEGER)",
)


_TYPES = TableSchema(
    "types",
    (
        ColumnSchema("i", DataType.INTEGER),
        ColumnSchema("r", DataType.REAL),
        ColumnSchema("t", DataType.TEXT),
        ColumnSchema("b", DataType.BLOB),
    ),
    root_page=3,
    sql="CREATE TABLE types (i INTEGER, r REAL, t TEXT, b BLOB)",
)




class _FakeCatalog:
    """Satisfies SchemaSource with a dict. Nothing else -- if bind() ever
    reaches for a pager or a b-tree, it will fail here with AttributeError
    instead of quietly touching storage.
    """


    def __init__(self, *tables: TableSchema) -> None:
        self._tables = {t.name.casefold(): t for t in tables}


    def get_table(self, name: str) -> TableSchema:
        try:
            return self._tables[name.casefold()]
        except KeyError:
            raise TableNotFoundError(f"no such table: {name}") from None




def _bind(sql: str, *parameters: object, catalog: _FakeCatalog | None = None):
    return bind(parse(sql), catalog or _FakeCatalog(_USERS, _TYPES), tuple(parameters))




# =====================================================================
# CREATE TABLE: nothing to resolve, passes through
# =====================================================================




def test_create_table_binds_to_a_passthrough_wrapper() -> None:
    statement = parse("CREATE TABLE fresh (id INTEGER)")
    assert isinstance(statement, CreateTable)
    bound = bind(statement, _FakeCatalog())
    assert bound == BoundCreateTable(statement)




def test_create_table_does_not_need_the_table_to_exist() -> None:
    # It INTRODUCES the name rather than referring to it, so an empty
    # catalog is fine -- no TableNotFoundError.
    bound = _bind("CREATE TABLE brand_new (id INTEGER)", catalog=_FakeCatalog())
    assert isinstance(bound, BoundCreateTable)




# =====================================================================
# CREATE INDEX: also a passthrough -- table/column resolution happens
# inside Catalog.create_index(), not here (see BoundCreateIndex's docstring)
# =====================================================================




def test_create_index_binds_to_a_passthrough_wrapper() -> None:
    statement = parse("CREATE INDEX idx_name ON users (name)")
    assert isinstance(statement, CreateIndex)
    bound = bind(statement, _FakeCatalog(_USERS))
    assert bound == BoundCreateIndex(statement)




def test_create_index_does_not_need_the_table_to_exist() -> None:
    # Same reasoning as CREATE TABLE against an empty catalog: nothing here
    # resolves `table`/`columns`, so a nonexistent table doesn't raise yet.
    bound = _bind("CREATE INDEX idx_ghost ON ghost (col)", catalog=_FakeCatalog())
    assert isinstance(bound, BoundCreateIndex)




# =====================================================================
# SELECT: name resolution
# =====================================================================




def test_select_star_expands_to_every_column_in_declaration_order() -> None:
    bound = _bind("SELECT * FROM users")
    assert isinstance(bound, BoundSelect)
    assert bound.expressions == (
        BoundColumn(0, "id", DataType.INTEGER),
        BoundColumn(1, "name", DataType.TEXT),
        BoundColumn(2, "age", DataType.INTEGER),
    )
    assert bound.where is None




def test_select_resolves_named_columns_to_indices() -> None:
    bound = _bind("SELECT age, id FROM users")
    assert isinstance(bound, BoundSelect)
    assert bound.expressions == (
        BoundColumn(2, "age", DataType.INTEGER),
        BoundColumn(0, "id", DataType.INTEGER),
    )




def test_select_carries_the_resolved_table_schema() -> None:
    bound = _bind("SELECT id FROM users")
    assert isinstance(bound, BoundSelect)
    assert bound.table is _USERS




def test_column_names_resolve_case_insensitively_but_report_declared_spelling() -> None:
    bound = _bind("SELECT NAME FROM users")
    assert isinstance(bound, BoundSelect)
    assert bound.expressions == (BoundColumn(1, "name", DataType.TEXT),)




def test_table_names_resolve_case_insensitively() -> None:
    bound = _bind("SELECT id FROM USERS")
    assert isinstance(bound, BoundSelect)
    assert bound.table is _USERS




def test_unknown_table_raises() -> None:
    with pytest.raises(TableNotFoundError):
        _bind("SELECT id FROM ghosts")




def test_unknown_column_in_projection_raises() -> None:
    with pytest.raises(ColumnNotFoundError):
        _bind("SELECT nickname FROM users")




def test_unknown_column_in_where_raises() -> None:
    with pytest.raises(ColumnNotFoundError):
        _bind("SELECT id FROM users WHERE nickname = 'ada'")




def test_unknown_column_in_a_nested_expression_raises() -> None:
    with pytest.raises(ColumnNotFoundError):
        _bind("SELECT id FROM users WHERE (age + nickname) > 1")




# =====================================================================
# SELECT: expression tree rewriting
# =====================================================================




def test_where_binds_a_comparison() -> None:
    bound = _bind("SELECT id FROM users WHERE age > 30")
    assert isinstance(bound, BoundSelect)
    assert bound.where == BoundBinaryOp(BoundColumn(2, "age", DataType.INTEGER), ">", BoundLiteral(30))




def test_nested_binary_ops_are_bound_recursively() -> None:
    bound = _bind("SELECT id FROM users WHERE age > 30 AND name = 'ada'")
    assert isinstance(bound, BoundSelect)
    assert bound.where == BoundBinaryOp(
        BoundBinaryOp(BoundColumn(2, "age", DataType.INTEGER), ">", BoundLiteral(30)),
        "AND",
        BoundBinaryOp(BoundColumn(1, "name", DataType.TEXT), "=", BoundLiteral("ada")),
    )




def test_unary_op_is_bound() -> None:
    bound = _bind("SELECT id FROM users WHERE NOT age = 30")
    assert isinstance(bound, BoundSelect)
    assert bound.where == BoundUnaryOp(
        "NOT", BoundBinaryOp(BoundColumn(2, "age", DataType.INTEGER), "=", BoundLiteral(30))
    )




def test_is_null_is_bound_and_keeps_its_negation() -> None:
    bound = _bind("SELECT id FROM users WHERE age IS NOT NULL")
    assert isinstance(bound, BoundSelect)
    assert bound.where == BoundIsNull(BoundColumn(2, "age", DataType.INTEGER), True)




def test_expression_in_projection_is_bound() -> None:
    bound = _bind("SELECT age + 1 FROM users")
    assert isinstance(bound, BoundSelect)
    assert bound.expressions == (
        BoundBinaryOp(BoundColumn(2, "age", DataType.INTEGER), "+", BoundLiteral(1)),
    )




def test_null_literal_binds_to_none() -> None:
    bound = _bind("SELECT NULL FROM users")
    assert isinstance(bound, BoundSelect)
    assert bound.expressions == (BoundLiteral(None),)




# =====================================================================
# Parameters
# =====================================================================




def test_parameter_in_where_is_substituted() -> None:
    bound = _bind("SELECT name FROM users WHERE age > ?", 30)
    assert isinstance(bound, BoundSelect)
    assert bound.where == BoundBinaryOp(BoundColumn(2, "age", DataType.INTEGER), ">", BoundLiteral(30))




def test_parameters_are_substituted_positionally_across_the_whole_statement() -> None:
    bound = _bind("SELECT ? FROM users WHERE age > ?", "first", 30)
    assert isinstance(bound, BoundSelect)
    assert bound.expressions == (BoundLiteral("first"),)
    assert bound.where == BoundBinaryOp(
        BoundColumn(2, "age", DataType.INTEGER), ">", BoundLiteral(30)
    )




def test_too_few_parameters_raises() -> None:
    with pytest.raises(ParameterCountError):
        _bind("SELECT id FROM users WHERE age > ?")




def test_too_many_parameters_raises() -> None:
    with pytest.raises(ParameterCountError):
        _bind("SELECT id FROM users WHERE age > ?", 30, 40)




def test_unused_parameters_are_an_error_not_silently_ignored() -> None:
    with pytest.raises(ParameterCountError):
        _bind("SELECT id FROM users", 30)




def test_a_none_parameter_is_a_valid_value_not_a_missing_one() -> None:
    bound = _bind("SELECT id FROM users WHERE age > ?", None)
    assert isinstance(bound, BoundSelect)
    assert bound.where == BoundBinaryOp(
        BoundColumn(2, "age", DataType.INTEGER), ">", BoundLiteral(None)
    )




# A WHERE-clause parameter has no declared column type to be checked
# against, so before _require_bindable() nothing checked it at all -- these
# all bound successfully and only failed later, inside the evaluator.
@pytest.mark.parametrize(
    "value",
    [
        True,                              # bool: an int subclass, so `True > 30` would
        False,                             # silently evaluate rather than raise
        datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC),
        [1, 2, 3],
        {"a": 1},
        bytearray(b"x"),                   # not a bytes subclass; Value doesn't cover it
        object(),
    ],
)
def test_a_parameter_that_is_not_a_storable_value_raises(value: object) -> None:
    with pytest.raises(TypeMismatchError):
        _bind("SELECT id FROM users WHERE age > ?", value)




@pytest.mark.parametrize("value", [None, 0, -1, 2.5, "", "ada", b"", b"\x00\xff"])
def test_every_storable_value_is_accepted_as_a_parameter(value: object) -> None:
    bound = _bind("SELECT id FROM users WHERE age > ?", value)
    assert isinstance(bound, BoundSelect)
    assert bound.where == BoundBinaryOp(
        BoundColumn(2, "age", DataType.INTEGER), ">", BoundLiteral(value)
    )




def test_an_unusable_parameter_is_rejected_before_the_table_is_resolved() -> None:
    # The point of checking in bind() rather than the evaluator: the caller's
    # arguments are wrong regardless of whether the table even exists, and
    # saying so does not require a catalog lookup.
    with pytest.raises(TypeMismatchError):
        _bind("SELECT id FROM ghosts WHERE id > ?", object())




# =====================================================================
# INSERT: arity, constants, declared types
# =====================================================================




def test_insert_binds_literals_and_carries_the_table() -> None:
    bound = _bind("INSERT INTO users VALUES (1, 'ada', 36)")
    assert bound == BoundInsert(_USERS, (1, "ada", 36))




def test_insert_substitutes_parameters() -> None:
    bound = _bind("INSERT INTO users VALUES (?, ?, ?)", 1, "ada", 36)
    assert bound == BoundInsert(_USERS, (1, "ada", 36))




def test_insert_accepts_null_for_any_column() -> None:
    bound = _bind("INSERT INTO users VALUES (NULL, NULL, NULL)")
    assert bound == BoundInsert(_USERS, (None, None, None))




def test_insert_folds_a_unary_minus() -> None:
    bound = _bind("INSERT INTO users VALUES (-1, 'ada', -36)")
    assert bound == BoundInsert(_USERS, (-1, "ada", -36))




def test_insert_unknown_table_raises() -> None:
    with pytest.raises(TableNotFoundError):
        _bind("INSERT INTO ghosts VALUES (1)")




def test_insert_too_few_values_raises_column_count_error() -> None:
    with pytest.raises(ColumnCountError):
        _bind("INSERT INTO users VALUES (1, 'ada')")




def test_insert_too_many_values_raises_column_count_error() -> None:
    with pytest.raises(ColumnCountError):
        _bind("INSERT INTO users VALUES (1, 'ada', 36, 'extra')")




def test_insert_rejects_a_column_reference_as_a_value() -> None:
    with pytest.raises(UnsupportedFeatureError):
        _bind("INSERT INTO users VALUES (id, 'ada', 36)")




def test_insert_rejects_arithmetic_pending_the_evaluator() -> None:
    # Documented narrow scope: constant folding of `+ - * / %` waits for
    # exec/expressions.py rather than being duplicated here.
    with pytest.raises(UnsupportedFeatureError):
        _bind("INSERT INTO users VALUES (1 + 1, 'ada', 36)")




# =====================================================================
# INSERT: the declared-type table from §17
# =====================================================================




def test_integer_column_accepts_an_int() -> None:
    bound = _bind("INSERT INTO types VALUES (?, 1.0, 't', ?)", 5, b"x")
    assert isinstance(bound, BoundInsert)
    assert bound.values[0] == 5




def test_real_column_coerces_an_int_to_float() -> None:
    bound = _bind("INSERT INTO types VALUES (1, ?, 't', ?)", 5, b"x")
    assert isinstance(bound, BoundInsert)
    assert bound.values[1] == 5.0
    assert isinstance(bound.values[1], float)




def test_real_column_accepts_a_float() -> None:
    bound = _bind("INSERT INTO types VALUES (1, 2.5, 't', ?)", b"x")
    assert isinstance(bound, BoundInsert)
    assert bound.values[1] == 2.5




@pytest.mark.parametrize(
    "values, parameters",
    [
        ("(?, 1.0, 't', ?)", (True, b"x")),      # bool into INTEGER
        ("(1, ?, 't', ?)", (True, b"x")),        # bool into REAL
        ("(?, 1.0, 't', ?)", ("42", b"x")),      # str into INTEGER (no affinity)
        ("(?, 1.0, 't', ?)", (2.5, b"x")),       # float into INTEGER
        ("(1, ?, 't', ?)", ("2.5", b"x")),       # str into REAL
        ("(1, 1.0, ?, ?)", (5, b"x")),           # int into TEXT
        ("(1, 1.0, ?, ?)", (b"bytes", b"x")),    # bytes into TEXT
        ("(1, 1.0, 't', ?)", ("not bytes",)),    # str into BLOB
        ("(1, 1.0, 't', ?)", (5,)),              # int into BLOB
    ],
)
def test_declared_type_violations_raise(values: str, parameters: tuple[object, ...]) -> None:
    with pytest.raises(TypeMismatchError):
        _bind(f"INSERT INTO types VALUES {values}", *parameters)




def test_blob_column_accepts_bytes() -> None:
    bound = _bind("INSERT INTO types VALUES (1, 1.0, 't', ?)", b"\x00\xff")
    assert isinstance(bound, BoundInsert)
    assert bound.values[3] == b"\x00\xff"




def test_text_column_accepts_str() -> None:
    bound = _bind("INSERT INTO types VALUES (1, 1.0, ?, ?)", "hello", b"x")
    assert isinstance(bound, BoundInsert)
    assert bound.values[2] == "hello"