# rw fiber lifecycle: detach and safe reclamation

## Context

A server written in rw spawns one fiber per connection and never awaits it:

```rw
while true:
    client: int = tcp_accept(listen_fd)
    spawn handle(client)
```

Until now a fiber handle — including its 64 KiB mmap'd stack — was freed only
by `rw_sched_join_*` (that is, by `await`). A fire-and-forget `spawn` therefore
leaked one handle and one stack per connection, which makes any long-running
server (spec 18's `http_hello.rw`, and the web framework that follows) run out
of memory. This is issue #32.

While designing the fix, a second problem surfaced in the existing join path.
`finalize_fiber` publishes `DONE` first, then keeps using the fiber — it wakes
waiters and finally saves its registers into `h->ctx` while still running on
the fiber's stack. A joiner on another thread may observe `DONE` and free the
handle and unmap the stack in that window: a use-after-free that is rare but
real. Reclamation needs one protocol that covers both cases.

## Goals

- A fiber whose `spawn` expression is used as a statement (its `Future` is
  discarded) is **detached**: its handle and stack are freed as soon as it
  finishes, with no `await`
- Exactly one party frees each handle, and only after the fiber has completely
  switched off its stack — for detached and joined fibers alike
- Runtime API: `rw_sched_detach(h)` (scheduler) and `rw_detach(f)` (the
  `rw_future_t` boundary used by generated code)
- A test-only counter of live handles, `rw_sched_live_fibers()`, and C tests run
  from pytest so CI exercises them

## Non-Goals

- Detecting a `Future` that is stored in a variable but never awaited (that
  still leaks; only the statement form is detached)
- Cancellation, timeouts, or structured concurrency (#24, #27)
- Freeing fibers that are parked forever (e.g. blocked in `read` at shutdown)
- Freeing the strings and buffers a fiber allocated (memory management RFC, #67)
- A user-visible `detach` builtin or syntax

## Design

### Ownership handoff

Each handle gets an atomic `reap` field with three values:

| value | meaning |
|---|---|
| `REAP_NONE` | initial |
| `REAP_FINISHED` | set by the worker after the fiber finished **and** swapped off its stack |
| `REAP_RELEASED` | set by the owner: after `join` read the result, or on `detach` |

Both sides use an atomic exchange, and **whoever arrives second frees**:

```c
/* worker_main, after rw_fiber_swap returns and state == DONE */
if (atomic_exchange(&g->reap, REAP_FINISHED) == REAP_RELEASED) free_handle(g);

/* release(h): end of join (after reading the result) or detach */
if (atomic_exchange(&h->reap, REAP_RELEASED) == REAP_FINISHED) free_handle(h);
```

Exactly one exchange observes the other's value, so a handle is freed exactly
once. The worker only marks `REAP_FINISHED` from its scheduler loop, after the
inbound swap has saved the fiber's context, so the stack and `ctx` are never in
use when they are freed. This also closes the join race described above: a
joiner that wakes on `DONE` no longer frees immediately unless the worker has
already finished with the fiber.

Handles that never ran are still reclaimed by `rw_sched_shutdown` from the run
queues, as before.

### Compiler

`irgen` emits `rw_detach(fut)` right after the `rw_spawn_*` call when the spawn
is the whole expression statement. Sema is unchanged: the statement form already
type-checks.

### Test counter

`rw_sched_live_fibers()` returns spawned-minus-freed handles (an atomic counter
updated in `spawn_common` and `free_handle`). It exists for tests; generated
code never calls it.

## Changes per component

- `runtime/fiber/sched.c`, `sched.h`: `reap` field, `release`, `rw_sched_detach`,
  the worker-side mark, the live counter
- `runtime/runtime.c`, `runtime.h`: `rw_detach`
- `rwc/irgen.py`: detach after a statement-level spawn
- `runtime/fiber/test_detach.c` (new): detached and joined fibers across worker
  counts, live count returns to 0
- `tests/test_runtime_c.py` (new): build and run the C tests from pytest
- `tests/test_irgen.py`: statement spawn emits `rw_detach`; an assigned spawn
  does not

Layers touched: irgen / runtime (2) + tests.

## Verification

```sh
make -C runtime
uv run --frozen pytest tests/test_runtime_c.py tests/test_irgen.py -q
uv run --frozen pytest -q
```

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Double free or a missed free from a racy handoff | A single atomic exchange per side; the C test spawns thousands of detached and joined fibers under several worker counts and checks the live count returns to exactly 0 |
| Freeing a stack that is still executing | The worker marks `REAP_FINISHED` only from `worker_main` after the swap back, never from inside `finalize_fiber` |
| A detached fiber that is still running at shutdown | It is not freed (process exit reclaims it); documented as a Non-Goal |
