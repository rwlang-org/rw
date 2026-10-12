# rw local type inference (`x = expr` declares `x`)

## Context

Every local in rw needs an annotation: `names: List[string] = str_split(s, ",")`.
The type is already known from the right-hand side, and Python — whose
surface rw follows — declares a variable by assigning to it. Issue #12.

```rw
def main() -> int:
    names = str_split("a,b", ",")      # List[string]
    total = 0                          # int
    for n in names:
        total += len(n)
    user = User("ann", 31)             # User
    found = dict_get(index, "ann")     # Option[int]
    return total
```

## Goals

- An assignment to a name that is not yet declared in scope declares it,
  with the type of the right-hand side
- Annotated declarations (`x: T = e`) keep working and are still required
  when the right-hand side does not determine a type: `[]`, `None`,
  `list_new()`, `dict_new()`, a bare `Ok(...)` / `Err(...)` — these are an
  error rather than silently defaulting to `int`
- Later assignments to the variable must have the same type, as before

## Non-Goals

- Inferring function parameter or return types
- Widening a variable's type by later assignments
- Declaring through compound assignment (`x += 1` on an undeclared `x`
  stays an error) or through field / index assignment

## Design

Sema treats `Assign(x, e)` with `x` not in scope as a declaration: it checks
`e` with no expected type, rejects the untyped forms above and `void`, and
adds `x` to the current scope. The declaration's type is recorded **per
statement** (`decl_types[id(stmt)]`), and irgen allocates the slot from it.

The same per-statement record replaces the `(function, name)`-keyed lookup
for annotated declarations, which fixes a latent bug: two sibling blocks
declaring the same name with different types (e.g. in `if` and `else`)
shared one entry, so one of them got a slot of the wrong type.

Layers touched: sema / irgen (2).
