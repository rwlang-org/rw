# netpoller + TCP API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** rw 言語に最小 TCP API (`tcp_listen` / `tcp_accept` / `tcp_read` / `tcp_write` / `tcp_close`) と、それを支える netpoller (kqueue/epoll) を追加し、`examples/tcp_echo.rw` が動く状態にする。

**Architecture:** 専用 netpoller pthread を 1 つ用意し、`kevent` / `epoll_wait` で fd readiness を ONESHOT モードで監視。fiber が `tcp_read` 等で EAGAIN を受けたら `rw_net_park_read(fd)` で netpoller に登録し WAITING、netpoller スレッドが ready 検知時に `enqueue_ready(fiber)` で起こす。main thread は fiber じゃないので `tcp_accept` を呼ぶと blocking accept で kernel sleep、worker M / netpoller は別 thread なので並行進行。

**Tech Stack:** C11 (ランタイム、kqueue on macOS / epoll on Linux)、Python 3.12 + llvmlite (コンパイラ)、pytest (テスト)。

**Spec:** `docs/specs/12-netpoller-tcp.md`

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `runtime/net/netpoller.h` | netpoller 共通 API | 新規 |
| `runtime/net/netpoller.c` | init / shutdown / park / 共通ロジック | 新規 |
| `runtime/net/netpoller_kqueue.c` | macOS 固有 (kevent) | 新規 |
| `runtime/net/netpoller_epoll.c` | Linux 固有 (epoll) | 新規 |
| `runtime/net/tcp.h` | TCP helper 宣言 | 新規 |
| `runtime/net/tcp.c` | TCP helper 実装 | 新規 |
| `runtime/runtime.h` | 5 つの tcp_* + 2 つの park プロトタイプ | 追加 |
| `runtime/runtime.c` | `rw_init` / `rw_shutdown` で netpoller 呼び出し | 変更 |
| `runtime/Makefile` | net/*.o + uname 分岐 | 変更 |
| `runtime/fiber/sched.h` | netpoller 向け sched API export | 変更 |
| `runtime/fiber/sched.c` | `rw_sched_enqueue_ready` / `rw_sched_current_fiber` を export | 変更 |
| `runtime/fiber/test_netpoller_pipe.c` | C 単体テスト (pipe) | 新規 |
| `runtime/fiber/test_tcp_loopback.c` | C 単体テスト (localhost) | 新規 |
| `.gitignore` | test バイナリ無視 | 追加 |
| `rwc/sema.py` | 5 組込み + spawn 禁止 | 変更 |
| `rwc/irgen.py` | 5 組込み emit | 変更 |
| `tests/test_sema.py` | positive 5 + negative 5 | 追加 |
| `tests/test_e2e_tcp.py` | Python から socket で echo を検証 | 新規 |
| `examples/tcp_echo.rw` | echo server デモ | 新規 |

---

## Task 1: sched API を netpoller 向けに export

netpoller スレッドから fiber を起こすため、sched.c の `enqueue_ready` と `tls_m->current` を外部に公開する。

**Files:**
- Modify: `runtime/fiber/sched.h`
- Modify: `runtime/fiber/sched.c`

- [ ] **Step 1.1: `runtime/fiber/sched.h` に新 API を追加**

ファイル末尾の `#ifdef __cplusplus ... #endif` の **直前** に追加:

```c
/* ---- Exported for the netpoller (runtime/net/netpoller.c) ---- */

/* Move the given fiber handle to the ready queue. Safe to call from
 * any thread (uses the same mutex/cv as spawn). */
void rw_sched_enqueue_ready(rw_fiber_handle *h);

/* Return the fiber handle currently running on this worker thread,
 * or NULL if the calling thread is not a worker (main / netpoller). */
rw_fiber_handle *rw_sched_current_fiber(void);
```

- [ ] **Step 1.2: `runtime/fiber/sched.c` に export 関数を追加**

`enqueue_ready` (sched.c:206 付近、static) のすぐ下に export 関数を追加:

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

- [ ] **Step 1.3: ビルドと既存テストが緑か確認**

```sh
make -C /Users/ryuichi/ghq/github.com/ryuichi1208/rw/runtime clean
make -C /Users/ryuichi/ghq/github.com/ryuichi1208/rw/runtime
```

Expected: 警告なしビルド成功 (`librw.a` 生成)。

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw && uv run pytest -q 2>&1 | tail -5
```

Expected: `131 passed`。

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

## Task 2: netpoller スケルトン (init/shutdown + 共通ヘッダ)

netpoller スレッドの起動・停止だけを実装する。park / wake はまだ動かない。

**Files:**
- Create: `runtime/net/netpoller.h`
- Create: `runtime/net/netpoller.c`
- Create: `runtime/net/netpoller_kqueue.c`
- Create: `runtime/net/netpoller_epoll.c`
- Modify: `runtime/Makefile`
- Modify: `runtime/runtime.h`
- Modify: `runtime/runtime.c`

- [ ] **Step 2.1: `runtime/net/netpoller.h` を新規作成**

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

- [ ] **Step 2.2: `runtime/net/netpoller.c` を新規作成**

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

注: `rw_sched_park_current` は Task 3 で sched.c に追加する。

- [ ] **Step 2.3: `runtime/net/netpoller_kqueue.c` を新規作成**

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

- [ ] **Step 2.4: `runtime/net/netpoller_epoll.c` を新規作成**

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

- [ ] **Step 2.5: `runtime/Makefile` を更新**

`OBJS` 行を更新し、新しい .o を追加:

```makefile
OBJS := runtime.o \
        fiber/fiber.o fiber/sched.o fiber/runq.o fiber/park.o \
        net/netpoller.o net/tcp.o $(NET_PLATFORM_O) \
        $(FIBER_ASM)
```

`UNAME_M` ブロックの **直下** に platform 分岐を追加:

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

各 .o 用のルールを既存ルール (`fiber/park.o`) のすぐ下に追加:

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

- [ ] **Step 2.6: `runtime/runtime.h` にプロトタイプ追加**

`/* List[int] type and ops */` ブロックのすぐ下、`/* spawn (one per return type) */` の上に追加:

```c
/* TCP API (runtime/net/tcp.c). */
int64_t rw_tcp_listen(int64_t port);
int64_t rw_tcp_accept(int64_t listen_fd);
void    rw_tcp_read  (rw_str *out, int64_t fd, int64_t max);
int64_t rw_tcp_write (int64_t fd, rw_str b);
int64_t rw_tcp_close (int64_t fd);
```

- [ ] **Step 2.7: `runtime/runtime.c` の `rw_init` / `rw_shutdown` に呼び出し追加**

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

`runtime.c` 先頭の `#include` ブロックに追加:

```c
#include "net/netpoller.h"
```

- [ ] **Step 2.8: ビルドが緑か確認**

```sh
make -C /Users/ryuichi/ghq/github.com/ryuichi1208/rw/runtime clean
make -C /Users/ryuichi/ghq/github.com/ryuichi1208/rw/runtime
```

Expected: 警告なしビルド成功。`librw.a` に `net/netpoller.o` 等が含まれる。

- [ ] **Step 2.9: 既存テスト緑か確認**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw && uv run pytest -q 2>&1 | tail -5
```

Expected: `131 passed`。netpoller スレッドは init/shutdown のみで実害無し。

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

## Task 3: park / wake 完成 + pipe テスト

netpoller スレッドが既に動いているので、fiber を park する API を完成させ、ready 通知で fiber が起きることを C テストで確認する。

**Files:**
- Modify: `runtime/fiber/sched.h` (`rw_sched_park_current` を追加)
- Modify: `runtime/fiber/sched.c` (`rw_sched_park_current` を実装)
- Create: `runtime/fiber/test_netpoller_pipe.c`
- Modify: `.gitignore`

- [ ] **Step 3.1: `runtime/fiber/sched.h` に `rw_sched_park_current` を追加**

Task 1 で追加した export 群の下に:

```c
/* Mark the current fiber as WAITING and swap out to the scheduler.
 * The fiber will NOT be re-enqueued by the scheduler; someone must
 * call rw_sched_enqueue_ready(handle) to wake it later. Used by the
 * netpoller to park fibers on fd readiness. */
void rw_sched_park_current(void);
```

- [ ] **Step 3.2: `runtime/fiber/sched.c` に `rw_sched_park_current` を実装**

既存 `rw_sched_yield` のすぐ下に追加:

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

worker_main の `if (state == RUNNING) enqueue_ready(g)` 判定は既存のままで、
WAITING の fiber は自動的に re-enqueue されないので意図通り動く。

- [ ] **Step 3.3: ビルドが緑か確認**

```sh
make -C /Users/ryuichi/ghq/github.com/ryuichi1208/rw/runtime
```

Expected: 警告なしビルド成功。

- [ ] **Step 3.4: `runtime/fiber/test_netpoller_pipe.c` を新規作成**

pipe(2) を 2 つの fiber で読み書きして、reader が park → writer が write → reader が起きる挙動を確認:

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

- [ ] **Step 3.5: `.gitignore` に test バイナリを追加**

`runtime/fiber/test_option` の下に追加:

```
runtime/fiber/test_netpoller_pipe
```

- [ ] **Step 3.6: テストをビルドして実行**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw/runtime
cc -O2 -Wall -Wextra -std=c11 -D_GNU_SOURCE -pthread fiber/test_netpoller_pipe.c librw.a -o fiber/test_netpoller_pipe && ./fiber/test_netpoller_pipe
```

Expected: `netpoller pipe test ok`。

タイムアウトする場合は netpoller スレッドが ready 通知を正しく `enqueue_ready` していない (Task 2 の platform 実装か Task 3.2 の park ロジックに問題)。`pkill test_netpoller_pipe` で殺してログを見直す。

- [ ] **Step 3.7: 既存テストも回帰なし**

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

## Task 4: tcp_* helper + localhost loopback テスト

`runtime/net/tcp.c` / `tcp.h` を実装し、localhost で listen → connect → recv/send が動くことを C で確認。

**Files:**
- Create: `runtime/net/tcp.h`
- Create: `runtime/net/tcp.c`
- Create: `runtime/fiber/test_tcp_loopback.c`
- Modify: `.gitignore`

- [ ] **Step 4.1: `runtime/net/tcp.h` を新規作成**

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

- [ ] **Step 4.2: `runtime/net/tcp.c` を新規作成**

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

- [ ] **Step 4.3: `runtime/fiber/test_tcp_loopback.c` を新規作成**

localhost で listen → 別 fiber が connect → recv/send を実施:

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

- [ ] **Step 4.4: `.gitignore` 更新**

`test_netpoller_pipe` の下に追加:

```
runtime/fiber/test_tcp_loopback
```

- [ ] **Step 4.5: ビルド + 実行**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw/runtime
make
cc -O2 -Wall -Wextra -std=c11 -D_GNU_SOURCE -pthread fiber/test_tcp_loopback.c librw.a -o fiber/test_tcp_loopback && ./fiber/test_tcp_loopback
```

Expected: `tcp loopback test ok`。

タイムアウトする場合は `tcp_accept` で park した fiber が起きていない、もしくは `tcp_read` / `tcp_write` の park が動いていない。`netpoller_pipe` が動いているなら netpoller 自体は健全。

- [ ] **Step 4.6: pipe テストも引き続き緑か**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw/runtime
./fiber/test_netpoller_pipe
```

Expected: `netpoller pipe test ok`。

- [ ] **Step 4.7: pytest も緑**

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

## Task 5: rwc に 5 つの組込みを追加 + sema/irgen テスト

**Files:**
- Modify: `rwc/sema.py`
- Modify: `rwc/irgen.py`
- Modify: `tests/test_sema.py`

- [ ] **Step 5.1: Sema に 5 組込みを追加**

`rwc/sema.py` の `list_at_opt` の Sema 直下に追加:

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

- [ ] **Step 5.2: SpawnExpr 禁止リストに 5 つ追加**

`list_at_opt` の禁止分岐の **直下** に追加:

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

- [ ] **Step 5.3: irgen に 5 組込みの emit を追加**

`rwc/irgen.py` の `_declare_runtime` で `rw_list_int_at_opt` の宣言の **直下** に追加:

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

`_emit_call` で `list_at_opt` の分岐の **直下** に追加:

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

- [ ] **Step 5.4: Positive テスト**

`tests/test_sema.py` の末尾に追加:

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

- [ ] **Step 5.5: Negative テスト**

`tests/test_sema.py` の末尾に追加:

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

- [ ] **Step 5.6: pytest を回す**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw && uv run pytest -q 2>&1 | tail -5
```

Expected: 既存 131 + positive 5 + negative 5 = `141 passed`。

- [ ] **Step 5.7: smoke check (IR を見て build できる)**

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
- IR に `declare i64 @"rw_tcp_listen"` と `call i64 @"rw_tcp_close"` などが見える
- ビルド成功
- 実行は `exit=0` (listen → close、即終了)

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

- [ ] **Step 6.1: `examples/tcp_echo.rw` を新規作成**

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

`__PORT__` プレースホルダは e2e でテスト毎に動的な空きポートに置換する。
ユーザが手動で `rwc run examples/tcp_echo.rw` するときは `__PORT__` のままだとパースエラーになるので、**最初から `8080` を埋めておき、e2e は別の tmp .rw をビルドする** 方が手軽。修正:

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

e2e は `examples/tcp_echo.rw` を読み、`tcp_listen(8080)` を `tcp_listen(<random>)` に sed して /tmp に保存、それをビルド+起動する。

- [ ] **Step 6.2: `tests/test_e2e_tcp.py` を新規作成**

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

- [ ] **Step 6.3: 手動 smoke**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
uv run rwc build examples/tcp_echo.rw -o /tmp/tcp_echo
/tmp/tcp_echo &
SERVER_PID=$!
sleep 0.3
echo -n "hello" | nc -w 1 127.0.0.1 8080
kill $SERVER_PID 2>/dev/null
```

Expected: `hello` がそのまま返ってくる。

- [ ] **Step 6.4: pytest を全件回す**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw && uv run pytest -q 2>&1 | tail -5
```

Expected: 既存 141 (Task 5 まで) + e2e_tcp 新規 2 = `143 passed`。

`test_echo_ten_concurrent_connections` が flaky な場合 (CI で `sleep 0.3` が短すぎる等) は `_start_server` の `time.sleep` を 0.5 まで上げる。

- [ ] **Step 6.5: 既存 example の回帰確認**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
RW_WORKERS=1 uv run rwc run examples/option_basic.rw
RW_WORKERS=1 uv run rwc run examples/result_basic.rw
RW_WORKERS=1 uv run rwc run examples/spawn_many.rw
```

Expected: それぞれ `5\n-1`, `5\n0`, `30`。

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

## Task 7: plan ファイル commit

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

| Spec 要求 | カバーするタスク |
|---|---|
| 専用 netpoller pthread (kqueue / epoll) | Task 2 (全 step) |
| `rw_net_park_read/write` | Task 2.2 + 3.2 |
| `rw_set_nonblocking` | Task 2.2 |
| `rw_netpoller_init/shutdown` from `rw_init/shutdown` | Task 2.7 |
| Shutdown wake-up (kqueue EVFILT_USER / epoll eventfd) | Task 2.3 / 2.4 |
| ONESHOT 監視 | Task 2.3 / 2.4 |
| `tcp_listen(port) -> int` | Task 4.2 (runtime) + 5.1 (sema) + 5.3 (irgen) + 5.4 (test) |
| `tcp_accept(int) -> int`、main は blocking / fiber は park | Task 4.2 + 5.1 + 5.3 + 5.4 |
| `tcp_read(int, int) -> Bytes`、len==0 で EOF/error | Task 4.2 + 5.1 + 5.3 + 5.4 |
| `tcp_write(int, Bytes) -> int` | Task 4.2 + 5.1 + 5.3 + 5.4 |
| `tcp_close(int) -> int` | Task 4.2 + 5.1 + 5.3 + 5.4 |
| 5 組込みすべて spawn 禁止 | Task 5.2 + test 5.5 (`test_cannot_spawn_tcp_accept`) |
| C テスト: netpoller pipe | Task 3.4 |
| C テスト: tcp loopback | Task 4.3 |
| e2e: 1 接続 + 10 並列接続 | Task 6.2 |
| `examples/tcp_echo.rw` | Task 6.1 |
| 既存 131 テスト緑、既存 example 回帰なし | Task 2.9 / 3.7 / 4.7 / 6.4 / 6.5 |
| sched.h に enqueue_ready / current_fiber export | Task 1 |
| `rw_sched_park_current` 追加 | Task 3.1 + 3.2 |
| Makefile uname 分岐 | Task 2.5 |

すべての spec 要求にタスクがある。

### Placeholder スキャン

「TBD」「TODO」「(要確認)」「fill in」「Add appropriate」「Similar to Task N」は plan 内 0 件。

### Type consistency

- `rw_netpoller_init` / `_shutdown` / `_platform_init` / `_platform_shutdown` / `_platform_run` / `_register_read` / `_register_write` のシグネチャを Task 2.1 (宣言) と Task 2.2/2.3/2.4 (実装) で完全一致
- `rw_net_park_read` / `_write` / `rw_set_nonblocking` のシグネチャを Task 2.1 / 2.2 / 4.2 で揃って使用
- `rw_sched_enqueue_ready` / `_current_fiber` / `_park_current` を Task 1.1 / 1.2 / 3.1 / 3.2 で揃えて宣言・実装、Task 2.2 / 2.3 / 2.4 / 4.2 で呼び出し
- `rw_tcp_listen` / `_accept` / `_read` / `_write` / `_close` のシグネチャを Task 2.6 (runtime.h) / 4.2 (実装) / 5.3 (irgen 宣言) で完全一致
- `RW_STR_TY` を `tcp_read` の pointer-out shim で使用 (Task 5.3)、既存 string ヘルパと同じ alloca/load パターン
