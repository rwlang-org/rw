# rw `with` and `defer`

## Context

Releasing a resource on every exit path — unlock a mutex, close a file —
had to be repeated before each `return` and `break` (#102).

## Goals

- `defer <call>` at the top level of a function body: the call runs when the
  function returns, by any `return` or by falling off the end, last deferred
  first. Only defers already reached run
- `with <expr> [as name]:` where the value is a `Mutex` (locked on entry,
  unlocked on exit) or a record with a `close(self)` method (called on exit)
- Exits through `return`, `break`, and `continue` inside the body run the
  pending cleanups first; a `return` value is evaluated before them, as in
  Python

## Non-Goals

- `defer` inside loops or branches (Go re-evaluates and accumulates them at
  run time; rw keeps them static)
- Exceptions (rw has none), `__enter__` / `__exit__` protocols, several
  context managers in one `with`
- `Mutex[T]` guards (#26)

## Design

irgen keeps a per-function stack of pending cleanups, each tagged with the
loop depth at which it was pushed. A `return` emits all of them innermost
first; `break` / `continue` emit those pushed inside the innermost loop; the
end of a `with` body emits its own. Cleanups are emitted as code at each exit
point, so there is no run-time bookkeeping. `with` and `defer` become
keywords.

Layers touched: lexer / parser / sema / irgen (4).
