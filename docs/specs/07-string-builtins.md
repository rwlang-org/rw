# rw string extensions: len / == / +

## Context

Apart from `print` and `spawn`/`await`, rw has almost no built-in features, and
`string` exists only to **`print` a literal**.

```rw
def main() -> int:
    name: string = "alice"
    # You cannot write anything beyond this point:
    #   - len(name) to get the length
    #   - if name == "alice": to branch
    #   - print("hello, " + name) to build a log
    print(name)
    return 0
```

This PR, on the premise of adding a **netpoller / TCP API** in the future, adds
the first step of the "minimal language extension" that precedes it. What you
need to write an echo server in rw code is the three things "write back the
string you read," "branch on the length," and "build a log," and these are
covered by just `len` / `==` / `+`.

The long-term plan proceeds outside this spec:

1. **This sub-project**: `len` / `==` / `+` for string
2. Bytes type and buffer API
3. List[T]
4. Result[T, E] / Option[T]
5. netpoller + TCP API

## Goals

- Add the built-in function `len(s: string) -> int`
- Allow the operators `==` / `!=` between two strings (byte equality)
- Allow the operator `+` between two strings (concatenation, allocating a new string)
- Keep the existing tests (66 + C tests) green
- Do not change the public ABI (`rw_spawn_*` / `rw_await_*` / `rw_str`)

## Non-Goals

- The `s.len` dot notation (rw has no member access via structs, so it is a bad fit)
- Lexicographic comparison `<` / `<=` / `>` / `>=` (not needed for an echo server)
- `*` (string repetition) or `s[i]` (indexing)
- String mutation (`rw_str` assumes a const pointer)
- Memory reclamation (concatenation results are left malloc'd, leaks tolerated; fine because this is for learning/experimentation)
- Internationalization, Unicode normalization (treated only as byte sequences)

## Design

### Behavior seen from the language level

```rw
def main() -> int:
    a: string = "hello"
    b: string = ", world"
    c: string = a + b               # "hello, world" (newly allocated)
    print(len(c))                   # 12
    if c == "hello, world":         # true
        print("matched")
    if a != b:                      # true
        print("differ")
    return 0
```

### Runtime helper functions to add (`runtime.c` / `runtime.h`)

Add three helper functions with the C ABI.

```c
/* New helpers — internal but exposed for irgen. */
int64_t rw_str_len   (rw_str s);
int8_t  rw_str_eq    (rw_str a, rw_str b);
rw_str  rw_str_concat(rw_str a, rw_str b);
```

Implementation approach:

- `rw_str_len`: a one-liner that just returns `s.len`. irgen could have emitted
  `extractvalue` directly, but we emit it via a C function to keep the ABI
  uniform. This also leaves room to later add a "bounds check for the case where
  it is not \0-terminated."
- `rw_str_eq`: `a.len != b.len` → immediately return 0. If they are equal,
  return `memcmp(a.ptr, b.ptr, a.len) == 0` as an `int8_t` (0/1), matching the
  same i8 representation as `rw_print_bool`.
- `rw_str_concat`: allocate with `malloc(a.len + b.len)`, `memcpy(p, a.ptr, a.len)`,
  `memcpy(p + a.len, b.ptr, b.len)`, and return `{len = a.len+b.len, ptr = p}`.
  If both have len=0, return ptr=NULL, len=0 (do not call malloc(0)).
  It does not free (leaks tolerated).

These are pthread-safe (each call is local), side-effect-free (except for
concat's malloc), and safe under both single-threaded and multi-threaded
execution.

### Compiler changes

#### lexer / parser

No change. `len(s)` parses as the existing `Call` node, and `s1 + s2` and
`s1 == s2` parse as the existing binary-operator nodes.

#### sema (`rwc/sema.py`)

Three places:

1. Add `len(string) -> int` to the **built-in function table**. The special
   handling for `print` is currently around `analyze_call`, so add `len` in the
   same place. `print`'s argument type is "anything as long as it's a printable
   type," but `len` is strict: "one argument, of type string, returning int."
2. In the **type check for `==` / `!=`**, allow the result type bool when both
   sides are string. The existing code has a branch that rejects it along with
   the comment `# We could allow string equality later; disallow for MVP simplicity.`.
   Change this to "string is also OK."
3. In the **type check for `+`**, allow the result type string when both sides
   are string. Add `string + string -> string` with the same pattern next to
   `int + int -> int` / `float + float -> float`.

Add negative tests to `tests/test_sema.py`:
- `"a" + 1` → type error
- `"a" == 1` → type error
- `len(1)` → type error (wrong argument type)
- `len("a", "b")` → type error (wrong argument count)

#### irgen (`rwc/irgen.py`)

Three places:

1. Add `rw_str_len`, `rw_str_eq`, `rw_str_concat` to the **external function
   declarations** (in the same form as the existing `rw_print_*`).
2. Generate the **`len(s)` call**. When `_emit_call` receives a `Call` marked by
   Sema as "a call to the built-in `len`," emit IR that calls `rw_str_len`.
3. Generate the **string case for binary operators**. `==` / `!=` call
   `rw_str_eq` and convert the resulting i8 to i1 with `icmp` (for `!=`, invert
   with `xor`). `+` calls `rw_str_concat` and uses the returned `rw_str` as is.

### Boundaries between components

- The runtime side only adds **three C ABI functions**. Testing is also possible
  at the C level.
- The Sema/irgen changes are **only extensions of existing patterns**. No new
  AST node or IR instruction is introduced.
- The public ABI (`rw_spawn_*`, etc.) is unchanged. Only the symbols in
  `librw.a` increase.

## Changes by file

### Changed

- `runtime/runtime.c` — add the implementations of the 3 functions
- `runtime/runtime.h` — add the 3 prototypes
- `rwc/sema.py` — `len` built-in, allow `+`/`==`/`!=` for strings
- `rwc/irgen.py` — external declarations and call generation for `rw_str_*`
- `tests/test_sema.py` — add type-check cases for string operations
- `tests/test_e2e.py` — add `string_ops` to the parametrize list

### New

- `examples/string_ops.rw` — a sample that uses len / `==` / `+` in a single main
- `examples/string_ops.rw.expected` — the expected output

### Unchanged

- lexer, parser, driver
- fiber-related (`runtime/fiber/*`)
- existing examples / spec docs

## Verification

```sh
# runtime alone
make -C runtime clean && make -C runtime

# all tests
uv run pytest -q

# the new example runs on its own
uv run rwc run examples/string_ops.rw

# existing examples are not broken
uv run rwc run examples/hello.rw
uv run rwc run examples/spawn_many.rw
```

Success criteria: all tests green, the output of `string_ops.rw` matches
`.expected`, and the new negative Sema tests return type errors as expected.

## Commit structure

3 commits:

1. **runtime**: add `rw_str_len` / `rw_str_eq` / `rw_str_concat`. Verify by
   calling them directly at the C level.
2. **rwc**: allow string `len` / `==` / `+` in Sema, and generate IR in irgen
   that calls the runtime helpers. Add Sema negative tests.
3. **examples + e2e**: add `string_ops.rw` and wire it into the parametrize list
   of `tests/test_e2e.py`.

## Risks and remedies

| Risk | Remedy |
|---|---|
| The case where `rw_str`'s `ptr` is NULL (empty string literal) | concat and eq branch on len=0 first to avoid a NULL deref |
| Sema's type inference for `+` breaks (e.g. integer arithmetic falls into the string path) | make the existing `+` handler explicitly three branches: "both int / both float / both string." Cover `1 + 1` / `1.0 + 2.0` / `"a" + "b"` in tests |
| Code where the user uses `len` as a variable name | there is no place in the repo's `examples/*.rw` that uses `len` as a variable (confirmed by grep). Sema could separately error on "a variable definition that collides with a built-in name," but we do not do that this time (shadowing allowed; a built-in is a built-in "only when it is called") |
| Concatenation leaks become a problem in a long-running echo server | a non-goal of this sub-project. If we decide to add GC before integrating the netpoller, we address it in a separate PR |
