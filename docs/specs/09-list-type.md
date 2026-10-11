# rw List[int] type (immutable, monomorphic, minimal echo set)

## Context

The "modern rw language" that this sub-project assumes is a small language in
which the compiler, runtime, sample programs, and tests grow together as one,
with the language user and the implementer inside the same loop.

Real-world evolution takes many forms. One way is to freeze the language
specification and then write the compiler; another is to start from a minimal
runtime and gradually stack up language features. There is even an academic
approach that designs the type system first and bolts on the evaluator later.
That said, not every language implementation follows one of those "correct"
orderings.

Regardless of the chosen approach, however, tasks such as adding a new type,
adding more built-in functions, or adding an ABI to the runtime inevitably
combine in a modern language implementation, and their connection points give
rise to complexity of their own.

This specification organizes the sources of that complexity into the following
three. First, **the problem of how to bring a container type into the language
in the first place**; next, **interoperation with existing types (string /
Bytes / Future)**; and finally, **the judgment of incremental extension that
re-asks "do we really need generics?" every time**.

The primitives put into the rw language so far are:

- `string` + `len` / `==` / `+` (#91)
- `Bytes` + `len` / `==` / conversions (#92)

`List[T]` is a **true generic type**, but at this stage of the roadmap the echo
server we are targeting only needs to "hold an array of client fds." For that,
we introduce **only the single type `List[int]`**.

We keep the path open for future extension to `List[string]` / `List[Bytes]` /
generic `List[T]`, but for now we **do not introduce generic syntax into the
language**. We treat `List[int]` as one primitive type, and any type parameter
other than `int` is turned into a parser error.

Roadmap:

1. String `len` / `==` / `+` (done)
2. Bytes type (done)
3. **this sub-project**: minimal List[int] API
4. Result[T, E] / Option[T]
5. netpoller + TCP API

## Goals

- Introduce a new primitive type `List[int]` (a fixed form in which, like
  `Future[T]`, the parser requires `[int]` after `List`)
- Four built-ins:
  - `list_new() -> List[int]`
  - `list_push(l: List[int], v: int) -> List[int]` (returns a new List)
  - `list_at(l: List[int], i: int) -> int`
  - `len(l: List[int]) -> int` (overloads the existing `len`)
- The existing public ABI stays unchanged
- Get to a state where the echo server can handle an fd array

## Non-Goals

- Generic `List[T]` (T other than int): a "only `List[int]` is supported"
  error in the parser
- `Future[List[int]]` (= `spawn fn() -> List[int]`): forbidden in Sema
- mutable list (push with in-place side effects): value-type, immutable only
- `list_pop` / `list_remove` / `list_slice` / `list_concat` / `list_eq` /
  `list_set` (element update)
- `for x in l` iteration syntax
- Expressing out-of-range access at the language level (= there is still no
  `Result[int, IndexError]` etc., so for now `rw_list_int_at` calls `abort()`
  on out-of-range)
- `print(l)` (it might be handy for debugging, but we avoid the work of
  widening the definition of `print`'s behavior via overloading this time)

## Design

### Internal representation

`List[int]` is a 3-word fat struct:

```c
typedef struct {
    int64_t  len;   /* current element count */
    int64_t  cap;   /* allocated capacity in elements */
    int64_t *data;  /* pointer to int64_t[cap]; NULL when cap == 0 */
} rw_list_int;
```

LLVM IR:

```
%rw_list_int = { i64 len, i64 cap, i64* data }
```

We pass the 3 words around directly as an SSA value (the same style as Bytes /
string being a `{len, ptr}` 2-word fat struct). There is no need to pass it via
`i8*` through `Future[List[int]]` (a non-goal this time).

### Handling immutability

On every `list_push`, we **malloc a new `data` array and memcpy all the
elements**, append the new value at the end, and return a new
`{len+1, new_cap, new_data}`. We do not free the old `data` (= another SSA might
still be referencing it, **leak allowed**). Push is O(n), but at echo-server
scale (around 1024 fds) that is no problem.

Because there is no sharing, a reader of the code can immediately conclude that
"after `l2 = list_push(l1, x)`, `l1` is unchanged." This is a natural extension
of the same "fat pointer value type" model used by Bytes / string.

### Capacity growth policy

Since we allocate a new array every time, the "policy" is effectively "4
elements the first time, then double":

```c
int64_t new_cap = (l.cap == 0) ? 4 : l.cap * 2;
while (new_cap < l.len + 1) new_cap *= 2;
```

We guarantee `l.len + 1 <= new_cap` and then `malloc(new_cap * 8)`.

### Behavior as seen from the language level

```rw
def main() -> int:
    l: List[int] = list_new()
    l = list_push(l, 10)
    l = list_push(l, 20)
    l = list_push(l, 30)
    print(len(l))           # => 3
    print(list_at(l, 0))    # => 10
    print(list_at(l, 2))    # => 30
    return 0
```

Cases that become errors:

```rw
def main() -> int:
    a: List[string] = list_new()      # parser error: only List[int] supported
    b: List[int] = list_new()
    print(b)                          # sema error: print does not support `List[int]`
    c: List[int] = b + b              # sema error: + requires int or float
    if b == b:                        # sema error: == requires int/float/bool/string (no List)
        return 0
    n: int = list_at(b, "x")          # sema error: list_at index must be int
    f: Future[List[int]] = spawn list_new()  # sema error: cannot spawn builtin
    return 0
```

### Changes by component

#### Runtime (`runtime/runtime.h`, `runtime/runtime.c`)

Add the public struct `rw_list_int` and 4 functions (same style as the string
ops):

```c
typedef struct {
    int64_t  len;
    int64_t  cap;
    int64_t *data;
} rw_list_int;

rw_list_int  rw_list_int_new (void);
rw_list_int  rw_list_int_push(rw_list_int l, int64_t v);
int64_t      rw_list_int_at  (rw_list_int l, int64_t i);
int64_t      rw_list_int_len (rw_list_int l);
```

Implementation:

- `_new`: returns `{0, 0, NULL}`.
- `_push`: malloc using the capacity growth policy above,
  `memcpy(new_data, l.data, l.len * 8)`, `new_data[l.len] = v`, return
  `{l.len+1, new_cap, new_data}`.
- `_at`: if `i < 0 || i >= l.len`, do
  `fputs("rw: list_at: index out of bounds\n", stderr); abort();`. If in range,
  return `l.data[i]`.
- `_len`: returns `l.len` (for irgen simplification; the call target of
  `len(l)`).

#### `rwc/types.py`

```python
LIST_INT = _Primitive("List[int]")
```

Do not include it in `is_printable` / `is_numeric`.

#### `rwc/lexer.py`

No change. We could have kept `List` **not a keyword and had the parser handle
it as an IDENT**, just like the existing `Future`, but `Future` is in fact
treated as a `KW_FUTURE` keyword. We want to do the same.

→ **Add `KW_LIST = auto()` and `KEYWORDS["List"] = KW_LIST`**. The spec proposal
said the parser would treat it as IDENT, but for consistency with `Future` we
make it a keyword (it takes just one line).

#### `rwc/parser.py`

Add a branch for `List` **directly below** the branch in `parse_type` that
handles `Future`:

```python
if t.kind == TokenKind.KW_LIST:
    self.i += 1
    self.eat(TokenKind.LBRACK, "'[' after List")
    inner_tok = self.cur
    if inner_tok.kind != TokenKind.KW_INT:
        raise ParserError(
            "only List[int] is supported in this version of rw",
            inner_tok.line, inner_tok.col, max(1, len(inner_tok.value)),
        )
    self.i += 1
    self.eat(TokenKind.RBRACK, "']' to close List[int]")
    return A.TypeName("List[int]", t.line, t.col)
```

Reuse the existing `A.TypeName` as-is for the AST (name `"List[int]"`). No new
AST node is needed.

#### `rwc/sema.py`

Three places:

1. Add `"List[int]": T.LIST_INT` to the dict in `_resolve_type`.
2. In `_check_call`, extend the `len` handler to also allow `T.LIST_INT`.
   Additionally add the 3 built-ins:
   - `list_new()` arity 0, returns `T.LIST_INT`
   - `list_push(List[int], int)` arity 2, returns `T.LIST_INT`
   - `list_at(List[int], int)` arity 2, returns `T.INT`
3. Add `list_new` / `list_push` / `list_at` to the SpawnExpr forbidden list.

#### `rwc/irgen.py`

- Define the `RW_LIST_INT_TY` constant at the same level as `RW_STR_TY`:
  ```python
  RW_LIST_INT_TY = ir.LiteralStructType([I64, I64, I64.as_pointer()])
  ```
- `llvm_type_of(T.LIST_INT) -> RW_LIST_INT_TY`.
- Declare the 4 external functions in `_declare_runtime`:
  ```python
  self._rw_list_int_new  = ir.Function(m, ir.FunctionType(RW_LIST_INT_TY, []), "rw_list_int_new")
  self._rw_list_int_push = ir.Function(m, ir.FunctionType(RW_LIST_INT_TY, [RW_LIST_INT_TY, I64]), "rw_list_int_push")
  self._rw_list_int_at   = ir.Function(m, ir.FunctionType(I64, [RW_LIST_INT_TY, I64]), "rw_list_int_at")
  self._rw_list_int_len  = ir.Function(m, ir.FunctionType(I64, [RW_LIST_INT_TY]), "rw_list_int_len")
  ```
- Handle the 3 built-ins + `len(List[int])` in `_emit_call`. For `len`, look at
  the argument type passed from Sema (`self.sema.expr_types[id(call.args[0])]`);
  if it is `T.LIST_INT`, call `rw_list_int_len`, and if it is `T.STRING` /
  `T.BYTES`, call the existing `rw_str_len`.
- Do **not** add `T.LIST_INT` to `_decl_spawn` / `_decl_await` (= if passed, it
  is a `RuntimeError`, but since Sema already rejects it, this point is never
  reached).

### Passing List[int] values in the runtime

At the ABI level, we pass a `{i64, i64, i64*}` fat struct to C code by value.
Calling conventions differ by architecture:
- aarch64 (Apple/Linux ARM64): a struct of 16 bytes or less is passed in xN
  registers. 3 words (24 bytes) is passed **via memory** (under the SysV
  AArch64 AAPCS it falls outside the HFA rules, so it normally goes on the stack
  or via a hidden pointer).
- x86_64 SysV: a struct larger than 16 bytes → passed via memory.

clang picks the correct convention automatically, so rwc just needs to pass
`{i64, i64, i64*}` through in the IR and it is OK. llvmlite works as-is with
LLVM resolving the ABI.

### Thread safety

`rw_list_int_push` on its own only has side effects (`malloc` + `memcpy`) and
does not mutate `l`. Even if the same `l` is pushed concurrently, both create
independent new Lists (= they only share and read the original `l`). No problem.

Because `l.data` is **never freed**, there is no risk of it being freed by GC
while another fiber is reading the old data (the price of allowing leaks).

## Changes by file

### Changed

- `runtime/runtime.h` — the `rw_list_int` struct and 4 prototypes
- `runtime/runtime.c` — the 4 function implementations
- `rwc/types.py` — `LIST_INT = _Primitive("List[int]")`
- `rwc/lexer.py` — `KW_LIST` + `KEYWORDS["List"]`
- `rwc/parser.py` — a List branch next to the Future branch in `parse_type`
- `rwc/sema.py` — `_resolve_type` / `_check_call` (3 built-ins + len) / 3
  entries in the SpawnExpr forbidden list
- `rwc/irgen.py` — `RW_LIST_INT_TY` / `llvm_type_of` / `_declare_runtime` /
  `_emit_call`
- `tests/test_sema.py` — positive 4 + negative 7
- `tests/test_e2e.py` — add `list_basic` to the parametrize
- `.gitignore` — `runtime/fiber/test_list_int`

### New

- `runtime/fiber/test_list_int.c` — C unit test
- `examples/list_basic.rw` + `.expected`

### Unchanged

- fiber-related (`runtime/fiber/sched.c` etc.), driver, `docs/specs/05`–`08`

## Verification

```sh
# runtime unit
make -C runtime clean && make -C runtime
cd runtime
cc -O2 -Wall -Wextra -std=c11 -D_GNU_SOURCE -pthread fiber/test_list_int.c librw.a -o fiber/test_list_int
./fiber/test_list_int

# pytest
cd ..
uv run pytest -q
# expected: existing 87 + sema positive 4 + sema negative 7 + e2e 1 = 99 all green

# standalone run
uv run rwc run examples/list_basic.rw

# existing example regression
uv run rwc run examples/string_ops.rw
uv run rwc run examples/bytes_basic.rw
uv run rwc run examples/spawn_many.rw
```

## Commit structure

4 commits:

1. **runtime**: the `rw_list_int` struct and 4 helpers, unit-tested with
   `test_list_int.c`
2. **rwc (lexer/parser/types)**: `KW_LIST`, the List branch in `parse_type`,
   `T.LIST_INT`, updating the `_resolve_type` dict. A state where only the type
   annotation can be parsed + resolved (Sema/irgen not yet supported, so
   `list_*` calls error out)
3. **rwc (sema + irgen)**: Sema validation for the 4 built-ins, the `len`
   overload extension, IR generation in irgen, negative tests in one batch
4. **examples + e2e**: add `list_basic.rw` and update the parametrize in
   `tests/test_e2e.py`

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| `List` clashes with an existing variable / function name | Confirmed with `grep -rE '\bList\b' examples/ tests/` (no hits). Making `List` a new keyword means a user can no longer use `List` as an identifier, but since it is treated the same as `Future`, this is no problem |
| Aborting on out-of-range in `list_at` is crude | Ideally we would return `Result[int, IndexError]`, but the Result type is not implemented. Noted in the spec non-goals; revisit once Result is introduced after netpoller |
| Is it safe if two fibers push the same `l`? | `_push` only reads `l` and does not mutate it, so concurrent push is safe. Both return distinct new Lists (= they cannot be shared; a broadcast-like use needs a separate API) |
| Compatibility when introducing `Future[List[int]]` in the future | Sema forbids it for now, so later it is enough to add LIST_INT to `_decl_spawn` / `_decl_await` (an ABI design that hands off the struct via a new spawn helper is needed, but that is a separate PR) |
| 2^63 overflow in `cap` growth | Impossible at echo-server scale. In the implementation `new_cap` is `int64_t` and the `l.len + 1` check is done in `int64_t` too, but the overflow check is omitted (rw is for learning) |
| Guidance for users who try to write a List other than `int` | The parser error message makes it explicit: "only `List[int]` is supported in this version of rw". If someone mistakenly writes `List[string]` etc., the intent gets across |
