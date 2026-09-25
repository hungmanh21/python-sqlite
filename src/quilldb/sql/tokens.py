"""Tokens produced by the SQL scanner.

Token kinds exist for punctuation/operators and for the small keyword set.
Keeping keywords distinct from IDENTIFIER makes parser branches readable
("expect SELECT" beats "expect an identifier spelled 'select'"); preserving
the original lexeme on every token (not just identifiers) makes error
messages useful without a second pass over the source text.
"""

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
    NE = auto()  # both != and <>
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
    DELETE = auto()
    UPDATE = auto()
    SET = auto()
    INDEX = auto()
    ON = auto()
    UNIQUE = auto()
    ANALYZE = auto()
    EXPLAIN = auto()
    BEGIN = auto()
    COMMIT = auto()
    ROLLBACK = auto()
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
    """Zero-based character offset of this token's first character in the
    original SQL string. Only an offset is kept -- not line/column -- because
    deriving line/column from an offset is cheap (see _line_and_column in
    tokenizer.py) while recovering an exact offset from a stored line/column
    is not, and every downstream consumer (parser errors, binder errors)
    only ever needs to point at *one* character.
    """


KEYWORDS: dict[str, TokenType] = {
    "create": TokenType.CREATE,
    "table": TokenType.TABLE,
    "insert": TokenType.INSERT,
    "into": TokenType.INTO,
    "values": TokenType.VALUES,
    "select": TokenType.SELECT,
    "from": TokenType.FROM,
    "where": TokenType.WHERE,
    "delete": TokenType.DELETE,
    "update": TokenType.UPDATE,
    "set": TokenType.SET,
    "index": TokenType.INDEX,
    "on": TokenType.ON,
    "unique": TokenType.UNIQUE,
    "analyze": TokenType.ANALYZE,
    "explain": TokenType.EXPLAIN,
    "begin": TokenType.BEGIN,
    "commit": TokenType.COMMIT,
    "rollback": TokenType.ROLLBACK,
    "integer": TokenType.INTEGER_TYPE,
    "real": TokenType.REAL_TYPE,
    "text": TokenType.TEXT_TYPE,
    "blob": TokenType.BLOB_TYPE,
    "null": TokenType.NULL,
    "true": TokenType.TRUE,
    "false": TokenType.FALSE,
    "and": TokenType.AND,
    "or": TokenType.OR,
    "not": TokenType.NOT,
    "is": TokenType.IS,
    "like": TokenType.LIKE,
}