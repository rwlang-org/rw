# rw default and keyword arguments

## Context

Optional parameters had to be separate functions, and long argument lists
(constructors of wide records) could only be passed by position (#17).

```rw
def greet(name: string, greeting: string = "hello", times: int = 1) -> string:
    ...

greet("ann")
greet("bob", times=2)
user = User(age=31, name="ann")
```

## Goals

- Literal defaults on parameters of functions and methods; parameters with
  defaults come last
- Keyword arguments `name=value` after positional ones, for user functions,
  methods, and record constructors (by field name); also through `spawn`
- Errors: unknown keyword, a value given twice, a missing required
  argument, a default whose type does not match, keywords passed to a
  builtin or a function value

## Non-Goals

- Non-literal defaults (Python evaluates them once at definition time, which
  is a well-known trap; literals avoid the question)
- `*args` / `**kwargs`, keyword-only parameters
- Defaults for record fields

## Design

The parser records `Param.default` and emits `KeywordArg(name, value)` in
argument lists. Before type checking a call to a user function, method, or
constructor, sema binds the arguments to parameters — positional first,
then keywords, then defaults — and **replaces** the call's argument list
with the resulting positional list. All later checks and irgen therefore see
an ordinary call; a default literal is simply emitted at each call site that
omits it.

Layers touched: parser / sema (2).
