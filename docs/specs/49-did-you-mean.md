# 49. Did-you-mean suggestions and conversion hints

Issue: #59

## Motivation

A typo in a name or a missing conversion currently produces a bare error.
Pointing at the likely fix saves a round trip.

## Rendering

`Diagnostic` gains an optional `help` string, printed as one extra line after
the caret:

```
error: undefined variable: cuont
    --> app.rw:3:11
     |
  3  |     print(cuont)
     |           ^^^^^
     = help: did you mean `count`?
```

## Name suggestions

`suggest(name, candidates)` returns at most three candidates, nearest first.
A candidate qualifies when it equals `name` ignoring case, or is within
`max(1, len(name) // 3)` edits (insert, delete, substitute, or swap of two
adjacent characters). No help line is printed when nothing qualifies.

| Error | Candidates |
| --- | --- |
| undefined variable | locals, module constants, `from`-imported names, module functions |
| undefined function | module functions, `from`-imported names, builtins, class names |
| unknown type | builtin types, type aliases, classes |
| `module 'm' has no function` | functions (and constants) of `m` |
| `C has no field` / `has no field or method` | fields and methods of `C` |

## Conversion hints

Type mismatches (declarations, assignments, returns, call arguments) get a
hint when a standard fix exists:

| Expected | Found | Help |
| --- | --- | --- |
| `float` | `int` | convert with `float(...)` |
| `int` | `float` | convert with `int(...)` (truncates toward zero) |
| `string` | `int` / `float` / `bool` | convert with `str(...)` |
| `int` | `string` | parse with `str_to_int(...)` (returns `Option[int]`) |
| `Option[T]` | `T` | wrap the value with `Some(...)` |
| `Result[T, E]` | `T` | wrap the value with `Ok(...)` |
