# rw scheduler: make a parked fiber wakeable only after its context is saved

## Context

A fiber parks in three places: on the netpoller (socket not ready), on the
aio pool (regular-file I/O), and on another fiber's wait list (`await`). Each
used to make itself wakeable *first* and swap out *second*:

```c
rw_netpoller_register_read(fd, me);   /* netpoller may wake `me` from now on */
rw_sched_park_current();              /* ... while this swap saves me->ctx  */
```

If the event fires in between — an fd that is already readable, an aio read
that finishes in microseconds, a child that finishes immediately — the waker
re-enqueues the fiber while its registers are still being saved. Another
worker can then resume a half-saved context (crash), or resume it fully and
set it RUNNING, after which the original worker sees RUNNING and enqueues it
a second time (the fiber runs twice). This is the "publish before save"
mistake the scheduler already avoids for `yield`.

`runtime/fiber/test_park_race.c` reproduces it: on the code before this
change, 7 of 20 runs aborted, crashed, or hung.

## Goals

- A parked fiber becomes visible to wakers only after its context is saved,
  for all three kinds of parking
- One primitive, `rw_sched_park_commit(commit, arg)`, replacing
  `rw_sched_park_current()`

## Non-Goals

- Changing what can park, or the netpoller / aio backends themselves

## Design

Like Go's `gopark(unlockf, ...)`, parking is split in two:

1. On the fiber's stack, `rw_sched_park_commit` marks the fiber WAITING,
   stores `commit` / `arg` in the worker, and swaps to the scheduler.
2. On the scheduler's stack, after the swap has returned (so the context is
   saved), `worker_main` calls `commit(fiber, arg)` and does not touch the
   fiber again.

The commits:

| Parking | Commit |
|---|---|
| netpoller read / write | register the fd with the fiber; if that fails, enqueue the fiber at once |
| aio read / write | push the task (on the parked fiber's stack) to the pool queue |
| join (`await` from a fiber) | take the target's wait lock; if the target is already DONE, enqueue the fiber, otherwise push it on the wait list |

## Verification

```sh
make -C runtime
uv run --frozen pytest tests/test_runtime_c.py -q    # includes test_park_race
uv run --frozen pytest -q
```
