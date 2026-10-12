# 53. I/O timeouts

Issue: #162

## Motivation

A fiber parked on the netpoller waits forever. A client that connects and
sends nothing, or sends half a request (slowloris), pins a fiber and its
memory indefinitely. `examples/web.rw` had no defence against this.

## Builtins

| Builtin | Type | Meaning |
| --- | --- | --- |
| `set_read_timeout(h, ms)` | `(int, int) -> void` | bound each `read` / `accept` on `h` |
| `set_write_timeout(h, ms)` | `(int, int) -> void` | bound each `write` / `write_all` on `h` |
| `dial_timeout(addr, ms)` | `(string, int) -> int` | `dial` that gives up connecting after `ms` |

- `ms <= 0` means no timeout, which is the default.
- A timeout bounds one call: from its first wait until it gives up. For
  `write_all` that is the whole call, across all chunks. The value is a
  per-call limit, not an idle timer and not an absolute deadline.
- On timeout:
  - `read` returns empty bytes
  - `accept`, `write`, and `write_all` return -1
  - `dial_timeout` returns -1

  In every case `last_error()` is `ETIMEDOUT` (spec 51).
- Timeouts belong to the handle and are cleared when it is closed with
  `close`, and when a new handle is created (`dial`, `listen`, `accept`,
  `open`, `file_open`, `tcp_listen`, `tcp_accept`), so a reused fd number does
  not inherit them.
- Timeouts apply to sockets. Files and pipes go through the aio pool and
  ignore them.
- `dial_timeout` covers connecting, not name resolution.

## Runtime design

### Per-fd poll descriptors

The netpoller used to register the parked fiber itself as the kqueue/epoll
user data. With a timer that can also wake the fiber, that design allows a
double resume (spec 24's problem again). A second fiber waiting on the same
fd also overwrote the first one's registration, so the first never woke.

Each fd now has a poll descriptor (`rw_pollfd`):
- Allocated lazily in chunks and never freed, so a pointer to it stays valid
  for the kernel and the timer forever.
- Holds a lock and, per direction (read and write), a list of waiters.
- A waiter is a node on the waiting fiber's stack, with an id unique within
  its descriptor.

`rw_net_wait(fd, dir, deadline)`:
1. Under the descriptor lock, links a waiter node.
2. Schedules a timer callback `(pd, id, dir)` if there is a deadline.
3. Parks with a commit (spec 24). Under the lock, the commit stores the
   fiber in the node and registers interest in the netpoller. If the timer
   already marked the node timed out, it wakes the fiber immediately.
4. After resuming, unlinks the node if it is still linked and returns
   whether it timed out.

Who wakes a waiter, always under the descriptor lock:
- **Netpoller event**: wakes every parked waiter in that direction. They retry
  their syscall, so an extra wake costs only one `EAGAIN`. It then re-arms
  interest for waiters that are left (needed for epoll's single per-fd
  registration).
- **Timer**: looks the waiter up by id. If it is parked, the timer unlinks it,
  marks it timed out, and wakes it. If it is still arming, the timer marks it
  and the commit wakes it. If it is gone, the wait already ended and the
  timer does nothing.

Because both paths unlink the node under the lock before waking, a fiber is
resumed at most once per wait. Stale kernel events (a one-shot registration
from an earlier wait) only cause a spurious wake.

### Timers

The timer heap (spec 39) gains callback entries: `rw_timer_call_at(deadline,
fn, arg, tag)` runs `fn` on the timer thread. Timers are never cancelled. A
timer for a finished wait stays in the heap until its deadline and then does
nothing.

### The main thread

Off-fiber code cannot park. `read` / `write` / `accept` / `dial_timeout` on
the main thread wait in `poll(2)` with the remaining time
(`rw_net_block`).

## `examples/web.rw`

`App` gains `timeout_ms` (default 10000). Each connection sets it as both its
read and write timeout. A client that stalls mid-request gets `408 Request
Timeout`; one that sends nothing is closed.

## Tests

- `runtime/fiber/test_net_timeout.c`:
  - a wait times out no earlier than its deadline
  - readiness beats a long deadline
  - 64 socket pairs race random sub-2 ms deadlines against writers for 300
    rounds each, asserting no fiber is resumed while it is already running
  - 8 fibers waiting on one fd all wake
- `tests/test_e2e_timeouts.py`:
  - accept, read, and write_all time out with `ETIMEDOUT`, on the main thread
    and on a fiber
  - normal traffic is unaffected after clearing the timeouts
  - `dial_timeout` to an unroutable address returns quickly
- `tests/test_e2e_web.py`: a partial request head gets 408; an idle
  connection is closed.
