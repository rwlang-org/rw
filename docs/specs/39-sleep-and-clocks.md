# rw `sleep` and clocks

## Context

rw had no notion of time (#28, #35): no way to wait without blocking a
worker, measure elapsed time, or read the wall clock.

## Goals

- `sleep(ms: int)`: parks the calling fiber for at least `ms` milliseconds;
  other fibers keep running on its worker. On the main thread (not a fiber)
  it is a plain `nanosleep`
- `time_ms() -> int`: wall clock, Unix time in milliseconds
- `mono_ns() -> int`: monotonic clock in nanoseconds, for measuring
- Many concurrent sleepers: 10,000 sleeping fibers cost O(log N) per sleep

## Non-Goals

- Timeouts on I/O and `Future` cancellation (#24, #162) — they will reuse
  the timer heap
- A `Duration` type; calendar / time-zone handling; sub-millisecond sleeps

## Design

A dedicated timer thread owns a binary min-heap of `(deadline, seq, fiber)`
(seq keeps equal deadlines FIFO). `sleep` parks the fiber with
`rw_sched_park_commit` (spec 24); the commit pushes the entry and signals the
timer thread only after the fiber's context is saved. The thread sleeps on a
condition variable until the earliest deadline and re-enqueues due fibers.
On Linux the condition variable uses `CLOCK_MONOTONIC`; macOS has no
`pthread_condattr_setclock`, so the monotonic deadline is converted to a
`CLOCK_REALTIME` one for each wait.

The names avoid the conflicting `now()` / `time()` of the two issues: the
unit and the clock are in the name.

Layers touched: sema / irgen / runtime (3).
