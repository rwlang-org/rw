# rw Option[int] type + match syntax (monomorphic minimal form)

## Context

Primitives put into the rw language so far:

- `string`'s `len` / `==` / `+` (#91)
- `Bytes` (#92)
- `List[int]` (#93)

The last wall we hit with `List[int]` was that **out-of-range access in
`list_at(l, i)` cannot be expressed in the language**. Currently the only
option is to `abort()` in the runtime, which is unavoidable for a learning
language, but if we want to step one level deeper right before the echo server,
we need **a way to "return failure as a value"**.

To **seriously** introduce a tagged union (sum type) would involve all of:

- true generics (`Option[T]`)
- `Result[T, E]`
- pattern matching (syntax, exhaustiveness, nesting, guards)
- rewriting the signatures of existing built-ins (`list_at` etc.)

This clearly goes beyond the scope of a single PR. Following the iron rules of
`incremental-language-extensions` — "generics when you need them the second
time" and "at most 4 layers touched in one PR" — we carve out only
**`Option[int]` alone + minimal match syntax** into this sub-project.

The long-term plan proceeds outside the spec:

1. String `len` / `==` / `+` (done)
2. Bytes type (done)
3. List[int] (done)
4. **this sub-project**: Option[int] + match (4a)
5. (future) establish the 2-arm form of Result[int, int] + match (4b)
6. (future) true generics (4c)
7. (future) netpoller + TCP API

## Goals

- Introduce a new primitive type `Option[int]` (the parser rejects anything
  other than `[int]`)
- Value construction: `Some(int) -> Option[int]`, `None: Option[int]`
- Value deconstruction: a minimal `match` statement (Python 3.10-style `case`
  keyword + block)
  - `case Some(x): <block>` (`x` is a single IDENT, bound as an `int` inside the
    block)
  - `case None: <block>`
  - both arms required, order-independent
- Add `list_at_opt(l: List[int], i: int) -> Option[int]` so that out-of-range
  access can be returned as `None` (leave `list_at` aborting, for backward
  compatibility)
- The existing public ABI stays unchanged, existing tests green

## Non-Goals

- Full generics (T of `Option[T]` other than int): a "only `Option[int]` is
  supported" error in the parser
- `Result[T, E]` (separate PR, 4b)
- Using match as an expression (`x = match v: ...`): match is a statement only
- Nested patterns (`Some(Some(x))`), guards (`case Some(x) if x > 0`),
  wildcards (`case _`)
- match with only 1 arm / 3 or more arms (`Option[int]` has only 2 values, so
  it requires exactly 2 arms)
- `Future[Option[int]]` (via spawn): forbidden in Sema
- Built-in methods for `Option` (`unwrap`, `is_some`, `or_else`)
- `print(opt)` (not added to the printable list; make people write
  `Some(x): print(x)` in a `match`)
- `opt == opt` (not added to the == whitelist; make people deconstruct via
  `match`)
- A breaking change that replaces `list_at` with `list_at_opt` (existing
  examples would regress, so they coexist)
- Expressing out-of-range with `Result[int, IndexError]` (there is still no
  IndexError type, so we substitute None)

## Design

### Internal representation

`Option[int]` is a 2-word fat struct:

```c
typedef struct {
    int64_t tag;       /* 0 = None, 1 = Some */
    int64_t payload;   /* the int value when Some, undefined when None */
} rw_option_int;
```

LLVM IR:

```
%rw_option_int = { i64 tag, i64 payload }
```

Size 16 bytes → it **can be returned in 2 registers** on both arm64 and x86_64
SysV. The pointer-out ABI problem we hit with `List[int]` **does not occur here**
(a value return is OK for 16 bytes or less; see the `llvm-ir-c-abi` skill).

The only runtime function added is **`list_at_opt`**:

```c
void rw_list_int_at_opt(rw_option_int *out, const rw_list_int *l, int64_t i);
```

Making `rw_option_int` pointer-out too is more symmetric and safer (it can be
unified when we extend to types over 24 bytes in the future), so `list_at_opt`
is also pointer-out. Construction of `Some` / `None` itself completes inside
LLVM IR (`insertvalue` / constant struct) and does not go through the runtime.

### Language-level behavior

#### Value construction

```rw
def safe_div(a: int, b: int) -> Option[int]:
    if b == 0:
        return None
    return Some(a / b)
```

- `Some(<int expr>)`: evaluated as an expression, returns `{tag=1, payload=<int>}`
- `None`: evaluated as a literal, returns `{tag=0, payload=0}`
  - `None` is a new reserved word (the lexer's `KW_NONE`). It can no longer be
    used as a variable name

#### Value deconstruction

```rw
def main() -> int:
    r: Option[int] = safe_div(10, 2)
    match r:
        case Some(x):
            print(x)             # => 5
        case None:
            print(-1)
    e: Option[int] = safe_div(10, 0)
    match e:
        case Some(x):
            print(x)
        case None:
            print(-1)            # => -1
    return 0
```

- The expression in `match <expr>:` must be of type `Option[int]`, otherwise it
  is an error
- Inside the following indent, **both** `case Some(<IDENT>):` and `case None:`
  are required (order-independent)
- To the right of each case is a block (one or more statements)
- The whole match is a **statement**. It cannot be used as an expression (in
  line with the current rw having no expression-form if)

#### Returning out-of-range as None

```rw
def main() -> int:
    l: List[int] = list_new()
    l = list_push(l, 42)
    safe: Option[int] = list_at_opt(l, 0)
    match safe:
        case Some(x):
            print(x)          # => 42
        case None:
            print(-1)
    oob: Option[int] = list_at_opt(l, 5)
    match oob:
        case Some(x):
            print(x)
        case None:
            print(-1)         # => -1
    return 0
```

### Changes by component

#### Runtime

Only 1 new function:

```c
void rw_list_int_at_opt(rw_option_int *out, const rw_list_int *l, int64_t i);
```

The implementation is a version of the `rw_list_int_at` logic changed to write
`out->tag = 0` instead of `abort`. If in range, it writes
`{tag=1, payload=l->data[i]}` into out.

Also add the C type definition of `rw_option_int` (at the top of `runtime.h`):

```c
typedef struct {
    int64_t tag;
    int64_t payload;
} rw_option_int;
```

C unit test `runtime/fiber/test_option.c` (new):
- that `rw_list_int_at_opt` returns Some when in range and None when out of range
- that it also returns None when the list is empty

#### `rwc/types.py`

```python
OPTION_INT = _Primitive("Option[int]")
```

Do not include it in `is_printable` / `is_numeric`.

#### `rwc/lexer.py`

5 new keywords:

```python
KW_OPTION = auto()
KW_MATCH  = auto()
KW_CASE   = auto()
KW_SOME   = auto()
KW_NONE   = auto()
```

```python
KEYWORDS = {
    ...,
    "Option": KW_OPTION,
    "match":  KW_MATCH,
    "case":   KW_CASE,
    "Some":   KW_SOME,
    "None":   KW_NONE,
}
```

#### `rwc/parser.py`

##### Accept `Option[int]` in `parse_type`

Same pattern as `Future` / `List`:

```python
if t.kind == TokenKind.KW_OPTION:
    self.i += 1
    self.eat(TokenKind.LBRACK, "'[' after Option")
    inner = self.cur
    if inner.kind != TokenKind.KW_INT:
        raise ParserError("only Option[int] is supported in this version of rw",
                          inner.line, inner.col, max(1, len(inner.value)))
    self.i += 1
    self.eat(TokenKind.RBRACK, "']' to close Option[int]")
    return A.TypeName("Option[int]", t.line, t.col)
```

##### Accept `Some(e)` and `None` in the expression parser

At the level of the primary expression:
- `KW_SOME` + `(` + expr + `)` → `A.SomeExpr(arg)`
- `KW_NONE` → `A.NoneExpr()`

##### Accept `match` in the statement parser

In parallel with if/while/return in `parse_stmt`:

```python
if self.cur.kind == TokenKind.KW_MATCH:
    return self._parse_match()
```

What `_parse_match` does:
1. Consume the `match` keyword, parse the expression, require `:`
2. Require INDENT
3. Parse the 2 cases (order-independent, both required):
   - `case Some(<IDENT>):` + block
   - `case None:` + block
4. Require DEDENT
5. Return `A.MatchStmt(target, some_var, some_block, none_block)`

A third-or-later case, a missing side, `case _:` etc. are parser errors.

#### `rwc/ast_nodes.py`

```python
@dataclass
class SomeExpr(Expr):
    arg: Expr

@dataclass
class NoneExpr(Expr):
    pass

@dataclass
class MatchStmt(Stmt):
    target: Expr
    some_var: str        # IDENT bound in some_block
    some_block: List[Stmt]
    none_block: List[Stmt]
```

#### `rwc/sema.py`

##### `_resolve_type`

Add `"Option[int]": T.OPTION_INT` to the dict.

##### Expression Sema

- `SomeExpr`: error if `arg`'s type is not `T.INT`, return type `T.OPTION_INT`
- `NoneExpr`: return type `T.OPTION_INT`

##### `MatchStmt` Sema

```python
if isinstance(stmt, A.MatchStmt):
    tt = self._check_expr(fn, stmt.target, locals_)
    if tt is not T.OPTION_INT:
        raise CompileError("match target must be Option[int]")
    # Some arm: bind some_var as int in a new locals scope
    sub = dict(locals_)
    sub[stmt.some_var] = T.INT
    self._check_block(fn, stmt.some_block, sub)
    # None arm: no binding
    self._check_block(fn, stmt.none_block, locals_)
    return
```

The return-coverage check (whether the last statement of a function is a return)
treats the whole match as "returning overall if both arms return." This is
implemented with the same pattern as the existing `if/elif/else`
terminates-in-return logic.

##### `list_at_opt` built-in

Add `list_at_opt(List[int], int) -> Option[int]` to `_check_call`. Also add it
to the SpawnExpr forbidden list.

##### Do not touch the == whitelist

By not including `Option[int]`, a comparison like `opt == None` is automatically
rejected.

#### `rwc/irgen.py`

##### Type definition

```python
RW_OPTION_INT_TY = ir.LiteralStructType([I64, I64])  # {tag, payload}
```

`llvm_type_of(T.OPTION_INT) -> RW_OPTION_INT_TY`.

##### Value construction

```python
def _emit_some(self, expr, ctx):
    v = self._emit_expr(expr.arg, ctx)            # i64
    s = ir.Constant(RW_OPTION_INT_TY, [ir.Constant(I64, 1), ir.Constant(I64, 0)])
    s = ctx.builder.insert_value(s, v, 1)         # set payload
    return s

def _emit_none(self, expr, ctx):
    return ir.Constant(RW_OPTION_INT_TY,
                       [ir.Constant(I64, 0), ir.Constant(I64, 0)])
```

##### Lowering of match

```python
def _emit_match(self, stmt, ctx):
    b = ctx.builder
    v = self._emit_expr(stmt.target, ctx)
    tag = b.extract_value(v, 0)
    payload = b.extract_value(v, 1)

    some_bb = ctx.function.append_basic_block("match.some")
    none_bb = ctx.function.append_basic_block("match.none")
    end_bb = ctx.function.append_basic_block("match.end")

    # i64 switch: 1 -> some, default (0) -> none
    sw = b.switch(tag, none_bb)
    sw.add_case(ir.Constant(I64, 1), some_bb)

    # Some arm: allocate slot for bound var, store payload
    b.position_at_end(some_bb)
    slot = b.alloca(I64, name=stmt.some_var)
    b.store(payload, slot)
    ctx.locals[stmt.some_var] = slot
    self._emit_block(stmt.some_block, ctx)
    if not b.block.is_terminated:
        b.branch(end_bb)
    del ctx.locals[stmt.some_var]

    # None arm
    b.position_at_end(none_bb)
    self._emit_block(stmt.none_block, ctx)
    if not b.block.is_terminated:
        b.branch(end_bb)

    b.position_at_end(end_bb)
```

The add/remove of `ctx.locals` follows the existing scope management in
`_emit_block`. If both arms return (terminated), end_bb is unreachable, but
merely positioning there is harmless (LLVM removes it).

##### `list_at_opt` call

Add the declaration of `rw_list_int_at_opt` to `_declare_runtime` (pointer-out
pattern: `void (out*, l*, i)`). In `_emit_call`, handle `list_at_opt` as:

```python
if call.callee == "list_at_opt":
    lv = self._emit_expr(call.args[0], ctx)
    iv = self._emit_expr(call.args[1], ctx)
    in_slot = ctx.builder.alloca(RW_LIST_INT_TY)
    ctx.builder.store(lv, in_slot)
    out_slot = ctx.builder.alloca(RW_OPTION_INT_TY)
    ctx.builder.call(self._rw_list_int_at_opt, [out_slot, in_slot, iv])
    return ctx.builder.load(out_slot)
```

##### `_decl_spawn` / `_decl_await`

Do not include T.OPTION_INT (= `Future[Option[int]]` is not allowed; Sema
rejects it first).

### Tests

#### `tests/test_sema.py` (positive 6 + negative 7)

Positive:
- `Some(5)` is `Option[int]`
- `None` is `Option[int]`
- `def f() -> Option[int]: return Some(1)` passes
- `def f() -> Option[int]: return None` passes
- `match v:` passes if both arms are present
- The bound `x` inside some_arm is usable as int (`print(x + 1)`)

Negative:
- `Some("hi")` → argument type error
- `Option[string]` → parser error
- A missing side in `match` → Sema error
- The `match` target is not Option[int] → Sema error
- `print(Some(1))` → printable error
- `Some(1) == None` → comparison-not-allowed error
- Calling fn via `spawn fn() -> Option[int]` → spawn error

#### `examples/option_basic.rw` + `.expected`

(Adopt the example from the top of the spec)

```rw
def safe_div(a: int, b: int) -> Option[int]:
    if b == 0:
        return None
    return Some(a / b)

def main() -> int:
    r: Option[int] = safe_div(10, 2)
    match r:
        case Some(x):
            print(x)
        case None:
            print(-1)
    e: Option[int] = safe_div(10, 0)
    match e:
        case Some(x):
            print(x)
        case None:
            print(-1)
    return 0
```

Expected output:

```
5
-1
```

Add `option_basic` to the parametrize in `tests/test_e2e.py`.

#### C unit test

`runtime/fiber/test_option.c`: covers the Some / None / empty-list paths of
`rw_list_int_at_opt`.

## Changes by file

### Changed

- `runtime/runtime.h` — the `rw_option_int` struct and the `rw_list_int_at_opt`
  prototype
- `runtime/runtime.c` — the `rw_list_int_at_opt` implementation
- `rwc/types.py` — `OPTION_INT`
- `rwc/lexer.py` — the 5 new keywords
- `rwc/parser.py` — the Option branch in `parse_type` + `Some`/`None`
  expressions + `parse_match`
- `rwc/ast_nodes.py` — `SomeExpr` / `NoneExpr` / `MatchStmt`
- `rwc/sema.py` — `_resolve_type` / expression Sema / `MatchStmt` Sema /
  `list_at_opt` built-in / SpawnExpr forbid / return-coverage check update
- `rwc/irgen.py` — `RW_OPTION_INT_TY` / `llvm_type_of` / `_emit_some` /
  `_emit_none` / `_emit_match` / `list_at_opt` call / `_declare_runtime`
- `tests/test_sema.py` — positive 6 + negative 7
- `tests/test_e2e.py` — `option_basic` in the parametrize
- `.gitignore` — `runtime/fiber/test_option`

### New

- `runtime/fiber/test_option.c`
- `examples/option_basic.rw` + `.expected`
- `docs/specs/10-option-type.md` (this file)
- `docs/plans/2026-05-22-option-type.md` (created with writing-plans)

### Unchanged

- fiber scheduler (`runtime/fiber/sched.c` etc.), driver, existing spec docs

## Verification

```sh
# runtime unit
make -C runtime clean && make -C runtime
cd runtime
cc -O2 -Wall -Wextra -std=c11 -D_GNU_SOURCE -pthread fiber/test_option.c librw.a -o fiber/test_option
./fiber/test_option

# pytest
cd ..
uv run pytest -q
# expected: existing 101 + Sema positive 6 + negative 7 + e2e 1 = 115 all green

# standalone run
uv run rwc run examples/option_basic.rw
# expected output: 5\n-1\n

# existing example regression
uv run rwc run examples/list_basic.rw
uv run rwc run examples/string_ops.rw
uv run rwc run examples/spawn_many.rw
```

## Commit structure

4 commits:

1. **rwc (lexer/parser/types)**: the 5 keywords, `Option[int]` parsing, the
   `Some(e)` and `None` expressions, parsing the `match` statement, the new AST
   nodes, `T.OPTION_INT`, `_resolve_type`. Only the type annotation and AST
   construction pass (Sema/irgen not implemented)
2. **rwc (sema)**: Sema for the `Some`/`None` expressions, Sema for `MatchStmt`
   (exhaustiveness, bound variable, return-coverage), negative tests in one batch
3. **runtime + rwc (irgen)**: the `rw_option_int` struct, the
   `rw_list_int_at_opt` implementation and C test, IR generation in irgen for
   `Some`/`None`/`match`/`list_at_opt`, smoke verification
4. **examples + e2e**: add `option_basic.rw` and update the parametrize in
   `tests/test_e2e.py`

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Making `None` a reserved word clashes with existing user code | There is no place in `examples/*.rw` in the rw repository that uses `None` as a variable name (grep confirmed). Since it is a new reserved word, it will not unintentionally clash in the future either |
| The match return-coverage check implementation is complex | Extract the existing `if/elif/else` terminates-in-return logic into one function and reuse it for match too. Recursively decide whether both arms return |
| A user who expects `Some(x) == ...` comparison | Noted in the spec Non-Goals; the error message guides with "use match to inspect Option[int]" (though this time the error message stays simple as "cannot compare `Option[int]`") |
| Wanting to reuse match when introducing `Result[T, E]` in the future | The parser hardcodes `case Some(IDENT)`, so it cannot be used as-is. The spec Non-Goals note "the Result type is a separate PR; generalize the match parser then" |
| Can the == whitelisting (added in #93) reject Option? | Simply not adding `Option[int]` to the whitelist automatically produces a "cannot compare Option[int]" error. Covered by tests |
| Do `_emit_match`'s SSA / basic-block management line up with the existing `if`? | Following the existing `_emit_if`, adopt the same scope rules (alloca in the entry BB, push/pop the locals dict per block). At implementation time, confirm the existing pattern with Read |
