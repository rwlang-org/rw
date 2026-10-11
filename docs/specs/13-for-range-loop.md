# rw `for ... in range(...)` loop (syntactic sugar, desugared to while)

## Context

rw is a small language whose compiler, runtime, examples, and tests grow
together. So far it has accumulated types (string / Bytes / List[int] / Option
/ Result) and concurrency/I/O (fibers / scheduler / netpoller + TCP) at a
granularity of one sub-project per PR.

Control flow, on the other hand, has not been extended beyond the initial `if`
/ `while`, so the most frequently used "counting loop" requires writing the
following boilerplate by hand every time:

```rw
i: int = 0
while i < n:
    # body
    i = i + 1
```

This sub-project introduces `for <var> in range(...)` as the first step of the
control-flow enhancement roadmap. The core of the design is to **not create a
type that holds `range` as a value; instead treat it as a `for`-specific
syntactic element and desugar it into a `while` loop in sema**. This keeps the
ripple effect on the type system (`types.py`) at zero and confines the change
to the three layers lexer / parser / sema plus the examples.

Control-flow roadmap (this sub-project is the first of it):

1. **This sub-project**: `for ... in range(start, stop[, step])`
2. `break` / `continue` (shared between `for` / `while`)
3. Iterator forms such as `for <var> in <list>`
4. Using `if` as an expression / ternary equivalent

## Goals

- Introduce a new statement `for <ident> in range(<args>):`
  - `range(stop)` — start=0, step=1
  - `range(start, stop)` — step=1
  - `range(start, stop, step)`
- start / stop / step are **arbitrary `int` expressions** (including variables
  and function calls)
- Half-open interval `[start, stop)`; the loop variable is an `int` scoped to
  the loop body
- Support negative `step` (iterate in descending order until falling below
  `stop`)
- `step == 0` means **zero iterations** (terminate immediately without
  executing the body). This exploits the property that the desugared loop
  condition makes both sides false when step==0
- Desugar the `for` node into a `while` + assignment AST; sema / irgen reuse the
  existing `while` handling as-is (both layers unchanged)

## Non-Goals

- **Treating `range` as a value**. `x = range(0, 10)` or passing `range` as a
  function argument is a syntax error. `range` is only accepted immediately
  after `for ... in` (the parser raises the error "`range` can only appear in a
  for-loop header")
- The iterator form `for <var> in <list/string/Bytes>` (separate PR)
- `break` / `continue` (separate PR, the second item of the control-flow
  roadmap)
- Using `range` with types other than `int` (`List`, etc.)
- Strict checking that forbids reassignment of the loop variable (handled
  minimally)
- Condition simplification via constant folding of `step` (delegated to LLVM's
  optimizer)

## Syntax

```
for_stmt   := "for" IDENT "in" "range" "(" range_args ")" ":" NEWLINE block
range_args := expr                       # stop
            | expr "," expr              # start, stop
            | expr "," expr "," expr     # start, stop, step
```

`range` is not made a keyword (the parser matches the identifier `range` at the
for-header position). Making it a keyword would have the side effect of
prohibiting `range` as a variable name, so it is handled via identifier
matching. `for` / `in` are already reserved in the lexer (`KW_FOR` / `KW_IN`).

### Usage example

```rw
def main() -> int:
    total: int = 0
    for i in range(0, 10):       # 0,1,...,9
        total = total + i
    for j in range(10, 0, -1):   # 10,9,...,1
        total = total + j
    return total
```

## Internal design: desugaring to while in sema

sema transforms `for v in range(a, b, s):` into an AST equivalent to the
following. To **prevent double evaluation, the arguments are bound to temporary
variables** (so that start/stop/step are evaluated only once even when they are
function calls with side effects):

```
__stop = b
__step = s
v = a                     # loop variable (user-visible, int)
while (__step > 0 and v < __stop) or (__step < 0 and v > __stop):
    <body>
    v = v + __step
```

- The temporary variable names are internal names that do not collide with user
  identifiers (`__for_stop_N`, etc., where N is a sequence number)
- The loop condition is a general form that branches on both sides based on the
  sign of step. No constant folding is done; it is delegated to LLVM's optimizer
  (Non-Goal)
- `range(stop)` / `range(start, stop)` are completed by filling in the missing
  arguments with `0` / `1` literal nodes before expanding into the above form
- After desugaring, everything is an existing AST node (`While` / `Assign` /
  `BinOp` / `If`), so sema / irgen work without modification

## Behavior when step == 0

Because `step` is an arbitrary expression, there are cases where it cannot be
determined to be 0 at compile time. No trap is set up; instead the property that
the desugared loop condition `(step>0 and v<stop) or (step<0 and v>stop)` makes
both sides false when step==0 is exploited directly, treating it as **zero
iterations** (terminate immediately without executing the body). This means the
runtime needs no modification.

## Layers touched

| Layer | File | Change |
|---|---|---|
| Lexer | `rwc/lexer.py` | `KW_FOR` / `KW_IN` are already reserved (L70-71, L129-130). No addition needed |
| AST | `rwc/ast_nodes.py` | Add `For` node (var, start, stop, step, body). Add to the `Stmt` Union |
| Parser | `rwc/parser.py` | Add `parse_for()`, add `KW_FOR` to the statement dispatch. Accept `range(...)` only at the for-header position |
| Desugar | `rwc/desugar.py` (new) | Independent pass that runs right after the parser and before sema. Rewrites `For` into `VarDecl`/`While`/`Assign`. Insert one line into the CLI pipeline |
| Sema | `rwc/sema.py` | **Unchanged**. Since only existing nodes remain after desugaring, type checking and `local_types` registration work automatically |
| irgen | `rwc/irgen.py` | **Unchanged**. Processes the desugared while as-is |
| Runtime | `runtime/` | **Unchanged**. step==0 is handled as zero iterations |
| Examples | `examples/for_count.rw` (+ `.expected`) | 1 new example |
| Tests | `tests/test_e2e.py` and others | Add `for_count` to parametrize. Unit tests for parser/desugar |

Effectively AST / parser / desugar (new) + examples, with sema / irgen / runtime
unchanged. This stays within the "up to 4 layers per PR" of
`incremental-language-extensions`.

## Verification

- Build the example `examples/for_count.rw` with `rwc build`, run it, and check
  it matches `.expected`
- Positive tests:
  - Ascending `range(0, 5)` yields 0..4
  - `range(5)` (1 argument) yields 0..4
  - Descending `range(5, 0, -1)` yields 5..1
  - step=2 `range(0, 10, 2)` yields 0,2,4,6,8
  - Empty loops `range(5, 5)` / `range(0, -3)` execute the body 0 times
  - step==0 `range(0, 10, 0)` executes the body 0 times (immediate termination)
  - Using variables and expressions as range arguments
- Negative tests:
  - `range(...)` outside `for` → syntax error
  - `x = range(0, 5)` → syntax error
  - Non-int range argument → type error (sema)
  - `range()` with 0 arguments / 4 or more arguments → syntax error
- Desugar unit test: the `For` node expands into `VarDecl`/`While`/`Assign`

## Risks and mitigations

- **Double evaluation**: avoided by binding start/stop/step to temporary
  variables (the desugaring above)
- **Internal name collision**: separated from user identifiers by numbered
  internal names (`__for_stop_N`)
- **Boundary with negative step**: split the condition into both sides as
  `(step>0 and v<stop) or (step<0 and v>stop)`, terminating correctly based on
  the sign of step
- **step==0**: the above condition makes both sides false and terminates safely
  as zero iterations (no trap needed)
- **Missing desugar insertion**: call desugar right after parse on all three
  paths `compile_source` / `emit_ir` / `emit_ast` (driver.py). If it is missed,
  a raw for is passed to irgen and becomes "unknown stmt", so this is guaranteed
  by tests that exercise all three paths end-to-end
