# `for ... in range(...)` Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Introduce a `for <var> in range(start, stop[, step])` loop into rw as syntactic sugar.

**Architecture:** The parser produces a new `For` AST node, and an independent pass `desugar.py` — which runs immediately after parsing and before sema — rewrites `For` into the existing `VarDecl` / `While` / `Assign` nodes. This leaves sema, irgen, and the runtime unmodified. `range` is accepted only in the `for` header position and is never treated as a value. `step==0` yields a zero-iteration loop because the desugared loop condition is false on both sides.

**Tech Stack:** Python (rwc compiler: lexer/parser/sema/irgen), llvmlite, pytest, C runtime (unmodified).

Reference spec: `docs/specs/13-for-range-loop.md`

---

## File Structure

- **Modify** `rwc/ast_nodes.py` — add the `For` dataclass; add it to the `Stmt` Union
- **Modify** `rwc/parser.py` — add `parse_for()`; add `KW_FOR` to the `parse_stmt` dispatch
- **Create** `rwc/desugar.py` — the `desugar_module(mod)` pass. Expand `For` into `While` etc.
- **Modify** `rwc/driver.py` — insert desugar immediately after parse in `compile_source` / `emit_ir` / `emit_ast`
- **Modify** `tests/test_parser.py` — tests for the for parse result
- **Create** `tests/test_desugar.py` — tests for desugar expansion
- **Modify** `tests/test_sema.py` — negative tests for for (type errors)
- **Create** `examples/for_count.rw` + `examples/for_count.rw.expected` — e2e sample
- **Modify** `tests/test_e2e.py` — add `for_count` to parametrize

---

## Task 1: Add the `For` AST node

**Files:**
- Modify: `rwc/ast_nodes.py`

- [ ] **Step 1: Add the `For` dataclass directly after `While` (around L193)**

Insert directly after the `While` class definition in `rwc/ast_nodes.py`:

```python
@dataclass
class For:
    var: str               # loop variable name
    start: Expr            # int expr
    stop: Expr             # int expr
    step: Expr             # int expr (defaults filled by parser)
    body: List["Stmt"]
    line: int
    col: int
```

- [ ] **Step 2: Add `For` to the `Stmt` Union**

Change the `Stmt = Union[...]` line (currently L212) in `rwc/ast_nodes.py`:

```python
Stmt = Union[VarDecl, Assign, ExprStmt, Return, If, While, For, MatchStmt]
```

- [ ] **Step 3: Confirm the import is not broken**

Run: `uv run python -c "from rwc import ast_nodes as A; A.For"`
Expected: no error (nothing printed)

- [ ] **Step 4: Commit**

```bash
git add rwc/ast_nodes.py
git commit -m "ast: add For node for range-based loops"
```

---

## Task 2: Add `for ... in range(...)` to the parser

**Files:**
- Modify: `rwc/parser.py`
- Test: `tests/test_parser.py`

- [ ] **Step 1: Write a failing test**

Append to the end of `tests/test_parser.py`:

```python
def test_parse_for_two_args():
    src = "def main() -> int:\n    for i in range(0, 10):\n        return i\n"
    mod = parse_src(src)
    f = mod.functions[0]
    loop = f.body[0]
    assert isinstance(loop, A.For)
    assert loop.var == "i"
    assert isinstance(loop.start, A.IntLit) and loop.start.value == 0
    assert isinstance(loop.stop, A.IntLit) and loop.stop.value == 10
    # step defaults to literal 1
    assert isinstance(loop.step, A.IntLit) and loop.step.value == 1


def test_parse_for_one_arg():
    src = "def main() -> int:\n    for i in range(5):\n        return i\n"
    loop = parse_src(src).functions[0].body[0]
    assert isinstance(loop, A.For)
    assert isinstance(loop.start, A.IntLit) and loop.start.value == 0
    assert isinstance(loop.stop, A.IntLit) and loop.stop.value == 5
    assert isinstance(loop.step, A.IntLit) and loop.step.value == 1


def test_parse_for_three_args():
    src = "def main() -> int:\n    for i in range(10, 0, -1):\n        return i\n"
    loop = parse_src(src).functions[0].body[0]
    assert isinstance(loop, A.For)
    assert isinstance(loop.stop, A.IntLit) and loop.stop.value == 0
    # step is unary minus on 1
    assert isinstance(loop.step, A.UnaryOp) and loop.step.op == "-"


def test_parse_range_outside_for_is_error():
    src = "def main() -> int:\n    x: int = range(0, 5)\n    return x\n"
    with pytest.raises(ParserError):
        parse_src(src)


def test_parse_for_zero_args_is_error():
    src = "def main() -> int:\n    for i in range():\n        return i\n"
    with pytest.raises(ParserError):
        parse_src(src)


def test_parse_for_four_args_is_error():
    src = "def main() -> int:\n    for i in range(0, 1, 2, 3):\n        return i\n"
    with pytest.raises(ParserError):
        parse_src(src)
```

- [ ] **Step 2: Confirm the test fails**

Run: `uv run pytest tests/test_parser.py -k for_ -v`
Expected: FAIL (`for` cannot be parsed, raising ParserError, or no For node is produced)

- [ ] **Step 3: Add `KW_FOR` to the `parse_stmt` dispatch**

Add directly after the `KW_WHILE` branch in `parse_stmt` (around L239) of `rwc/parser.py`:

```python
        if t.kind == TokenKind.KW_FOR:
            return self.parse_for()
```

- [ ] **Step 4: Implement `parse_for()`**

Add directly after the `parse_while` method (currently L302) in `rwc/parser.py`:

```python
    def parse_for(self) -> A.For:
        kw = self.eat(TokenKind.KW_FOR)
        var_tok = self.eat(TokenKind.IDENT, "loop variable name")
        self.eat(TokenKind.KW_IN, "'in' after for variable")
        # range header: the identifier `range` followed by ( args )
        if not (self.cur.kind == TokenKind.IDENT and self.cur.value == "range"):
            raise ParserError(
                "for loop must iterate over range(...)",
                self.cur.line, self.cur.col,
            )
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
```

> Note: If `range` is used outside a for, the ordinary expression path
> produces `Call("range", ...)` and sema rejects it with "undefined function
> range". Since the parser does not treat `range` as a builtin outside the
> for header either, `x = range(0,5)` becomes an error at the sema stage.
> However, the negative test `test_parse_range_outside_for_is_error` expects a
> ParserError, so verify the behavior in Step 5; if it becomes a CompileError
> rather than a ParserError, move that test to the negative cases in
> `test_sema.py` (see Step 5).

- [ ] **Step 5: Run the tests and confirm**

Run: `uv run pytest tests/test_parser.py -k for_ -v`
Expected: the 3 positive cases PASS. `test_parse_for_zero_args_is_error` / `test_parse_for_four_args_is_error` PASS.
`test_parse_range_outside_for_is_error` FAILs if `range` passes as an expression. In that case, remove that test from `tests/test_parser.py` and cover it with the sema negative test in Task 5 (`test_for_range_outside_is_sema_error`). Place it in one location or the other according to the actual behavior.

- [ ] **Step 6: Commit**

```bash
git add rwc/parser.py tests/test_parser.py
git commit -m "parser: parse for-in-range loop header into For node"
```

---

## Task 3: Implement the desugar pass

**Files:**
- Create: `rwc/desugar.py`
- Test: `tests/test_desugar.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_desugar.py`:

```python
from __future__ import annotations

from rwc import ast_nodes as A
from rwc.desugar import desugar_module
from rwc.lexer import tokenize
from rwc.parser import parse


def desugar_src(src: str) -> A.Module:
    return desugar_module(parse(tokenize(src)))


def test_for_expands_to_vardecl_and_while():
    src = "def main() -> int:\n    for i in range(0, 3):\n        return i\n"
    mod = desugar_src(src)
    body = mod.functions[0].body
    # No For node remains anywhere.
    assert all(not isinstance(s, A.For) for s in body)
    # Expect: temp decls + loop var decl + a While.
    whiles = [s for s in body if isinstance(s, A.While)]
    assert len(whiles) == 1
    # The loop variable `i` is declared as a VarDecl before the while.
    vardecls = [s for s in body if isinstance(s, A.VarDecl)]
    assert any(v.name == "i" for v in vardecls)


def test_for_while_condition_uses_or_of_two_comparisons():
    src = "def main() -> int:\n    for i in range(0, 3):\n        return i\n"
    mod = desugar_src(src)
    w = [s for s in mod.functions[0].body if isinstance(s, A.While)][0]
    # cond is: (step>0 and i<stop) or (step<0 and i>stop)
    assert isinstance(w.cond, A.BinOp) and w.cond.op == "or"


def test_for_body_ends_with_increment():
    src = "def main() -> int:\n    for i in range(0, 3):\n        return i\n"
    mod = desugar_src(src)
    w = [s for s in mod.functions[0].body if isinstance(s, A.While)][0]
    last = w.body[-1]
    # i = i + __step
    assert isinstance(last, A.Assign) and last.name == "i"
    assert isinstance(last.value, A.BinOp) and last.value.op == "+"
```

- [ ] **Step 2: Confirm the tests fail**

Run: `uv run pytest tests/test_desugar.py -v`
Expected: FAIL (`ModuleNotFoundError: rwc.desugar`)

- [ ] **Step 3: Implement `rwc/desugar.py`**

Create `rwc/desugar.py`:

```python
"""Desugaring pass: lower syntactic-sugar AST nodes to core nodes.

Runs after parsing and before sema. Currently lowers `For` (range-based
loops) into `VarDecl` + `While` + `Assign` using only core AST nodes, so
sema and irgen need no knowledge of `For`.
"""

from __future__ import annotations

from typing import List

from . import ast_nodes as A


class _Desugarer:
    def __init__(self) -> None:
        self._tmp_counter = 0

    def _fresh(self, base: str) -> str:
        n = self._tmp_counter
        self._tmp_counter += 1
        return f"__for_{base}_{n}"

    def module(self, mod: A.Module) -> A.Module:
        for fn in mod.functions:
            fn.body = self._block(fn.body)
        return mod

    def _block(self, stmts: List[A.Stmt]) -> List[A.Stmt]:
        out: List[A.Stmt] = []
        for s in stmts:
            out.extend(self._stmt(s))
        return out

    def _stmt(self, s: A.Stmt) -> List[A.Stmt]:
        if isinstance(s, A.For):
            return self._lower_for(s)
        if isinstance(s, A.If):
            s.then_body = self._block(s.then_body)
            s.else_body = self._block(s.else_body)
            return [s]
        if isinstance(s, A.While):
            s.body = self._block(s.body)
            return [s]
        if isinstance(s, A.MatchStmt):
            if s.some_block is not None:
                s.some_block = self._block(s.some_block)
            if s.none_block is not None:
                s.none_block = self._block(s.none_block)
            if s.ok_block is not None:
                s.ok_block = self._block(s.ok_block)
            if s.err_block is not None:
                s.err_block = self._block(s.err_block)
            return [s]
        return [s]

    def _int_type(self, ln: int, col: int) -> A.TypeName:
        return A.TypeName("int", ln, col)

    def _lower_for(self, f: A.For) -> List[A.Stmt]:
        ln, col = f.line, f.col
        stop_name = self._fresh("stop")
        step_name = self._fresh("step")

        # Recursively desugar the body first (nested fors).
        body = self._block(f.body)

        out: List[A.Stmt] = []
        # __stop = <stop>; __step = <step>; <var> = <start>
        out.append(A.VarDecl(stop_name, self._int_type(ln, col), f.stop, ln, col))
        out.append(A.VarDecl(step_name, self._int_type(ln, col), f.step, ln, col))
        out.append(A.VarDecl(f.var, self._int_type(ln, col), f.start, ln, col))

        zero = A.IntLit(0, ln, col)
        step_pos = A.BinOp(">", A.Name(step_name, ln, col), zero, ln, col)
        step_neg = A.BinOp("<", A.Name(step_name, ln, col), A.IntLit(0, ln, col), ln, col)
        lt = A.BinOp("<", A.Name(f.var, ln, col), A.Name(stop_name, ln, col), ln, col)
        gt = A.BinOp(">", A.Name(f.var, ln, col), A.Name(stop_name, ln, col), ln, col)
        asc = A.BinOp("and", step_pos, lt, ln, col)
        desc = A.BinOp("and", step_neg, gt, ln, col)
        cond = A.BinOp("or", asc, desc, ln, col)

        # body + (var = var + __step)
        incr = A.Assign(
            f.var,
            A.BinOp("+", A.Name(f.var, ln, col), A.Name(step_name, ln, col), ln, col),
            ln, col,
        )
        while_body = list(body) + [incr]
        out.append(A.While(cond, while_body, ln, col))
        return out


def desugar_module(mod: A.Module) -> A.Module:
    return _Desugarer().module(mod)
```

- [ ] **Step 4: Run the tests and confirm**

Run: `uv run pytest tests/test_desugar.py -v`
Expected: all 3 PASS

- [ ] **Step 5: Commit**

```bash
git add rwc/desugar.py tests/test_desugar.py
git commit -m "desugar: lower For range loops to While + assignments"
```

---

## Task 4: Wire desugar into the driver

**Files:**
- Modify: `rwc/driver.py`

- [ ] **Step 1: Add the import**

Add to the imports in `rwc/driver.py` (directly after L24 `from .parser import ...`):

```python
from .desugar import desugar_module
```

- [ ] **Step 2: Insert desugar immediately after parse in `compile_source`**

In `compile_source` of `rwc/driver.py`, directly after `ast = parse(tokens)` (currently L82):

```python
        tokens = tokenize(source, filename=filename)
        ast = parse(tokens)
        ast = desugar_module(ast)
        sema = analyze(ast, filename=filename)
        llmod = irgen_generate(ast, sema)
```

- [ ] **Step 3: Make the same insertion in `emit_ir`**

In `emit_ir` of `rwc/driver.py` (currently L128-131):

```python
    tokens = tokenize(source, filename=filename)
    ast = parse(tokens)
    ast = desugar_module(ast)
    sema = analyze(ast, filename=filename)
    llmod = irgen_generate(ast, sema)
```

- [ ] **Step 4: Make the same insertion in `emit_ast`**

In `emit_ast` of `rwc/driver.py` (currently L136-138):

```python
def emit_ast(source: str, filename: str) -> ASTModule:
    tokens = tokenize(source, filename=filename)
    return desugar_module(parse(tokens))
```

- [ ] **Step 5: Confirm the full pipeline passes (with a temporary file)**

Run:
```bash
uv run python -c "
from rwc.driver import emit_ir
src = 'def main() -> int:\n    total: int = 0\n    for i in range(0, 5):\n        total = total + i\n    return total\n'
ir = emit_ir(src, filename='t.rw')
print('rw_user_main' in ir)
"
```
Expected: `True` (the for goes through desugar → sema → irgen and IR is generated)

- [ ] **Step 6: Commit**

```bash
git add rwc/driver.py
git commit -m "driver: run desugar pass between parse and sema on all paths"
```

---

## Task 5: Add sema negative tests

**Files:**
- Test: `tests/test_sema.py`

> Purpose: pin down that a type error occurs when a for argument is non-int,
> and that using `range` outside a for is an error (whether it is rejected by
> the parser or sema follows the actual behavior from Task 2 Step 5).

- [ ] **Step 1: Add the tests**

Append to the end of `tests/test_sema.py` (the `check` / `err` helpers already exist):

```python
def test_for_loop_int_args_ok():
    src = (
        "def main() -> int:\n"
        "    total: int = 0\n"
        "    for i in range(0, 5):\n"
        "        total = total + i\n"
        "    return total\n"
    )
    # Must be desugared before sema.
    from rwc.desugar import desugar_module
    from rwc.parser import parse
    from rwc.lexer import tokenize
    from rwc.sema import analyze
    res = analyze(desugar_module(parse(tokenize(src))), filename="t.rw")
    assert "main" in res.functions


def test_for_loop_non_int_stop_is_error():
    src = (
        "def main() -> int:\n"
        '    for i in range(0, "x"):\n'
        "        return i\n"
        "    return 0\n"
    )
    from rwc.desugar import desugar_module
    from rwc.parser import parse
    from rwc.lexer import tokenize
    from rwc.sema import analyze
    import pytest as _pytest
    with _pytest.raises(CompileError):
        analyze(desugar_module(parse(tokenize(src))), filename="t.rw")
```

- [ ] **Step 2: Run the tests and confirm**

Run: `uv run pytest tests/test_sema.py -k for_loop -v`
Expected: 2 PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_sema.py
git commit -m "sema: tests for for-loop int typing"
```

---

## Task 6: e2e sample and expected values

**Files:**
- Create: `examples/for_count.rw`
- Create: `examples/for_count.rw.expected`
- Modify: `tests/test_e2e.py`

- [ ] **Step 1: Create the sample**

Create `examples/for_count.rw`:

```
def main() -> int:
    total: int = 0
    for i in range(0, 10):
        total = total + i
    down: int = 0
    for j in range(10, 0, -1):
        down = down + j
    step2: int = 0
    for k in range(0, 10, 2):
        step2 = step2 + k
    empty: int = 0
    for m in range(5, 5):
        empty = empty + 1
    print(total)
    print(down)
    print(step2)
    print(empty)
    return 0
```

> total = 0+1+...+9 = 45, down = 10+9+...+1 = 55, step2 = 0+2+4+6+8 = 20,
> empty is a zero-iteration loop so it is 0.

- [ ] **Step 2: Check print's output format and build the expected value**

Run:
```bash
uv run python -m rwc.cli run examples/for_count.rw
```
Expected: four lines of numeric output. Verify the actual output (including newlines and formatting).

- [ ] **Step 3: Build `.expected` from the verified output**

Save the actual output from Step 2 verbatim into `examples/for_count.rw.expected`.
It is expected to use the same formatting as the other examples (`examples/while_count.rw.expected`). Expected value:

```
45
55
20
0
```

(If there is any difference from the actual output in Step 2, treat the actual output as authoritative.)

- [ ] **Step 4: Add to parametrize**

Add `"for_count"` to the end of the `@pytest.mark.parametrize` list (around L52) in `tests/test_e2e.py`:

```python
@pytest.mark.parametrize(
    "name",
    ["hello", "arith", "fib", "while_count", "spawn_basic", "spawn_many", "spawn_string", "string_ops", "bytes_basic", "list_basic", "option_basic", "result_basic", "for_count"],
)
```

- [ ] **Step 5: Run the e2e test**

Run: `uv run pytest tests/test_e2e.py -k for_count -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add examples/for_count.rw examples/for_count.rw.expected tests/test_e2e.py
git commit -m "examples: add for_count exercising for-in-range loops"
```

---

## Task 7: Confirm all tests green

**Files:** none (verification only)

- [ ] **Step 1: Run all tests**

Run: `uv run pytest -q`
Expected: all PASS (no regression in existing tests, all new tests green)

- [ ] **Step 2: Visually inspect the post-desugar form with emit-ast (optional)**

Run: `uv run python -m rwc.cli emit-ast examples/for_count.rw`
Expected: no `For` node appears; it is expanded into `While` + `VarDecl` + `Assign`

---

## Self-Review (completed)

**Spec coverage:**
- 1-to-3-argument range → Task 2 (parser fills in defaults)
- Arbitrary int expressions → Task 2 (arguments taken via `parse_expr`) + Task 5 (type checking)
- Negative step / half-open interval → Task 3 (condition `(step>0 and v<stop) or (step<0 and v>stop)`)
- step==0 yields a zero-iteration loop → Task 3 (both sides of the condition false) + Task 6 (verified via empty)
- Do not allow `range` to be used as a value → Task 2 (range is not made a builtin outside the for header)
- Prevent double evaluation → Task 3 (bound to `__for_stop_N` / `__for_step_N`)
- No changes to sema/irgen/runtime → Task 3/4 (desugar produces only core nodes)
- Desugar on 3 paths → Task 4 (compile_source / emit_ir / emit_ast)

**Placeholder scan:** No placeholders. Only the `.expected` value is explicitly noted as taking the real output from Task 6 Step 2 as authoritative (the anticipated value is also provided).

**Type consistency:** `For(var, start, stop, step, body, line, col)` is defined in Task 1 and used with the same signature in Tasks 2/3. `desugar_module` is defined in Task 3 and used in Tasks 4/5. Consistent.
