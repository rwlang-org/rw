# rw generic `Option[T]` and `Result[T, E]`

## Context

`Option[int]` (spec 10) and `Result[int, int]` (spec 11) were introduced
monomorphically. Real code needs other payloads: a lookup that may find a
record (`Option[User]`), a parser that fails with a message
(`Result[int, string]`). With `List[T]` generic (spec 22), these are the
remaining hard-coded types (#15, #14).

## Goals

- `Option[T]` and `Result[T, E]` for any non-void `T`, `E`, nested freely
  (`List[Option[string]]`, `Option[List[int]]`, ...)
- `Some(x)`, `None`, `Ok(x)`, `Err(e)` take their type from the expected
  type where there is one (declarations, assignments, returns, arguments,
  fields, list elements, both arms of a ternary); without one, the unknown
  part defaults to `int`, so existing programs keep their meaning
- `match` binds the payload with its real type
- Existing helpers that return `Option[int]` (`str_to_int`, `list_at_opt`)
  are unchanged

## Non-Goals

- `==` / `print` on options and results; methods (`unwrap`, `map`, ...)
- `?`-style propagation
- Spawning a function that returns an option or a result
- Generic user functions (#105)

## Design

`types.OptionType(inner)` and `types.ResultType(ok, err)`, compared
structurally, with `Option[int]` / `Result[int, int]` canonicalised to the
existing `OPTION_INT` / `RESULT_INT_INT` objects.

| Type | LLVM layout |
|---|---|
| `Option[T]` | `{i64 tag, T value}` — identical to the old `Option[int]` for `T = int`, so the runtime ABI is unchanged |
| `Result[T, E]` | `{i64 tag, T ok, E err}` (no runtime helper uses `Result`) |

`tag` is 1 for `Some` / `Ok` and 0 for `None` / `Err`. Constructors start
from a zero constant of the result type and insert the tag and the payload.

Expected types reach `None` / `Some` / `Ok` / `Err` through the same hint
mechanism that types `list_new()` and `[]` (spec 22, 29).

Layers touched: parser / sema (+ types) / irgen (3).
