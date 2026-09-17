"""Tests for the pure in-memory schema dataclasses (no b-tree/pager involved
-- catalog/catalog.py's durability tests exercise those together)."""

import pytest

from quilldb.catalog.schema import ColumnSchema, TableSchema
from quilldb.errors import ColumnNotFoundError
from quilldb.sql.ast import DataType

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


def test_column_index_matches_exact_case() -> None:
    assert _USERS.column_index("name") == 1


def test_column_index_is_case_insensitive() -> None:
    assert _USERS.column_index("NAME") == 1
    assert _USERS.column_index("Name") == 1


def test_column_index_first_and_last() -> None:
    assert _USERS.column_index("id") == 0
    assert _USERS.column_index("age") == 2


def test_column_index_unknown_column_raises() -> None:
    with pytest.raises(ColumnNotFoundError):
        _USERS.column_index("nickname")
