# rw `await_all` and `await_any`

## Context

Waiting for several spawned fibers meant a loop of `await`s, and there was
no way to take whichever finished first (#27).

## Goals

- `await_all(futures: List[Future[T]]) -> List[T]`: waits for all, results
  in list order (not completion order)
- `await_any(futures: List[Future[T]]) -> tuple[int, T]`: the index and value
  of the first future to finish; the others keep running and can still be
  awaited; an empty list aborts
- Each future may still be awaited only once (awaiting frees it)

## Non-Goals

- `select` over channels, and timeouts — both need a fiber to wait on several
  things at once (see Design)
- `Future[void]` in these builtins

## Design

`await_all` is lowered to a loop that awaits each element and pushes the
result. `await_any` calls `rw_await_any`, which checks each future's state
(`rw_sched_is_done`) and, if none has finished, sleeps 1 ms (parking the
fiber) before checking again; it then awaits the winner. Polling is a
deliberate first step: a fiber is linked into at most one wait list today
(the `next` pointer doubles as the run-queue link), so registering one fiber
on several targets would schedule it more than once. A multi-wait primitive
would replace the polling and also enable `select`.

Layers touched: sema / irgen / runtime (3).
