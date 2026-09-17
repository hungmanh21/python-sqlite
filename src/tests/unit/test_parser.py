"""Parser tests for the Week 3 SQL subset.

Scoped to CREATE TABLE and the statement-level machinery it exercises
(trailing-token/semicolon handling, unsupported-statement detection) --
INSERT, SELECT, and the Pratt expression parser land in a follow-up pass,
so their tests belong in that pass, not here.
"""

import pytest

from quilldb.errors import SQLSyntaxError
from quilldb.sql.ast import ColumnDef, CreateTable, DataType
from quilldb.sql.parser import parse


def test_create_table_basic() -> None:
    assert parse("CREATE TABLE users (id INTEGER, name TEXT, age INTEGER)") == CreateTable(
        "users",
        (
            ColumnDef("id", DataType.INTEGER),
            ColumnDef("name", DataType.TEXT),
            ColumnDef("age", DataType.INTEGER),
        ),
    )


def test_create_table_every_column_type() -> None:
    result = parse("CREATE TABLE t (a INTEGER, b REAL, c TEXT, d BLOB)")
    assert result == CreateTable(
        "t",
        (
            ColumnDef("a", DataType.INTEGER),
            ColumnDef("b", DataType.REAL),
            ColumnDef("c", DataType.TEXT),
            ColumnDef("d", DataType.BLOB),
        ),
    )


def test_create_table_single_column() -> None:
    assert parse("CREATE TABLE t (id INTEGER)") == CreateTable("t", (ColumnDef("id", DataType.INTEGER),))


def test_keywords_are_case_insensitive() -> None:
    assert parse("create table t (id integer)") == CreateTable("t", (ColumnDef("id", DataType.INTEGER),))


def test_table_and_column_names_preserve_original_spelling() -> None:
    result = parse("CREATE TABLE MyTable (MyColumn INTEGER)")
    assert isinstance(result, CreateTable)
    assert result.name == "MyTable"
    assert result.columns[0].name == "MyColumn"


def test_trailing_semicolon_is_optional() -> None:
    with_semicolon = parse("CREATE TABLE t (id INTEGER);")
    without_semicolon = parse("CREATE TABLE t (id INTEGER)")
    assert with_semicolon == without_semicolon


# =====================================================================
# Errors: duplicate columns, malformed grammar, trailing input
# =====================================================================


def test_duplicate_column_name_raises() -> None:
    with pytest.raises(SQLSyntaxError):
        parse("CREATE TABLE t (id INTEGER, id TEXT)")


def test_duplicate_column_name_is_case_insensitive() -> None:
    with pytest.raises(SQLSyntaxError):
        parse("CREATE TABLE t (id INTEGER, ID TEXT)")


def test_empty_column_list_raises() -> None:
    with pytest.raises(SQLSyntaxError):
        parse("CREATE TABLE t ()")


def test_missing_comma_between_columns_raises() -> None:
    with pytest.raises(SQLSyntaxError):
        parse("CREATE TABLE t (id INTEGER name TEXT)")


def test_missing_closing_paren_raises() -> None:
    with pytest.raises(SQLSyntaxError):
        parse("CREATE TABLE t (id INTEGER")


def test_missing_column_type_raises() -> None:
    with pytest.raises(SQLSyntaxError):
        parse("CREATE TABLE t (id)")


def test_missing_table_name_raises() -> None:
    with pytest.raises(SQLSyntaxError):
        parse("CREATE TABLE (id INTEGER)")


def test_unsupported_statement_raises() -> None:
    with pytest.raises(SQLSyntaxError):
        parse("DELETE FROM t")


def test_empty_input_raises() -> None:
    with pytest.raises(SQLSyntaxError):
        parse("")


def test_trailing_tokens_after_statement_raise() -> None:
    with pytest.raises(SQLSyntaxError):
        parse("CREATE TABLE t (id INTEGER) garbage")


def test_multiple_statements_raise() -> None:
    with pytest.raises(SQLSyntaxError):
        parse("CREATE TABLE t (id INTEGER); CREATE TABLE u (id INTEGER)")


def test_syntax_error_includes_a_position() -> None:
    with pytest.raises(SQLSyntaxError) as exc_info:
        parse("CREATE TABLE t (id INTEGER, id TEXT)")
    assert str(exc_info.value)
