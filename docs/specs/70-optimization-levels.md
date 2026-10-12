# 70. Optimization levels

Issue: #55

## Interface

`rwc build` and `rwc run` take `-O0` … `-O3`. Without the flag, the level
comes from `$RW_OPT_LEVEL`, and otherwise defaults to **2**.

The level is passed to clang, which runs the matching LLVM pipeline over the
generated IR before linking. Out-of-range values are rejected (argparse
exits 2; `compile_source(opt_level=…)` raises a `CompileError`).

## Why -O2 by default

Code was built at clang's default, -O0. Safe-point preemption (spec 23)
made tight loops especially slow there. On a loop of 300M iterations plus
`fib(36)`:

| Level | Time |
|---|---|
| -O0 | 0.77 s |
| -O2 | 0.23 s |

Debugging the IR is the main reason to want -O0. `-O0` / `RW_OPT_LEVEL=0`
still gives it.

## Is the IR safe to optimize?

- The safe-point check loads `rw_preempt_flag` with an atomic (monotonic)
  load, so LLVM cannot hoist it out of a loop.
- irgen emits no `nsw` / `nuw` flags. Signed overflow wraps, so optimizers
  cannot assume it does not happen. Integer division keeps its explicit
  checks (spec 42).
- The conservative collector (spec 67) tolerates what the optimizer does
  with pointers: interior pointers are recognized, and values held in
  registers are scanned through the saved contexts and thread states.
- The whole suite passes at -O0 (a CI job), -O2 (the default, every other
  job), and -O3 (checked locally).

## Not done

- LTO between the program and `librw.a`, which would let small runtime
  helpers such as string equality inline. The runtime is a plain static
  library built at -O2.
- A benchmark job in CI (#79).

## Tests

- `tests/test_cli_opt.py`:
  - each level builds and runs
  - -O7 is rejected
  - `RW_OPT_LEVEL` is honoured, and an explicit level wins over it
- CI: one job runs the full suite with `RW_OPT_LEVEL=0`.
