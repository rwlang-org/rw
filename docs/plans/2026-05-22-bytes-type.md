# Bytes Type Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Introduce a new primitive type `Bytes` into the rw language, and get the minimal 4 operations `len(b)` / `Bytes == Bytes` / `bytes_from_str` / `str_from_bytes` plus `Future[Bytes]` working.

**Architecture:** At the LLVM IR level, represent `Bytes` with the same `{i64 len, i8* ptr}` (= `RW_STR_TY`) as the existing `string`, and distinguish `T.BYTES` as a separate type only within Sema. Do not add any new runtime function.

**Tech Stack:** Python 3.12 + llvmlite (compiler), pytest (tests), C11 (runtime — read-only this time).

**Spec:** `docs/specs/08-bytes-type.md`

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `rwc/types.py` | Primitive type definitions | Add `BYTES = _Primitive("Bytes")` |
| `rwc/lexer.py` | Keyword recognition | `KW_BYTES` + `KEYWORDS["Bytes"]` |
| `rwc/parser.py` | Type parsing | 1 line in the `parse_type` dict |
| `rwc/sema.py` | Type resolution + builtins + operator extension | 4 changes |
| `rwc/irgen.py` | LLVM IR generation | 4 changes |
| `tests/test_sema.py` | Positive/negative type checking | Add tests |
| `tests/test_e2e.py` | Add bytes_basic to parametrize | 1 line added |
| `examples/bytes_basic.rw` | Feature demo | New |
| `examples/bytes_basic.rw.expected` | Expected output | New |

Do not touch the runtime (`runtime/*`) or anything related to the existing fibers.

---

## Task 1: Recognize the `Bytes` type name in lexer / parser / types

The goal of this task is only to "make the `b: Bytes = ...` type annotation parse + resolve." Since there is no builtin that actually uses `Bytes` yet, the code ends up in a state at the Sema level where "the type `Bytes` exists, but there is still no way to construct it."

**Files:**
- Modify: `rwc/types.py` (add BYTES)
- Modify: `rwc/lexer.py` (KW_BYTES + KEYWORDS)
- Modify: `rwc/parser.py` (parse_type dict)
- Modify: `rwc/sema.py` (_resolve_type dict)

- [ ] **Step 1.1: Add the `BYTES` primitive to `rwc/types.py`**

Add it **immediately below** `VOID = _Primitive("void")` in `rwc/types.py`:

```python
BYTES = _Primitive("Bytes")
```

Do **not** include it in `is_printable` / `is_numeric` (leaving them as-is is OK).

- [ ] **Step 1.2: Add `KW_BYTES` to `rwc/lexer.py`**

Add **immediately below** `KW_STRING = auto()` in the `TokenKind` enum:

```python
    KW_BYTES = auto()
```

and add **immediately below** `"string": TokenKind.KW_STRING,` in the `KEYWORDS` dict:

```python
    "Bytes": TokenKind.KW_BYTES,
```

- [ ] **Step 1.3: Make `parse_type` in `rwc/parser.py` recognize Bytes**

Change the `kind_to_name` dict inside the `parse_type` method (around parser.py:154) to the following:

```python
        kind_to_name = {
            TokenKind.KW_INT: "int",
            TokenKind.KW_FLOAT: "float",
            TokenKind.KW_BOOL: "bool",
            TokenKind.KW_STRING: "string",
            TokenKind.KW_BYTES: "Bytes",
            TokenKind.KW_VOID: "void",
        }
```

- [ ] **Step 1.4: Make `_resolve_type` in `rwc/sema.py` recognize Bytes**

Change the `m` dict in the `_resolve_type` function (around sema.py:39) to the following:

```python
        m = {
            "int": T.INT,
            "float": T.FLOAT,
            "bool": T.BOOL,
            "string": T.STRING,
            "Bytes": T.BYTES,
            "void": T.VOID,
        }
```

- [ ] **Step 1.5: Confirm existing tests are green**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw && uv run pytest -q 2>&1 | tail -5
```

Expected: `75 passed`.

- [ ] **Step 1.6: Add a test confirming that just the `b: Bytes = ...` type annotation can parse+resolve**

Add to the end of `tests/test_sema.py`:

```python
def test_bytes_type_annotation_parses():
    # Declaring a Bytes parameter and using it should parse and resolve.
    # We don't yet have a way to *produce* a Bytes value, so use a
    # function parameter (the only way to introduce a Bytes binding
    # before Task 2 lands bytes_from_str).
    src = (
        "def takes_bytes(b: Bytes) -> int:\n"
        "    return 0\n"
        "def main() -> int:\n"
        "    return 0\n"
    )
    res = check(src)
    assert "takes_bytes" in res.functions
    assert res.functions["takes_bytes"].params[0][1] is T.BYTES


def test_unknown_type_name_still_errors():
    # Sanity: an arbitrary unknown type name remains an error.
    src = (
        "def f(x: Nope) -> int:\n"
        "    return 0\n"
        "def main() -> int:\n"
        "    return 0\n"
    )
    e = err(src)
    assert "unknown type" in e.diagnostic.message
```

- [ ] **Step 1.7: Run the tests above**

```sh
uv run pytest tests/test_sema.py::test_bytes_type_annotation_parses tests/test_sema.py::test_unknown_type_name_still_errors -v 2>&1 | tail -10
```

Expected: both PASS. The representation of `takes_bytes`'s parameter type depends on the format of `res.functions[...].params`, so if accessing `params[0][1]` raises an AttributeError, check how `params` is used in the existing tests (in `tests/test_sema.py`) and match that same format.

- [ ] **Step 1.8: Commit**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
git add rwc/types.py rwc/lexer.py rwc/parser.py rwc/sema.py tests/test_sema.py
git commit -m "$(cat <<'EOF'
rwc: introduce Bytes type name in lexer / parser / sema

Adds the primitive type T.BYTES, the lexer keyword Bytes
(TokenKind.KW_BYTES), the parser kind-to-name entry, and the sema
resolver entry. With this in place, `b: Bytes` type annotations
parse and resolve, but there is still no way to construct or
operate on a Bytes value — that lands in the next commit.

is_printable and is_numeric intentionally remain string/int/float/
bool only, so attempting to print or arithmetic on a Bytes will
fail at sema (covered by tests in the next commit).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Implement Bytes operations in Sema + irgen

This task enables the following:

- `len(b: Bytes) -> int` (existing len overload)
- `bytes_from_str(s: string) -> Bytes`
- `str_from_bytes(b: Bytes) -> string`
- `Bytes == Bytes`, `Bytes != Bytes` (routed through the existing string `==` path)
- `Future[Bytes]` (spawn / await)

`Bytes + Bytes` and `print(Bytes)` remain forbidden (Sema error).

**Files:**
- Modify: `rwc/sema.py` (extend len in _check_call + add bytes_from_str / str_from_bytes, add 2 entries to the SpawnExpr forbidden list)
- Modify: `rwc/irgen.py` (llvm_type_of / _decl_spawn / _decl_await / _emit_binop / _emit_call)
- Modify: `tests/test_sema.py` (5 positive + 5 negative)

### Sema

- [ ] **Step 2.1: In Sema's `_check_call`, extend the argument type of `len` to `string` or `Bytes`**

Inside the `if call.callee == "len":` block (around `sema.py:394`), change the argument type check to the following:

```python
        # Builtin: len(string) -> int.  (also len(Bytes) -> int)
        if call.callee == "len":
            if len(call.args) != 1:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, 3,
                    f"len takes exactly 1 argument, got {len(call.args)}",
                ))
            at = self._check_expr(fn, call.args[0], locals_)
            if at is not T.STRING and at is not T.BYTES:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, 3,
                    f"len argument must be string or Bytes, found `{at}`",
                ))
            return T.INT
```

- [ ] **Step 2.2: Add `bytes_from_str` and `str_from_bytes` to Sema**

Add **immediately below** the `len` branch in `sema.py`:

```python
        # Builtin: bytes_from_str(string) -> Bytes.
        if call.callee == "bytes_from_str":
            if len(call.args) != 1:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"bytes_from_str takes exactly 1 argument, got {len(call.args)}",
                ))
            at = self._check_expr(fn, call.args[0], locals_)
            if at is not T.STRING:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"bytes_from_str argument must be string, found `{at}`",
                ))
            return T.BYTES
        # Builtin: str_from_bytes(Bytes) -> string.
        if call.callee == "str_from_bytes":
            if len(call.args) != 1:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"str_from_bytes takes exactly 1 argument, got {len(call.args)}",
                ))
            at = self._check_expr(fn, call.args[0], locals_)
            if at is not T.BYTES:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"str_from_bytes argument must be Bytes, found `{at}`",
                ))
            return T.STRING
```

- [ ] **Step 2.3: Add 2 entries to Sema's `SpawnExpr` forbidden list**

Add **immediately after** the `if call.callee == "print":` / `if call.callee == "len":` blocks (around `sema.py:352`):

```python
                if call.callee == "bytes_from_str":
                    raise CompileError(Diagnostic(
                        self.filename, expr.line, expr.col, 5,
                        "cannot spawn the builtin `bytes_from_str`",
                    ))
                if call.callee == "str_from_bytes":
                    raise CompileError(Diagnostic(
                        self.filename, expr.line, expr.col, 5,
                        "cannot spawn the builtin `str_from_bytes`",
                    ))
```

### irgen

- [ ] **Step 2.4: Add Bytes to `llvm_type_of` in irgen**

In the `llvm_type_of` function (around `rwc/irgen.py:40`), add **immediately below** `if t is T.STRING:`:

```python
    if t is T.BYTES:
        return RW_STR_TY
```

- [ ] **Step 2.5: In irgen's `_decl_spawn` / `_decl_await`, route T.BYTES through the same path as string**

Change `elif ret_ty is T.STRING:` inside `_decl_spawn` (around `rwc/irgen.py:93`) to:

```python
        elif ret_ty is T.STRING or ret_ty is T.BYTES:
            name, ret_llvm = "rw_spawn_str", RW_STR_TY
```

Similarly change `elif ret_ty is T.STRING:` inside `_decl_await` to:

```python
        elif ret_ty is T.STRING or ret_ty is T.BYTES:
            name, ret_llvm = "rw_await_str", RW_STR_TY
```

- [ ] **Step 2.6: Extend irgen's `_emit_binop` so `==` / `!=` can handle Bytes**

Inside `_emit_binop` (around `rwc/irgen.py:340`), change `is_str = lty is T.STRING` to the following:

```python
        is_str = lty is T.STRING
        is_strlike = lty is T.STRING or lty is T.BYTES
```

Then change the condition of the `elif is_str and op in ("==", "!="):` block inside `_emit_binop` (in irgen.py, where `!=` is flipped with xor) from `is_str` to `is_strlike`:

```python
            elif is_strlike and op in ("==", "!="):
                eq_i8 = b.call(self._rw_str_eq, [l, r])
                i1 = b.icmp_unsigned("!=", eq_i8, ir.Constant(I8, 0))
                if op == "!=":
                    i1 = b.xor(i1, ir.Constant(ir.IntType(1), 1))
```

The string branch of `+` (`if is_str and op == "+":`) is **left as-is** and that's OK (= since Bytes has no concatenation, with `is_str` unchanged Bytes falls straight through = a type error on the arith op path, ultimately landing in `raise RuntimeError(f"arith op {op} on {lty}")`). But that is bad because irgen crashes — **Bytes + Bytes must be rejected in Sema**. Up through Step 2.2, once it passes through the `+` handler of `_check_expr`, it should be rejected via "if both sides are the same type, proceed to the general BinOp check → string special case → numeric check (Bytes has is_numeric=False) → 'operator + requires int or float' error." Needs verification (in the negative test of Step 2.10).

- [ ] **Step 2.7: Handle `len`, `bytes_from_str`, `str_from_bytes` in irgen's `_emit_call`**

The `if call.callee == "len":` block (around `rwc/irgen.py:386`) is **left as-is** and that's OK (= it just passes the argument's SSA value to `rw_str_len`; whether the argument's Sema type is string or Bytes, at the IR level it is the same `RW_STR_TY`).

Add **immediately below** the `len` branch:

```python
        if call.callee in ("bytes_from_str", "str_from_bytes"):
            # Both are noops at the IR level: the value carries the
            # same {len, ptr} layout, only the sema type changes.
            return self._emit_expr(call.args[0], ctx)
```

### Tests

- [ ] **Step 2.8: Add positive tests**

Add to the end of `tests/test_sema.py`:

```python
def test_bytes_from_str_returns_bytes():
    src = (
        "def main() -> int:\n"
        "    b: Bytes = bytes_from_str(\"hi\")\n"
        "    return 0\n"
    )
    check(src)


def test_len_bytes_returns_int():
    src = (
        "def main() -> int:\n"
        "    b: Bytes = bytes_from_str(\"hello\")\n"
        "    n: int = len(b)\n"
        "    return n\n"
    )
    check(src)


def test_bytes_equality_ok():
    src = (
        "def main() -> int:\n"
        "    a: Bytes = bytes_from_str(\"x\")\n"
        "    b: Bytes = bytes_from_str(\"y\")\n"
        "    if a == b:\n"
        "        return 0\n"
        "    return 1\n"
    )
    check(src)


def test_str_from_bytes_returns_string():
    src = (
        "def main() -> int:\n"
        "    b: Bytes = bytes_from_str(\"x\")\n"
        "    s: string = str_from_bytes(b)\n"
        "    print(s)\n"
        "    return 0\n"
    )
    check(src)


def test_future_bytes_ok():
    src = (
        "def make() -> Bytes:\n"
        "    return bytes_from_str(\"x\")\n"
        "def main() -> int:\n"
        "    f: Future[Bytes] = spawn make()\n"
        "    b: Bytes = await f\n"
        "    return len(b)\n"
    )
    check(src)
```

- [ ] **Step 2.9: Add negative tests**

Continue adding to the end of `tests/test_sema.py`:

```python
def test_print_bytes_is_type_error():
    src = (
        "def main() -> int:\n"
        "    b: Bytes = bytes_from_str(\"x\")\n"
        "    print(b)\n"
        "    return 0\n"
    )
    e = err(src)
    assert "print" in e.diagnostic.message


def test_bytes_plus_bytes_is_type_error():
    src = (
        "def main() -> int:\n"
        "    a: Bytes = bytes_from_str(\"x\")\n"
        "    b: Bytes = bytes_from_str(\"y\")\n"
        "    c: Bytes = a + b\n"
        "    return 0\n"
    )
    e = err(src)
    # `+` falls through string-only special-case into the numeric check,
    # which rejects with "int or float".
    assert "+" in e.diagnostic.message


def test_bytes_eq_string_is_type_error():
    src = (
        "def main() -> int:\n"
        "    b: Bytes = bytes_from_str(\"x\")\n"
        "    if b == \"x\":\n"
        "        return 0\n"
        "    return 1\n"
    )
    e = err(src)
    assert "same type" in e.diagnostic.message


def test_bytes_from_str_wrong_arg_type():
    src = (
        "def main() -> int:\n"
        "    b: Bytes = bytes_from_str(1)\n"
        "    return 0\n"
    )
    e = err(src)
    assert "bytes_from_str argument must be string" in e.diagnostic.message


def test_cannot_spawn_bytes_from_str():
    src = (
        "def main() -> int:\n"
        "    f: Future[Bytes] = spawn bytes_from_str(\"x\")\n"
        "    return 0\n"
    )
    e = err(src)
    assert "cannot spawn the builtin `bytes_from_str`" in e.diagnostic.message
```

- [ ] **Step 2.10: Run the full Sema test suite**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw && uv run pytest tests/test_sema.py -v 2>&1 | tail -30
```

Expected: existing 27 + 2 added in Task 1 + 5 positive added here + 5 negative = all 39 PASS.

Whether `test_bytes_plus_bytes_is_type_error` produces the `+` error as expected is especially important. With `is_numeric(T.BYTES) == False`, the `"operator + requires int or float"` message should appear. If it does not, re-read the BinOp branch of `_check_expr` (around `sema.py:298`) and check whether Bytes is taking an unexpected route.

- [ ] **Step 2.11: Standalone IR generation + execution smoke check**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
cat > /tmp/bytes_smoke.rw <<'EOF'
def main() -> int:
    b: Bytes = bytes_from_str("hello")
    print(len(b))
    if b == bytes_from_str("hello"):
        print("ok")
    s: string = str_from_bytes(b)
    print(s)
    return 0
EOF
uv run rwc emit-ir /tmp/bytes_smoke.rw 2>&1 | grep -E "rw_str_(eq|len)"
echo "---"
uv run rwc run /tmp/bytes_smoke.rw
```

Expected: the IR contains `call`s to `rw_str_len` and `rw_str_eq`. Execution result:

```
5
ok
hello
```

- [ ] **Step 2.12: Commit**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
git add rwc/sema.py rwc/irgen.py tests/test_sema.py
git commit -m "$(cat <<'EOF'
rwc: Bytes operations in sema + irgen

Sema:
  - Overload `len` to accept Bytes (was string-only).
  - Add `bytes_from_str(string) -> Bytes` and
    `str_from_bytes(Bytes) -> string` as builtins.
  - Forbid spawn of those two new builtins.
  - `==` / `!=` on Bytes goes through the existing same-type check
    and lands on the new irgen branch below.

irgen:
  - llvm_type_of(T.BYTES) -> RW_STR_TY (shared with string).
  - _decl_spawn / _decl_await collapse T.STRING and T.BYTES onto
    rw_spawn_str / rw_await_str (same ABI shape).
  - _emit_binop ==/!= now accepts the string-like family (string or
    Bytes), routing through rw_str_eq with xor for !=.
  - _emit_call recognises bytes_from_str / str_from_bytes as
    no-ops that just thread the underlying SSA value through.

Tests (10 new): positive coverage of all four ops + Future[Bytes],
and negative coverage of print(Bytes), Bytes + Bytes, Bytes ==
string, wrong-arg-type, and spawn-of-builtin.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: example + e2e

**Files:**
- Create: `examples/bytes_basic.rw`
- Create: `examples/bytes_basic.rw.expected`
- Modify: `tests/test_e2e.py` (add bytes_basic to parametrize)

- [ ] **Step 3.1: Write `examples/bytes_basic.rw`**

File `examples/bytes_basic.rw`:

```rw
def main() -> int:
    b: Bytes = bytes_from_str("hello")
    print(len(b))
    if b == bytes_from_str("hello"):
        print("eq ok")
    s: string = str_from_bytes(b)
    print(s)
    return 0
```

- [ ] **Step 3.2: Write `examples/bytes_basic.rw.expected`**

File `examples/bytes_basic.rw.expected`:

```
5
eq ok
hello
```

(Save with a trailing newline.)

- [ ] **Step 3.3: Run it locally and confirm it matches the expected output**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
diff <(RW_WORKERS=1 uv run rwc run examples/bytes_basic.rw 2>&1) examples/bytes_basic.rw.expected && echo OK
```

Expected: only `OK` is printed (no diff output).

- [ ] **Step 3.4: Add `bytes_basic` to the parametrize in `tests/test_e2e.py`**

Change the following line at `tests/test_e2e.py:45`:

```python
    ["hello", "arith", "fib", "while_count", "spawn_basic", "spawn_many", "spawn_string", "string_ops"],
```

to:

```python
    ["hello", "arith", "fib", "while_count", "spawn_basic", "spawn_many", "spawn_string", "string_ops", "bytes_basic"],
```

- [ ] **Step 3.5: Run the full pytest suite**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw && uv run pytest -q 2>&1 | tail -5
```

Expected: 75 existing + 2 new sema (Task 1) + 10 new sema (Task 2) + 1 new e2e (Task 3) = **88 tests** all green.

- [ ] **Step 3.6: Regression-check the existing examples**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
RW_WORKERS=1 uv run rwc run examples/hello.rw
RW_WORKERS=1 uv run rwc run examples/string_ops.rw
RW_WORKERS=1 uv run rwc run examples/spawn_many.rw
```

Expected:
```
hello
---
hello, world
12
eq ok
neq ok
---
30
```

- [ ] **Step 3.7: Confirm the runtime unit tests are also green**

The runtime is untouched, but just to be safe:

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw/runtime
make clean && make
cc -O2 -Wall -Wextra -std=c11 -D_GNU_SOURCE -pthread fiber/test_sched.c librw.a -o fiber/test_sched && ./fiber/test_sched
cc -O2 -Wall -Wextra -std=c11 -D_GNU_SOURCE -pthread fiber/test_str_ops.c librw.a -o fiber/test_str_ops && ./fiber/test_str_ops
```

Expected: `total = 333833500` / `all str_ops tests passed`.

- [ ] **Step 3.8: Commit**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
git add examples/bytes_basic.rw examples/bytes_basic.rw.expected tests/test_e2e.py
git commit -m "$(cat <<'EOF'
examples: add bytes_basic exercising the new Bytes type

examples/bytes_basic.rw uses every Bytes operation introduced in
this PR in a single main:
  - bytes_from_str on a literal
  - len on Bytes
  - Bytes == Bytes
  - str_from_bytes round-trip back to string for printing

The .expected captures the byte-for-byte stdout, and
tests/test_e2e.py picks it up via the existing parametrize list.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review

### Spec coverage

| Spec requirement | Covering task |
|---|---|
| Primitive type Bytes (keyword `Bytes`) | Task 1.1, 1.2, 1.3, 1.4 |
| Internal representation shared with string (`RW_STR_TY`) | Task 2.4 (llvm_type_of) |
| `len(b: Bytes) -> int` | Task 2.1 (sema), 2.7 (irgen reuses the existing route) + test 2.8 + 2.10 |
| `Bytes == Bytes`, `Bytes != Bytes` | Task 2.6 (irgen is_strlike) + test 2.8 + 2.10 |
| `bytes_from_str(s: string) -> Bytes` | Task 2.2 (sema), 2.7 (irgen noop) + test 2.8, 2.9 |
| `str_from_bytes(b: Bytes) -> string` | Task 2.2 (sema), 2.7 (irgen noop) + test 2.8 |
| spawn/await via `Future[Bytes]` | Task 2.5 + test `test_future_bytes_ok` (2.8) |
| `Bytes + Bytes` forbidden | Automatically falls into the numeric check via the existing sema route + test `test_bytes_plus_bytes_is_type_error` (2.9) |
| `print(Bytes)` forbidden | Automatically rejected by not changing `is_printable` + test `test_print_bytes_is_type_error` (2.9) |
| Mixed `==` between Bytes and string forbidden | Automatic via the existing sema "same type" check + test `test_bytes_eq_string_is_type_error` (2.9) |
| `spawn bytes_from_str(...)` forbidden | Task 2.3 + test `test_cannot_spawn_bytes_from_str` (2.9) |
| Do not touch the runtime | Confirmed by green C tests in Task 3.7 |
| Existing tests green | Task 3.5 (full pytest) + Task 3.6 (example regression) |

Every spec requirement is assigned to a task.

### Placeholder scan

"TBD", "TODO", "(to verify)", "fill in", "Add appropriate", and "Similar to Task N" appear 0 times in the plan. The "needs verification (in the negative test of Step 2.10)" note at the end of Step 2.6 is **not** a placeholder, because the means of verification is explicitly stated in the plan (i.e., the instruction is to write and confirm the test within the same commit).

### Type consistency

- The name `T.BYTES` matches exactly across Tasks 1.1, 1.4, 2.1, 2.2, 2.4, 2.5, 2.6, 2.7
- The LLVM representation is `RW_STR_TY`, consistent across Tasks 2.4 and 2.5
- Builtin function names: `bytes_from_str` / `str_from_bytes` / `len` are consistent throughout Task 2
- Runtime calls: `rw_str_len` / `rw_str_eq` / `rw_spawn_str` / `rw_await_str` are reused with no new additions (irgen calls the existing definitions as-is)
