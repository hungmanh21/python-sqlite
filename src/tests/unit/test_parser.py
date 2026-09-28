"""Parser tests for the Week 3 SQL subset, plus week 7's FROM-clause grammar.

Scoped to CREATE TABLE and the statement-level machinery it exercises
(trailing-token/semicolon handling, unsupported-statement detection) --
INSERT, SELECT's expression list, and the Pratt expression parser are
exercised through sql/binder.py's tests instead, since a bound tree is
easier to assert on than a raw AST. The FROM-clause grammar (JOIN/ON,
comma joins, aliases, qualified names) is pure syntax with nothing to
bind yet, so it's tested directly against the AST here.
"""

import pytest

from quilldb.errors import SQLSyntaxError
from quilldb.sql.ast import (
    Column,
    ColumnDef,
    CreateTable,
    DataType,
    FunctionCall,
    JoinClause,
    Literal,
    OrderKey,
    Parameter,
    Select,
    TableRef,
)
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
        parse("DROP TABLE t")


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


# =====================================================================
# FROM clause: comma joins, JOIN ... ON, aliases, qualified names
# (week7-query-processing.md session 1)
# =====================================================================


def test_a_bare_select_has_no_joins() -> None:
    statement = parse("SELECT * FROM users")
    assert isinstance(statement, Select)
    assert statement.table == TableRef("users")
    assert statement.joins == ()


def test_a_comma_join_is_an_inner_join_with_no_on() -> None:
    statement = parse("SELECT * FROM users, orders")
    assert isinstance(statement, Select)
    assert statement.table == TableRef("users")
    assert statement.joins == (JoinClause("INNER", TableRef("orders"), on=None),)


def test_two_comma_joins_chain_in_order() -> None:
    statement = parse("SELECT * FROM a, b, c")
    assert isinstance(statement, Select)
    assert statement.joins == (
        JoinClause("INNER", TableRef("b"), on=None),
        JoinClause("INNER", TableRef("c"), on=None),
    )


def test_join_on_requires_and_captures_the_condition() -> None:
    statement = parse("SELECT * FROM users JOIN orders ON users.id = orders.user_id")
    assert isinstance(statement, Select)
    assert len(statement.joins) == 1
    join = statement.joins[0]
    assert join.join_type == "INNER"
    assert join.table == TableRef("orders")
    assert join.on is not None


def test_inner_join_keyword_is_equivalent_to_bare_join() -> None:
    statement = parse("SELECT * FROM users INNER JOIN orders ON users.id = orders.user_id")
    assert isinstance(statement, Select)
    assert statement.joins[0].join_type == "INNER"


def test_left_join_is_recorded_as_left() -> None:
    statement = parse("SELECT * FROM users LEFT JOIN orders ON users.id = orders.user_id")
    assert isinstance(statement, Select)
    assert statement.joins[0].join_type == "LEFT"


def test_left_outer_join_is_the_same_as_left_join() -> None:
    statement = parse("SELECT * FROM users LEFT OUTER JOIN orders ON users.id = orders.user_id")
    assert isinstance(statement, Select)
    assert statement.joins[0].join_type == "LEFT"


def test_join_without_on_raises() -> None:
    with pytest.raises(SQLSyntaxError):
        parse("SELECT * FROM users JOIN orders")


def test_table_alias_with_as() -> None:
    statement = parse("SELECT * FROM users AS u")
    assert isinstance(statement, Select)
    assert statement.table == TableRef("users", "u")


def test_table_alias_without_as() -> None:
    statement = parse("SELECT * FROM users u")
    assert isinstance(statement, Select)
    assert statement.table == TableRef("users", "u")


def test_joined_table_may_also_carry_an_alias() -> None:
    statement = parse("SELECT * FROM users u JOIN orders o ON u.id = o.user_id")
    assert isinstance(statement, Select)
    assert statement.table == TableRef("users", "u")
    assert statement.joins[0].table == TableRef("orders", "o")


def test_qualified_column_name_captures_its_table_prefix() -> None:
    statement = parse("SELECT u.id FROM users u")
    assert isinstance(statement, Select)
    assert statement.expressions == (Column("id", table="u"),)


def test_bare_column_name_has_no_table_prefix() -> None:
    statement = parse("SELECT id FROM users")
    assert isinstance(statement, Select)
    assert statement.expressions == (Column("id"),)


def test_where_still_parses_after_a_join() -> None:
    statement = parse("SELECT * FROM users u JOIN orders o ON u.id = o.user_id WHERE o.total > 10")
    assert isinstance(statement, Select)
    assert statement.where is not None


# =====================================================================
# FunctionCall (week 7 session 3, §42): syntax only -- whether "count" is
# a function quilldb knows, and whether its arguments make sense, is
# sql/binder.py's job (see test_binder.py's aggregate section).
# =====================================================================


def test_count_star_is_a_function_call_with_the_star_flag() -> None:
    statement = parse("SELECT COUNT(*) FROM t")
    assert isinstance(statement, Select)
    assert statement.expressions == (FunctionCall("COUNT", (), star=True),)


def test_function_call_with_one_argument() -> None:
    statement = parse("SELECT SUM(total) FROM t")
    assert isinstance(statement, Select)
    assert statement.expressions == (FunctionCall("SUM", (Column("total"),)),)


def test_function_call_name_is_not_case_normalized_by_the_parser() -> None:
    # Case-folding "count" vs "COUNT" is the binder's job (matching how a
    # bare Column's name isn't folded here either) -- the parser just
    # records exactly what the query wrote.
    statement = parse("SELECT count(*) FROM t")
    assert isinstance(statement, Select)
    assert statement.expressions == (FunctionCall("count", (), star=True),)


def test_function_call_multiple_arguments() -> None:
    statement = parse("SELECT FOO(a, b) FROM t")
    assert isinstance(statement, Select)
    assert statement.expressions == (FunctionCall("FOO", (Column("a"), Column("b"))),)


def test_function_call_argument_can_be_an_expression() -> None:
    statement = parse("SELECT SUM(a + 1) FROM t")
    assert isinstance(statement, Select)
    assert statement.expressions is not None
    call = statement.expressions[0]
    assert isinstance(call, FunctionCall)
    assert call.name == "SUM"
    assert len(call.args) == 1


def test_function_call_missing_close_paren_raises() -> None:
    with pytest.raises(SQLSyntaxError):
        parse("SELECT COUNT(* FROM t")


# =====================================================================
# GROUP BY, HAVING, DISTINCT (week 7 session 4): syntax only -- whether a
# select list's mix of aggregates/columns actually makes sense is
# sql/binder.py's job (test_binder.py's bind_aggregate_select section).
# =====================================================================


def test_a_bare_select_has_no_group_by_or_having() -> None:
    statement = parse("SELECT * FROM t")
    assert isinstance(statement, Select)
    assert statement.group_by == ()
    assert statement.having is None
    assert statement.distinct is False


def test_group_by_single_key() -> None:
    statement = parse("SELECT region, COUNT(*) FROM t GROUP BY region")
    assert isinstance(statement, Select)
    assert statement.group_by == (Column("region"),)


def test_group_by_multiple_keys_in_order() -> None:
    statement = parse("SELECT a, b FROM t GROUP BY a, b")
    assert isinstance(statement, Select)
    assert statement.group_by == (Column("a"), Column("b"))


def test_group_by_key_can_be_an_expression() -> None:
    statement = parse("SELECT COUNT(*) FROM t GROUP BY a + 1")
    assert isinstance(statement, Select)
    assert len(statement.group_by) == 1


def test_group_by_without_by_raises() -> None:
    with pytest.raises(SQLSyntaxError):
        parse("SELECT COUNT(*) FROM t GROUP region")


def test_having_captures_the_condition() -> None:
    statement = parse("SELECT COUNT(*) FROM t GROUP BY region HAVING COUNT(*) > 1")
    assert isinstance(statement, Select)
    assert statement.having is not None


def test_having_without_group_by_still_parses() -> None:
    statement = parse("SELECT COUNT(*) FROM t HAVING COUNT(*) > 1")
    assert isinstance(statement, Select)
    assert statement.group_by == ()
    assert statement.having is not None


def test_distinct_sets_the_flag() -> None:
    statement = parse("SELECT DISTINCT region FROM t")
    assert isinstance(statement, Select)
    assert statement.distinct is True
    assert statement.expressions == (Column("region"),)


def test_where_group_by_and_having_parse_together_in_order() -> None:
    statement = parse(
        "SELECT region, COUNT(*) FROM t WHERE total > 0 GROUP BY region HAVING COUNT(*) > 1"
    )
    assert isinstance(statement, Select)
    assert statement.where is not None
    assert statement.group_by == (Column("region"),)
    assert statement.having is not None


# =====================================================================
# ORDER BY, LIMIT, OFFSET (week 7 session 5): syntax only -- resolving an
# ordinal or a column ORDER BY didn't already select is sql/binder.py's
# job (test_binder.py's ORDER BY section).
# =====================================================================


def test_a_bare_select_has_no_order_by_limit_or_offset() -> None:
    statement = parse("SELECT * FROM t")
    assert isinstance(statement, Select)
    assert statement.order_by == ()
    assert statement.limit is None
    assert statement.offset is None


def test_order_by_single_key_defaults_to_ascending() -> None:
    statement = parse("SELECT * FROM t ORDER BY age")
    assert isinstance(statement, Select)
    assert statement.order_by == (OrderKey(Column("age"), descending=False),)


def test_order_by_asc_is_the_same_as_no_direction() -> None:
    statement = parse("SELECT * FROM t ORDER BY age ASC")
    assert isinstance(statement, Select)
    assert statement.order_by == (OrderKey(Column("age"), descending=False),)


def test_order_by_desc_sets_the_flag() -> None:
    statement = parse("SELECT * FROM t ORDER BY age DESC")
    assert isinstance(statement, Select)
    assert statement.order_by == (OrderKey(Column("age"), descending=True),)


def test_order_by_multiple_keys_with_mixed_directions() -> None:
    statement = parse("SELECT * FROM t ORDER BY region ASC, age DESC")
    assert isinstance(statement, Select)
    assert statement.order_by == (
        OrderKey(Column("region"), descending=False),
        OrderKey(Column("age"), descending=True),
    )


def test_order_by_an_ordinal() -> None:
    statement = parse("SELECT name, age FROM t ORDER BY 2")
    assert isinstance(statement, Select)
    assert statement.order_by == (OrderKey(Literal(2), descending=False),)


def test_order_by_without_by_raises() -> None:
    with pytest.raises(SQLSyntaxError):
        parse("SELECT * FROM t ORDER age")


def test_limit_captures_the_value() -> None:
    statement = parse("SELECT * FROM t LIMIT 10")
    assert isinstance(statement, Select)
    assert statement.limit == Literal(10)
    assert statement.offset is None


def test_limit_with_offset() -> None:
    statement = parse("SELECT * FROM t LIMIT 10 OFFSET 5")
    assert isinstance(statement, Select)
    assert statement.limit == Literal(10)
    assert statement.offset == Literal(5)


def test_limit_accepts_a_parameter() -> None:
    statement = parse("SELECT * FROM t LIMIT ?")
    assert isinstance(statement, Select)
    assert statement.limit == Parameter(0)


def test_where_group_by_having_order_by_limit_offset_parse_together_in_order() -> None:
    statement = parse(
        "SELECT region, COUNT(*) FROM t WHERE total > 0 GROUP BY region "
        "HAVING COUNT(*) > 1 ORDER BY 2 DESC LIMIT 10 OFFSET 5"
    )
    assert isinstance(statement, Select)
    assert statement.where is not None
    assert statement.group_by == (Column("region"),)
    assert statement.having is not None
    assert statement.order_by == (OrderKey(Literal(2), descending=True),)
    assert statement.limit == Literal(10)
    assert statement.offset == Literal(5)
