# rw Result[int, int] type + Ok/Err extension of match

## Context

With `Option[int]` (#94), the foundation of the language feature **tagged union
+ pattern matching** is in place. Result is the other sum type that should come
next, and it becomes the standard form for expressing the typical "success, or
failure with a reason for the error."

If we strictly apply the `incremental-language-extensions` skill's iron rule
"generics when you need them the second time," then Option (first time) + Result
(second time) could also be **the timing to introduce true generics**. However,
going truly generic includes a large overhaul that routes `parse_match` through
sema, which goes beyond the scope of a single PR.

So this sub-project **still keeps things monomorphic** while extending only the
match parser to determine, from its first arm, whether it is a "Some/None pair"
or an "Ok/Err pair." Going truly generic is done in a separate PR (4c).

Roadmap:

1. String `len` / `==` / `+` (#91)
2. Bytes type (#92)
3. List[int] (#93)
4a. Option[int] + match (#94)
4b. **this sub-project**: Result[int, int] + match Ok/Err
4c. (future) true generics
5. (future) netpoller + TCP API

## Goals

- Introduce a new primitive type `Result[int, int]` (the parser rejects
  anything other than `[int, int]`)
- Value construction: `Ok(int) -> Result[int, int]`, `Err(int) -> Result[int, int]`
- Value deconstruction: extend the existing `match` statement to a 2-arm form
  that is either a "Some/None pair" or an "Ok/Err pair" (mixing the pairs is a
  parser error)
- Add a `style: "option" | "result"` field to the `MatchStmt` AST, and have
  Sema / irgen branch by style
- The existing public ABI stays unchanged, existing tests green

## Non-Goals

- Full generics (T/E of `Result[T, E]` other than int): a "only
  `Result[int, int]` is supported" error in the parser
- Going truly generic with `Result[T, E]` (separate PR, 4c)
- Using match as an expression (`x = match v: ...`): match is a statement only
- Nested patterns (`Ok(Some(x))`), guards (`case Ok(x) if x > 0`), wildcards
  (`case _`)
- The `?` operator (early-return)
- match arm order: since Some/None remain order-independent, Ok/Err are also
  order-independent
- `Future[Result[int, int]]` (via spawn): forbidden in Sema
- Built-in methods for `Result` (`unwrap`, `is_ok`, `or_else`)
- Conversion built-ins between `Option[int]` <-> `Result[int, int]`
- `print(r: Result[int, int])` (not added to the printable list)
- `r1 == r2` (not added to the == whitelist)
- Built-in helpers such as `div_checked` (a function that returns Result **can
  be written as user code on the rw language side**, so we get by writing it in
  an example)

## Design

### Internal representation

`Result[int, int]` is represented by the **same 2-word fat struct as
`Option[int]`**:

```c
typedef struct {
    int64_t tag;       /* 0 = Err, 1 = Ok */
    int64_t payload;   /* the success value when Ok, the error value when Err */
} rw_result_int_int;
```

LLVM IR:

```
%rw_result_int_int = { i64 tag, i64 payload }
```

Size 16 bytes → it can be returned in 2 registers on both arm64 and x86_64 SysV
(no pointer-out ABI needed; see the `llvm-ir-c-abi` skill).

In fact **the LLVM struct shape is exactly identical to Option[int]**, but at
the Sema level we distinguish them as different types (`T.OPTION_INT` vs
`T.RESULT_INT_INT`). This is the same pattern as Bytes and string sharing the
same `RW_STR_TY`.

Meaning of the tag values:

| tag | meaning |
|---|---|
| 0 | Err (failure) |
| 1 | Ok (success) |

Correspondence with Option:
- `Option.None` (tag=0) ↔ `Result.Err` (tag=0)
- `Option.Some` (tag=1) ↔ `Result.Ok` (tag=1)

This leaves room to make a future Option → Result cast 0-cost, but this PR does
not implement the cast (Non-Goal).

### Language-level behavior

#### Value construction and deconstruction

```rw
def div_checked(a: int, b: int) -> Result[int, int]:
    if b == 0:
        return Err(0)
    return Ok(a / b)

def main() -> int:
    r: Result[int, int] = div_checked(10, 2)
    match r:
        case Ok(x):
            print(x)              # => 5
        case Err(e):
            print(-1)

    e: Result[int, int] = div_checked(10, 0)
    match e:
        case Ok(x):
            print(x)
        case Err(code):
            print(code)           # => 0
    return 0
```

#### Behavior of the match parser

`parse_match` looks at the first arm and **decides the style**:

- first is `case Some(x):` or `case None:` → Option-style
- first is `case Ok(x):` or `case Err(e):` → Result-style

The second arm is required to be the other one of the same style:
- Option-style with first Some → second requires None
- Option-style with first None → second requires Some
- Result-style with first Ok → second requires Err
- Result-style with first Err → second requires Ok

Mixing (`case Some(x): ... case Err(e):`) is rejected as a parser error with an
explicit message like "expected `case None` (Option-style match)".

#### Cases that become errors

```rw
def main() -> int:
    # parser: only Result[int, int] supported
    a: Result[string, int] = Ok(1)
    # sema: Ok argument must be int
    b: Result[int, int] = Ok("hi")
    # parser: mixed arms (Result + Option)
    c: Result[int, int] = Ok(1)
    match c:
        case Ok(x):
            print(x)
        case None:
            print(0)
    # sema: cannot compare `Result[int, int]`
    if c == Err(0):
        return 0
    # sema: print does not support `Result[int, int]`
    print(c)
    # sema: cannot spawn the builtin / Future[Result[int, int]] forbidden
    return 0
```

### Changes by component

#### Runtime

**No change**. `Result[int, int]` completes inside LLVM IR (`insertvalue` +
constant struct). `div_checked` is written as user code in an example; it is not
a built-in.

#### `rwc/types.py`

```python
RESULT_INT_INT = _Primitive("Result[int, int]")
```

Do not include it in `is_printable` / `is_numeric`.

#### `rwc/lexer.py`

3 new keywords:

```python
KW_RESULT = auto()
KW_OK     = auto()
KW_ERR    = auto()
```

```python
KEYWORDS = {
    ...,
    "Result": KW_RESULT,
    "Ok":     KW_OK,
    "Err":    KW_ERR,
}
```

#### `rwc/parser.py`

##### Accept `Result[int, int]` in `parse_type`

Add next to the `Option` branch:

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

##### Accept `Ok(e)` / `Err(e)` in the expression parser

Add directly below `KW_SOME` / `KW_NONE` in `parse_unary`:

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

##### Extending `parse_match`

Replace the current `parse_match` with the following structure:

```python
def parse_match(self) -> A.MatchStmt:
    kw = self.eat(TokenKind.KW_MATCH)
    target = self.parse_expr()
    self.eat(TokenKind.COLON, "':' after match target")
    self.eat(TokenKind.NEWLINE)
    self.eat(TokenKind.INDENT, "indented match body")

    style: Optional[str] = None  # "option" or "result"
    some_var = some_block = none_block = None
    ok_var = ok_block = err_var = err_block = None

    while self.cur.kind != TokenKind.DEDENT:
        if self.cur.kind == TokenKind.NEWLINE:
            self.i += 1; continue
        if self.cur.kind != TokenKind.KW_CASE:
            raise ParserError("expected `case` arm in match body", ...)
        self.eat(TokenKind.KW_CASE)
        arm_tok = self.cur

        if arm_tok.kind in (TokenKind.KW_SOME, TokenKind.KW_NONE):
            this_style = "option"
        elif arm_tok.kind in (TokenKind.KW_OK, TokenKind.KW_ERR):
            this_style = "result"
        else:
            raise ParserError("match case must be Some/None or Ok/Err", ...)

        if style is None:
            style = this_style
        elif style != this_style:
            expected = "Some/None" if style == "option" else "Ok/Err"
            raise ParserError(
                f"mixed match arms: expected {expected} pair, got `{arm_tok.value}`",
                arm_tok.line, arm_tok.col, max(1, len(arm_tok.value)),
            )

        # Dispatch on the four constructor cases:
        if arm_tok.kind == TokenKind.KW_SOME:
            # duplicate-check, parse Some(IDENT), parse block, set some_var/some_block
            ...
        elif arm_tok.kind == TokenKind.KW_NONE:
            # parse None, parse block, set none_block
            ...
        elif arm_tok.kind == TokenKind.KW_OK:
            # parse Ok(IDENT), parse block, set ok_var/ok_block
            ...
        elif arm_tok.kind == TokenKind.KW_ERR:
            # parse Err(IDENT), parse block, set err_var/err_block
            ...

    self.eat(TokenKind.DEDENT)

    if style == "option":
        if some_block is None or none_block is None:
            raise ParserError("match on Option[int] must cover both Some and None", ...)
    elif style == "result":
        if ok_block is None or err_block is None:
            raise ParserError("match on Result[int, int] must cover both Ok and Err", ...)
    else:
        raise ParserError("match must have at least one case arm", ...)

    return A.MatchStmt(
        target, style,
        some_var, some_block, none_block,
        ok_var, ok_block, err_var, err_block,
        kw.line, kw.col,
    )
```

#### `rwc/ast_nodes.py`

New expression nodes:

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

Add to the `Expr` Union.

Extend `MatchStmt` to a style-unified form:

```python
@dataclass
class MatchStmt:
    target: Expr
    style: str                                  # "option" or "result"
    # Option-style fields
    some_var: Optional[str]
    some_block: Optional[List["Stmt"]]
    none_block: Optional[List["Stmt"]]
    # Result-style fields
    ok_var: Optional[str]
    ok_block: Optional[List["Stmt"]]
    err_var: Optional[str]
    err_block: Optional[List["Stmt"]]
    line: int
    col: int
```

An exclusive relationship in which either the Option-style fields are filled or
the Result-style fields are filled, depending on the style.

#### `rwc/sema.py`

Add `"Result[int, int]": T.RESULT_INT_INT` to the dict in `_resolve_type`.

Handle `OkExpr` / `ErrExpr` in `_check_expr`:

```python
if isinstance(expr, A.OkExpr):
    at = self._check_expr(fn, expr.arg, locals_)
    if at is not T.INT:
        raise CompileError("Ok argument must be int")
    return T.RESULT_INT_INT
if isinstance(expr, A.ErrExpr):
    at = self._check_expr(fn, expr.arg, locals_)
    if at is not T.INT:
        raise CompileError("Err argument must be int")
    return T.RESULT_INT_INT
```

Branch the `MatchStmt` in `_check_stmt` by style:

```python
if isinstance(stmt, A.MatchStmt):
    tt = self._check_expr(fn, stmt.target, locals_)
    if stmt.style == "option":
        if tt is not T.OPTION_INT:
            raise CompileError("match target must be Option[int]")
        some_locals = dict(locals_)
        some_locals[stmt.some_var] = T.INT
        some_ret = self._check_block(fn, stmt.some_block, some_locals, ret_ty)
        none_ret = self._check_block(fn, stmt.none_block, dict(locals_), ret_ty)
        return some_ret and none_ret
    elif stmt.style == "result":
        if tt is not T.RESULT_INT_INT:
            raise CompileError("match target must be Result[int, int]")
        ok_locals = dict(locals_)
        ok_locals[stmt.ok_var] = T.INT
        ok_ret = self._check_block(fn, stmt.ok_block, ok_locals, ret_ty)
        err_locals = dict(locals_)
        err_locals[stmt.err_var] = T.INT
        err_ret = self._check_block(fn, stmt.err_block, err_locals, ret_ty)
        return ok_ret and err_ret
```

Do not add `T.RESULT_INT_INT` to the `==` whitelist (= comparison not allowed,
existing pattern). No addition is needed for the `Spawn` forbidden list (`Ok` /
`Err` are not built-ins but expression nodes, so a syntax like `spawn Ok(1)` is
interpreted by the parser as an OkExpr rather than a Call, and is automatically
rejected by the `Call` constraint of SpawnExpr).

#### `rwc/irgen.py`

```python
RW_RESULT_INT_INT_TY = ir.LiteralStructType([I64, I64])
```

(Same shape as `RW_OPTION_INT_TY`, but a separate alias for readability.)

`llvm_type_of(T.RESULT_INT_INT) -> RW_RESULT_INT_INT_TY`

`_emit_expr`:

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

`MatchStmt` in `_emit_stmt`:

```python
if isinstance(stmt, A.MatchStmt):
    v = self._emit_expr(stmt.target, ctx)
    tag = b.extract_value(v, 0)
    payload = b.extract_value(v, 1)
    arm1_bb = ctx.function.append_basic_block("match.arm1")  # tag == 1
    arm0_bb = ctx.function.append_basic_block("match.arm0")  # tag == 0 (default)
    end_bb = ctx.function.append_basic_block("match.end")
    sw = b.switch(tag, arm0_bb)
    sw.add_case(ir.Constant(I64, 1), arm1_bb)
    if stmt.style == "option":
        var1, block1 = stmt.some_var, stmt.some_block   # tag=1 -> Some
        var0, block0 = None, stmt.none_block            # tag=0 -> None (no bind)
    else:  # "result"
        var1, block1 = stmt.ok_var, stmt.ok_block       # tag=1 -> Ok
        var0, block0 = stmt.err_var, stmt.err_block     # tag=0 -> Err
    # arm1 (tag == 1)
    b.position_at_end(arm1_bb)
    self._emit_arm(var1, block1, payload, ctx, end_bb)
    # arm0 (tag == 0)
    b.position_at_end(arm0_bb)
    self._emit_arm(var0, block0, payload, ctx, end_bb)
    b.position_at_end(end_bb)
    return
```

`_emit_arm` is a helper that stores the payload into the bound variable's slot,
emits the block, and branches to end_bb (a case with no bound variable, like
Option's None arm, is branched on `var is None`):

```python
def _emit_arm(self, var, block, payload, ctx, end_bb):
    b = ctx.builder
    if var is not None:
        slot = b.alloca(I64, name=var)
        b.store(payload, slot)
        saved = ctx.locals.get(var)
        ctx.locals[var] = slot
        self._emit_block(block, ctx)
        if saved is not None:
            ctx.locals[var] = saved
        else:
            ctx.locals.pop(var, None)
    else:
        self._emit_block(block, ctx)
    if not b.block.is_terminated:
        b.branch(end_bb)
```

With this, both the Option and Result match share the same lowering logic.

### Tests

#### `tests/test_sema.py` (positive 6 + negative 6)

Positive:
- `Ok(5)` is `Result[int, int]`
- `Err(0)` is `Result[int, int]`
- `def f() -> Result[int, int]: return Ok(1)` passes
- A form with both Ok/Err arms of `match` present passes
- The bound `x` of the Ok arm is usable as int, and the bound `e` of the Err arm
  is usable as int too
- The Result match satisfies return-coverage (both arms return → the whole match
  returns)

Negative:
- `Ok("hi")` → argument type error
- `Result[string, int]` → parser error
- Mixing Some + Err in `match` → parser error
- First Ok / second None in `match` → parser error
- `print(Ok(1))` → printable error
- `Ok(1) == Err(0)` → comparison-not-allowed error
- `Ok(1) == Some(1)` → a different error since they are not the same type (bonus)

#### `examples/result_basic.rw` + `.expected`

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

Expected output:

```
5
0
```

## Changes by file

### Changed

- `rwc/types.py` — the `RESULT_INT_INT` primitive
- `rwc/lexer.py` — the `KW_RESULT` / `KW_OK` / `KW_ERR` keywords
- `rwc/parser.py` — the Result branch in `parse_type`, the `Ok(e)`/`Err(e)`
  expressions, extending `parse_match` to a style-unified form
- `rwc/ast_nodes.py` — add `OkExpr` / `ErrExpr` to the Expr Union, extend
  `MatchStmt` to a style-unified form
- `rwc/sema.py` — `_resolve_type` / Sema for `OkExpr`/`ErrExpr` / per-style Sema
  for `MatchStmt`
- `rwc/irgen.py` — `RW_RESULT_INT_INT_TY` / irgen for `Ok`/`Err` / branch
  `_emit_match` by style (shared helper `_emit_arm`)
- `tests/test_sema.py` — positive 6 + negative 7
- `tests/test_e2e.py` — `result_basic` in the parametrize

### New

- `examples/result_basic.rw` + `.expected`
- `docs/specs/11-result-type.md` (this file)
- `docs/plans/2026-05-23-result-type.md` (created with writing-plans)

### Unchanged

- runtime (`runtime/*`)
- fiber scheduler
- existing spec docs

## Verification

```sh
# pytest
uv run pytest -q
# expected: existing 115 + sema positive 6 + negative 7 + e2e 1 = 129 all green

# standalone run
uv run rwc run examples/result_basic.rw
# expected output: 5\n0\n

# existing example regression
uv run rwc run examples/option_basic.rw
uv run rwc run examples/list_basic.rw
uv run rwc run examples/string_ops.rw
uv run rwc run examples/spawn_many.rw

# runtime unit tests are untouched since there is no runtime change (regression only)
make -C runtime clean && make -C runtime
```

## Commit structure

4 commits:

1. **rwc (lexer/parser/types/ast)**: `KW_RESULT/OK/ERR`, the Result branch in
   `parse_type`, the `Ok(e)`/`Err(e)` expressions, the `OkExpr`/`ErrExpr` AST
   nodes, extending `MatchStmt` to a style-unified form, a full rewrite of
   `parse_match`, `T.RESULT_INT_INT`, `_resolve_type`. Only the type annotation
   and AST pass
2. **rwc (sema)**: Sema for `OkExpr`/`ErrExpr`, per-style Sema for `MatchStmt`,
   confirming the existing Option path is unaffected, negative tests in one batch
3. **rwc (irgen)**: `RW_RESULT_INT_INT_TY`, irgen for `Ok`/`Err`, branch
   `_emit_match` per style via `_emit_arm`, smoke verification
4. **examples + e2e**: add `result_basic.rw`, update the parametrize in
   `tests/test_e2e.py`

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| The AST extension of `MatchStmt` breaks existing Option-style code | No default is needed for the `style` field (the parser always sets it). Since the existing `examples/option_basic.rw` is kept in e2e, regressions are detectable |
| The full rewrite of `parse_match` drops existing tests | Confirm step by step that the Option-style behavior is preserved so that existing tests (`test_match_two_arms_ok` etc.) pass without changes |
| Preserving the duplicate detection inside the match parser (the same arm twice) | Do not put a duplicate-check TODO into the `parse_match` skeleton in the spec; follow the existing pattern like `if some_block is not None: raise duplicate error` in the code |
| Cases where `Ok`/`Err` are used as variable names in user code | Confirmed with `grep -rE '\b(Ok\|Err)\b' examples/ tests/*.rw` (no hits). Introduce them as new keywords, the same as Some/None |
| Since the Option and Result LLVM structs have the same shape, an irgen bug could confuse the types | Sema always rejects first + the named alias `RW_RESULT_INT_INT_TY` ensures readability. There is no actual harm, but "a separate alias because confusion is scary" is reasonable |
| Whether a path toward true generics is visible | The `style` field of `MatchStmt` is easy to replace with a "constructor name list" when generalizing. The style branch in Sema can be turned into a type dispatch table. There are few obstacles to the generalization in 4c |
