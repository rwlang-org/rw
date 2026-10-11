# rw Language Overview

## What kind of language is it?

rw is a **statically typed, async-first, compiled language with the ergonomics of Python**.
It uses LLVM as its backend and produces native executables for macOS arm64 / Linux x86_64.

## Design pillars

1. **Async at the core**: `Future[T]` is a first-class citizen of the type system. `spawn`/`await` are reserved words.
2. **Python-like appearance**: indentation-based, `def`, `elif`, `and/or/not`, `true/false`.
3. **Static typing with mandatory type annotations**: arguments, return values, and local variables all require type annotations. The MVP has no type inference.
4. **Thin runtime + LLVM**: a `librw.a` written in C is linked in to provide core features (threads, Future, print).
5. **Learning- and experiment-friendly**: the compiler is written in Python. `rwc emit-ir` / `emit-ast` let you inspect the internals.

## Targets

- macOS arm64
- Linux x86_64

Windows and embedded targets are out of scope for the MVP.

## Toolchain

| Tool | Purpose |
|---|---|
| Python 3.11 | The compiler itself |
| llvmlite | Building LLVM IR |
| clang | Invoking the linker + linking against librw.a |
| make + cc | Building librw.a |

## Pipeline

```
.rw → Lexer → Parser → Sema → IRGen → Driver → executable
                                          ↓
                                     librw.a (C)
```

## CLI

```
rwc build foo.rw [-o foo]
rwc run   foo.rw
rwc emit-ir  foo.rw
rwc emit-ast foo.rw
```

## MVP goal

The 7 programs under `examples/` (hello, arith, fib, while_count, spawn_basic, spawn_many,
spawn_string) should be **green on both macOS arm64 and Linux x86_64**.

## Non-goals (future extensions)

- list / dict / for / class / import
- Type inference
- GC (leaks are tolerated for now; only Future and malloc/free pairs are managed)
- String concatenation / slicing
- Direct Python calls (to be supported later via `extern "c"` + process isolation)
- Exceptions / Result type
- Multi-error recovery (the MVP stops at the first error)
