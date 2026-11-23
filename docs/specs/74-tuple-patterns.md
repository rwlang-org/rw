# 74. Tuple patterns in `match`

Issue: #13

## Syntax

```rw
match (method, path):
    case ("GET", "/"):
        return "index"
    case ("GET", p):
        return "get " + p
    case ("POST", "/login") | ("POST", "/signin"):
        return "login"
    case (m, _):
        return "unsupported " + m
```

A value match (spec 38) may target a tuple (spec 37). Its arms are then
tuple patterns, `_`, or a name.

- A tuple pattern is `(p1, ..., pn)` with n >= 2 (a trailing comma is
  allowed). Each element is an `int` / `string` / `bool` literal (negative
  ints included), `_`, a name, or a nested tuple pattern.
- An element literal matches when it equals the element (`==` for ints and
  bools, string equality for strings). `_` matches anything. A name matches
  anything and binds the element, with the element's type, inside the arm.
- Several tuple patterns may be joined by `|`. Patterns joined this way
  cannot bind names.
- A tuple pattern made only of `_`, names, and such nested patterns matches
  every value, so it is a catch-all: an arm after it is an error, and the
  `match` returns on every path if every arm returns.

## Errors

- A tuple pattern whose length differs from the tuple's
- A literal whose type differs from the element's (so an element of type
  `float`, a record, and so on can only be matched by `_` or a name)
- A tuple pattern on a non-tuple value, or a literal pattern on a tuple
- A name bound twice in one pattern, or a name already declared in the
  function
- A name bound in patterns joined by `|`
- An arm after a catch-all

## Non-goals

- Exhaustiveness beyond catch-alls: `(true, _)` and `(false, _)` together
  do not count as covering a `tuple[bool, int]`
- Duplicate detection between tuple patterns
- Guards (`case (x, y) if x > y:`) and record patterns

## Design

`MatchArm.tuple_pats` holds the alternatives. irgen evaluates the target
once, takes elements with `extractvalue`, and branches on the OR over the
alternatives of the AND of each one's equality tests. An alternative with
no tests always matches.

Layers touched: parser / sema / irgen (3).
