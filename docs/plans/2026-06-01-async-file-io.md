# Async File I/O (abstraction + thread pool) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make file `read`/`write` non-blocking for fibers by offloading regular-file I/O to a thread pool, parking the calling fiber and waking it on completion.

**Architecture:** New `runtime/aio.c`/`aio.h` provides `rw_aio_read`/`rw_aio_write` backed by a fixed thread pool. `io.c`'s `rw_read`/`rw_write` use `fstat` to route regular files (`S_ISREG`) to the aio backend and keep sockets on the existing netpoller path. The park/wake protocol reuses the scheduler's `rw_sched_park_current()` / `rw_sched_enqueue_ready()`, identical to the netpoller and join paths. `rwc/` is untouched. io_uring is a later PR behind the same abstraction.

**Tech Stack:** C11 (POSIX pthreads, `read(2)`/`write(2)`/`fstat(2)`), the rw fiber scheduler, pytest/uv for e2e.

**Spec:** `docs/specs/16-async-file-io.md`

---

## File Structure

- `runtime/aio.h` (new) — public interface: `rw_aio_init`, `rw_aio_shutdown`, `rw_aio_read`, `rw_aio_write`.
- `runtime/aio.c` (new) — thread pool backend (fixed workers, locked task queue, condvar).
- `runtime/io.c` (modify) — `rw_read`/`rw_write` gain an `fstat` check that routes `S_ISREG` fds to `rw_aio_*`.
- `runtime/runtime.c` (modify) — `rw_init` calls `rw_aio_init`; `rw_shutdown` calls `rw_aio_shutdown`.
- `runtime/Makefile` (modify) — build and link `aio.o`.
- `examples/file_par.rw` (+ `.expected`) (new) — concurrent file I/O across fibers (e2e).
- `tests/test_e2e.py` (modify) — add `file_par` to the parametrize list.

Scheduler primitives used (verified in `runtime/fiber/sched.c` / `sched.h`):
- `rw_fiber_handle *rw_sched_current_fiber(void)` — NULL when not on a fiber.
- `void rw_sched_park_current(void)` — set current fiber WAITING and swap to scheduler; caller arranges wake via enqueue.
- `void rw_sched_enqueue_ready(rw_fiber_handle *h)` — safe from any thread; sets READY and pushes to a ready queue.

---

## Task 1: aio thread pool backend (`aio.c` / `aio.h`)

**Files:**
- Create: `runtime/aio.h`
- Create: `runtime/aio.c`
- Modify: `runtime/runtime.c`
- Modify: `runtime/Makefile`

- [ ] **Step 1: Add `aio.h`**

Create `runtime/aio.h`:

```c
#ifndef RW_AIO_H
#define RW_AIO_H

#include <stdint.h>

#include "runtime.h"   /* rw_str */

#ifdef __cplusplus
extern "C" {
#endif

/* Async file I/O backend. Lifecycle driven by rw_init / rw_shutdown. */
void rw_aio_init(void);
void rw_aio_shutdown(void);

/* Offload a blocking read(2)/write(2) on `fd` to a worker thread, parking
 * the calling fiber until it completes. Off-fiber (main thread) callers
 * fall back to synchronous I/O. Semantics mirror rw_read/rw_write in
 * io.c: read fills *out (len=0 on EOF/error, ptr owns a malloc'd buffer
 * when len>0); write returns bytes written, negative on error. */
void    rw_aio_read (rw_str *out, int64_t fd, int64_t max);
int64_t rw_aio_write(int64_t fd, rw_str b);

#ifdef __cplusplus
}
#endif

#endif /* RW_AIO_H */
```

- [ ] **Step 2: Add `aio.c` — the thread pool**

Create `runtime/aio.c`. Submit→park / worker→enqueue order is identical to
the netpoller (`rw_net_park_read` → `rw_sched_park_current`; poll thread →
`rw_sched_enqueue_ready`).

```c
/*
 * Async file I/O via a fixed thread pool. See docs/specs/16-async-file-io.md.
 *
 * rw_read/rw_write (io.c) route regular files here. A worker thread runs
 * the blocking read(2)/write(2) while the calling fiber is parked, so the
 * worker M is free to run other fibers. Protocol mirrors the netpoller:
 * the caller fills a task + its own fiber handle, then parks; the pool
 * worker stores the result and calls rw_sched_enqueue_ready(handle).
 *
 * io_uring (Linux) will later replace this backend behind rw_aio_*.
 */

#include "aio.h"
#include "fiber/sched.h"
#include "runtime.h"

#include <pthread.h>
#include <stdint.h>
#include <stdlib.h>
#include <unistd.h>

#define RW_AIO_DEFAULT_THREADS 4
#define RW_AIO_MAX_THREADS 64

typedef enum { RW_AIO_READ, RW_AIO_WRITE } rw_aio_op;

typedef struct rw_aio_task {
    rw_aio_op op;
    int64_t   fd;
    rw_str   *out;           /* read: worker fills this */
    int64_t   max;           /* read */
    rw_str    in;            /* write: caller-provided buffer */
    int64_t   wret;          /* write result */
    rw_fiber_handle *waiter; /* fiber to wake on completion */
    struct rw_aio_task *next;
} rw_aio_task;

static pthread_t        g_threads[RW_AIO_MAX_THREADS];
static int              g_nthreads = 0;
static pthread_mutex_t  g_mu  = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t   g_cv  = PTHREAD_COND_INITIALIZER;
static rw_aio_task     *g_head = NULL;
static rw_aio_task     *g_tail = NULL;
static int              g_shutdown = 0;

static void queue_push(rw_aio_task *t) {
    t->next = NULL;
    pthread_mutex_lock(&g_mu);
    if (g_tail) g_tail->next = t; else g_head = t;
    g_tail = t;
    pthread_cond_signal(&g_cv);
    pthread_mutex_unlock(&g_mu);
}

static rw_aio_task *queue_pop(void) {
    pthread_mutex_lock(&g_mu);
    while (g_head == NULL && !g_shutdown) {
        pthread_cond_wait(&g_cv, &g_mu);
    }
    if (g_shutdown && g_head == NULL) {
        pthread_mutex_unlock(&g_mu);
        return NULL;
    }
    rw_aio_task *t = g_head;
    g_head = t->next;
    if (g_head == NULL) g_tail = NULL;
    pthread_mutex_unlock(&g_mu);
    return t;
}

static void run_task(rw_aio_task *t) {
    if (t->op == RW_AIO_READ) {
        char *buf = (char *)malloc((size_t)t->max);
        if (!buf) { t->out->len = 0; t->out->ptr = NULL; return; }
        ssize_t n = read((int)t->fd, buf, (size_t)t->max);
        if (n > 0) { t->out->len = n; t->out->ptr = buf; }
        else       { free(buf); t->out->len = 0; t->out->ptr = NULL; }
    } else { /* RW_AIO_WRITE */
        ssize_t n = write((int)t->fd, t->in.ptr, (size_t)t->in.len);
        t->wret = (int64_t)n;
    }
}

static void *worker_main(void *arg) {
    (void)arg;
    for (;;) {
        rw_aio_task *t = queue_pop();
        if (t == NULL) return NULL;   /* shutdown */
        run_task(t);
        /* Result store above happens-before this enqueue; the woken
         * fiber reads the result after resuming. Same edge as netpoller. */
        rw_sched_enqueue_ready(t->waiter);
    }
}

void rw_aio_init(void) {
    g_shutdown = 0;
    int n = RW_AIO_DEFAULT_THREADS;
    const char *env = getenv("RW_AIO_THREADS");
    if (env) {
        int v = atoi(env);
        if (v >= 1 && v <= RW_AIO_MAX_THREADS) n = v;
    }
    g_nthreads = 0;
    for (int i = 0; i < n; i++) {
        if (pthread_create(&g_threads[i], NULL, worker_main, NULL) == 0) {
            g_nthreads++;
        }
    }
}

void rw_aio_shutdown(void) {
    pthread_mutex_lock(&g_mu);
    g_shutdown = 1;
    pthread_cond_broadcast(&g_cv);
    pthread_mutex_unlock(&g_mu);
    for (int i = 0; i < g_nthreads; i++) {
        pthread_join(g_threads[i], NULL);
    }
    g_nthreads = 0;
}

void rw_aio_read(rw_str *out, int64_t fd, int64_t max) {
    if (max <= 0) { out->len = 0; out->ptr = NULL; return; }
    rw_fiber_handle *me = rw_sched_current_fiber();
    if (!me) {
        /* Not on a fiber: cannot park, run synchronously. */
        char *buf = (char *)malloc((size_t)max);
        if (!buf) { out->len = 0; out->ptr = NULL; return; }
        ssize_t n = read((int)fd, buf, (size_t)max);
        if (n > 0) { out->len = n; out->ptr = buf; }
        else       { free(buf); out->len = 0; out->ptr = NULL; }
        return;
    }
    /* Task on the fiber's stack: the fiber is parked (alive) until the
     * worker wakes it, so the pointer stays valid. */
    rw_aio_task t;
    t.op = RW_AIO_READ; t.fd = fd; t.out = out; t.max = max;
    t.waiter = me; t.next = NULL;
    queue_push(&t);                 /* submit */
    rw_sched_park_current();        /* worker enqueues us when done */
    /* resumed: out filled by the worker */
}

int64_t rw_aio_write(int64_t fd, rw_str b) {
    if (b.len <= 0) return 0;
    rw_fiber_handle *me = rw_sched_current_fiber();
    if (!me) {
        ssize_t n = write((int)fd, b.ptr, (size_t)b.len);
        return (int64_t)n;
    }
    rw_aio_task t;
    t.op = RW_AIO_WRITE; t.fd = fd; t.in = b; t.wret = 0;
    t.waiter = me; t.next = NULL;
    queue_push(&t);
    rw_sched_park_current();
    return t.wret;
}
```

- [ ] **Step 3: Hook init/shutdown into `runtime.c`**

In `runtime/runtime.c`, add near the other runtime includes at the top of
the file:

```c
#include "aio.h"
```

Update lifecycle (currently lines ~140-148):

```c
void rw_init(void) {
    rw_sched_init();
    rw_netpoller_init();
    rw_aio_init();
}

void rw_shutdown(void) {
    rw_aio_shutdown();
    rw_netpoller_shutdown();
    rw_sched_shutdown();
}
```

- [ ] **Step 4: Add `aio.o` to the Makefile**

In `runtime/Makefile`, add `aio.o` to `OBJS` next to `io.o`:

```make
OBJS := runtime.o io.o aio.o \
        fiber/fiber.o fiber/sched.o fiber/runq.o fiber/park.o \
        net/netpoller.o net/tcp.o $(NET_PLATFORM_O) \
        $(FIBER_ASM)
```

Add a build rule near the `io.o` rule (match the existing `$(CC) $(CFLAGS)` form):

```make
aio.o: aio.c aio.h runtime.h fiber/sched.h
	$(CC) $(CFLAGS) -c aio.c -o aio.o
```

- [ ] **Step 5: Build the runtime**

Run: `make -C runtime`
Expected: compiles cleanly (no warnings under `-Wall -Wextra`), links
`librw.a` including `aio.o`. Nothing calls `rw_aio_*` from rw programs yet
(io.c routing is Task 2); the lib links fine.

- [ ] **Step 6: Commit**

```bash
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
git add runtime/aio.c runtime/aio.h runtime/runtime.c runtime/Makefile
git commit -m "runtime: add async file I/O thread pool (rw_aio_read/rw_aio_write)

Fixed worker pool with a locked task queue. Workers run blocking
read(2)/write(2) while the calling fiber is parked via
rw_sched_park_current; on completion the worker calls
rw_sched_enqueue_ready, identical to the netpoller protocol. Off-fiber
callers fall back to synchronous I/O. Wired into rw_init/rw_shutdown.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Route regular files through aio in `io.c`

**Files:**
- Modify: `runtime/io.c` (`rw_read` and `rw_write`)

- [ ] **Step 1: Add the include and `fstat` routing to `rw_read`**

In `runtime/io.c`, add to the includes block (after `#include <unistd.h>`):

```c
#include <sys/stat.h>

#include "aio.h"
```

Replace the body of `rw_read` (lines ~22-39):

```c
void rw_read(rw_str *out, int64_t fd, int64_t max) {
    if (max <= 0) { out->len = 0; out->ptr = NULL; return; }

    /* Regular files block on read(2) (no EAGAIN), pinning the worker M.
     * Offload them to the aio thread pool so the fiber parks instead.
     * Sockets keep the nonblocking + netpoller path below. */
    struct stat st;
    if (fstat((int)fd, &st) == 0 && S_ISREG(st.st_mode)) {
        rw_aio_read(out, fd, max);
        return;
    }

    char *buf = (char *)malloc((size_t)max);
    if (!buf)     { out->len = 0; out->ptr = NULL; return; }
    for (;;) {
        ssize_t n = read((int)fd, buf, (size_t)max);
        if (n > 0)  { out->len = n; out->ptr = buf; return; }
        if (n == 0) { free(buf); out->len = 0; out->ptr = NULL; return; }
        if (errno == EAGAIN || errno == EWOULDBLOCK) {
            if (rw_sched_current_fiber()) {
                rw_net_park_read((int)fd);
                continue;
            }
            free(buf); out->len = 0; out->ptr = NULL; return;
        }
        free(buf); out->len = 0; out->ptr = NULL; return;
    }
}
```

- [ ] **Step 2: Add `fstat` routing to `rw_write`**

Replace the body of `rw_write` (lines ~41-55):

```c
int64_t rw_write(int64_t fd, rw_str b) {
    if (b.len <= 0) return 0;

    struct stat st;
    if (fstat((int)fd, &st) == 0 && S_ISREG(st.st_mode)) {
        return rw_aio_write(fd, b);
    }

    for (;;) {
        ssize_t n = write((int)fd, b.ptr, (size_t)b.len);
        if (n >= 0) return (int64_t)n;
        if (errno == EAGAIN || errno == EWOULDBLOCK) {
            if (rw_sched_current_fiber()) {
                rw_net_park_write((int)fd);
                continue;
            }
            return -1;
        }
        return -1;
    }
}
```

- [ ] **Step 3: Build and run the existing file_io round-trip**

Run:
```bash
make -C runtime
uv run rwc run examples/file_io.rw
```
Expected (unchanged from spec #15; `main` is off-fiber so it uses the sync
fallback, output identical):

```
hello file
second line

23
```

- [ ] **Step 4: Run the full suite (regression)**

Run: `uv run pytest -q`
Expected: all green (169 existing). file_io e2e and tcp e2e unaffected —
sockets still take the netpoller path (not `S_ISREG`), files take aio.

- [ ] **Step 5: Commit**

```bash
git add runtime/io.c
git commit -m "io: route regular-file read/write through the aio thread pool

fstat(fd) selects S_ISREG fds for rw_aio_read/rw_aio_write (non-blocking
for the fiber); sockets keep the existing EAGAIN + netpoller path.
The read/write builtins are unchanged.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Concurrent file I/O e2e (verifies fibers don't block each other)

**Files:**
- Create: `examples/file_par.rw`
- Create: `examples/file_par.rw.expected`
- Modify: `tests/test_e2e.py` (parametrize list)

- [ ] **Step 1: Write a concurrent file I/O sample**

Create `examples/file_par.rw`. Three fibers each write then read their own
file, returning the byte count. Each fiber uses a distinct path so the
result is deterministic regardless of interleaving:

```python
def roundtrip(path: string) -> int:
    wfd: int = file_open(path, "w")
    write(wfd, bytes_from_str("payload\n"))
    close(wfd)
    rfd: int = file_open(path, "r")
    b: Bytes = read(rfd, 4096)
    close(rfd)
    return len(b)

def main() -> int:
    f1: Future[int] = spawn roundtrip("/tmp/rw_file_par_1.txt")
    f2: Future[int] = spawn roundtrip("/tmp/rw_file_par_2.txt")
    f3: Future[int] = spawn roundtrip("/tmp/rw_file_par_3.txt")
    r1: int = await f1
    r2: int = await f2
    r3: int = await f3
    # each reads back "payload\n" = 8 bytes; total 24
    print(r1 + r2 + r3)
    return 0
```

- [ ] **Step 2: Build and run to confirm behavior**

Run:
```bash
make -C runtime
uv run rwc run examples/file_par.rw
```
Expected: `24` (three fibers × 8 bytes). Confirm the actual output. If
`spawn` on a builtin-using function or distinct-path approach surfaces an
issue, debug it (do not paper over) — but the expectation is a clean `24`.

- [ ] **Step 3: Generate the `.expected` from the real run**

Run:
```bash
uv run rwc run examples/file_par.rw > examples/file_par.rw.expected
cat examples/file_par.rw.expected
```
(Generated from the binary — not hand-written.)

- [ ] **Step 4: Add `file_par` to the e2e parametrize list**

In `tests/test_e2e.py`, the parametrize list currently ends with
`"for_count", "ternary", "file_io"`. Append `"file_par"`:

```python
    ["hello", "arith", "fib", "while_count", "spawn_basic", "spawn_many", "spawn_string", "string_ops", "bytes_basic", "list_basic", "option_basic", "result_basic", "for_count", "ternary", "file_io", "file_par"],
```

- [ ] **Step 5: Run the e2e under one and multiple workers**

The stackful-coroutine-scheduling skill requires verifying both. Run:
```bash
uv run pytest tests/test_e2e.py -k file_par -q
RW_WORKERS=1 uv run pytest tests/test_e2e.py -k file_par -q
RW_WORKERS=4 uv run pytest tests/test_e2e.py -k file_par -q
```
Expected: PASS in all three (no SEGV / hang; output matches `.expected`).
A crash under RW_WORKERS=4 but not =1 indicates a ctx-publish race —
escalate, do not work around it.

- [ ] **Step 6: Commit**

```bash
git add examples/file_par.rw examples/file_par.rw.expected tests/test_e2e.py
git commit -m "examples: concurrent file I/O across fibers (aio park/wake e2e)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Full verification

- [ ] **Step 1: Clean rebuild + full suite, single and multi worker**

Run:
```bash
make -C runtime clean && make -C runtime
uv run pytest -q
RW_WORKERS=1 uv run pytest -q
RW_WORKERS=4 uv run pytest -q
```
Expected: ALL green under every worker count (existing 169 + file_par). No
warnings in the C build.

- [ ] **Step 2: Confirm sockets still use the netpoller (not aio)**

Run: `uv run pytest tests/test_e2e_tcp.py -q`
Expected: PASS (echo single + 10-concurrent). Sockets are not `S_ISREG`, so
`fstat` routing leaves them on the netpoller path.

- [ ] **Step 3: Manual smoke of the file round-trip**

Run: `uv run rwc run examples/file_io.rw`
Confirm output matches spec #15 (`hello file` / `second line` / blank / `23`).

---

## Notes for the implementer

- **TDD shape for C:** no C unit-test harness in this repo; the
  failing/passing checks are the build (Task 1) and the e2e runs (Task 3).
  Write the e2e expectation from the real binary, never by hand.
- **Concurrency protocol is load-bearing:** the submit→`park_current` /
  worker→`enqueue_ready` order must match the netpoller exactly. Do not add
  a publish-before-save shortcut. See `docs/specs/16-async-file-io.md` →
  "並行性の正しさ" and the stackful-coroutine-scheduling skill.
- **Task struct on the stack:** `rw_aio_task t;` lives on the calling
  fiber's stack; a pointer is queued. The fiber is parked (alive, stack
  retained) until the worker wakes it, so the pointer stays valid. Do not
  heap-allocate it (matches the join `park_on` style; no free to manage).
- **No rwc changes:** the `read`/`write` builtins are unchanged; only the
  runtime behind them changes.
- **io_uring is the next PR**, slotting a Linux backend behind `rw_aio_*`.
