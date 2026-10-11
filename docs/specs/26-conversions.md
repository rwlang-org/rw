# rw type conversions and string ordering

## Context

rw has no way to turn an `int` into a `float`, a `float` into an `int`, or
any number into a string other than `str_from_int` (spec 18). Mixed
arithmetic therefore cannot be written at all (`+` requires both sides to
have the same numeric type), and strings cannot be ordered, so a list of
names cannot be sorted. These are the most basic gaps found when probing
the language for "minimum features":

```rw
def average(total: int, n: int) -> float:
    return float(total) / float(n)

def main() -> int:
    print("avg = " + str(average(7, 2)))   # avg = 3.5
    print(int(3.9))                         # 3
    if "apple" < "banana":
        print("ordered")
    return 0
```

## Goals

- `int(x)` for `int`, `float` (truncates toward zero), and `bool` (0 / 1)
- `float(x)` for `int` and `float`
- `str(x)` for `int`, `float`, `bool` (`"true"` / `"false"`), and `string`
- `str(float)` prints the shortest text that reads back as the same value,
  as Python's `repr` does (`0.1`, `3.5`, `1e+100`), so no precision is lost
- `<`, `<=`, `>`, `>=` on two strings, comparing bytes lexicographically
  (shorter prefix first), like Python's comparison of byte strings

## Non-Goals

- `int(string)` / `float(string)`: parsing can fail, so it belongs with a
  generic `Option[T]`; `str_to_int` (spec 18) stays the way to parse ints
- `int(x)` on a `float` that is NaN or out of the `int` range (the result
  is unspecified, as in C)
- Locale-aware or Unicode collation for string ordering
- Implicit numeric conversions in arithmetic

## Design

`int` and `float` are keywords (type names), so the parser accepts
`int(` and `float(` in expression position and turns them into ordinary
builtin calls named `int` / `float`. `str` is a plain identifier and is
added to the builtin set like the others.

| Call | IR |
|---|---|
| `int(float)` | `fptosi` |
| `int(bool)` | `zext i8 → i64` |
| `float(int)` | `sitofp` |
| `int(int)`, `float(float)`, `str(string)` | the value itself |
| `str(int)` | `rw_str_from_int` (spec 18) |
| `str(float)` | `rw_str_from_float` (new) |
| `str(bool)` | a `select` between two string constants |

`rw_str_from_float` formats with `%.15g`, and falls back to `%.17g` when
the shorter text does not parse back to the same `double`.

String ordering calls a new `rw_str_cmp(a, b) -> int64` (`memcmp` over the
common prefix, then the shorter string first) and compares the result with
0.

## Changes per component

- `rwc/parser.py`: `int(` / `float(` in expression position
- `rwc/sema.py`: `int` / `float` / `str` builtins; string operands for `<` `<=` `>` `>=`
- `rwc/irgen.py`: conversions and string comparisons
- `runtime/runtime.h`, `runtime/runtime.c`: `rw_str_from_float`, `rw_str_cmp`
- `examples/conversions.rw` + `.expected`
- tests: `test_parser`, `test_sema`, `test_irgen`, `test_e2e`

Layers touched: parser / sema / irgen / runtime (4).

## Verification

```sh
uv run --frozen pytest tests/test_parser.py tests/test_sema.py tests/test_irgen.py -q
uv run --frozen pytest tests/test_e2e.py -k conversions -q
uv run --frozen pytest -q
```
