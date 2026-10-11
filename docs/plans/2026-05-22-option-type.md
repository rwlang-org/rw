# Option[int] + match Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an `Option[int]` type and a minimal `match` statement (Python 3.10 style) to the rw language, so that code where a function like `safe_div(10, 0)` returns `None` and is destructured with `match` can run. Also add `list_at_opt(l, i) -> Option[int]` to the runtime so that out-of-range access returns `None` instead of `abort`.

**Architecture:** `Option[int]` is represented in LLVM IR as a two-word fat struct `{i64 tag, i64 payload}` (tag=1 = Some, 0 = None). At 16 bytes it can be returned in registers on both arm64 and x86_64 SysV (no pointer-out ABI needed). Introduce the new keywords `Option` / `match` / `case` / `Some` / `None`; the parser parses `match` as a statement requiring exactly two arms, and irgen lowers it with a `switch` instruction.

**Tech Stack:** C11 (runtime), Python 3.12 + llvmlite (compiler), pytest (tests).

**Spec:** `docs/specs/10-option-type.md`

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `runtime/runtime.h` | ABI declarations | `rw_option_int` struct + `rw_list_int_at_opt` prototype |
| `runtime/runtime.c` | helper implementation | Add `rw_list_int_at_opt` |
| `runtime/fiber/test_option.c` | C unit test | New |
| `.gitignore` | test binary | Add 1 line |
| `rwc/types.py` | primitive type definitions | Add `OPTION_INT` |
| `rwc/lexer.py` | keyword recognition | `KW_OPTION` / `KW_MATCH` / `KW_CASE` / `KW_SOME` / `KW_NONE` |
| `rwc/parser.py` | type + expression + statement parsing | Option branch in `parse_type`, Some/None expressions, match statement |
| `rwc/ast_nodes.py` | AST nodes | Add `SomeExpr` / `NoneExpr` / `MatchStmt` |
| `rwc/sema.py` | type resolution + expression/statement Sema + return coverage | 4 modifications |
| `rwc/irgen.py` | LLVM IR generation | Emit `RW_OPTION_INT_TY` / Some/None/match |
| `tests/test_sema.py` | positive/negative type checks | Add tests |
| `tests/test_e2e.py` | add option_basic to parametrize | Add 1 line |
| `examples/option_basic.rw` | demo | New |
| `examples/option_basic.rw.expected` | expected output | New |

---

## Task 1: Recognize `Option[int]` syntax in lexer / parser / types / AST

The goal of this task is to make the `Option[int]` type annotation, the `Some(e)` / `None` expressions, and the `match v: case Some(x): ... case None: ...` statement **buildable all the way to the AST**. Sema / irgen remain unimplemented, so actually trying to evaluate `Some` will error.

**Files:**
- Modify: `rwc/types.py`
- Modify: `rwc/lexer.py`
- Modify: `rwc/ast_nodes.py`
- Modify: `rwc/parser.py`
- Modify: `rwc/sema.py` (`_resolve_type` only)
- Modify: `tests/test_sema.py`

- [ ] **Step 1.1: Add `OPTION_INT` to `rwc/types.py`**

In `rwc/types.py`, add **directly below** `LIST_INT = _Primitive("List[int]")`:

```python
OPTION_INT = _Primitive("Option[int]")
```

**Do not include** it in `is_printable` / `is_numeric`.

- [ ] **Step 1.2: Add 5 keywords to `rwc/lexer.py`**

**Directly below** `KW_LIST = auto()` in the `TokenKind` enum:

```python
    KW_OPTION = auto()
    KW_MATCH = auto()
    KW_CASE = auto()
    KW_SOME = auto()
    KW_NONE = auto()
```

**Directly below** `"List": TokenKind.KW_LIST,` in the `KEYWORDS` dict:

```python
    "Option": TokenKind.KW_OPTION,
    "match":  TokenKind.KW_MATCH,
    "case":   TokenKind.KW_CASE,
    "Some":   TokenKind.KW_SOME,
    "None":   TokenKind.KW_NONE,
```

- [ ] **Step 1.3: Add the new nodes to `rwc/ast_nodes.py`**

Read `ast_nodes.py` and check the structure at the end of the file (locate where `SpawnExpr` / `AwaitExpr` are defined). Add **directly after** them:

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
    some_var: str
    some_block: List[Stmt]
    none_block: List[Stmt]
```

`SomeExpr` / `NoneExpr` inherit from `Expr`, and `MatchStmt` inherits from `Stmt`.
Use Read to check the base class names and the fields of the existing `Stmt`/`Expr`
(whether there are dataclass fields such as line, col), and add `line: int = 0`, `col: int = 0`
if needed. **Match the format you saw with Read**.

- [ ] **Step 1.4: Add an Option branch to `parse_type` in `rwc/parser.py`**

In the `parse_type` method, add **directly below** the `List` branch:

```python
        if t.kind == TokenKind.KW_OPTION:
            self.i += 1
            self.eat(TokenKind.LBRACK, "'[' after Option")
            inner_tok = self.cur
            if inner_tok.kind != TokenKind.KW_INT:
                raise ParserError(
                    "only Option[int] is supported in this version of rw",
                    inner_tok.line, inner_tok.col, max(1, len(inner_tok.value)),
                )
            self.i += 1
            self.eat(TokenKind.RBRACK, "']' to close Option[int]")
            return A.TypeName("Option[int]", t.line, t.col)
```

- [ ] **Step 1.5: Accept `Some(e)` and `None` as expressions in `parser.py`**

Use Read to locate where primary expressions are handled (the method that
processes literals / IDENT / `spawn` / `await`, etc.). Landmarks to look for: the
`KW_TRUE` / `KW_FALSE` / `KW_SPAWN` / `KW_AWAIT` branches. Add at the same level:

```python
        if t.kind == TokenKind.KW_SOME:
            kw = t
            self.i += 1
            self.eat(TokenKind.LPAREN, "'(' after Some")
            arg = self.parse_expr()
            self.eat(TokenKind.RPAREN, "')' to close Some(...)")
            return A.SomeExpr(arg=arg, line=kw.line, col=kw.col)
        if t.kind == TokenKind.KW_NONE:
            kw = t
            self.i += 1
            return A.NoneExpr(line=kw.line, col=kw.col)
```

Whether the `A.SomeExpr` / `A.NoneExpr` constructors take `line`/`col` as arguments
depends on the dataclass shape decided in Step 1.3. Match how `SpawnExpr` is
constructed.

- [ ] **Step 1.6: Add a `match` branch to `parse_stmt` in `parser.py`**

Add at the same level as `if t.kind == TokenKind.KW_RETURN:` / `KW_IF:` / `KW_WHILE:` in the `parse_stmt` method (around parser.py:207):

```python
        if t.kind == TokenKind.KW_MATCH:
            return self._parse_match()
```

Add the `_parse_match` method right after `_parse_while` in `parser.py`:

```python
    def _parse_match(self) -> A.MatchStmt:
        kw = self.eat(TokenKind.KW_MATCH)
        target = self.parse_expr()
        self.eat(TokenKind.COLON, "':' after match target")
        self.eat(TokenKind.NEWLINE, "newline before match body")
        self.eat(TokenKind.INDENT, "indented match body")

        some_var: Optional[str] = None
        some_block: Optional[List[A.Stmt]] = None
        none_block: Optional[List[A.Stmt]] = None

        while self.cur.kind == TokenKind.KW_CASE:
            self.eat(TokenKind.KW_CASE)
            arm_tok = self.cur
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
                self.eat(TokenKind.NEWLINE, "newline before case body")
                self.eat(TokenKind.INDENT, "indented case body")
                some_var = ident.value
                some_block = []
                while self.cur.kind != TokenKind.DEDENT:
                    some_block.append(self.parse_stmt())
                self.eat(TokenKind.DEDENT, "dedent to close case body")
            elif arm_tok.kind == TokenKind.KW_NONE:
                if none_block is not None:
                    raise ParserError(
                        "duplicate `case None` arm in match",
                        arm_tok.line, arm_tok.col, 4,
                    )
                self.eat(TokenKind.KW_NONE)
                self.eat(TokenKind.COLON, "':' after case pattern")
                self.eat(TokenKind.NEWLINE, "newline before case body")
                self.eat(TokenKind.INDENT, "indented case body")
                none_block = []
                while self.cur.kind != TokenKind.DEDENT:
                    none_block.append(self.parse_stmt())
                self.eat(TokenKind.DEDENT, "dedent to close case body")
            else:
                raise ParserError(
                    "match case must be `Some(x)` or `None`",
                    arm_tok.line, arm_tok.col,
                    max(1, len(arm_tok.value)),
                )

        self.eat(TokenKind.DEDENT, "dedent to close match body")

        if some_block is None or none_block is None:
            raise ParserError(
                "match on Option[int] must cover both Some and None",
                kw.line, kw.col, 5,
            )
        return A.MatchStmt(
            target=target,
            some_var=some_var,
            some_block=some_block,
            none_block=none_block,
            line=kw.line, col=kw.col,
        )
```

How the rw lexer actually generates NEWLINE / INDENT / DEDENT (whether it is the
same sequence as when the parser reads `if` or `while`) should be matched by
reading parser.py:236-280 (`_parse_if` / `_parse_while`). **Be sure to align
this plan's skeleton with the existing `_parse_if` sequence**.

- [ ] **Step 1.7: Add `Option[int]` to `_resolve_type` in `rwc/sema.py`**

Add 1 line to the `m` dict in the `_resolve_type` function:

```python
        m = {
            "int": T.INT,
            "float": T.FLOAT,
            "bool": T.BOOL,
            "string": T.STRING,
            "Bytes": T.BYTES,
            "List[int]": T.LIST_INT,
            "Option[int]": T.OPTION_INT,
            "void": T.VOID,
        }
```

- [ ] **Step 1.8: Confirm the build and existing tests are green**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw && uv run pytest -q 2>&1 | tail -5
```

Expected: `101 passed`. Since Sema / irgen do not handle the new AST, code that
**actually uses** `Some(1)` will fall through in Sema, but no such code has been
written yet.

- [ ] **Step 1.9: Tests that only parse the type annotation and match**

Add to the end of `tests/test_sema.py`:

```python
def test_option_int_type_annotation_parses():
    src = (
        "def takes_opt(o: Option[int]) -> int:\n"
        "    return 0\n"
        "def main() -> int:\n"
        "    return 0\n"
    )
    res = check(src)
    assert "takes_opt" in res.functions
    assert res.functions["takes_opt"].params[0][1] is T.OPTION_INT


def test_option_with_non_int_param_is_parser_error():
    import pytest
    from rwc.lexer import tokenize
    from rwc.parser import parse, ParserError
    src = (
        "def f(o: Option[string]) -> int:\n"
        "    return 0\n"
        "def main() -> int:\n"
        "    return 0\n"
    )
    with pytest.raises(ParserError) as ei:
        parse(tokenize(src))
    assert "Option[int]" in str(ei.value) or "only Option" in str(ei.value)


def test_match_with_missing_arm_is_parser_error():
    import pytest
    from rwc.lexer import tokenize
    from rwc.parser import parse, ParserError
    # only Some arm — parser must reject
    src = (
        "def main() -> int:\n"
        "    o: Option[int] = None\n"
        "    match o:\n"
        "        case Some(x):\n"
        "            return x\n"
        "    return 0\n"
    )
    with pytest.raises(ParserError) as ei:
        parse(tokenize(src))
    assert "must cover both" in str(ei.value)
```

- [ ] **Step 1.10: Run pytest**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw && uv run pytest tests/test_sema.py -q 2>&1 | tail -5
```

Expected: existing 38 + 3 new = `41 passed`. `test_match_with_missing_arm`
expects a ParserError, but if the parser accepts a `Some(x)` expression inside
match (which Sema does not yet handle), the error may come from a different path.
In that case, adjust the shape to instead expect a `ParserError` from
`parse(tokenize(...))`.

- [ ] **Step 1.11: Commit**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
git add rwc/types.py rwc/lexer.py rwc/ast_nodes.py rwc/parser.py rwc/sema.py tests/test_sema.py
git commit -m "$(cat <<'EOF'
rwc: introduce Option[int] type and match syntax in lexer / parser

Adds the primitive type T.OPTION_INT, five new lexer keywords
(Option / match / case / Some / None), parser branches for the
Option[int] type annotation, Some(e) / None expressions, and the
match statement with two-arm requirement (Some(x) and None,
order-independent, both mandatory).

AST gets three new nodes: SomeExpr, NoneExpr, MatchStmt. Sema's
_resolve_type recognizes Option[int]. With this commit, the type
annotation `o: Option[int]` parses, but actually constructing or
matching on a value will fall through sema and crash at irgen —
that lands in the next two commits.

Parser-level negatives covered: Option[T] for T != int rejects,
match with only one arm rejects.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Type-check `Some` / `None` / `MatchStmt` in Sema

In this task Sema learns to understand the new AST. Implement the argument type
of `Some(e)`, the target type and bound-variable scope of `MatchStmt`, and the
determination of whether both arms return (return-coverage).

**Files:**
- Modify: `rwc/sema.py`
- Modify: `tests/test_sema.py`

- [ ] **Step 2.1: Handle `SomeExpr` / `NoneExpr` in Sema's `_check_expr`**

Read the `_check_expr` method in `rwc/sema.py` (around sema.py:298, near the
`isinstance(expr, A.BinOp)` branch). Add **directly below**
`isinstance(expr, A.SpawnExpr)`:

```python
        if isinstance(expr, A.SomeExpr):
            at = self._check_expr(fn, expr.arg, locals_)
            if at is not T.INT:
                raise CompileError(Diagnostic(
                    self.filename, expr.line, expr.col, 4,
                    f"Some argument must be int, found `{at}`",
                ))
            return T.OPTION_INT
        if isinstance(expr, A.NoneExpr):
            return T.OPTION_INT
```

Match the `expr.line` / `expr.col` field names to the AST definition from Step 1.3.

- [ ] **Step 2.2: Handle `MatchStmt` in Sema's `_check_stmt`**

Read the `_check_stmt` method around `rwc/sema.py:153`. Add **directly below**
`if isinstance(stmt, A.WhileStmt):`:

```python
        if isinstance(stmt, A.MatchStmt):
            tt = self._check_expr(fn, stmt.target, locals_)
            if tt is not T.OPTION_INT:
                raise CompileError(Diagnostic(
                    self.filename, stmt.line, stmt.col, 5,
                    f"match target must be Option[int], found `{tt}`",
                ))
            # Some arm: bind some_var as int in a new locals scope
            some_locals = dict(locals_)
            some_locals[stmt.some_var] = T.INT
            some_ret = self._check_block(fn, stmt.some_block, some_locals, ret_ty)
            # None arm: no binding
            none_ret = self._check_block(fn, stmt.none_block, dict(locals_), ret_ty)
            # match terminates in return iff both arms do
            return some_ret and none_ret
```

The return value of `_check_block` follows the existing convention: it is True
when the block exits via a `return` before reaching its end. Align this with how
`then_ret` / `else_ret` are used in `if/elif/else` (around sema.py:230).

- [ ] **Step 2.3: Add positive tests**

Add to the end of `tests/test_sema.py`:

```python
# ---- Option[int] positive cases ----

def test_some_int_returns_option_int():
    src = (
        "def main() -> int:\n"
        "    o: Option[int] = Some(5)\n"
        "    return 0\n"
    )
    check(src)


def test_none_returns_option_int():
    src = (
        "def main() -> int:\n"
        "    o: Option[int] = None\n"
        "    return 0\n"
    )
    check(src)


def test_function_returning_option_int_with_both_arms():
    src = (
        "def f(b: int) -> Option[int]:\n"
        "    if b == 0:\n"
        "        return None\n"
        "    return Some(1)\n"
        "def main() -> int:\n"
        "    return 0\n"
    )
    check(src)


def test_match_two_arms_ok():
    src = (
        "def main() -> int:\n"
        "    o: Option[int] = Some(7)\n"
        "    match o:\n"
        "        case Some(x):\n"
        "            print(x)\n"
        "        case None:\n"
        "            print(-1)\n"
        "    return 0\n"
    )
    check(src)


def test_match_some_bound_var_is_int():
    src = (
        "def main() -> int:\n"
        "    o: Option[int] = Some(7)\n"
        "    match o:\n"
        "        case Some(x):\n"
        "            y: int = x + 1\n"
        "            print(y)\n"
        "        case None:\n"
        "            print(-1)\n"
        "    return 0\n"
    )
    check(src)


def test_match_terminates_via_both_arms_return():
    # main has no `return` after match because match itself terminates
    # in both arms.
    src = (
        "def pick(b: int) -> int:\n"
        "    o: Option[int] = None\n"
        "    if b == 0:\n"
        "        o = None\n"
        "    else:\n"
        "        o = Some(b)\n"
        "    match o:\n"
        "        case Some(x):\n"
        "            return x\n"
        "        case None:\n"
        "            return -1\n"
        "def main() -> int:\n"
        "    return pick(7)\n"
    )
    check(src)
```

- [ ] **Step 2.4: Add negative tests**

Continue adding to `tests/test_sema.py`:

```python
# ---- Option[int] negative cases ----

def test_some_string_is_type_error():
    src = (
        "def main() -> int:\n"
        "    o: Option[int] = Some(\"hi\")\n"
        "    return 0\n"
    )
    e = err(src)
    assert "Some argument must be int" in e.diagnostic.message


def test_match_on_int_is_type_error():
    src = (
        "def main() -> int:\n"
        "    x: int = 5\n"
        "    match x:\n"
        "        case Some(v):\n"
        "            print(v)\n"
        "        case None:\n"
        "            print(0)\n"
        "    return 0\n"
    )
    e = err(src)
    assert "match target must be Option[int]" in e.diagnostic.message


def test_print_option_is_type_error():
    src = (
        "def main() -> int:\n"
        "    o: Option[int] = Some(1)\n"
        "    print(o)\n"
        "    return 0\n"
    )
    e = err(src)
    assert "print" in e.diagnostic.message


def test_option_eq_is_type_error():
    # Option[int] is intentionally not on the == whitelist
    src = (
        "def main() -> int:\n"
        "    a: Option[int] = Some(1)\n"
        "    b: Option[int] = None\n"
        "    if a == b:\n"
        "        return 0\n"
        "    return 1\n"
    )
    e = err(src)
    assert "compare" in e.diagnostic.message or "==" in e.diagnostic.message
```

- [ ] **Step 2.5: Run pytest**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw && uv run pytest tests/test_sema.py -q 2>&1 | tail -5
```

Expected: existing 41 + 6 positive + 4 negative = `51 passed`.
`test_match_terminates_via_both_arms_return` requires the return-coverage
implementation to look at both arms of the match. If it fails, re-check the
return-value logic of `_check_stmt` in Step 2.2.

The message in `test_print_option` assumes the current Sema returns "print does
not support `Option[int]`". If the actual string differs, loosen the assert.

- [ ] **Step 2.6: Confirm the full existing test suite is also green**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw && uv run pytest -q 2>&1 | tail -5
```

Expected: existing 101 + 3 from Task 1 + 10 from Task 2 = `114 passed`.

- [ ] **Step 2.7: Commit**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
git add rwc/sema.py tests/test_sema.py
git commit -m "$(cat <<'EOF'
rwc: sema for Some / None / match

Sema:
  - SomeExpr requires its arg to be int, returns Option[int].
  - NoneExpr returns Option[int].
  - MatchStmt requires target to be Option[int], binds the
    Some(x) pattern's identifier as int in the Some arm's scope,
    and treats the whole statement as "terminates in return" iff
    both arms terminate (same pattern as if/else return coverage).
  - Option[int] is deliberately absent from the == whitelist, so
    `a == None` style comparisons get caught at sema.

Tests: 10 new in test_sema.py covering Some/None typing, both-arms
match, Some-bound-as-int, return coverage via match, and four
negatives (Some("hi"), match on int, print(Option), Option == ).

irgen doesn't know how to emit Some/None/MatchStmt yet — code that
actually executes those forms will still crash at irgen until the
next commit.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Implement the runtime + irgen so Option values can run

This task makes it possible to emit IR for Some / None / match / list_at_opt.
This is the first point where rw code actually runs while handling Option values.

**Files:**
- Modify: `runtime/runtime.h`
- Modify: `runtime/runtime.c`
- Create: `runtime/fiber/test_option.c`
- Modify: `.gitignore`
- Modify: `rwc/sema.py` (add the `list_at_opt` builtin)
- Modify: `rwc/irgen.py`

### Runtime

- [ ] **Step 3.1: Add the `rw_option_int` struct and `rw_list_int_at_opt` prototype to `runtime.h`**

In `runtime/runtime.h`, add this **immediately below** the definition of the `rw_list_int` struct, before the `rw_list_int_new` family of prototypes:

```c
/* Option[int] type. Two-word fat struct: tag (0=None, 1=Some) +
 * payload (int value when Some). 16 bytes — fits in two registers,
 * so value-return ABI is safe (no pointer-out needed). */
typedef struct {
    int64_t tag;       /* 0 = None, 1 = Some */
    int64_t payload;
} rw_option_int;
```

Add this **immediately below** the `rw_list_int_len` prototype:

```c
/* List[int]: range-checked accessor returning Option[int]. */
void rw_list_int_at_opt(rw_option_int *out, const rw_list_int *l, int64_t i);
```

Leave `rw_list_int_at` unchanged (it still aborts).

- [ ] **Step 3.2: Add the `rw_list_int_at_opt` implementation to `runtime.c`**

Add this **immediately below** `rw_list_int_len` in `runtime/runtime.c`:

```c
void rw_list_int_at_opt(rw_option_int *out, const rw_list_int *l, int64_t i) {
    if (i < 0 || i >= l->len) {
        out->tag = 0;
        out->payload = 0;
        return;
    }
    out->tag = 1;
    out->payload = l->data[i];
}
```

- [ ] **Step 3.3: Create the C unit test `test_option.c`**

`/Users/ryuichi/ghq/github.com/ryuichi1208/rw/runtime/fiber/test_option.c`:

```c
/*
 * Unit test for rw_list_int_at_opt and the rw_option_int struct.
 */

#include "../runtime.h"

#include <assert.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>

int main(void) {
    /* Build a small list: [10, 20, 30] */
    rw_list_int l, l1, l2;
    rw_list_int_new(&l);
    rw_list_int_push(&l1, &l, 10);
    rw_list_int_push(&l2, &l1, 20);
    rw_list_int l3;
    rw_list_int_push(&l3, &l2, 30);

    /* In-range: Some */
    rw_option_int o;
    rw_list_int_at_opt(&o, &l3, 0);
    assert(o.tag == 1);
    assert(o.payload == 10);

    rw_list_int_at_opt(&o, &l3, 2);
    assert(o.tag == 1);
    assert(o.payload == 30);

    /* Out-of-range: None */
    rw_list_int_at_opt(&o, &l3, 3);
    assert(o.tag == 0);

    rw_list_int_at_opt(&o, &l3, -1);
    assert(o.tag == 0);

    /* Empty list: None for any index */
    rw_list_int empty;
    rw_list_int_new(&empty);
    rw_list_int_at_opt(&o, &empty, 0);
    assert(o.tag == 0);

    printf("all option tests passed\n");
    return 0;
}
```

- [ ] **Step 3.4: Add the binary to `.gitignore`**

Add this **immediately below** `runtime/fiber/test_list_int` in `/Users/ryuichi/ghq/github.com/ryuichi1208/rw/.gitignore`:

```
runtime/fiber/test_option
```

- [ ] **Step 3.5: Build the runtime and run the C test**

```sh
make -C /Users/ryuichi/ghq/github.com/ryuichi1208/rw/runtime clean
make -C /Users/ryuichi/ghq/github.com/ryuichi1208/rw/runtime
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw/runtime
cc -O2 -Wall -Wextra -std=c11 -D_GNU_SOURCE -pthread fiber/test_option.c librw.a -o fiber/test_option && ./fiber/test_option
```

Expected: a warning-free build + `all option tests passed`.

- [ ] **Step 3.6: Confirm the existing C tests are green, just to be safe**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw/runtime
cc -O2 -Wall -Wextra -std=c11 -D_GNU_SOURCE -pthread fiber/test_list_int.c librw.a -o fiber/test_list_int && ./fiber/test_list_int
cc -O2 -Wall -Wextra -std=c11 -D_GNU_SOURCE -pthread fiber/test_sched.c librw.a -o fiber/test_sched && ./fiber/test_sched
```

Expected: `all list_int tests passed` / `total = 333833500`.

### Sema for list_at_opt

- [ ] **Step 3.7: Add the `list_at_opt` builtin to Sema**

Add this **immediately below** the `list_at` builtin (within `_check_call`) in `rwc/sema.py`:

```python
        # Builtin: list_at_opt(List[int], int) -> Option[int].
        if call.callee == "list_at_opt":
            if len(call.args) != 2:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"list_at_opt takes 2 arguments, got {len(call.args)}",
                ))
            t0 = self._check_expr(fn, call.args[0], locals_)
            t1 = self._check_expr(fn, call.args[1], locals_)
            if t0 is not T.LIST_INT:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"list_at_opt first argument must be List[int], found `{t0}`",
                ))
            if t1 is not T.INT:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"list_at_opt second argument must be int, found `{t1}`",
                ))
            return T.OPTION_INT
```

Add to the SpawnExpr ban list (next to the `list_at` ban branch):

```python
                if call.callee == "list_at_opt":
                    raise CompileError(Diagnostic(
                        self.filename, expr.line, expr.col, 5,
                        "cannot spawn the builtin `list_at_opt`",
                    ))
```

### irgen

- [ ] **Step 3.8: Define `RW_OPTION_INT_TY` in irgen**

Near the top of `rwc/irgen.py`, **immediately below** `RW_LIST_INT_TY = ...`:

```python
RW_OPTION_INT_TY = ir.LiteralStructType([I64, I64])  # {tag, payload}
```

- [ ] **Step 3.9: Add Option[int] to `llvm_type_of`**

In `llvm_type_of`, **immediately below** `if t is T.LIST_INT:`:

```python
    if t is T.OPTION_INT:
        return RW_OPTION_INT_TY
```

- [ ] **Step 3.10: Add `rw_list_int_at_opt` to `_declare_runtime`**

Inside `_declare_runtime`, **immediately below** the List ops declarations:

```python
        # Option[int] ops — pointer-out for the output struct, matching
        # the List helpers' calling convention.
        option_ptr = RW_OPTION_INT_TY.as_pointer()
        self._rw_list_int_at_opt = ir.Function(
            m, ir.FunctionType(ir.VoidType(),
                               [option_ptr, RW_LIST_INT_TY.as_pointer(), I64]),
            "rw_list_int_at_opt")
```

- [ ] **Step 3.11: Handle `SomeExpr` / `NoneExpr` in irgen's `_emit_expr`**

In `_emit_expr`, add this alongside the existing branches such as `if isinstance(expr, A.SpawnExpr):`:

```python
        if isinstance(expr, A.SomeExpr):
            v = self._emit_expr(expr.arg, ctx)
            base = ir.Constant(RW_OPTION_INT_TY,
                               [ir.Constant(I64, 1), ir.Constant(I64, 0)])
            return ctx.builder.insert_value(base, v, 1)
        if isinstance(expr, A.NoneExpr):
            return ir.Constant(RW_OPTION_INT_TY,
                               [ir.Constant(I64, 0), ir.Constant(I64, 0)])
```

- [ ] **Step 3.12: Handle `MatchStmt` in irgen's `_emit_stmt`**

In `_emit_stmt` (around irgen.py:217), add this **immediately below**
`if isinstance(stmt, A.WhileStmt):`:

```python
        if isinstance(stmt, A.MatchStmt):
            self._emit_match(stmt, ctx)
            return
```

Add `_emit_match` next to `_emit_while`:

```python
    def _emit_match(self, stmt: A.MatchStmt, ctx: "FunctionCtx") -> None:
        b = ctx.builder
        v = self._emit_expr(stmt.target, ctx)
        tag = b.extract_value(v, 0)
        payload = b.extract_value(v, 1)

        some_bb = ctx.function.append_basic_block("match.some")
        none_bb = ctx.function.append_basic_block("match.none")
        end_bb = ctx.function.append_basic_block("match.end")

        sw = b.switch(tag, none_bb)
        sw.add_case(ir.Constant(I64, 1), some_bb)

        # Some arm
        b.position_at_end(some_bb)
        slot = b.alloca(I64, name=stmt.some_var)
        b.store(payload, slot)
        saved = ctx.locals.get(stmt.some_var)
        ctx.locals[stmt.some_var] = slot
        self._emit_block(stmt.some_block, ctx)
        if not b.block.is_terminated:
            b.branch(end_bb)
        if saved is not None:
            ctx.locals[stmt.some_var] = saved
        else:
            ctx.locals.pop(stmt.some_var, None)

        # None arm
        b.position_at_end(none_bb)
        self._emit_block(stmt.none_block, ctx)
        if not b.block.is_terminated:
            b.branch(end_bb)

        b.position_at_end(end_bb)
```

How to obtain `ctx.builder.block.is_terminated` depends on the llvmlite version.
Read the existing `_emit_if` (if any) to see how it makes this determination and
match that style. If there is none, try `b.block.is_terminated` and make it work.

- [ ] **Step 3.13: Handle `list_at_opt` in irgen's `_emit_call`**

In `_emit_call`, add this **immediately below** the existing `list_at` branch:

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

### Smoke

- [ ] **Step 3.14: Standalone IR + execution smoke test**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
cat > /tmp/option_smoke.rw <<'EOF'
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
EOF
uv run rwc emit-ir /tmp/option_smoke.rw 2>&1 | grep -E "switch|insertvalue|extractvalue" | head -10
echo "---"
uv run rwc run /tmp/option_smoke.rw
```

Expected:

The IR contains the `switch`, `insertvalue`, and `extractvalue` instructions.

Execution result:
```
5
-1
```

If `exit != 0` (especially 139 = SIGSEGV), it is either an ABI problem or a match
lowering bug. `Option[int]` is 16 bytes, so pointer-out should not be needed, but
just in case, refer to the verification steps in the `llvm-ir-c-abi` skill.

- [ ] **Step 3.15: Run the full pytest suite**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw && uv run pytest -q 2>&1 | tail -5
```

Expected: existing 114 + (no tests added in this task) = `114 passed`.

- [ ] **Step 3.16: Commit**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
git add runtime/runtime.h runtime/runtime.c runtime/fiber/test_option.c .gitignore rwc/sema.py rwc/irgen.py
git commit -m "$(cat <<'EOF'
rwc + runtime: emit IR for Some / None / match, add list_at_opt

Runtime:
  - Add rw_option_int struct {tag, payload} (16 bytes, value-return
    safe — no pointer-out ABI quirks like the 24-byte rw_list_int).
  - Add rw_list_int_at_opt(out*, l*, i): returns Some(value) when
    in range, None when out of bounds. The old rw_list_int_at is
    preserved unchanged (still aborts) for backward compatibility.
  - C unit test covers in-range Some, out-of-range / negative /
    empty-list None.

Sema:
  - Add list_at_opt(List[int], int) -> Option[int] builtin and
    forbid spawn of it.

irgen:
  - RW_OPTION_INT_TY = {i64 tag, i64 payload}.
  - llvm_type_of(T.OPTION_INT) -> RW_OPTION_INT_TY.
  - SomeExpr: insertvalue {1, 0} with arg into payload slot.
  - NoneExpr: constant {0, 0}.
  - MatchStmt: extract_value tag + payload, switch on tag to the
    Some / None basic blocks, alloca-and-store for the bound var,
    branch to a common end block for falls-through arms.
  - list_at_opt call: pointer-out shim (alloca input list, alloca
    output option, call, load result).

Smoke:
  RW_WORKERS=1 uv run rwc run examples/option_basic.rw
  -> 5\n-1\n (matches .expected from next commit)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: example + e2e

**Files:**
- Create: `examples/option_basic.rw`
- Create: `examples/option_basic.rw.expected`
- Modify: `tests/test_e2e.py`

- [ ] **Step 4.1: Write `examples/option_basic.rw`**

`/Users/ryuichi/ghq/github.com/ryuichi1208/rw/examples/option_basic.rw`:

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

- [ ] **Step 4.2: Write `examples/option_basic.rw.expected`**

```
5
-1
```

(Save with a trailing newline.)

- [ ] **Step 4.3: Confirm a byte-for-byte match locally**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
diff <(RW_WORKERS=1 uv run rwc run examples/option_basic.rw 2>&1) examples/option_basic.rw.expected && echo OK
```

Expected: only `OK` is printed.

- [ ] **Step 4.4: Add `option_basic` to the parametrize list in `tests/test_e2e.py`**

The following line at `tests/test_e2e.py:45`:

```python
    ["hello", "arith", "fib", "while_count", "spawn_basic", "spawn_many", "spawn_string", "string_ops", "bytes_basic", "list_basic"],
```

to the following:

```python
    ["hello", "arith", "fib", "while_count", "spawn_basic", "spawn_many", "spawn_string", "string_ops", "bytes_basic", "list_basic", "option_basic"],
```

- [ ] **Step 4.5: Run the full pytest suite**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw && uv run pytest -q 2>&1 | tail -5
```

Expected: 114 + 1 = `115 passed`.

- [ ] **Step 4.6: Regression-check the existing examples**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
RW_WORKERS=1 uv run rwc run examples/hello.rw
RW_WORKERS=1 uv run rwc run examples/list_basic.rw
RW_WORKERS=1 uv run rwc run examples/string_ops.rw
RW_WORKERS=1 uv run rwc run examples/bytes_basic.rw
RW_WORKERS=1 uv run rwc run examples/spawn_many.rw
```

Expected:
```
hello
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

- [ ] **Step 4.7: Confirm the runtime unit tests are green too**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw/runtime
make clean && make
cc -O2 -Wall -Wextra -std=c11 -D_GNU_SOURCE -pthread fiber/test_option.c librw.a -o fiber/test_option && ./fiber/test_option
cc -O2 -Wall -Wextra -std=c11 -D_GNU_SOURCE -pthread fiber/test_list_int.c librw.a -o fiber/test_list_int && ./fiber/test_list_int
cc -O2 -Wall -Wextra -std=c11 -D_GNU_SOURCE -pthread fiber/test_sched.c librw.a -o fiber/test_sched && ./fiber/test_sched
```

Expected: `all option tests passed` / `all list_int tests passed` /
`total = 333833500`.

- [ ] **Step 4.8: Commit**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
git add examples/option_basic.rw examples/option_basic.rw.expected tests/test_e2e.py
git commit -m "$(cat <<'EOF'
examples: add option_basic exercising Some / None / match

examples/option_basic.rw exercises the full Option[int] surface
in a single program: a safe_div helper that returns Some(quotient)
when b != 0 and None when b == 0, plus two match statements that
print the value (Some arm) or a sentinel -1 (None arm).

The .expected captures the byte-for-byte stdout (5 then -1), and
tests/test_e2e.py picks it up via the existing parametrize list,
so any regression in lexer / parser / sema / irgen / runtime
flows that touch Some / None / match / list_at_opt will fail the
suite.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review

### Spec coverage

| Spec requirement | Covering tasks |
|---|---|
| New type `Option[int]` (keyword `Option`, parser enforces `[int]`) | Task 1.1 / 1.2 / 1.4 |
| `Some(int) -> Option[int]` expression | Task 1.5 (parser), 2.1 (sema), 3.11 (irgen) |
| `None` literal | Task 1.2 (lexer KW_NONE), 1.5 (parser), 2.1 (sema), 3.11 (irgen) |
| `match v: case Some(x): ... case None: ...` syntax | Task 1.6 (parser), 2.2 (sema), 3.12 (irgen) |
| Both arms required (parser) | Task 1.6 (mandatory-both-arms check) + test 1.9 |
| Some arm's bound variable is int | Task 2.2 (sema scope) + test 2.3 |
| Return-coverage looks at both match arms | Task 2.2 (`some_ret and none_ret`) + test 2.3 |
| `list_at_opt(List[int], int) -> Option[int]` builtin | Task 3.1 / 3.2 (runtime), 3.7 (sema), 3.10 / 3.13 (irgen) |
| Runtime helper returns None when out of range | Task 3.2 + C test 3.3 |
| Internal representation is a 16-byte value return | Task 3.1 (struct), 3.8 (`RW_OPTION_INT_TY`) — no pointer-out needed |
| `Option[string]` etc. is a parser error | Task 1.4 + test 1.9 |
| A missing `match` arm is a parser error | Task 1.6 + test 1.9 |
| `print(Option[int])` is a type error | Automatic by not changing `is_printable` + test 2.4 |
| `Option == Option` is a type error | Automatic by not changing the == whitelist + test 2.4 |
| `Some(string)` is a type error | Task 2.1 + test 2.4 |
| A `match` target that is not Option[int] is a type error | Task 2.2 + test 2.4 |
| `Future[Option[int]]` forbidden | Documented in the spec; just as LIST_INT is not added to `_decl_spawn` / `_decl_await`, OPTION_INT is likewise not added ("deliberately left untouched" throughout Task 3) |
| Existing 101 tests green | Task 1.8, 2.6, 3.15, 4.5 |

Every spec requirement has a task.

### Placeholder scan

"TBD", "TODO", "(to be confirmed)", "fill in", "Add appropriate", and "Similar to
Task N" appear 0 times in the plan. The note at the end of Step 1.10 ("adjust the
shape to instead expect a ParserError"), Step 2.5 ("if the actual string differs,
loosen the assert"), and Step 3.12 ("depends on the llvmlite version") each
**explicitly spell out an alternative procedure within the plan**, so they are not
placeholders.

### Type consistency

- `T.OPTION_INT` matches exactly across Task 1.1 / 1.7 / 2.1 / 2.2 / 3.7 / 3.9
- The LLVM representation `RW_OPTION_INT_TY = LiteralStructType([I64, I64])` is consistent across Task 3.8 / 3.9 / 3.10 / 3.11 / 3.12 / 3.13
- Runtime function: `rw_list_int_at_opt(out*, l*, i)` matches exactly across Task 3.1 (declaration) / 3.2 (implementation) / 3.3 (C test) / 3.10 (irgen declaration) / 3.13 (irgen call)
- AST node names: `SomeExpr` / `NoneExpr` / `MatchStmt` are used consistently across Task 1.3 (definition) / 1.5 / 1.6 (parser) / 2.1 / 2.2 (sema) / 3.11 / 3.12 (irgen)
- Sema type-error messages: `"Some argument must be int"` / `"match target must be Option[int]"` / `"list_at_opt first argument must be List[int]"` / `"list_at_opt second argument must be int"` match exactly between the implementation (Task 2.1, 2.2, 3.7) and the negative-test asserts (Task 2.4)
- Management of the Some arm's bound variable: `some_locals[stmt.some_var] = T.INT` in Task 2.2, `ctx.locals[stmt.some_var] = slot` in Task 3.12. The same field name `some_var` is used consistently
