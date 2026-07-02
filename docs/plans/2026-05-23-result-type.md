# Result[int, int] + match Ok/Err Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `Result[int, int]` type and `Ok(e)` / `Err(e)` value-construction expressions to the rw language, and extend the existing `match` statement to accept either a Some/None pair or an Ok/Err pair of two arms. The target state is that a function like `div_checked(a, b) -> Result[int, int]` can be written in rw and destructured with `match`.

**Architecture:** `Result[int, int]` uses the same LLVM struct as `Option[int]` — `{i64 tag, i64 payload}` (tag=0 = Err, 1 = Ok) — but at the Sema level `T.OPTION_INT` and `T.RESULT_INT_INT` are distinguished as separate types. A `style: "option" | "result"` field is added to the `MatchStmt` AST, and the parser determines the style from the first arm. Sema / irgen branch on the style, and on the irgen side a shared `_emit_arm` helper factors out the Option / Result lowering.

**Tech Stack:** Python 3.12 + llvmlite (compiler), pytest (tests). No runtime changes.

**Spec:** `docs/specs/11-result-type.md`

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `rwc/types.py` | Primitive type definitions | Add `RESULT_INT_INT` |
| `rwc/lexer.py` | Keyword recognition | `KW_RESULT` / `KW_OK` / `KW_ERR` |
| `rwc/ast_nodes.py` | AST nodes | Add `OkExpr` / `ErrExpr`; add style and Result fields to `MatchStmt` |
| `rwc/parser.py` | Type + expression + statement parsing | Result branch in `parse_type`, `Ok(e)` / `Err(e)` expressions, full rewrite of `parse_match` |
| `rwc/sema.py` | Type resolution + expr/stmt Sema | `_resolve_type` / `OkExpr` / `ErrExpr` Sema / `MatchStmt` style branching |
| `rwc/irgen.py` | LLVM IR generation | `RW_RESULT_INT_INT_TY` / `Ok` / `Err` emit / style-specific lowering of `MatchStmt` via `_emit_arm` |
| `tests/test_sema.py` | Positive/negative type checking | Add tests |
| `tests/test_e2e.py` | Add result_basic to parametrize | Add 1 line |
| `examples/result_basic.rw` | Demo | New |
| `examples/result_basic.rw.expected` | Expected output | New |

The runtime (`runtime/*`) and anything fiber-related are left untouched.

---

## Task 1: Recognize `Result[int, int]` syntax in lexer / parser / types / AST

This task makes the `Result[int, int]` type annotation, `Ok(e)` / `Err(e)` expressions, and the `match v: case Ok(x): ... case Err(e): ...` statement (as well as the existing Some/None) **constructible all the way to the AST**. It includes a breaking change that rewrites the `MatchStmt` AST structure into a style-unified form, so verify that existing Option-style code/tests do not regress.

**Files:**
- Modify: `rwc/types.py`
- Modify: `rwc/lexer.py`
- Modify: `rwc/ast_nodes.py`
- Modify: `rwc/parser.py`
- Modify: `rwc/sema.py` (add only `_resolve_type` + make the existing `_check_stmt` MatchStmt handle style branching)
- Modify: `rwc/irgen.py` (make the existing `_emit_stmt` MatchStmt handle style branching)
- Modify: `tests/test_sema.py`

### types / lexer

- [ ] **Step 1.1: Add `RESULT_INT_INT` to `rwc/types.py`**

Add it **directly below** `OPTION_INT = _Primitive("Option[int]")`:

```python
RESULT_INT_INT = _Primitive("Result[int, int]")
```

Do not include it in `is_printable` / `is_numeric`.

- [ ] **Step 1.2: Add three keywords to `rwc/lexer.py`**

**Directly below** `KW_NONE = auto()` in the `TokenKind` enum:

```python
    KW_RESULT = auto()
    KW_OK = auto()
    KW_ERR = auto()
```

**Directly below** `"None": TokenKind.KW_NONE,` in the `KEYWORDS` dict:

```python
    "Result": TokenKind.KW_RESULT,
    "Ok":     TokenKind.KW_OK,
    "Err":    TokenKind.KW_ERR,
```

### AST

- [ ] **Step 1.3: Add `OkExpr` / `ErrExpr` to `rwc/ast_nodes.py`**

**Directly below** the existing `NoneExpr`:

```python
@dataclass
class OkExpr:
    arg: "Expr"
    line: int
    col: int


@dataclass
class ErrExpr:
    arg: "Expr"
    line: int
    col: int
```

Add two entries to the `Expr` Union:

```python
Expr = Union[
    IntLit, FloatLit, BoolLit, StringLit, Name,
    UnaryOp, BinOp, Call, SpawnExpr, AwaitExpr,
    SomeExpr, NoneExpr,
    OkExpr, ErrExpr,
]
```

- [ ] **Step 1.4: Extend `MatchStmt` in `rwc/ast_nodes.py` into the style-unified form**

The current `MatchStmt`:

```python
@dataclass
class MatchStmt:
    target: Expr
    some_var: str
    some_block: List["Stmt"]
    none_block: List["Stmt"]
    line: int
    col: int
```

Replace it with the following:

```python
@dataclass
class MatchStmt:
    target: Expr
    style: str                              # "option" or "result"
    # Option-style fields (None when style == "result")
    some_var: Optional[str]
    some_block: Optional[List["Stmt"]]
    none_block: Optional[List["Stmt"]]
    # Result-style fields (None when style == "option")
    ok_var: Optional[str]
    ok_block: Optional[List["Stmt"]]
    err_var: Optional[str]
    err_block: Optional[List["Stmt"]]
    line: int
    col: int
```

Add an import for `Optional` if it is not already imported.

### parser

- [ ] **Step 1.5: Add the Result branch directly below the Option branch in `parse_type`**

**Directly below** the Option branch inside `parse_type` in `rwc/parser.py`:

```python
        if t.kind == TokenKind.KW_RESULT:
            self.i += 1
            self.eat(TokenKind.LBRACK, "'[' after Result")
            inner1 = self.cur
            if inner1.kind != TokenKind.KW_INT:
                raise ParserError(
                    "only Result[int, int] is supported in this version of rw",
                    inner1.line, inner1.col, max(1, len(inner1.value)),
                )
            self.i += 1
            self.eat(TokenKind.COMMA, "',' between Result type arguments")
            inner2 = self.cur
            if inner2.kind != TokenKind.KW_INT:
                raise ParserError(
                    "only Result[int, int] is supported in this version of rw",
                    inner2.line, inner2.col, max(1, len(inner2.value)),
                )
            self.i += 1
            self.eat(TokenKind.RBRACK, "']' to close Result[int, int]")
            return A.TypeName("Result[int, int]", t.line, t.col)
```

- [ ] **Step 1.6: Accept `Ok(e)` / `Err(e)` as expressions in `parse_unary`**

**Directly below** the `KW_NONE` branch:

```python
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
```

- [ ] **Step 1.7: Rewrite `parse_match` into the style-unified form**

Replace the **entire** existing `parse_match` method with the following:

```python
    def parse_match(self) -> A.MatchStmt:
        kw = self.eat(TokenKind.KW_MATCH)
        target = self.parse_expr()
        self.eat(TokenKind.COLON, "':' after match target")
        self.eat(TokenKind.NEWLINE)
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
            if ok_block is None or err_block is None or ok_var is None or err_var is None:
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
```

### Sema and irgen: minimal updates to match the new AST shape

- [ ] **Step 1.8: Add `Result[int, int]` to `_resolve_type`**

Add one line to the `m` dict of `_resolve_type` in `rwc/sema.py`:

```python
        m = {
            "int": T.INT,
            "float": T.FLOAT,
            "bool": T.BOOL,
            "string": T.STRING,
            "Bytes": T.BYTES,
            "List[int]": T.LIST_INT,
            "Option[int]": T.OPTION_INT,
            "Result[int, int]": T.RESULT_INT_INT,
            "void": T.VOID,
        }
```

- [ ] **Step 1.9: Minimally update the existing Sema `MatchStmt` branch to support style branching**

Replace the `MatchStmt` branch in `_check_stmt` of `rwc/sema.py` (currently hardcoded to Option-style) with the following. Full `style == "result"` Sema is completed in Task 2; here, add a minimal guard that "runs the existing Option logic only when `style == "option"`" so that the presence of the new AST fields does not break existing tests:

```python
        if isinstance(stmt, A.MatchStmt):
            tt = self._check_expr(fn, stmt.target, locals_)
            if stmt.style == "option":
                if tt is not T.OPTION_INT:
                    raise CompileError(Diagnostic(
                        self.filename, stmt.line, stmt.col, 5,
                        f"match target must be Option[int], found `{tt}`",
                    ))
                some_locals = dict(locals_)
                some_locals[stmt.some_var] = T.INT
                some_ret = self._check_block(fn, stmt.some_block, some_locals, ret_ty)
                none_ret = self._check_block(fn, stmt.none_block, dict(locals_), ret_ty)
                return some_ret and none_ret
            # style == "result" — handled in Task 2; reject for now so
            # accidentally-constructed Result matches fail with a clear
            # message rather than crashing.
            raise CompileError(Diagnostic(
                self.filename, stmt.line, stmt.col, 5,
                "internal: Result-style match not yet implemented in sema",
            ))
```

- [ ] **Step 1.10: Add a similar style guard to the existing irgen `MatchStmt` branch**

Add one line at the start of the `MatchStmt` branch in `_emit_stmt` of `rwc/irgen.py`:

```python
        if isinstance(stmt, A.MatchStmt):
            if stmt.style != "option":
                # style == "result" lowering lands in Task 3.
                raise RuntimeError("internal: Result-style match not yet implemented in irgen")
            v = self._emit_expr(stmt.target, ctx)
            tag = b.extract_value(v, 0)
            # ... existing Option-style lowering, unchanged ...
```

(Leave the current Option-style body as-is, **below** the `if stmt.style != "option": raise ...` guard.)

### Tests for Task 1

- [ ] **Step 1.11: Tests that pass only the type annotation + syntax**

Append to the end of `tests/test_sema.py`:

```python
def test_result_int_int_type_annotation_parses():
    src = (
        "def takes_res(r: Result[int, int]) -> int:\n"
        "    return 0\n"
        "def main() -> int:\n"
        "    return 0\n"
    )
    res = check(src)
    assert "takes_res" in res.functions
    assert res.functions["takes_res"].params[0][1] is T.RESULT_INT_INT


def test_result_with_non_int_param_is_parser_error():
    import pytest
    from rwc.lexer import tokenize
    from rwc.parser import parse, ParserError
    src = (
        "def f(r: Result[string, int]) -> int:\n"
        "    return 0\n"
        "def main() -> int:\n"
        "    return 0\n"
    )
    with pytest.raises(ParserError) as ei:
        parse(tokenize(src))
    assert "Result[int, int]" in str(ei.value) or "only Result" in str(ei.value)


def test_match_with_mixed_arms_is_parser_error():
    import pytest
    from rwc.lexer import tokenize
    from rwc.parser import parse, ParserError
    src = (
        "def main() -> int:\n"
        "    o: Option[int] = None\n"
        "    match o:\n"
        "        case Some(x):\n"
        "            return x\n"
        "        case Err(e):\n"
        "            return e\n"
    )
    with pytest.raises(ParserError) as ei:
        parse(tokenize(src))
    assert "mixed match arms" in str(ei.value)
```

- [ ] **Step 1.12: Run the full pytest suite (including existing tests) and confirm green**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw && uv run pytest -q 2>&1 | tail -5
```

Expected: existing 115 + 3 new in Task 1 = `118 passed`.

The existing Option-style `test_match_*` tests are expected to behave
identically even after the parser/Sema emit the new AST. If they fail,
cross-check the `MatchStmt` field order and Optional settings against Step 1.4.

- [ ] **Step 1.13: Commit**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
git add rwc/types.py rwc/lexer.py rwc/ast_nodes.py rwc/parser.py rwc/sema.py rwc/irgen.py tests/test_sema.py
git commit -m "$(cat <<'EOF'
rwc: introduce Result[int, int] in lexer / parser / ast (parse-only)

Adds the primitive type T.RESULT_INT_INT, three new lexer keywords
(Result / Ok / Err), parser branches for the Result[int, int] type
annotation, Ok(e) / Err(e) expressions, and a major rewrite of
parse_match: the first case arm now locks in a style ("option" or
"result") and subsequent arms must match.

AST gets two new expression nodes (OkExpr, ErrExpr) and MatchStmt
grows a `style` field plus Option-style and Result-style fields
(Optional, exclusive per style).

Sema and irgen recognise the new MatchStmt shape but only handle
style == "option" for now; Result-style matches raise a clear
"not yet implemented" error so the next two commits can land them
in isolation.

Parser-level negatives covered: Result[T, E] for non-int T or E
rejects; mixed arms (Some + Err, Ok + None, etc.) reject with
"mixed match arms".

Existing Option tests stay green: the parser still locks Option
matches with Some / None as before, just via the generalised
case-lookup table.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Complete `OkExpr` / `ErrExpr` / Result-style `MatchStmt` in Sema

In this task Sema learns to understand Result expressions and match. Replace the places that raised `"not yet implemented"` in Task 1 with real implementations.

**Files:**
- Modify: `rwc/sema.py`
- Modify: `tests/test_sema.py`

- [ ] **Step 2.1: Handle `OkExpr` / `ErrExpr` in `_check_expr`**

**Directly below** the existing `SomeExpr` / `NoneExpr` in `_check_expr` of `rwc/sema.py`:

```python
        if isinstance(expr, A.OkExpr):
            at = self._check_expr(fn, expr.arg, locals_)
            if at is not T.INT:
                raise CompileError(Diagnostic(
                    self.filename, expr.line, expr.col, 2,
                    f"Ok argument must be int, found `{at}`",
                ))
            return T.RESULT_INT_INT
        if isinstance(expr, A.ErrExpr):
            at = self._check_expr(fn, expr.arg, locals_)
            if at is not T.INT:
                raise CompileError(Diagnostic(
                    self.filename, expr.line, expr.col, 3,
                    f"Err argument must be int, found `{at}`",
                ))
            return T.RESULT_INT_INT
```

- [ ] **Step 2.2: Replace the Result-style guard of `MatchStmt` in `_check_stmt` with a real implementation**

Replace the place that did `raise "not yet implemented"` in Task 1 with the following:

```python
            if stmt.style == "option":
                # ... existing Option-style body unchanged ...
                some_locals = dict(locals_)
                some_locals[stmt.some_var] = T.INT
                some_ret = self._check_block(fn, stmt.some_block, some_locals, ret_ty)
                none_ret = self._check_block(fn, stmt.none_block, dict(locals_), ret_ty)
                return some_ret and none_ret
            # style == "result"
            if tt is not T.RESULT_INT_INT:
                raise CompileError(Diagnostic(
                    self.filename, stmt.line, stmt.col, 5,
                    f"match target must be Result[int, int], found `{tt}`",
                ))
            ok_locals = dict(locals_)
            ok_locals[stmt.ok_var] = T.INT
            ok_ret = self._check_block(fn, stmt.ok_block, ok_locals, ret_ty)
            err_locals = dict(locals_)
            err_locals[stmt.err_var] = T.INT
            err_ret = self._check_block(fn, stmt.err_block, err_locals, ret_ty)
            return ok_ret and err_ret
```

- [ ] **Step 2.3: Positive tests**

Append to the end of `tests/test_sema.py`:

```python
# ---- Result[int, int] positive cases ----

def test_ok_int_returns_result_int_int():
    src = (
        "def main() -> int:\n"
        "    r: Result[int, int] = Ok(5)\n"
        "    return 0\n"
    )
    check(src)


def test_err_int_returns_result_int_int():
    src = (
        "def main() -> int:\n"
        "    r: Result[int, int] = Err(0)\n"
        "    return 0\n"
    )
    check(src)


def test_function_returning_result_with_both_arms():
    src = (
        "def f(b: int) -> Result[int, int]:\n"
        "    if b == 0:\n"
        "        return Err(0)\n"
        "    return Ok(1)\n"
        "def main() -> int:\n"
        "    return 0\n"
    )
    check(src)


def test_match_result_two_arms_ok():
    src = (
        "def main() -> int:\n"
        "    r: Result[int, int] = Ok(7)\n"
        "    match r:\n"
        "        case Ok(x):\n"
        "            print(x)\n"
        "        case Err(e):\n"
        "            print(e)\n"
        "    return 0\n"
    )
    check(src)


def test_match_result_bound_vars_are_int():
    src = (
        "def main() -> int:\n"
        "    r: Result[int, int] = Ok(7)\n"
        "    match r:\n"
        "        case Ok(x):\n"
        "            y: int = x + 1\n"
        "            print(y)\n"
        "        case Err(e):\n"
        "            z: int = e * 2\n"
        "            print(z)\n"
        "    return 0\n"
    )
    check(src)


def test_match_result_terminates_via_both_arms_return():
    src = (
        "def pick(b: int) -> int:\n"
        "    r: Result[int, int] = Err(0)\n"
        "    if b == 0:\n"
        "        r = Err(0)\n"
        "    else:\n"
        "        r = Ok(b)\n"
        "    match r:\n"
        "        case Ok(x):\n"
        "            return x\n"
        "        case Err(e):\n"
        "            return e\n"
        "def main() -> int:\n"
        "    return pick(7)\n"
    )
    check(src)
```

- [ ] **Step 2.4: Negative tests**

Append further to `tests/test_sema.py`:

```python
# ---- Result[int, int] negative cases ----

def test_ok_string_is_type_error():
    src = (
        "def main() -> int:\n"
        "    r: Result[int, int] = Ok(\"hi\")\n"
        "    return 0\n"
    )
    e = err(src)
    assert "Ok argument must be int" in e.diagnostic.message


def test_err_string_is_type_error():
    src = (
        "def main() -> int:\n"
        "    r: Result[int, int] = Err(\"hi\")\n"
        "    return 0\n"
    )
    e = err(src)
    assert "Err argument must be int" in e.diagnostic.message


def test_match_result_on_int_is_type_error():
    src = (
        "def main() -> int:\n"
        "    x: int = 5\n"
        "    match x:\n"
        "        case Ok(v):\n"
        "            print(v)\n"
        "        case Err(e):\n"
        "            print(e)\n"
        "    return 0\n"
    )
    e = err(src)
    assert "match target must be Result[int, int]" in e.diagnostic.message


def test_print_result_is_type_error():
    src = (
        "def main() -> int:\n"
        "    r: Result[int, int] = Ok(1)\n"
        "    print(r)\n"
        "    return 0\n"
    )
    e = err(src)
    assert "print" in e.diagnostic.message


def test_result_eq_is_type_error():
    src = (
        "def main() -> int:\n"
        "    a: Result[int, int] = Ok(1)\n"
        "    b: Result[int, int] = Err(0)\n"
        "    if a == b:\n"
        "        return 0\n"
        "    return 1\n"
    )
    e = err(src)
    assert "compare" in e.diagnostic.message or "==" in e.diagnostic.message


def test_ok_eq_some_is_type_error():
    src = (
        "def main() -> int:\n"
        "    a: Result[int, int] = Ok(1)\n"
        "    b: Option[int] = Some(1)\n"
        "    if a == b:\n"
        "        return 0\n"
        "    return 1\n"
    )
    e = err(src)
    assert "same type" in e.diagnostic.message
```

- [ ] **Step 2.5: Run pytest**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw && uv run pytest tests/test_sema.py -q 2>&1 | tail -5
```

Expected: existing 64 + 3 added in Task 1 + 6 positive here + 6 negative = `79 passed`.

Since irgen does not yet handle Result-style match, trying to run
`examples/result_basic.rw` will fail (Step 1.10 raises `"not yet implemented"`).
At the Sema level everything passes cleanly.

- [ ] **Step 2.6: Full pytest**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw && uv run pytest -q 2>&1 | tail -5
```

Expected: existing 115 + 3 in Task 1 + 12 in Task 2 = `130 passed`.

- [ ] **Step 2.7: Commit**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
git add rwc/sema.py tests/test_sema.py
git commit -m "$(cat <<'EOF'
rwc: sema for Ok / Err / Result-style match

Sema:
  - OkExpr / ErrExpr require their arg to be int, both return
    Result[int, int].
  - MatchStmt with style == "result" requires target to be
    Result[int, int], binds Ok(x) and Err(e) patterns' identifiers
    as int in their respective arm scopes, and treats the whole
    statement as "terminates in return" iff both arms terminate
    (same pattern as Option's match).
  - Result[int, int] is deliberately absent from the == whitelist,
    so a == b comparisons get caught at sema.
  - Mixed Ok == Some style comparisons get caught earlier by the
    existing "same type" check (covered as a bonus negative).

Tests: 12 new in test_sema.py covering Ok/Err typing, both-arms
match, bound-as-int (both Ok and Err arms), return coverage via
Result match, and six negatives (Ok("hi"), Err("hi"), match on
int, print(Result), Result == , Ok == Some).

irgen still doesn't know how to emit Result-style match — code
that actually executes those forms will still crash with the
"not yet implemented" error from Task 1 until the next commit.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Implement `Ok` / `Err` / Result-style `MatchStmt` in irgen

In this task rw code actually handles Result and runs. Remove the irgen
guard put in place in Task 1 (`if stmt.style != "option": raise`) and
implement the Option / Result lowering via the shared `_emit_arm` helper.

**Files:**
- Modify: `rwc/irgen.py`

- [ ] **Step 3.1: Add `RW_RESULT_INT_INT_TY` to irgen**

At the top of `rwc/irgen.py`, **directly below** `RW_OPTION_INT_TY = ...`:

```python
RW_RESULT_INT_INT_TY = ir.LiteralStructType([I64, I64])  # {tag, payload}
```

- [ ] **Step 3.2: Add Result to `llvm_type_of`**

**Directly below** `if t is T.OPTION_INT:`:

```python
    if t is T.RESULT_INT_INT:
        return RW_RESULT_INT_INT_TY
```

- [ ] **Step 3.3: Handle `OkExpr` / `ErrExpr` in `_emit_expr`**

**Directly below** the existing `SomeExpr` / `NoneExpr` branch:

```python
        if isinstance(expr, A.OkExpr):
            v = self._emit_expr(expr.arg, ctx)
            base = ir.Constant(RW_RESULT_INT_INT_TY,
                               [ir.Constant(I64, 1), ir.Constant(I64, 0)])
            return ctx.builder.insert_value(base, v, 1)
        if isinstance(expr, A.ErrExpr):
            v = self._emit_expr(expr.arg, ctx)
            base = ir.Constant(RW_RESULT_INT_INT_TY,
                               [ir.Constant(I64, 0), ir.Constant(I64, 0)])
            return ctx.builder.insert_value(base, v, 1)
```

- [ ] **Step 3.4: Add the shared `_emit_arm` helper**

Add the method **directly above** the existing `_emit_match` (or before `_emit_stmt` if there is none):

```python
    def _emit_arm(self, var_name, block, payload, ctx, end_bb):
        """Emit one match arm. var_name=None means no payload binding."""
        b = ctx.builder
        if var_name is not None:
            slot = b.alloca(I64, name=var_name)
            b.store(payload, slot)
            saved = ctx.locals.get(var_name)
            ctx.locals[var_name] = slot
            self._emit_block(block, ctx)
            if saved is not None:
                ctx.locals[var_name] = saved
            else:
                ctx.locals.pop(var_name, None)
        else:
            self._emit_block(block, ctx)
        if not b.block.is_terminated:
            b.branch(end_bb)
```

- [ ] **Step 3.5: Rewrite `MatchStmt` in `_emit_stmt` with style-specific lowering**

**Remove** the `if stmt.style != "option": raise ...` guard added in Task 1
and replace it, including the existing Option-style body, with the following
(you may factor it out into a method named `_emit_match`, but keep it inline here):

```python
        if isinstance(stmt, A.MatchStmt):
            v = self._emit_expr(stmt.target, ctx)
            tag = b.extract_value(v, 0)
            payload = b.extract_value(v, 1)
            arm1_bb = ctx.function.append_basic_block("match.arm1")
            arm0_bb = ctx.function.append_basic_block("match.arm0")
            end_bb = ctx.function.append_basic_block("match.end")
            # tag == 1 -> arm1, default (tag == 0) -> arm0.
            sw = b.switch(tag, arm0_bb)
            sw.add_case(ir.Constant(I64, 1), arm1_bb)
            if stmt.style == "option":
                var1, block1 = stmt.some_var, stmt.some_block   # Some(payload)
                var0, block0 = None, stmt.none_block            # None (no bind)
            else:  # "result"
                var1, block1 = stmt.ok_var, stmt.ok_block       # Ok(payload)
                var0, block0 = stmt.err_var, stmt.err_block     # Err(payload)
            # arm1 (tag == 1)
            b.position_at_end(arm1_bb)
            self._emit_arm(var1, block1, payload, ctx, end_bb)
            # arm0 (tag == 0)
            b.position_at_end(arm0_bb)
            self._emit_arm(var0, block0, payload, ctx, end_bb)
            b.position_at_end(end_bb)
            return
```

- [ ] **Step 3.6: Smoke check**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
cat > /tmp/result_smoke.rw <<'EOF'
def div_checked(a: int, b: int) -> Result[int, int]:
    if b == 0:
        return Err(0)
    return Ok(a / b)

def main() -> int:
    r: Result[int, int] = div_checked(10, 2)
    match r:
        case Ok(x):
            print(x)
        case Err(e):
            print(-1)
    e: Result[int, int] = div_checked(10, 0)
    match e:
        case Ok(x):
            print(x)
        case Err(code):
            print(code)
    return 0
EOF
uv run rwc emit-ir /tmp/result_smoke.rw 2>&1 | grep -E "switch|insertvalue" | head -10
echo "---"
uv run rwc run /tmp/result_smoke.rw
```

Expected:

The IR contains both `switch i64` and `insertvalue` (both the constant Err base `{i64 0, i64 0}` and the constant Ok base `{i64 1, i64 0}` appear).

Execution result:
```
5
0
```

- [ ] **Step 3.7: No regression in the existing Option smoke test**

```sh
RW_WORKERS=1 uv run rwc run examples/option_basic.rw
```

Expected:
```
5
-1
```

Since Option-style is also rewritten in this commit via `_emit_arm`, the regression check is mandatory.

- [ ] **Step 3.8: Full pytest**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw && uv run pytest -q 2>&1 | tail -5
```

Expected: `130 passed` (same count as Task 2; no tests added in this task).

- [ ] **Step 3.9: Commit**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
git add rwc/irgen.py
git commit -m "$(cat <<'EOF'
rwc: irgen for Ok / Err / Result-style match

irgen:
  - RW_RESULT_INT_INT_TY = {i64 tag, i64 payload} (same shape as
    RW_OPTION_INT_TY but kept as a named alias for readability).
  - llvm_type_of(T.RESULT_INT_INT) -> RW_RESULT_INT_INT_TY.
  - OkExpr: insertvalue {1, 0} (tag = 1) with the arg into payload.
  - ErrExpr: insertvalue {0, 0} (tag = 0) with the arg into payload.
  - MatchStmt: factored out an _emit_arm helper that handles
    "allocate a slot for the bound var (if any), store the payload,
    emit the block, branch to end_bb if not terminated". Both
    Option (Some/None — None has no bound var) and Result (Ok/Err
    — both have bound vars) lowering call into it.
  - The "not yet implemented" guards from Task 1 are removed.

Smoke:
  uv run rwc run /tmp/result_smoke.rw
  -> 5\n0\n
  uv run rwc run examples/option_basic.rw
  -> 5\n-1\n (Option regression check)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: example + e2e

**Files:**
- Create: `examples/result_basic.rw`
- Create: `examples/result_basic.rw.expected`
- Modify: `tests/test_e2e.py`

- [ ] **Step 4.1: Write `examples/result_basic.rw`**

```rw
def div_checked(a: int, b: int) -> Result[int, int]:
    if b == 0:
        return Err(0)
    return Ok(a / b)

def main() -> int:
    r: Result[int, int] = div_checked(10, 2)
    match r:
        case Ok(x):
            print(x)
        case Err(e):
            print(-1)
    e: Result[int, int] = div_checked(10, 0)
    match e:
        case Ok(x):
            print(x)
        case Err(code):
            print(code)
    return 0
```

- [ ] **Step 4.2: `examples/result_basic.rw.expected`**

```
5
0
```

(Save with a trailing newline.)

- [ ] **Step 4.3: Confirm byte-for-byte match locally**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
diff <(RW_WORKERS=1 uv run rwc run examples/result_basic.rw 2>&1) examples/result_basic.rw.expected && echo OK
```

Expected: only `OK` is printed.

- [ ] **Step 4.4: Add `result_basic` to the parametrize in `tests/test_e2e.py`**

The following line at `tests/test_e2e.py:45`:

```python
    ["hello", "arith", "fib", "while_count", "spawn_basic", "spawn_many", "spawn_string", "string_ops", "bytes_basic", "list_basic", "option_basic"],
```

changes to the following:

```python
    ["hello", "arith", "fib", "while_count", "spawn_basic", "spawn_many", "spawn_string", "string_ops", "bytes_basic", "list_basic", "option_basic", "result_basic"],
```

- [ ] **Step 4.5: Full pytest**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw && uv run pytest -q 2>&1 | tail -5
```

Expected: 130 + 1 = `131 passed`.

- [ ] **Step 4.6: Existing example regression**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
RW_WORKERS=1 uv run rwc run examples/option_basic.rw
RW_WORKERS=1 uv run rwc run examples/list_basic.rw
RW_WORKERS=1 uv run rwc run examples/string_ops.rw
RW_WORKERS=1 uv run rwc run examples/bytes_basic.rw
RW_WORKERS=1 uv run rwc run examples/spawn_many.rw
```

Expected:
```
5
-1
---
3
10
30
---
hello, world
12
eq ok
neq ok
---
5
eq ok
hello
---
30
```

- [ ] **Step 4.7: Confirm the runtime unit tests are green too (no runtime changes, but just in case)**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw/runtime
make clean && make
cc -O2 -Wall -Wextra -std=c11 -D_GNU_SOURCE -pthread fiber/test_option.c librw.a -o fiber/test_option && ./fiber/test_option
cc -O2 -Wall -Wextra -std=c11 -D_GNU_SOURCE -pthread fiber/test_sched.c librw.a -o fiber/test_sched && ./fiber/test_sched
```

Expected: `all option tests passed` / `total = 333833500`。

- [ ] **Step 4.8: Commit**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
git add examples/result_basic.rw examples/result_basic.rw.expected tests/test_e2e.py
git commit -m "$(cat <<'EOF'
examples: add result_basic exercising Ok / Err / match Result-style

examples/result_basic.rw exercises the full Result[int, int]
surface: a div_checked helper that returns Ok(quotient) when b != 0
and Err(0) when b == 0, plus two match statements that print the
value (Ok arm) or the error code (Err arm).

The .expected captures the byte-for-byte stdout (5 then 0), and
tests/test_e2e.py picks it up via the existing parametrize list,
so any regression in lexer / parser / sema / irgen flows that
touch Ok / Err / Result-style match will fail the suite.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review

### Spec coverage

| Spec requirement | Covering tasks |
|---|---|
| New type `Result[int, int]` (keyword `Result`, parser enforces `[int, int]`) | Task 1.1 / 1.2 / 1.5 / 1.8 |
| `Ok(int) -> Result[int, int]` / `Err(int) -> Result[int, int]` expressions | Task 1.3 (AST) / 1.6 (parser) / 2.1 (sema) / 3.3 (irgen) |
| match supports two styles, Option / Result | Task 1.4 (AST style field) / 1.7 (parse_match rewrite) / 1.9 (sema branching) / 2.2 (Result-style sema) / 3.5 (irgen branching) |
| Disallow mixing Some/None and Ok/Err (parser) | Task 1.7 (`mixed match arms`) + test 1.11 |
| Ok/Err bound variables in Result-style match are int | Task 2.2 (sema) + test 2.3 |
| return-coverage considers both arms of a Result | Task 2.2 (`ok_ret and err_ret`) + test 2.3 |
| Internal representation returned as a 16-byte value | Task 3.1 (`RW_RESULT_INT_INT_TY = LiteralStructType([I64, I64])`) |
| `Result[string, int]` and similar are parser errors | Task 1.5 + test 1.11 |
| A missing arm is a parser error (both styles) | Task 1.7 + test 1.11 (mixed arms and a missing arm are distinct errors, both covered by the parser) |
| `print(Result[int, int])` is a type error | `is_printable` unchanged + test 2.4 |
| `Result == Result` is a type error | == whitelist unchanged + test 2.4 |
| `Ok(string)` / `Err(string)` are type errors | Task 2.1 + test 2.4 |
| match target that is not Result[int, int] is a type error | Task 2.2 + test 2.4 |
| `Future[Result[int, int]]` disallowed | Automatically disallowed by not adding it to `_decl_spawn`/`_decl_await` (same decision as Option) |
| Existing 115 tests green | Task 1.12, 2.6, 3.8, 4.5 |
| No behavior regression in Option-style match | Preserve the `style == "option"` path in Task 1.7 + the post-`_emit_arm`-unification smoke test in Task 3.5 (Step 3.7) + Task 4.6 |

Every spec requirement has a task.

### Placeholder scan

Zero occurrences of "TBD", "TODO", "(to be confirmed)", "fill in", "Add appropriate", or "Similar to Task N" in the plan. The `raise "not yet implemented"` guards (Task 1.9 / 1.10) are an **intentional intermediate state**, and it is explicitly stated that they are removed in Task 2 / Task 3. They are not placeholders.

### Type consistency

- `T.RESULT_INT_INT` matches exactly across Task 1.1 / 1.8 / 2.1 / 2.2 / 3.2 / 3.3
- The LLVM representation `RW_RESULT_INT_INT_TY = LiteralStructType([I64, I64])` is consistent across Task 3.1 / 3.2 / 3.3
- New AST nodes: `OkExpr` / `ErrExpr` are used consistently in Task 1.3 (definition) / 1.6 (parser) / 2.1 (sema) / 3.3 (irgen)
- New `MatchStmt` fields: `style` / `ok_var` / `ok_block` / `err_var` / `err_block` are used consistently in Task 1.4 (definition) / 1.7 (parser) / 1.9 + 2.2 (sema) / 3.5 (irgen)
- Sema type-error messages: `"Ok argument must be int"` / `"Err argument must be int"` / `"match target must be Result[int, int]"` match exactly between the implementation (Task 2.1, 2.2) and the negative-test asserts (Task 2.4)
- The argument order of `_emit_arm`, `(var_name, block, payload, ctx, end_bb)`, matches between Task 3.4 (definition) / 3.5 (call)
- The existing Option-style field names `some_var` / `some_block` / `none_block` are consistently retained in Task 1.4 (AST redefinition) / 1.7 (parser) / 1.9 (sema) / 3.5 (irgen)
