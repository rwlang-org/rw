# rw Syntax and Type System

## Literals and primitive types

| Type | Literal examples | LLVM representation |
|---|---|---|
| `int` | `42`, `-7`, `0` | `i64` |
| `float` | `3.14`, `-0.5` | `double` |
| `bool` | `true`, `false` | `i1` (`i8` in the function ABI) |
| `string` | `"hello"` | `{i64 len, i8* ptr}` (immutable, not concatenable) |
| `Future[T]` | no literal | `i8*` (opaque) |

## Operators

- Arithmetic: `+ - * / %` (only between operands of the same type)
- Comparison: `== != < <= > >=`
- Logical: `and`, `or`, `not`
- Conditional expression (ternary): `then if cond else els` (lowest precedence, right-associative)
- Assignment: `=` (reassignment is allowed, but the type is fixed)

### Conditional expression (ternary operator)

The Python-compatible `then if cond else els`. `cond` is a `bool`; `then` and `els` must
have the same type, which becomes the type of the whole expression (there is no implicit
type promotion). Only the selected branch is evaluated.

```python
larger: int = a if a > b else b
label: string = "even" if a % 2 == 0 else "odd"
# right-associative: a if p else (b if q else c)
sign: int = 1 if n > 0 else 0 if n == 0 else -1
```

See [`14-ternary-expr.md`](14-ternary-expr.md) for details.

## Function definitions

```python
def add(a: int, b: int) -> int:
    return a + b

def greet(name: string) -> void:
    print(name)
```

- Type annotations on arguments and return values are **mandatory**
- No return value is written as `-> void`
- Local variables also require type annotations:
  ```python
  x: int = 1 + 2
  ```

## Control flow

```python
if x > 0:
    print("positive")
elif x == 0:
    print("zero")
else:
    print("negative")

while i < 10:
    i = i + 1
```

When choosing a value based on a condition, you can use a conditional expression (ternary
operator) instead of a statement-level `if`/`else`:

```python
larger: int = a if a > b else b
```

A `for ... in range(...)` counting loop is also available (see
[`13-for-range-loop.md`](13-for-range-loop.md)). The `for <var> in <list>` iterator form
is not yet supported.

## Async syntax

```python
def fetch(n: int) -> int:
    return n * 2

f: Future[int] = spawn fetch(21)
result: int = await f
print(result)
```

- `spawn expr` **runs a function call on a separate thread** and returns a `Future[T]`
- `await expr` waits for the `Future[T]` to complete and extracts the `T`
- The target of `spawn` must be **a function call only** (not an arbitrary expression)

## The main function

```python
def main() -> int:
    print("hello")
    return 0
```

`main` is required. Its return value is the process exit code (`i64` truncated to `i32`).

## Comments

```python
# Line comment. Use consecutive # for multiple lines.
```

## Reserved words

```
def return if elif else while and or not true false void
spawn await Future
int float bool string
# Reserved for future use (syntax error in the MVP):
extern class import for in as None
```

## Notes on lexical structure

- 4 spaces of indentation is recommended, but 2 spaces or tabs are fine as long as they are consistent within the file
- Mixing tabs and spaces **within the same block is forbidden** (the Lexer errors out)
- Blank lines and comment-only lines are excluded from indentation computation
