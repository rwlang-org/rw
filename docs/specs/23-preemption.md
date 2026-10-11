# rw fiber preemption at safe points

## Context

rw schedules fibers cooperatively: a fiber gives up its worker thread only
when it parks (socket I/O, file I/O, `await`) or finishes. A fiber that
computes without doing I/O keeps its worker forever:

```rw
def busy(req: Request) -> Response:
    n: int = 0
    while n < 1000000000:
        n = n + 1
    return text(200, "done")
```

In the web framework (docs/web-framework.md) one such handler per worker
stops the whole server: fibers woken by the netpoller sit in the run queues
with no thread to run them. Go solved the same problem by preempting long
running goroutines; CPython's threads switch the GIL at safe points in the
eval loop every 5 ms. This is issue #31.

## Goals

- A fiber that has run for longer than a time slice (default 10 ms) yields
  its worker at the next **safe point**, so other fibers on that worker run
- Safe points are inserted by the compiler at the start of every function
  body and at the head of every loop iteration (`while`, and `for`, which
  desugars to `while`), so every non-terminating rw computation passes
  through one
- A monitor thread (like Go's `sysmon`) decides when a fiber is overdue; the
  check on the fast path is one relaxed atomic load and a branch
- `RW_PREEMPT_MS` sets the slice in milliseconds; `0` disables preemption

## Non-Goals

- Asynchronous (signal-based) preemption at arbitrary instructions: a long
  stretch of straight-line code or a long call into the C runtime is not
  interrupted
- Preempting the main thread: it is not a fiber (spec 12), and blocking it
  does not hold a worker
- Priorities, fairness guarantees beyond "round-robin through the run queue",
  or a configurable slice per fiber
- Handing a worker blocked in a system call to another thread (Go's P
  handoff); blocking fds are handled separately by offloading to the aio pool

## Design

### Monitor thread

Each worker M gets two new fields: `tick`, incremented every time the worker
resumes a fiber, and `preempt`, a request flag. The monitor thread wakes
every millisecond and, for every worker that is running a fiber, compares
`tick` with what it saw last time. If the same fiber has been running for at
least the slice, it sets that worker's `preempt` flag and increments a global
counter `rw_preempt_flag` (the number of pending requests).

### Safe points

The compiler emits, at each safe point:

```llvm
%p = load atomic i32, i32* @rw_preempt_flag monotonic, align 4
%req = icmp ne i32 %p, 0
br i1 %req, label %preempt, label %cont
preempt:
  call void @rw_safepoint()
  br label %cont
```

`rw_safepoint()` runs on the fiber's own stack. If the calling thread is a
worker whose `preempt` flag is set, it clears the flag (atomic exchange),
decrements `rw_preempt_flag`, and calls `rw_sched_yield()`. Otherwise it
returns immediately (another worker's request, or the main thread).

`rw_sched_yield()` only swaps to the worker's scheduler context; the worker
loop re-enqueues the fiber after the swap has fully saved its context
(the "publish after save" rule of the existing scheduler), at the tail of
the run queue.

If a fiber finishes or parks before reaching a safe point, the worker clears
a pending request for itself when it regains control, so a stale request
never preempts the next fiber early.

### Why safe points are enough

A safe point is an ordinary call to a C function, exactly like the calls
that already park fibers (`read`, `await`). LLVM already treats every call
as a point where the stack and callee-saved registers must be consistent,
so switching fibers there needs no new register-saving machinery, and the
loop body's memory accesses are not hoisted across it.

## Changes per component

- `runtime/fiber/sched.c`, `sched.h`: `tick` / `preempt` per worker, the
  monitor thread (started in `rw_sched_init`, joined in `rw_sched_shutdown`),
  `rw_preempt_flag`, `rw_safepoint`, `RW_PREEMPT_MS`
- `rwc/irgen.py`: safe points at function entry and loop heads
- `runtime/fiber/test_preempt.c` (new): a spinning fiber that only passes
  through safe points no longer starves another fiber on a single worker
- `examples/preempt_basic.rw` + `.expected`: the same with rw code, run in
  the e2e suite with `RW_WORKERS=1` (it would hang without preemption)
- `tests/test_e2e.py`: a timeout for example runs, so a scheduling
  regression fails instead of hanging CI

Layers touched: irgen / runtime (2).

## Verification

```sh
make -C runtime
uv run --frozen pytest tests/test_runtime_c.py tests/test_e2e.py -k "preempt" -q
uv run --frozen pytest -q
```

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Overhead of a check in every loop iteration and call | One relaxed load + a never-taken branch; measured on `examples/fib.rw` and a tight loop and reported in the PR |
| Publishing a half-saved context when a preempted fiber is re-enqueued | `rw_safepoint` only calls `rw_sched_yield`, which swaps; re-enqueueing stays in `worker_main` after the swap returns |
| A request outliving the fiber it was meant for | The worker clears its own pending request each time it regains control |
| New interleavings in user code (a field read and write can now be separated by another fiber) | Workers already run in parallel (M:N), so this adds no new kind of data race; synchronization primitives are #26 |
