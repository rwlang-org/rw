# String Builtins (len, ==, +) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the minimal string operations `len(s)` / `s == s` / `s + s` to the rw language, laying the groundwork for writing an echo server once the netpoller is integrated later.

**Architecture:** Add three C ABI helpers to the runtime (`rw_str_len` / `rw_str_eq` / `rw_str_concat`), extend Sema's builtin-function table and the binary-operator type rules, and emit IR from irgen that calls these helpers. The public ABI (`rw_spawn_*`, `rw_str`) is unchanged.

**Tech Stack:** C11 (runtime), Python 3.12 + llvmlite (compiler), pytest (tests).

**Spec:** `docs/specs/07-string-builtins.md`

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `runtime/runtime.h` | ABI declarations | Add 3 lines |
| `runtime/runtime.c` | 3 helper implementations | Add 3 functions |
| `runtime/fiber/test_str_ops.c` | C-level unit test | New |
| `runtime/Makefile` | (unchanged — `librw.a` uses the same OBJS) | — |
| `rwc/sema.py` | Builtin functions / operator type rules | 3 edits |
| `rwc/irgen.py` | Emit IR that calls the helpers | 3 edits |
| `tests/test_sema.py` | Positive/negative type-checking | Add tests |
| `tests/test_e2e.py` | Add string_ops to the parametrize list | Add 1 line |
| `examples/string_ops.rw` | Sample showing every feature in a single main | New |
| `examples/string_ops.rw.expected` | Expected output | New |
| `.gitignore` | Ignore the C test binary | Add 1 line |

---

## Task 1: Three runtime helper functions

**Files:**
- Modify: `runtime/runtime.h` (add prototypes)
- Modify: `runtime/runtime.c` (add implementations)

- [ ] **Step 1.1: Add the three prototypes to `runtime.h`**

Add them at the end of the `/* string helper */` section, immediately below the `/* print */` block in `runtime/runtime.h`:

```c
/* string ops (Commit added by string-builtins PR) */
int64_t rw_str_len   (rw_str s);
int8_t  rw_str_eq    (rw_str a, rw_str b);
rw_str  rw_str_concat(rw_str a, rw_str b);
```

- [ ] **Step 1.2: Add the implementations to `runtime.c`**

Add them immediately below the `/* ---------- string helper ---------- */` section and above `/* ---------- lifecycle ---------- */` in `runtime/runtime.c`:

```c
/* ---------- string ops ---------- */

int64_t rw_str_len(rw_str s) {
    return s.len;
}

int8_t rw_str_eq(rw_str a, rw_str b) {
    if (a.len != b.len) return 0;
    if (a.len == 0) return 1;  /* both empty: equal */
    return (int8_t)(memcmp(a.ptr, b.ptr, (size_t)a.len) == 0);
}

rw_str rw_str_concat(rw_str a, rw_str b) {
    rw_str out;
    out.len = a.len + b.len;
    if (out.len == 0) {
        out.ptr = NULL;
        return out;
    }
    char *p = (char *)malloc((size_t)out.len);
    if (!p) {
        /* OOM: degrade to empty string rather than crash. */
        out.len = 0;
        out.ptr = NULL;
        return out;
    }
    if (a.len > 0) memcpy(p, a.ptr, (size_t)a.len);
    if (b.len > 0) memcpy(p + a.len, b.ptr, (size_t)b.len);
    out.ptr = p;
    return out;
}
```

- [ ] **Step 1.3: Verify the existing C tests still pass**

```sh
make -C runtime clean && make -C runtime
```

Expected: `librw.a` builds without errors (three new symbols added, no warnings).

```sh
cd runtime
cc -O2 -Wall -Wextra -std=c11 -D_GNU_SOURCE -pthread fiber/test_sched.c fiber/fiber.o fiber/fiber_arm64.o fiber/sched.o fiber/runq.o fiber/park.o -o fiber/test_sched && ./fiber/test_sched
```

Expected: `total = 333833500` / `expected = 333833500`。

- [ ] **Step 1.4: Write the C test `test_str_ops.c`**

Create `runtime/fiber/test_str_ops.c`:

```c
/*
 * Unit test for rw_str_len / rw_str_eq / rw_str_concat.
 *
 * These helpers are pure functions of their inputs; we don't need
 * the scheduler at all here.
 */

#include "../runtime.h"

#include <assert.h>
#include <stdio.h>
#include <string.h>

static rw_str lit(const char *s) {
    rw_str r;
    r.len = (int64_t)strlen(s);
    r.ptr = s;
    return r;
}

static rw_str empty(void) {
    rw_str r = { .len = 0, .ptr = NULL };
    return r;
}

int main(void) {
    /* len */
    assert(rw_str_len(lit("hello")) == 5);
    assert(rw_str_len(empty()) == 0);
    assert(rw_str_len(lit("")) == 0);

    /* eq */
    assert(rw_str_eq(lit("hello"), lit("hello")) == 1);
    assert(rw_str_eq(lit("hello"), lit("world")) == 0);
    assert(rw_str_eq(lit("hello"), lit("hell")) == 0);
    assert(rw_str_eq(empty(), empty()) == 1);
    assert(rw_str_eq(empty(), lit("")) == 1);
    assert(rw_str_eq(lit("hello"), empty()) == 0);

    /* concat */
    {
        rw_str c = rw_str_concat(lit("foo"), lit("bar"));
        assert(c.len == 6);
        assert(memcmp(c.ptr, "foobar", 6) == 0);
        /* leak: not freed (by design) */
    }
    {
        rw_str c = rw_str_concat(empty(), lit("x"));
        assert(c.len == 1);
        assert(c.ptr[0] == 'x');
    }
    {
        rw_str c = rw_str_concat(lit("x"), empty());
        assert(c.len == 1);
        assert(c.ptr[0] == 'x');
    }
    {
        rw_str c = rw_str_concat(empty(), empty());
        assert(c.len == 0);
        assert(c.ptr == NULL);
    }
    printf("all str_ops tests passed\n");
    return 0;
}
```

- [ ] **Step 1.5: Add test_str_ops to `.gitignore`**

Add a line below `runtime/fiber/test_shutdown` in `/Users/ryuichi/ghq/github.com/ryuichi1208/rw/.gitignore`:

```
runtime/fiber/test_str_ops
```

- [ ] **Step 1.6: Build and run**

```sh
cd runtime && cc -O2 -Wall -Wextra -std=c11 -D_GNU_SOURCE fiber/test_str_ops.c runtime.o -o fiber/test_str_ops && ./fiber/test_str_ops
```

Expected: `all str_ops tests passed`.

- [ ] **Step 1.7: Verify the full existing test suite is green**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw && uv run pytest -q
```

Expected: `66 passed`.

- [ ] **Step 1.8: Commit**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
git add runtime/runtime.h runtime/runtime.c runtime/fiber/test_str_ops.c .gitignore
git commit -m "$(cat <<'EOF'
runtime: add rw_str_len / rw_str_eq / rw_str_concat

Three pure helpers that operate on the existing rw_str (len, ptr)
fat pointer:
  - rw_str_len  -> int64_t                    (just returns .len)
  - rw_str_eq   -> int8_t (0/1)               (memcmp under same-length)
  - rw_str_concat -> rw_str (malloc'd buffer) (leak-allowed; see spec)

Edge cases covered in fiber/test_str_ops.c:
  - empty <-> non-empty equality
  - concat with one or both sides empty (no malloc(0))
  - NULL ptr (treated as empty)

No public ABI changes: the existing rw_spawn_* / rw_await_* / rw_str
shape is unchanged.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Extend the compiler (sema + irgen)

**Files:**
- Modify: `rwc/sema.py` (lines around 290-345 = binop check, around 378 = call check)
- Modify: `rwc/irgen.py` (lines around 68-71 = decls, 289-366 = binop, 368+ = call)
- Modify: `tests/test_sema.py`

### Sema side

- [ ] **Step 2.1: Review the current state of Sema**

There is a binary-operator type check around `rwc/sema.py:289-345`. `==` / `!=` between two strings is rejected with `"string equality not supported in MVP"` (around line 322). `+` is allowed only for int/float. Builtin functions are hardcoded around line 378 with `print` only.

- [ ] **Step 2.2: Allow `+` / `==` / `!=` on strings in Sema**

Take the branch in `rwc/sema.py` that returns the "string equality not supported in MVP" error (around sema.py:322):

```python
                    # We could allow string equality later; disallow for MVP simplicity.
                    raise CompileError(Diagnostic(
                        self.filename, expr.line, expr.col, len(op),
                        "string equality not supported in MVP",
                    ))
```

**Delete** it and change the code so that `==`/`!=` between two strings passes as bool. Concretely, rewrite the `==`/`!=` handler into the following form (open sema.py with Read at implementation time to match the surrounding context):

```python
            # equality on int / float / bool / string
            if op in ("==", "!="):
                if lt == rt and lt in (T.INT, T.FLOAT, T.BOOL, T.STRING):
                    return T.BOOL
                raise CompileError(Diagnostic(
                    self.filename, expr.line, expr.col, len(op),
                    f"cannot compare {lt} {op} {rt}",
                ))
```

Add one new string branch to the `+` handler:

```python
            if op == "+":
                if lt == T.INT and rt == T.INT:
                    return T.INT
                if lt == T.FLOAT and rt == T.FLOAT:
                    return T.FLOAT
                if lt == T.STRING and rt == T.STRING:
                    return T.STRING
                raise CompileError(Diagnostic(
                    self.filename, expr.line, expr.col, len(op),
                    f"cannot add {lt} + {rt}",
                ))
```

- [ ] **Step 2.3: Add the `len` builtin to Sema**

Add a special case for `len` at the same level, immediately below the `if call.callee == "print":` block at `rwc/sema.py:378`:

```python
        if call.callee == "len":
            if len(call.args) != 1:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"len takes exactly 1 argument, got {len(call.args)}",
                ))
            at = self.analyze_expr(call.args[0])
            if at != T.STRING:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"len argument must be string, got `{at}`",
                ))
            return T.INT
```

Also add `len` to the branch that forbids `spawn len(x)` in the `spawn` path (the same location as `if call.callee == "print":`, around sema.py:352):

```python
                if call.callee == "print":
                    raise CompileError(Diagnostic(
                        self.filename, call.line, call.col, len(call.callee),
                        "cannot spawn the builtin `print`",
                    ))
                if call.callee == "len":
                    raise CompileError(Diagnostic(
                        self.filename, call.line, call.col, len(call.callee),
                        "cannot spawn the builtin `len`",
                    ))
```

- [ ] **Step 2.4: Sema unit tests (positive)**

Functions to add to `tests/test_sema.py`. Use an existing helper (something shaped like `analyze_str`) if one exists. If there is no helper, add something like this:

```python
def _analyze(src: str) -> None:
    from rwc.lexer import tokenize
    from rwc.parser import parse
    from rwc.sema import analyze
    tokens = tokenize(src, filename="<t>")
    ast = parse(tokens)
    analyze(ast, filename="<t>")
```

Then, on top of that:

```python
def test_string_len_returns_int():
    _analyze("""\
def main() -> int:
    s: string = "abc"
    n: int = len(s)
    return n
""")


def test_string_eq_returns_bool():
    _analyze("""\
def main() -> int:
    a: string = "x"
    b: string = "y"
    if a == b:
        return 1
    return 0
""")


def test_string_neq_returns_bool():
    _analyze("""\
def main() -> int:
    a: string = "x"
    b: string = "y"
    if a != b:
        return 1
    return 0
""")


def test_string_concat_returns_string():
    _analyze("""\
def main() -> int:
    a: string = "x"
    b: string = "y"
    c: string = a + b
    print(c)
    return 0
""")
```

- [ ] **Step 2.5: Sema unit tests (negative)**

Add to `tests/test_sema.py`:

```python
import pytest
from rwc.diagnostics import CompileError


def test_string_plus_int_is_type_error():
    with pytest.raises(CompileError) as excinfo:
        _analyze("""\
def main() -> int:
    s: string = "x" + 1
    return 0
""")
    assert "cannot add" in str(excinfo.value)


def test_string_eq_int_is_type_error():
    with pytest.raises(CompileError) as excinfo:
        _analyze("""\
def main() -> int:
    if "x" == 1:
        return 0
    return 1
""")
    assert "cannot compare" in str(excinfo.value)


def test_len_wrong_arg_type():
    with pytest.raises(CompileError) as excinfo:
        _analyze("""\
def main() -> int:
    n: int = len(1)
    return n
""")
    assert "len argument must be string" in str(excinfo.value)


def test_len_wrong_arity():
    with pytest.raises(CompileError) as excinfo:
        _analyze("""\
def main() -> int:
    n: int = len("a", "b")
    return n
""")
    assert "len takes exactly 1 argument" in str(excinfo.value)
```

- [ ] **Step 2.6: Run the Sema tests**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw && uv run pytest tests/test_sema.py -v 2>&1 | tail -20
```

Expected: the existing tests plus all 8 new ones PASS.

If the `CompileError` format or the style of the existing test_sema.py differs, adjust accordingly (e.g. add the `from rwc.diagnostics import CompileError` import). If the `_analyze` helper already exists in test_sema.py, use it rather than redefining it.

### irgen side

- [ ] **Step 2.7: Add the three external function declarations to irgen**

Add them immediately below the `self._rw_print_*` declarations at `rwc/irgen.py:68-71`:

```python
        self._rw_str_len = ir.Function(
            m, ir.FunctionType(I64, [RW_STR_TY]), "rw_str_len")
        self._rw_str_eq = ir.Function(
            m, ir.FunctionType(I8, [RW_STR_TY, RW_STR_TY]), "rw_str_eq")
        self._rw_str_concat = ir.Function(
            m, ir.FunctionType(RW_STR_TY, [RW_STR_TY, RW_STR_TY]), "rw_str_concat")
```

- [ ] **Step 2.8: Handle `len` in irgen's `_emit_call`**

In `_emit_call` around `rwc/irgen.py:368`, add the following in the same style immediately below the block that handles `print`:

```python
            if call.callee == "len":
                v = self._emit_expr(call.args[0], ctx)
                return ctx.builder.call(self._rw_str_len, [v])
```

- [ ] **Step 2.9: Handle the string cases in irgen's `_emit_binop`**

In `_emit_binop` around `rwc/irgen.py:289`, add string branches to the handling of `+` / `==` / `!=`. Discriminate using `a.type == RW_STR_TY` or the type information attached by Sema (Read irgen.py first to confirm which approach is currently used before applying). Skeleton:

```python
        if op == "+":
            # existing int/float branches above remain unchanged
            if isinstance(l.type, ir.LiteralStructType) and l.type == RW_STR_TY:
                return b.call(self._rw_str_concat, [l, r])
            ...

        if op in ("==", "!="):
            # existing int/float/bool branches above remain unchanged
            if isinstance(l.type, ir.LiteralStructType) and l.type == RW_STR_TY:
                eq = b.call(self._rw_str_eq, [l, r])   # i8 0/1
                eq_bool = b.icmp_unsigned("!=", eq, ir.Constant(I8, 0))  # i1
                if op == "!=":
                    eq_bool = b.not_(eq_bool)
                return eq_bool
            ...
```

Follow llvmlite conventions for the actual API names (whether `b.icmp_unsigned` or `b.icmp_signed`, whether `not_` exists, etc.). Look at how the existing irgen.py emits int `==` and match that style.

- [ ] **Step 2.10: Build and verify the existing e2e tests pass**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw && uv run pytest -q 2>&1 | tail -5
```

Expected: 66 + 8 new sema = 74 tests all PASS. The existing 7 e2e tests stay green as well.

- [ ] **Step 2.11: Generate IR standalone and inspect it visually**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
cat > /tmp/string_smoke.rw <<'EOF'
def main() -> int:
    a: string = "foo"
    b: string = "bar"
    c: string = a + b
    print(c)
    print(len(c))
    if c == "foobar":
        print("ok")
    return 0
EOF
uv run rwc emit-ir /tmp/string_smoke.rw | grep -E "rw_str_(concat|eq|len)"
```

Expected: the output contains the three calls `call ... @rw_str_concat`, `@rw_str_eq`, and `@rw_str_len`.

- [ ] **Step 2.12: Build and run standalone**

```sh
uv run rwc run /tmp/string_smoke.rw
```

Expected:
```
foobar
6
ok
```

- [ ] **Step 2.13: Commit**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
git add rwc/sema.py rwc/irgen.py tests/test_sema.py
git commit -m "$(cat <<'EOF'
rwc: allow len, string ==, and string + in sema + irgen

Sema:
  - Add `len(string) -> int` as a builtin (alongside `print`).
  - Allow `==` and `!=` between strings (was an explicit MVP block).
  - Allow `+` between strings (returns a fresh string).
  - Forbid `spawn len(...)` to match the `spawn print(...)` rule.

irgen:
  - Declare extern `rw_str_len(rw_str) -> i64`,
    `rw_str_eq(rw_str, rw_str) -> i8`,
    `rw_str_concat(rw_str, rw_str) -> rw_str`.
  - Emit calls to these for the new sema-allowed forms.

Negative tests in test_sema.py cover wrong-type and wrong-arity
diagnostics; positive tests cover the four happy paths.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: example + e2e

**Files:**
- Create: `examples/string_ops.rw`
- Create: `examples/string_ops.rw.expected`
- Modify: `tests/test_e2e.py` (parametrize list)

- [ ] **Step 3.1: Write `examples/string_ops.rw`**

```rw
def main() -> int:
    hello: string = "hello"
    world: string = "world"
    sep: string = ", "
    greeting: string = hello + sep + world
    print(greeting)
    print(len(greeting))
    if greeting == "hello, world":
        print("eq ok")
    if hello != world:
        print("neq ok")
    return 0
```

- [ ] **Step 3.2: Write `examples/string_ops.rw.expected`**

```
hello, world
12
eq ok
neq ok
```

(Save with a trailing newline. `print` appends a newline automatically, so match the line count and contents accordingly.)

- [ ] **Step 3.3: Run locally and confirm it matches the expected output**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
RW_WORKERS=1 uv run rwc run examples/string_ops.rw
diff <(RW_WORKERS=1 uv run rwc run examples/string_ops.rw 2>&1) examples/string_ops.rw.expected
```

Expected: no `diff` output (byte-for-byte match).

- [ ] **Step 3.4: Add `string_ops` to the parametrize list in `tests/test_e2e.py`**

Change the following lines in `tests/test_e2e.py` (around 37-40):

```python
@pytest.mark.parametrize(
    "name",
    ["hello", "arith", "fib", "while_count", "spawn_basic", "spawn_many", "spawn_string"],
)
```

to:

```python
@pytest.mark.parametrize(
    "name",
    ["hello", "arith", "fib", "while_count", "spawn_basic", "spawn_many", "spawn_string", "string_ops"],
)
```

- [ ] **Step 3.5: Run the full e2e test suite**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw && uv run pytest -q 2>&1 | tail -5
```

Expected: all green (66 + 8 new sema + 1 new e2e = 75 tests).

- [ ] **Step 3.6: Verify the runtime unit tests are also green**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw/runtime
make clean && make
# spot-check
cc -O2 -Wall -Wextra -std=c11 -D_GNU_SOURCE -pthread fiber/test_sched.c fiber/fiber.o fiber/fiber_arm64.o fiber/sched.o fiber/runq.o fiber/park.o -o fiber/test_sched && ./fiber/test_sched
cc -O2 -Wall -Wextra -std=c11 -D_GNU_SOURCE fiber/test_str_ops.c runtime.o -o fiber/test_str_ops && ./fiber/test_str_ops
```

Expected: `total = 333833500` / `all str_ops tests passed`.

- [ ] **Step 3.7: Commit**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
git add examples/string_ops.rw examples/string_ops.rw.expected tests/test_e2e.py
git commit -m "$(cat <<'EOF'
examples: add string_ops example exercising len, ==, +

examples/string_ops.rw uses every new operator introduced in this PR
in a single main: concat with two operands of differing literal
origin, len, ==, and != against literals. The .expected captures the
byte-for-byte stdout, and tests/test_e2e.py picks it up via the
existing parametrize list, so any later regression in sema / irgen /
runtime helpers will fail the suite.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review

Spec coverage check:

- `len(s) -> int` builtin → Task 2.3 (sema) + 2.7-2.8 (irgen) + 2.4/2.5 (test) + Task 3 (e2e)
- `==` / `!=` for string → Task 2.2 (sema) + 2.9 (irgen) + 2.4-2.5 (test) + Task 3 (e2e)
- `+` for string → Task 2.2 (sema) + 2.9 (irgen) + 2.4-2.5 (test) + Task 3 (e2e)
- Three runtime helper functions → Task 1.1-1.2 + C unit test 1.4-1.6
- Handling of empty string / NULL ptr → Task 1.2 (concat's `len == 0` branch) + 1.4 (empty cases in test_str_ops)
- Existing e2e tests don't break → run everything in Task 2.10 + Task 3.5
- Public ABI unchanged → only new symbols added, no existing symbols modified

Placeholder scan: zero occurrences of "TBD", "TODO", "(TBC)", or "fill in" in the plan.

Type consistency:
- `rw_str_len` arg = `rw_str`, return = `int64_t` (= LLVM `I64`) — consistent across tasks 1.1, 1.2, 2.7
- `rw_str_eq` args = `rw_str, rw_str`, return = `int8_t` (= LLVM `I8`) — same
- `rw_str_concat` args = `rw_str, rw_str`, return = `rw_str` (= LLVM `RW_STR_TY`) — same
- The notation for Sema's internal type constants `T.STRING` / `T.INT` / `T.BOOL` is consistent across task 2

Risk: the current shape of `_emit_binop` in irgen.py (how it discriminates types) was written into the plan without reading it, so be sure to Read and match it at implementation time. Whether the comparison `isinstance(l.type, ir.LiteralStructType) and l.type == RW_STR_TY` works as an equality check is llvmlite-dependent, but the assumption is to match the pattern used at the existing `rw_print_str` call site.
