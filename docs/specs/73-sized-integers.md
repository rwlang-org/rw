# 73. Sized integer types

Issue: #103 (relates to #21 and #23)

## Context

`int` (a 64-bit signed integer) was the only integer type. Binary formats,
checksums and hashes, and C interfaces (#23) need explicit widths and unsigned
arithmetic: an FNV-1a hash is `u64` multiplication modulo 2⁶⁴, a header field
is a `u16`.

## Goals

- `i8`, `i16`, `i32`, `i64`, `u8`, `u16`, `u32`, `u64`, plus `usize` and
  `isize`
- `int` stays as it is; `i64` and `isize` are other names for it, and
  `usize` is another name for `u64` (rw targets 64-bit platforms only)
- Integer literals take the integer type the context expects
- No implicit conversions; `T(x)` converts explicitly
- Existing `int` programs compile and behave as before

## Non-Goals

- Literal suffixes (`42_u8`)
- Implicit widening (`i32` → `i64`)
- Indexing with, or `len` returning, anything other than `int`
- `checked_add`-style builtins and overflow traps (#21; arithmetic wraps)
- Sized integers as `Dict` keys, `range` bounds, or builtin parameters that
  take `int` (convert with `int(x)`)
- 32-bit targets

## Types

| Type | Bits | Range | Same type as |
|---|---|---|---|
| `i8` / `i16` / `i32` | 8 / 16 / 32 | −2ⁿ⁻¹ … 2ⁿ⁻¹−1 | |
| `int` | 64 | −2⁶³ … 2⁶³−1 | `i64`, `isize` |
| `u8` / `u16` / `u32` | 8 / 16 / 32 | 0 … 2ⁿ−1 | |
| `u64` | 64 | 0 … 2⁶⁴−1 | `usize` |

The aliases are the same type, so `i64` values mix freely with `int` and
error messages print `int` / `u64`. The names are built-in type names, not
keywords, and are also conversion functions; a program cannot define a
function with one of these names.

Sized integers can be used anywhere a value type can: variables, parameters,
return values, record fields, constants, defaults, `List`, `Option`,
`Result`, tuples, `Channel`, `Dict` values, `Future` (`spawn` / `await`), and
value `match`.

## Literals

An integer literal has the type the context expects (spec 30's mechanism,
extended): an annotated declaration, an assignment, `return`, an argument, a
field, an element of a literal, a constant or default, a `match` pattern, and
the other operand of a binary operator. Without one it is `int`.

```rw
x: u8 = 200          # u8
y = x + 1            # u8: the literal follows x
mask: u32 = 1 << 31  # u32: both literals follow the declaration
z = 7                # int
```

A literal outside the range of its type is a compile-time error, including
for `int` (`9223372036854775808`, unless negated):

```text
error: integer literal `256` is out of range for `u8` (0..255)
```

`-128` is a negated literal; it is checked as −128, so it fits an `i8`.

## Operators

Both operands of `+ - * / % & | ^ << >>` and the comparisons have the same
integer type; the result of arithmetic has that type.

| Operation | Signed | Unsigned |
|---|---|---|
| `+` `-` `*` | wrap (two's complement) | wrap modulo 2ⁿ |
| `/` `%` | truncate toward zero; MIN / −1 panics, MIN % −1 is 0 | `udiv` / `urem` |
| division by zero | panic | panic |
| `>>` | arithmetic shift | logical shift |
| `<` `<=` `>` `>=` | signed compare | unsigned compare |
| unary `-` | wraps | **error** (write `0 - x`) |
| unary `~` | complement | complement |

`--unchecked` drops the division checks for every width, as for `int`
(spec 42). A shift by a count outside 0 … bits−1 is unspecified.

## Conversions

`int(x)`, `i8(x)`, …, `u64(x)`, `usize(x)`, `isize(x)` accept any integer,
`float`, or `bool`:

- integer → integer: truncate to the target width, or extend by the
  **source's** signedness; the bits are reinterpreted (`u8(-1)` is 255,
  `i8(200)` is −56, `int(u64 max)` is −1), as Rust's `as`
- `float` → integer: truncates toward zero; NaN or out of range is unspecified
- `bool` → integer: 0 / 1

`float(x)` accepts every integer type (`uitofp` for unsigned). `str(x)`,
`print(x)`, f-strings, and `assert_eq` accept every integer type and print
unsigned values as unsigned (`18446744073709551615`).

A type mismatch between two integer types suggests the conversion:

```text
error: operator `+` requires same type on both sides, found `u8` and `int`
     = help: convert with `u8(...)` (truncates or wraps if the value does not fit)
```

## Design

- `types.IntType(name, bits, signed)`; `INT` is `IntType("int", 64, True)`,
  so every existing `is T.INT` test still means `int`. `INT_TYPE_NAMES` maps
  every spelling, aliases included, to its type.
- sema: an `IntLit` takes an integer hint recorded by `_hint_expected`, which
  now also passes integer hints through unary `-` / `~` and arithmetic and
  bitwise operators. In a binary operator whose one side is made only of
  literals, the other side is checked first and its type becomes the
  literal side's hint. Literal ranges are checked there; an `IntLit` directly
  under unary `-` is checked negated.
- irgen: `llvm_type_of` gives `iN`. Signedness selects `sdiv`/`udiv`,
  `srem`/`urem`, `ashr`/`lshr`, signed/unsigned `icmp`, `sitofp`/`uitofp`,
  `fptosi`/`fptoui`, and `sext`/`zext`. Literals are emitted as their two's
  complement bit pattern.
- Futures carry every integer type as `int` through `rw_spawn_i64` /
  `rw_await_i64`: the trampoline widens the result and `await` truncates it.
- Runtime: `rw_print_u64`, `rw_str_from_u64`, `rw_assert_eq_fail_u64`. Other
  widths are extended to `int` and use the existing functions.

Layers touched: types / sema / irgen / runtime (+ diagnostics, vim syntax).
