# 57. Signals and graceful shutdown

Issue: #168

## Motivation

Ctrl-C or SIGTERM killed an rw server at once. Requests in progress were cut
off, and an accept loop had no way to stop: closing the listening handle from
another fiber left the acceptor parked forever, because the kernel silently
drops a kqueue/epoll registration when its fd is closed.

## `signal_wait`

`signal_wait(names: List[string]) -> string` blocks until one of the named
signals arrives and returns its name.

- Names: `"SIGINT"`, `"SIGTERM"`, `"SIGHUP"`, `"SIGQUIT"`, `"SIGUSR1"`,
  `"SIGUSR2"`. If none of the names is recognised, it returns `""` at once.
- The first `signal_wait` that names a signal installs rw's handler for it.
  From then on that signal no longer has its default action (terminating the
  process): it is delivered only through `signal_wait`. Signals never named
  keep their default action.
- Occurrences that arrive while nobody waits are counted and returned by
  later waits, one per call. Several fibers may wait at once, and each
  occurrence goes to exactly one of them.
- It works on a fiber (parks) and on the main thread (blocks).

### Implementation

`runtime/signal.c`:
- The handler is async-signal-safe: it writes the signal number to a
  self-pipe.
- A dedicated thread reads the pipe, increments a pending count under a
  mutex, and wakes every waiter. Fibers wake through `rw_sched_enqueue_ready`;
  main-thread waiters wake through a condvar.
- Each waiter re-checks the counts for its own set, consumes one, or waits
  again.
- A fiber publishes itself and drops the mutex inside its park commit
  (spec 24), so no wake-up can be lost between the check and the park.

## Closing a handle wakes its waiters

`close(h)` now wakes every fiber parked on `h` (read or write, including
`accept`). Their call returns -1, or empty bytes for `read`, with
`last_error()` = `EBADF`:
- The netpoller descriptor (spec 53) is marked closed, and each waiter node
  is flagged and resumed under the descriptor lock. A waiter still arming is
  woken by its own commit.
- Later waits on the closed descriptor fail at once. Creating a new handle
  that reuses the fd number clears the mark. `dial` clears it right after
  `socket(2)`, before it waits for the connect.
- Off-fiber code (the main thread) cannot be woken from `poll(2)` by a close
  in another thread. `rw_net_block` therefore polls in slices of at most
  50 ms and checks the closed mark between slices.
- `accept` on the main thread is now a nonblocking accept plus
  `rw_net_block`, the same as on a fiber, instead of a blocking `accept(2)`.
  Otherwise it could not be interrupted.

Together these give the standard shape for stopping an accept loop:

```python
def stopper(ln: int) -> int:
    signal_wait(["SIGINT", "SIGTERM"])
    close(ln)                      # the accept below returns -1 / EBADF
    return 0

spawn stopper(ln)
while true:
    c = accept(ln)
    if c < 0 and error_name(last_error()) == "EBADF":
        break
    ...
```

## `examples/web.rw`

`serve` handles SIGINT and SIGTERM. It stops accepting (new connections are
refused), waits until every connection in progress has been answered (an
in-flight counter guarded by a `Mutex`), and returns 0. Other accept errors
(such as `EMFILE`) back off for 10 ms instead of ending the loop.

## Tests

`tests/test_e2e_shutdown.py`:
- `close` wakes four parked acceptors with `EBADF`, and a later `accept`
  fails at once.
- Signals sent while nobody waits are queued and returned one per wait; an
  unknown name returns `""`.
- web: after SIGTERM the listener refuses connections, a request already in
  progress still gets its `200`, and the process exits 0. An idle server
  exits 0 on SIGINT.
