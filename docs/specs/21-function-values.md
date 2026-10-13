# rw function values: `Callable[[...], R]`

## Context

Step 3 of the web framework roadmap (spec 18). With records (spec 20) a
handler can take a `Request` and return a `Response`, but a framework also
has to *store* handlers — in a route table — and call them later:

```rw
class Route:
    path: string
    handler: Callable[[Request], Response]

def hello(req: Request) -> Response:
    return Response(200, "hello")

def dispatch(route: Route, req: Request) -> Response:
    return route.handler(req)

def main() -> int:
    r: Route = Route("/hello", hello)
    print(dispatch(r, Request("/hello")).status)
    return 0
```

This sub-project makes top-level functions first-class values. It does not
add closures: a function value is just a code pointer.

## Goals

- A function type written as in Python's `typing`: `Callable[[T1, ..., Tn], R]`
  (`Callable[[], R]` for no parameters; `R` may be `void`)
- Function references: a bare function name that is not called — a function
  of the current module, or one bound by `from x import f` — and `mod.f` for
  an imported module
- Calling a function value held in a local variable or parameter (`h(req)`),
  in a record field (`route.handler(req)`, `app.r.handler(req)`), or returned
  by a call (`pick()(req)`)
- Function values in locals, parameters, return values, and record fields;
  type equality is structural (same parameter and return types)

## Non-Goals

- Closures / lambdas / capturing locals (#16 keeps them)
- Referencing a builtin (`len`, `print`, ...) as a value
- `spawn` of a function value (`spawn h(req)`)
- `==` or `print` on function values
- Generic or overloaded function types
- A `Callable` keyword: the name is only special in type position, so it
  remains usable as an identifier elsewhere

## Design

### Syntax

```
type := ... | "Callable" "[" "[" (type ("," type)*)? "]" "," type "]"
```

New AST: `TypeCallable(params, ret)`; `CallValue(callee: Expr, args)` for a
call whose callee is not a bare name (`a.b.c(x)`, `f()(x)`).

The parser cannot tell `route.handler(req)` (call a field) from
`web.ok(req)` (module-qualified call): both are `Call(callee, module=...)`.
Sema decides — if the qualifier is a local variable, it is a field call;
otherwise it is an import qualifier as before (spec 17).

### Types

`types.FunctionType(params, ret)`, displayed as `Callable[[A, B], R]`, compared
structurally.

### Sema

Name resolution for an expression `Name` (not called):

1. a local variable → its value (unchanged)
2. a user function of the current module, or a `from`-imported function →
   a function reference
3. a builtin → error "cannot use builtin `len` as a value"

`FieldAccess(Name q, f)` where `q` is not a local and is an import qualifier
→ a reference to `f` in that module.

Calls:

- `Call(name)`: if `name` is a local of function type → indirect call;
  otherwise as before (constructor, builtin, user function)
- `Call(callee, module=q)`: if `q` is a local → call of field `callee` of the
  record `q`; otherwise a qualified call / constructor as before
- `CallValue(e, args)`: `e` must have a function type

Arity and argument types are checked against the function type. Resolutions
are recorded for irgen: `func_refs` (expr id → function key),
`indirect_calls` (call id → how to load the callee).

### IR and ABI

A function value is an LLVM pointer to the function's own type,
`R (P1, ..., Pn)*`, so a reference is the `ir.Function` itself and an indirect
call is `call` through the pointer. Every user function already has a stable
symbol (`rw_user_...`) and the rw calling convention, so no thunks are needed.
In a record a function value is stored as that pointer.

## Changes per component

- `rwc/ast_nodes.py`: `TypeCallable`, `CallValue`
- `rwc/parser.py`: `Callable[[...], R]` types; calls on non-name callees
- `rwc/types.py`: `FunctionType`
- `rwc/sema.py`: references, indirect / field / value calls
- `rwc/irgen.py`: function pointer types, references, indirect calls
- `examples/func_values.rw` + `.expected`
- tests: `test_parser`, `test_sema`, `test_irgen`, `test_e2e`

Layers touched: parser / sema (+ types) / irgen (3). No lexer or runtime change.

## Verification

```sh
uv run --frozen pytest tests/test_parser.py tests/test_sema.py tests/test_irgen.py -q
uv run --frozen pytest tests/test_e2e.py -k func_values -q
uv run --frozen pytest -q
```

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| `x.f(...)` meaning a field call vs a module-qualified call | Locals shadow import qualifiers; sema checks locals first and reports a field-call error if `x` is a local that is not a record with a function field |
| A local variable and a function with the same name | A local of function type wins for calls; a local of another type falls back to the function, so existing programs keep their meaning |
| LLVM type mismatch between a referenced function and the declared function type | Both derive from the same `llvm_type_of`, and sema has already required the types to be equal |

## Update (spec 71)

Closures arrived in spec 71. A function value is now `{code, env}`, and a
top-level function is passed as an environment-ignoring thunk; see that spec.
