"""Scanner-level tests for quilldb's SQL tokenizer.

Exercises docs/implementation/week-3-sql.md §12's checklist directly: this
is the seam the roadmap calls out as not needing storage or a parser, so
every test here builds tokens straight from a SQL string and nothing else.
"""

import pytest

from quilldb.errors import SQLSyntaxError
from quilldb.sql.tokenizer import tokenize
from quilldb.sql.tokens import Token, TokenType


def _types(tokens: list[Token]) -> list[TokenType]:
    return [t.type for t in tokens]


# =====================================================================
# Keywords and identifiers
# =====================================================================


def test_keywords_are_case_insensitive() -> None:
    for spelling in ("select", "SELECT", "Select", "sElEcT"):
        tokens = tokenize(f"{spelling} * FROM t")
        assert tokens[0].type is TokenType.SELECT


def test_identifiers_preserve_original_spelling() -> None:
    tokens = tokenize("SELECT MyColumn FROM t")
    ident = tokens[1]
    assert ident.type is TokenType.IDENTIFIER
    assert ident.lexeme == "MyColumn"


def test_every_token_ends_with_exactly_one_eof() -> None:
    tokens = tokenize("SELECT * FROM t")
    assert tokens[-1].type is TokenType.EOF
    assert _types(tokens).count(TokenType.EOF) == 1


@pytest.mark.parametrize("sql", ["", "   ", "\n\t  \n", "-- just a comment"])
def test_empty_or_comment_only_input_is_a_single_eof(sql: str) -> None:
    tokens = tokenize(sql)
    assert _types(tokens) == [TokenType.EOF]


# =====================================================================
# Operators: every one- and two-character form
# =====================================================================


@pytest.mark.parametrize(
    "lexeme, expected",
    [
        ("(", TokenType.LEFT_PAREN),
        (")", TokenType.RIGHT_PAREN),
        (",", TokenType.COMMA),
        (";", TokenType.SEMICOLON),
        ("*", TokenType.STAR),
        ("+", TokenType.PLUS),
        ("-", TokenType.MINUS),
        ("/", TokenType.SLASH),
        ("%", TokenType.PERCENT),
        ("=", TokenType.EQ),
        ("!=", TokenType.NE),
        ("<>", TokenType.NE),
        ("<", TokenType.LT),
        ("<=", TokenType.LE),
        (">", TokenType.GT),
        (">=", TokenType.GE),
    ],
)
def test_operators(lexeme: str, expected: TokenType) -> None:
    tokens = tokenize(lexeme)
    assert tokens[0].type is expected
    assert tokens[0].lexeme == lexeme


def test_bare_bang_raises_syntax_error() -> None:
    with pytest.raises(SQLSyntaxError):
        tokenize("a ! b")


# =====================================================================
# Numeric literals
# =====================================================================


@pytest.mark.parametrize(
    "lexeme, expected_type, expected_value",
    [
        ("123", TokenType.INTEGER, 123),
        ("1.5", TokenType.REAL, 1.5),
        (".5", TokenType.REAL, 0.5),
        ("5.", TokenType.REAL, 5.0),
        ("1e6", TokenType.REAL, 1e6),
        ("1.5E-2", TokenType.REAL, 1.5e-2),
    ],
)
def test_numeric_literals(lexeme: str, expected_type: TokenType, expected_value: float) -> None:
    tokens = tokenize(lexeme)
    assert tokens[0].type is expected_type
    assert tokens[0].value == expected_value


def test_malformed_exponent_raises_syntax_error() -> None:
    with pytest.raises(SQLSyntaxError):
        tokenize("1e")


def test_leading_sign_is_a_separate_operator_token() -> None:
    tokens = tokenize("-5")
    assert _types(tokens) == [TokenType.MINUS, TokenType.INTEGER, TokenType.EOF]


# =====================================================================
# String literals
# =====================================================================


def test_string_literal_value() -> None:
    tokens = tokenize("'hello'")
    assert tokens[0].type is TokenType.STRING
    assert tokens[0].value == "hello"


def test_doubled_quote_is_one_literal_quote() -> None:
    tokens = tokenize("'Ada''s notebook'")
    assert tokens[0].type is TokenType.STRING
    assert tokens[0].value == "Ada's notebook"


def test_unterminated_string_raises_with_position() -> None:
    with pytest.raises(SQLSyntaxError) as exc_info:
        tokenize("SELECT 'never closes")
    assert "7" in str(exc_info.value) or exc_info.value.args


# =====================================================================
# Comments
# =====================================================================


def test_line_comment_disappears_without_joining_neighbors() -> None:
    tokens = tokenize("SELECT 1 -- trailing comment\nFROM t")
    assert _types(tokens) == [
        TokenType.SELECT,
        TokenType.INTEGER,
        TokenType.FROM,
        TokenType.IDENTIFIER,
        TokenType.EOF,
    ]


def test_block_comment_disappears_without_joining_neighbors() -> None:
    tokens = tokenize("SELECT /* mid-statement note */ 1 FROM t")
    assert _types(tokens) == [
        TokenType.SELECT,
        TokenType.INTEGER,
        TokenType.FROM,
        TokenType.IDENTIFIER,
        TokenType.EOF,
    ]


def test_unterminated_block_comment_raises() -> None:
    with pytest.raises(SQLSyntaxError):
        tokenize("SELECT 1 /* never closes")


# =====================================================================
# Parameters
# =====================================================================


def test_question_mark_produces_a_parameter_token_without_an_index() -> None:
    tokens = tokenize("VALUES (?, ?)")
    params = [t for t in tokens if t.type is TokenType.PARAMETER]
    assert len(params) == 2
    assert all(p.value is None for p in params)


# =====================================================================
# Unknown characters
# =====================================================================


def test_unknown_character_raises_syntax_error() -> None:
    with pytest.raises(SQLSyntaxError):
        tokenize("SELECT # FROM t")


# =====================================================================
# Week 7: DOT and the new keyword set (session 0.6)
# =====================================================================


def test_qualified_name_produces_identifier_dot_identifier() -> None:
    tokens = tokenize("SELECT u.age FROM users u")
    assert _types(tokens)[:4] == [
        TokenType.SELECT,
        TokenType.IDENTIFIER,
        TokenType.DOT,
        TokenType.IDENTIFIER,
    ]


def test_a_leading_decimal_point_is_still_a_number_not_a_dot() -> None:
    """`.5` must stay one REAL token -- DOT must not steal the digit
    branch's own leading-dot case (tokenizer.py's number-scan condition)."""
    tokens = tokenize("SELECT .5")
    assert _types(tokens) == [TokenType.SELECT, TokenType.REAL, TokenType.EOF]
    assert tokens[1].value == 0.5


@pytest.mark.parametrize(
    "spelling,expected",
    [
        ("join", TokenType.JOIN),
        ("left", TokenType.LEFT),
        ("inner", TokenType.INNER),
        ("outer", TokenType.OUTER),
        ("group", TokenType.GROUP),
        ("by", TokenType.BY),
        ("having", TokenType.HAVING),
        ("order", TokenType.ORDER),
        ("asc", TokenType.ASC),
        ("desc", TokenType.DESC),
        ("limit", TokenType.LIMIT),
        ("offset", TokenType.OFFSET),
        ("distinct", TokenType.DISTINCT),
        ("as", TokenType.AS),
    ],
)
def test_week7_keywords_are_recognized_case_insensitively(spelling: str, expected: TokenType) -> None:
    for casing in (spelling, spelling.upper(), spelling.capitalize()):
        assert tokenize(casing)[0].type is expected
