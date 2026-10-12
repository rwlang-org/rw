# 61. `process`: child processes

Issue: #42

## Module

`stdlib/process.rw` (spec 54):

```python
import process

match process.run(["sh", "-c", "sort"], "b\na\n"):
    case Some(out):
        print(out.status)        # 0
        print(out.stdout)        # "a\nb\n"
    case None:
        print(error_name(last_error()))   # ENOENT when the command is missing
```

| | |
|---|---|
| `process.run(argv, input = "") -> Option[process.Output]` | run to completion; `Output(status, stdout, stderr)` |
| `process.start(argv) -> Option[process.Process]` | `Process(pid, stdin, stdout, stderr)`: the handles are the parent's pipe ends, used with `read` / `write` / `close` |
| `p.wait() -> int` | closes `stdin` if still open, then waits: the exit status, `128 + N` if the child was killed by signal N, or -1 |
| `p.kill(sig = "SIGTERM") -> bool` | send a signal by name (SIGTERM, SIGKILL, SIGINT, SIGHUP, SIGQUIT, SIGUSR1, SIGUSR2, SIGSTOP, SIGCONT) |

`argv[0]` is looked up in `PATH`. There is no shell: use `["sh", "-c", ...]`
explicitly. The child inherits the environment and working directory.

`run` feeds `input` and reads stderr in two extra fibers while it reads
stdout. A child that writes a lot before consuming all its input therefore
cannot deadlock against the parent.

## Builtins

| Builtin | Result |
|---|---|
| `proc_start(argv: List[string]) -> List[int]` | `[pid, stdin, stdout, stderr]`, or `[]` with `last_error` (`EINVAL` for an empty argv or a NUL byte, `ENOENT` for a missing command) |
| `proc_wait(pid) -> int` | as `Process.wait`; `waitpid` runs on the aio pool |
| `proc_kill(pid, sig) -> int` | 0, or -1 with `last_error` (`EINVAL` for an unknown name) |

## Runtime

- `posix_spawnp` (safe in a multi-threaded process, unlike `fork`) with
  three pipes dup'd onto 0/1/2.
- Every pipe end is `FD_CLOEXEC`, and pipe creation plus spawning hold a
  mutex. A child started by one fiber therefore never inherits the pipes of
  another child being set up concurrently.
- The child starts with the default disposition for SIGPIPE and the
  signals `signal_wait` may have claimed (spec 57), and with an empty signal
  mask. Otherwise rw's ignored SIGPIPE (spec 51) would leak into it and
  `yes | head` would never end.
- The parent's ends are nonblocking and their netpoller state is reset
  (spec 57: a recycled fd number must not inherit a closed mark).

### Nonblocking pipes use the netpoller

Spec 25 sent every non-socket fd to the aio pool. For pipes that pump into
each other this deadlocks: with 4 pool threads, five concurrent `run`s park
15 blocking reads and writes, and a read can wait forever for a write
queued behind it. `io.c` now routes a FIFO that is in nonblocking mode
through the netpoller, like a socket (kqueue and epoll both support pipes).
Pipes rw creates are nonblocking. Inherited ones (a blocking stdin) and
regular files still use the pool.

## Tests

- `tests/test_e2e_process.py`:
  - 1 MiB through `cat` plus 1 MiB on stderr, from the main thread and from
    four concurrent fibers
  - line-by-line streaming with `start`
  - `128 + 9` for a child that kills itself, kill by name, unknown signal
    names, and an empty argv
  - `yes | head` ends (default SIGPIPE in the child)
- `examples/process_basic.rw` (expected output).
