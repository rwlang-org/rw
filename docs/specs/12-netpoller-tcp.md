# rw netpoller + minimal TCP API

## Context

The PR that achieves the roadmap's **final goal**: "you can write an echo server in rw."
It combines the language extensions built so far (string / Bytes / List / Option / Result / match)
with the runtime extension (the M:N scheduler) to deliver a Go-style feel where **a fiber
appears to block on `recv` while kqueue/epoll spins underneath**.

Tools assembled so far:

- M:N scheduler (#90): pthread workers + work-stealing + inter-fiber wait list
- string / Bytes (#91, #92): representation of byte data
- List[int] (#93): can hold an array of client fds
- Option / Result (#94, #95): the foundation for error representation (not used this time)

What remains:

- **nonblocking I/O + fd readiness monitoring**: `kqueue(2)` / `epoll(7)`
- **park / wake a fiber tied to fd monitoring**: the netpoller thread
- **TCP API from the rw language**: `tcp_listen` / `tcp_accept` / `tcp_read` / `tcp_write` / `tcp_close`
  *(Note: `tcp_read` / `tcp_write` / `tcp_close` were later consolidated into `read` / `write` / `close`, described below.)*

Roadmap:

1. string `len` / `==` / `+` (#91)
2. Bytes type (#92)
3. List[int] (#93)
4a. Option[int] + match (#94)
4b. Result[int, int] (#95)
4c. (future) true generics
5. **this sub-project**: netpoller + TCP API -> echo server

## Goals

- Add a netpoller thread to the runtime (kqueue/epoll, one dedicated pthread)
- An internal API to park / wake a fiber on fd readiness (`rw_net_park_read/write`)
- TCP builtins in the rw language:
  - `tcp_listen(port: int) -> int`
  - `tcp_accept(listen_fd: int) -> int`
  - `read(fd: int, max: int) -> Bytes`  *(formerly `tcp_read`, consolidated as generic fd)*
  - `write(fd: int, b: Bytes) -> int`   *(formerly `tcp_write`, consolidated as generic fd)*
  - `close(fd: int) -> int`             *(formerly `tcp_close`, consolidated as generic fd)*
- `examples/tcp_echo.rw` works (verified from Python with 1 connection + 10 concurrent connections)
- No regressions to the existing public ABI or any existing example

## Non-Goals

- IPv6 / UDP / TLS / Unix domain socket
- Host specification for `tcp_listen` (fixed to IPv4 `0.0.0.0`)
- Detailed errors (an errno-retrieval API)
- Graceful shutdown / SIGINT handler (assume the process is killed with Ctrl-C)
- Automatic retry of partial writes (`write` only returns the number of bytes actually
  written; writing everything out is the user code's responsibility)
- Multiple fibers parking on the same fd simultaneously (protocol convention: 1 fd = 1 fiber)
- Error representation via the Result type (`read` returns `Bytes` and uses len==0 to represent
  both EOF and error)
- A C10k connection-count benchmark (minimal e2e only: 1 successful connection + 10 concurrent connections)
- File I/O / pipe / TTY (socket only this time; other fd kinds, even those monitorable by
  kqueue/epoll, are out of scope)
- Automatic raising of the fd limit (assume `ulimit -n` is raised in the shell; the runtime
  does not call `setrlimit`)
- Turning the main thread into a fiber (main stays a standalone thread that is neither a worker
  nor a fiber; calling `tcp_accept` from main does a blocking accept that sleeps in the kernel)

## Design

### System overview diagram

```
                                 +---------------------+
                                 |  netpoller thread   |
                                 |  kevent / epoll_wait|
                                 +----------+----------+
                                            |
                                            | enqueue_ready(fiber)
                                            v
+--------+   spawn    +------------------------------------+
|  main  | ---------> | ready queue (global + per-P rings) |
| thread |   accept   +-------+----------+-------+---------+
+--------+                    |          |       |
  (kernel sleep               |          |       |
   in blocking                v          v       v
   accept)                +--------+ +--------+ +--------+
                          | wkr M1 | | wkr M2 | | wkr Mn |
                          +---+----+ +---+----+ +---+----+
                              |          |          |
                              v          v          v
                          fiber execution (read / write / ...)
                              |
                              | EAGAIN → rw_net_park_read(fd)
                              |   ↓
                              | register with netpoller, fiber WAITING, worker M moves to next fiber
                              |
                              | (when the netpoller detects ready, control returns to the arrow above)
```

Thread counts:

| Kind | Count | Role |
|---|---|---|
| main | 1 | Runs `rw_user_main`, blocking-sleeps in `tcp_accept` |
| worker M | `sysconf(_SC_NPROCESSORS_ONLN)`, capped at 64 | Runs fibers via `find_runnable` + `rw_fiber_swap` |
| netpoller | 1 | Watches fd readiness with `kevent` / `epoll_wait` and `enqueue_ready`s the corresponding fiber |

= **`nproc + 2` threads** (independent of the connection count).

### How the netpoller thread works

```c
void *rw_netpoller_main(void *arg) {
    while (!atomic_load(&g_netpoller_shutdown)) {
        // Fetch up to 128 events per syscall (the kernel sleeps for us)
        int n = kevent(g_kq, NULL, 0, events, 128, NULL);
        for (int i = 0; i < n; i++) {
            rw_fiber_handle *f = (rw_fiber_handle *)events[i].udata;
            // 1 fd = 1 fiber (protocol convention); ONESHOT means no re-registration
            enqueue_ready(f);   // Reuse the existing M:N scheduler
        }
    }
    return NULL;
}
```

Why ONESHOT mode (`EV_ONESHOT` / `EPOLLONESHOT`) is used:

- Once readiness is signaled, the kernel automatically removes the fd from monitoring
- To park the same fd again, the fiber calls `rw_net_park_*` once more to re-register
- Minimizes the race window where "multiple fibers park before the fiber closes"

### park / wake API

```c
// runtime/net/netpoller.h
void rw_netpoller_init(void);
void rw_netpoller_shutdown(void);

int  rw_set_nonblocking(int fd);    // Set O_NONBLOCK (idempotent)

// Block current fiber until fd is readable / writable.
// Must be called from a fiber (tls_m != NULL).
void rw_net_park_read(int fd);
void rw_net_park_write(int fd);
```

Implementation of `rw_net_park_read`:

```c
void rw_net_park_read(int fd) {
    rw_M *m = tls_m;
    rw_fiber_handle *me = m->current;
    atomic_store_explicit(&me->state, RW_FIBER_WAITING, memory_order_relaxed);
    register_for_read(fd, me);     // Register with kqueue / epoll using EV_ONESHOT
    rw_fiber_swap(&me->ctx, &m->sched_ctx);
    // By the time control returns here, the netpoller has enqueue_ready'd this fiber
}
```

Platform-specific implementations of `register_for_read`:

```c
// netpoller_kqueue.c
static void register_for_read(int fd, rw_fiber_handle *f) {
    struct kevent kev;
    EV_SET(&kev, fd, EVFILT_READ, EV_ADD | EV_ONESHOT, 0, 0, f);
    kevent(g_kq, &kev, 1, NULL, 0, NULL);
}

// netpoller_epoll.c
static void register_for_read(int fd, rw_fiber_handle *f) {
    struct epoll_event ev = {
        .events = EPOLLIN | EPOLLONESHOT,
        .data.ptr = f,
    };
    // MOD if already registered, ADD if not. Try both and accept whichever succeeds.
    if (epoll_ctl(g_ep, EPOLL_CTL_MOD, fd, &ev) != 0) {
        epoll_ctl(g_ep, EPOLL_CTL_ADD, fd, &ev);
    }
}
```

epoll behaves differently for ADD vs. MOD, so try both. For kqueue, EV_ADD alone suffices.

### Implementation pattern for the tcp_* helpers

> **Note (#33):** `rw_tcp_read` / `rw_tcp_write` / `rw_tcp_close` have been consolidated into
> `rw_read` / `rw_write` / `rw_close` in `runtime/io.c` and removed. The following is the
> reference implementation from design time.

```c
// runtime/net/tcp.c (historical reference implementation — now consolidated into runtime/io.c)

int64_t rw_tcp_listen(int64_t port) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) return -1;
    int one = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    struct sockaddr_in addr = {
        .sin_family = AF_INET,
        .sin_addr.s_addr = htonl(INADDR_ANY),
        .sin_port = htons((uint16_t)port),
    };
    if (bind(fd, (void*)&addr, sizeof(addr)) < 0) { close(fd); return -1; }
    if (listen(fd, 128) < 0)                       { close(fd); return -1; }
    return fd;
    // Note: the listen fd stays blocking, so the main thread can kernel-sleep
    // while it waits in tcp_accept. It is switched to nonblocking on the first
    // tcp_accept invoked from a fiber.
}

int64_t rw_tcp_accept(int64_t listen_fd) {
    if (tls_m == NULL) {
        // main thread: blocking accept (kernel puts main to sleep; worker M /
        // netpoller run on separate threads, so they are unaffected)
        int c = accept(listen_fd, NULL, NULL);
        if (c < 0) return -1;
        rw_set_nonblocking(c);
        return c;
    }
    // in a fiber: nonblocking accept + netpoller park
    rw_set_nonblocking(listen_fd);   // idempotent (effect only on first call)
    for (;;) {
        int c = accept(listen_fd, NULL, NULL);
        if (c >= 0) { rw_set_nonblocking(c); return c; }
        if (errno == EAGAIN || errno == EWOULDBLOCK) {
            rw_net_park_read(listen_fd);
            continue;
        }
        return -1;
    }
}

void rw_tcp_read(rw_str *out, int64_t fd, int64_t max) {
    if (max <= 0) { out->len = 0; out->ptr = NULL; return; }
    char *buf = malloc((size_t)max);
    if (!buf)     { out->len = 0; out->ptr = NULL; return; }
    for (;;) {
        ssize_t n = recv((int)fd, buf, (size_t)max, 0);
        if (n > 0)  { out->len = n; out->ptr = buf; return; }
        if (n == 0) { free(buf); out->len = 0; out->ptr = NULL; return; }
        if (errno == EAGAIN || errno == EWOULDBLOCK) {
            if (tls_m) { rw_net_park_read((int)fd); continue; }
            // main thread (it is unusual for main to read a nonblocking fd, but
            // just in case) treats EAGAIN as an error
            free(buf); out->len = 0; out->ptr = NULL; return;
        }
        free(buf); out->len = 0; out->ptr = NULL; return;
    }
}

int64_t rw_tcp_write(int64_t fd, rw_str b) {
    if (b.len <= 0) return 0;
    for (;;) {
        ssize_t n = send((int)fd, b.ptr, (size_t)b.len, 0);
        if (n >= 0) return n;
        if (errno == EAGAIN || errno == EWOULDBLOCK) {
            if (tls_m) { rw_net_park_write((int)fd); continue; }
            return -1;
        }
        return -1;
    }
}

int64_t rw_tcp_close(int64_t fd) {
    return close((int)fd);
}
```

### Builtins on the rw language side

> **Update (#33 / [15-file-io](15-file-io.md)):** `tcp_read` / `tcp_write` /
> `tcp_close` have been consolidated into the generic-fd `read` / `write` / `close`.
> Reading, writing, and closing a socket use these (implemented in `runtime/io.c`). The
> socket-specific `tcp_listen` / `tcp_accept` remain as-is. See
> [`15-file-io.md`](15-file-io.md) for details.

Add the builtin functions to Sema / irgen. Signatures:

```
tcp_listen(int)     -> int
tcp_accept(int)     -> int
read(int, int)      -> Bytes   # formerly tcp_read
write(int, Bytes)   -> int     # formerly tcp_write
close(int)          -> int     # formerly tcp_close
```

All forbid `spawn` (existing rule: builtins cannot be spawned).

irgen follows the existing patterns:
- `read` is **pointer-out** (a 16-byte return value, but aligned with the alloca + load pattern)
- `write` passes Bytes (16 bytes) by value
- The remaining three are scalar

### File structure

```
runtime/net/                          (new directory)
├── netpoller.h                       (shared API declarations)
├── netpoller.c                       (init / shutdown / park / shared logic)
├── netpoller_kqueue.c                (macOS-specific: kevent-based)
├── netpoller_epoll.c                 (Linux-specific: epoll-based)
├── tcp.h
└── tcp.c

runtime/runtime.h                     (add tcp_listen/tcp_accept + read/write/close/file_open + the two park prototypes)
runtime/runtime.c                     (add netpoller calls to rw_init / rw_shutdown)
runtime/Makefile                      (net/*.o + uname branching)

runtime/fiber/test_netpoller_pipe.c   (new C test)
runtime/fiber/test_tcp_loopback.c     (new C test)

rwc/sema.py                           (tcp_listen/tcp_accept/read/write/close + spawn prohibition)
rwc/irgen.py                          (emit tcp_listen/tcp_accept/read/write/close)

examples/tcp_echo.rw                  (echo server demo)
examples/tcp_echo.rw.expected         (not used this time, described below)

tests/test_e2e_tcp.py                 (connect via socket from Python and verify echo)

docs/specs/12-netpoller-tcp.md        (this file)
docs/plans/2026-05-23-netpoller-tcp.md (created with writing-plans)
```

### e2e test strategy

The usual "compare stdout against `.expected`" approach of `tests/test_e2e.py` does not fit an
echo server (an echo server runs an infinite loop and produces no stdout).

New file `tests/test_e2e_tcp.py`:

```python
import socket, subprocess, time, signal, os

def _free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port

def _start_server(port):
    # Build and launch examples/tcp_echo.rw
    # The example side also accepts the port via the RW_ECHO_PORT env var
    # (or generate a tmp .rw with the tcp_listen argument baked in)
    ...

def test_echo_single():
    port = _free_port()
    proc = _start_server(port)
    try:
        time.sleep(0.2)  # wait for the server to start
        s = socket.create_connection(('127.0.0.1', port))
        s.sendall(b"hello\n")
        assert s.recv(1024) == b"hello\n"
        s.close()
    finally:
        proc.terminate(); proc.wait()

def test_echo_concurrent_10():
    port = _free_port()
    proc = _start_server(port)
    try:
        time.sleep(0.2)
        socks = [socket.create_connection(('127.0.0.1', port)) for _ in range(10)]
        for i, s in enumerate(socks):
            s.sendall(f"client-{i}\n".encode())
        for i, s in enumerate(socks):
            assert s.recv(1024) == f"client-{i}\n".encode()
        for s in socks:
            s.close()
    finally:
        proc.terminate(); proc.wait()
```

The port is reserved as a free one via `socket.bind(0)`. Since the example's port cannot be
hardcoded, the final form of `tcp_echo.rw` needs to **read the port from `argv` or an
environment variable**.

However, rw does not yet have an argv / env reading API -> instead, **text-substitute
`examples/tcp_echo.rw` for each e2e run** (a simple template that only rewrites the port
number). This is the lowest-cost approach.

### Commit structure (1 PR, 6 commits)

1. **runtime: netpoller skeleton (init/shutdown only)**
   - `runtime/net/netpoller.{c,h}`, `netpoller_kqueue.c`, `netpoller_epoll.c`
   - `rw_netpoller_init` starts the pthread, `rw_netpoller_shutdown` joins it
   - Add `net/*.o` to the Makefile, branch on kqueue/epoll via `uname -s`
   - Add calls to `rw_init` / `rw_shutdown` in `runtime/runtime.c`
   - C test `test_netpoller_init.c` (run init/shutdown 10 times)

2. **runtime: park/wake and pipe test**
   - Real implementation of `rw_set_nonblocking` and `rw_net_park_read/write`
   - The netpoller thread's main loop
   - C test `test_netpoller_pipe.c`: create two pipes; the reader fiber parks via
     `rw_net_park_read`, the writer fiber writes, and check that the reader wakes up

3. **runtime: tcp_* helpers**
   - `runtime/net/tcp.{c,h}`
   - Implementation of the 5 functions
   - C test `test_tcp_loopback.c`: listen -> connect -> recv/send on localhost

4. **rwc: builtins in sema + irgen**
   - Branches in `_check_call` + spawn prohibition
   - Branches in `_emit_call` (`read` uses the pointer-out shim)
   - positive / negative tests (argument-type errors, spawn prohibition)

5. **examples + e2e**
   - `examples/tcp_echo.rw`
   - `tests/test_e2e_tcp.py` (single + concurrent 10)
   - Not added to `tests/test_e2e.py` (an echo server does not fit stdout comparison, so a
     separate e2e file)

6. **commit the plan file**

### Verification

```sh
# runtime alone
make -C runtime clean && make -C runtime
cd runtime
cc -O2 -Wall -Wextra -std=c11 -D_GNU_SOURCE -pthread fiber/test_netpoller_pipe.c librw.a -o fiber/test_netpoller_pipe && ./fiber/test_netpoller_pipe
cc -O2 -Wall -Wextra -std=c11 -D_GNU_SOURCE -pthread fiber/test_tcp_loopback.c librw.a -o fiber/test_tcp_loopback && ./fiber/test_tcp_loopback

# pytest
cd ..
uv run pytest -q
# Expected: existing 131 + 10 new sema + 2 new e2e_tcp = 143 total

# manual echo check
RW_ECHO_PORT=18080 uv run rwc run examples/tcp_echo.rw &
sleep 0.2
nc 127.0.0.1 18080 <<< 'hello'   # => hello
kill %1
```

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| API differences between kqueue and epoll leak through | Shared API in netpoller.h; fully separate `netpoller_kqueue.c` / `netpoller_epoll.c` with `#if defined(__APPLE__)`. Verify the epoll path in Linux CI and the kqueue path locally on macOS |
| The ONESHOT registration becomes garbage if a fiber is closed right after parking | ONESHOT is auto-removed by the kernel. Even if kevent returns ready after close, the fiber pointer is not invalid (the fiber handle lives until join). Only a double-wake risk, with no real harm |
| Ctrl-C arrives while the main thread sleeps in tcp_accept | The OS wakes main with SIGINT, accept returns with EINTR -> rw_tcp_accept returns -1 and the user code breaks out of the loop. This is the current spec behavior |
| Port collision when reserving an ephemeral port in CI | Take a free port on the Python side with `socket.bind(0)` and pass it to rw via env |
| Infinite `tcp_accept` retry loop on connection failure | If accept returns an error other than EAGAIN (such as ECONNABORTED), return -1. The user code is responsible for breaking out with `if c < 0: break` |
| The netpoller thread stays asleep in kevent and never notices the shutdown signal | On shutdown, set `g_netpoller_shutdown = 1` and wake the netpoller with a self-pipe or eventfd (EVFILT_USER for kqueue, eventfd for epoll) |
| The existing e2e (`test_e2e.py`) is fine since it does not run an infinite-loop server | Do not add tcp_echo to the parametrize list; handle it in the new `test_e2e_tcp.py` |
| Whether to call MOD or ADD for `epoll_ctl` on Linux | On ADD failure (EEXIST) fall back to MOD, or vice versa. Absorb by trying both |
| accept returns EMFILE for connections beyond the fd limit (`ulimit -n`) | Treat as an error (return `-1`). Handle in user code. The spec makes the "assume ulimit -n is raised" assumption explicit |
