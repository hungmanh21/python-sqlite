# 07 — From SQL Text to a Tree


> **Read at the start of week 3, before writing the tokenizer.**
>
> **Time:** ~40 minutes. **Prerequisite:** none beyond Python, though [chapter 03](../codec/03-encoding-varints-and-records.md) is a useful reminder that representation choices determine what later layers can do cheaply.


---


## 7.0 The problem: a string has no structure


The user gives you this:


```sql
SELECT name FROM users WHERE age > 30 AND active = 1;
```


Python gives you a `str`: a flat sequence of characters. But execution needs answers to structural
questions:


- Is `name` a column, a string literal, or a keyword?
- Does `age > 30 AND active = 1` mean `(age > 30) AND (active = 1)`?
- In `a + b * c`, which operation happens first?
- Is the `?` in a query an operator, punctuation, or a parameter slot?
- Where should an error point when a closing parenthesis is missing?


You cannot execute the characters directly. You need to turn a flat representation into a nested
one whose shape carries the meaning.


The pipeline is:


```text
characters        tokens                    syntax tree
"a + b * c"  →  [a] [+] [b] [*] [c]  →       (+)
                                              /   \
                                            (a)   (*)
                                                  / \
                                                (b) (c)
```


That final tree says `b * c` is one unit and `a` is added to its result. The information was
implicit in the grammar; parsing makes it explicit in data.


> **Say this out loud:** "The SQL frontend is a representation pipeline. Tokenization turns
> characters into meaningful atoms; parsing turns a flat token stream into a tree whose shape
> records precedence and nesting. Execution should consume that tree, never rediscover structure
> by inspecting the original string."


---


## 7.1 Wrong answer 1: split on whitespace


```python
sql.split()
```


It appears to work:


```text
SELECT name FROM users
→ ["SELECT", "name", "FROM", "users"]
```


Then ordinary SQL arrives:


```sql
SELECT name,age FROM users WHERE age>=30;
```


Whitespace splitting produces:


```text
["SELECT", "name,age", "FROM", "users", "WHERE", "age>=30;"]
```


Comma, comparison operator, and semicolon are glued to neighboring words. You could add replacements
before splitting, until a string contains punctuation:


```sql
SELECT 'Ada, age >= 30' FROM users;
```


Now punctuation inside a literal must *not* split. Comments have the same property. SQL's meaning is
stateful: whether `,` is punctuation depends on whether the scanner is currently inside a string.


Whitespace splitting fails because tokens are not separated by whitespace. They are separated by
the lexical rules of the language.


---


## 7.2 Wrong answer 2: one giant regular expression


A regex can recognize identifiers, numbers, and operators. For a tiny language, a carefully built
ordered set of regexes is a legitimate scanner.


The trap is making the *whole language* one expression. The pattern acquires branches for quoted
strings, doubled quotes, line comments, block comments, decimal exponents, and two-character
operators. Then error behavior becomes the hardest part: a failed match tells you that something
didn't match, not whether the user had an unterminated string, a malformed exponent, or a bare `!`.


A single cursor over the input is clearer:


```python
while current < len(sql):
    char = sql[current]
    if char.isspace(): ...
    elif char == "'": scan_string()
    elif char.isdigit() or starts_dot_number(): scan_number()
    elif starts_identifier(char): scan_identifier_or_keyword()
    else: scan_operator_or_punctuation()
```


Each branch owns one rule and one failure mode. An unterminated string can say exactly that and point
to the opening quote.


This is not an argument that regex is bad. It is the same engineering rule used throughout this
project: **when failure states matter, make them explicit in control flow.** Chapter 03 did this for
truncated varints instead of relying on an accidental `IndexError`; the tokenizer should do the same.


---


## 7.3 Tokenization: deciding where one thing ends


A token carries at least four facts:


```python
Token(
    type=TokenType.INTEGER,
    lexeme="030",
    value=30,
    position=37,
)
```


- **type** tells the parser which grammar rule applies.
- **lexeme** preserves what the user wrote, useful in diagnostics.
- **value** is the interpreted literal, so the parser does not re-parse `030`.
- **position** points back into the source.


### Maximal munch


When several tokens could begin at one character, take the longest valid one. This is the
**maximal-munch** rule:


```text
>=  is GE, not GT followed by EQ
!=  is NE, not an error on ! followed by EQ
123.45e-6 is one REAL, not INTEGER DOT INTEGER IDENTIFIER MINUS INTEGER
```


Maximal munch is why two-character operators must be checked before their one-character prefixes.
It is also why number scanning is one routine with states (integer, fraction, exponent), rather than
three unrelated token rules.


### Keywords are identifiers with a second lookup


`select`, `users`, and `age` all have the same character shape. Scan all three as identifier-shaped
lexemes, then case-fold and check a keyword table:


```python
token_type = KEYWORDS.get(lexeme.casefold(), TokenType.IDENTIFIER)
```


This does two useful things:


1. Keyword matching is case-insensitive, as SQL users expect.
2. The original spelling remains available for table names and error messages.


Do not lowercase the entire SQL string before scanning. That would corrupt string literal contents:
`'Ada'` and `'ada'` are different values.


### Strings need a language rule, not Python's rule


SQL string literals use single quotes. A quote inside the string is represented by two quotes:


```sql
'Ada''s notebook'  →  Ada's notebook
```


Backslash escaping is not part of the portable SQLite string-literal rule. If you silently accept
Python-like `\'`, you have created a language that looks like SQL but disagrees at exactly the
security-sensitive boundary where quoting matters.


### Why source positions survive every phase


Suppose the query says:


```sql
SELECT name FROM users WHERE age > ;
                                   ^
```


The tokenizer knows where every token began. The parser can therefore report "expected expression
at line 1, column 36" rather than "syntax error." Later, the binder can attach the original column
token to an unknown-name error.


Positions are cheap when captured early and expensive to reconstruct later. This is the same
front-load-the-navigation-information principle as record headers (chapter 03) and child pointers
(chapter 06).


> **Say this out loud:** "A scanner is mostly boundary decisions. I use maximal munch for operators
> and numbers, scan identifier-shaped text once and classify keywords afterward, preserve original
> lexemes, and attach source positions immediately because useful errors are impossible to bolt on
> after those positions have been discarded."


---


## 7.4 Why the parser produces an AST rather than callbacks


You could make the parser call execution methods as it recognizes syntax:


```python
if see_select():
    execute_select(...)
```


That works for a calculator. It breaks for a database because parsing is only the first question.
Before execution, you still need to:


- resolve table and column names
- count and substitute parameters
- reject type errors before touching data
- choose a scan or an index
- print `EXPLAIN`
- potentially cache a prepared plan


An **abstract syntax tree (AST)** is the stable handoff between those phases:


```python
Select(
    expressions=(Column("name"),),
    table="users",
    where=BinaryOp(Column("age"), ">", Literal(30)),
)
```


"Abstract" means punctuation that mattered to parsing no longer appears. Parentheses determine the
tree's shape, so the AST does not need a `Parenthesized` node. Commas separated list elements, so
they become tuple boundaries. The keyword `FROM` established a relationship, so it becomes the
`table` field.


The AST retains meaning and discards spelling machinery.


### Why immutable dataclasses are the right default


Immutable nodes buy three things:


- tests compare entire trees directly
- later phases cannot accidentally rewrite the parser's result in place
- a future prepared-statement cache can safely share parsed trees


The binder should produce a *new* bound tree rather than replacing `Column("age")` with slot 2
inside the AST. Syntax and catalog-dependent meaning have different lifetimes: the same parsed SQL
may be rebound after a schema change.


---


## 7.5 Statement grammar: recursive descent where the shape is obvious


At the statement level, one token usually determines the rule:


```text
CREATE → create_table
INSERT → insert
SELECT → select
```


This is ideal for **recursive-descent parsing**: one function per grammar rule, reading left to
right and calling another function for nested constructs.


```python
def parse_statement(self):
    if self.match(CREATE): return self.create_table()
    if self.match(INSERT): return self.insert()
    if self.match(SELECT): return self.select()
    raise self.error("expected CREATE, INSERT, or SELECT")
```


The control flow resembles the grammar, which is why handwritten recursive descent is so readable.
The cost is that you own every production and error message. For quilldb's narrow subset that is a
good trade; for SQLite's full grammar it would not be.


### Reject the rest deliberately


An unsupported feature should fail where it becomes unambiguous:


```sql
SELECT * FROM a JOIN b ON a.id = b.id;
                ^^^^
```


Do not parse the prefix as a complete query and ignore the trailing tokens. "Parse exactly one
statement, then require EOF" is a correctness and safety property. Silent partial parsing turns a
query the engine does not understand into a different query it does execute.


---


## 7.6 Expressions: why ordinary recursive descent becomes repetitive


Expressions have precedence:


```text
OR
AND
NOT
= != < <= > >= IS LIKE
+ -
* / %
prefix + -
```


The traditional recursive-descent solution has one function per level:


```text
parse_or → parse_and → parse_comparison → parse_term → parse_factor → parse_unary
```


It works. Crafting Interpreters teaches it well. But SQL has many levels, and every new operator
requires editing the function ladder. The grammar's real fact is just a number: how tightly does
this operator bind?


Pratt parsing makes that number explicit.


### Binding power


Give multiplication a higher binding power than addition:


```text
+ : 40
* : 50
```


Parse `a + b * c`:


1. Parse `a` as the left expression.
2. See `+` at power 40. It is strong enough to continue.
3. Parse the right side while requiring power greater than 40.
4. See `b`, then `*` at 50. It qualifies, so form `b * c`.
5. Return that whole subtree as `+`'s right side.


Result:


```text
      +
     / \
    a   *
       / \
      b   c
```


No special case said "multiplication before addition." The two numbers caused the shape.


### Associativity from asymmetric powers


Subtraction is left-associative:


```text
10 - 3 - 2  means  (10 - 3) - 2
```


For an operator at power `p`, parse its right side at `p + 1`. Another operator at the same power
cannot enter the right subtree, so it is handled by the outer loop and associates left.


This is the part of Pratt parsing worth understanding rather than memorizing: **precedence chooses
which operators nest; asymmetric left/right powers choose which direction equal operators nest.**


### Prefix operators


`NOT`, unary `-`, and unary `+` begin an expression rather than sitting between two expressions.
The parser handles them in its prefix step, then parses their operand at a high binding power:


```text
-a * b  →  (-a) * b
NOT a = 1 → NOT (a = 1)   # with SQL's chosen precedence table
```


Write precedence tests against whole ASTs. Testing only final numeric results can let two parser
bugs cancel each other inside the evaluator.


> **Say this out loud:** "I used recursive descent for statements because the leading keyword picks
> the production, and Pratt parsing for expressions because SQL precedence is naturally a binding-
> power table. The AST test for `a + b * c` proves the parser's shape independently of evaluation."


---


## 7.7 The road not taken: generated parser, parser combinators, or bytecode now


### A generated parser — SQLite's choice


SQLite's grammar lives in `parse.y` and is processed by **Lemon**, an LALR(1) parser generator.
That is the right choice for hundreds of productions accumulated over decades. The grammar is a
declarative artifact; conflict reports expose ambiguity; syntax expansion does not require hand-
maintaining a call stack of parser functions.


Why not for quilldb: it adds a build tool, generated source, and a second formalism before you have
enough grammar for those costs to pay back. The handwritten parser is a learning goal and remains
small because the supported SQL subset is explicit.


### Parser combinators


Combinators can make a grammar elegant and compositional. In Python they either require a runtime
dependency (prohibited here) or a combinator framework you would have to write and debug. They can
also make precise, context-aware error selection surprisingly difficult when many alternatives have
partially consumed input.


### Compile directly to execution instructions


SQLite's parser actions feed code generation that ultimately produces VDBE bytecode. Doing that now
would fuse syntax recognition, name resolution, and execution. You would lose the AST boundary that
makes binder tests, cost-based candidate generation and estimation, `EXPLAIN`, and plan caching
straightforward.


The road not taken is not "worse." SQLite's generated parser and bytecode VM are appropriate to its
language size, compatibility requirements, and compact C runtime. Your constraints favor a small
handwritten parser and explicit intermediate trees.


---


## 7.8 What you're building


`docs/implementation/week-3-*.md` sessions 1–3:


| File           | Responsibility                  | Boundary it protects                       |
| -------------- | ------------------------------- | ------------------------------------------ |
| `tokens.py`    | token vocabulary and positions  | parser never inspects raw characters       |
| `tokenizer.py` | characters → tokens             | strings/comments/operators handled once    |
| `ast.py`       | immutable syntax representation | later phases do not depend on parser state |
| `parser.py`    | tokens → one statement AST      | execution never interprets SQL text        |


The essential tests are independent:


- tokenizer tests assert tokens without invoking the parser
- parser tests construct tokens only through public `parse`, but assert AST shape without binding
- malformed syntax tests prove trailing input is rejected and positions survive


That separation is the frontend equivalent of chapter 06's advice to test search against a hand-
built tree before insert exists. When a parser test fails, storage is not a suspect.


---


## 7.9 Check yourself


1. Why does whitespace splitting fail even on ordinary SQL without quoted identifiers?
2. What does maximal munch decide for `>=`, and why must two-character operators be checked first?
3. Why scan keywords as identifier-shaped text before classifying them?
4. Why must lowercasing apply to identifier comparison rather than the whole SQL string?
5. What four facts does a useful token carry?
6. What does "abstract" mean in abstract syntax tree? Name two pieces of punctuation the AST drops.
7. Why should binding produce a new tree instead of mutating the AST?
8. Why is recursive descent a natural fit for statements beginning with `CREATE`, `INSERT`, or `SELECT`?
9. What safety bug appears if the parser accepts a valid prefix and ignores trailing tokens?
10. Parse `a + b * c` using binding powers. Where does `b * c` become a subtree?
11. How does `p + 1` on the right side make subtraction left-associative?
12. Why is Lemon sensible for SQLite and unnecessary for quilldb?


If 10 of 12 come out cleanly, you are ready to implement the frontend.


---


## 7.10 Sources


- [Crafting Interpreters, chapters 4–6](https://craftinginterpreters.com/) — scanning, AST representation, recursive-descent expression parsing, and the principle that each phase should produce an explicit representation for the next.
- [Simple but Powerful Pratt Parsing](https://matklad.github.io/2020/04/13/simple-but-powerful-pratt-parsing.html) — binding power, asymmetric powers for associativity, and the compact Pratt loop used by this project.
- [SQLite SQL Language](https://www.sqlite.org/lang.html) — the syntax diagrams used as the grammar reference; quilldb intentionally implements a named subset.
- [SQLite Architecture](https://www.sqlite.org/arch.html) — tokenizer, Lemon-generated parser, code generator, and VDBE boundaries.
- [`parse.y`](https://github.com/sqlite/sqlite/blob/master/src/parse.y) and [`tokenize.c`](https://github.com/sqlite/sqlite/blob/master/src/tokenize.c) — SQLite's concrete generated-parser and handwritten-tokenizer split.
- Vaughan Pratt, **"Top Down Operator Precedence"** (1973) — the original Pratt parsing paper.


---


**Next:** [08 — The catalog and binding](../catalog/08-the-catalog-and-binding.md) — how a database remembers its own schema, why SQLite stores DDL text, and why names must disappear before execution.