# Installing rw

`rw` consists of a Python-based compiler (`rwc`) and a small C runtime
(`librw.a`). Both need to be built before you can run programs.

## 1. Prerequisites

| Tool | Purpose | macOS | Linux (Ubuntu/Debian) |
|---|---|---|---|
| **uv** | Manage Python deps and runner | `brew install uv` or the official installer | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| **clang** | Link LLVM IR to native binaries | Xcode Command Line Tools (`xcode-select --install`) | `sudo apt install clang` |
| **make** + **cc** | Build the C runtime `librw.a` | Xcode Command Line Tools | `sudo apt install build-essential` |
| Python 3.12 | The compiler itself | Fetched automatically by uv | Fetched automatically by uv |

Python is pinned to 3.12 via `.python-version`. `uv sync` downloads it
automatically, so you do not need a system Python.

## 2. Clone the repository

```sh
git clone https://github.com/ryuichi1208/rw.git
cd rw
```

## 3. Set up (two steps)

```sh
# (a) Create the Python virtualenv and install deps
uv sync --extra dev

# (b) Build the C runtime librw.a
make -C runtime
```

After this, `.venv/`, `runtime/librw.a`, and `uv.lock` exist and `rwc` is
usable.

## 4. Smoke tests

```sh
# Hello world
uv run rwc run examples/hello.rw
# => hello

# Async sample (spawn + await)
uv run rwc run examples/spawn_basic.rw
# => 7

# Full test suite (~3s)
uv run pytest
```

## 5. (Optional) Put `rwc` on your PATH

```sh
# A. This shell only
source .venv/bin/activate
rwc run examples/hello.rw

# B. Permanent symlink
ln -s "$PWD/.venv/bin/rwc" /usr/local/bin/rwc
```

## 6. (Optional) Vim syntax highlighting

A syntax file is bundled under `vim/`.

```sh
mkdir -p ~/.vim/syntax ~/.vim/ftdetect
ln -s "$PWD/vim/syntax/rw.vim"   ~/.vim/syntax/rw.vim
ln -s "$PWD/vim/ftdetect/rw.vim" ~/.vim/ftdetect/rw.vim
```

Or, in your `.vimrc`:

```vim
set runtimepath+=/path/to/rw/vim
```

## CLI cheat sheet

```
rwc build  foo.rw [-o foo]   # Build a native binary
rwc run    foo.rw            # Build and execute immediately
rwc emit-ir  foo.rw          # Dump the generated LLVM IR
rwc emit-ast foo.rw          # Dump the parsed AST
```

## Examples

See [`examples/`](examples/) for runnable snippets:

| File | What it shows |
|---|---|
| `hello.rw` | String literals and `print` |
| `arith.rw` | Int / float / bool, comparison, if/elif/else |
| `fib.rw` | Recursive `fib(20)` |
| `while_count.rw` | `while` loop from 1..5 |
| `spawn_basic.rw` | `spawn add(3, 4)` → `await` returns 7 |
| `spawn_many.rw` | 4 parallel fibers, summed |
| `spawn_string.rw` | `Future[string]` |

Run any of them with `uv run rwc run examples/<name>.rw`.

## Development workflow

```sh
uv sync --extra dev              # Install deps
make -C runtime                  # Build librw.a
uv run pytest -v                 # Run all tests
uv run pytest tests/test_e2e.py  # E2E only
```

To experiment with new syntax, edit
`rwc/lexer.py` → `rwc/parser.py` → `rwc/sema.py` → `rwc/irgen.py` in order.
Every file is kept under 500 lines.
