# rw conditional expression / ternary operator (`then if cond else els`)

## Context

rw is a small language whose compiler, runtime, examples, and tests grow
together. It has accumulated types (string / Bytes / List[int] / Option /
Result), concurrency/I/O, and control flow (`if` / `while` / `for ... in
range`) at a granularity of one sub-project per PR.

This sub-project corresponds to the fourth item of the control-flow roadmap
presented in the `for ... in range` spec ([[13-for-range-loop]]): "using `if`
as an expression / ternary equivalent". Until now, "choosing a value based on a
condition" required a statement `if`/`else` that assigns to a variable first:

```rw
larger: int = 0
if a > b:
    larger = a
else:
    larger = b
```

This sub-project introduces the Python-compatible conditional expression `then
if cond else els`, so that the above can be written in a single-line expression:

```rw
larger: int = a if a > b else b
```

The core of the design is to **reuse the cbranch + phi IR pattern that
short-circuit `and` / `or` already use**. The only new node is a single
expression (`IfExpr`); no desugar and no lexer change are needed. The change is
confined to parser / sema / irgen + tests and examples.

## Goals

- Introduce a new expression `then if cond else els` (identical syntax to
  Python's ternary operator)
- The ternary operator has the **lowest precedence**. It can be used in any
  position where an expression is allowed, such as the right-hand side of an
  assignment, function arguments, or a `return` value, as in
  `x: int = 1 if c else 2`
- Require `cond` to be `bool` and `then` / `els` to be **the same type**, and
  use that type as the type of the whole expression
- Nesting is **right-associative**: `a if p else b if q else c` is interpreted
  as `a if p else (b if q else c)`
- Evaluation is short-circuiting: only the selected branch is evaluated (cbranch
  ensures the other is not executed)

## Non-Goals

- **if expressions / block expressions** (expression-oriented syntax, #109 RFC).
  A mechanism for statement blocks to return values as expressions is a separate
  matter and is not addressed here
- **C-style ternary symbol syntax** `cond ? a : b`. Only the Python-compatible
  `a if c else b`
- **Implicit type promotion between branches** (`int` ↔ `float`, etc.). Both
  branches must have the same type. Mixing them is a type error in sema
- **Constant folding or condition simplification of nesting**. Generate it
  straightforwardly with cbranch + phi and delegate optimization to LLVM (and to
  the future optimization level #55)

## Syntax

```
expr     := ternary
ternary  := or_expr [ "if" or_expr "else" ternary ]   # right-associative
```

- `if` / `else` are already reserved in the lexer (`KW_IF` / `KW_ELSE`). **No
  lexer change is needed**
- The `then` side and the `cond` side read `or_expr` (higher precedence than the
  ternary), while the `else` side reads `ternary` again, making it
  right-associative
- The ternary operator is handled in the `parse_ternary` layer between
  `parse_expr` and `parse_or`

### Non-collision with the statement `if`

The statement `if` (a compound statement beginning with `if cond:`) and the
expression `if` do not collide for the following reasons:

- The statement `if` branches only in the **line-head dispatch** of `parse_stmt`
- The expression `if` appears only inside `parse_ternary`, that is, **only after
  expression parsing has begun**

Therefore the `if` in `x: int = 1 if c else 2` is interpreted as an expression,
and the line-head `if c:` as a statement, each without ambiguity.

### Usage example

```rw
def classify(n: int) -> int:
    # nested conditional expression (right-associative)
    return 1 if n > 0 else 0 if n == 0 else -1

def main() -> int:
    a: int = 10
    b: int = 20
    larger: int = a if a > b else b            # int branch
    label: string = "even" if a % 2 == 0 else "odd"  # string branch
    ok: bool = true if larger == 20 else false       # bool branch
    print(larger)            # 20
    print(label)             # even
    print(ok)                # true
    print(classify(-3))      # -1
    return 0
```

## Typing (sema)

The `IfExpr` branch of `_infer_expr` checks the following:

1. Infer the type of `cond`; if it is not `bool`, raise a type error
   (`conditional expression requires bool condition, found ...`)
2. Infer the types of `then` / `els`; if the two **do not match**, raise a type
   error
   (`conditional expression branches must have the same type, found ... and ...`)
3. Return the matched type as the type of the whole expression. Via
   `_check_expr` it is registered in `expr_types[id(expr)]`, which irgen uses to
   determine the phi type

## IR generation (irgen)

Lower `IfExpr` into **cbranch + phi** with the same shape as short-circuit `and`
/ `or`:

```
  cond_i1 = (cond != 0)            ; bool is i8, converted to i1 via !=0
  br i1 cond_i1, label tern.then, label tern.else
tern.then:
  <evaluate then>
  br label tern.end
tern.else:
  <evaluate els>
  br label tern.end
tern.end:
  %r = phi <ty> [ then_val, tern.then ], [ else_val, tern.else ]
```

- The phi type is taken from `llvm_type_of(sema.expr_types[id(expr)])`. `and` /
  `or` always produce a fixed `i8` result, but the ternary can take any of int /
  float / bool / string, so the only difference is that it uses the type sema
  inferred
- Only the selected branch is executed (short-circuiting via cbranch)

## Layers touched

| Layer | File | Change |
|---|---|---|
| Lexer | `rwc/lexer.py` | `KW_IF` / `KW_ELSE` are already reserved. **Unchanged** |
| AST | `rwc/ast_nodes.py` | Add `IfExpr` node (then, cond, els). Add to the `Expr` Union |
| Parser | `rwc/parser.py` | Insert `parse_ternary()` between `parse_expr` and `parse_or`. Right-associative |
| Sema | `rwc/sema.py` | Add an `IfExpr` branch to `_infer_expr` (cond is bool, both branches same type) |
| irgen | `rwc/irgen.py` | Add an `IfExpr` branch to `_emit_expr` + `_emit_if_expr` (cbranch + phi) |
| Runtime | `runtime/` | **Unchanged** |
| Desugar | `rwc/desugar.py` | **Unchanged** (no desugar needed) |
| Examples | `examples/ternary.rw` (+ `.expected`) | 1 new example |
| Tests | `tests/test_*.py` | Unit tests for parser / sema / irgen; add `ternary` to the e2e parametrize |

This stays within the "up to 4 layers per PR" of
`incremental-language-extensions` (parser / sema / irgen + examples).

## Verification

- Build the example `examples/ternary.rw` with `rwc build`, run it, and check it
  matches `.expected`
- positive (parser): `1 if c else 2` parses into an `IfExpr`, nesting is
  right-associative
- positive (sema): same-type branches of int / string / bool pass
- positive (irgen): `br i1` and `phi` appear in the generated IR
- negative (sema): non-bool `cond` → type error, mismatched branch types → type
  error
- negative (parser): missing `else` → syntax error

## Risks and mitigations

- **Confusing the statement if with the expression if**: the statement `if` is
  line-head dispatch, and the expression `if` appears only inside
  `parse_ternary`, so they do not collide. Parser tests also guarantee that the
  existing `if` statement is not broken
- **phi type mismatch**: the phi type is taken not from a fixed value but from
  the type sema inferred (`expr_types`). Since sema guarantees that both branches
  have the same type, the then-side value and the else-side value have the same
  LLVM type
- **The "while we're at it" temptation**: do not touch the if-expression RFC
  (#109) or constant folding (explicitly stated in Non-Goals)
