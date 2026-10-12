# 68. `rwc test` and `assert_eq`

Issue: #47

## Writing tests

```python
# test_math.rw
import mathlib

def test_add() -> void:
    assert_eq(mathlib.add(2, 3), 5)

def test_greeting() -> void:
    assert_eq(mathlib.greet("rw"), "hello, rw\n")
```

- A **test file** is named `test_*.rw` or `*_test.rw`. It is an ordinary
  module: it may import modules next to it or from the stdlib, and it must
  not define `main`.
- A **test** is a top-level function named `test_*` that takes no
  arguments. Its return value, if any, is ignored. Other functions are
  helpers.
- A test **passes** when it returns and **fails** when it exits non-zero:
  `assert`, `assert_eq`, a run-time check such as an out-of-range index or a
  division by zero, `os_exit(n)`, a crash, or the timeout.

### `assert_eq(a, b)`

A builtin that compares two values of the same type: `int`, `float`,
`bool`, or `string`. Mismatched types are a compile error. On inequality it
prints the location and both values to stderr and exits with status 1:

```
rw: assert_eq failed in test_greeting (line 8)
  left:  "hello rw\n"
  right: "hello, rw\n"
```

Strings are shown as quoted literals with escapes. If either side spans
several lines, the first differing line is shown too:

```
  first difference at line 2:
  - "two"
  + "three"
```

Floats compare with `==` and print with 17 significant digits.

## Running

```
rwc test [PATH...] [-k SUBSTRING] [--timeout SECONDS] [-v]
```

- PATHs are files or directories (default `.`). Directories are searched
  recursively, skipping hidden directories, `node_modules`, and
  `__pycache__`.
- For each file, `rwc` compiles a runner that imports the file and calls the
  test named in argv[1]. The runner is compiled as if it sat next to the
  file, so its imports resolve the same way; nothing is written there.
- Each test runs in its own process, in parallel up to the CPU count. A
  failing or crashing test cannot affect the others.
- Output per file:

  ```
  tests/test_math.rw
    PASS test_add (3 ms)
    FAIL test_greeting (2 ms)
      rw: assert_eq failed in test_greeting (line 8)
      ...
  1 passed, 1 failed in 0.41 s
  ```

  A failing test's stdout and stderr are shown indented; `-v` shows passing
  tests' output too.
- `-k` keeps tests whose names contain the substring. `--timeout` (default
  60 s) fails a test that runs longer.
- The exit status is 0 only if every test passed and every file built. A
  file that does not compile is reported with the usual diagnostic. No test
  files found is also an error.

## Tests

`tests/test_cli_test.py`:
- discovery (both name patterns, a subdirectory, non-test helpers ignored)
- pass / fail / crash reporting, with the int and multi-line string output
  of `assert_eq`
- `-k`, the success exit status, a type error in `assert_eq`, an empty
  directory, and the timeout
