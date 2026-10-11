# rw records: `class` with fields

## Context

Step 2 of the web framework roadmap (spec 18). With string builtins rw can
parse an HTTP request, but the parsed pieces travel as loose local variables:
a handler cannot receive "the request" or return "the response" as one value.
The framework needs user-defined aggregate types:

```rw
class Request:
    method: string
    path: string
    body: string

class Response:
    status: int
    content_type: string
    body: string

def hello(req: Request) -> Response:
    return Response(200, "text/plain", "hello " + req.path + "\n")

def main() -> int:
    res: Response = hello(Request("GET", "/x", ""))
    res.body = res.body + "!"
    print(res.status)
    return 0
```

The `class` keyword has been reserved by the lexer since the MVP; this
sub-project gives it the smallest useful meaning: a named record of typed
fields with a positional constructor, field reads, and field writes.

## Goals

- Module-level `class Name:` followed by an indented block of one or more
  `field: Type` lines
- Construction with a positional call `Name(e1, ..., en)`: one argument per
  field, in declaration order, with matching types
- Field read `e.field` (chains like `a.b.c` work) and field write
  `e.field = value` as a statement
- Classes as parameter types, return types, local variable types, and field
  types (including classes declared later in the same module)
- **Reference semantics** (like Python objects): a record lives on the heap;
  assignment and argument passing copy the reference, so a write through one
  name is visible through every other

## Non-Goals

- Methods, `self`, constructors with logic, default field values, keyword
  arguments
- Inheritance, traits / protocols (#104), generics (#105)
- Importing a class from another module, or `mod.Class` type names (a
  follow-up PR; classes are visible only in their own module for now)
- A null / absent record (`Option[Request]`) — every record value is a
  constructed object
- `==`, `print`, or string conversion of a record
- Spawning a function that **returns** a record (spawning one that takes
  records as arguments is fine)
- Freeing records (memory management RFC, #67); field writes are not
  synchronized across fibers

## Design

### Syntax and AST

```
class_def  := "class" IDENT ":" NEWLINE INDENT field_decl+ DEDENT
field_decl := IDENT ":" type NEWLINE
```

New nodes: `ClassDef(name, fields)` with `FieldDecl(name, type_expr)`, stored
in `Module.classes`; `FieldAccess(obj, field)` (expression);
`FieldAssign(obj, field, value)` (statement). Imports must still come before
any `def`, `type`, or `class`.

`e.field` is parsed in the postfix loop. A bare name followed by `.ident(`
stays a module-qualified call (spec 17); `.ident` without `(` is a field
access. Methods do not exist, so there is no ambiguity. An expression
statement whose expression is a `FieldAccess` and that is followed by `=` is a
`FieldAssign`.

### Types

`types.ClassType(name, module, fields)` compares **by identity**: two classes
named `Point` in different modules are different types. Field types are
filled in after all class names of a module are known, which is what allows
forward references between classes.

Name rules, per module: a class name must not equal another class, a type
alias, a function, or a builtin.

### Sema

- `Name(args)` where `Name` is a class of the current module is a
  constructor call: argument count and types must match the fields. It is
  recorded in `SemaResult.constructor_calls` (by `id(call)`)
- `FieldAccess`: the object must have a class type and the field must exist
- `FieldAssign`: as above, and the value type must equal the field type
- The existing `==` / `print` whitelists already reject records
- `spawn f()` where `f` returns a record is an error

### IR and ABI

A record value is an `i8*` to a heap block. The block layout is a literal
LLVM struct of the field types (record fields are themselves `i8*`, so
layouts are never recursive). The constructor `malloc`s `sizeof(struct)`
(via the GEP-on-null idiom already used by `spawn`), stores each field, and
returns the pointer. Field access and assignment bitcast the pointer to the
struct pointer type and use a GEP + load / store. No runtime C changes.

Because a record is one pointer, it passes through function calls, `spawn`
closures, ternaries, and other records unchanged.

## Changes per component

- `rwc/ast_nodes.py`: `ClassDef`, `FieldDecl`, `FieldAccess`, `FieldAssign`, `Module.classes`
- `rwc/parser.py`: class definitions, field access in the postfix loop, field assignment
- `rwc/types.py`: `ClassType`
- `rwc/sema.py`: class collection and name checks, type resolution, constructor / field access / field assignment checks
- `rwc/irgen.py`: constructor, field access, field assignment
- `examples/records.rw` + `.expected`
- tests: `test_parser`, `test_sema`, `test_irgen`, `test_e2e`

Layers touched: parser / sema (+ types) / irgen (3). The lexer already has
`class` and `.`; the runtime is unchanged.

## Verification

```sh
uv run --frozen pytest tests/test_parser.py tests/test_sema.py tests/test_irgen.py -q
uv run --frozen pytest tests/test_e2e.py -k records -q
uv run --frozen rwc run examples/records.rw
uv run --frozen pytest -q
```

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| `a.b` ambiguity between a module-qualified call and a field access | A qualified call always has `(` right after the name; fields never do. Sema additionally rejects a field access on a non-record |
| Two same-named classes in different modules treated as one type | `ClassType` compares by identity, not by name |
| Recursive layouts (`A` has a `B`, `B` has an `A`) | Record fields are pointers, so the LLVM struct of a record never contains another struct by value |
| Data races on field writes from concurrent fibers | Documented as a Non-Goal; synchronization primitives are #26 |
