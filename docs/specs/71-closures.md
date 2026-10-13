# 71. Closures: nested `def` and `lambda`

Issue: #16

## Syntax

```python
def make_adder(n: int) -> Callable[[int], int]:
    def add(x: int) -> int:        # a nested def
        return x + n               # n is captured
    return add

inc: Callable[[int], int] = lambda x: x + 1
web.get(app, "/ping", lambda req: text(200, "pong\n"))
```

- A `def` statement inside a function body defines a closure and binds it
  to a local of type `Callable[[P...], R]`. Parameters and the return type
  are annotated as for any function. Default arguments are not allowed, and
  keyword arguments cannot be used: function values reject them (spec 41).
  A nested def may call itself by name.
- `lambda a, b: expr` is an expression with a single-expression body. Its
  parameter types and return type come from the `Callable` type the context
  expects: a parameter, a variable declared with a Callable type, a return
  value, or a record field. Without such a context it is an error ("cannot
  infer the parameter types of this lambda"). For a `void` Callable, the body
  is evaluated and the value discarded.
- `lambda` is now a keyword.

## Capture semantics: by value

A closure may read any local of the enclosing function, including
parameters, that is declared before the closure. Each one it reads is
**copied into the closure when the closure is created**: when the `def`
statement runs, or when the lambda expression is evaluated.

```python
count = 0
def show() -> string:
    return str(count)
count = 5
print(show())        # "0": the value at creation
```

- **Captured variables are read-only inside the closure.** Assigning one
  (`=`, `+=`, a `for` loop variable, a tuple target) is a compile error that
  names the variable. A local the closure declares itself is its own.
- **Records, dicts, and channels are references**, so a closure that
  captures one can change what it refers to (`c.value += 1`), and the change
  is visible outside. Shared mutable state is written that way, explicitly.
- Lists and strings are immutable values (spec 29), so a captured list
  cannot change under the closure.
- A closure inside a closure captures from both levels: what the inner one
  reads from the outer function is also captured by the middle one, which
  needs it to build the inner closure.
- `break` / `continue` never cross into a closure, and `return` returns
  from the closure.

This rules out Python's late-binding surprise. A lambda built in a loop sees
that iteration's values.

## Representation

- A `Callable` value is now `{i8* code, i8* env}` (it used to be a bare
  function pointer).
- `code` has type `R (i8* env, P1, ..., Pn)`. Calls through a value pass the
  environment first.
- A top-level function used as a value is `{thunk, null}`. The thunk
  (`rw_fnval_<name>`, internal) ignores the environment and calls the
  function, so top-level functions keep their ordinary signature.
- Creating a closure allocates the environment, a struct of the captured
  values in name order, on the GC heap (spec 67) and stores the values. A
  closure with no captures has a null environment.
- The closure's code is a separate internal function (`rw_closure_…`). Its
  prologue copies each capture from the environment into a local slot, and
  binds its own name to `{itself, env}` for recursion. The body is then
  emitted like any function body.

Sema checks each closure body as its own function, keyed
`<outer>$<name>$<n>` so its locals do not collide with the enclosing
function's. It records the captures (the outer names actually read,
propagated to enclosing closures) for irgen. Calls through local function
values record the value's type per call, which also covers loop variables
and captures.

## Not supported

- Spawning a closure value (`spawn f()` with a local `f`), as before for
  function values.
- `nonlocal`-style shared variables. Use a record.
- Generic closures (#105).

## Tests

- `examples/closures.rw` (expected output):
  - an adder factory, a lambda as an argument, snapshot semantics
  - a captured record mutated, recursion
  - a closure returning a lambda that captures two levels
  - a list of closures
- `tests/test_closures.py`:
  - which names are captured (and propagated)
  - each assignment error, records being mutable
  - closure-local names, later variables not visible
  - lambda inference errors, lambda body type errors
  - `break` not crossing into a closure, default and redefinition errors
  - `spawn` rejected
- `examples/web_app.rw`: middleware built by a closure factory
  (`require_token("/admin", "secret")`) and a lambda route; the web e2e test
  covers both.
