"""rw lexer: produces a token stream with INDENT/DEDENT/NEWLINE tokens.

The lexer is line-oriented. For each non-blank, non-comment-only line:
  1. Compute its indentation depth and emit INDENT/DEDENT tokens relative
     to the previous indent stack.
  2. Tokenize the line content.
  3. Emit a NEWLINE token at the end.

Blank lines and pure-comment lines are skipped for indentation purposes.
At end-of-file, any remaining indents are closed with DEDENT tokens, and
a final NEWLINE + EOF are emitted.

Errors are raised as LexerError carrying file/line/col for diagnostics.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class TokenKind(Enum):
    # Structural
    NEWLINE = auto()
    INDENT = auto()
    DEDENT = auto()
    EOF = auto()

    # Literals
    INT = auto()
    FLOAT = auto()
    STRING = auto()

    # Identifier + keyword
    IDENT = auto()
    KW_DEF = auto()
    KW_RETURN = auto()
    KW_IF = auto()
    KW_ELIF = auto()
    KW_ELSE = auto()
    KW_WHILE = auto()
    KW_BREAK = auto()
    KW_CONTINUE = auto()
    KW_ASSERT = auto()
    KW_AND = auto()
    KW_OR = auto()
    KW_NOT = auto()
    KW_TRUE = auto()
    KW_FALSE = auto()
    KW_VOID = auto()
    KW_SPAWN = auto()
    KW_AWAIT = auto()
    KW_FUTURE = auto()
    # Type names (treated as keywords for clarity)
    KW_INT = auto()
    KW_FLOAT = auto()
    KW_BOOL = auto()
    KW_STRING = auto()
    KW_BYTES = auto()
    KW_LIST = auto()
    KW_OPTION = auto()
    KW_MATCH = auto()
    KW_CASE = auto()
    KW_SOME = auto()
    KW_NONE = auto()
    KW_RESULT = auto()
    KW_OK = auto()
    KW_ERR = auto()
    # Reserved for the future (cause errors in Sema, but tokenizable)
    KW_EXTERN = auto()
    KW_CLASS = auto()
    KW_IMPORT = auto()
    KW_FOR = auto()
    KW_IN = auto()
    KW_AS = auto()
    KW_TYPE = auto()

    # Punctuation / operators
    LPAREN = auto()
    RPAREN = auto()
    LBRACK = auto()
    RBRACK = auto()
    COMMA = auto()
    COLON = auto()
    DOT = auto()  # . (qualified call: module.func)
    ARROW = auto()  # ->
    ASSIGN = auto()  # =
    PLUS = auto()
    MINUS = auto()
    STAR = auto()
    SLASH = auto()
    PERCENT = auto()
    EQ = auto()  # ==
    NE = auto()  # !=
    LT = auto()
    LE = auto()
    GT = auto()
    GE = auto()
    AMP = auto()  # &
    PIPE = auto()  # |
    CARET = auto()  # ^
    TILDE = auto()  # ~
    LSHIFT = auto()  # <<
    RSHIFT = auto()  # >>


KEYWORDS: dict[str, TokenKind] = {
    "def": TokenKind.KW_DEF,
    "return": TokenKind.KW_RETURN,
    "if": TokenKind.KW_IF,
    "elif": TokenKind.KW_ELIF,
    "else": TokenKind.KW_ELSE,
    "while": TokenKind.KW_WHILE,
    "break": TokenKind.KW_BREAK,
    "continue": TokenKind.KW_CONTINUE,
    "assert": TokenKind.KW_ASSERT,
    "and": TokenKind.KW_AND,
    "or": TokenKind.KW_OR,
    "not": TokenKind.KW_NOT,
    "true": TokenKind.KW_TRUE,
    "false": TokenKind.KW_FALSE,
    "void": TokenKind.KW_VOID,
    "spawn": TokenKind.KW_SPAWN,
    "await": TokenKind.KW_AWAIT,
    "Future": TokenKind.KW_FUTURE,
    "int": TokenKind.KW_INT,
    "float": TokenKind.KW_FLOAT,
    "bool": TokenKind.KW_BOOL,
    "string": TokenKind.KW_STRING,
    "Bytes": TokenKind.KW_BYTES,
    "List": TokenKind.KW_LIST,
    "Option": TokenKind.KW_OPTION,
    "match": TokenKind.KW_MATCH,
    "case": TokenKind.KW_CASE,
    "Some": TokenKind.KW_SOME,
    "None": TokenKind.KW_NONE,
    "Result": TokenKind.KW_RESULT,
    "Ok": TokenKind.KW_OK,
    "Err": TokenKind.KW_ERR,
    "extern": TokenKind.KW_EXTERN,
    "class": TokenKind.KW_CLASS,
    "import": TokenKind.KW_IMPORT,
    "for": TokenKind.KW_FOR,
    "in": TokenKind.KW_IN,
    "as": TokenKind.KW_AS,
    "type": TokenKind.KW_TYPE,
}


@dataclass
class Token:
    kind: TokenKind
    value: str
    line: int  # 1-origin
    col: int  # 1-origin

    def __repr__(self) -> str:
        return f"Token({self.kind.name}, {self.value!r}, {self.line}:{self.col})"


class LexerError(Exception):
    def __init__(self, message: str, line: int, col: int, length: int = 1) -> None:
        super().__init__(message)
        self.message = message
        self.line = line
        self.col = col
        self.length = length


class Lexer:
    def __init__(self, source: str, filename: str = "<input>") -> None:
        # Normalize line endings; keep trailing newline to simplify loop.
        if not source.endswith("\n"):
            source = source + "\n"
        self.source = source
        self.filename = filename
        self.tokens: list[Token] = []
        self.indents: list[int] = [0]  # column stack
        self.line = 1
        self.col = 1
        self.i = 0  # absolute char index

    # --- public API ---
    def lex(self) -> list[Token]:
        while self.i < len(self.source):
            self._process_line()
        # Close remaining indents.
        while len(self.indents) > 1:
            self.indents.pop()
            self._emit_struct(TokenKind.DEDENT, "")
        self._emit_struct(TokenKind.EOF, "")
        return self.tokens

    # --- helpers ---
    def _emit_struct(self, kind: TokenKind, value: str) -> None:
        self.tokens.append(Token(kind, value, self.line, self.col))

    def _peek(self, off: int = 0) -> str:
        j = self.i + off
        return self.source[j] if j < len(self.source) else ""

    def _advance(self) -> str:
        ch = self.source[self.i]
        self.i += 1
        if ch == "\n":
            self.line += 1
            self.col = 1
        else:
            self.col += 1
        return ch

    # --- line processing ---
    def _process_line(self) -> None:
        # Read leading whitespace, classify the line.
        line_start_i = self.i
        indent_chars: list[str] = []
        while self.i < len(self.source) and self.source[self.i] in (" ", "\t"):
            indent_chars.append(self.source[self.i])
            self.i += 1
            self.col += 1

        # Blank or comment-only line: skip indentation handling, just consume to newline.
        if self.i < len(self.source) and self.source[self.i] in ("\n", "#"):
            # consume to end of line including newline
            while self.i < len(self.source) and self.source[self.i] != "\n":
                self._advance()
            if self.i < len(self.source):
                self._advance()  # newline
            return

        # If EOF reached while scanning indent, just return.
        if self.i >= len(self.source):
            return

        # Indent depth = column position of first non-space char (1-origin minus 1).
        indent_str = "".join(indent_chars)
        # Reject mixed tabs and spaces in the same indent prefix.
        if " " in indent_str and "\t" in indent_str:
            raise LexerError(
                "inconsistent indentation: do not mix tabs and spaces",
                self.line,
                1,
                len(indent_str),
            )
        depth = len(indent_str)

        prev = self.indents[-1]
        if depth > prev:
            self.indents.append(depth)
            self._emit_struct(TokenKind.INDENT, "")
        else:
            while depth < self.indents[-1]:
                self.indents.pop()
                self._emit_struct(TokenKind.DEDENT, "")
            if depth != self.indents[-1]:
                raise LexerError(
                    "inconsistent indentation",
                    self.line,
                    1,
                    depth,
                )

        # Tokenize the rest of the line.
        self._tokenize_line_body()

        # Consume newline if any and emit NEWLINE.
        if self.i < len(self.source) and self.source[self.i] == "\n":
            nl_line, nl_col = self.line, self.col
            self._advance()
            self.tokens.append(Token(TokenKind.NEWLINE, "\\n", nl_line, nl_col))
        else:
            # EOF without newline (shouldn't happen because we appended one)
            self._emit_struct(TokenKind.NEWLINE, "")

        # Avoid lint about unused.
        _ = line_start_i

    def _tokenize_line_body(self) -> None:
        while self.i < len(self.source):
            ch = self.source[self.i]
            if ch == "\n":
                return
            if ch == "#":
                # comment until end of line
                while self.i < len(self.source) and self.source[self.i] != "\n":
                    self._advance()
                return
            if ch in (" ", "\t"):
                self._advance()
                continue
            start_line, start_col = self.line, self.col
            if ch.isalpha() or ch == "_":
                self._read_ident(start_line, start_col)
            elif ch.isdigit():
                self._read_number(start_line, start_col)
            elif ch == '"':
                self._read_string(start_line, start_col)
            else:
                self._read_operator(start_line, start_col)

    def _read_ident(self, line: int, col: int) -> None:
        start = self.i
        while self.i < len(self.source):
            c = self.source[self.i]
            if c.isalnum() or c == "_":
                self._advance()
            else:
                break
        text = self.source[start : self.i]
        kind = KEYWORDS.get(text, TokenKind.IDENT)
        self.tokens.append(Token(kind, text, line, col))

    def _read_number(self, line: int, col: int) -> None:
        start = self.i

        # --- Prefixed integers: 0x.. / 0o.. / 0b.. ---
        if self.source[self.i] == "0" and self.i + 1 < len(self.source):
            prefix = self.source[self.i + 1]
            base_digits: str | None = None
            if prefix in ("x", "X"):
                base_digits = "0123456789abcdefABCDEF"
            elif prefix in ("o", "O"):
                base_digits = "01234567"
            elif prefix in ("b", "B"):
                base_digits = "01"
            if base_digits is not None:
                self._advance()  # '0'
                self._advance()  # prefix char
                # Underscore right after the prefix is not allowed.
                if self.i < len(self.source) and self.source[self.i] == "_":
                    raise LexerError(
                        "underscore not allowed right after numeric prefix",
                        self.line,
                        self.col,
                        1,
                    )
                self._read_digit_run(base_digits)
                # At least one base digit is required.
                if self.i - start <= 2:
                    raise LexerError(
                        f"missing digits after '0{prefix}'",
                        line,
                        col,
                        self.i - start,
                    )
                text = self.source[start : self.i]
                self.tokens.append(Token(TokenKind.INT, text, line, col))
                return

        # --- Decimal integer part (with underscores) ---
        self._read_digit_run("0123456789")

        is_float = False
        # Fractional part: '.' followed by a digit.
        if (
            self.i < len(self.source)
            and self.source[self.i] == "."
            and self.i + 1 < len(self.source)
            and self.source[self.i + 1].isdigit()
        ):
            is_float = True
            self._advance()  # consume '.'
            self._read_digit_run("0123456789")

        # --- Exponent part: e/E [ +/- ] digits ---
        if self.i < len(self.source) and self.source[self.i] in ("e", "E"):
            j = self.i + 1
            if j < len(self.source) and self.source[j] in ("+", "-"):
                j += 1
            if j < len(self.source) and self.source[j].isdigit():
                is_float = True
                self._advance()  # 'e'/'E'
                if self.source[self.i] in ("+", "-"):
                    self._advance()  # sign
                self._read_digit_run("0123456789")

        text = self.source[start : self.i]
        self.tokens.append(
            Token(TokenKind.FLOAT if is_float else TokenKind.INT, text, line, col)
        )

    def _read_digit_run(self, digits: str) -> None:
        """Consume a run of `digits` characters allowing single underscores
        between digits. Rejects leading/trailing/consecutive underscores."""
        while self.i < len(self.source):
            c = self.source[self.i]
            if c in digits:
                self._advance()
            elif c == "_":
                # Underscore must be surrounded by valid digits.
                prev = self.source[self.i - 1] if self.i > 0 else ""
                nxt = self.source[self.i + 1] if self.i + 1 < len(self.source) else ""
                if prev not in digits or nxt not in digits:
                    raise LexerError(
                        "invalid underscore placement in numeric literal",
                        self.line,
                        self.col,
                        1,
                    )
                self._advance()
            else:
                break

    def _read_string(self, line: int, col: int) -> None:
        # Consume opening quote
        self._advance()
        chars: list[str] = []
        while True:
            if self.i >= len(self.source) or self.source[self.i] == "\n":
                raise LexerError("unterminated string literal", line, col, 1)
            c = self.source[self.i]
            if c == '"':
                self._advance()
                break
            if c == "\\":
                self._advance()
                if self.i >= len(self.source):
                    raise LexerError("unterminated string literal", line, col, 1)
                esc = self.source[self.i]
                self._advance()
                mapping = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "0": "\0"}
                if esc in mapping:
                    chars.append(mapping[esc])
                else:
                    raise LexerError(
                        f"unknown escape sequence: \\{esc}", self.line, self.col - 1, 2
                    )
            else:
                chars.append(c)
                self._advance()
        self.tokens.append(Token(TokenKind.STRING, "".join(chars), line, col))

    def _read_operator(self, line: int, col: int) -> None:
        ch = self.source[self.i]
        nxt = self._peek(1)
        two = ch + nxt
        two_char = {
            "->": TokenKind.ARROW,
            "==": TokenKind.EQ,
            "!=": TokenKind.NE,
            "<=": TokenKind.LE,
            ">=": TokenKind.GE,
            "<<": TokenKind.LSHIFT,
            ">>": TokenKind.RSHIFT,
        }
        one_char = {
            "(": TokenKind.LPAREN,
            ")": TokenKind.RPAREN,
            "[": TokenKind.LBRACK,
            "]": TokenKind.RBRACK,
            ",": TokenKind.COMMA,
            ":": TokenKind.COLON,
            ".": TokenKind.DOT,
            "=": TokenKind.ASSIGN,
            "+": TokenKind.PLUS,
            "-": TokenKind.MINUS,
            "*": TokenKind.STAR,
            "/": TokenKind.SLASH,
            "%": TokenKind.PERCENT,
            "<": TokenKind.LT,
            ">": TokenKind.GT,
            "&": TokenKind.AMP,
            "|": TokenKind.PIPE,
            "^": TokenKind.CARET,
            "~": TokenKind.TILDE,
        }
        if two in two_char:
            self._advance()
            self._advance()
            self.tokens.append(Token(two_char[two], two, line, col))
            return
        if ch in one_char:
            self._advance()
            self.tokens.append(Token(one_char[ch], ch, line, col))
            return
        raise LexerError(f"unexpected character: {ch!r}", line, col, 1)


def tokenize(source: str, filename: str = "<input>") -> list[Token]:
    """Convenience entry point used by tests and downstream stages."""
    return Lexer(source, filename).lex()
