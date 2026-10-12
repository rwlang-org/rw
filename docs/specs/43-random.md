# rw random numbers

## Context

rw had no random numbers (#36): no dice for a game, no shuffled test data,
no session tokens.

## Goals

| Builtin | Result |
|---|---|
| `rand_int(lo, hi) -> int` | uniform in `[lo, hi]` (inclusive, like Python's `randint`); aborts if `lo > hi` |
| `rand_float() -> float` | uniform in `[0, 1)` |
| `rand_seed(s)` | make the following sequence reproducible |
| `rand_bytes(n) -> Bytes` | `n` bytes from the OS CSPRNG, for secrets |

## Non-Goals

- Per-fiber streams, distributions other than uniform, shuffling helpers
- Using the seeded generator for anything security-related (use
  `rand_bytes`)

## Design

One xoshiro256** generator behind a mutex (fibers on several workers share
it), seeded from the OS on first use unless `rand_seed` was called;
`rand_seed` expands the seed with splitmix64. `rand_int` uses rejection
sampling so every value in the range is equally likely, and handles the full
64-bit range. `rand_float` takes the top 53 bits. `rand_bytes` calls
`arc4random_buf` (macOS) or `getrandom` (Linux).

The issue's `rand.int(...)` spelling would need namespaced builtins; the
`rand_` prefix follows the existing convention.

Layers touched: sema / irgen / runtime (3).
