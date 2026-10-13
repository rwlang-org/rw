# rw Language Reference

rw is a statically typed, compiled language with a Python-flavored surface
syntax. The compiler `rwc` (written in Python) translates rw source to LLVM IR,
and clang links it with the C runtime `librw.a` and the Boehm GC into a native
executable.

This document is the reference for the whole language. It is based on the
compiler's implementation (`rwc/lexer.py`, `rwc/parser.py`, `rwc/desugar.py`,
`rwc/sema.py`, `rwc/loader.py`, `rwc/cli.py`). The design history lives in the
specs under `docs/specs/`, cited at the end of each section as "Details:
spec NN". Where a spec and the implementation disagree, this document follows
the implementation.

A Japanese version is available at [ja/language.md](ja/language.md).

## Contents

1. [Overview](#1-overview)
2. [Lexical structure](#2-lexical-structure)
3. [Types](#3-types)
4. [Declarations and statements](#4-declarations-and-statements)
5. [Expressions and operators](#5-expressions-and-operators)
6. [Concurrency](#6-concurrency)
7. [Modules and imports](#7-modules-and-imports)
8. [Builtin function reference](#8-builtin-function-reference)
9. [Errors and run-time checks](#9-errors-and-run-time-checks)
10. [Spec index](#10-spec-index)

---

## 1. Overview

### 1.1 Program structure

An rw program consists of one or more modules (`.rw` files). The file passed
to the compiler is the entry module, and it must define `def main() -> int`.
`main` takes no arguments, and its return value becomes the process exit
status (the OS truncates it to the low 8 bits, so `return 300` exits with
44).

```rw
def main() -> int:
    print("hello, rw")
    return 0
```

Only five kinds of items may appear at the top level of a module. `import`s
come before everything else.

| Item | Example |
|---|---|
| import | `import json`, `from path import join` |
| constant | `MAX: int = 10` |
| type alias | `type Score = Dict[string, int]` |
| record | `class Point:` |
| function | `def add(a: int, b: int) -> int:` |

Statements (assignments, expressions) cannot appear at the top level. There
are no mutable global variables.

### 1.2 Compiling and running

`rwc` has the following subcommands (in the repository, run them as
`uv run rwc ...`).

| Command | Description |
|---|---|
| `rwc build FILE [-o OUT] [-O N] [--unchecked]` | Build an executable. Without `-o`, the output is `FILE` with its extension removed |
| `rwc run FILE [-O N] [--unchecked]` | Build in a temporary directory, run it, and return its exit status. No arguments can be passed to the program |
| `rwc check [-q] FILE...` | Type-check only; no IR generation or linking. Prints `FILE: ok` for each file that passes (`-q` suppresses it). Exit status 1 if any file fails |
| `rwc test [PATH...] [-k SUBSTR] [--timeout SEC] [-v]` | Run the `test_*` functions in `test_*.rw` / `*_test.rw` files ([9.4](#94-tests-rwc-test-and-assert_eq)) |
| `rwc emit-ir FILE [--unchecked]` | Write the generated LLVM IR to stdout |
| `rwc emit-ast FILE` | Print the AST of the entry module |
| `rwc --version` | Print the version |

Options:

- `-O N` (`-O0` to `-O3`): the LLVM optimization level passed to clang.
  Without it, the level comes from the environment variable `RW_OPT_LEVEL`,
  and otherwise defaults to 2. Out-of-range values are an error. Only `build`
  and `run` accept it.
- `--unchecked`: omit the run-time checks for integer division
  ([9.2](#92-run-time-checks)). `build`, `run`, and `emit-ir` accept it.

The compiler stops at the first error and prints a diagnostic with the file
name, line, column, and a caret. A misspelled name gets a
`help: did you mean ...?` line, and a type mismatch gets the conversion that
fixes it, such as `float(...)`.

If the C runtime `runtime/librw.a` is missing, the first build creates it with
`make -C runtime`. Linking needs `clang` (or `cc`) and the Boehm GC.

Details: [spec 48](specs/48-rwc-check.md), [spec 49](specs/49-did-you-mean.md),
[spec 68](specs/68-rwc-test.md), [spec 70](specs/70-optimization-levels.md)

### 1.3 Environment variables

| Variable | Read by | Meaning |
|---|---|---|
| `RW_OPT_LEVEL` | compiler | Optimization level when `-O` is omitted (0–3, default 2) |
| `RW_STDLIB` | compiler | Standard library directory (default: the repository's `stdlib/`) |
| `RW_GC_PREFIX` | compiler (`scripts/gc-prefix.sh`) | Install prefix of the Boehm GC; overrides the pkg-config / brew search |
| `RW_WORKERS` | run time | Number of worker threads that run fibers (1–64, default: CPU count) |
| `RW_PREEMPT_MS` | run time | Preemption time slice in milliseconds (0–60000, default 10; 0 disables it) |
| `RW_AIO_THREADS` | run time | Size of the thread pool that takes over blocking I/O (1–64, default 4) |

The three run-time variables ignore out-of-range or non-numeric values and use
the default. `RW_OPT_LEVEL` ignores a value that is not a number, but a number
of 4 or more is an error.

---

## 2. Lexical structure

### 2.1 Comments and lines

- Source is UTF-8 text.
- A comment runs from `#` to the end of the line. There are no block
  comments.
- One statement per line. An expression cannot continue onto the next line,
  even inside brackets, and there is no `\` line continuation. Long lists and
  argument lists are written on one line.
- Blank lines and comment-only lines are ignored for indentation.

### 2.2 Indentation

Blocks are delimited by indentation, as in Python. The line after one ending
in `:` is indented one level deeper, and returning to the outer depth closes
the block.

- Any indentation width works, but lines in one block must line up. A
  dedent that matches no enclosing depth is an error.
- Mixing tabs and spaces in one line's indentation is an error.
- A block cannot be empty. There is no `pass`.

### 2.3 Identifiers

A letter or `_`, followed by letters, digits, and `_`. Identifiers are
case-sensitive. Keywords cannot be used as identifiers.

### 2.4 Keywords

The lexer reserves these 43 keywords:

```
def return if elif else while for in break continue match case
assert with defer lambda and or not true false
spawn await class type import from as extern
void int float bool string Bytes List Option Result Future
Some None Ok Err
```

`extern` is only reserved; it has no meaning in the grammar.

### 2.5 Names that are special in context

These names are not keywords, but have a special meaning in certain positions.

| Name | Where it is special |
|---|---|
| `Dict`, `Callable`, `Channel`, `tuple` / `Tuple` | in type position, followed by `[` |
| `Mutex` | as a type name |
| `range` | only in a `for x in range(...)` header |
| `self` | the first parameter of a method |
| `_` | discards a value in tuple unpacking; a wildcard in a value `match` |
| `str` and the other builtin names | as function calls ([chapter 8](#8-builtin-function-reference)) |

`int(...)` and `float(...)` are keywords, but followed by `(` they act as
conversion functions.

### 2.6 Literals

**Integers** (`int`, 64-bit signed)

| Form | Examples |
|---|---|
| decimal | `0`, `42`, `1_000_000` |
| hexadecimal | `0xff`, `0XFF` |
| octal | `0o17` |
| binary | `0b1010` |

- A single `_` may separate two digits. One right after the prefix, at the
  start or end, or doubled, is an error.
- There are no negative literals; `-5` is unary minus.
- A decimal with leading zeros (`007`) is an error. Write octal as `0o7`.
  A run of zeros (`000`) is allowed.

**Floats** (`float`, IEEE 754 double)

Written as `3.14`, `1e10`, `2.5E-3`, `1_000.5`. A decimal point needs digits on
both sides (`.5` and `5.` are not allowed). With an exponent, the literal is a
`float` even without a decimal point.

**Strings** (`string`)

Enclosed in double quotes. There are no single-quoted, triple-quoted, or raw
strings. A string must end on the line where it starts. Only these six escapes
exist; any other is an error.

| Escape | Meaning |
|---|---|
| `\n` | newline |
| `\t` | tab |
| `\r` | carriage return |
| `\\` | backslash |
| `\"` | double quote |
| `\0` | NUL byte |

**f-strings**

In `f"..."`, each `{expr}` is replaced by `str()` of the expression. `{{` and
`}}` are a literal `{` and `}`. The text parts take the usual escapes. The
expression may contain string literals and brackets (`f"{d["k"]}"`). There are
no format specs (`{x:.2f}`), `!r`, or `=`. `f"a{x}b"` expands to
`"a" + str(x) + "b"`, so the expression's type is limited to what `str()`
accepts: `int`, `float`, `bool`, and `string`.

**Booleans and None**

`true` and `false` (lowercase). `None` is the empty side of `Option[T]`
([3.7](#37-optiont-and-resultt-e)).

Details: [spec 34](specs/34-fstrings.md)

### 2.7 Operators and punctuation

```
+  -  *  /  %  &  |  ^  ~  <<  >>
== !=  <  <=  >  >=
=  +=  -=  *=  /=  %=  &=  |=  ^=
(  )  [  ]  {  }  ,  :  .  ->
```

There is no `**`, `//`, `<<=`, `>>=`, `++`, `--`, or `!`.

---

## 3. Types

### 3.1 Type overview

| Type | Example | Semantics |
|---|---|---|
| `int` | `42` | 64-bit signed integer; value |
| `float` | `3.14` | double-precision float; value |
| `bool` | `true` | value |
| `string` | `"hi"` | immutable byte string; value |
| `Bytes` | `bytes_from_str("hi")` | immutable byte string; value |
| `List[T]` | `[1, 2, 3]` | immutable list; value |
| `Dict[K, V]` | `{"a": 1}` | insertion-ordered hash table; reference |
| `tuple[T1, ..., Tn]` | `("ann", 31)` | tuple of two or more elements; value |
| `Option[T]` | `Some(1)`, `None` | a value that may be absent; value |
| `Result[T, E]` | `Ok(1)`, `Err("bad")` | success or failure; value |
| `Callable[[P...], R]` | `add`, `lambda x: x + 1` | function value (including closures) |
| `Future[T]` | `spawn f()` | result of a running fiber |
| `Channel[T]` | `chan_new(8)` | channel between fibers; reference |
| `Mutex` | `mutex_new()` | lock; reference |
| record | `Point(1, 2)` | a type declared with `class`; reference |
| `void` | — | no value. Only as a return type, a `Callable` return type, and in `Future[void]` |

`void` cannot be the type of a variable, parameter, field, list element, or
the payload of `Option` and the like.

There are no implicit conversions. Arithmetic cannot mix `int` and `float`
either; convert explicitly, as in `float(n)`.

### 3.2 int, float, bool

- `int` `+`, `-`, and `*` wrap around in two's complement on overflow.
- `int` `/` truncates toward zero, and `%` takes the sign of the dividend
  (`-7 / 2 == -3`, `-7 % 2 == -1`; unlike Python). Division by zero is a
  run-time error ([9.2](#92-run-time-checks)).
- `float` `%` is C's `fmod`: the result has the sign of the dividend.
  `1.0 / 0.0` is `inf`.
- `bool` is only `true` / `false` and does not convert to or from integers
  implicitly (`int(true)` is 1).

### 3.3 string and Bytes

Both `string` and `Bytes` are immutable byte strings; lengths and indices
count bytes. UTF-8 validity is never checked, and case conversion and
whitespace handling only treat ASCII specially.

- `string`: concatenation with `+`, `==` / `!=`, byte-wise lexicographic
  `<` and friends, `s[i]` (a one-byte `string`), `s[a:b]`, `for c in s` (one
  byte at a time).
- `Bytes`: byte strings for I/O. Only `==` / `!=` and `len` work; there is no
  indexing or slicing. Convert with `bytes_from_str` / `str_from_bytes`.

Details: [spec 07](specs/07-string-builtins.md), [spec 08](specs/08-bytes-type.md),
[spec 18](specs/18-string-search.md), [spec 28](specs/28-string-builtins.md)

### 3.4 List[T]

`List[T]` is a list of any non-void type `T`, and lists nest
(`List[List[string]]`). Lists are immutable.

- Creation: a literal `[e1, ..., en]`, or `list_new()`.
- Appending: `list_push(xs, v)` returns a **new** list with `v` at the end;
  `xs` is unchanged. Write `xs = list_push(xs, v)`.
- `xs[i]` (a negative `i` counts from the end; out of range is a run-time
  error), `xs[a:b]` (a new list), `for x in xs`, `len(xs)`.
- `xs[i] = v` is a compile error.

An empty literal `[]` and `list_new()` take their element type from the
expected type ([3.14](#314-typing-from-the-expected-type)). Without one, they
are `List[int]`.

```rw
def main() -> int:
    names: List[string] = []
    names = list_push(names, "ann")
    names = list_push(names, "bob")
    for n in names:
        print(n)
    print(names[-1])
    print(len(names[1:]))
    return 0
```

Details: [spec 09](specs/09-list-type.md), [spec 22](specs/22-generic-list.md),
[spec 29](specs/29-list-syntax.md)

### 3.5 Dict[K, V]

The key `K` is `int` or `string`; the value `V` is any non-void type. A dict is
a heap object; assignment and argument passing share the reference.

- Creation: a literal `{k: v, ...}`, or `dict_new()`. `{}` and `dict_new()`
  need an expected type and are an error without one. A key repeated in a
  literal keeps the last value.
- `d[k]` (a missing key is a run-time error), `d[k] = v`, and compound
  assignment such as `d[k] += 1`.
- `dict_get` / `dict_has` / `dict_remove` / `dict_keys` / `dict_values`,
  `len(d)`.
- `for k in d` visits the keys in insertion order. It iterates over a
  snapshot of the keys taken at the start, so changing the dict inside the
  loop is safe. A re-assigned key keeps its position; a removed and re-added
  key moves to the end.
- Concurrent updates from several fibers are not synchronized; guard them
  with a `Mutex`.

```rw
def main() -> int:
    counts: Dict[string, int] = {}
    for w in str_split("a b a c a", " "):
        if dict_has(counts, w):
            counts[w] += 1
        else:
            counts[w] = 1
    for w in counts:
        print(w + " " + str(counts[w]))
    return 0
```

Details: [spec 31](specs/31-dict.md), [spec 36](specs/36-dict-literals.md)

### 3.6 Tuples

`tuple[T1, ..., Tn]` (also spelled `Tuple[...]`) is an immutable tuple of two
or more elements.

- Literal `(e1, ..., en)`. `return a, b` also returns a tuple. `(e)` is just
  parentheses. There are no one-element or empty tuples.
- Elements are read only with a constant index, `t[0]` or `t[-1]`. An
  out-of-range constant is a compile error.
- Unpacking `a, b = t` ([4.5](#45-assignment)).
- Tuples have no `==` and cannot be printed.

```rw
def divmod_(a: int, b: int) -> tuple[int, int]:
    return a / b, a % b

def main() -> int:
    q, r = divmod_(17, 5)
    pair = ("ann", 31)
    print(f"{pair[0]} is {pair[1]}, q={q} r={r}")
    return 0
```

Details: [spec 37](specs/37-tuples.md)

### 3.7 Option[T] and Result[T, E]

`Option[T]` is `Some(x)` or `None`; `Result[T, E]` is `Ok(x)` or `Err(e)`. `T`
and `E` are any non-void types and may nest. The payload is taken out with
`match` ([4.10](#410-match)). There is no `==`, no `print`, and no method such
as `unwrap`.

`None`, `Some(...)`, `Ok(...)`, and `Err(...)` take the expected type when
there is one. Without one, `Some(x)` is `Option[type of x]`, `None` is
`Option[int]`, `Ok(x)` is `Result[type of x, int]`, and `Err(e)` is
`Result[int, type of e]`. However, the unannotated declarations `x = None`,
`x = Ok(1)`, and `x = Err(...)` cannot be inferred and are an error.

```rw
def parse(s: string) -> Result[int, string]:
    match str_to_int(s):
        case Some(n):
            return Ok(n)
        case None:
            return Err("not a number: " + s)

def main() -> int:
    match parse("x1"):
        case Ok(n):
            print(n)
        case Err(msg):
            print(msg)
    return 0
```

Details: [spec 10](specs/10-option-type.md), [spec 11](specs/11-result-type.md),
[spec 30](specs/30-generic-option-result.md)

### 3.8 Callable (function values)

`Callable[[P1, ..., Pn], R]` is the type of a function value. With no
parameters it is `Callable[[], R]`; with no return value, `R` is `void`. Types
compare structurally (same parameter and return types mean the same type).

These are function values:

- the name of a top-level function (written without a call), a function
  brought in with `from`, and `mod.f`
- a nested `def` and a `lambda` ([4.15](#415-closures-nested-def-and-lambda))

Function values can be stored in variables, parameters, return values, record
fields, and list elements, and called as `h(x)`, `r.handler(x)`, or
`pick()(x)`. Builtins and methods cannot be used as values. Calls through a
function value do not take keyword arguments. Function values have no `==`.

Details: [spec 21](specs/21-function-values.md), [spec 71](specs/71-closures.md)

### 3.9 Future, Channel, Mutex

- `Future[T]`: the result of `spawn f(...)`. Take its value with `await`. A
  `Future` can be awaited only once.
- `Channel[T]`: a channel that sends values between fibers. A reference.
- `Mutex`: a lock. A reference.

See [chapter 6](#6-concurrency) for how to use them.

### 3.10 Records (class)

`class` declares a named record type with fields. A record is a heap object;
assignment and argument passing copy the reference. A write through one name
is visible through every other name for the same record.

```rw
class Point:
    x: int
    y: int

    def norm1(self) -> int:
        return self.x + self.y

    def shift(self, dx: int = 1) -> void:
        self.x += dx

def main() -> int:
    p = Point(1, 2)
    q = p
    q.x = 10
    print(p.x)            # 10: p and q are the same record
    p.shift(dx=5)
    print(p.norm1())      # 17
    r = Point(y=4, x=3)   # fields can be passed as keywords
    print(r.x)
    return 0
```

- Fields are written `name: Type`, one per line. Classes declared later in
  the same module can be used as types (recursion such as
  `next: Option[Node]` works).
- The constructor `Name(e1, ..., en)` takes one argument per field, in
  declaration order. Fields may also be passed by keyword. Fields have no
  default values.
- Read with `e.field`; write with `e.field = v` / `e.field += v`.
- For methods, see [4.14](#414-records-and-methods).
- There is no "null record". Use `Option[Point]` when one is needed.
- Records have no `==`, `print`, or `str()`.

Details: [spec 20](specs/20-records.md), [spec 33](specs/33-methods.md)

### 3.11 Type aliases

`type Name = Type` gives a type another name at the top level of a module.

```rw
type Handler = Callable[[string], string]
type Score = Dict[string, int]
```

An alias is visible only in its own module and cannot be imported. Forward
references are not allowed; a later alias may refer to an earlier one.

### 3.12 Value and reference semantics

| Types that behave as values | Types that behave as references |
|---|---|
| `int`, `float`, `bool`, `string`, `Bytes`, `List[T]`, `tuple[...]`, `Option[T]`, `Result[T, E]` | records, `Dict[K, V]`, `Channel[T]`, `Mutex` |

Value types are immutable, so assignment and argument passing behave as
copies, and nothing the receiver does affects the original variable. Records
and dicts placed inside a value type are still references, though (changing a
`Point` element of a `List[Point]` is visible wherever that record is
reachable). Reference types share one object, and every change is visible to
all holders. The same distinction applies to closure captures
([4.15](#415-closures-nested-def-and-lambda)).

Memory is reclaimed by the Boehm GC. There is no explicit freeing.

Details: [spec 67](specs/67-garbage-collection.md)

### 3.13 Equality, ordering, print

| Operation | Types |
|---|---|
| `==`, `!=` | `int`, `float`, `bool`, `string`, `Bytes` (both sides the same type) |
| `<`, `<=`, `>`, `>=` | `int`, `float`, `string` (both sides the same type; strings compare byte-wise lexicographically) |
| `print(x)` | `int`, `float`, `bool`, `string` |
| `str(x)` | `int`, `float`, `bool`, `string` |
| `assert_eq(a, b)` | `int`, `float`, `bool`, `string` |

Lists, dicts, tuples, Option, Result, records, and function values can be
neither compared nor printed. Write a function when you need to.

`print` writes the value and a newline to stdout. A `float` is printed with
C's `%g` (6 significant digits), so `print(1.0 / 3.0)` shows `0.333333`.
`str(float)` returns the shortest text that reads back as the same value
(trying `%.15g`, `%.16g`, then `%.17g`), so use `print(str(x))` or an f-string
when every digit matters. Both show `2.0` as `2` and `1e100` as `1e+100`. A
`bool` prints as `true` / `false`.

### 3.14 Typing from the expected type

These expressions cannot determine their type by themselves, so they use the
type the context expects.

| Expression | With no expected type |
|---|---|
| `[]`, `list_new()` | `List[int]` |
| `{}`, `dict_new()` | error |
| `chan_new(n)` | error |
| `None`, `Some(x)`, `Ok(x)`, `Err(e)` | the defaults in [3.7](#37-optiont-and-resultt-e) |
| `lambda ...` | error |

The contexts that supply an expected type are: an annotated declaration, an
assignment to an existing variable, `return`, arguments of functions, methods,
and constructors, elements of list, tuple, and dict literals, the first
argument of `list_push`, and both branches of a conditional expression.

---

## 4. Declarations and statements

### 4.1 Functions

```rw
def greet(name: string, greeting: string = "hello", times: int = 1) -> string:
    out = ""
    for i in range(times):
        out += greeting + ", " + name + "\n"
    return out
```

- Parameter and return type annotations are required. With no return value,
  write `-> void`.
- Defaults are limited to literals (`int`, `float`, `string`, `bool`, and
  negative numbers) whose type matches the parameter. Parameters with defaults
  come last. A default is inlined at each call that omits it.
- A call may pass `name=value` keyword arguments after the positional ones.
  An unknown keyword, a value given twice, or a missing required argument is
  an error. Keyword arguments work for user functions, methods, constructors,
  and `spawn`, but not for builtins or function values.
- A function returning anything but `void` must `return` on every path. The
  compiler looks at both branches of `if`/`else` and every arm of a `match`.
  `while` and `for` (even `while true:`) do not guarantee a return, so a
  `return` is also needed after the loop.
- Functions of the same module can be called regardless of definition order.
  There is no overloading.
- An unqualified call to a function that has the same name as a builtin calls
  the builtin. Avoid builtin names.

Details: [spec 41](specs/41-default-keyword-args.md)

### 4.2 Variables

```rw
def main() -> int:
    count: int = 0            # annotated declaration
    names = ["a", "b"]        # type inferred from the right side (List[string])
    count = count + len(names)
    print(count)
    return 0
```

- `x: T = e` declares a variable of type `T`.
- `x = e` with a name not yet declared declares a variable with the type of
  `e`. When the right side does not determine a type (`[]`, `{}`, `None`,
  `list_new()`, `dict_new()`, a bare `Ok(...)` / `Err(...)`, a `void`
  expression), it is an error; add an annotation.
- Later assignments must have the same type. No assignment widens the type.
- Declaring the same name twice in one scope is an error.
- Scope: the function body is one scope, and `if`/`elif`/`else`, `while`,
  `for x in ...`, `match` arms, and `with` bodies each create an inner scope.
  A variable declared inside is not visible after the block, and the same
  name can be declared again outside with another type.
- As an exception, the loop variable of `for i in range(...)` is declared in
  the enclosing scope and is still visible after the loop (holding its value
  at loop exit). The loop variable of `for x in xs` exists only inside the
  loop.
- A local variable may shadow a function or constant with the same name.

Details: [spec 32](specs/32-local-inference.md)

### 4.3 Constants

```rw
MAX_BODY: int = 1048576
GREETING: string = "hello"
RATIO: float = -0.5
```

- Written `NAME: T = literal` at the top level of a module. The value must
  be an `int`, `float`, `string`, or `bool` literal (negative numbers
  included).
- A constant is replaced by its value at compile time. Assigning to it is an
  error, but an annotated declaration may create a local with the same name
  that shadows it.
- Other modules use it as `mod.NAME` or via `from mod import NAME`.
- The name must not clash with a function, class, alias, other constant, or
  builtin.

Details: [spec 35](specs/35-constants.md)

### 4.4 Type aliases and records

See [3.11](#311-type-aliases) and [3.10](#310-records-class).

### 4.5 Assignment

| Form | Meaning |
|---|---|
| `x = e` | assign to a variable (declares it if new) |
| `r.f = e` | assign to a record field; chains such as `a.b.c = e` work |
| `d[k] = e` | assign to a dict entry; index assignment to lists and strings is an error |
| `x OP= e` | `x = x OP e`, with `OP` one of `+ - * / % & \| ^` |
| `r.f OP= e`, `d[k] OP= e` | compound assignment to fields and dict entries |
| `a, b = e` | tuple unpacking |

- Compound assignment follows the same typing rules, so `s += "!"` works.
  `x += 1` on an undeclared variable is an error.
- The target of a field compound assignment must be a path of names and field
  accesses; for a dict entry, the key must also be a literal or a plain path
  (the target is evaluated twice).
- Unpacking takes as many names as the tuple has elements. A new name is
  declared with the element type; an existing name must have a matching type.
  `_` discards the value.
- Assignment is not an expression, and there is no chained assignment
  (`a = b = 0`).

Details: [spec 27](specs/27-compound-assignment.md), [spec 37](specs/37-tuples.md)

### 4.6 if / elif / else

```rw
def sign(n: int) -> string:
    if n > 0:
        return "positive"
    elif n == 0:
        return "zero"
    else:
        return "negative"

def main() -> int:
    print(sign(-3))
    return 0
```

The condition must be a `bool`. Integers and strings are never treated as
truth values implicitly.

### 4.7 while

```rw
def main() -> int:
    i = 0
    while i < 3:
        print(i)
        i += 1
    return 0
```

There is no `while ... else`.

### 4.8 for

**Range loops**

```rw
def main() -> int:
    for i in range(3):            # 0, 1, 2
        print(i)
    for j in range(10, 0, -3):    # 10, 7, 4, 1
        print(j)
    return 0
```

- `range(stop)`, `range(start, stop)`, `range(start, stop, step)`. The
  arguments are arbitrary `int` expressions, evaluated once when the loop
  starts.
- Iterates over the half-open interval `[start, stop)` in steps of `step`. A
  negative `step` counts down; `step == 0` runs zero times.
- The loop variable is an `int`. An existing `int` variable with the same name
  is reused (another type is an error). It stays visible after the loop
  ([4.2](#42-variables)).
- `range` is only valid inside a `for` header.

**Element loops**

```rw
def main() -> int:
    for name in ["ann", "bob"]:
        print(name)
    for ch in "hi":
        print(ch)
    return 0
```

| Iterable | Loop variable |
|---|---|
| `List[T]` | `T` |
| `string` | one-byte `string` |
| `Dict[K, V]` | `K` (insertion order) |
| `Channel[T]` | `T` (receives until the channel is closed and empty) |

The iterable expression is evaluated once. There is no unpacking
(`for k, v in ...`), `enumerate`, or `zip`.

Details: [spec 13](specs/13-for-range-loop.md), [spec 29](specs/29-list-syntax.md)

### 4.9 break / continue

Leave the innermost loop, or go on to its next iteration. Using them outside a
loop is an error. In a range loop, `continue` still updates the loop variable.
They never reach a loop outside a closure body.

### 4.10 match

`match` has two forms, chosen by the first `case`.

**Destructuring Option / Result**

```rw
def main() -> int:
    d: Dict[string, int] = {"a": 1}
    match dict_get(d, "a"):
        case Some(v):
            print(v)
        case None:
            print("missing")
    return 0
```

- Write both `case Some(name):` and `case None:`, or both `case Ok(name):` and
  `case Err(name):` (in either order). A missing arm, a duplicate, or mixing
  Option and Result arms is an error.
- The bound name is visible only inside its arm and has the payload's type.
- There are no nested patterns such as `Some(_)`.

**Value match**

```rw
def describe(code: int) -> string:
    match code:
        case 200 | 204:
            return "ok"
        case 404:
            return "not found"
        case other:
            return "status " + str(other)

def main() -> int:
    print(describe(500))
    return 0
```

- The target is an `int`, `string`, or `bool`.
- A pattern is a literal (negative integers included), several literals
  joined by `|`, `_` (matches anything), or a name (matches anything and binds
  the value).
- Arms are tried top to bottom, and only the first matching arm runs. If none
  matches, nothing runs.
- A pattern of the wrong type, a duplicate literal, or an arm after a
  catch-all arm is an error.
- The whole `match` counts as returning only if it has a catch-all arm (or,
  for a `bool`, covers both `true` and `false`) and every arm returns.

Details: [spec 10](specs/10-option-type.md), [spec 11](specs/11-result-type.md),
[spec 38](specs/38-value-match.md)

### 4.11 assert

```rw
def main() -> int:
    n = 3
    assert n > 0
    assert n < 10, "n is too large"
    return 0
```

The condition is a `bool`; the optional message is a `string`. On failure it
writes `rw: assertion failed: <message>` to stderr and exits with status 1
(the message defaults to `assertion failed`).

### 4.12 with / defer

```rw
class Res:
    name: string

    def close(self) -> void:
        print("close " + self.name)

def work() -> int:
    defer print("deferred 1")
    defer print("deferred 2")
    with Res("r1") as r:
        print("using " + r.name)
        return 7

def main() -> int:
    print(work())   # using r1 / close r1 / deferred 2 / deferred 1 / 7
    return 0
```

- `defer call` runs the call when the function exits (by any `return`, or by
  falling off the end). The last one registered runs first, and only `defer`s
  that execution has reached run.
- `defer` may only appear at the top level of a function body (not inside an
  `if` or a loop), and must be followed by a call expression.
- The arguments of a deferred call are evaluated when the function exits, not
  when the `defer` is reached (unlike Go).
- In `with expr [as name]:`, the expression is a `Mutex` (locked on entry,
  unlocked on exit) or a record with a `close(self)` method taking no other
  arguments (`close` is called on exit). The `as` name must be new.
- Leaving the body through `return`, `break`, or `continue` runs the pending
  cleanups first. A `return` value is evaluated before the cleanups.

Details: [spec 47](specs/47-with-defer.md)

### 4.13 return

`return expr` returns a value, and a bare `return` returns from a `void`
function. `return a, b` returns a tuple. Returning a value from a `void`
function, or a bare `return` in a non-void function, is an error.

### 4.14 Records and methods

A `class` body may contain methods between or after its fields.

- A method is written `def name(self, params...) -> R:`, and its first
  parameter must be `self` without a type annotation. `self` has the class's
  type.
- Call it as `obj.m(args)`, where `obj` is any expression of the class type: a
  local, a field chain, an element, a call result. Methods of imported classes
  can be called too.
- A method may call other methods with `self.other()` and read and write
  `self.f`. Defaults and keyword arguments work.
- A class cannot have a field and a method with the same name. There is no
  taking a method as a value, `spawn obj.m()`, inheritance, static methods, or
  `__init__`.
- A field holding a function value can also be called as `r.handler(x)`.

Details: [spec 33](specs/33-methods.md)

### 4.15 Closures (nested def and lambda)

```rw
def make_adder(n: int) -> Callable[[int], int]:
    def add(x: int) -> int:
        return x + n          # captures n
    return add

def apply(f: Callable[[int], int], v: int) -> int:
    return f(v)

def main() -> int:
    inc = make_adder(1)
    print(inc(41))                         # 42
    print(apply(lambda x: x * 2, 21))      # 42
    double: Callable[[int], int] = lambda x: x * 2
    print(double(4))
    return 0
```

- A `def` statement inside a function body defines a closure and binds it to
  a local of type `Callable[[P...], R]`. Parameter and return annotations are
  required, and defaults are not allowed. It can call itself by its own name.
- `lambda a, b: expr` is a function whose body is one expression. Its
  parameter and return types come from the `Callable` type the context expects
  (an argument, a variable annotated with a `Callable` type, a return value, a
  record field); without such a context it is an error (`f = lambda x: x`
  does not compile). For a `Callable` returning `void`, the body's value is
  discarded.
- **Captures are by value.** A closure copies each local of the enclosing
  function (parameters included) that is declared before the closure and read
  in its body, at the moment the closure is created (when the `def` statement
  runs, or when the `lambda` expression is evaluated). Changing the outer
  variable later does not change the closure's copy.
- Assigning to a captured variable inside the closure is a compile error.
  Keep state that must be shared and changed in a reference type: a record, a
  dict, or a channel.
- A closure inside a closure can capture from both levels.
- A closure cannot be started with `spawn`.

Details: [spec 71](specs/71-closures.md)

---

## 5. Expressions and operators

### 5.1 Precedence

Lower rows bind tighter.

| Level | Operators | Associativity |
|---|---|---|
| 1 | `lambda ...: ...` | — |
| 2 | `a if cond else b` | right |
| 3 | `or` | left |
| 4 | `and` | left |
| 5 | `not` | prefix |
| 6 | `==` `!=` `<` `<=` `>` `>=` | none (one per expression) |
| 7 | `\|` | left |
| 8 | `^` | left |
| 9 | `&` | left |
| 10 | `<<` `>>` | left |
| 11 | `+` `-` | left |
| 12 | `*` `/` `%` | left |
| 13 | unary `-` `~`, `await`, `spawn` | prefix |
| 14 | call `f(...)`, index `x[i]`, slice `x[a:b]`, field `x.f` | left |

Bitwise operators bind tighter than comparisons (as in Python, unlike C):
`x & 1 == 1` is `(x & 1) == 1`. Chained comparisons such as `a < b < c` are a
syntax error; write `a < b and b < c`.

### 5.2 Arithmetic operators

| Operator | Operands | Result |
|---|---|---|
| `+` `-` `*` `/` `%` | two `int`s or two `float`s | the operand type |
| `+` | two `string`s | the concatenated `string` |
| unary `-` | `int` or `float` | the same type |

For the details of integer arithmetic, see [3.2](#32-int-float-bool).

Details: [spec 42](specs/42-checked-division.md)

### 5.3 Comparison operators

The result is a `bool`. The allowed types are in the table in
[3.13](#313-equality-ordering-print); both sides must have the same type.

### 5.4 Bitwise operators

`&`, `|`, `^`, `<<`, `>>`, and unary `~` work on `int` only. `>>` is an
arithmetic shift (it keeps the sign). The result of a shift by a count outside
0–63 is unspecified.

### 5.5 Logical operators

`and`, `or`, and `not` take only `bool`s and return a `bool`. `and` and `or`
short-circuit.

### 5.6 Conditional expression

`then if cond else els` evaluates `then` if `cond` (a `bool`) is true and
`els` otherwise. Only the chosen branch is evaluated. Both branches must have
the same type, which is the type of the whole expression. It is
right-associative: `a if p else b if q else c` is `a if p else (b if q else c)`.

Details: [spec 14](specs/14-ternary-expr.md)

### 5.7 Indexing and slicing

| Expression | Operand | Result |
|---|---|---|
| `xs[i]` | `List[T]` | `T`. A negative `i` counts from the end. Out of range is a run-time error |
| `s[i]` | `string` | a one-byte `string`. A negative `i` counts from the end. Out of range is a run-time error |
| `d[k]` | `Dict[K, V]` | `V`. A missing key is a run-time error |
| `t[n]` | tuple | element n. `n` is an integer literal (negative allowed) |
| `xs[a:b]` | `List[T]` | a new `List[T]` |
| `s[a:b]` | `string` | a `string` (no copy) |

Either slice bound may be omitted; negative bounds count from the end, and
out-of-range bounds are clamped to `[0, len]` (as in Python). There is no step
(`[::2]`). `Bytes` has neither indexing nor slicing.

### 5.8 Calls

| Form | Meaning |
|---|---|
| `f(args)` | a function of the same module, a function brought in with `from`, a builtin, a record constructor, or a local holding a function value |
| `mod.f(args)` | a function of an imported module, or `mod.Class(...)` |
| `x.m(args)` | a method call, or a call of a function-valued field |
| `expr(args)` | a call of an expression that returns a function value (`pick()(x)`) |

A local holding a function value takes precedence over a function or builtin
with the same name. A local variable takes precedence over a module qualifier
with the same name.

### 5.9 f-strings

See [2.6](#26-literals).

### 5.10 Conversions

| Call | Accepts | Result |
|---|---|---|
| `int(x)` | `int`, `float`, `bool` | a `float` truncates toward zero; a `bool` gives 0 / 1. The result for NaN or an out-of-range `float` is unspecified |
| `float(x)` | `int`, `float` | `float` |
| `str(x)` | `int`, `float`, `bool`, `string` | decimal, the shortest round-trip text ([3.13](#313-equality-ordering-print)), `"true"` / `"false"`, the string itself |

Converting from a string can fail, so use the functions that return an
`Option`: `str_to_int(s)` and `str_to_float(s)`.

Details: [spec 26](specs/26-conversions.md)

---

## 6. Concurrency

The unit of concurrency in rw is the fiber (a lightweight thread with its own
stack). Fibers are scheduled M:N onto `RW_WORKERS` worker threads and run in
parallel. `main` is not a fiber; it runs on the main thread. For the
scheduler, the netpoller, and how I/O is handled, see the
[runtime documentation](runtime.md), [spec 05](specs/05-fibers.md), and
[spec 06](specs/06-scheduler-mn.md).

### 6.1 spawn and await

```rw
def work(n: int) -> int:
    return n * n

def say(msg: string) -> void:
    print(msg)

def main() -> int:
    f: Future[int] = spawn work(7)
    print(await f)          # 49
    g = spawn say("hi")
    await g                 # Future[void]
    spawn say("detached")   # as a statement, the fiber is detached
    sleep(10)
    return 0
```

- `spawn f(args)` evaluates the arguments immediately, runs `f` in a new fiber,
  and returns a `Future[R]`. `f` must be a user-defined function (`mod.f` is
  fine); builtins, function values, closures, and methods cannot be spawned.
  Keyword arguments and defaults work.
- Only functions returning `int`, `float`, `bool`, `string`, `Bytes`, or `void`
  can be spawned. Parameter types are unrestricted (records can be passed).
- `await e` waits for a `Future[T]` to complete and returns the `T`. In a fiber
  only that fiber waits (the worker runs other fibers); on the main thread it
  blocks. A `Future` can be awaited only once.
- **Detaching**: a fiber whose `spawn` is written as an expression statement,
  discarding the `Future`, is detached and reclaimed as soon as it finishes.
  A fiber whose `Future` is kept in a variable but never awaited is never
  freed.
- When `main` returns, the process exits without waiting for running fibers.

Details: [spec 19](specs/19-fiber-lifecycle.md)

### 6.2 await_all and await_any

```rw
def slow(ms: int) -> int:
    sleep(ms)
    return ms

def main() -> int:
    futs: List[Future[int]] = [spawn slow(30), spawn slow(1)]
    i, v = await_any(futs)
    print(f"{i} finished first with {v}")
    print(await futs[0])
    more: List[Future[int]] = [spawn slow(2), spawn slow(3)]
    print(len(await_all(more)))
    return 0
```

- `await_all(fs: List[Future[T]]) -> List[T]`: waits for all of them and
  returns the results in list order.
- `await_any(fs: List[Future[T]]) -> tuple[int, T]`: returns the index and
  value of the first `Future` to finish. The others keep running and can be
  awaited later. An empty list is a run-time error.
- Neither works with `Future[void]`.

Details: [spec 45](specs/45-await-all-any.md)

### 6.3 Channels

```rw
def producer(ch: Channel[int]) -> void:
    for i in range(3):
        chan_send(ch, i)
    chan_close(ch)

def main() -> int:
    ch: Channel[int] = chan_new(1)
    spawn producer(ch)
    for v in ch:
        print(v)
    return 0
```

- `chan_new(capacity)` takes its element type from the expected type.
  Capacity 0 is a rendezvous (a send waits until a receiver takes the value);
  N buffers up to N values.
- `chan_send(ch, v)` blocks while the channel is full. Sending on a closed
  channel is a run-time error.
- `chan_recv(ch) -> Option[T]` blocks until a value arrives. After the
  channel is closed, buffered values are still delivered, then it returns
  `None`.
- `for v in ch:` receives until the channel is closed and empty. `len(ch)` is
  the number of buffered values.
- A blocked fiber parks, and its worker runs others. The main thread can send
  and receive too. There is no `select` and no send / receive with a timeout.

Details: [spec 44](specs/44-channels.md)

### 6.4 Mutex

```rw
class Counter:
    n: int

def bump(c: Counter, m: Mutex) -> void:
    for i in range(1000):
        with m:
            c.n += 1

def main() -> int:
    c = Counter(0)
    m = mutex_new()
    fs: List[Future[void]] = [spawn bump(c, m), spawn bump(c, m)]
    for f in fs:
        await f
    print(c.n)    # 2000
    return 0
```

- `mutex_new()`, `mutex_lock(m)`, `mutex_unlock(m)`,
  `mutex_try_lock(m) -> bool`. `with m:` pairs the lock and unlock.
- A waiting fiber parks, and the lock is handed over in FIFO order. Unlocking
  a `Mutex` that is not locked is a run-time error. Locks are not recursive.

Details: [spec 46](specs/46-mutex.md)

### 6.5 sleep and clocks

- `sleep(ms)` waits for at least `ms` milliseconds. A fiber parks; the main
  thread calls `nanosleep`.
- `time_ms()` is the Unix time in milliseconds; `mono_ns()` is a monotonic
  clock in nanoseconds for measuring elapsed time.

Details: [spec 39](specs/39-sleep-and-clocks.md)

### 6.6 Preemption

The compiler inserts a safe point at the start of every function and at the
head of every loop iteration. A fiber that has run longer than the time slice
(`RW_PREEMPT_MS`, default 10 ms) gives up its worker at the next safe point.
Long work inside the C runtime, and long straight-line code with no loop or
call, are not interrupted. The main thread is never preempted.

Details: [spec 23](specs/23-preemption.md)

### 6.7 I/O and fibers

I/O such as `read`, `write`, `accept`, and `dial` makes only the calling fiber
wait; the worker does not stop. Sockets and nonblocking pipes use the
netpoller (kqueue / epoll); regular files and other blocking fds use a thread
pool (`RW_AIO_THREADS`). The value of `last_error()` is kept per fiber.

Details: [spec 12](specs/12-netpoller-tcp.md), [spec 16](specs/16-async-file-io.md),
[spec 25](specs/25-blocking-fd-offload.md), [spec 51](specs/51-io-errors.md),
[spec 53](specs/53-io-timeouts.md)

---

## 7. Modules and imports

### 7.1 Import forms

```rw
import geo                          # geo.f(...), geo.Vec(...), geo.PI
import geo as g                     # g.f(...)
from geo import Vec, area as circle_area, PI
```

- `import m` loads module `m` and makes `m.f(...)`, `m.Class(...)`, the type
  `m.Class`, the constant `m.NAME`, and the function value `m.f` available.
- `import m as a` uses `a` as the qualifier. Using the same qualifier twice is
  an error.
- `from m import x [as y], ...` brings functions, classes, and constants in
  under unqualified names. Type aliases cannot be imported. An imported name
  that clashes with a function or class of the module, a builtin, or another
  imported name is an error.
- `import`s go at the top of the module, before any `def`, `type`, `class`, or
  constant.
- Each module has its own namespace, so functions with the same name in
  different modules do not clash.
- An error inside an imported module is reported with that module's file name
  and line.

### 7.2 Search path

`import foo` looks for `foo.rw` in this order:

1. the directory of the entry file
2. the standard library directory (the repository's `stdlib/`, overridable
   with the environment variable `RW_STDLIB`)

A local module shadows a standard library module of the same name. If neither
exists, the error names both paths. When an imported module imports further
modules, the search still starts from the entry file's directory.

### 7.3 Rules

- An imported module must not define `main`.
- Import cycles (including importing the entry file or the module itself) are
  an error. The message shows the cycle.
- A module imported several times is loaded only once.

### 7.4 Standard library

`stdlib/` contains these modules, written in rw. For their APIs, see the
[standard library documentation](stdlib.md).

| Module | Contents | Spec |
|---|---|---|
| `bufio` | buffered reader (`read_line`, `read_until`, `read_exact`) | [spec 54](specs/54-stdlib-bufio.md) |
| `json` | JSON parsing and output | [spec 58](specs/58-json.md) |
| `path` | POSIX path strings | [spec 59](specs/59-fs-path.md) |
| `fs` | files and directories | [spec 59](specs/59-fs-path.md) |
| `log` | leveled logging | [spec 60](specs/60-log.md) |
| `process` | child processes | [spec 61](specs/61-process.md) |
| `url` | percent-encoding and query strings | [spec 62](specs/62-url-web-requests.md) |
| `template` | Mustache-style templates | [spec 66](specs/66-template.md) |

Details: [spec 17](specs/17-namespaced-imports.md), [spec 54](specs/54-stdlib-bufio.md)

---

## 8. Builtin function reference

There are 109 builtins. All are called unqualified, take no keyword
arguments, and cannot be used as values. In the tables, `T`, `K`, and `V`
stand for any type except `void`.

The "handles" returned by the I/O functions are `int`s. Today they are OS
fds, but treat them as opaque. On failure most functions return -1 (`read`
returns empty `Bytes`) and leave the cause in `last_error()`
([9.3](#93-io-errors-last_error)).

### 8.1 Output and general

| Function | Description |
|---|---|
| `print(x: int \| float \| bool \| string) -> void` | write the value and a newline to stdout; a `float` uses `%g` |
| `len(x: string \| Bytes \| List[T] \| Dict[K, V] \| Channel[T]) -> int` | number of bytes, elements, entries, or values buffered in a channel |
| `assert_eq(a: T, b: T) -> void` | `T` is `int`, `float`, `bool`, or `string`. If they differ, write the location and both values to stderr and exit with status 1 |

### 8.2 Conversions

| Function | Description |
|---|---|
| `int(x: int \| float \| bool) -> int` | a `float` truncates toward zero; a `bool` gives 0 / 1 |
| `float(x: int \| float) -> float` | |
| `str(x: int \| float \| bool \| string) -> string` | a `float` gives the shortest round-trip text |

### 8.3 Strings

All indices and lengths count bytes. "Whitespace" is ASCII whitespace
(space, `\t`, `\n`, `\r`, `\v`, `\f`).

| Function | Description |
|---|---|
| `str_find(s: string, sub: string) -> int` | index of the first occurrence, or -1; an empty `sub` gives 0 |
| `str_rfind(s: string, sub: string) -> int` | index of the last occurrence, or -1; an empty `sub` gives `len(s)` |
| `str_slice(s: string, start: int, end: int) -> string` | bytes `[start, end)`. Both bounds are clamped to `[0, len(s)]`; `start >= end` gives `""`. Negative values do not count from the end |
| `str_starts_with(s: string, prefix: string) -> bool` | |
| `str_ends_with(s: string, suffix: string) -> bool` | |
| `str_contains(s: string, sub: string) -> bool` | `str_find(s, sub) >= 0` |
| `str_split(s: string, sep: string) -> List[string]` | the pieces between occurrences of `sep`; `"a,,b"` gives `["a", "", "b"]`; an empty `sep` gives `[s]` |
| `str_join(parts: List[string], sep: string) -> string` | the parts joined with `sep` between them |
| `str_strip(s: string) -> string` | without leading and trailing whitespace |
| `str_lstrip(s: string) -> string` | without leading whitespace |
| `str_rstrip(s: string) -> string` | without trailing whitespace |
| `str_replace(s: string, old: string, new: string) -> string` | every non-overlapping `old` replaced, left to right; an empty `old` returns `s` |
| `str_lower(s: string) -> string` | ASCII letters to lowercase |
| `str_upper(s: string) -> string` | ASCII letters to uppercase |
| `str_from_int(n: int) -> string` | decimal text (same as `str(n)`) |
| `str_to_int(s: string) -> Option[int]` | the value if `s` is an optional `-` followed by one or more digits and nothing else; empty, whitespace, `+`, trailing characters, or overflow give `None` |
| `str_to_float(s: string) -> Option[float]` | parsed with `strtod` syntax; `None` if anything is left over or `s` starts with whitespace |
| `byte_at(s: string, i: int) -> int` | the byte at index `i` (0–255), or -1 when out of range |
| `chr(cp: int) -> string` | the UTF-8 encoding of code point `cp`; invalid values (negative, surrogates, above U+10FFFF) give U+FFFD |
| `str_from_byte(b: int) -> string` | a one-byte string holding the low 8 bits of `b` |

Details: [spec 18](specs/18-string-search.md), [spec 28](specs/28-string-builtins.md),
[spec 55](specs/55-string-extras.md), [spec 58](specs/58-json.md),
[spec 62](specs/62-url-web-requests.md)

### 8.4 Bytes

| Function | Description |
|---|---|
| `bytes_from_str(s: string) -> Bytes` | `Bytes` with the same bytes |
| `str_from_bytes(b: Bytes) -> string` | a `string` with the same bytes (no UTF-8 check) |

### 8.5 Lists

| Function | Description |
|---|---|
| `list_new() -> List[T]` | an empty list; `T` comes from the expected type (`int` without one) |
| `list_push(xs: List[T], v: T) -> List[T]` | a new list with `v` at the end; `xs` is unchanged |
| `list_at(xs: List[T], i: int) -> T` | element `i`; `i < 0` or out of range is a run-time error (no negative indices) |
| `list_at_opt(xs: List[int], i: int) -> Option[int]` | `None` when out of range; `List[int]` only |

### 8.6 Dicts

| Function | Description |
|---|---|
| `dict_new() -> Dict[K, V]` | an empty dict; the type comes from the expected type (an error without one) |
| `dict_get(d: Dict[K, V], k: K) -> Option[V]` | the value, or `None` |
| `dict_has(d: Dict[K, V], k: K) -> bool` | |
| `dict_remove(d: Dict[K, V], k: K) -> bool` | remove the key; `true` if it was present |
| `dict_keys(d: Dict[K, V]) -> List[K]` | the keys in insertion order |
| `dict_values(d: Dict[K, V]) -> List[V]` | the values in insertion order |

### 8.7 Math

| Function | Description |
|---|---|
| `sqrt(x: float) -> float` | square root |
| `floor(x: float) -> float` | round down |
| `ceil(x: float) -> float` | round up |
| `exp(x: float) -> float` | e to the power x |
| `log(x: float) -> float` | natural logarithm |
| `sin(x: float) -> float` | sine (radians) |
| `cos(x: float) -> float` | cosine (radians) |
| `fabs(x: float) -> float` | absolute value |
| `pow(x: float, y: float) -> float` | x to the power y |

Convert an `int` argument with `float(n)`.

### 8.8 Time

| Function | Description |
|---|---|
| `sleep(ms: int) -> void` | wait for at least `ms` milliseconds |
| `time_ms() -> int` | Unix time in milliseconds |
| `mono_ns() -> int` | monotonic clock in nanoseconds |

### 8.9 Random numbers

| Function | Description |
|---|---|
| `rand_int(lo: int, hi: int) -> int` | uniform in `[lo, hi]` (inclusive). `lo > hi` is a run-time error |
| `rand_float() -> float` | uniform in `[0, 1)` |
| `rand_seed(s: int) -> void` | make the following `rand_int` / `rand_float` sequence reproducible |
| `rand_bytes(n: int) -> Bytes` | `n` bytes from the OS CSPRNG; use it for secrets |

Details: [spec 43](specs/43-random.md)

### 8.10 Concurrency

| Function | Description |
|---|---|
| `await_all(fs: List[Future[T]]) -> List[T]` | wait for all; results in list order |
| `await_any(fs: List[Future[T]]) -> tuple[int, T]` | index and value of the first to finish; an empty list is a run-time error |
| `chan_new(capacity: int) -> Channel[T]` | type from the expected type. Capacity 0 is a rendezvous; a negative capacity is a run-time error |
| `chan_send(ch: Channel[T], v: T) -> void` | blocks while full; a closed channel is a run-time error |
| `chan_recv(ch: Channel[T]) -> Option[T]` | blocks until a value arrives; `None` when closed and empty |
| `chan_close(ch: Channel[T]) -> void` | close the channel |
| `mutex_new() -> Mutex` | |
| `mutex_lock(m: Mutex) -> void` | wait until acquired (FIFO) |
| `mutex_unlock(m: Mutex) -> void` | a run-time error if not locked |
| `mutex_try_lock(m: Mutex) -> bool` | try to acquire without waiting |

### 8.11 OS

| Function | Description |
|---|---|
| `os_args() -> List[string]` | command-line arguments, program name first |
| `os_getenv(name: string) -> Option[string]` | `None` when unset |
| `os_setenv(name: string, value: string) -> bool` | set, overwriting; `false` on failure |
| `os_exit(code: int) -> void` | flush stdout and stderr and exit. `defer`s do not run, and fibers are not waited for |
| `os_getcwd() -> string` | current directory |
| `os_chdir(path: string) -> bool` | `false` on failure |
| `signal_wait(names: List[string]) -> string` | wait until one of the named signals arrives and return its name |

`signal_wait` accepts the names `"SIGINT"`, `"SIGTERM"`, `"SIGHUP"`,
`"SIGQUIT"`, `"SIGUSR1"`, and `"SIGUSR2"`; if none is recognized it returns
`""` at once. Once named, a signal loses its default action (terminating the
process) and is delivered only through `signal_wait`. Signals that arrive
while nobody waits are counted and returned by later calls, one per call.

Details: [spec 40](specs/40-os-api.md), [spec 57](specs/57-signals-graceful-shutdown.md)

### 8.12 Processes

| Function | Description |
|---|---|
| `proc_start(argv: List[string]) -> List[int]` | start `argv[0]`, looked up in `PATH`, and return `[pid, stdin, stdout, stderr]` (the parent's pipe handles); `[]` on failure |
| `proc_wait(pid: int) -> int` | wait for exit: the exit status, `128 + N` if killed by signal N, or -1 on error |
| `proc_kill(pid: int, sig: string) -> int` | send a signal by name (`"SIGTERM"`, `"SIGKILL"`, ...); 0, or -1 on error |

No shell is involved. Usually you want the standard library's `process`
module.

Details: [spec 61](specs/61-process.md)

### 8.13 File system

| Function | Description |
|---|---|
| `fs_kind(p: string) -> string` | `"file"`, `"dir"`, or `"other"`; `""` on error. Follows symlinks |
| `fs_size(p: string) -> int` | size in bytes; -1 on error |
| `fs_mtime_ms(p: string) -> int` | modification time (Unix milliseconds); -1 on error |
| `fs_read_dir(p: string) -> List[string]` | entry names in byte order, without `.` and `..` |
| `fs_rename(src: string, dst: string) -> int` | 0, or -1 on error |
| `fs_remove(p: string) -> int` | remove a file or an empty directory; 0, or -1 on error |
| `fs_mkdir(p: string) -> int` | create with mode 0755; 0, or -1 on error |

Details: [spec 59](specs/59-fs-path.md)

### 8.14 I/O

| Function | Description |
|---|---|
| `read(h: int, max: int) -> Bytes` | read up to `max` bytes; empty at EOF, on error, or on timeout. At EOF, `last_error()` is 0 |
| `write(h: int, b: Bytes) -> int` | bytes written (possibly fewer than `len(b)`); -1 on error |
| `write_all(h: int, b: Bytes) -> int` | write everything; `len(b)`, or -1 on error (some prefix may have been sent) |
| `close(h: int) -> int` | close; 0, or -1 on error. Fibers waiting on the handle are woken with `EBADF` |
| `open(addr: string, mode: string) -> int` | open `file:///abs/path` or a path without `:`. `mode` is `"r"`, `"w"` (create, truncate), or `"a"` (create, append) |
| `file_open(path: string, mode: string) -> int` | legacy API; same modes as `open` |

Handles `0`, `1`, and `2` are stdin, stdout, and stderr.

Details: [spec 15](specs/15-file-io.md), [spec 50](specs/50-dial-listen-open.md),
[spec 52](specs/52-write-all.md)

### 8.15 Networking

Addresses take the forms `tcp://host:port`, `udp://host:port`, and
`unix:///abs/path`. IPv6 hosts go in brackets, as in `tcp://[::1]:80`. For
`listen`, `tcp://:port` with no host binds every IPv4 interface, and port 0
picks a free port.

| Function | Description |
|---|---|
| `dial(addr: string) -> int` | connect (`tcp`, `unix`, `udp`); `udp` gives a connected socket |
| `dial_timeout(addr: string, ms: int) -> int` | `dial` that gives up connecting after `ms` milliseconds (name resolution not included) |
| `listen(addr: string) -> int` | bind and listen (`tcp` and `unix` with backlog 128; `udp` only binds) |
| `accept(ln: int) -> int` | handle of the next connection (`tcp`, `unix`); -1 on error |
| `local_addr(h: int) -> string` | own address (in a form `dial` accepts); `""` on error |
| `peer_addr(h: int) -> string` | remote address; `""` on error |
| `recv_from(h: int, max: int) -> tuple[Bytes, string]` | one UDP datagram and its sender `"udp://ip:port"`; both empty on error or timeout |
| `send_to(h: int, data: Bytes, addr: string) -> int` | send one datagram to `addr` (`udp://`); bytes sent, or -1 on error |
| `shutdown(h: int, how: string) -> int` | half-close; `how` is `"read"`, `"write"`, or `"both"`. 0 / -1 |
| `set_nodelay(h: int, on: bool) -> int` | `TCP_NODELAY`. 0 / -1 |
| `set_keepalive(h: int, on: bool) -> int` | `SO_KEEPALIVE`. 0 / -1 |
| `set_backlog(ln: int, n: int) -> int` | pending-connection queue length (`n < 1` is `EINVAL`; capped at 65535). 0 / -1 |
| `set_read_timeout(h: int, ms: int) -> void` | limit for each `read`, `accept`, and `recv_from`; `ms <= 0` disables it |
| `set_write_timeout(h: int, ms: int) -> void` | limit for each `write`, `write_all`, and `send_to`; `ms <= 0` disables it |
| `tcp_listen(port: int) -> int` | legacy API; listen on every IPv4 interface |
| `tcp_accept(ln: int) -> int` | legacy API; same as `accept` |

Timeouts apply to sockets only, and set `last_error()` to `ETIMEDOUT`. Closing
the handle, or creating a new handle with the same fd number, clears them.

Details: [spec 50](specs/50-dial-listen-open.md), [spec 53](specs/53-io-timeouts.md),
[spec 56](specs/56-socket-options.md), [spec 69](specs/69-udp.md)

### 8.16 Errors

| Function | Description |
|---|---|
| `last_error() -> int` | outcome of this fiber's most recent I/O builtin: 0 for success, otherwise an errno value |
| `error_name(e: int) -> string` | a name such as `"ECONNREFUSED"`; `""` for 0; `"E<n>"` for an unknown value |
| `error_message(e: int) -> string` | the `strerror` text; `""` for 0 |
| `set_last_error(e: int) -> void` | set `last_error()`; used to restore a cause that a cleanup call such as `close` overwrote |

### 8.17 GC

| Function | Description |
|---|---|
| `gc_collect() -> void` | run a full garbage collection now |
| `gc_heap_size() -> int` | size of the GC heap in bytes |

Details: [spec 67](specs/67-garbage-collection.md)

---

## 9. Errors and run-time checks

rw has no exceptions. Operations that can fail return their outcome as an
`Option` / `Result` or a sentinel such as -1, and unrecoverable mistakes end
the process (a panic). A panic cannot be caught.

### 9.1 Compile-time errors

Type mismatches, undefined names, wrong argument counts, missing `return`s,
and similar mistakes are detected at compile time. The compiler stops at the
first error.

### 9.2 Run-time checks

| Situation | Message (on stderr) | Ends with |
|---|---|---|
| `assert` fails | `rw: assertion failed: <message>` | exit status 1 |
| `assert_eq` fails | `rw: assert_eq failed in <function> (line N)` and both values | exit status 1 |
| integer division by zero (`/`, `%`) | `rw: assertion failed: division by zero in <function> (line N)` | exit status 1 |
| `INT64_MIN / -1` | `rw: assertion failed: integer overflow in division in <function> (line N)` | exit status 1 |
| list index out of range (`xs[i]`, `list_at`) | `rw: list_at: index out of bounds` | abort (SIGABRT) |
| string index out of range | `rw: string index out of range` | abort |
| missing dict key (`d[k]`) | `rw: dict key not found: <key>` | abort |
| send on a closed channel | `rw: send on a closed channel` | abort |
| unlocking a `Mutex` that is not locked | `rw: unlock of a mutex that is not locked` | abort |
| `rand_int(lo, hi)` with `lo > hi` | `rw: rand_int: lo is greater than hi` | abort |
| `await_any` of an empty list | `rw: await_any of an empty list` | abort |
| negative `chan_new` capacity | `rw: chan_new: negative capacity` | abort |

- `--unchecked` omits the integer division checks. Division by zero is then
  undefined behaviour. `INT64_MIN % -1` is 0 with or without the checks.
- Overflow in `+`, `-`, and `*` is not checked; it wraps.
- Float division follows IEEE 754 (`inf`, `nan`).
- `rwc run` returns the child's exit status. An abort ends the child with a
  signal.

Details: [spec 42](specs/42-checked-division.md)

### 9.3 I/O errors (last_error)

I/O builtins report failure as -1 or empty `Bytes` and store the cause,
errno-style, per fiber. `last_error()` returns the outcome of the most recent
I/O builtin (0 on success). errno numbers differ between operating systems, so
compare the names from `error_name`.

```rw
def main() -> int:
    c = dial("tcp://127.0.0.1:1")
    if c < 0:
        print(error_name(last_error()))    # e.g. ECONNREFUSED
    fd = open("/no/such/file", "r")
    if fd < 0:
        print(error_message(last_error()))
    return 0
```

- When `read` returns empty bytes, `last_error()` is 0 at EOF and nonzero on
  an error.
- A malformed address or an unsupported `open` mode is `EINVAL`; a
  name-resolution failure is rw's own `ENOHOST` (10001).
- The I/O, networking, file system, and process builtins update
  `last_error()`. Other functions leave it unchanged.
- `SIGPIPE` is ignored, so writing to a peer that closed does not kill the
  process; it fails with `EPIPE` or a similar error.

Details: [spec 51](specs/51-io-errors.md)

### 9.4 Tests (rwc test and assert_eq)

```rw
# test_math.rw
def add(a: int, b: int) -> int:
    return a + b

def test_add() -> void:
    assert_eq(add(2, 3), 5)
```

- A test file is an ordinary module named `test_*.rw` or `*_test.rw`. It does
  not define `main`.
- A test is a top-level function `test_*` that takes no arguments. Its return
  value is ignored.
- Each test runs in its own process, in parallel. It passes when it returns
  normally, and fails on an `assert` or `assert_eq` failure, a run-time check,
  `os_exit(n)` with n ≠ 0, a crash, or the timeout.
- `assert_eq` shows strings as quoted literals with escapes and, for
  multi-line values, the first line that differs. Floats compare with `==` and
  print with 17 significant digits.

Details: [spec 68](specs/68-rwc-test.md)

---

## 10. Spec index

The design history of each feature is in these specs. Specs 01–06 describe
the early MVP and differ from the current implementation in places (no type
inference, no `for`, and so on).

| Area | Specs |
|---|---|
| Overview and MVP | [01](specs/01-overview.md), [02](specs/02-syntax-and-types.md), [03](specs/03-runtime-and-irgen.md), [04](specs/04-diagnostics-and-testing.md) |
| Fibers and scheduler | [05](specs/05-fibers.md), [06](specs/06-scheduler-mn.md), [19](specs/19-fiber-lifecycle.md), [23](specs/23-preemption.md), [24](specs/24-park-after-save.md) |
| Strings and Bytes | [07](specs/07-string-builtins.md), [08](specs/08-bytes-type.md), [18](specs/18-string-search.md), [28](specs/28-string-builtins.md), [55](specs/55-string-extras.md) |
| Collections | [09](specs/09-list-type.md), [22](specs/22-generic-list.md), [29](specs/29-list-syntax.md), [31](specs/31-dict.md), [36](specs/36-dict-literals.md), [37](specs/37-tuples.md) |
| Option / Result / match | [10](specs/10-option-type.md), [11](specs/11-result-type.md), [30](specs/30-generic-option-result.md), [38](specs/38-value-match.md) |
| Syntax | [13](specs/13-for-range-loop.md), [14](specs/14-ternary-expr.md), [27](specs/27-compound-assignment.md), [32](specs/32-local-inference.md), [34](specs/34-fstrings.md), [35](specs/35-constants.md), [41](specs/41-default-keyword-args.md), [47](specs/47-with-defer.md) |
| Records, function values, closures | [20](specs/20-records.md), [21](specs/21-function-values.md), [33](specs/33-methods.md), [71](specs/71-closures.md) |
| Conversions and arithmetic | [26](specs/26-conversions.md), [42](specs/42-checked-division.md) |
| Modules | [17](specs/17-namespaced-imports.md), [54](specs/54-stdlib-bufio.md) |
| Concurrency | [39](specs/39-sleep-and-clocks.md), [44](specs/44-channels.md), [45](specs/45-await-all-any.md), [46](specs/46-mutex.md) |
| I/O and networking | [12](specs/12-netpoller-tcp.md), [15](specs/15-file-io.md), [16](specs/16-async-file-io.md), [25](specs/25-blocking-fd-offload.md), [50](specs/50-dial-listen-open.md), [51](specs/51-io-errors.md), [52](specs/52-write-all.md), [53](specs/53-io-timeouts.md), [56](specs/56-socket-options.md), [69](specs/69-udp.md) |
| OS, processes, file system | [40](specs/40-os-api.md), [43](specs/43-random.md), [57](specs/57-signals-graceful-shutdown.md), [59](specs/59-fs-path.md), [61](specs/61-process.md) |
| Standard library and web | [58](specs/58-json.md), [60](specs/60-log.md), [62](specs/62-url-web-requests.md), [63](specs/63-web-static-middleware.md), [64](specs/64-web-keepalive.md), [65](specs/65-web-chunked-requests.md), [66](specs/66-template.md) |
| Tools and runtime | [48](specs/48-rwc-check.md), [49](specs/49-did-you-mean.md), [67](specs/67-garbage-collection.md), [68](specs/68-rwc-test.md), [70](specs/70-optimization-levels.md) |
