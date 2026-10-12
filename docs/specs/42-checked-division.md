# rw run-time checks for integer division

## Context

Integer `/` and `%` compiled straight to LLVM `sdiv` / `srem`. Dividing by
zero, and `INT64_MIN / -1`, are undefined behaviour there: in practice the
process dies with SIGFPE, or the optimizer assumes it never happens (#62).

## Goals

- `a / b` and `a % b` on ints panic with a message naming the function and
  line when `b == 0`: `rw: division by zero in div (line 2)`
- `INT64_MIN / -1` panics (`integer overflow in division ...`) because the
  quotient does not fit in an int; `INT64_MIN % -1` is `0`
- `rwc build|run|emit-ir --unchecked` omits the checks
- Division keeps truncating toward zero (`7 / -2 == -3`), as before

## Non-Goals

- Overflow checks for `+`, `-`, `*` (they wrap; the policy is #21)
- Float division (IEEE: `x / 0.0` is `inf` or `nan`)
- Recovering from the panic

## Design

irgen emits, before each integer `sdiv` / `srem`, a compare-and-branch to a
block that calls the existing `rw_panic` (used by `assert`) with a constant
message. For `%`, a divisor of `-1` is replaced by `1` and the result
selected as `0`, so `srem` never sees the trapping case. Compound
assignments (`x /= y`) desugar to the same operators and are covered.

Layers touched: irgen / driver / CLI.
