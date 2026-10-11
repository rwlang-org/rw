# rw Bytes type (immutable, minimal echo set)

## Context

In `docs/specs/07-string-builtins.md` we added `len` / `==` / `+` for `string`.
The next thing we need is a representation for **binary-safe variable-length
byte sequences**, which will be used as the return type of `read(fd, n)`
(formerly `tcp_read`, unified to be fd-generic in #33) in the future
netpoller + TCP API.

Why `string` won't do:
- `string` is effectively an immutable `{i64 len, i8* ptr}`, and **in the type
  system it is a "string."** Holding binary data (containing \0, non-UTF-8 data)
  in a `string` breaks the semantics
- Text data is assumed on the premise that "you can `print(s)`" and "you can
  concatenate with `s + s`"

We introduce `Bytes` as a **separate type**, distinct from `string`. This time
we add only the minimal operations an echo server needs (`len`, `==`, mutual
conversion with string).

Long-term plan (restated):

1. String `len` / `==` / `+` (done)
2. **This sub-project**: Bytes type + minimal API
3. List[T]
4. Result[T, E] / Option[T]
5. netpoller + TCP API

## Goals

- Introduce a new primitive type `Bytes` (keyword `Bytes`)
- Built-ins:
  - `len(b: Bytes) -> int` (overloads the existing `len(string)`)
  - `b1 == b2`, `b1 != b2` (only between Bytes; comparing `string` and Bytes is forbidden)
  - `bytes_from_str(s: string) -> Bytes`
  - `str_from_bytes(b: Bytes) -> string`
- `Bytes` can also be used as the return type of `spawn fn() -> Bytes`
  (i.e. `Future[Bytes]` works)
- Public ABI unchanged, existing tests green

## Non-Goals

- Bytes literal syntax like `b"..."` (needs a lexer extension, later)
- `bytes_at(b, i)` / `bytes_slice(b, i, j)` / Bytes concatenation (`+`) — for
  protocol parsing, not needed for an echo server, a separate PR
- Allowing `print(b: Bytes)` — directly outputting data that may not be UTF-8 is
  forbidden by the type system. If needed, convert explicitly with
  `str_from_bytes(b)`
- Implicit conversion between `Bytes` and `string` / mixing them in `==`

## Design

### Internal representation

At the LLVM IR level, `Bytes` is represented as the **same `{i64 len, i8* ptr}`
(= `RW_STR_TY`) as `string`**. At the Sema level, `T.STRING` and `T.BYTES` are
treated as distinct, and Sema statically rejects type confusion.

Benefits:
- No new runtime function needs to be added at all. `rw_str_len` / `rw_str_eq`
  are reused as is
- `bytes_from_str` / `str_from_bytes` are **noops** where only the type
  information changes — irgen returns the argument's SSA value as is
- `Future[Bytes]` can also use `rw_spawn_str` / `rw_await_str` as is

Note: since they use the same representation, **mixing up `Bytes` and `string`
only causes Sema to fail; it does not cause an invalid access**. The distinction
is for language cleanliness, not for safety.

### Behavior seen from the language level

```rw
def main() -> int:
    b: Bytes = bytes_from_str("hello")
    print(len(b))                    # => 5
    if b == bytes_from_str("hello"):
        print("eq ok")
    s: string = str_from_bytes(b)
    print(s)                         # => hello
    return 0
```

Cases that become errors:

```rw
def main() -> int:
    b: Bytes = bytes_from_str("hi")
    print(b)                  # error: print does not support `Bytes`
    x: Bytes = b + b          # error: `+` requires int/float/string
    y: bool = b == "hi"       # error: `==` requires same type (Bytes vs string)
    z: Bytes = bytes_from_str(1)  # error: argument must be string
    return 0
```

### Changes by component

#### `rwc/types.py`

Add

```python
BYTES = _Primitive("Bytes")
```

Do **not** include it in `is_printable` / `is_numeric`.

#### `rwc/lexer.py`

Add

```python
KW_BYTES = auto()
```

to `TokenKind`, and add `"Bytes": TokenKind.KW_BYTES` to `KEYWORDS`. The keyword
naming starts with an uppercase letter (like `Future`, a separate group from the
lowercase primitives such as `int`/`string`).

#### `rwc/parser.py`

Add `TokenKind.KW_BYTES: "Bytes"` to the `kind_to_name` dict in `parse_type`.
One line.

#### `rwc/sema.py`

Three places:

1. Add `"Bytes": T.BYTES` to the dict in `_resolve_type`.
2. Extend the `len` handler in `_check_call` to `string` or `Bytes`:
   ```python
   if at is not T.STRING and at is not T.BYTES:
       raise ... f"len argument must be string or Bytes, found `{at}`"
   ```
3. Add two new built-ins to `_check_call`:
   ```python
   if call.callee == "bytes_from_str":
       # arity 1, arg is string, returns Bytes
   if call.callee == "str_from_bytes":
       # arity 1, arg is Bytes, returns string
   ```
   For both, add a branch on the `SpawnExpr` path that forbids
   `spawn bytes_from_str(...)` / `spawn str_from_bytes(...)`, the same as
   `print` / `len`.

The binary operators `==` / `!=` already require "both sides the same type," so
`T.BYTES == T.BYTES` passes automatically. On the irgen side, you only need to
add a branch that routes it through `rw_str_eq`, the same as string.

#### `rwc/irgen.py`

- Add Bytes to `llvm_type_of`:
  ```python
  if t is T.BYTES:
      return RW_STR_TY
  ```
- In the `==`/`!=` branch of `_emit_binop`, use `is_strlike = lty in (T.STRING, T.BYTES)` as the decision criterion instead of `is_str`, and pass it to `rw_str_eq`.
- `_emit_call`:
  - The `len` call: call `rw_str_len` whether the argument is `T.STRING` or `T.BYTES`. Sema has already validated the type, so the irgen side just calls the helper regardless of type.
  - `bytes_from_str` / `str_from_bytes`: a noop that returns the argument's SSA value as is:
    ```python
    if call.callee in ("bytes_from_str", "str_from_bytes"):
        return self._emit_expr(call.args[0], ctx)
    ```
- Add `T.BYTES` to the return-type branch of `_decl_spawn` / `_decl_await`, returning `rw_spawn_str` / `rw_await_str`:
  ```python
  elif ret_ty is T.STRING or ret_ty is T.BYTES:
      name, ret_llvm = "rw_spawn_str", RW_STR_TY
  ```

#### Runtime

No change.

### Tests

#### `tests/test_sema.py`

Positive (5):
- `Bytes` type annotation and return-value inference of `bytes_from_str`
- `len(Bytes)` returns int
- `Bytes == Bytes` is bool
- `str_from_bytes(b)` returns string
- `spawn fn() -> Bytes` is `Future[Bytes]`

Negative (5):
- `print(b)` errors because `Bytes` is not printable
- `b + b` errors because `+` does not allow Bytes
- `b == "hi"` errors on `==` type mismatch
- `bytes_from_str(1)` errors on argument type
- `spawn bytes_from_str("a")` errors on forbidden built-in

#### e2e

- Add `examples/bytes_basic.rw` (per the behavior example above) and its `.expected`
- Add `"bytes_basic"` to the parametrize list of `tests/test_e2e.py`

No new runtime unit tests are needed (no new C functions).

## Changes by file

### Changed

- `rwc/types.py` — `BYTES = _Primitive("Bytes")`
- `rwc/lexer.py` — add `KW_BYTES`, extend `KEYWORDS`
- `rwc/parser.py` — extend the dict in `parse_type`
- `rwc/sema.py` — `_resolve_type` / `_check_call` (extend 3 built-ins) / SpawnExpr forbidden list
- `rwc/irgen.py` — `llvm_type_of` / `_emit_binop` / `_emit_call` / `_decl_spawn` / `_decl_await`
- `tests/test_sema.py` — positive 5 + negative 5
- `tests/test_e2e.py` — add 1 line to the parametrize list

### New

- `examples/bytes_basic.rw`
- `examples/bytes_basic.rw.expected`

### Unchanged

- `runtime/` (not touched at all)
- `docs/specs/05-fibers.md` / `06-scheduler-mn.md` / `07-string-builtins.md`

## Verification

```sh
# pytest
uv run pytest -q
# expected: existing 75 + Sema positive 5 + negative 5 + e2e 1 = 86, all green

# run standalone
uv run rwc run examples/bytes_basic.rw

# regression of existing examples
uv run rwc run examples/hello.rw
uv run rwc run examples/string_ops.rw
uv run rwc run examples/spawn_many.rw

# runtime is untouched, so the C tests stay green as before
cd runtime && make clean && make
```

## Commit structure

3 commits:

1. **rwc: introduce Bytes type (lexer/parser/types)** — only `T.BYTES` /
   `KW_BYTES` / parse_type / `_resolve_type`. Sema/irgen are not there yet, so
   code that uses `Bytes` errors elsewhere, but the type annotation
   `b: Bytes = ...` alone can be parsed + resolved
2. **rwc: Bytes operations in sema + irgen** — the 4 built-ins (`len`
   overload, `bytes_from_str`, `str_from_bytes`, `==`) and spawn/await support
   for Bytes. Add positive/negative tests together
3. **examples + e2e** — add `bytes_basic.rw`, wire it into the parametrize list

## Risks and remedies

| Risk | Remedy |
|---|---|
| `Bytes` already appears as a variable name in user code | confirmed with `grep -rE '\bBytes\b' examples/` (no matches). As a new keyword, even if it collides with a general variable name it is not fatal (a keyword is reserved; the user picks another name) |
| `==` gives "a type error despite the same representation" for string vs Bytes | as specified. Bytes and string are separate types and require explicit conversion. The Sema error message is an existing form like `"\`==\` requires same type, found \`Bytes\` and \`string\`"` |
| `Future[Bytes]` sharing `rw_spawn_str` causes virtual type confusion in the future | the current Sema distinguishes Bytes/string at the type level, so there is no confusion from the user code's perspective. If we later change the representation of Bytes (e.g. make it its own struct), we can handle it by just splitting the branch in `_decl_spawn` |
| The risk that `print(b)` is not rejected on the language side (= irgen crashes) | test `is_printable(T.BYTES) == False` reliably at the Sema level (covered by a negative test) |
