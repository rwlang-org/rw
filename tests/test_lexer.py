from __future__ import annotations

import pytest

from rwc.lexer import LexerError, TokenKind, tokenize


def kinds(src: str) -> list[TokenKind]:
    return [t.kind for t in tokenize(src)]


def test_empty_source_emits_only_eof():
    toks = tokenize("")
    assert [t.kind for t in toks] == [TokenKind.EOF]


def test_simple_identifier_and_newline():
    toks = tokenize("foo\n")
    assert [t.kind for t in toks] == [
        TokenKind.IDENT,
        TokenKind.NEWLINE,
        TokenKind.EOF,
    ]
    assert toks[0].value == "foo"


def test_keywords_are_recognized():
    src = "def return if elif else while and or not true false void spawn await Future int float bool string\n"
    toks = tokenize(src)
    expected = [
        TokenKind.KW_DEF,
        TokenKind.KW_RETURN,
        TokenKind.KW_IF,
        TokenKind.KW_ELIF,
        TokenKind.KW_ELSE,
        TokenKind.KW_WHILE,
        TokenKind.KW_AND,
        TokenKind.KW_OR,
        TokenKind.KW_NOT,
        TokenKind.KW_TRUE,
        TokenKind.KW_FALSE,
        TokenKind.KW_VOID,
        TokenKind.KW_SPAWN,
        TokenKind.KW_AWAIT,
        TokenKind.KW_FUTURE,
        TokenKind.KW_INT,
        TokenKind.KW_FLOAT,
        TokenKind.KW_BOOL,
        TokenKind.KW_STRING,
        TokenKind.NEWLINE,
        TokenKind.EOF,
    ]
    assert [t.kind for t in toks] == expected


def test_numbers_int_and_float():
    toks = tokenize("42 3.14\n")
    assert toks[0].kind == TokenKind.INT and toks[0].value == "42"
    assert toks[1].kind == TokenKind.FLOAT and toks[1].value == "3.14"


def test_prefixed_integers():
    toks = tokenize("0x1F 0o17 0b1010\n")
    assert toks[0].kind == TokenKind.INT and toks[0].value == "0x1F"
    assert toks[1].kind == TokenKind.INT and toks[1].value == "0o17"
    assert toks[2].kind == TokenKind.INT and toks[2].value == "0b1010"


def test_exponent_floats():
    toks = tokenize("1e10 1.5e-3 2E+5\n")
    assert toks[0].kind == TokenKind.FLOAT and toks[0].value == "1e10"
    assert toks[1].kind == TokenKind.FLOAT and toks[1].value == "1.5e-3"
    assert toks[2].kind == TokenKind.FLOAT and toks[2].value == "2E+5"


def test_underscore_separators():
    toks = tokenize("1_000_000 0xFF_FF\n")
    assert toks[0].kind == TokenKind.INT and toks[0].value == "1_000_000"
    assert toks[1].kind == TokenKind.INT and toks[1].value == "0xFF_FF"


def test_prefix_without_digits_raises():
    with pytest.raises(LexerError):
        tokenize("0x\n")


def test_consecutive_underscore_raises():
    with pytest.raises(LexerError):
        tokenize("1__0\n")


def test_trailing_underscore_raises():
    with pytest.raises(LexerError):
        tokenize("1_\n")


def test_string_literal_basic():
    toks = tokenize('"hello"\n')
    assert toks[0].kind == TokenKind.STRING
    assert toks[0].value == "hello"


def test_string_escape_sequences():
    toks = tokenize(r'"a\nb\t\"\\"' + "\n")
    assert toks[0].kind == TokenKind.STRING
    assert toks[0].value == "a\nb\t\"\\"


def test_unterminated_string_raises():
    with pytest.raises(LexerError):
        tokenize('"oops\n')


def test_operators_two_char_then_one_char():
    toks = tokenize("== != <= >= -> + - * / %\n")
    expected = [
        TokenKind.EQ,
        TokenKind.NE,
        TokenKind.LE,
        TokenKind.GE,
        TokenKind.ARROW,
        TokenKind.PLUS,
        TokenKind.MINUS,
        TokenKind.STAR,
        TokenKind.SLASH,
        TokenKind.PERCENT,
        TokenKind.NEWLINE,
        TokenKind.EOF,
    ]
    assert [t.kind for t in toks] == expected


def test_indent_dedent_emitted_correctly():
    src = "def f():\n    a\n    b\nc\n"
    ks = kinds(src)
    # def f ( ) : NL INDENT a NL b NL DEDENT c NL EOF
    assert ks == [
        TokenKind.KW_DEF,
        TokenKind.IDENT,
        TokenKind.LPAREN,
        TokenKind.RPAREN,
        TokenKind.COLON,
        TokenKind.NEWLINE,
        TokenKind.INDENT,
        TokenKind.IDENT,
        TokenKind.NEWLINE,
        TokenKind.IDENT,
        TokenKind.NEWLINE,
        TokenKind.DEDENT,
        TokenKind.IDENT,
        TokenKind.NEWLINE,
        TokenKind.EOF,
    ]


def test_nested_indent_dedent():
    src = "a\n    b\n        c\n    d\ne\n"
    ks = kinds(src)
    assert ks == [
        TokenKind.IDENT,
        TokenKind.NEWLINE,
        TokenKind.INDENT,
        TokenKind.IDENT,
        TokenKind.NEWLINE,
        TokenKind.INDENT,
        TokenKind.IDENT,
        TokenKind.NEWLINE,
        TokenKind.DEDENT,
        TokenKind.IDENT,
        TokenKind.NEWLINE,
        TokenKind.DEDENT,
        TokenKind.IDENT,
        TokenKind.NEWLINE,
        TokenKind.EOF,
    ]


def test_blank_and_comment_lines_are_ignored_for_indent():
    src = "a\n\n  # comment\n    b\nc\n"
    ks = kinds(src)
    assert ks == [
        TokenKind.IDENT,
        TokenKind.NEWLINE,
        TokenKind.INDENT,
        TokenKind.IDENT,
        TokenKind.NEWLINE,
        TokenKind.DEDENT,
        TokenKind.IDENT,
        TokenKind.NEWLINE,
        TokenKind.EOF,
    ]


def test_tab_space_mix_in_indent_raises():
    with pytest.raises(LexerError):
        tokenize(" \tfoo\n")


def test_inconsistent_indentation_raises():
    # opens at 4, then dedents to 2 which is not on the stack
    src = "a\n    b\n  c\n"
    with pytest.raises(LexerError):
        tokenize(src)


def test_final_dedents_emitted_at_eof():
    src = "a\n    b\n"
    ks = kinds(src)
    assert ks[-3:] == [TokenKind.DEDENT, TokenKind.NEWLINE, TokenKind.EOF] or ks[-2:] == [
        TokenKind.DEDENT,
        TokenKind.EOF,
    ]


def test_function_signature_tokens():
    src = "def add(a: int, b: int) -> int:\n    return a + b\n"
    toks = tokenize(src)
    expected = [
        TokenKind.KW_DEF,
        TokenKind.IDENT,
        TokenKind.LPAREN,
        TokenKind.IDENT,
        TokenKind.COLON,
        TokenKind.KW_INT,
        TokenKind.COMMA,
        TokenKind.IDENT,
        TokenKind.COLON,
        TokenKind.KW_INT,
        TokenKind.RPAREN,
        TokenKind.ARROW,
        TokenKind.KW_INT,
        TokenKind.COLON,
        TokenKind.NEWLINE,
        TokenKind.INDENT,
        TokenKind.KW_RETURN,
        TokenKind.IDENT,
        TokenKind.PLUS,
        TokenKind.IDENT,
        TokenKind.NEWLINE,
        TokenKind.DEDENT,
        TokenKind.EOF,
    ]
    assert [t.kind for t in toks] == expected


def test_dot_splits_qualified_call():
    toks = tokenize("math_lib.add\n")
    assert [t.kind for t in toks] == [
        TokenKind.IDENT,
        TokenKind.DOT,
        TokenKind.IDENT,
        TokenKind.NEWLINE,
        TokenKind.EOF,
    ]
    assert toks[0].value == "math_lib"
    assert toks[2].value == "add"


def test_dot_does_not_break_float_literals():
    # `.` between digits is a float, not a DOT token.
    toks = tokenize("1.5 3.14e2\n")
    assert toks[0].kind == TokenKind.FLOAT and toks[0].value == "1.5"
    assert toks[1].kind == TokenKind.FLOAT and toks[1].value == "3.14e2"
    assert TokenKind.DOT not in [t.kind for t in toks]
