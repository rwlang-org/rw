<div align="center">
  <img src="docs/assets/logo.png" alt="rw logo" width="320">

  [Specs](docs/specs/) | [Examples](examples/) | [Issues](https://github.com/ryuichi1208/rw/issues) | [Changelog](CHANGELOG.md)
</div>

This is the main source code repository for **rw** — a statically typed,
async-first compiled language with a Python-flavored surface syntax. It
contains the compiler, the C runtime, and the language specs.

## Why rw?

- **Async first:** `spawn` / `await` and `Future[T]` are first-class. The
  fiber runtime handles 100k+ tasks on a single thread.
- **Familiar syntax:** Indentation, `def`, `elif`, `and` / `or` / `not`,
  `true` / `false`.
- **Static types:** All parameters, returns, and locals are annotated.
- **LLVM backend:** Native binaries on macOS arm64 and Linux x86_64.

## Quick Start

```sh
uv sync --extra dev
make -C runtime
uv run rwc run examples/hello.rw
```

For installation prerequisites and detailed setup, see [INSTALL.md](INSTALL.md).

## Getting Help

See [Issues](https://github.com/ryuichi1208/rw/issues) and the language specs
under [`docs/specs/`](docs/specs/).

## Contributing

Contribution guidelines are tracked in
[#71](https://github.com/ryuichi1208/rw/issues/71). For now, pick any open
issue and send a pull request.
