# rw module constants

## Context

rw has no module-level values, so limits and settings are either repeated
literals or zero-argument functions (`examples/web.rw` has
`def max_body_bytes() -> int: return 1048576`).

## Goals

- `NAME: T = literal` at module level, for `int`, `float`, `string`, and
  `bool` literals (and negative numbers)
- Usable by name in the module, and from other modules as `mod.NAME` or via
  `from mod import NAME`
- Immutable: assigning to a constant is an error; a local declared with an
  annotation may shadow it

## Non-Goals

- Initialisers other than literals (expressions, calls, lists)
- Mutable module-level variables (shared mutable state across fibers needs
  synchronisation first, #26)

## Design

A constant is resolved at compile time: sema records each reference
(`const_refs`), and irgen emits the constant's literal in its place. There is
no global storage, so nothing is shared between fibers at run time. Names
must not clash with functions, classes, aliases, other constants, or
builtins.

Layers touched: parser / sema / irgen (3).
