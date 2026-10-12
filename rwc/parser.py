"""Recursive-descent parser for rw.

Grammar (informal):

    module    = { func_def }
    func_def  = 'def' IDENT '(' [params] ')' '->' type ':' NEWLINE INDENT stmt+ DEDENT
    params    = param { ',' param }
    param     = IDENT ':' type
    type      = 'int' | 'float' | 'bool' | 'string' | 'void' | 'Future' '[' type ']'

    stmt      = var_decl | assign | return_stmt | if_stmt | while_stmt | expr_stmt
    var_decl  = IDENT ':' type '=' expr NEWLINE
    assign    = IDENT '=' expr NEWLINE
    return    = 'return' [expr] NEWLINE
    if_stmt   = 'if' expr ':' block ( 'elif' expr ':' block )* [ 'else' ':' block ]
    while     = 'while' expr ':' block
    expr_stmt = expr NEWLINE
    block     = NEWLINE INDENT stmt+ DEDENT

    expr      = ternary
    ternary   = or_expr [ 'if' or_expr 'else' ternary ]   # right-assoc
    or_expr   = and_expr ( 'or' and_expr )*
    and_expr  = not_expr ( 'and' not_expr )*
    not_expr  = 'not' not_expr | cmp_expr
    cmp_expr  = add_expr ( CMPOP add_expr )?
    add_expr  = mul_expr ( ('+'|'-') mul_expr )*
    mul_expr  = unary  ( ('*'|'/'|'%') unary )*
    unary     = '-' unary | 'await' unary | spawn_or_atom
    spawn_or_atom = 'spawn' call | atom_postfix
    atom_postfix  = atom { '(' arglist ')' }    # only IDENT may be followed by '(' (call)
    atom      = INT | FLOAT | STRING | 'true' | 'false' | IDENT | '(' expr ')'
"""

from __future__ import annotations

from typing import List, Optional

from . import ast_nodes as A
from .lexer import LexerError, Token, TokenKind, tokenize


class ParserError(Exception):
    def __init__(self, message: str, line: int, col: int, length: int = 1) -> None:
        super().__init__(message)
        self.message = message
        self.line = line
        self.col = col
        self.length = length


_CMP_TOKENS: dict[TokenKind, str] = {
    TokenKind.EQ: "==",
    TokenKind.NE: "!=",
    TokenKind.LT: "<",
    TokenKind.LE: "<=",
    TokenKind.GT: ">",
    TokenKind.GE: ">=",
}

_ADD_TOKENS: dict[TokenKind, str] = {
    TokenKind.PLUS: "+",
    TokenKind.MINUS: "-",
}

_MUL_TOKENS: dict[TokenKind, str] = {
    TokenKind.STAR: "*",
    TokenKind.SLASH: "/",
    TokenKind.PERCENT: "%",
}

_SHIFT_TOKENS: dict[TokenKind, str] = {
    TokenKind.LSHIFT: "<<",
    TokenKind.RSHIFT: ">>",
}

_BITAND_TOKENS: dict[TokenKind, str] = {
    TokenKind.AMP: "&",
}

_BITXOR_TOKENS: dict[TokenKind, str] = {
    TokenKind.CARET: "^",
}

_BITOR_TOKENS: dict[TokenKind, str] = {
    TokenKind.PIPE: "|",
}


class Parser:
    def __init__(self, tokens: List[Token]) -> None:
        self.toks = tokens
        self.i = 0

    # ------- token helpers -------
    @property
    def cur(self) -> Token:
        return self.toks[self.i]

    def peek(self, off: int = 1) -> Token:
        j = self.i + off
        if j >= len(self.toks):
            return self.toks[-1]
        return self.toks[j]

    def eat(self, kind: TokenKind, what: Optional[str] = None) -> Token:
        if self.cur.kind != kind:
            label = what or kind.name
            raise ParserError(
                f"expected {label}, got {self.cur.kind.name} ({self.cur.value!r})",
                self.cur.line,
                self.cur.col,
                max(1, len(self.cur.value)),
            )
        t = self.cur
        self.i += 1
        return t

    def match(self, *kinds: TokenKind) -> Optional[Token]:
        if self.cur.kind in kinds:
            t = self.cur
            self.i += 1
            return t
        return None

    def skip_newlines(self) -> None:
        while self.cur.kind == TokenKind.NEWLINE:
            self.i += 1

    # ------- entry -------
    def parse_module(self) -> A.Module:
        mod = A.Module()
        self.skip_newlines()
        while self.cur.kind != TokenKind.EOF:
            if self.cur.kind in (TokenKind.KW_IMPORT, TokenKind.KW_FROM):
                if mod.functions or mod.type_aliases or mod.classes or mod.constants:
                    raise ParserError(
                        "import must appear before any 'def', 'type', 'class', or constant",
                        self.cur.line,
                        self.cur.col,
                        max(1, len(self.cur.value)),
                    )
                mod.imports.append(self.parse_import())
            elif self.cur.kind == TokenKind.KW_DEF:
                mod.functions.append(self.parse_func_def())
            elif self.cur.kind == TokenKind.KW_TYPE:
                mod.type_aliases.append(self.parse_type_alias())
            elif self.cur.kind == TokenKind.KW_CLASS:
                cdef = self.parse_class_def()
                mod.classes.append(cdef)
                mod.functions.extend(cdef.method_defs)
            elif self.cur.kind == TokenKind.IDENT and self.peek(1).kind == TokenKind.COLON:
                mod.constants.append(self.parse_const_def())
            else:
                raise ParserError(
                    "expected 'import', 'def', 'type', 'class', or a constant at module level",
                    self.cur.line,
                    self.cur.col,
                    max(1, len(self.cur.value)),
                )
            self.skip_newlines()
        return mod

    # ------- imports -------
    def parse_import(self) -> A.Import:
        if self.cur.kind == TokenKind.KW_FROM:
            return self._parse_from_import()
        # Plain `import IDENT [as IDENT] NEWLINE`.
        kw = self.eat(TokenKind.KW_IMPORT, "'import'")
        name_tok = self.eat(TokenKind.IDENT, "module name after 'import'")
        alias = None
        if self.match(TokenKind.KW_AS):
            alias = self.eat(TokenKind.IDENT, "alias after 'as'").value
        self.eat(TokenKind.NEWLINE)
        return A.Import(name_tok.value, alias, None, kw.line, kw.col)

    def _parse_from_import(self) -> A.Import:
        # `from IDENT import IDENT [as IDENT] { ',' IDENT [as IDENT] }`
        kw = self.eat(TokenKind.KW_FROM, "'from'")
        mod_tok = self.eat(TokenKind.IDENT, "module name after 'from'")
        self.eat(TokenKind.KW_IMPORT, "'import' after module name")
        names: List[tuple] = [self._parse_import_name()]
        while self.match(TokenKind.COMMA):
            names.append(self._parse_import_name())
        self.eat(TokenKind.NEWLINE)
        return A.Import(mod_tok.value, None, names, kw.line, kw.col)

    def _parse_import_name(self) -> tuple:
        """Parse `IDENT [as IDENT]`, returning (name, alias|None)."""
        name_tok = self.eat(TokenKind.IDENT, "imported name")
        alias = None
        if self.match(TokenKind.KW_AS):
            alias = self.eat(TokenKind.IDENT, "alias after 'as'").value
        return (name_tok.value, alias)

    # ------- type aliases -------
    def parse_type_alias(self) -> A.TypeAlias:
        kw = self.eat(TokenKind.KW_TYPE, "'type'")
        name_tok = self.eat(TokenKind.IDENT, "type alias name")
        self.eat(TokenKind.ASSIGN, "'=' in type alias")
        target = self.parse_type()
        self.eat(TokenKind.NEWLINE)
        return A.TypeAlias(name_tok.value, target, kw.line, kw.col)

    # ------- module constants (spec 35) -------
    def parse_const_def(self) -> A.ConstDef:
        name_tok = self.eat(TokenKind.IDENT)
        self.eat(TokenKind.COLON)
        ty = self.parse_type()
        self.eat(TokenKind.ASSIGN, "'=' in constant definition")
        value = self.parse_expr()
        self.eat(TokenKind.NEWLINE)
        lit = value.operand if isinstance(value, A.UnaryOp) and value.op == "-" else value
        if not isinstance(lit, (A.IntLit, A.FloatLit, A.StringLit, A.BoolLit)) or (
            value is not lit and not isinstance(lit, (A.IntLit, A.FloatLit))
        ):
            raise ParserError(
                "a constant must be initialised with a literal",
                name_tok.line, name_tok.col, len(name_tok.value),
            )
        return A.ConstDef(name_tok.value, ty, value, name_tok.line, name_tok.col)

    # ------- records (spec 20) -------
    def parse_class_def(self) -> A.ClassDef:
        kw = self.eat(TokenKind.KW_CLASS, "'class'")
        name_tok = self.eat(TokenKind.IDENT, "class name")
        self.eat(TokenKind.COLON, "':' to start class body")
        self.eat(TokenKind.NEWLINE)
        self.eat(TokenKind.INDENT, "indented block of fields")
        fields: List[A.FieldDecl] = []
        methods: List[str] = []
        method_defs: List[A.FuncDef] = []
        while self.cur.kind != TokenKind.DEDENT:
            if self.cur.kind == TokenKind.NEWLINE:
                self.i += 1
                continue
            if self.cur.kind == TokenKind.KW_DEF:
                fdef = self._parse_method(name_tok.value)
                methods.append(fdef.name.split(".", 1)[1])
                method_defs.append(fdef)
                continue
            f_tok = self.eat(TokenKind.IDENT, "field name")
            self.eat(TokenKind.COLON, "':' after field name")
            ty = self.parse_type()
            self.eat(TokenKind.NEWLINE)
            fields.append(A.FieldDecl(f_tok.value, ty, f_tok.line, f_tok.col))
        self.eat(TokenKind.DEDENT)
        return A.ClassDef(name_tok.value, fields, kw.line, kw.col,
                          methods=methods, method_defs=method_defs)

    def _parse_method(self, class_name: str) -> A.FuncDef:
        """`def m(self, params) -> R:` in a class body, lowered to a function
        `Class.m` with `self: Class` as its first parameter (spec 33)."""
        kw = self.eat(TokenKind.KW_DEF, "'def'")
        name_tok = self.eat(TokenKind.IDENT, "method name")
        self.eat(TokenKind.LPAREN, "'('")
        self_tok = self.cur
        if not (self_tok.kind == TokenKind.IDENT and self_tok.value == "self"):
            raise ParserError(
                "a method's first parameter must be `self`",
                self_tok.line, self_tok.col, max(1, len(self_tok.value)),
            )
        self.i += 1
        params: List[A.Param] = [A.Param(
            "self", A.TypeName(class_name, self_tok.line, self_tok.col),
            self_tok.line, self_tok.col,
        )]
        while self.match(TokenKind.COMMA):
            params.append(self.parse_param())
        self.eat(TokenKind.RPAREN, "')'")
        self.eat(TokenKind.ARROW, "'->' after parameter list")
        ret_ty = self.parse_type()
        self.eat(TokenKind.COLON, "':' to start method body")
        self.eat(TokenKind.NEWLINE)
        body = self.parse_block()
        return A.FuncDef(f"{class_name}.{name_tok.value}", params, ret_ty, body, kw.line, kw.col)

    # ------- function definitions -------
    def parse_func_def(self) -> A.FuncDef:
        kw = self.eat(TokenKind.KW_DEF, "'def'")
        name_tok = self.eat(TokenKind.IDENT, "function name")
        self.eat(TokenKind.LPAREN, "'('")
        params: List[A.Param] = []
        if self.cur.kind != TokenKind.RPAREN:
            params.append(self.parse_param())
            while self.match(TokenKind.COMMA):
                params.append(self.parse_param())
        self.eat(TokenKind.RPAREN, "')'")
        self.eat(TokenKind.ARROW, "'->' after parameter list")
        ret_ty = self.parse_type()
        self.eat(TokenKind.COLON, "':' to start function body")
        self.eat(TokenKind.NEWLINE)
        body = self.parse_block()
        return A.FuncDef(name_tok.value, params, ret_ty, body, kw.line, kw.col)

    def parse_param(self) -> A.Param:
        name_tok = self.eat(TokenKind.IDENT, "parameter name")
        self.eat(TokenKind.COLON, "':' after parameter name")
        ty = self.parse_type()
        return A.Param(name_tok.value, ty, name_tok.line, name_tok.col)

    def parse_type(self) -> A.TypeExpr:
        t = self.cur
        kind_to_name = {
            TokenKind.KW_INT: "int",
            TokenKind.KW_FLOAT: "float",
            TokenKind.KW_BOOL: "bool",
            TokenKind.KW_STRING: "string",
            TokenKind.KW_BYTES: "Bytes",
            TokenKind.KW_VOID: "void",
        }
        if t.kind in kind_to_name:
            self.i += 1
            return A.TypeName(kind_to_name[t.kind], t.line, t.col)
        if t.kind == TokenKind.KW_FUTURE:
            self.i += 1
            self.eat(TokenKind.LBRACK, "'[' after Future")
            inner = self.parse_type()
            self.eat(TokenKind.RBRACK, "']' to close Future[...]")
            return A.TypeFuture(inner, t.line, t.col)
        if t.kind == TokenKind.KW_LIST:
            self.i += 1
            self.eat(TokenKind.LBRACK, "'[' after List")
            if self.cur.kind == TokenKind.KW_INT and self.peek(1).kind == TokenKind.RBRACK:
                self.i += 2  # `int` `]`
                return A.TypeName("List[int]", t.line, t.col)
            elem = self.parse_type()
            self.eat(TokenKind.RBRACK, "']' to close List[...]")
            return A.TypeList(elem, t.line, t.col)
        if t.kind == TokenKind.KW_OPTION:
            self.i += 1
            self.eat(TokenKind.LBRACK, "'[' after Option")
            if self.cur.kind == TokenKind.KW_INT and self.peek(1).kind == TokenKind.RBRACK:
                self.i += 2
                return A.TypeName("Option[int]", t.line, t.col)
            inner = self.parse_type()
            self.eat(TokenKind.RBRACK, "']' to close Option[...]")
            return A.TypeOption(inner, t.line, t.col)
        if t.kind == TokenKind.KW_RESULT:
            self.i += 1
            self.eat(TokenKind.LBRACK, "'[' after Result")
            ok = self.parse_type()
            self.eat(TokenKind.COMMA, "',' between Result type arguments")
            err = self.parse_type()
            self.eat(TokenKind.RBRACK, "']' to close Result[...]")
            if (isinstance(ok, A.TypeName) and ok.name == "int" and ok.module is None
                    and isinstance(err, A.TypeName) and err.name == "int" and err.module is None):
                return A.TypeName("Result[int, int]", t.line, t.col)
            return A.TypeResult(ok, err, t.line, t.col)
        # `Callable[[P1, ...], R]` (spec 21). `Callable` is not a keyword; it is
        # only special in type position.
        # `Dict[K, V]` (spec 31); like Callable, special only in type position.
        if t.kind == TokenKind.IDENT and t.value == "Dict" and self.peek(1).kind == TokenKind.LBRACK:
            self.i += 2  # `Dict` `[`
            key = self.parse_type()
            self.eat(TokenKind.COMMA, "',' between Dict type arguments")
            value = self.parse_type()
            self.eat(TokenKind.RBRACK, "']' to close Dict[...]")
            return A.TypeDict(key, value, t.line, t.col)
        if t.kind == TokenKind.IDENT and t.value == "Callable" and self.peek(1).kind == TokenKind.LBRACK:
            self.i += 1
            self.eat(TokenKind.LBRACK, "'[' after Callable")
            self.eat(TokenKind.LBRACK, "'[' to start the parameter type list")
            params: List[A.TypeExpr] = []
            if self.cur.kind != TokenKind.RBRACK:
                params.append(self.parse_type())
                while self.match(TokenKind.COMMA):
                    params.append(self.parse_type())
            self.eat(TokenKind.RBRACK, "']' to close the parameter type list")
            self.eat(TokenKind.COMMA, "',' before the return type")
            ret = self.parse_type()
            self.eat(TokenKind.RBRACK, "']' to close Callable[...]")
            return A.TypeCallable(params, ret, t.line, t.col)
        if t.kind == TokenKind.IDENT:
            self.i += 1
            if self.cur.kind == TokenKind.DOT:
                self.i += 1  # consume '.'
                name = self.eat(TokenKind.IDENT, "class name after '.'")
                return A.TypeName(name.value, t.line, t.col, module=t.value)
            return A.TypeName(t.value, t.line, t.col)
        raise ParserError(
            f"expected type, got {t.kind.name}", t.line, t.col, max(1, len(t.value))
        )

    # ------- blocks and statements -------
    def parse_block(self) -> List[A.Stmt]:
        self.eat(TokenKind.INDENT, "indented block")
        stmts: List[A.Stmt] = []
        while self.cur.kind != TokenKind.DEDENT:
            if self.cur.kind == TokenKind.EOF:
                raise ParserError(
                    "unexpected end of file inside block",
                    self.cur.line,
                    self.cur.col,
                )
            if self.cur.kind == TokenKind.NEWLINE:
                self.i += 1
                continue
            stmts.append(self.parse_stmt())
        self.eat(TokenKind.DEDENT)
        if not stmts:
            raise ParserError("empty block", self.cur.line, self.cur.col)
        return stmts

    def parse_stmt(self) -> A.Stmt:
        t = self.cur
        if t.kind == TokenKind.KW_RETURN:
            return self.parse_return()
        if t.kind == TokenKind.KW_IF:
            return self.parse_if()
        if t.kind == TokenKind.KW_WHILE:
            return self.parse_while()
        if t.kind == TokenKind.KW_BREAK:
            kw = self.eat(TokenKind.KW_BREAK)
            self.eat(TokenKind.NEWLINE)
            return A.Break(kw.line, kw.col)
        if t.kind == TokenKind.KW_CONTINUE:
            kw = self.eat(TokenKind.KW_CONTINUE)
            self.eat(TokenKind.NEWLINE)
            return A.Continue(kw.line, kw.col)
        if t.kind == TokenKind.KW_FOR:
            return self.parse_for()
        if t.kind == TokenKind.KW_MATCH:
            return self.parse_match()
        if t.kind == TokenKind.KW_ASSERT:
            return self.parse_assert()
        # IDENT followed by ':'  => var_decl
        # IDENT followed by '='  => assignment
        if t.kind == TokenKind.IDENT:
            nxt = self.peek(1).kind
            if nxt == TokenKind.COLON:
                return self.parse_var_decl()
            if nxt == TokenKind.ASSIGN:
                return self.parse_assign()
            if nxt in self._COMPOUND_OPS:
                # `x OP= e` -> `x = x OP e` (spec 27).
                name_tok = self.eat(TokenKind.IDENT)
                op = self._COMPOUND_OPS[self.cur.kind]
                self.i += 1
                value = self.parse_expr()
                self.eat(TokenKind.NEWLINE)
                target = A.Name(name_tok.value, name_tok.line, name_tok.col)
                return A.Assign(
                    name_tok.value,
                    A.BinOp(op, target, value, name_tok.line, name_tok.col),
                    name_tok.line, name_tok.col,
                )
        # else: expression statement
        return self.parse_expr_stmt()

    def parse_return(self) -> A.Return:
        kw = self.eat(TokenKind.KW_RETURN)
        if self.cur.kind == TokenKind.NEWLINE:
            self.eat(TokenKind.NEWLINE)
            return A.Return(None, kw.line, kw.col)
        expr = self.parse_expr()
        self.eat(TokenKind.NEWLINE)
        return A.Return(expr, kw.line, kw.col)

    def parse_assert(self) -> A.Assert:
        kw = self.eat(TokenKind.KW_ASSERT)
        cond = self.parse_expr()
        msg: Optional[A.Expr] = None
        if self.match(TokenKind.COMMA):
            msg = self.parse_expr()
        self.eat(TokenKind.NEWLINE)
        return A.Assert(cond, msg, kw.line, kw.col)

    def parse_if(self) -> A.If:
        kw = self.eat(TokenKind.KW_IF)
        cond = self.parse_expr()
        self.eat(TokenKind.COLON, "':' after if condition")
        self.eat(TokenKind.NEWLINE)
        then_body = self.parse_block()
        else_body: List[A.Stmt] = []
        if self.cur.kind == TokenKind.KW_ELIF:
            # Normalize `elif` into nested If inside else_body.
            else_body = [self.parse_if_from_elif()]
        elif self.cur.kind == TokenKind.KW_ELSE:
            self.eat(TokenKind.KW_ELSE)
            self.eat(TokenKind.COLON, "':' after else")
            self.eat(TokenKind.NEWLINE)
            else_body = self.parse_block()
        return A.If(cond, then_body, else_body, kw.line, kw.col)

    def parse_if_from_elif(self) -> A.If:
        kw = self.eat(TokenKind.KW_ELIF)
        cond = self.parse_expr()
        self.eat(TokenKind.COLON, "':' after elif condition")
        self.eat(TokenKind.NEWLINE)
        then_body = self.parse_block()
        else_body: List[A.Stmt] = []
        if self.cur.kind == TokenKind.KW_ELIF:
            else_body = [self.parse_if_from_elif()]
        elif self.cur.kind == TokenKind.KW_ELSE:
            self.eat(TokenKind.KW_ELSE)
            self.eat(TokenKind.COLON, "':' after else")
            self.eat(TokenKind.NEWLINE)
            else_body = self.parse_block()
        return A.If(cond, then_body, else_body, kw.line, kw.col)

    def parse_while(self) -> A.While:
        kw = self.eat(TokenKind.KW_WHILE)
        cond = self.parse_expr()
        self.eat(TokenKind.COLON, "':' after while condition")
        self.eat(TokenKind.NEWLINE)
        body = self.parse_block()
        return A.While(cond, body, kw.line, kw.col)

    def parse_for(self) -> A.Stmt:
        kw = self.eat(TokenKind.KW_FOR)
        var_tok = self.eat(TokenKind.IDENT, "loop variable name")
        self.eat(TokenKind.KW_IN, "'in' after for variable")
        if not (
            self.cur.kind == TokenKind.IDENT
            and self.cur.value == "range"
            and self.peek(1).kind == TokenKind.LPAREN
        ):
            # `for x in <list or string>` (spec 29).
            iterable = self.parse_expr()
            self.eat(TokenKind.COLON, "':' after for header")
            self.eat(TokenKind.NEWLINE)
            body = self.parse_block()
            return A.ForEach(var_tok.value, iterable, body, kw.line, kw.col)
        # range header: the identifier `range` followed by ( args )
        self.i += 1  # consume `range`
        self.eat(TokenKind.LPAREN, "'(' after range")
        args: List[A.Expr] = []
        if self.cur.kind != TokenKind.RPAREN:
            args.append(self.parse_expr())
            while self.cur.kind == TokenKind.COMMA:
                self.i += 1
                args.append(self.parse_expr())
        self.eat(TokenKind.RPAREN, "')' to close range")
        if not (1 <= len(args) <= 3):
            raise ParserError(
                "range() takes 1 to 3 arguments",
                kw.line, kw.col,
            )
        # Fill defaults: range(stop) / range(start, stop) / range(start, stop, step)
        if len(args) == 1:
            start: A.Expr = A.IntLit(0, kw.line, kw.col)
            stop = args[0]
        else:
            start = args[0]
            stop = args[1]
        if len(args) == 3:
            step: A.Expr = args[2]
        else:
            step = A.IntLit(1, kw.line, kw.col)
        self.eat(TokenKind.COLON, "':' after for header")
        self.eat(TokenKind.NEWLINE)
        body = self.parse_block()
        return A.For(var_tok.value, start, stop, step, body, kw.line, kw.col)

    def parse_match(self) -> A.MatchStmt:
        kw = self.eat(TokenKind.KW_MATCH)
        target = self.parse_expr()
        self.eat(TokenKind.COLON, "':' after match target")
        self.eat(TokenKind.NEWLINE)
        # The match body is a sequence of `case` arms inside one indented
        # block. We can't use parse_block here because parse_block calls
        # parse_stmt on each line and `case` is not a statement.
        self.eat(TokenKind.INDENT, "indented match body")

        style: Optional[str] = None  # "option" or "result"
        some_var: Optional[str] = None
        some_block: Optional[List[A.Stmt]] = None
        none_block: Optional[List[A.Stmt]] = None
        ok_var: Optional[str] = None
        ok_block: Optional[List[A.Stmt]] = None
        err_var: Optional[str] = None
        err_block: Optional[List[A.Stmt]] = None

        while self.cur.kind != TokenKind.DEDENT:
            if self.cur.kind == TokenKind.NEWLINE:
                self.i += 1
                continue
            if self.cur.kind != TokenKind.KW_CASE:
                raise ParserError(
                    "expected `case` arm in match body",
                    self.cur.line, self.cur.col,
                    max(1, len(self.cur.value)),
                )
            self.eat(TokenKind.KW_CASE)
            arm_tok = self.cur

            # Determine this arm's style.
            if arm_tok.kind in (TokenKind.KW_SOME, TokenKind.KW_NONE):
                this_style = "option"
            elif arm_tok.kind in (TokenKind.KW_OK, TokenKind.KW_ERR):
                this_style = "result"
            else:
                raise ParserError(
                    "match case must be `Some(x)` / `None` or `Ok(x)` / `Err(e)`",
                    arm_tok.line, arm_tok.col,
                    max(1, len(arm_tok.value)),
                )

            # Lock in style on the first arm; reject mixed pairs.
            if style is None:
                style = this_style
            elif style != this_style:
                expected = "Some/None" if style == "option" else "Ok/Err"
                raise ParserError(
                    f"mixed match arms: expected `{expected}` pair, got `{arm_tok.value}`",
                    arm_tok.line, arm_tok.col,
                    max(1, len(arm_tok.value)),
                )

            if arm_tok.kind == TokenKind.KW_SOME:
                if some_block is not None:
                    raise ParserError(
                        "duplicate `case Some(...)` arm in match",
                        arm_tok.line, arm_tok.col, 4,
                    )
                self.eat(TokenKind.KW_SOME)
                self.eat(TokenKind.LPAREN, "'(' after Some")
                ident = self.eat(TokenKind.IDENT, "identifier in Some(...)")
                self.eat(TokenKind.RPAREN, "')' to close Some(...)")
                self.eat(TokenKind.COLON, "':' after case pattern")
                self.eat(TokenKind.NEWLINE)
                some_var = ident.value
                some_block = self.parse_block()
            elif arm_tok.kind == TokenKind.KW_NONE:
                if none_block is not None:
                    raise ParserError(
                        "duplicate `case None` arm in match",
                        arm_tok.line, arm_tok.col, 4,
                    )
                self.eat(TokenKind.KW_NONE)
                self.eat(TokenKind.COLON, "':' after case pattern")
                self.eat(TokenKind.NEWLINE)
                none_block = self.parse_block()
            elif arm_tok.kind == TokenKind.KW_OK:
                if ok_block is not None:
                    raise ParserError(
                        "duplicate `case Ok(...)` arm in match",
                        arm_tok.line, arm_tok.col, 2,
                    )
                self.eat(TokenKind.KW_OK)
                self.eat(TokenKind.LPAREN, "'(' after Ok")
                ident = self.eat(TokenKind.IDENT, "identifier in Ok(...)")
                self.eat(TokenKind.RPAREN, "')' to close Ok(...)")
                self.eat(TokenKind.COLON, "':' after case pattern")
                self.eat(TokenKind.NEWLINE)
                ok_var = ident.value
                ok_block = self.parse_block()
            elif arm_tok.kind == TokenKind.KW_ERR:
                if err_block is not None:
                    raise ParserError(
                        "duplicate `case Err(...)` arm in match",
                        arm_tok.line, arm_tok.col, 3,
                    )
                self.eat(TokenKind.KW_ERR)
                self.eat(TokenKind.LPAREN, "'(' after Err")
                ident = self.eat(TokenKind.IDENT, "identifier in Err(...)")
                self.eat(TokenKind.RPAREN, "')' to close Err(...)")
                self.eat(TokenKind.COLON, "':' after case pattern")
                self.eat(TokenKind.NEWLINE)
                err_var = ident.value
                err_block = self.parse_block()

        self.eat(TokenKind.DEDENT)

        if style is None:
            raise ParserError(
                "match must have at least one case arm",
                kw.line, kw.col, 5,
            )
        if style == "option":
            if some_block is None or none_block is None or some_var is None:
                raise ParserError(
                    "match on Option[int] must cover both Some and None",
                    kw.line, kw.col, 5,
                )
        else:  # "result"
            if (ok_block is None or err_block is None
                    or ok_var is None or err_var is None):
                raise ParserError(
                    "match on Result[int, int] must cover both Ok and Err",
                    kw.line, kw.col, 5,
                )

        return A.MatchStmt(
            target, style,
            some_var, some_block, none_block,
            ok_var, ok_block, err_var, err_block,
            kw.line, kw.col,
        )

    # Compound assignment operator token -> the binary operator (spec 27).
    _COMPOUND_OPS = {
        TokenKind.PLUS_ASSIGN: "+",
        TokenKind.MINUS_ASSIGN: "-",
        TokenKind.STAR_ASSIGN: "*",
        TokenKind.SLASH_ASSIGN: "/",
        TokenKind.PERCENT_ASSIGN: "%",
        TokenKind.AMP_ASSIGN: "&",
        TokenKind.PIPE_ASSIGN: "|",
        TokenKind.CARET_ASSIGN: "^",
    }

    def parse_var_decl(self) -> A.VarDecl:
        name_tok = self.eat(TokenKind.IDENT)
        self.eat(TokenKind.COLON)
        ty = self.parse_type()
        self.eat(TokenKind.ASSIGN, "'=' in variable declaration")
        value = self.parse_expr()
        self.eat(TokenKind.NEWLINE)
        return A.VarDecl(name_tok.value, ty, value, name_tok.line, name_tok.col)

    def parse_assign(self) -> A.Assign:
        name_tok = self.eat(TokenKind.IDENT)
        self.eat(TokenKind.ASSIGN)
        value = self.parse_expr()
        self.eat(TokenKind.NEWLINE)
        return A.Assign(name_tok.value, value, name_tok.line, name_tok.col)

    @classmethod
    def _is_simple_key(cls, e: A.Expr) -> bool:
        """A literal or a plain path: safe to evaluate twice."""
        return isinstance(e, (A.IntLit, A.StringLit, A.BoolLit)) or cls._is_plain_path(e)

    @classmethod
    def _is_plain_path(cls, e: A.Expr) -> bool:
        """A name followed by field accesses and indexes with simple keys (no
        calls): evaluating it twice reads the same thing again."""
        while isinstance(e, (A.FieldAccess, A.Index)):
            if isinstance(e, A.Index) and not cls._is_simple_key(e.index):
                return False
            e = e.obj
        return isinstance(e, A.Name)

    def parse_expr_stmt(self) -> A.Stmt:
        t = self.cur
        expr = self.parse_expr()
        if self.cur.kind in self._COMPOUND_OPS and isinstance(expr, A.Index):
            # `d[k] OP= e` -> `d[k] = d[k] OP e` (spec 31). Both the object and
            # the key are evaluated twice, so they must be side-effect free.
            if not (self._is_plain_path(expr.obj) and self._is_simple_key(expr.index)):
                raise ParserError(
                    "invalid compound assignment target", t.line, t.col, max(1, len(t.value)),
                )
            op = self._COMPOUND_OPS[self.cur.kind]
            self.i += 1
            value = self.parse_expr()
            self.eat(TokenKind.NEWLINE)
            current = A.Index(expr.obj, expr.index, expr.line, expr.col)
            return A.IndexAssign(
                expr.obj, expr.index,
                A.BinOp(op, current, value, expr.line, expr.col),
                expr.line, expr.col,
            )
        if self.cur.kind in self._COMPOUND_OPS:
            # `a.b.f OP= e` -> `a.b.f = a.b.f OP e` (spec 27). The record path
            # is evaluated twice, so it may only be names and field accesses.
            if not isinstance(expr, A.FieldAccess) or not self._is_plain_path(expr.obj):
                raise ParserError(
                    "invalid compound assignment target", t.line, t.col, max(1, len(t.value)),
                )
            op = self._COMPOUND_OPS[self.cur.kind]
            self.i += 1
            value = self.parse_expr()
            self.eat(TokenKind.NEWLINE)
            current = A.FieldAccess(expr.obj, expr.field, expr.line, expr.col)
            return A.FieldAssign(
                expr.obj, expr.field,
                A.BinOp(op, current, value, expr.line, expr.col),
                expr.line, expr.col,
            )
        if self.cur.kind == TokenKind.ASSIGN:
            if isinstance(expr, A.Index):
                # `obj[k] = v`: sema accepts it for dicts only (spec 31).
                self.i += 1  # consume '='
                value = self.parse_expr()
                self.eat(TokenKind.NEWLINE)
                return A.IndexAssign(expr.obj, expr.index, value, expr.line, expr.col)
            if isinstance(expr, A.Slice):
                raise ParserError(
                    "cannot assign to a slice", t.line, t.col, max(1, len(t.value)),
                )
            # `obj.field = value` (spec 20): the only assignable expression.
            if not isinstance(expr, A.FieldAccess):
                raise ParserError(
                    "invalid assignment target", t.line, t.col, max(1, len(t.value)),
                )
            self.i += 1  # consume '='
            value = self.parse_expr()
            self.eat(TokenKind.NEWLINE)
            return A.FieldAssign(expr.obj, expr.field, value, expr.line, expr.col)
        self.eat(TokenKind.NEWLINE)
        return A.ExprStmt(expr, t.line, t.col)

    # ------- expressions -------
    def parse_expr(self) -> A.Expr:
        return self.parse_ternary()

    def parse_ternary(self) -> A.Expr:
        # Conditional expression: `then if cond else els` (Python ternary).
        # Lowest precedence; the `else` branch parses another ternary so that
        # chained `a if p else b if q else c` is right-associative.
        then = self.parse_or()
        if self.cur.kind != TokenKind.KW_IF:
            return then
        t = self.cur
        self.i += 1  # consume `if`
        cond = self.parse_or()
        self.eat(TokenKind.KW_ELSE, "'else' in conditional expression")
        els = self.parse_ternary()
        return A.IfExpr(then, cond, els, t.line, t.col)

    def parse_or(self) -> A.Expr:
        left = self.parse_and()
        while self.cur.kind == TokenKind.KW_OR:
            t = self.cur
            self.i += 1
            right = self.parse_and()
            left = A.BinOp("or", left, right, t.line, t.col)
        return left

    def parse_and(self) -> A.Expr:
        left = self.parse_not()
        while self.cur.kind == TokenKind.KW_AND:
            t = self.cur
            self.i += 1
            right = self.parse_not()
            left = A.BinOp("and", left, right, t.line, t.col)
        return left

    def parse_not(self) -> A.Expr:
        if self.cur.kind == TokenKind.KW_NOT:
            t = self.cur
            self.i += 1
            inner = self.parse_not()
            return A.UnaryOp("not", inner, t.line, t.col)
        return self.parse_cmp()

    def parse_cmp(self) -> A.Expr:
        left = self.parse_bitor()
        if self.cur.kind in _CMP_TOKENS:
            t = self.cur
            op = _CMP_TOKENS[t.kind]
            self.i += 1
            right = self.parse_bitor()
            left = A.BinOp(op, left, right, t.line, t.col)
        return left

    def parse_bitor(self) -> A.Expr:
        left = self.parse_bitxor()
        while self.cur.kind in _BITOR_TOKENS:
            t = self.cur
            op = _BITOR_TOKENS[t.kind]
            self.i += 1
            right = self.parse_bitxor()
            left = A.BinOp(op, left, right, t.line, t.col)
        return left

    def parse_bitxor(self) -> A.Expr:
        left = self.parse_bitand()
        while self.cur.kind in _BITXOR_TOKENS:
            t = self.cur
            op = _BITXOR_TOKENS[t.kind]
            self.i += 1
            right = self.parse_bitand()
            left = A.BinOp(op, left, right, t.line, t.col)
        return left

    def parse_bitand(self) -> A.Expr:
        left = self.parse_shift()
        while self.cur.kind in _BITAND_TOKENS:
            t = self.cur
            op = _BITAND_TOKENS[t.kind]
            self.i += 1
            right = self.parse_shift()
            left = A.BinOp(op, left, right, t.line, t.col)
        return left

    def parse_shift(self) -> A.Expr:
        left = self.parse_add()
        while self.cur.kind in _SHIFT_TOKENS:
            t = self.cur
            op = _SHIFT_TOKENS[t.kind]
            self.i += 1
            right = self.parse_add()
            left = A.BinOp(op, left, right, t.line, t.col)
        return left

    def parse_add(self) -> A.Expr:
        left = self.parse_mul()
        while self.cur.kind in _ADD_TOKENS:
            t = self.cur
            op = _ADD_TOKENS[t.kind]
            self.i += 1
            right = self.parse_mul()
            left = A.BinOp(op, left, right, t.line, t.col)
        return left

    def parse_mul(self) -> A.Expr:
        left = self.parse_unary()
        while self.cur.kind in _MUL_TOKENS:
            t = self.cur
            op = _MUL_TOKENS[t.kind]
            self.i += 1
            right = self.parse_unary()
            left = A.BinOp(op, left, right, t.line, t.col)
        return left

    def parse_unary(self) -> A.Expr:
        t = self.cur
        if t.kind == TokenKind.MINUS:
            self.i += 1
            inner = self.parse_unary()
            return A.UnaryOp("-", inner, t.line, t.col)
        if t.kind == TokenKind.TILDE:
            self.i += 1
            inner = self.parse_unary()
            return A.UnaryOp("~", inner, t.line, t.col)
        if t.kind == TokenKind.KW_AWAIT:
            self.i += 1
            inner = self.parse_unary()
            return A.AwaitExpr(inner, t.line, t.col)
        if t.kind == TokenKind.KW_SPAWN:
            self.i += 1
            # spawn must be followed by a call expression
            atom = self.parse_atom_postfix()
            if not isinstance(atom, A.Call):
                raise ParserError(
                    "'spawn' must be followed by a function call",
                    t.line,
                    t.col,
                    5,
                )
            return A.SpawnExpr(atom, t.line, t.col)
        if t.kind == TokenKind.KW_SOME:
            self.i += 1
            self.eat(TokenKind.LPAREN, "'(' after Some")
            arg = self.parse_expr()
            self.eat(TokenKind.RPAREN, "')' to close Some(...)")
            return A.SomeExpr(arg, t.line, t.col)
        if t.kind == TokenKind.KW_NONE:
            self.i += 1
            return A.NoneExpr(t.line, t.col)
        if t.kind == TokenKind.KW_OK:
            self.i += 1
            self.eat(TokenKind.LPAREN, "'(' after Ok")
            arg = self.parse_expr()
            self.eat(TokenKind.RPAREN, "')' to close Ok(...)")
            return A.OkExpr(arg, t.line, t.col)
        if t.kind == TokenKind.KW_ERR:
            self.i += 1
            self.eat(TokenKind.LPAREN, "'(' after Err")
            arg = self.parse_expr()
            self.eat(TokenKind.RPAREN, "')' to close Err(...)")
            return A.ErrExpr(arg, t.line, t.col)
        return self.parse_atom_postfix()

    def parse_atom_postfix(self) -> A.Expr:
        atom = self.parse_atom()
        # Only allow call when atom is a bare Name (MVP: no first-class fns).
        if self.cur.kind == TokenKind.LPAREN and isinstance(atom, A.Name):
            self.i += 1  # consume '('
            args = self._parse_arglist()
            atom = A.Call(atom.name, args, atom.line, atom.col)
        while self.cur.kind in (TokenKind.DOT, TokenKind.LPAREN, TokenKind.LBRACK):
            if self.cur.kind == TokenKind.LBRACK:
                atom = self._parse_subscript(atom)
                continue
            if self.cur.kind == TokenKind.LPAREN:
                # Call of a function value: `a.b.f(x)`, `pick()(x)` (spec 21).
                self.i += 1  # consume '('
                args = self._parse_arglist()
                atom = A.CallValue(atom, args, atom.line, atom.col)
                continue
            self.i += 1  # consume '.'
            name = self.eat(TokenKind.IDENT, "name after '.'")
            if isinstance(atom, A.Name) and self.cur.kind == TokenKind.LPAREN:
                # `q.f(...)`: a module-qualified call (spec 17), or a call of
                # a function-valued field when `q` is a local (spec 21; sema
                # decides).
                self.i += 1  # consume '('
                args = self._parse_arglist()
                atom = A.Call(name.value, args, atom.line, atom.col, module=atom.name)
            else:
                # Field access `expr.field` (spec 20).
                atom = A.FieldAccess(atom, name.value, name.line, name.col)
        return atom

    def _lower_fstring(self, t: Token) -> A.Expr:
        """f"a{x}b" -> "a" + str(x) + "b" (spec 34)."""
        pieces: List[A.Expr] = []
        for kind, text, line, col in t.parts or []:
            if kind == "lit":
                pieces.append(A.StringLit(text, line, col))
                continue
            stripped = text.lstrip()
            col += len(text) - len(stripped)
            try:
                toks = tokenize(stripped.rstrip())
            except LexerError as e:
                raise ParserError(e.message, line, col + e.col - 1, e.length) from e
            for tok in toks:  # report errors at the f-string's position
                if tok.line == 1:
                    tok.col += col - 1
                tok.line += line - 1
            sub = Parser(toks)
            expr = sub.parse_expr()
            if sub.cur.kind not in (TokenKind.NEWLINE, TokenKind.EOF):
                raise ParserError(
                    "invalid expression in f-string (format specs are not supported)",
                    sub.cur.line, sub.cur.col, max(1, len(sub.cur.value)),
                )
            pieces.append(A.Call("str", [expr], line, col))
        if not pieces:
            return A.StringLit("", t.line, t.col)
        out = pieces[0]
        if isinstance(out, A.Call):
            out = A.BinOp("+", A.StringLit("", t.line, t.col), out, t.line, t.col)
        for p in pieces[1:]:
            out = A.BinOp("+", out, p, t.line, t.col)
        return out

    def _parse_subscript(self, obj: A.Expr) -> A.Expr:
        """`[index]` or `[start?:stop?]` after `obj` (spec 29)."""
        lb = self.eat(TokenKind.LBRACK)
        start: Optional[A.Expr] = None
        if self.cur.kind != TokenKind.COLON:
            start = self.parse_expr()
        if self.match(TokenKind.COLON):
            stop: Optional[A.Expr] = None
            if self.cur.kind != TokenKind.RBRACK:
                stop = self.parse_expr()
            self.eat(TokenKind.RBRACK, "']' to close the slice")
            return A.Slice(obj, start, stop, lb.line, lb.col)
        self.eat(TokenKind.RBRACK, "']' to close the index")
        assert start is not None
        return A.Index(obj, start, lb.line, lb.col)

    def _parse_arglist(self) -> List[A.Expr]:
        """Parse a comma-separated argument list, with `(` already consumed,
        up to and including the closing `)`."""
        args: List[A.Expr] = []
        if self.cur.kind != TokenKind.RPAREN:
            args.append(self.parse_expr())
            while self.match(TokenKind.COMMA):
                args.append(self.parse_expr())
        self.eat(TokenKind.RPAREN, "')'")
        return args

    def parse_atom(self) -> A.Expr:
        t = self.cur
        if t.kind == TokenKind.INT:
            self.i += 1
            # base=0 auto-detects 0x/0o/0b prefixes; underscores are accepted
            # by Python's int(). Plain decimals (incl. "0") work too because
            # the lexer never emits a leading-zero non-zero decimal.
            return A.IntLit(int(t.value, 0), t.line, t.col)
        if t.kind == TokenKind.FLOAT:
            self.i += 1
            return A.FloatLit(float(t.value), t.line, t.col)
        if t.kind == TokenKind.STRING:
            self.i += 1
            return A.StringLit(t.value, t.line, t.col)
        if t.kind == TokenKind.FSTRING:
            self.i += 1
            return self._lower_fstring(t)
        if t.kind == TokenKind.KW_TRUE:
            self.i += 1
            return A.BoolLit(True, t.line, t.col)
        if t.kind == TokenKind.KW_FALSE:
            self.i += 1
            return A.BoolLit(False, t.line, t.col)
        if t.kind == TokenKind.IDENT:
            self.i += 1
            return A.Name(t.value, t.line, t.col)
        # `int(x)` / `float(x)` conversions (spec 26): the type keywords act
        # as builtin function names when called.
        if t.kind in (TokenKind.KW_INT, TokenKind.KW_FLOAT) and self.peek(1).kind == TokenKind.LPAREN:
            self.i += 1
            return A.Name(t.value, t.line, t.col)
        if t.kind == TokenKind.LBRACK:
            # List literal `[e1, ..., en]` (spec 29).
            self.i += 1
            elems: List[A.Expr] = []
            if self.cur.kind != TokenKind.RBRACK:
                elems.append(self.parse_expr())
                while self.match(TokenKind.COMMA):
                    elems.append(self.parse_expr())
            self.eat(TokenKind.RBRACK, "']' to close the list")
            return A.ListLit(elems, t.line, t.col)
        if t.kind == TokenKind.LPAREN:
            self.i += 1
            inner = self.parse_expr()
            self.eat(TokenKind.RPAREN, "')'")
            return inner
        raise ParserError(
            f"unexpected token in expression: {t.kind.name}",
            t.line, t.col,
            max(1, len(t.value)),
        )


def parse(tokens: List[Token]) -> A.Module:
    return Parser(tokens).parse_module()
