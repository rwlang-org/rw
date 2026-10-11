# netpoller + TCP API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a minimal TCP API (`tcp_listen` / `tcp_accept` / `tcp_read` / `tcp_write` / `tcp_close`) to the rw language, along with the netpoller (kqueue/epoll) that backs it, so that `examples/tcp_echo.rw` runs.

**Architecture:** Run a single dedicated netpoller pthread that watches fd readiness in ONESHOT mode via `kevent` / `epoll_wait`. When a fiber gets EAGAIN from `tcp_read` and friends, it registers with the netpoller through `rw_net_park_read(fd)` and enters WAITING; when the netpoller thread detects readiness it wakes the fiber via `enqueue_ready(fiber)`. The main thread is not a fiber, so calling `tcp_accept` there does a blocking accept and the main thread sleeps in the kernel; the worker M's and the netpoller run on separate threads, so work proceeds concurrently.

**Tech Stack:** C11 (runtime; kqueue on macOS / epoll on Linux), Python 3.12 + llvmlite (compiler), pytest (tests).

**Spec:** `docs/specs/12-netpoller-tcp.md`

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `runtime/net/netpoller.h` | Shared netpoller API | New |
| `runtime/net/netpoller.c` | init / shutdown / park / shared logic | New |
| `runtime/net/netpoller_kqueue.c` | macOS-specific (kevent) | New |
| `runtime/net/netpoller_epoll.c` | Linux-specific (epoll) | New |
| `runtime/net/tcp.h` | TCP helper declarations | New |
| `runtime/net/tcp.c` | TCP helper implementation | New |
| `runtime/runtime.h` | The five tcp_* + two park prototypes | Add |
| `runtime/runtime.c` | Call the netpoller from `rw_init` / `rw_shutdown` | Modify |
| `runtime/Makefile` | net/*.o + uname branching | Modify |
| `runtime/fiber/sched.h` | Export sched API for the netpoller | Modify |
| `runtime/fiber/sched.c` | Export `rw_sched_enqueue_ready` / `rw_sched_current_fiber` | Modify |
| `runtime/fiber/test_netpoller_pipe.c` | C unit test (pipe) | New |
| `runtime/fiber/test_tcp_loopback.c` | C unit test (localhost) | New |
| `.gitignore` | Ignore test binaries | Add |
| `rwc/sema.py` | Five builtins + spawn prohibition | Modify |
| `rwc/irgen.py` | Emit the five builtins | Modify |
| `tests/test_sema.py` | 5 positive + 5 negative | Add |
| `tests/test_e2e_tcp.py` | Verify echo over a socket from Python | New |
| `examples/tcp_echo.rw` | Echo server demo | New |

---

## Task 1: Export the sched API for the netpoller

So the netpoller thread can wake fibers, expose sched.c's `enqueue_ready` and `tls_m->current` externally.

**Files:**
- Modify: `runtime/fiber/sched.h`
- Modify: `runtime/fiber/sched.c`

- [ ] **Step 1.1: Add the new API to `runtime/fiber/sched.h`**

Add it **immediately before** the trailing `#ifdef __cplusplus ... #endif` at the end of the file:

```c
/* ---- Exported for the netpoller (runtime/net/netpoller.c) ---- */

/* Move the given fiber handle to the ready queue. Safe to call from
 * any thread (uses the same mutex/cv as spawn). */
void rw_sched_enqueue_ready(rw_fiber_handle *h);

/* Return the fiber handle currently running on this worker thread,
 * or NULL if the calling thread is not a worker (main / netpoller). */
rw_fiber_handle *rw_sched_current_fiber(void);
```

- [ ] **Step 1.2: Add the export functions to `runtime/fiber/sched.c`**

Add the export functions right below `enqueue_ready` (near sched.c:206, static):

```c
/* Public wrapper for rw_sched_enqueue_ready (see sched.h). */
void rw_sched_enqueue_ready(rw_fiber_handle *h) {
    enqueue_ready(h);
}

/* Public wrapper for rw_sched_current_fiber (see sched.h). */
rw_fiber_handle *rw_sched_current_fiber(void) {
    rw_M *m = tls_m;
    return m ? m->current : NULL;
}
```

- [ ] **Step 1.3: Confirm the build and the existing tests are green**

```sh
make -C /Users/ryuichi/ghq/github.com/ryuichi1208/rw/runtime clean
make -C /Users/ryuichi/ghq/github.com/ryuichi1208/rw/runtime
```

Expected: Warning-free successful build (`librw.a` produced).

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw && uv run pytest -q 2>&1 | tail -5
```

Expected: `131 passed`.

- [ ] **Step 1.4: Commit**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
git add runtime/fiber/sched.h runtime/fiber/sched.c
git commit -m "$(cat <<'EOF'
runtime: export sched API for the upcoming netpoller

The netpoller thread (next commit) needs to:
  - enqueue a fiber back onto the ready queue when its fd becomes
    ready (rw_sched_enqueue_ready)
  - know which fiber is currently running on the calling thread so
    rw_net_park_read can record the handle before swapping out
    (rw_sched_current_fiber)

Both are thin wrappers around already-existing internals (the
private enqueue_ready helper and the _Thread_local tls_m pointer).
No behavior change for existing callers.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: netpoller skeleton (init/shutdown + shared header)

Implement only the startup and shutdown of the netpoller thread. park / wake does not work yet.

**Files:**
- Create: `runtime/net/netpoller.h`
- Create: `runtime/net/netpoller.c`
- Create: `runtime/net/netpoller_kqueue.c`
- Create: `runtime/net/netpoller_epoll.c`
- Modify: `runtime/Makefile`
- Modify: `runtime/runtime.h`
- Modify: `runtime/runtime.c`

- [ ] **Step 2.1: Create `runtime/net/netpoller.h`**

```c
#ifndef RW_NETPOLLER_H
#define RW_NETPOLLER_H

#include <stdint.h>

#include "../fiber/sched.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Process-level init/shutdown. Called from rw_init / rw_shutdown. */
void rw_netpoller_init(void);
void rw_netpoller_shutdown(void);

/* Set O_NONBLOCK on fd. Idempotent. Returns 0 on success, -1 on
 * failure (errno set). */
int rw_set_nonblocking(int fd);

/* Park the current fiber until fd becomes readable / writable.
 * Caller MUST be a worker thread running a fiber (rw_sched_current_fiber()
 * != NULL). Behavior on main thread or netpoller thread is undefined. */
void rw_net_park_read(int fd);
void rw_net_park_write(int fd);

/* ---- Internals shared between netpoller.c and the platform files ---- */
/* These are intentionally exposed inside the runtime so the platform
 * file can implement init/shutdown/register without exposing kqueue or
 * epoll types in the public header. */

/* Platform-specific init: open kqueue/epoll fd, return 0 on success. */
int  rw_netpoller_platform_init(void);
/* Platform-specific shutdown: close fds, wake the poll loop. */
void rw_netpoller_platform_shutdown(void);
/* The poll loop body. Called from the netpoller pthread. Returns when
 * shutdown has been requested. */
void rw_netpoller_platform_run(void);
/* Register fd for readable/writable readiness, associated with handle.
 * ONESHOT semantics: kernel auto-deregisters after one notification. */
int  rw_netpoller_register_read (int fd, rw_fiber_handle *h);
int  rw_netpoller_register_write(int fd, rw_fiber_handle *h);

#ifdef __cplusplus
}
#endif

#endif /* RW_NETPOLLER_H */
```

- [ ] **Step 2.2: Create `runtime/net/netpoller.c`**

```c
/*
 * Common netpoller logic: thread lifecycle, nonblocking setup, park
 * helpers. Platform-specific event-loop body lives in netpoller_kqueue.c
 * (macOS) or netpoller_epoll.c (Linux).
 */

#include "netpoller.h"

#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

static pthread_t   g_netpoller_thread;
static int         g_netpoller_started = 0;
static _Atomic int g_netpoller_shutdown = 0;

int rw_netpoller_is_shutdown(void);  /* exposed to platform files */
int rw_netpoller_is_shutdown(void) {
    return atomic_load_explicit(&g_netpoller_shutdown, memory_order_acquire);
}

int rw_set_nonblocking(int fd) {
    int flags = fcntl(fd, F_GETFL, 0);
    if (flags < 0) return -1;
    if (flags & O_NONBLOCK) return 0;
    return fcntl(fd, F_SETFL, flags | O_NONBLOCK);
}

static void *netpoller_main(void *arg) {
    (void)arg;
    rw_netpoller_platform_run();
    return NULL;
}

void rw_netpoller_init(void) {
    atomic_store_explicit(&g_netpoller_shutdown, 0, memory_order_release);
    if (rw_netpoller_platform_init() != 0) {
        perror("rw_netpoller_platform_init");
        abort();
    }
    if (pthread_create(&g_netpoller_thread, NULL, netpoller_main, NULL) != 0) {
        perror("pthread_create netpoller");
        abort();
    }
    g_netpoller_started = 1;
}

void rw_netpoller_shutdown(void) {
    if (!g_netpoller_started) return;
    atomic_store_explicit(&g_netpoller_shutdown, 1, memory_order_release);
    rw_netpoller_platform_shutdown();   /* wake the poll loop */
    pthread_join(g_netpoller_thread, NULL);
    g_netpoller_started = 0;
}

void rw_net_park_read(int fd) {
    rw_fiber_handle *me = rw_sched_current_fiber();
    if (!me) {
        fputs("rw: rw_net_park_read called outside a fiber\n", stderr);
        abort();
    }
    if (rw_netpoller_register_read(fd, me) != 0) {
        /* Registration failed: fall back to a yield to avoid deadlock. */
        return;
    }
    /* Manually park: set WAITING then swap out via the scheduler.
     * The scheduler's rw_sched_yield path normally re-enqueues, but
     * we want this fiber to stay off the run queue until the
     * netpoller wakes us. Use rw_sched_park_current() (Task 3). */
    extern void rw_sched_park_current(void);
    rw_sched_park_current();
}

void rw_net_park_write(int fd) {
    rw_fiber_handle *me = rw_sched_current_fiber();
    if (!me) {
        fputs("rw: rw_net_park_write called outside a fiber\n", stderr);
        abort();
    }
    if (rw_netpoller_register_write(fd, me) != 0) {
        return;
    }
    extern void rw_sched_park_current(void);
    rw_sched_park_current();
}
```

Note: `rw_sched_park_current` is added to sched.c in Task 3.

- [ ] **Step 2.3: Create `runtime/net/netpoller_kqueue.c`**

```c
/*
 * macOS / BSD netpoller backend using kqueue.
 */

#if defined(__APPLE__) || defined(__FreeBSD__)

#include "netpoller.h"

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/event.h>
#include <sys/types.h>
#include <unistd.h>

extern int rw_netpoller_is_shutdown(void);

static int g_kq = -1;

int rw_netpoller_platform_init(void) {
    g_kq = kqueue();
    return (g_kq < 0) ? -1 : 0;
}

void rw_netpoller_platform_shutdown(void) {
    /* Wake the kevent() that may be parked. EVFILT_USER with NOTE_TRIGGER
     * is the canonical way; we register it lazily inside run(). */
    struct kevent ev;
    EV_SET(&ev, 1, EVFILT_USER, 0, NOTE_TRIGGER, 0, NULL);
    kevent(g_kq, &ev, 1, NULL, 0, NULL);
}

static int register_user_wakeup(void) {
    struct kevent ev;
    EV_SET(&ev, 1, EVFILT_USER, EV_ADD | EV_CLEAR, 0, 0, NULL);
    return kevent(g_kq, &ev, 1, NULL, 0, NULL);
}

void rw_netpoller_platform_run(void) {
    register_user_wakeup();
    struct kevent events[128];
    while (!rw_netpoller_is_shutdown()) {
        int n = kevent(g_kq, NULL, 0, events, 128, NULL);
        if (n < 0) {
            if (errno == EINTR) continue;
            perror("kevent");
            break;
        }
        for (int i = 0; i < n; i++) {
            if (events[i].filter == EVFILT_USER) {
                /* shutdown wake-up; loop top will see the flag */
                continue;
            }
            rw_fiber_handle *h = (rw_fiber_handle *)events[i].udata;
            if (h) rw_sched_enqueue_ready(h);
        }
    }
    if (g_kq >= 0) { close(g_kq); g_kq = -1; }
}

int rw_netpoller_register_read(int fd, rw_fiber_handle *h) {
    struct kevent ev;
    EV_SET(&ev, fd, EVFILT_READ, EV_ADD | EV_ONESHOT, 0, 0, h);
    return kevent(g_kq, &ev, 1, NULL, 0, NULL);
}

int rw_netpoller_register_write(int fd, rw_fiber_handle *h) {
    struct kevent ev;
    EV_SET(&ev, fd, EVFILT_WRITE, EV_ADD | EV_ONESHOT, 0, 0, h);
    return kevent(g_kq, &ev, 1, NULL, 0, NULL);
}

#endif /* __APPLE__ || __FreeBSD__ */
```

- [ ] **Step 2.4: Create `runtime/net/netpoller_epoll.c`**

```c
/*
 * Linux netpoller backend using epoll + eventfd.
 */

#if defined(__linux__)

#include "netpoller.h"

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/epoll.h>
#include <sys/eventfd.h>
#include <unistd.h>

extern int rw_netpoller_is_shutdown(void);

static int g_ep = -1;
static int g_wake_fd = -1;     /* eventfd for shutdown wake-up */

int rw_netpoller_platform_init(void) {
    g_ep = epoll_create1(EPOLL_CLOEXEC);
    if (g_ep < 0) return -1;
    g_wake_fd = eventfd(0, EFD_CLOEXEC | EFD_NONBLOCK);
    if (g_wake_fd < 0) { close(g_ep); g_ep = -1; return -1; }
    struct epoll_event ev = {
        .events = EPOLLIN,
        .data.ptr = NULL,   /* NULL marks the wake-up fd */
    };
    if (epoll_ctl(g_ep, EPOLL_CTL_ADD, g_wake_fd, &ev) != 0) {
        close(g_wake_fd); close(g_ep); g_wake_fd = -1; g_ep = -1;
        return -1;
    }
    return 0;
}

void rw_netpoller_platform_shutdown(void) {
    if (g_wake_fd >= 0) {
        uint64_t one = 1;
        ssize_t r = write(g_wake_fd, &one, sizeof(one));
        (void)r;
    }
}

void rw_netpoller_platform_run(void) {
    struct epoll_event events[128];
    while (!rw_netpoller_is_shutdown()) {
        int n = epoll_wait(g_ep, events, 128, -1);
        if (n < 0) {
            if (errno == EINTR) continue;
            perror("epoll_wait");
            break;
        }
        for (int i = 0; i < n; i++) {
            void *p = events[i].data.ptr;
            if (p == NULL) {
                /* shutdown wake-up; drain the eventfd */
                uint64_t v;
                ssize_t r = read(g_wake_fd, &v, sizeof(v));
                (void)r;
                continue;
            }
            rw_sched_enqueue_ready((rw_fiber_handle *)p);
        }
    }
    if (g_wake_fd >= 0) { close(g_wake_fd); g_wake_fd = -1; }
    if (g_ep >= 0)      { close(g_ep);      g_ep = -1; }
}

static int register_oneshot(int fd, rw_fiber_handle *h, uint32_t events) {
    struct epoll_event ev = {
        .events = events | EPOLLONESHOT,
        .data.ptr = h,
    };
    /* Try MOD first (already registered), fall back to ADD. */
    if (epoll_ctl(g_ep, EPOLL_CTL_MOD, fd, &ev) == 0) return 0;
    if (errno != ENOENT) return -1;
    return epoll_ctl(g_ep, EPOLL_CTL_ADD, fd, &ev);
}

int rw_netpoller_register_read(int fd, rw_fiber_handle *h) {
    return register_oneshot(fd, h, EPOLLIN);
}

int rw_netpoller_register_write(int fd, rw_fiber_handle *h) {
    return register_oneshot(fd, h, EPOLLOUT);
}

#endif /* __linux__ */
```

- [ ] **Step 2.5: Update `runtime/Makefile`**

Update the `OBJS` line to add the new .o files:

```makefile
OBJS := runtime.o \
        fiber/fiber.o fiber/sched.o fiber/runq.o fiber/park.o \
        net/netpoller.o net/tcp.o $(NET_PLATFORM_O) \
        $(FIBER_ASM)
```

Add platform branching **directly below** the `UNAME_M` block:

```makefile
UNAME_S := $(shell uname -s)
ifeq ($(UNAME_S),Darwin)
  NET_PLATFORM_O := net/netpoller_kqueue.o
else ifeq ($(UNAME_S),Linux)
  NET_PLATFORM_O := net/netpoller_epoll.o
else
  $(error unsupported OS: $(UNAME_S))
endif
```

Add a rule for each .o right below the existing rule (`fiber/park.o`):

```makefile
net/netpoller.o: net/netpoller.c net/netpoller.h fiber/sched.h
	$(CC) $(CFLAGS) -c $< -o $@

net/netpoller_kqueue.o: net/netpoller_kqueue.c net/netpoller.h fiber/sched.h
	$(CC) $(CFLAGS) -c $< -o $@

net/netpoller_epoll.o: net/netpoller_epoll.c net/netpoller.h fiber/sched.h
	$(CC) $(CFLAGS) -c $< -o $@

net/tcp.o: net/tcp.c net/tcp.h net/netpoller.h fiber/sched.h
	$(CC) $(CFLAGS) -c $< -o $@
```

- [ ] **Step 2.6: Add prototypes to `runtime/runtime.h`**

Add them right below the `/* List[int] type and ops */` block and above `/* spawn (one per return type) */`:

```c
/* TCP API (runtime/net/tcp.c). */
int64_t rw_tcp_listen(int64_t port);
int64_t rw_tcp_accept(int64_t listen_fd);
void    rw_tcp_read  (rw_str *out, int64_t fd, int64_t max);
int64_t rw_tcp_write (int64_t fd, rw_str b);
int64_t rw_tcp_close (int64_t fd);
```

- [ ] **Step 2.7: Add the calls to `rw_init` / `rw_shutdown` in `runtime/runtime.c`**

`rw_init`:

```c
void rw_init(void) {
    rw_sched_init();
    rw_netpoller_init();
}
```

`rw_shutdown`:

```c
void rw_shutdown(void) {
    rw_netpoller_shutdown();
    rw_sched_shutdown();
}
```

Add to the `#include` block at the top of `runtime.c`:

```c
#include "net/netpoller.h"
```

- [ ] **Step 2.8: Confirm the build is green**

```sh
make -C /Users/ryuichi/ghq/github.com/ryuichi1208/rw/runtime clean
make -C /Users/ryuichi/ghq/github.com/ryuichi1208/rw/runtime
```

Expected: Warning-free successful build. `librw.a` includes `net/netpoller.o` and the others.

- [ ] **Step 2.9: Confirm the existing tests are green**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw && uv run pytest -q 2>&1 | tail -5
```

Expected: `131 passed`. The netpoller thread only does init/shutdown, so there is no harm.

- [ ] **Step 2.10: Commit**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
git add runtime/net/netpoller.h runtime/net/netpoller.c \
        runtime/net/netpoller_kqueue.c runtime/net/netpoller_epoll.c \
        runtime/runtime.h runtime/runtime.c runtime/Makefile
git commit -m "$(cat <<'EOF'
runtime/net: netpoller skeleton (init/shutdown only)

Adds the dedicated netpoller pthread and the platform-specific
event-loop bodies (kqueue on macOS/FreeBSD, epoll on Linux). The
loop just runs and idles on kevent/epoll_wait; nothing parks on
it yet — that lands in the next commit.

Wake-up mechanism for shutdown:
  - kqueue: EVFILT_USER with NOTE_TRIGGER
  - epoll: eventfd written from rw_netpoller_shutdown

rw_init/rw_shutdown now bracket the netpoller lifecycle around the
existing scheduler lifecycle. The TCP API prototypes are added to
runtime.h ahead of their implementation in a later commit so
rwc/irgen can declare them without ordering hassles.

No behavior change for non-network code: the netpoller pthread sits
in epoll_wait and consumes no CPU while idle.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Complete park / wake + pipe test

The netpoller thread is already running, so complete the API that parks a fiber and use a C test to confirm the fiber wakes on a ready notification.

**Files:**
- Modify: `runtime/fiber/sched.h` (add `rw_sched_park_current`)
- Modify: `runtime/fiber/sched.c` (implement `rw_sched_park_current`)
- Create: `runtime/fiber/test_netpoller_pipe.c`
- Modify: `.gitignore`

- [ ] **Step 3.1: Add `rw_sched_park_current` to `runtime/fiber/sched.h`**

Below the exports added in Task 1:

```c
/* Mark the current fiber as WAITING and swap out to the scheduler.
 * The fiber will NOT be re-enqueued by the scheduler; someone must
 * call rw_sched_enqueue_ready(handle) to wake it later. Used by the
 * netpoller to park fibers on fd readiness. */
void rw_sched_park_current(void);
```

- [ ] **Step 3.2: Implement `rw_sched_park_current` in `runtime/fiber/sched.c`**

Add it right below the existing `rw_sched_yield`:

```c
/* Park the current fiber: mark it WAITING and swap to sched_ctx.
 * Unlike rw_sched_yield, the fiber is NOT re-enqueued — the caller
 * must arrange a wake-up via rw_sched_enqueue_ready(). */
void rw_sched_park_current(void) {
    rw_M *m = tls_m;
    if (!m) return;
    rw_fiber_handle *me = m->current;
    if (!me) return;
    atomic_store_explicit(&me->state, RW_FIBER_WAITING,
                          memory_order_relaxed);
    rw_fiber_swap(&me->ctx, &m->sched_ctx);
}
```

worker_main's `if (state == RUNNING) enqueue_ready(g)` check stays as-is, and since a WAITING
fiber is not automatically re-enqueued, this works as intended.

- [ ] **Step 3.3: Confirm the build is green**

```sh
make -C /Users/ryuichi/ghq/github.com/ryuichi1208/rw/runtime
```

Expected: Warning-free successful build.

- [ ] **Step 3.4: Create `runtime/fiber/test_netpoller_pipe.c`**

Read and write a pipe(2) across two fibers to confirm the behavior: reader parks -> writer writes -> reader wakes:

```c
/*
 * Pipe-based smoke test for the netpoller.
 *
 *   - Create a pipe(2).
 *   - Spawn a reader fiber: rw_net_park_read(read_end), then read(2).
 *     Verify the bytes match what the writer wrote.
 *   - Spawn a writer fiber: write(write_end, ...).
 *   - Join both.
 *
 * Tests:
 *   - rw_netpoller_init/shutdown lifecycle
 *   - rw_set_nonblocking on a pipe fd
 *   - rw_net_park_read actually blocks until the writer makes data
 *     available, then resumes
 */

#include "../../net/netpoller.h"
#include "../sched.h"
#include "../../runtime.h"

#include <assert.h>
#include <errno.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static int g_pipe[2];

static int64_t reader_fiber(void *arg) {
    (void)arg;
    rw_set_nonblocking(g_pipe[0]);
    char buf[16];
    for (;;) {
        ssize_t n = read(g_pipe[0], buf, sizeof(buf));
        if (n > 0) {
            return (int64_t)n;
        }
        if (n == 0) return 0;  /* EOF */
        if (errno == EAGAIN || errno == EWOULDBLOCK) {
            rw_net_park_read(g_pipe[0]);
            continue;
        }
        return -1;
    }
}

static int64_t writer_fiber(void *arg) {
    (void)arg;
    /* Sleep a moment to make sure the reader has parked. */
    struct timespec ts = { .tv_sec = 0, .tv_nsec = 50 * 1000 * 1000 };
    nanosleep(&ts, NULL);
    const char *msg = "hello\n";
    ssize_t n = write(g_pipe[1], msg, strlen(msg));
    return (int64_t)n;
}

int main(void) {
    rw_init();

    if (pipe(g_pipe) != 0) { perror("pipe"); return 1; }

    rw_future_t *r = rw_spawn_i64(reader_fiber, NULL);
    rw_future_t *w = rw_spawn_i64(writer_fiber, NULL);

    int64_t wrote = rw_await_i64(w);
    int64_t got   = rw_await_i64(r);

    assert(wrote == 6);
    assert(got == 6);

    close(g_pipe[0]);
    close(g_pipe[1]);

    rw_shutdown();
    printf("netpoller pipe test ok\n");
    return 0;
}
```

- [ ] **Step 3.5: Add the test binary to `.gitignore`**

Add below `runtime/fiber/test_option`:

```
runtime/fiber/test_netpoller_pipe
```

- [ ] **Step 3.6: Build and run the test**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw/runtime
cc -O2 -Wall -Wextra -std=c11 -D_GNU_SOURCE -pthread fiber/test_netpoller_pipe.c librw.a -o fiber/test_netpoller_pipe && ./fiber/test_netpoller_pipe
```

Expected: `netpoller pipe test ok`.

If it times out, the netpoller thread is not correctly `enqueue_ready`ing on ready notifications (a problem in the Task 2 platform implementation or the Task 3.2 park logic). Kill it with `pkill test_netpoller_pipe` and review the logs.

- [ ] **Step 3.7: Existing tests also stay regression-free**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw && uv run pytest -q 2>&1 | tail -5
```

Expected: `131 passed`。

- [ ] **Step 3.8: Commit**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
git add runtime/fiber/sched.h runtime/fiber/sched.c runtime/fiber/test_netpoller_pipe.c .gitignore
git commit -m "$(cat <<'EOF'
runtime/net: park/wake working end-to-end (pipe test)

Adds rw_sched_park_current(): sets the calling fiber's state to
WAITING and swaps out to the worker's scheduler ctx, with no
re-enqueue (the netpoller will do that later via
rw_sched_enqueue_ready). worker_main already skips re-enqueue when
state != RUNNING, so this composes cleanly with the existing
M:N scheduler.

C-level test (fiber/test_netpoller_pipe.c) verifies the full loop:
  - Open pipe(2), spawn reader and writer fibers.
  - Reader calls rw_net_park_read on the read end (nonblocking
    pipe, EAGAIN on first read), then resumes when the netpoller
    notifies.
  - Writer sleeps 50ms then writes "hello\n".
  - Both fibers join with the expected 6 bytes.

Works on macOS (kqueue) and Linux (epoll); same test source for both.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: tcp_* helpers + localhost loopback test

Implement `runtime/net/tcp.c` / `tcp.h` and confirm in C that listen -> connect -> recv/send works on localhost.

**Files:**
- Create: `runtime/net/tcp.h`
- Create: `runtime/net/tcp.c`
- Create: `runtime/fiber/test_tcp_loopback.c`
- Modify: `.gitignore`

- [ ] **Step 4.1: Create `runtime/net/tcp.h`**

```c
#ifndef RW_TCP_H
#define RW_TCP_H

#include <stdint.h>

#include "../runtime.h"

#ifdef __cplusplus
extern "C" {
#endif

/* See runtime.h for the prototypes; this header exists so net/tcp.c
 * can include the netpoller internals without polluting the public
 * runtime.h. */

#ifdef __cplusplus
}
#endif

#endif /* RW_TCP_H */
```

- [ ] **Step 4.2: Create `runtime/net/tcp.c`**

```c
/*
 * TCP helpers. See docs/specs/12-netpoller-tcp.md for the design.
 *
 * - Listen fd is created blocking; main-thread tcp_accept uses the
 *   blocking accept() so the OS can kernel-sleep the main thread.
 * - Fiber-thread tcp_accept switches the listen fd to nonblocking
 *   on first use, then loops on accept + netpoller park.
 * - tcp_read / tcp_write always assume the fd is nonblocking
 *   (set by tcp_accept when handing out the client fd).
 */

#include "tcp.h"
#include "netpoller.h"
#include "../fiber/sched.h"
#include "../runtime.h"

#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>

int64_t rw_tcp_listen(int64_t port) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) return -1;
    int one = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    addr.sin_port = htons((uint16_t)port);
    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        close(fd);
        return -1;
    }
    if (listen(fd, 128) < 0) {
        close(fd);
        return -1;
    }
    return (int64_t)fd;
}

int64_t rw_tcp_accept(int64_t listen_fd) {
    rw_fiber_handle *me = rw_sched_current_fiber();
    if (me == NULL) {
        /* main thread: blocking accept */
        int c = accept((int)listen_fd, NULL, NULL);
        if (c < 0) return -1;
        rw_set_nonblocking(c);
        return (int64_t)c;
    }
    /* fiber thread: nonblocking + park */
    rw_set_nonblocking((int)listen_fd);
    for (;;) {
        int c = accept((int)listen_fd, NULL, NULL);
        if (c >= 0) {
            rw_set_nonblocking(c);
            return (int64_t)c;
        }
        if (errno == EAGAIN || errno == EWOULDBLOCK) {
            rw_net_park_read((int)listen_fd);
            continue;
        }
        return -1;
    }
}

void rw_tcp_read(rw_str *out, int64_t fd, int64_t max) {
    if (max <= 0) { out->len = 0; out->ptr = NULL; return; }
    char *buf = (char *)malloc((size_t)max);
    if (!buf)     { out->len = 0; out->ptr = NULL; return; }
    for (;;) {
        ssize_t n = recv((int)fd, buf, (size_t)max, 0);
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

int64_t rw_tcp_write(int64_t fd, rw_str b) {
    if (b.len <= 0) return 0;
    for (;;) {
        ssize_t n = send((int)fd, b.ptr, (size_t)b.len, 0);
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

int64_t rw_tcp_close(int64_t fd) {
    return (int64_t)close((int)fd);
}
```

- [ ] **Step 4.3: Create `runtime/fiber/test_tcp_loopback.c`**

Listen on localhost -> a separate fiber connects -> perform recv/send:

```c
/*
 * TCP loopback smoke test. Spawns a server fiber that listens on
 * 127.0.0.1:<random>, accepts one connection, echoes a single
 * message, then closes. The client side runs in a separate pthread
 * (NOT a fiber) so we can do blocking connect/send/recv from a
 * "user-perspective" caller.
 */

#include "../../net/netpoller.h"
#include "../sched.h"
#include "../../runtime.h"

#include <arpa/inet.h>
#include <assert.h>
#include <netinet/in.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

static int g_port;

static int64_t server_fiber(void *arg) {
    int listen_fd = (int)(intptr_t)arg;
    int64_t client = rw_tcp_accept((int64_t)listen_fd);
    if (client < 0) return -1;
    rw_str msg = { .len = 0, .ptr = NULL };
    rw_tcp_read(&msg, client, 64);
    if (msg.len <= 0) { rw_tcp_close(client); return -2; }
    rw_tcp_write(client, msg);
    rw_tcp_close(client);
    return 0;
}

static void *client_thread(void *arg) {
    (void)arg;
    /* Give the server time to start listening. */
    struct timespec ts = { .tv_sec = 0, .tv_nsec = 100 * 1000 * 1000 };
    nanosleep(&ts, NULL);

    int s = socket(AF_INET, SOCK_STREAM, 0);
    if (s < 0) return (void *)1;
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    addr.sin_port = htons((uint16_t)g_port);
    if (connect(s, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        close(s); return (void *)2;
    }
    const char *m = "ping\n";
    send(s, m, strlen(m), 0);
    char buf[64];
    ssize_t n = recv(s, buf, sizeof(buf), 0);
    if (n != 5 || memcmp(buf, "ping\n", 5) != 0) {
        close(s); return (void *)3;
    }
    close(s);
    return NULL;
}

int main(void) {
    rw_init();

    int64_t lfd = rw_tcp_listen(0);   /* let kernel pick port */
    if (lfd < 0) { fprintf(stderr, "listen failed\n"); return 1; }
    /* Read the assigned port via getsockname. */
    struct sockaddr_in sa; socklen_t sl = sizeof(sa);
    getsockname((int)lfd, (struct sockaddr *)&sa, &sl);
    g_port = ntohs(sa.sin_port);

    pthread_t client;
    pthread_create(&client, NULL, client_thread, NULL);

    rw_future_t *sfut = rw_spawn_i64(server_fiber, (void *)(intptr_t)lfd);
    int64_t srv_rc = rw_await_i64(sfut);
    assert(srv_rc == 0);

    void *cli_rc;
    pthread_join(client, &cli_rc);
    assert(cli_rc == NULL);

    rw_tcp_close(lfd);
    rw_shutdown();
    printf("tcp loopback test ok\n");
    return 0;
}
```

- [ ] **Step 4.4: Update `.gitignore`**

Add below `test_netpoller_pipe`:

```
runtime/fiber/test_tcp_loopback
```

- [ ] **Step 4.5: Build + run**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw/runtime
make
cc -O2 -Wall -Wextra -std=c11 -D_GNU_SOURCE -pthread fiber/test_tcp_loopback.c librw.a -o fiber/test_tcp_loopback && ./fiber/test_tcp_loopback
```

Expected: `tcp loopback test ok`.

If it times out, the fiber parked in `tcp_accept` is not waking, or the park in `tcp_read` / `tcp_write` is not working. If `netpoller_pipe` works, the netpoller itself is healthy.

- [ ] **Step 4.6: The pipe test also stays green**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw/runtime
./fiber/test_netpoller_pipe
```

Expected: `netpoller pipe test ok`.

- [ ] **Step 4.7: pytest also green**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw && uv run pytest -q 2>&1 | tail -5
```

Expected: `131 passed`。

- [ ] **Step 4.8: Commit**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
git add runtime/net/tcp.h runtime/net/tcp.c runtime/fiber/test_tcp_loopback.c .gitignore
git commit -m "$(cat <<'EOF'
runtime/net: tcp_* helpers (listen / accept / read / write / close)

Adds the five C helpers backing the upcoming rw-language TCP API:

  rw_tcp_listen(port)  — bind 0.0.0.0:port, listen(128), return fd
  rw_tcp_accept(lfd)   — branches on rw_sched_current_fiber():
                         main thread = blocking accept,
                         fiber thread = nonblocking + park
  rw_tcp_read(out, fd, max) — recv loop with EAGAIN park on fibers,
                              malloc'd Bytes (len==0 for EOF/error)
  rw_tcp_write(fd, b)  — send loop with EAGAIN park
  rw_tcp_close(fd)     — straight close()

C-level smoke test (fiber/test_tcp_loopback.c):
  - server fiber listens on 127.0.0.1:<kernel-chosen port>, accepts
    one client, echoes "ping\n" once, closes.
  - client runs in a regular pthread, connect/send/recv "ping\n".
  - assert both round-trip is byte-identical.

The server fiber exercises the full "park on accept", "park on read"
chain via the netpoller; the kernel-chosen port avoids CI clashes.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Add the five builtins to rwc + sema/irgen tests

**Files:**
- Modify: `rwc/sema.py`
- Modify: `rwc/irgen.py`
- Modify: `tests/test_sema.py`

- [ ] **Step 5.1: Add the five builtins to Sema**

Add them right below the `list_at_opt` Sema in `rwc/sema.py`:

```python
        # Builtin: tcp_listen(int) -> int.
        if call.callee == "tcp_listen":
            if len(call.args) != 1:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"tcp_listen takes 1 argument, got {len(call.args)}",
                ))
            at = self._check_expr(fn, call.args[0], locals_)
            if at is not T.INT:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"tcp_listen argument must be int, found `{at}`",
                ))
            return T.INT
        # Builtin: tcp_accept(int) -> int.
        if call.callee == "tcp_accept":
            if len(call.args) != 1:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"tcp_accept takes 1 argument, got {len(call.args)}",
                ))
            at = self._check_expr(fn, call.args[0], locals_)
            if at is not T.INT:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"tcp_accept argument must be int, found `{at}`",
                ))
            return T.INT
        # Builtin: tcp_read(int, int) -> Bytes.
        if call.callee == "tcp_read":
            if len(call.args) != 2:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"tcp_read takes 2 arguments, got {len(call.args)}",
                ))
            t0 = self._check_expr(fn, call.args[0], locals_)
            t1 = self._check_expr(fn, call.args[1], locals_)
            if t0 is not T.INT:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"tcp_read first argument must be int, found `{t0}`",
                ))
            if t1 is not T.INT:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"tcp_read second argument must be int, found `{t1}`",
                ))
            return T.BYTES
        # Builtin: tcp_write(int, Bytes) -> int.
        if call.callee == "tcp_write":
            if len(call.args) != 2:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"tcp_write takes 2 arguments, got {len(call.args)}",
                ))
            t0 = self._check_expr(fn, call.args[0], locals_)
            t1 = self._check_expr(fn, call.args[1], locals_)
            if t0 is not T.INT:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"tcp_write first argument must be int, found `{t0}`",
                ))
            if t1 is not T.BYTES:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"tcp_write second argument must be Bytes, found `{t1}`",
                ))
            return T.INT
        # Builtin: tcp_close(int) -> int.
        if call.callee == "tcp_close":
            if len(call.args) != 1:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"tcp_close takes 1 argument, got {len(call.args)}",
                ))
            at = self._check_expr(fn, call.args[0], locals_)
            if at is not T.INT:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"tcp_close argument must be int, found `{at}`",
                ))
            return T.INT
```

- [ ] **Step 5.2: Add the five to the SpawnExpr prohibition list**

Add them **directly below** the `list_at_opt` prohibition branch:

```python
                if call.callee == "tcp_listen":
                    raise CompileError(Diagnostic(
                        self.filename, expr.line, expr.col, 5,
                        "cannot spawn the builtin `tcp_listen`",
                    ))
                if call.callee == "tcp_accept":
                    raise CompileError(Diagnostic(
                        self.filename, expr.line, expr.col, 5,
                        "cannot spawn the builtin `tcp_accept`",
                    ))
                if call.callee == "tcp_read":
                    raise CompileError(Diagnostic(
                        self.filename, expr.line, expr.col, 5,
                        "cannot spawn the builtin `tcp_read`",
                    ))
                if call.callee == "tcp_write":
                    raise CompileError(Diagnostic(
                        self.filename, expr.line, expr.col, 5,
                        "cannot spawn the builtin `tcp_write`",
                    ))
                if call.callee == "tcp_close":
                    raise CompileError(Diagnostic(
                        self.filename, expr.line, expr.col, 5,
                        "cannot spawn the builtin `tcp_close`",
                    ))
```

- [ ] **Step 5.3: Add emit for the five builtins to irgen**

In `rwc/irgen.py`'s `_declare_runtime`, add **directly below** the `rw_list_int_at_opt` declaration:

```python
        # TCP API (runtime/net/tcp.c)
        self._rw_tcp_listen = ir.Function(
            m, ir.FunctionType(I64, [I64]), "rw_tcp_listen")
        self._rw_tcp_accept = ir.Function(
            m, ir.FunctionType(I64, [I64]), "rw_tcp_accept")
        self._rw_tcp_read = ir.Function(
            m, ir.FunctionType(ir.VoidType(),
                               [RW_STR_TY.as_pointer(), I64, I64]),
            "rw_tcp_read")
        self._rw_tcp_write = ir.Function(
            m, ir.FunctionType(I64, [I64, RW_STR_TY]), "rw_tcp_write")
        self._rw_tcp_close = ir.Function(
            m, ir.FunctionType(I64, [I64]), "rw_tcp_close")
```

In `_emit_call`, add **directly below** the `list_at_opt` branch:

```python
        if call.callee == "tcp_listen":
            v = self._emit_expr(call.args[0], ctx)
            return ctx.builder.call(self._rw_tcp_listen, [v])
        if call.callee == "tcp_accept":
            v = self._emit_expr(call.args[0], ctx)
            return ctx.builder.call(self._rw_tcp_accept, [v])
        if call.callee == "tcp_read":
            fd_v = self._emit_expr(call.args[0], ctx)
            mx_v = self._emit_expr(call.args[1], ctx)
            out_slot = ctx.builder.alloca(RW_STR_TY)
            ctx.builder.call(self._rw_tcp_read, [out_slot, fd_v, mx_v])
            return ctx.builder.load(out_slot)
        if call.callee == "tcp_write":
            fd_v = self._emit_expr(call.args[0], ctx)
            b_v  = self._emit_expr(call.args[1], ctx)
            return ctx.builder.call(self._rw_tcp_write, [fd_v, b_v])
        if call.callee == "tcp_close":
            v = self._emit_expr(call.args[0], ctx)
            return ctx.builder.call(self._rw_tcp_close, [v])
```

- [ ] **Step 5.4: Positive tests**

Add to the end of `tests/test_sema.py`:

```python
# ---- TCP builtins positive cases ----

def test_tcp_listen_returns_int():
    src = (
        "def main() -> int:\n"
        "    fd: int = tcp_listen(8080)\n"
        "    return fd\n"
    )
    check(src)


def test_tcp_accept_returns_int():
    src = (
        "def main() -> int:\n"
        "    lfd: int = tcp_listen(8080)\n"
        "    cfd: int = tcp_accept(lfd)\n"
        "    return cfd\n"
    )
    check(src)


def test_tcp_read_returns_bytes():
    src = (
        "def main() -> int:\n"
        "    lfd: int = tcp_listen(8080)\n"
        "    cfd: int = tcp_accept(lfd)\n"
        "    b: Bytes = tcp_read(cfd, 4096)\n"
        "    return len(b)\n"
    )
    check(src)


def test_tcp_write_returns_int():
    src = (
        "def main() -> int:\n"
        "    lfd: int = tcp_listen(8080)\n"
        "    cfd: int = tcp_accept(lfd)\n"
        "    b: Bytes = tcp_read(cfd, 4096)\n"
        "    n: int = tcp_write(cfd, b)\n"
        "    return n\n"
    )
    check(src)


def test_tcp_close_returns_int():
    src = (
        "def main() -> int:\n"
        "    lfd: int = tcp_listen(8080)\n"
        "    rc: int = tcp_close(lfd)\n"
        "    return rc\n"
    )
    check(src)
```

- [ ] **Step 5.5: Negative tests**

Add to the end of `tests/test_sema.py`:

```python
# ---- TCP builtins negative cases ----

def test_tcp_listen_wrong_arg_type():
    src = (
        "def main() -> int:\n"
        "    fd: int = tcp_listen(\"8080\")\n"
        "    return fd\n"
    )
    e = err(src)
    assert "tcp_listen argument must be int" in e.diagnostic.message


def test_tcp_read_wrong_max_type():
    src = (
        "def main() -> int:\n"
        "    b: Bytes = tcp_read(3, \"big\")\n"
        "    return 0\n"
    )
    e = err(src)
    assert "tcp_read second argument must be int" in e.diagnostic.message


def test_tcp_write_wrong_buffer_type():
    src = (
        "def main() -> int:\n"
        "    n: int = tcp_write(3, \"hi\")\n"
        "    return n\n"
    )
    e = err(src)
    assert "tcp_write second argument must be Bytes" in e.diagnostic.message


def test_tcp_listen_wrong_arity():
    src = (
        "def main() -> int:\n"
        "    fd: int = tcp_listen()\n"
        "    return fd\n"
    )
    e = err(src)
    assert "tcp_listen takes 1 argument" in e.diagnostic.message


def test_cannot_spawn_tcp_accept():
    src = (
        "def main() -> int:\n"
        "    f: Future[int] = spawn tcp_accept(3)\n"
        "    return 0\n"
    )
    e = err(src)
    assert "cannot spawn the builtin `tcp_accept`" in e.diagnostic.message
```

- [ ] **Step 5.6: Run pytest**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw && uv run pytest -q 2>&1 | tail -5
```

Expected: existing 131 + 5 positive + 5 negative = `141 passed`.

- [ ] **Step 5.7: smoke check (inspect the IR and build)**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
cat > /tmp/tcp_smoke.rw <<'EOF'
def main() -> int:
    fd: int = tcp_listen(8081)
    rc: int = tcp_close(fd)
    return rc
EOF
uv run rwc emit-ir /tmp/tcp_smoke.rw 2>&1 | grep -E "rw_tcp_" | head -5
echo "---"
uv run rwc build /tmp/tcp_smoke.rw -o /tmp/tcp_smoke
ls -la /tmp/tcp_smoke && /tmp/tcp_smoke && echo "exit=$?"
```

Expected:
- The IR shows `declare i64 @"rw_tcp_listen"`, `call i64 @"rw_tcp_close"`, and so on
- Build succeeds
- Execution gives `exit=0` (listen -> close, then exits immediately)

- [ ] **Step 5.8: Commit**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
git add rwc/sema.py rwc/irgen.py tests/test_sema.py
git commit -m "$(cat <<'EOF'
rwc: add five tcp_* builtins to sema + irgen

Sema:
  - tcp_listen(int) -> int
  - tcp_accept(int) -> int
  - tcp_read(int, int) -> Bytes
  - tcp_write(int, Bytes) -> int
  - tcp_close(int) -> int
  Each enforces arg types; spawn of any of these is forbidden.

irgen:
  - All five extern declarations land in _declare_runtime alongside
    the list_int helpers.
  - tcp_read uses the pointer-out shim (alloca RW_STR_TY, call with
    out-pointer, load the result back) because the 16-byte rw_str
    is value-returnable but we keep the helper signature consistent
    with the rest of the netpoller code.
  - The rest are scalar passes (i64 fd, i64 result).

Tests (10 new in test_sema.py): 5 positive (one per builtin), 5
negative (wrong-type, wrong-arity, spawn-of-builtin).

End-to-end smoke: a minimal `tcp_listen(8081); tcp_close(fd)`
program builds and runs cleanly.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: example + e2e

**Files:**
- Create: `examples/tcp_echo.rw`
- Create: `tests/test_e2e_tcp.py`

- [ ] **Step 6.1: Create `examples/tcp_echo.rw`**

```rw
def handle_client(fd: int) -> int:
    while true:
        b: Bytes = tcp_read(fd, 4096)
        if len(b) == 0:
            tcp_close(fd)
            return 0
        tcp_write(fd, b)

def main() -> int:
    listen_fd: int = tcp_listen(__PORT__)
    while true:
        client: int = tcp_accept(listen_fd)
        spawn handle_client(client)
    return 0
```

The `__PORT__` placeholder is replaced with a dynamically-chosen free port per test in the e2e.
Since `__PORT__` would cause a parse error when a user manually runs `rwc run examples/tcp_echo.rw`,
it is simpler to **bake in `8080` from the start and have the e2e build a separate tmp .rw**. Revised:

```rw
def handle_client(fd: int) -> int:
    while true:
        b: Bytes = tcp_read(fd, 4096)
        if len(b) == 0:
            tcp_close(fd)
            return 0
        tcp_write(fd, b)

def main() -> int:
    listen_fd: int = tcp_listen(8080)
    while true:
        client: int = tcp_accept(listen_fd)
        spawn handle_client(client)
    return 0
```

The e2e reads `examples/tcp_echo.rw`, seds `tcp_listen(8080)` to `tcp_listen(<random>)`, saves it to /tmp, then builds and launches it.

- [ ] **Step 6.2: Create `tests/test_e2e_tcp.py`**

```python
"""End-to-end tests for the TCP echo example."""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _build_echo_server(port: int) -> Path:
    """Materialise examples/tcp_echo.rw with the desired port and
    build it. Returns the path to the built binary."""
    src = (EXAMPLES / "tcp_echo.rw").read_text()
    src = re.sub(r"tcp_listen\(\d+\)", f"tcp_listen({port})", src, count=1)
    td = tempfile.mkdtemp()
    rw_path = Path(td) / "tcp_echo_test.rw"
    rw_path.write_text(src)
    out = Path(td) / "tcp_echo_test"
    build = subprocess.run(
        [sys.executable, "-m", "rwc.cli", "build", str(rw_path), "-o", str(out)],
        capture_output=True, text=True, cwd=REPO,
    )
    assert build.returncode == 0, f"rwc build failed:\n{build.stderr}"
    return out


def _start_server(binary: Path) -> subprocess.Popen:
    env = {**os.environ, "RW_WORKERS": "2"}
    proc = subprocess.Popen([str(binary)], env=env,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    time.sleep(0.3)  # give the server time to bind+listen
    return proc


def test_echo_single_connection():
    port = _free_port()
    binary = _build_echo_server(port)
    proc = _start_server(binary)
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=2.0)
        s.sendall(b"hello\n")
        data = s.recv(64)
        assert data == b"hello\n"
        s.close()
    finally:
        proc.terminate()
        proc.wait(timeout=2.0)


def test_echo_ten_concurrent_connections():
    port = _free_port()
    binary = _build_echo_server(port)
    proc = _start_server(binary)
    try:
        socks = [socket.create_connection(("127.0.0.1", port), timeout=2.0)
                 for _ in range(10)]
        for i, s in enumerate(socks):
            s.sendall(f"client-{i}\n".encode())
        for i, s in enumerate(socks):
            data = s.recv(64)
            assert data == f"client-{i}\n".encode(), f"client {i}: {data!r}"
        for s in socks:
            s.close()
    finally:
        proc.terminate()
        proc.wait(timeout=2.0)
```

- [ ] **Step 6.3: Manual smoke**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
uv run rwc build examples/tcp_echo.rw -o /tmp/tcp_echo
/tmp/tcp_echo &
SERVER_PID=$!
sleep 0.3
echo -n "hello" | nc -w 1 127.0.0.1 8080
kill $SERVER_PID 2>/dev/null
```

Expected: `hello` comes back unchanged.

- [ ] **Step 6.4: Run the full pytest suite**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw && uv run pytest -q 2>&1 | tail -5
```

Expected: existing 141 (through Task 5) + 2 new e2e_tcp = `143 passed`.

If `test_echo_ten_concurrent_connections` is flaky (e.g. `sleep 0.3` is too short in CI), raise `_start_server`'s `time.sleep` to 0.5.

- [ ] **Step 6.5: Verify existing examples are regression-free**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
RW_WORKERS=1 uv run rwc run examples/option_basic.rw
RW_WORKERS=1 uv run rwc run examples/result_basic.rw
RW_WORKERS=1 uv run rwc run examples/spawn_many.rw
```

Expected: `5\n-1`, `5\n0`, and `30` respectively.

- [ ] **Step 6.6: Commit**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
git add examples/tcp_echo.rw tests/test_e2e_tcp.py
git commit -m "$(cat <<'EOF'
examples + e2e: tcp_echo.rw with single + 10-concurrent tests

examples/tcp_echo.rw is the canonical TCP echo server demo. main()
calls tcp_listen(8080) — the only thing that runs on the main
OS thread, which blocks in accept(). Each accepted client is
handed to a fresh fiber via spawn handle_client(client), so 10000
clients map to 10000 fibers, not 10000 threads.

tests/test_e2e_tcp.py rewrites the port literal to a kernel-picked
free port at test time (avoids CI collisions), builds the example,
spawns the server, and exercises it from regular Python sockets:

  - test_echo_single_connection: one connect/send/recv round-trip
  - test_echo_ten_concurrent_connections: 10 parallel clients send
    distinct payloads and assert each gets its own payload back

The 10-client test is the smallest experiment that meaningfully
exercises spawn-per-connection + the netpoller; bigger benchmarks
(C10k etc.) are deliberately out of scope (see Non-Goals in the
spec).

RW_WORKERS=2 is set in the env so the server runs with at least
two worker M's even on single-core CI runners.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Commit the plan file

- [ ] **Step 7.1: Commit**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
git add docs/plans/2026-05-23-netpoller-tcp.md
git commit -m "docs: add netpoller-tcp implementation plan

Plan file authored during the planning phase and referenced by the
spec at docs/specs/12-netpoller-tcp.md.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Self-Review

### Spec coverage

| Spec requirement | Covering task |
|---|---|
| Dedicated netpoller pthread (kqueue / epoll) | Task 2 (all steps) |
| `rw_net_park_read/write` | Task 2.2 + 3.2 |
| `rw_set_nonblocking` | Task 2.2 |
| `rw_netpoller_init/shutdown` from `rw_init/shutdown` | Task 2.7 |
| Shutdown wake-up (kqueue EVFILT_USER / epoll eventfd) | Task 2.3 / 2.4 |
| ONESHOT monitoring | Task 2.3 / 2.4 |
| `tcp_listen(port) -> int` | Task 4.2 (runtime) + 5.1 (sema) + 5.3 (irgen) + 5.4 (test) |
| `tcp_accept(int) -> int`, main blocks / fiber parks | Task 4.2 + 5.1 + 5.3 + 5.4 |
| `tcp_read(int, int) -> Bytes`, len==0 for EOF/error | Task 4.2 + 5.1 + 5.3 + 5.4 |
| `tcp_write(int, Bytes) -> int` | Task 4.2 + 5.1 + 5.3 + 5.4 |
| `tcp_close(int) -> int` | Task 4.2 + 5.1 + 5.3 + 5.4 |
| All five builtins forbid spawn | Task 5.2 + test 5.5 (`test_cannot_spawn_tcp_accept`) |
| C test: netpoller pipe | Task 3.4 |
| C test: tcp loopback | Task 4.3 |
| e2e: 1 connection + 10 concurrent connections | Task 6.2 |
| `examples/tcp_echo.rw` | Task 6.1 |
| Existing 131 tests green, existing examples regression-free | Task 2.9 / 3.7 / 4.7 / 6.4 / 6.5 |
| Export enqueue_ready / current_fiber from sched.h | Task 1 |
| Add `rw_sched_park_current` | Task 3.1 + 3.2 |
| Makefile uname branching | Task 2.5 |

Every spec requirement has a task.

### Placeholder scan

"TBD", "TODO", "(needs confirmation)", "fill in", "Add appropriate", and "Similar to Task N" appear 0 times in the plan.

### Type consistency

- The signatures of `rw_netpoller_init` / `_shutdown` / `_platform_init` / `_platform_shutdown` / `_platform_run` / `_register_read` / `_register_write` match exactly between Task 2.1 (declaration) and Task 2.2/2.3/2.4 (implementation)
- The signatures of `rw_net_park_read` / `_write` / `rw_set_nonblocking` are used consistently across Task 2.1 / 2.2 / 4.2
- `rw_sched_enqueue_ready` / `_current_fiber` / `_park_current` are consistently declared/implemented in Task 1.1 / 1.2 / 3.1 / 3.2 and called in Task 2.2 / 2.3 / 2.4 / 4.2
- The signatures of `rw_tcp_listen` / `_accept` / `_read` / `_write` / `_close` match exactly between Task 2.6 (runtime.h) / 4.2 (implementation) / 5.3 (irgen declaration)
- `RW_STR_TY` is used in `tcp_read`'s pointer-out shim (Task 5.3), following the same alloca/load pattern as the existing string helpers
