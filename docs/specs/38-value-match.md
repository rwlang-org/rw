# rw `match` on values

## Context

`match` (specs 10, 11, 30) only destructures `Option` and `Result`.
Branching on a value — an HTTP method, a status code — needs an `if` /
`elif` chain. Issue #13 asks for literal patterns, bindings, and `_`.

```rw
match req.method:
    case "GET" | "HEAD":
        ...
    case "POST":
        ...
    case other:
        print(other)
```

## Goals

- `match` on an `int`, `string`, or `bool` with arms whose pattern is a
  literal (negative ints included), several literals joined by `|`, `_`
  (matches anything), or a name (matches anything and binds the value)
- Arms are tried in order; the first match runs. If none matches, nothing
  runs (as in Python)
- Errors: a pattern of the wrong type, a duplicate literal, an arm after a
  catch-all (unreachable)
- Exhaustiveness for return analysis: the match returns on every path only
  if it has a catch-all arm (or covers both `true` and `false`) and every
  arm returns

## Non-Goals

- Destructuring patterns for tuples and records, guards (`case x if ...`),
  enums — the arm representation (patterns + optional binding) leaves room
  for them
- Mixing value patterns with `Some` / `Ok` patterns in one match
- Matching floats (exact float equality is rarely what is meant)

## Design

The parser chooses the form from the first arm: `Some` / `None` / `Ok` /
`Err` keep the existing `MatchStmt`; anything else is a `ValueMatch(target,
arms)` with `MatchArm(patterns, bind, body)`. irgen evaluates the target once
and emits a chain of tests (`icmp eq` for ints and bools, `rw_str_eq` for
strings) ending in the catch-all arm, if any.

Layers touched: parser / sema / irgen (3).
