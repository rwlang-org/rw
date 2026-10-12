# rw tuples and multiple return values

## Context

A function that computes two things must return a record or encode one
value in another. Python returns a tuple and unpacks it (#9):

```rw
def divmod_(a: int, b: int) -> tuple[int, int]:
    return a / b, a % b

def main() -> int:
    q, r = divmod_(17, 5)
    pair = ("ann", 31)
    print(f"{pair[0]} is {pair[1]}, q={q} r={r}")
    return 0
```

## Goals

- Tuple types `tuple[T1, ..., Tn]` (also spelled `Tuple[...]`), n >= 2
- Literals `(e1, ..., en)` and `return e1, ..., en`
- Element access with a constant index `t[0]` (negative allowed)
- Unpacking `a, b = e` for a tuple of the same arity; each target that is not
  yet declared is declared with the element type (spec 32), `_` discards
- Tuples as values everywhere: locals, parameters, returns, record fields,
  list elements, dict values

## Non-Goals

- One-element and empty tuples; tuple equality, ordering, printing
- Indexing with a non-constant index; slicing tuples
- Unpacking in `for` headers or nested patterns
- Spawning a function that returns a tuple

## Design

`types.TupleType(elems)`, compared structurally, is an immutable value: an
LLVM literal struct of the element types, passed by value. A literal builds
the struct with `insertvalue`; `t[i]` is an `extractvalue`. Unpacking
evaluates the right-hand side once into a temporary and assigns or declares
each target from its element.

A parenthesised expression followed by a comma is a tuple literal; `(e)`
alone stays a parenthesised expression.

Layers touched: parser / sema (+ types) / irgen (3).
