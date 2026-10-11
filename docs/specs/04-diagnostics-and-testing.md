# rw Diagnostics and Testing Strategy

## Diagnostic format

Every diagnostic is a three-part set: **`file:line:col` + caret + message**.

```
error: type mismatch
  --> examples/hello.rw:3:13
   |
 3 |     x: int = "hello"
   |              ^^^^^^^ expected `int`, found `string`
```

`diagnostics.py` provides a single class `Diagnostic`:

```python
@dataclass
class Diagnostic:
    file: str
    line: int      # 1-origin
    col: int       # 1-origin
    length: int
    message: str
    severity: Literal["error", "warning"]

    def render(self, source: str) -> str: ...
```

The MVP **stops at the first error**. Error recovery (collecting multiple errors) is future work.

## Where diagnostics are emitted

| Stage | Examples |
|---|---|
| Lexer | unterminated string, inconsistent indentation |
| Parser | unexpected token, expected `:` after function signature |
| Sema | undefined variable, type mismatch, wrong argument count, await on non-Future |
| IRGen | none (everything is rejected in Sema) |
| Driver | clang not found, link error |

## Test structure

Everything runs under `pytest`. Four tiers:

### 1. Unit tests

- `tests/test_lexer.py`: input string → verify the token stream. INDENT/DEDENT, blank lines,
  comment lines, mixed-indentation detection
- `tests/test_parser.py`: token stream → compare the AST via `repr`
- `tests/test_sema.py`: AST → typed AST, or the expected `Diagnostic`

### 2. IRGen snapshot tests

`tests/test_irgen.py` + `tests/snapshots/*.ll`:
- rw source → stringify the generated LLVM IR
- Fail if it differs from the existing snapshot
- Intentional changes are updated with `pytest --update-snapshots`

### 3. E2E tests

`tests/test_e2e.py`:
- Compile `examples/*.rw` with `rwc build` → run → compare stdout against the expected value
- Expected values live alongside as `examples/*.rw.expected`
- CI runs on both macOS arm64 and Linux x86_64

### 4. Diagnostic tests

`tests/test_diagnostics.py` + `tests/bad/*.rw`:
- Collect rw code that should error
- Write the expected error in a comment at the top of the file:
  ```python
  # ERROR: type mismatch
  # ERROR_LINE: 3
  x: int = "hello"
  ```
- The runner extracts the comments and matches them against the output of `rwc build`

## MVP examples (equivalent to acceptance tests)

| File | What it verifies |
|---|---|
| `examples/hello.rw` | String literals and `print` |
| `examples/arith.rw` | Integers, floats, bools, comparisons, if/else |
| `examples/fib.rw` | Recursive function `fib(20)` |
| `examples/while_count.rw` | A `while` loop over 1..10 |
| `examples/spawn_basic.rw` | `spawn add(3,4)` → `await` yields 7 |
| `examples/spawn_many.rw` | Summation across 4 parallel threads |
| `examples/spawn_string.rw` | A function returning `Future[string]` |

**Once these 7 are green, the MVP is complete.**

## CI

`.github/workflows/ci.yml`:

```yaml
strategy:
  matrix:
    os: [macos-latest, ubuntu-latest]
    python: ["3.11"]
steps:
  - uses: actions/checkout@v4
  - uses: actions/setup-python@v5
    with: { python-version: ${{ matrix.python }} }
  - run: pip install -e ".[dev]"
  - run: make -C runtime
  - run: pytest -v
```

clang ships by default on both runners, so no extra installation is needed.
