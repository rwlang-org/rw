# rw list literals, indexing, slicing, and `for x in xs`

## Context

`List[T]` (spec 22) is usable only through builtins, and strings only
through `str_*` calls. Every Python-flavored program wants the syntax:

```rw
def main() -> int:
    names: List[string] = ["ann", "bob", "cy"]
    for name in names:
        print(name)
    print(names[0] + " " + names[-1])     # ann cy
    print(len(names[1:]))                  # 2
    word: string = "hello"
    print(word[1:4] + word[-1])            # ello
    for ch in "abc":
        print(ch)
    return 0
```

Issues #7 and #22 ask for these.

## Goals

- List literals `[e1, ..., en]`; all elements have the same type. `[]` takes
  its type from the expected type, like `list_new()` (spec 22)
- Indexing `xs[i]` on `List[T]` (→ `T`) and `s[i]` on `string` (→ a one-byte
  `string`). A negative index counts from the end; out of range aborts with
  a message, like `list_at`
- Slicing `xs[a:b]` (→ a new `List[T]`) and `s[a:b]` (→ `string`, zero-copy),
  with either bound optional and negative bounds counting from the end; out
  of range bounds are clamped, as in Python
- `for x in xs` over a `List[T]` (x: `T`) or a `string` (x: one-byte
  `string`), with `break` and `continue`

## Non-Goals

- Assigning through an index (`xs[i] = v`): lists keep the immutable,
  copy-on-push semantics of spec 09; this is a parse error that says so
- Slice steps (`xs[::2]`), indexing `Bytes`, `Dict` indexing (no `Dict` yet)
- Iterating with an index (`enumerate`) or over several lists (`zip`)
- Unicode code points: string indices and iteration are by byte (#106)

## Design

### Syntax and AST

```
postfix   := atom ( "(" args ")" | "." IDENT | "[" subscript "]" )*
subscript := expr | expr? ":" expr?
atom      := ... | "[" (expr ("," expr)*)? "]"
for_stmt  := "for" IDENT "in" ( "range" "(" ... ")" | expr ) ":" block
```

New nodes: `ListLit(elems)`, `Index(obj, index)`, `Slice(obj, start?, stop?)`,
and `ForEach(var, iterable, body)`. `for x in range(...)` keeps its existing
desugaring.

### Sema

- `ListLit`: the type of the first element, with every other element
  checked against it (and given it as the expected type, so `[[], [1]]`
  works). An empty literal uses the expected type, falling back to
  `List[int]` like `list_new()`
- `Index` / `Slice` / `ForEach`: the object / iterable must be a list or a
  string; indices and bounds are `int`
- `ForEach` declares the loop variable in the body's scope

### IR and runtime

| Construct | Lowering |
|---|---|
| `[e1, ..., en]` | `list_new` then one push per element (`rw_list_int_push` / `rw_list_push`) |
| `xs[i]` | normalise a negative `i` (`i + len`), then `list_at` (bounds-checked) |
| `s[i]` | `rw_str_index` (new): normalise, bounds-check, one-byte slice |
| `xs[a:b]` | `rw_list_slice(out, in, a, b, elem_size)` (new) |
| `s[a:b]` | `rw_str_slice_py(s, a, b)` (new) |
| `for x in e` | evaluate `e` once into a hidden local, loop over an index; `continue` goes to the increment |

A missing slice bound is passed as `INT64_MIN` (`RW_SLICE_NONE`); the runtime
helpers replace it with `0` / `len`, add `len` to negative bounds, and clamp
to `[0, len]`.

## Changes per component

- `rwc/ast_nodes.py`, `rwc/parser.py`: the new nodes and syntax
- `rwc/sema.py`: typing, expected types for `[]`, loop variable scope
- `rwc/irgen.py`: lowering above
- `runtime/runtime.h`, `runtime/runtime.c`: `rw_str_index`, `rw_str_slice_py`, `rw_list_slice`
- `examples/list_syntax.rw` + `.expected`
- tests: `test_parser`, `test_sema`, `test_irgen`, `test_e2e`

Layers touched: parser / sema / irgen / runtime (4).
