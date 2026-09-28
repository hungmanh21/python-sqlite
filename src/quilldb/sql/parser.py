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
    Begin,
    BinaryOp,
    Column,
    ColumnDef,
    Commit,
    CreateIndex,
    CreateTable,
    DataType,
    Delete,
    Explain,
    Expression,
    FunctionCall,
    Insert,
    IsNull,
    JoinClause,
    Literal,
    OrderKey,
    Parameter,
    Rollback,
    Select,
    Statement,
    TableRef,
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
        elif token.type is TokenType.BEGIN:
            statement = self._begin()
        elif token.type is TokenType.COMMIT:
            statement = self._commit()
        elif token.type is TokenType.ROLLBACK:
            statement = self._rollback()
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


        distinct = False
        if self._peek().type is TokenType.DISTINCT:
            self._advance()
            distinct = True


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
        table, joins = self._from_clause()


        where: Expression | None = None
        if self._peek().type is TokenType.WHERE:
            self._advance()
            where = self._expression()


        group_by: tuple[Expression, ...] = ()
        if self._peek().type is TokenType.GROUP:
            self._advance()
            self._expect(TokenType.BY, "expected BY after GROUP")
            keys = [self._expression()]
            while self._peek().type is TokenType.COMMA:
                self._advance()
                keys.append(self._expression())
            group_by = tuple(keys)


        having: Expression | None = None
        if self._peek().type is TokenType.HAVING:
            self._advance()
            having = self._expression()


        order_by: tuple[OrderKey, ...] = ()
        if self._peek().type is TokenType.ORDER:
            self._advance()
            self._expect(TokenType.BY, "expected BY after ORDER")
            order_keys = [self._order_key()]
            while self._peek().type is TokenType.COMMA:
                self._advance()
                order_keys.append(self._order_key())
            order_by = tuple(order_keys)


        limit: Expression | None = None
        if self._peek().type is TokenType.LIMIT:
            self._advance()
            limit = self._expression()


        offset: Expression | None = None
        if self._peek().type is TokenType.OFFSET:
            self._advance()
            offset = self._expression()


        return Select(expressions, table, joins, where, group_by, having, distinct, order_by, limit, offset)


    def _order_key(self) -> OrderKey:
        expression = self._expression()
        descending = False
        if self._peek().type is TokenType.ASC:
            self._advance()
        elif self._peek().type is TokenType.DESC:
            self._advance()
            descending = True
        return OrderKey(expression, descending)


    def _from_clause(self) -> tuple[TableRef, tuple[JoinClause, ...]]:
        """The first table, then zero or more comma joins and/or `JOIN ...
        ON` steps, in the order they appear -- that order is `table_ordinal`
        in the binder (sql/binder.py's TableScope), so it isn't just parsed
        and discarded.
        """
        first = self._table_ref()


        joins: list[JoinClause] = []
        while True:
            if self._peek().type is TokenType.COMMA:
                self._advance()
                joins.append(JoinClause("INNER", self._table_ref(), on=None))
                continue


            join_type = self._join_type()
            if join_type is None:
                break
            table = self._table_ref()
            self._expect(TokenType.ON, "expected ON after JOIN")
            on = self._expression()
            joins.append(JoinClause(join_type, table, on))


        return first, tuple(joins)


    def _join_type(self) -> str | None:
        token = self._peek()
        if token.type is TokenType.JOIN:
            self._advance()
            return "INNER"
        if token.type is TokenType.INNER:
            self._advance()
            self._expect(TokenType.JOIN, "expected JOIN after INNER")
            return "INNER"
        if token.type is TokenType.LEFT:
            self._advance()
            if self._peek().type is TokenType.OUTER:
                self._advance()
            self._expect(TokenType.JOIN, "expected JOIN after LEFT [OUTER]")
            return "LEFT"
        return None


    def _table_ref(self) -> TableRef:
        name = self._expect(TokenType.IDENTIFIER, "expected a table name").lexeme
        alias: str | None = None
        if self._peek().type is TokenType.AS:
            self._advance()
            alias = self._expect(TokenType.IDENTIFIER, "expected an alias after AS").lexeme
        elif self._peek().type is TokenType.IDENTIFIER:
            # Bare alias, no AS -- legal in SQL ("FROM orders o"), and
            # unambiguous here only because JOIN/ON/WHERE/comma are their
            # own reserved token types, not IDENTIFIER (session 0, B6-9).
            alias = self._advance().lexeme
        return TableRef(name, alias)


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


    def _begin(self) -> Begin:
        self._expect(TokenType.BEGIN, "expected BEGIN")
        # IMMEDIATE is matched as an identifier, not a keyword: SQLite
        # doesn't reserve it, so a column or table named `immediate` has to
        # keep parsing (NOTES.md B6-9).
        immediate = False
        token = self._peek()
        if token.type is TokenType.IDENTIFIER and token.lexeme.casefold() == "immediate":
            self._advance()
            immediate = True
        return Begin(immediate)


    def _commit(self) -> Commit:
        self._expect(TokenType.COMMIT, "expected COMMIT")
        return Commit()


    def _rollback(self) -> Rollback:
        self._expect(TokenType.ROLLBACK, "expected ROLLBACK")
        return Rollback()


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
            if self._peek().type is TokenType.LEFT_PAREN:
                return self._function_call(token.lexeme)
            if self._peek().type is TokenType.DOT:
                self._advance()
                column_name = self._expect(TokenType.IDENTIFIER, "expected a column name after '.'").lexeme
                return Column(column_name, table=token.lexeme)
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


    def _function_call(self, name: str) -> FunctionCall:
        """`name(` was just consumed up to (not including) the `(` -- called
        from _atom() the moment an identifier is immediately followed by
        `LEFT_PAREN`, which is unambiguous here: no other atom starts that
        way (a parenthesized expression, `_atom`'s own LEFT_PAREN case,
        never has an IDENTIFIER directly in front of it).

        `COUNT(*)` is the one special form: `*` alone is not a general
        expression (see FunctionCall.star's docstring), so it's checked for
        explicitly before falling into ordinary comma-separated argument
        parsing. Whether `*` is actually valid for THIS function name is
        the binder's call, not the parser's -- same "syntax now, meaning
        later" split as an unresolved Column.
        """
        self._expect(TokenType.LEFT_PAREN, "expected '(' after function name")

        if self._peek().type is TokenType.STAR:
            self._advance()
            self._expect(TokenType.RIGHT_PAREN, "expected ')' after '*'")
            return FunctionCall(name, (), star=True)

        args = [self._expression()]
        while self._peek().type is TokenType.COMMA:
            self._advance()
            args.append(self._expression())
        self._expect(TokenType.RIGHT_PAREN, "expected ')' to close a function call")
        return FunctionCall(name, tuple(args))


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