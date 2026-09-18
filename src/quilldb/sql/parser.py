"""Recursive-descent statements plus a Pratt expression parser.


Statements use straightforward recursive descent -- one method per grammar
rule, each consuming exactly the tokens its rule owns. Expressions (added
alongside INSERT/SELECT in a later pass) use precedence climbing instead,
so adding an operator later is one binding-power table entry rather than
another call-stack layer.
"""


from quilldb.errors import SQLSyntaxError
from quilldb.sql.ast import (
    Analyze,
    Assignment,
    BinaryOp,
    Column,
    ColumnDef,
    CreateIndex,
    CreateTable,
    DataType,
    Delete,
    Explain,
    Expression,
    Insert,
    IsNull,
    Literal,
    Parameter,
    Select,
    Statement,
    UnaryOp,
    Update,
)
from quilldb.sql.tokenizer import tokenize
from quilldb.sql.tokens import Token, TokenType


_COLUMN_TYPES: dict[TokenType, DataType] = {
    TokenType.INTEGER_TYPE: DataType.INTEGER,
    TokenType.REAL_TYPE: DataType.REAL,
    TokenType.TEXT_TYPE: DataType.TEXT,
    TokenType.BLOB_TYPE: DataType.BLOB,
}


# Binding power per SQLite's real precedence table (sqlite.org/lang_expr.html),
# highest to lowest: unary +/- > arithmetic > comparisons/equality/IS/LIKE >
# NOT [expr] > AND > OR. Comparisons/IS/LIKE share one tier and are
# non-chainable: `a < b < c` is rejected rather than silently associating
# some direction SQL never defined. NOT gets its own tier strictly between
# AND and the comparison group -- tighter than AND (so `NOT a AND b` is
# `(NOT a) AND b`), looser than `=`/`LIKE`/`IS` (so `NOT a = b` is
# `NOT (a = b)`), matching real SQLite rather than treating NOT as just
# another prefix operator alongside unary +/-.
_COMPARISON_BINDING_POWER = 30
_NOT_BINDING_POWER = 25
_UNARY_ARITHMETIC_BINDING_POWER = 60


_BINARY_OPERATORS: dict[TokenType, tuple[int, str]] = {
    TokenType.OR: (10, "OR"),
    TokenType.AND: (20, "AND"),
    TokenType.EQ: (_COMPARISON_BINDING_POWER, "="),
    TokenType.NE: (_COMPARISON_BINDING_POWER, "!="),
    TokenType.LT: (_COMPARISON_BINDING_POWER, "<"),
    TokenType.LE: (_COMPARISON_BINDING_POWER, "<="),
    TokenType.GT: (_COMPARISON_BINDING_POWER, ">"),
    TokenType.GE: (_COMPARISON_BINDING_POWER, ">="),
    TokenType.LIKE: (_COMPARISON_BINDING_POWER, "LIKE"),
    TokenType.PLUS: (40, "+"),
    TokenType.MINUS: (40, "-"),
    TokenType.STAR: (50, "*"),
    TokenType.SLASH: (50, "/"),
    TokenType.PERCENT: (50, "%"),
}


_UNARY_ARITHMETIC_OPERATORS: dict[TokenType, str] = {
    TokenType.PLUS: "+",
    TokenType.MINUS: "-",
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
            statement = self._create()
        elif token.type is TokenType.INSERT:
            statement = self._insert()
        elif token.type is TokenType.SELECT:
            statement = self._select()
        elif token.type is TokenType.DELETE:
            statement = self._delete()
        elif token.type is TokenType.UPDATE:
            statement = self._update()
        elif token.type is TokenType.ANALYZE:
            statement = self._analyze()
        elif token.type is TokenType.EXPLAIN:
            statement = self._explain()
        else:
            raise SQLSyntaxError(
                f"expected a statement, found {token.lexeme!r} at position {token.position}"
            )


        if self._peek().type is TokenType.SEMICOLON:
            self._advance()
        self._expect(TokenType.EOF, "unexpected trailing input after statement")
        return statement


    def _create(self) -> Statement:
        self._expect(TokenType.CREATE, "expected CREATE")


        unique = False
        if self._peek().type is TokenType.UNIQUE:
            self._advance()
            unique = True


        if unique or self._peek().type is TokenType.INDEX:
            return self._create_index(unique)


        return self._create_table()


    def _create_table(self) -> CreateTable:
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


    def _create_index(self, unique: bool) -> CreateIndex:
        self._expect(TokenType.INDEX, "expected INDEX")
        name = self._expect(TokenType.IDENTIFIER, "expected an index name").lexeme
        self._expect(TokenType.ON, "expected ON")
        table = self._expect(TokenType.IDENTIFIER, "expected a table name").lexeme
        self._expect(TokenType.LEFT_PAREN, "expected '(' before the column list")


        columns = [self._expect(TokenType.IDENTIFIER, "expected a column name").lexeme]
        while self._peek().type is TokenType.COMMA:
            self._advance()
            columns.append(self._expect(TokenType.IDENTIFIER, "expected a column name").lexeme)


        self._expect(TokenType.RIGHT_PAREN, "expected ')' to close the column list")
        return CreateIndex(name, table, tuple(columns), unique)


    def _insert(self) -> Insert:
        self._expect(TokenType.INSERT, "expected INSERT")
        self._expect(TokenType.INTO, "expected INTO")
        table = self._expect(TokenType.IDENTIFIER, "expected a table name").lexeme
        self._expect(TokenType.VALUES, "expected VALUES")
        self._expect(TokenType.LEFT_PAREN, "expected '(' before the VALUES list")


        values = [self._expression()]
        while self._peek().type is TokenType.COMMA:
            self._advance()
            values.append(self._expression())


        self._expect(TokenType.RIGHT_PAREN, "expected ')' to close the VALUES list")
        return Insert(table, tuple(values))


    def _select(self) -> Select:
        self._expect(TokenType.SELECT, "expected SELECT")


        expressions: tuple[Expression, ...] | None
        if self._peek().type is TokenType.STAR:
            self._advance()
            expressions = None
        else:
            exprs = [self._expression()]
            while self._peek().type is TokenType.COMMA:
                self._advance()
                exprs.append(self._expression())
            expressions = tuple(exprs)


        self._expect(TokenType.FROM, "expected FROM")
        table = self._expect(TokenType.IDENTIFIER, "expected a table name").lexeme


        where: Expression | None = None
        if self._peek().type is TokenType.WHERE:
            self._advance()
            where = self._expression()


        return Select(expressions, table, where)


    def _delete(self) -> Delete:
        self._expect(TokenType.DELETE, "expected DELETE")
        self._expect(TokenType.FROM, "expected FROM")
        table = self._expect(TokenType.IDENTIFIER, "expected a table name").lexeme


        where: Expression | None = None
        if self._peek().type is TokenType.WHERE:
            self._advance()
            where = self._expression()


        return Delete(table, where)


    def _update(self) -> Update:
        self._expect(TokenType.UPDATE, "expected UPDATE")
        table = self._expect(TokenType.IDENTIFIER, "expected a table name").lexeme
        self._expect(TokenType.SET, "expected SET")


        assignments = [self._assignment()]
        while self._peek().type is TokenType.COMMA:
            self._advance()
            assignments.append(self._assignment())


        where: Expression | None = None
        if self._peek().type is TokenType.WHERE:
            self._advance()
            where = self._expression()


        return Update(table, tuple(assignments), where)


    def _assignment(self) -> Assignment:
        column = self._expect(TokenType.IDENTIFIER, "expected a column name").lexeme
        self._expect(TokenType.EQ, "expected '=' in SET clause")
        value = self._expression()
        return Assignment(column, value)


    def _analyze(self) -> Analyze:
        self._expect(TokenType.ANALYZE, "expected ANALYZE")
        target: str | None = None
        if self._peek().type is TokenType.IDENTIFIER:
            target = self._advance().lexeme
        return Analyze(target)


    def _explain(self) -> Explain:
        self._expect(TokenType.EXPLAIN, "expected EXPLAIN")
        analyze = False
        if self._peek().type is TokenType.ANALYZE:
            self._advance()
            analyze = True
        return Explain(self._select(), analyze)


    def _expression(self, min_binding_power: int = 0) -> Expression:
        left = self._prefix()
        used_comparison = False


        while True:
            token = self._peek()


            if token.type is TokenType.IS:
                if _COMPARISON_BINDING_POWER < min_binding_power or used_comparison:
                    break
                self._advance()
                negated = False
                if self._peek().type is TokenType.NOT:
                    self._advance()
                    negated = True
                self._expect(TokenType.NULL, "expected NULL after IS [NOT]")
                left = IsNull(left, negated)
                used_comparison = True
                continue


            operator = _BINARY_OPERATORS.get(token.type)
            if operator is None:
                break
            binding_power, operator_lexeme = operator
            if binding_power < min_binding_power:
                break
            if binding_power == _COMPARISON_BINDING_POWER and used_comparison:
                break  # reject a second comparison-tier op chained onto this one


            self._advance()
            right = self._expression(binding_power + 1)
            left = BinaryOp(left, operator_lexeme, right)
            if binding_power == _COMPARISON_BINDING_POWER:
                used_comparison = True


        return left


    def _prefix(self) -> Expression:
        token = self._peek()


        if token.type is TokenType.NOT:
            self._advance()
            operand = self._expression(_NOT_BINDING_POWER)
            return UnaryOp("NOT", operand)


        operator = _UNARY_ARITHMETIC_OPERATORS.get(token.type)
        if operator is not None:
            self._advance()
            operand = self._expression(_UNARY_ARITHMETIC_BINDING_POWER)
            return UnaryOp(operator, operand)


        return self._atom()


    def _atom(self) -> Expression:
        token = self._peek()


        if token.type in (TokenType.INTEGER, TokenType.REAL, TokenType.STRING):
            self._advance()
            return Literal(token.value)


        if token.type is TokenType.NULL:
            self._advance()
            return Literal(None)


        if token.type is TokenType.IDENTIFIER:
            self._advance()
            return Column(token.lexeme)


        if token.type is TokenType.PARAMETER:
            self._advance()
            index = self.parameter_count
            self.parameter_count += 1
            return Parameter(index)


        if token.type is TokenType.LEFT_PAREN:
            self._advance()
            expression = self._expression()
            self._expect(TokenType.RIGHT_PAREN, "expected ')' to close a parenthesized expression")
            return expression


        raise SQLSyntaxError(f"expected an expression, found {token.lexeme!r} at position {token.position}")


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