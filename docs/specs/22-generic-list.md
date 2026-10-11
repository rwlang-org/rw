# rw `List[T]` for any element type

## Context

Step 4 of the web framework roadmap (spec 18). A framework keeps a route
table and a request keeps its headers — both are lists of something other
than `int`:

```rw
class Route:
    path: string
    handler: Callable[[Request], Response]

def main() -> int:
    routes: List[Route] = list_new()
    routes = list_push(routes, Route("/", index))
    names: List[string] = list_new()
    names = list_push(names, "Host")
    print(list_at(names, 0))
    print(list_at(routes, 0).path)
    return 0
```

`List[int]` (spec 09) was introduced monomorphically on purpose, with
generics deferred until a second element type was needed. This is that
moment: `List[string]` and `List[Route]` are both required.

## Goals

- `List[T]` for every non-void element type `T`: `int`, `float`, `bool`,
  `string`, `Bytes`, `Option[int]`, `Result[int, int]`, records, function
  values, `Future[...]`, and nested lists (`List[List[string]]`)
- The existing builtins become generic over `T`:
  - `list_new() -> List[T]`
  - `list_push(List[T], T) -> List[T]`
  - `list_at(List[T], int) -> T` (aborts when out of bounds, as before)
  - `len(List[T]) -> int`
- `list_new()` takes its element type from the expected type at the use site
  (variable declaration, assignment, argument, return, record field,
  `list_push`'s list argument). Without an expected type it is `List[int]`,
  so every existing program keeps its meaning
- Lists of records and function values can be stored in record fields

## Non-Goals

- List literals (`[a, b]`), indexing syntax (`xs[i]`, #22), `for x in xs`
- `list_at_opt` for element types other than `int` (it returns `Option[int]`,
  and `Option[T]` is not generic yet)
- Mutable lists / in-place append; the immutable copy-on-push semantics of
  spec 09 are kept (each push copies the buffer)
- `==` / `print` on lists
- Generic user-defined functions or records (#105)

## Design

### Types

`types.ListType(elem)`, displayed `List[T]`, compared structurally.
`T.LIST_INT` becomes `ListType(INT)`, so existing code that tests
`t is T.LIST_INT` keeps working for `List[int]` while other element types get
their own instances.

### Expected-type checking for `list_new()`

Sema gets a small helper, `_check_expr_expect(expr, expected)`: if `expr` is a
bare `list_new()` call and `expected` is a list type, the call takes that
type; otherwise it is an ordinary `_check_expr`. It is used at every site that
knows the type it wants: declarations, assignments, returns, user-function
arguments, function-value arguments, constructor arguments, field
assignments, and the element argument of `list_push`.

### Runtime and ABI

`List[int]` keeps its existing helpers (`rw_list_int_*`) and pointer-out ABI
unchanged. Other element types use new size-generic helpers over the same
three-word layout:

```c
typedef struct { int64_t len; int64_t cap; char *data; } rw_list;

void    rw_list_new (rw_list *out);
void    rw_list_push(rw_list *out, const rw_list *l, const void *elem, int64_t elem_size);
void    rw_list_at  (void *elem_out, const rw_list *l, int64_t i, int64_t elem_size);
int64_t rw_list_len (const rw_list *l);
```

irgen passes `sizeof(T)` (via the GEP-on-null idiom) and spills the element
to a stack slot to pass it by pointer. In LLVM every list is the same
`{i64, i64, i8*}` struct regardless of `T`.

## Changes per component

- `rwc/ast_nodes.py`, `rwc/parser.py`: `TypeList(elem)` for `List[...]`
- `rwc/types.py`: `ListType`
- `rwc/sema.py`: generic list builtins, expected-type checking
- `rwc/irgen.py`: generic list helpers
- `runtime/runtime.h`, `runtime/runtime.c`: `rw_list_*`
- `examples/generic_list.rw` + `.expected`
- tests: `test_parser`, `test_sema`, `test_irgen`, `test_e2e`

Layers touched: parser / sema (+ types) / irgen / runtime (4).

## Verification

```sh
uv run --frozen pytest tests/test_parser.py tests/test_sema.py tests/test_irgen.py -q
uv run --frozen pytest tests/test_e2e.py -k "generic_list or list_basic" -q
uv run --frozen pytest -q
```

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Breaking `List[int]` programs | `List[int]` keeps its helpers and ABI; `list_new()` with no expected type is still `List[int]`; `list_basic` stays in the e2e suite |
| Wrong element size or alignment in the generic helpers | The size comes from LLVM's own layout of the element type; elements are copied with `memcpy` into a `malloc`'d buffer (suitably aligned for any type) |
| O(n²) building of long lists from copy-on-push | Documented; acceptable for route tables and headers. A mutable list is a separate decision |
