# 72. Error handling RFC: `Result` over exceptions, and `?`

Issues: #14, #68

## Summary

rw handles recoverable errors with values — `Option[T]`, `Result[T, E]`,
and, for OS-level failures, `last_error()` — and has no exceptions.
Unrecoverable mistakes panic or abort the process. This RFC records the
choice, the alternatives that were weighed, and the guideline for which
mechanism an API should use. It adds one piece of syntax, the postfix `?`
operator, which removes the boilerplate that made `Result` awkward.

## Motivation

The mechanisms already exist (spec 10, 11, 30, 42, 51), but the decision was
never written down: `docs/specs/01-overview.md` still listed "exceptions /
Result type" as future work. Without `?`, every fallible call needed a
five-line `match` just to forward the error, which pushed library code
towards sentinels (`-1`, `""`) instead of `Result`.

## Options considered

| | A. Exceptions (`try` / `except` / `raise`) | B. `Result` + `?` (Rust, Swift `throws`-like) | C. Multiple returns `(v, err)` (Go) | D. Both A and B |
|---|---|---|---|---|
| Failure visible in the signature | no | yes | yes | partly |
| Cost on the success path | unwinding tables, or a check after every call | a tag check where `?` is written | a check where written | both |
| Interaction with fibers | an exception must not cross `spawn` / `await`; needs rules for what `await` re-raises | a `Result` is an ordinary value; `await` returns it unchanged | ordinary value | both sets of rules |
| Interaction with `defer` / `with` (spec 47) | cleanups must run during unwinding | `?` is an early `return`, cleanups already run there | plain `return` | both |
| Forgetting to handle an error | the error escapes silently to the caller | `match` / `?` needed to get the value out | easy to ignore `err` | — |
| Familiarity for Python users | high | medium | low | high |
| Implementation in rwc / runtime | landing pads or setjmp per frame; personality routine; GC roots in unwound frames | sema check + one branch in irgen | tuples (spec 37) | both |

### Why not exceptions

- **Signatures stop telling the truth.** rw is statically typed so that a
  reader can see what a function does from its type; an exception is an
  untyped second return channel.
- **Fibers.** A fiber's stack is switched by hand (spec 05). Unwinding has to
  stop at the fiber boundary and be re-thrown by `await`, and `await_any`
  (spec 45) would need rules for the losers. With values, a
  `Future[Result[T, E]]` just carries the `Result`.
- **Cost and complexity.** Zero-cost unwinding needs LLVM landing pads in
  every function that has a cleanup, a personality routine in the runtime,
  and unwind tables for the hand-written fiber switch. A setjmp-based scheme
  costs on every call instead.
- **Two error models.** Adding exceptions next to `Option` / `Result`
  (option D) means every API has to pick one and every caller has to know
  which; libraries end up wrapping one in the other.

### Compatibility with Python's `try` / `except`

Python code ported to rw maps as follows:

| Python | rw |
|---|---|
| `raise ValueError(msg)` | `return Err(msg)` |
| a call that may raise, letting it propagate | `f(x)?` |
| `try: ... except E as e: handle(e)` | `match f(x): case Ok(v): ... case Err(e): handle(e)` |
| `finally:` / `with` | `defer` / `with` (spec 47) |
| `int(s)` raising on bad input | `str_to_int(s)` returning `Option[int]` |
| an uncaught exception ending the program | a panic (exit status 1) |

`try`, `except`, `raise`, and `finally` stay unreserved; they can be added
later as sugar over `Result` if experience shows the need (see Open
Questions).

## Decision

1. **Recoverable errors are values.** A function that can fail returns
   `Option[T]` when the absence needs no explanation and `Result[T, E]`
   when it does.
2. **`?` propagates them** (below).
3. **Unrecoverable errors end the process** and cannot be caught: a *panic*
   prints a message and exits with status 1; an *abort* prints a message and
   raises SIGABRT. The table of run-time checks is in `docs/language.md` §9.2.
4. **OS failures keep the errno-style `last_error()`** (spec 51). A
   `Result`-returning layer over the I/O builtins may come later; it would
   not change the builtins.

### Panic vs abort

| | panic | abort |
|---|---|---|
| Raised by | `assert`, `assert_eq`, integer division checks (spec 42) — checks the compiler inserts or the user writes | runtime library invariants: index out of range, missing dict key, send on a closed channel, misuse of `Mutex` |
| Ends with | exit status 1 | SIGABRT (core dump if enabled) |
| Under `rwc test` | the test fails | the test fails (crash) |
| Catchable | no | no |

The split follows where the check lives (compiled code vs C runtime), not
how serious it is. Both mean "this program has a bug".

## The `?` operator

```rw
def parse_point(s: string) -> Result[Point, string]:
    parts = str_split(s, ",")
    if len(parts) != 2:
        return Err(f"expected x,y: \"{s}\"")
    x = parse_int(parts[0])?
    y = parse_int(parts[1])?
    return Ok(Point(x, y))
```

- `e?` is a postfix operator, binding like a call, index, or field access:
  `f(x)?.field`, `xs[0]?`, `g()??`. Prefix operators bind looser, so
  `await fut?` is `await (fut?)`; write `(await fut)?` to unwrap the awaited
  value.
- On `e: Option[T]` the value of `e?` is the `Some` payload, of type `T`. If
  `e` is `None`, the enclosing function returns `None`. The function must
  return some `Option[U]`; `U` need not be `T`.
- On `e: Result[T, E]` the value is the `Ok` payload, of type `T`. If `e` is
  `Err(x)`, the function returns `Err(x)`. The function must return
  `Result[U, E]` with the **same** `E`; there is no automatic conversion.
- The early return behaves exactly like `return`: pending `defer`s and
  `with` cleanups run, innermost first (spec 47).
- "The enclosing function" is the innermost `def` or `lambda`, so `?` works
  in closures whose return type is an Option or a Result.
- Errors:
  - `` `?` requires an Option or a Result, found `int` ``
  - `` `?` on `Option[int]` needs the function to return an Option, but it returns `int` ``
  - `` `?` on `Result[int, int]` needs the function to return `Result[_, int]`, but it returns `Result[int, string]` ``
  - `` `?` cannot be used in a deferred statement `` — a deferred call runs
    while the function is already returning.

### Implementation

`?` is a new token (`QUESTION`) and AST node (`TryExpr`), parsed in the
postfix loop. sema looks up the return type of the function being checked
(closures are checked as their own synthetic functions, spec 71). irgen
extracts the tag, branches to a block that builds `None` / `Err(e)` from a
zero constant of the function's return type, emits the pending cleanups, and
returns; the other branch continues with the payload at index 1.

Layers touched: lexer / parser / sema / irgen (4).

## Guidelines for APIs

| Situation | Use | Examples |
|---|---|---|
| A lookup that may find nothing, a conversion that may not apply | `Option[T]` | `str_to_int`, `list_at_opt`, `dict_get` |
| Parsing input, validating data — the caller wants to know why | `Result[T, string]` with `"<problem> at line L, column C"` when there is a position | `json.parse`, `template.compile` |
| A domain error the caller branches on | `Result[T, E]` with a record or an int code as `E` | — |
| A failure reported by the OS | the builtin's sentinel (`-1`, empty `Bytes`, `None`, `false`) + `last_error()` | `open`, `dial`, `read`, `fs.*`, `process.*` |
| A broken invariant, a caller's programming error | panic (`assert`) | out-of-range argument documented as a precondition |

- Prefer `Result[T, string]` to inventing an error type until callers
  actually branch on the kind of error.
- Do not panic on bad *input*; panic on bad *use*.
- Do not return sentinels from new non-I/O functions; use `Option`.

## Non-Goals

- Error type conversion on `?` (Rust's `From`), error chaining or wrapping
- `?` in `main` (it returns `int`) or in functions returning other types
- Methods on `Option` / `Result` (`unwrap`, `expect`, `map`, `ok_or`, ...)
- Stack traces for panics
- Catching panics, including in tests

## Open Questions

- `unwrap()` / `expect(msg)` as builtins or methods, once methods on
  built-in types exist.
- A `Result[T, IoError]` façade over the I/O builtins (spec 51 option A).
- An error type richer than `string` for the standard library, likely after
  traits (#104) or generics (#105) make a common interface possible.
- Whether `try` / `except` sugar over `Result` is worth reserving keywords.
