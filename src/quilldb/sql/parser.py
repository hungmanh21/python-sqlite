"""Recursive-descent statements plus a Pratt expression parser.

Statements use straightforward recursive descent -- one method per grammar
rule, each consuming exactly the tokens its rule owns. Expressions (added
alongside INSERT/SELECT in a later pass) use precedence climbing instead,
so adding an operator later is one binding-power table entry rather than
another call-stack layer.
"""

from quilldb.errors import SQLSyntaxError
from quilldb.sql.ast import ColumnDef, CreateTable, DataType, Expression, Statement
from quilldb.sql.tokenizer import tokenize
from quilldb.sql.tokens import Token, TokenType

_COLUMN_TYPES: dict[TokenType, DataType] = {
    TokenType.INTEGER_TYPE: DataType.INTEGER,
    TokenType.REAL_TYPE: DataType.REAL,
    TokenType.TEXT_TYPE: DataType.TEXT,
    TokenType.BLOB_TYPE: DataType.BLOB,
}


def parse(sql: str) -> Statement:
    """Parse exactly one statement with an optional trailing semicolon.

    Args:
        sql: the full source text -- one statement, optionally `;`-terminated.
    Returns:
        The parsed Statement.
    Raises:
        SQLSyntaxError: empty input, unsupported statement, unexpected
            token, trailing tokens after the statement (including a second
            statement), duplicate column name, or malformed expression.
    """
    return Parser(tokenize(sql)).parse_statement()


class Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self.tokens = tokens
        self.current = 0
        self.parameter_count = 0

    def parse_statement(self) -> Statement:
        token = self._peek()
        if token.type is TokenType.CREATE:
            statement = self._create_table()
        elif token.type is TokenType.INSERT:
            statement = self._insert()
        elif token.type is TokenType.SELECT:
            statement = self._select()
        else:
            raise SQLSyntaxError(
                f"expected CREATE, INSERT, or SELECT, found {token.lexeme!r} at position {token.position}"
            )

        if self._peek().type is TokenType.SEMICOLON:
            self._advance()
        self._expect(TokenType.EOF, "unexpected trailing input after statement")
        return statement

    def _create_table(self) -> Statement:
        self._expect(TokenType.CREATE, "expected CREATE")
        self._expect(TokenType.TABLE, "expected TABLE")
        name = self._expect(TokenType.IDENTIFIER, "expected a table name").lexeme
        self._expect(TokenType.LEFT_PAREN, "expected '(' after table name")

        columns: list[ColumnDef] = []
        seen_names: set[str] = set()
        while True:
            name_token = self._expect(TokenType.IDENTIFIER, "expected a column name")
            folded = name_token.lexeme.casefold()
            if folded in seen_names:
                raise SQLSyntaxError(f"duplicate column {name_token.lexeme!r} at position {name_token.position}")
            seen_names.add(folded)

            type_token = self._advance()
            if type_token.type not in _COLUMN_TYPES:
                raise SQLSyntaxError(f"expected a column type at position {type_token.position}")
            columns.append(ColumnDef(name_token.lexeme, _COLUMN_TYPES[type_token.type]))

            if self._peek().type is not TokenType.COMMA:
                break
            self._advance()

        self._expect(TokenType.RIGHT_PAREN, "expected ')' to close the column list")
        return CreateTable(name, tuple(columns))

    def _insert(self) -> Statement:
        raise NotImplementedError

    def _select(self) -> Statement:
        raise NotImplementedError

    def _expression(self, min_binding_power: int = 0) -> Expression:
        raise NotImplementedError

    def _prefix(self) -> Expression:
        raise NotImplementedError

    def _peek(self) -> Token:
        return self.tokens[self.current]

    def _advance(self) -> Token:
        token = self.tokens[self.current]
        self.current += 1
        return token

    def _expect(self, token_type: TokenType, message: str) -> Token:
        token = self._peek()
        if token.type is not token_type:
            raise SQLSyntaxError(f"{message}, found {token.lexeme!r} at position {token.position}")
        return self._advance()
