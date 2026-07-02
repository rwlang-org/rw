# rw fiber runtime: M:N scheduler

This document describes the implementation that extends the single-threaded
cooperative scheduler of `05-fibers.md` to **M:N (multiplexing many fibers
across multiple OS threads)**. The public ABI (`rw_spawn_*` / `rw_await_*` /
`rw_init` / `rw_shutdown` / `rw_str`) is fully compatible, and there are no
changes to `rwc/irgen.py`.

## Terminology (same as Go's GMP)

| Symbol | Meaning |
|---|---|
| **G** | One fiber. Represented by `rw_fiber_handle` |
| **M** | One OS thread (pthread). `rw_M` |
| **P** | A logical processor. Holds a 256-slot bounded ring. `rw_P` |
| **globq** | The global queue. A mutex-protected linked list. `rw_globq` |

M and P are paired 1:1. Number of M = number of P = number of workers, which
defaults to `sysconf(_SC_NPROCESSORS_ONLN)`, capped at 64. It can be overridden
with the `RW_WORKERS` environment variable.

The main thread is an **orchestrator, not a worker**. `rw_init` spawns the
worker group, and main just calls `rw_user_main` synchronously.

## G state machine

```
                       spawn
                         |
                         v
                      READY -----> RUNNING
                       ^               |
              unpark   |               | yield / park
                       |               v
                    WAITING <------ (park on wait list)
                                       |
                                       v
                                     DONE  (release-store; result published)
```

- `READY`: sitting in one of the queues (a P-local ring or the globq)
- `RUNNING`: currently executing on some M
- `WAITING`: parked on another G's `wait_head` list. Not in any queue
- `DONE`: completed. The result has been published by the trampoline

`state` is `_Atomic int`. Only `RUNNING -> DONE` is a **release/acquire**
synchronization edge. The trampoline does a release-store after writing the
result, and the join side does an acquire-load before reading the result.

## Execution flow

### spawn

```
rw_sched_spawn_*(fn, arg)
  └─ spawn_common
      ├─ calloc the handle + mmap the stack + rw_fiber_ctx_init
      ├─ initialize join_mu / join_cv / wait_lock
      └─ enqueue_ready(h)
            ├─ if tls_m exists, push onto m->p's ring
            │   + if another M is parked, cond_signal to wake one
            └─ if tls_m is NULL (from main), push onto globq + cond_signal
```

### worker main loop

```
worker_main(m):
  tls_m = m
  loop:
    g = find_runnable(m)
    if g == NULL: break   (shutdown)
    g->state = RUNNING
    m->current = g
    rw_fiber_swap(&m->sched_ctx, &g->ctx)
    ──── on return, g is in one of yield/park/done ────
    if g->state == RUNNING:
        enqueue_ready(g)     # it was a yield → put it back
    m->current = NULL
```

"Pushing onto the ring happens **after** the swap" is important (see below).

### find_runnable

```
find_runnable(m):
  loop:
    if g_shutdown: return NULL
    g = rw_runq_get(m->p)          # from your own P
    if g: return g
    g = refill up to CAP/2 from globq and take one
    if g: return g
    g = try_steal(m)               # steal half from another P
    if g: return g
    park on g_sched_cv (with shutdown re-check under lock)
```

### work-stealing (`try_steal`)

```
try_steal(m):
  offset = xorshift64(m) % nworkers   # a separate PRNG per M
  for i in 0..nworkers-1:
    idx = (offset + i) % nworkers
    if idx == m->id: continue
    n = rw_runq_grab(g_ps[idx], batch, CAP/2)
    if n > 0:
        return batch[0], and push batch[1..n-1] onto your own P
  return NULL
```

`rw_runq_grab` advances the victim's `head` with a single CAS and takes out
`ceil(n/2)` items.

### await within a fiber (`park_on`)

```
wait_done(target):
  if tls_m:                       # a fiber awaits another fiber
    while target->state != DONE:
      park_on(target):
        acquire wait_lock
        if target->state == DONE: release and return
        me->state = WAITING
        push me onto target->wait_head
        release wait_lock
        rw_fiber_swap(&me->ctx, &m->sched_ctx)
        ──── returns when the trampoline wakes it ────
  else:                           # main awaits a fiber
    pthread_mutex_lock(&target->join_mu)
    while target->state != DONE:
      pthread_cond_wait(&target->join_cv, ...)
    pthread_mutex_unlock(...)
```

### trampoline completion (`finalize_fiber`)

```
finalize_fiber(h):
  atomic_store(h->state, DONE, release)   # publish the result
  pthread_mutex_lock(h->join_mu)
  pthread_cond_broadcast(h->join_cv)      # wake the main-side joiner
  pthread_mutex_unlock(h->join_mu)
  acquire wait_lock
    waiters = h->wait_head
    h->wait_head = NULL
  release wait_lock
  for w in waiters:
    enqueue_ready(w)                      # move WAITING -> READY
  rw_fiber_swap(&h->ctx, &m->sched_ctx)
```

## Synchronization points

| Object | writer | reader | ordering |
|---|---|---|---|
| `h->state` -> DONE | trampoline | joiner | release / acquire |
| `h->result.*` | trampoline (before DONE) | joiner (after confirming DONE) | rides on the release/acquire of state |
| `p->ring` / `head` / `tail` | owner (put/get), stealer (grab) | same | Go-style: tail is a release-store, head is a CAS |
| `g_shutdown` | shutdown | worker loop | release / acquire |
| `wait_head` | parker (push with CAS), trampoline (atomic take-out) | same | protected by `wait_lock` (atomic_flag spinlock) |

## Pitfalls and remedies

### 1. "ctx publication timing" race on yield

At first, `rw_sched_yield` did "push yourself onto the ring, then swap." This
was a **serious bug**: the moment you land on the ring, another M grabs you via
`rw_runq_grab` and tries to swap you in. But `rw_fiber_swap` is in the middle of
saving your ctx, so the stealer **reads a half-written ctx and jumps to PC=0**.

Fix: yield does nothing to the ring and just swaps. The "put it back on the
ring" decision is made in `worker_main`, **after** the swap has fully returned.
This ensures the ctx save by swap is complete before it is handed to a stealer.

### 2. cond_wait race during park

If the trampoline writes DONE and posts a broadcast between the joiner's
"read state → cond_wait," the joiner misses the signal and sleeps forever.
Remedy: re-check state while holding `join_mu`, and do not cond_wait if it is
DONE. The trampoline side also acquires `join_mu` to broadcast, so if the joiner
"had DONE published before acquiring mu" it exits at that point, and if
"DONE is published after acquiring mu" it does not miss the broadcast.

### 3. wait-list park race

If the trampoline finishes publishing DONE + taking items out of wait_head
between the joiner's "check state → push onto wait_head," the joiner sleeps with
no one to wake it. Remedy: re-check state inside `wait_lock`.

## RW_WORKERS

| value | behavior |
|---|---|
| not set | `sysconf(_SC_NPROCESSORS_ONLN)`, capped at 64 |
| `1` | deterministic execution. `tests/test_e2e.py` pins this |
| `2`–`64` | as specified |
| out of range / non-numeric | falls back to the default |

## Observed parallel speedup

Measured on macOS arm64 with `runtime/fiber/test_steal.c` (200 CPU-bound fibers
spawned from a single primer fiber, i.e. initially all piled onto one P):

| RW_WORKERS | elapsed | vs 1 thread |
|---|---|---|
| 1 | 505ms | 1.00x |
| 2 | 259ms | 1.95x |
| 4 | 139ms | 3.62x |
| 8 |  82ms | 6.15x |

Without work-stealing this should stay pinned at the N=1 speed, which shows
that it "scales even with uneven placement."

## File layout

| file | role |
|---|---|
| `runtime/fiber/sched.{c,h}` | the M:N scheduler proper |
| `runtime/fiber/runq.{c,h}` | 256-slot bounded ring + globq |
| `runtime/fiber/park.{c,h}` | atomic_flag spinlock for the wait list |
| `runtime/fiber/fiber.{c,h}` | `rw_fiber_ctx_init` (unchanged) |
| `runtime/fiber/fiber_{arm64,x86_64}.S` | `rw_fiber_swap` (unchanged) |

## Tests

| binary | what it verifies |
|---|---|
| `test_sched` | spawn/join of 1000 fibers, correctness of the total |
| `test_c10k` | 100,000 fibers, elapsed-time benchmark |
| `test_pingpong` | unit behavior of `rw_fiber_swap` |
| `test_runq` | bounded ring unit: FIFO / overflow / grab / globq |
| `test_wait` | fiber→fiber await, nested await, fan-out |
| `test_steal` | confirms parallel speedup from unbalanced placement |
| `test_shutdown` | 35 cycles of init/shutdown, no leaks or deadlocks |
| `tests/test_e2e.py` | compiler + runtime integration (RW_WORKERS=1 pinned) |
