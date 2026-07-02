# rw async file I/O (abstraction + thread pool backend)

## Context

rw provides file I/O ([[15-file-io]]) via the fd-generic `read` / `write` /
`close`. The implementation is `rw_read` / `rw_write` in `runtime/io.c`, which
has completion-ish logic that parks the fiber on the netpoller on `EAGAIN`.

However, this logic only works for **non-blocking sockets**. A regular file fd
does not return `EAGAIN` on `read(2)` / `write(2)`; the kernel blocks until the
data is ready and then completes. During that time, the **worker thread M on
which the fiber that called that `read` is running is blocked entirely**. For a
runtime that runs 100k fibers, having a worker stop for a single file read is a
weakness.

This sub-project turns file I/O into an async model that "offloads to another
thread, parks the fiber, and wakes it when complete". The fiber yields the worker
even during a file read, so other fibers can keep running on the same worker.

### Position on the roadmap

The user's ultimate goal is "to be able to use io_uring for file I/O". io_uring
is a **completion model** (submit a read operation to the kernel and receive a
completion notification), which is fundamentally different from the current
readiness-based netpoller. Therefore:

1. **This sub-project (PR 1)**: Define the **abstract interface** for async file
   I/O and implement the first concrete instance with a **thread pool backend**
   (common to all OSes).
2. **Next sub-project (PR 2)**: Add io_uring as a **faster concrete instance on
   Linux** under the abstraction, swapping it in. macOS stays on the thread pool.

By putting the abstraction in place first, we achieve the same "file I/O does not
block the fiber" behavior on both OSes first, and when io_uring is introduced we
only need to plug the concrete instance into the already-completed park/completion
protocol.

## Goals

- Introduce the async file I/O abstraction `runtime/aio.h` / `runtime/aio.c`:
  - `void rw_aio_read(rw_str *out, int64_t fd, int64_t max)`
  - `int64_t rw_aio_write(int64_t fd, rw_str b)`
- Implement the first backend with a **thread pool** (a fixed number of pthreads
  + a task queue). Workers execute `read(2)` / `write(2)` and wake the calling
  fiber after completion.
- In `rw_read` / `rw_write` of `runtime/io.c`, determine the fd kind via
  `fstat(fd)`:
  - **Regular file (`S_ISREG`)** → `rw_aio_read` / `rw_aio_write` (thread pool)
  - **Otherwise (sockets, etc.)** → the traditional netpoller path
    (`EAGAIN`→park, unchanged)
- To park the fiber and wake it on completion, use the existing scheduler's
  `rw_sched_park_current()` / `rw_sched_enqueue_ready()` as-is.
- Manage the thread pool lifecycle in `rw_init` / `rw_shutdown`.
- The caller side (rw code, sema, irgen) is **not changed at all**. `read`/`write`
  transparently choose the optimal path depending on the fd kind.

## Non-Goals

- **Implementing io_uring itself** — done in PR 2. This PR is abstraction +
  thread pool only.
- Changing the path for anything other than file I/O (sockets) — the netpoller is
  unchanged.
- Making file operations other than `read` (seek/stat/truncate, etc.)
  asynchronous.
- io_uring-specific optimizations such as fixed buffers / batch submit /
  zero-copy.
- Auto-tuning the thread pool size / work stealing (a fixed number is enough).
- Async on the main thread (outside a fiber) — since it cannot park, it falls
  back to synchronous `read(2)`.

## Architecture

```
rw_read(fd, max)  ──┐  fstat(fd) decision in io.c
rw_write(fd, b)   ──┤
                    ├── S_ISREG (file) ──→ rw_aio_read / rw_aio_write (aio.c)
                    │                              │ if on a fiber:
                    │                              │   submit task → rw_sched_park_current()
                    │                              │   worker does read(2) → store result
                    │                              │   → rw_sched_enqueue_ready(handle)
                    │                              │ if outside a fiber: synchronous read(2)
                    └── otherwise (socket, etc.) ──→ traditional EAGAIN→rw_net_park_* (unchanged)
```

### Components

**`runtime/aio.h`** — the public interface (prototypes are placed in aio.h rather
than being consolidated into runtime.h; unlike net/tcp.h, aio is an independent
abstraction, so it has its own header):
- `void rw_aio_init(void);` / `void rw_aio_shutdown(void);`
- `void rw_aio_read(rw_str *out, int64_t fd, int64_t max);`
- `int64_t rw_aio_write(int64_t fd, rw_str b);`

**`runtime/aio.c`** — the thread pool backend:
- A fixed number (default 4, overridable via the `RW_AIO_THREADS` environment
  variable) of worker pthreads.
- A locked task queue (`pthread_mutex_t` + `pthread_cond_t`).
- Task struct: operation kind (READ/WRITE), fd, buffer/size, result storage
  slot, and the waiting `rw_fiber_handle *`.
- Flow of `rw_aio_read` / `rw_aio_write` (on a fiber):
  1. Build the task (giving it a slot to write the result back and its own fiber
     handle)
  2. push to the queue and wake a worker via the condvar
  3. `rw_sched_park_current()` to park (becomes WAITING and yields the worker)
  4. The worker executes `read(2)`/`write(2)` → stores the result in the task's
     slot → `rw_sched_enqueue_ready(task->handle)` to return the fiber to ready
  5. The fiber, returning from park, reads the result from the task's slot and
     returns it to the caller
- Outside a fiber (`rw_sched_current_fiber() == NULL`), it cannot park, so it
  executes a synchronous `read(2)`/`write(2)` on the spot and returns (the same
  safety measure as the netpoller's park).

**`runtime/io.c`** (changed):
- At the start of `rw_read` / `rw_write`, `fstat(fd)`, and if `S_ISREG` delegate
  to `rw_aio_*`. Otherwise execute the existing logic (socket-oriented
  EAGAIN+netpoller park) as-is.
- On `fstat` failure, fall back to the existing logic on the safe side.

**`runtime/runtime.c`** (changed):
- `rw_aio_init()` next to `rw_netpoller_init()` in `rw_init`.
- `rw_aio_shutdown()` in `rw_shutdown` (stop and join the workers).

### Data flow and ownership

- Buffer of `rw_aio_read`: as with the existing `rw_read`, transfer ownership of
  the malloc'd buffer to the caller via `out->ptr` (when n>0). The malloc is done
  on the aio.c side, and the worker sets len after reading. EOF/error is len=0 /
  ptr=NULL.
- The task struct is allocated on the calling fiber's stack, and a pointer to it
  is pushed onto the queue. Since the fiber is parked until completion, the stack
  is alive (the fiber's stack is retained even while parked). On completion the
  worker writes to the slot and enqueues the handle.
- Result visibility: the worker's slot write is done before
  `rw_sched_enqueue_ready`, riding on the happens-before of enqueue/park-return
  (the same acquire/release convention as the existing netpoller).

## Concurrency correctness

This protocol is made **completely isomorphic** to the two existing "park and let
another thread wake" paths. The real code has been verified
(`runtime/fiber/sched.c`, `runtime/net/netpoller*.c`):

- **netpoller** (`rw_net_park_read`): register on kqueue/epoll with
  `rw_netpoller_register_read(fd, h)` → `rw_sched_park_current()`. The poll thread
  does `rw_sched_enqueue_ready(h)` on the event.
- **join** (`park_on`): put itself on the wait list inside `wait_lock` and set
  `state=WAITING` → release → `rw_fiber_swap`. The completing side
  `finalize_fiber` sets `state=DONE` (release) → takes the list en masse under
  `wait_lock` → `enqueue_ready` for each waiter.
- **aio (this PR)**: fix the handle → submit the task → `rw_sched_park_current()`.
  After the worker completes `read(2)`/`write(2)`, it stores the result in the
  slot → `rw_sched_enqueue_ready(task->handle)`.

`rw_sched_enqueue_ready` is documented in sched.h as "safe to call from another
thread", and `enqueue_ready` sets `state=READY` and pushes to the ready queue
(`sched.c:206`). `rw_sched_park_current` sets `state=WAITING` and returns to the
scheduler via `rw_fiber_swap` (`sched.c:339`). worker_main returns a fiber that
returned from swap to ready **only when `state==RUNNING`**, and does not return
WAITING/DONE (`sched.c:373`). This guarantees the ironclad rule "a WAITING fiber
must not be in the ready queue".

### On the race in ctx publication timing

A stackful coroutine scheduler has the classic race where "before the
`rw_fiber_swap` (ctx save) inside `rw_sched_park_current` completes, another
thread does `enqueue_ready` → another worker steals → resumes a half-written ctx
→ PC=0 SEGV" ([[stackful-coroutine-scheduling]]).

The aio in this PR does **not newly introduce** this race. Reason: as above, it
uses the **same protocol** as netpoller / join (fix handle → register/submit →
park; the other thread enqueues after park) and does not create its own
publish-before-save pattern. Whether the worker can enqueue before park, and its
safety, is under **exactly the same conditions** as the netpoller's register→park
(if the fd is ready right after registration, the poll thread can enqueue
immediately), and this PR does not raise or lower the safety level of the
existing code.

Therefore this PR's responsibility is "to write the protocol in an order
indistinguishable from the netpoller's". Should this isomorphic race ever
materialize in the future, it is an issue of the scheduler layer common to
netpoller, join, and aio, and is addressed for all three paths together as a
separate task that builds the equivalent of `park.c`'s `wait_lock` into
park_current (out of scope for this PR).

### Flush out races in verification

Following the verification procedure of [[stackful-coroutine-scheduling]], run an
e2e where multiple fibers do file I/O concurrently, in **both `RW_WORKERS=1` and
`RW_WORKERS≥2` (where stealing occurs)**, and confirm that the results come out
correctly without SEGV/hang. This is evidence that the aio path cooperates
correctly with the existing scheduler.

## Layers touched

| Layer | File | Change |
|---|---|---|
| Runtime (new) | `runtime/aio.h` / `runtime/aio.c` | Abstraction + thread pool backend |
| Runtime | `runtime/io.c` | Add an `fstat` decision to `rw_read`/`rw_write`, and delegate files to aio |
| Runtime | `runtime/runtime.c` | aio init/shutdown in `rw_init`/`rw_shutdown` |
| Runtime | `runtime/Makefile` | Add `aio.o` to OBJS and the build rules |
| Compiler | `rwc/` | **Unchanged** (the `read`/`write` built-ins are unchanged; only the internal path changes) |
| Examples | Regression-check with the existing `examples/file_io.rw` | A new example is optional |
| Tests | `tests/` | The existing e2e (file_io / tcp) stays green. Add 1 e2e for concurrent file read |

The characteristic of this PR is that it does not touch `rwc/` at all. The
language specification does not change; only the runtime's file I/O implementation
changes from synchronous blocking to asynchronous offload.

## Verification

```sh
make -C runtime
uv run pytest -q                       # existing 169 + new all green
uv run rwc run examples/file_io.rw     # round-trip matches as before
```

- Regression: `examples/file_io.rw` (round-trip) and `tests/test_e2e_tcp.py` (the
  socket path works unchanged) are green.
- Confirming asynchrony (new e2e): run an rw example where multiple fibers each
  read/write a file concurrently via `spawn`, and confirm that the results of all
  fibers come out correctly. This is evidence that park/resume via the thread pool
  works correctly. (To avoid being timing-dependent, make the output
  deterministic content.)
- Main thread path: an existing example that calls `read`/`write` from outside a
  fiber (file_io directly under main) works via the synchronous fallback
  (file_io.rw qualifies).

## Risks and mitigations

- **park/enqueue race (ctx publication timing)**: strictly follow the **same
  protocol** as netpoller / join (fix handle → submit → `park_current`; the worker
  does `enqueue_ready` after storing the result), and do not create your own
  publish-before-save. This PR does not change the existing safety level (see
  "Concurrency correctness" above). During implementation, always confirm there is
  no SEGV/hang under `RW_WORKERS≥2` + steal load.
- **Task survival on the fiber stack**: since the fiber stack is retained even
  while parked, it is safe to place the task on the stack and pass a pointer. The
  worker only performs the completion write and does not free the task (the calling
  fiber owns it).
- **Cost of fstat**: one `fstat` is called each time to decide file/socket. This is
  1 extra syscall per read, but negligible as a replacement for a blocking read.
  The same decision can be reused when moving to io_uring in PR2.
- **Blocking the main thread**: outside a fiber it falls back to synchronous read,
  so the main thread can block as before (Non-Goal). However, as with the
  netpoller, the worker M and the netpoller/aio threads are separate, so concurrent
  tasks make progress.
- **The "while we're at it" temptation**: do not touch io_uring, fixed buffers,
  seek, etc. (Non-Goals). This PR is confined to the minimal implementation of
  abstraction + thread pool.
