# 75. Future cancellation and timed awaits

Issue: #24

## Context

Once spawned, a fiber ran to completion: there was no way to ask it to stop,
and `await` waited forever. A server cannot bound the time spent on a slow
backend, and a supervisor cannot shut workers down.

## Goals

- `cancel(f: Future[T]) -> void`: ask the fiber behind `f` to stop. O(1)
- `cancelled() -> bool`: inside a fiber, whether its own future was
  cancelled
- `is_done(f: Future[T]) -> bool`: whether the fiber has finished, without
  waiting
- `await_timeout(f: Future[T], ms: int) -> Option[T]`: `Some(await f)` if it
  finishes within `ms` milliseconds, otherwise `None`; the future stays valid

## Decision: cooperative cancellation, no exceptions

rw has no exceptions (spec 72), so cancellation cannot unwind a fiber.
Stopping a fiber at an arbitrary point would also skip its `defer`s and leave
a `Mutex` locked (spec 46, 47). Cancellation is therefore a **request**:

- `cancel(f)` sets a flag on the fiber's handle (one atomic store) and
  returns. It does not wake, interrupt, or wait for the fiber.
- The fiber checks `cancelled()` where stopping is safe — typically a loop
  condition — and returns whatever it chooses: a partial result, a sentinel,
  an `Err`.
- `await f` on a cancelled future returns that value. There is no separate
  "cancelled" outcome.
- Cancelling a finished future does nothing; cancelling twice is the same as
  once. Like `await`, `cancel` must not be used after the future has been
  awaited (awaiting frees it, spec 19).
- `cancelled()` is `false` on the main thread, which has no future.

```rw
def worker() -> int:
    n = 0
    while not cancelled():
        n += step()
    return n               # partial result

f = spawn worker()
...
cancel(f)
total = await f
```

## Timed awaits

```rw
match await_timeout(f, 100):
    case Some(v):
        use(v)
    case None:
        cancel(f)          # give up on it
        await f            # and reclaim it
```

- On `Some`, the future has been awaited (and freed), as with `await`.
- On `None`, nothing happened to the fiber; it may be awaited, timed out
  again, or cancelled. `ms <= 0` checks once without waiting.
- `Future[void]` is not supported (there is no `Option[void]`); use
  `is_done(f)` with `sleep`, then `await f`.
- Works on the main thread and in fibers.

## Non-Goals

- Interrupting a fiber blocked in `sleep`, I/O, `chan_recv`, or `await`:
  it notices the request when it next checks `cancelled()`. I/O deadlines
  are spec 53's `set_read_timeout` / `dial_timeout`.
- Propagating cancellation to the fibers a cancelled fiber spawned
  (structured concurrency)
- `select` over futures and channels (#27)

## Design

- The fiber handle gets `_Atomic int cancel_req` (zeroed by `calloc` at
  spawn). `rw_sched_cancel(h)` stores 1 (release); `rw_sched_cancelled()`
  loads the current fiber's flag (acquire), or returns 0 off-fiber.
- `rw_wait_timeout(f, ms)` polls `rw_sched_is_done` and sleeps 1 ms between
  checks, as `rw_await_any` does (spec 45): a fiber is on at most one wait
  list, and removing it from the target's list when the deadline wins would
  race with the target's wake-up. A multi-wait primitive can replace both.
- irgen: `await_timeout` branches on `rw_wait_timeout`; the done branch calls
  the usual `rw_await_<T>` and builds `Some`, the other yields `None`.
- Measured on macOS arm64: cancelling 100,000 parked fibers takes about
  1 ms in total. The E2E test cancels 10,000 (CI memory) and checks that the
  loop takes under 50 ms.

Layers touched: sema / irgen / runtime (3).
