# 51. I/O error reporting: `last_error` / `error_name` / `error_message`

Issue: #160

## Motivation

Every I/O builtin reports failure as -1, or as empty bytes for `read`. A
program cannot tell a refused connection from an address already in use, and
cannot tell EOF from a read error.

## Choice

Option B from #160: errno-style reporting, stored on the fiber. Returning
`Result` from every builtin (option A) would break all existing callers and
make simple loops verbose. A `Result`-returning layer can be added on top
later.

## Builtins

| Builtin | Type | Meaning |
| --- | --- | --- |
| `last_error()` | `() -> int` | outcome of this fiber's most recent I/O builtin: 0 = success, otherwise an errno value |
| `error_name(e)` | `(int) -> string` | symbolic name: `"ECONNREFUSED"`, `"EPIPE"`, …; `""` for 0; `"E<n>"` if unknown |
| `error_message(e)` | `(int) -> string` | human-readable text (`strerror`); `""` for 0 |

errno numbers differ between Linux and macOS, so programs compare names:

```python
c = dial(addr)
if c < 0 and error_name(last_error()) == "ECONNREFUSED":
    ...
b = read(fd, 4096)
if len(b) == 0 and last_error() != 0:
    print(f"read failed: {error_message(last_error())}")
```

## Rules

- These builtins set `last_error` on every call, to 0 on success:
  - `read`, `write`, `close`
  - `open`, `file_open`
  - `dial`, `listen`, `accept`
  - `tcp_listen`, `tcp_accept`

  Other builtins leave it unchanged.
- `read` at EOF is a success: it returns empty bytes and sets 0.
- A malformed address (`dial`, `listen`, `open`) and an unsupported `open`
  mode are `EINVAL`.
- A name-resolution failure is `ENOHOST` ("host not found"). It is rw's own
  code (10001), outside the errno range.
- Named codes:
  - EPERM, ENOENT, EINTR, EIO, EBADF, EAGAIN, ENOMEM, EACCES, EEXIST
  - ENOTDIR, EISDIR, EINVAL, ENFILE, EMFILE, ENOSPC, EPIPE, ENAMETOOLONG
  - ENOTSOCK, EAFNOSUPPORT, EADDRINUSE, EADDRNOTAVAIL
  - ENETDOWN, ENETUNREACH, ECONNABORTED, ECONNRESET, ENOTCONN
  - ETIMEDOUT, ECONNREFUSED, EHOSTUNREACH, EINPROGRESS, ENOHOST
  - ENOTEMPTY, EXDEV, ELOOP, EROFS, EBUSY (added with spec 59)

## Per-fiber storage

The value lives in the fiber handle, not in thread-local storage. A fiber
that parks can resume on another M, and its error must follow it. Off-fiber
code (the main thread) uses a thread-local slot.

The runtime code that can park never touches `errno` directly. It uses
`rw_errno`, which goes through `rw_errno_ptr()` defined in another translation
unit. glibc declares `__errno_location` const, so without this the compiler
may reuse the errno address computed before a park and then read the old
thread's errno after the fiber migrates.

Errors on the aio pool (offloaded file and pipe I/O, name resolution) are
captured on the pool thread and handed back in the task.

## SIGPIPE

`rw_init` ignores `SIGPIPE`, so a write to a peer that closed returns -1 with
`EPIPE` (or `ECONNRESET`) instead of killing the process. On macOS, sockets
from `dial` / `accept` also set `SO_NOSIGPIPE`. A side effect: `print` to a
closed stdout pipe (`rw-prog | head`) no longer terminates the program. Its
writes fail silently.

## Tests

- `examples/io_errors.rw`: covers EADDRINUSE, ECONNREFUSED, EINVAL, ENOHOST,
  ENOENT, EOF vs EBADF.
- `tests/test_e2e_io_errors.py`:
  - writes to a peer that closed: EPIPE / ECONNRESET, and the process survives
  - 120 fibers each fail differently, park, and still see their own error

## Update

`set_last_error(e: int) -> void` sets `last_error()`. Library code uses it
to restore an error after a cleanup `close` reset it (stdlib `fs` and
`process`).
