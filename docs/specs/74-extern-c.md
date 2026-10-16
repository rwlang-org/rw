# 74. Calling C: `extern "c"` and `ptr[T]`

Issue: #23 (builds on spec 73; a step of #230)

## Context

rw programs could only use what the runtime exposes as builtins. Calling a
libc function or a C library (zlib, sqlite, a system API) needed a new
builtin and a runtime change each time. `extern` was reserved but had no
meaning.

## Syntax

```rw
extern "c" def strlen(s: ptr[u8]) -> usize
extern "c" def c_write(fd: i32, buf: ptr[u8], n: usize) -> isize as "write"
```

- A module-level declaration with no body (no `:`). The parameters need
  types; defaults are not allowed. `-> void` for no result.
- `as "symbol"` names the C symbol when it differs from the rw name. Without
  it, the rw name is the symbol.
- The function is an ordinary rw function from then on: it is called,
  imported (`mod.strlen`, `from mod import strlen`), passed as a
  `Callable`, and spawned like any other.

## Types that cross the boundary

| rw | C |
|---|---|
| `i8` `i16` `i32` `int`/`i64` | `int8_t` … `int64_t` (`int` is `int32_t` on every supported target) |
| `u8` `u16` `u32` `u64`/`usize` | `uint8_t` … `uint64_t`, `size_t` |
| `isize` | `ssize_t`, `ptrdiff_t` |
| `float` | `double` |
| `bool` | `bool` (`_Bool`) |
| `ptr[T]` | `T *`, `void *` (`ptr[void]`) |

Anything else (`string`, `Bytes`, lists, records, ...) is a compile-time
error with a hint. A string goes to C as `cstr(s)`, bytes as `bytes_ptr(b)`
plus a length.

## Pointers

`ptr[T]` is opaque: it can be stored, passed, compared with `==` / `!=`, and
converted with these builtins. `T` only documents what it points to; there is
no dereference or arithmetic (#230).

| Builtin | Meaning |
|---|---|
| `ptr_null() -> ptr[T]` | NULL; `T` comes from the expected type (`ptr[void]` without one) |
| `ptr_is_null(p) -> bool` | |
| `cstr(s: string) -> ptr[u8]` | a NUL-terminated copy of `s` on the GC heap |
| `str_from_cstr(p: ptr[u8]) -> string` | a copy of the C string up to its NUL; `""` for NULL |
| `bytes_ptr(b: Bytes) -> ptr[u8]` | the address of `b`'s bytes (not a copy; C must not write to it) |
| `bytes_from_ptr(p: ptr[u8], n: int) -> Bytes` | a copy of `n` bytes at `p`; empty for NULL |

Memory from C (`malloc`) is freed by calling C (`free`). Memory from `cstr`
is managed by the GC: it stays alive while the rw program holds the pointer,
so C must not keep it after the call returns.

## Linking

libc is always linked. Other libraries are named on the command line, as for
clang:

```sh
rwc run app.rw -L /opt/zlib/lib -l z
```

`rwc build` and `rwc run` take repeatable `-l LIB` and `-L DIR`. A missing
symbol is a `linker error` naming it.

## Errors

```text
error: parameter `s` of extern function `puts` cannot be `string`: only integers, float, bool, and ptr[T] can be passed to C
     = help: pass a NUL-terminated copy with `cstr(s)` as a `ptr[u8]`
error: `write` is a builtin; give the extern function another name
     = help: keep the C symbol with `as`: `extern "c" def c_write(...) -> ... as "write"`
error: C symbol `abs` is already declared with a different signature
error: C symbol `rw_init` is reserved by the rw runtime
error: unsupported ABI "rust"; only extern "c" is supported
error: an extern function has no body; remove the ':'
```

Symbols starting with `rw_`, and `main`, belong to the runtime.

## Caveats

- **Fibers.** A C call runs on the calling fiber's 64 KiB stack and blocks
  its worker thread until it returns; the netpoller cannot interleave it.
  Deep C recursion or large C stack frames can overflow the stack (caught by
  the guard page as SIGSEGV).
- **Variadic functions** (`printf`, `open` with a mode) are not supported:
  calling one through a non-variadic declaration is undefined on some ABIs
  (Apple arm64).
- **stdout.** `print` goes through C stdio, so it mixes in order with C's
  `puts` / `printf`, but not with `write(1, ...)`, which bypasses the buffer.
- Structs by value, callbacks from C into rw, and global variables are not
  supported.

## Design

- Parser: `extern "c" def ... [as "sym"]` → `FuncDef(..., body=[],
  extern_symbol=sym)`. `ptr[T]` → `TypePtr`, special only in type position
  (like `Dict`).
- Sema: registers the signature like any function, then checks the types
  above, builtin-name collisions, reserved symbols, and that every
  declaration of one symbol agrees. Bodies are not checked.
- Irgen: `declare` with the C symbol. Integers narrower than 32 bits and
  `bool` get `signext` / `zeroext`, as clang emits, so the caller does the C
  ABI promotion. `ptr[T]` is `i8*`. Two declarations of one symbol share the
  `declare`.
- Runtime: `rw_cstr`, `rw_str_from_cptr`, `rw_bytes_from_ptr`.
- Driver / CLI: `-l` / `-L` go to clang before the runtime's own libraries.

Layers touched: parser / sema (+ types) / irgen / runtime / driver / CLI.
