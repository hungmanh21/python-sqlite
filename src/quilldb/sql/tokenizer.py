"""A deterministic, single-pass scanner for quilldb's SQL subset.

docs/implementation/week-3-sql.md §12's reasoning for why this is a hand
-written cursor over the string rather than one big regular expression:
unterminated strings/comments and exact error positions stay simple, and
comment/string rules stay local instead of fighting a single monster regex.
"""

from quilldb.errors import SQLSyntaxError
from quilldb.sql.tokens import KEYWORDS, Token, TokenType


def tokenize(sql: str) -> list[Token]:
    """Scan one SQL statement and append exactly one EOF token.

    Rules:
        - keywords are case-insensitive (compare casefolded); identifiers
          retain their original spelling
        - strings use single quotes; a doubled quote inside a string is one
          literal quote: 'Ada''s' -> Ada's
        - integers and decimal/exponent forms are separate token kinds
          (INTEGER vs. REAL)
        - `!=` and `<>` both become TokenType.NE
        - whitespace and both comment forms (`-- line` and `/* block */`)
          are discarded entirely -- they produce no token
        - a leading `+`/`-` is always its own operator token, never folded
          into a numeric literal (the parser's prefix/precedence-climbing
          logic is what turns `-5` into a unary op over a literal)

    Args:
        sql: the full source text of one SQL statement.
    Returns:
        Every token in source order, ending with exactly one
        Token(TokenType.EOF, ...) -- including for empty or
        whitespace/comment-only input.
    Raises:
        SQLSyntaxError: an unrecognized character, an unterminated string or
            block comment, a malformed numeric exponent, or a bare `!` (not
            followed by `=`).
    """
    tokens: list[Token] = []
    n = len(sql)
    i = 0

    two_char_operators = {
        "!=": TokenType.NE,
        "<>": TokenType.NE,
        "<=": TokenType.LE,
        ">=": TokenType.GE,
    }
    one_char_operators = {
        "(": TokenType.LEFT_PAREN,
        ")": TokenType.RIGHT_PAREN,
        ",": TokenType.COMMA,
        ";": TokenType.SEMICOLON,
        "*": TokenType.STAR,
        "+": TokenType.PLUS,
        "-": TokenType.MINUS,
        "/": TokenType.SLASH,
        "%": TokenType.PERCENT,
        "=": TokenType.EQ,
        "<": TokenType.LT,
        ">": TokenType.GT,
    }

    while i < n:
        c = sql[i]

        if c in " \t\r\n":
            i += 1
            continue

        if c == "-" and sql[i : i + 2] == "--":
            newline = sql.find("\n", i)
            i = n if newline == -1 else newline
            continue

        if c == "/" and sql[i : i + 2] == "/*":
            end = sql.find("*/", i + 2)
            if end == -1:
                line, column = _line_and_column(sql, i)
                raise SQLSyntaxError(f"unterminated block comment at line {line}, column {column}")
            i = end + 2
            continue

        if c == "'":
            string_value, end = _scan_string(sql, i)
            tokens.append(Token(TokenType.STRING, sql[i:end], string_value, i))
            i = end
            continue

        if c.isdigit() or (c == "." and i + 1 < n and sql[i + 1].isdigit()):
            number_value, end = _scan_number(sql, i)
            token_type = TokenType.REAL if isinstance(number_value, float) else TokenType.INTEGER
            tokens.append(Token(token_type, sql[i:end], number_value, i))
            i = end
            continue

        if c.isalpha() or c == "_":
            end = i + 1
            while end < n and (sql[end].isalnum() or sql[end] == "_"):
                end += 1
            lexeme = sql[i:end]
            token_type = KEYWORDS.get(lexeme.casefold(), TokenType.IDENTIFIER)
            tokens.append(Token(token_type, lexeme, None, i))
            i = end
            continue

        if c == "?":
            tokens.append(Token(TokenType.PARAMETER, "?", None, i))
            i += 1
            continue

        two = sql[i : i + 2]
        if two in two_char_operators:
            tokens.append(Token(two_char_operators[two], two, None, i))
            i += 2
            continue

        if c in one_char_operators:
            tokens.append(Token(one_char_operators[c], c, None, i))
            i += 1
            continue

        line, column = _line_and_column(sql, i)
        raise SQLSyntaxError(f"unexpected character {c!r} at line {line}, column {column}")

    tokens.append(Token(TokenType.EOF, "", None, n))
    return tokens


def _scan_string(sql: str, start: int) -> tuple[str, int]:
    """Scan a single-quoted string literal starting at sql[start] == "'".

    Args:
        sql: the full source text.
        start: index of the opening quote.
    Returns:
        (value, end) -- the literal's decoded contents (quotes un-doubled,
        the surrounding quotes stripped) and the index just past the
        closing quote.
    Raises:
        SQLSyntaxError: the string is never closed before the end of `sql`.
    """
    n = len(sql)
    i = start + 1
    chars: list[str] = []
    while True:
        if i >= n:
            line, column = _line_and_column(sql, start)
            raise SQLSyntaxError(f"unterminated string literal starting at line {line}, column {column}")
        if sql[i] == "'":
            if i + 1 < n and sql[i + 1] == "'":
                chars.append("'")
                i += 2
                continue
            return "".join(chars), i + 1
        chars.append(sql[i])
        i += 1


def _scan_number(sql: str, start: int) -> tuple[int | float, int]:
    """Scan a numeric literal starting at sql[start], a digit or `.`.

    Args:
        sql: the full source text.
        start: index of the first character of the literal (never a sign --
            tokenize() always emits +/- as separate operator tokens).
    Returns:
        (value, end) -- an int for a plain integer literal, a float for
        anything with a decimal point and/or exponent, and the index just
        past the last character consumed.
    Raises:
        SQLSyntaxError: an exponent marker (`e`/`E`) is not followed by an
            optionally-signed digit sequence.
    """
    n = len(sql)
    i = start
    is_real = False

    while i < n and sql[i].isdigit():
        i += 1

    if i < n and sql[i] == ".":
        is_real = True
        i += 1
        while i < n and sql[i].isdigit():
            i += 1

    if i < n and sql[i] in ("e", "E"):
        j = i + 1
        if j < n and sql[j] in ("+", "-"):
            j += 1
        if j < n and sql[j].isdigit():
            while j < n and sql[j].isdigit():
                j += 1
            is_real = True
            i = j
        else:
            line, column = _line_and_column(sql, i)
            raise SQLSyntaxError(f"malformed exponent at line {line}, column {column}")

    lexeme = sql[start:i]
    value: int | float = float(lexeme) if is_real else int(lexeme)
    return value, i


def _line_and_column(sql: str, position: int) -> tuple[int, int]:
    """Convert a character offset into a 1-based (line, column) pair, for
    error messages only -- Token.position always stores the raw offset.

    Args:
        sql: the full source text.
        position: a character offset into `sql`.
    Returns:
        (line, column), both 1-based.
    """
    line = sql.count("\n", 0, position) + 1
    last_newline = sql.rfind("\n", 0, position)
    column = position - last_newline
    return line, column
