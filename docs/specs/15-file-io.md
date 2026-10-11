# rw file I/O and fd-generic `read` / `write` / `close`

## Context

rw is a small language whose compiler, runtime, examples, and tests grow
together. It has provided TCP sockets ([[12-netpoller-tcp]]) via the built-ins
`tcp_listen` / `tcp_accept` / `tcp_read` / `tcp_write` / `tcp_close`. However, as
noted in issue #33 (stdlib: file I/O), there is still no way to open a file and
read/write it.

Revisiting the TCP built-ins here, `tcp_read` / `tcp_write` / `tcp_close` are in
fact **operations that are not socket-specific**. On Unix, `read(2)` / `write(2)`
/ `close(2)` are generic system calls usable on any fd such as files, sockets,
pipes, and standard I/O; the only socket-specific parts are `tcp_listen` /
`tcp_accept` (and establishing the connection).

Therefore this sub-project **removes `tcp_read` / `tcp_write` / `tcp_close` and
unifies them into the fd-generic `read` / `write` / `close`**, and adds
`file_open` to open a file. With this, the writer works in the Unix semantics of
"opening is source-specific, reading/writing/closing is common", so both TCP and
files can be written with the same `read` / `write` / `close`.

```
open (source-specific)        read/write/close (common)
─────────────────            ──────────────────────
tcp_listen(port) -> fd
tcp_accept(lfd)  -> fd  ──┐
file_open(path, mode) -> fd ─┤──→  read(fd, n) -> Bytes
                          │        write(fd, b) -> int
                          └──→     close(fd) -> int
```

## Goals

- Introduce fd-generic built-ins:
  - `read(fd: int, max: int) -> Bytes` — read at most max bytes from fd
  - `write(fd: int, b: Bytes) -> int` — write to fd. Returns the number of bytes
    written
  - `close(fd: int) -> int` — close fd. 0 on success / negative on failure
- Add a built-in to open a file:
  - `file_open(path: string, mode: string) -> int` — convert `"r"` / `"w"` /
    `"a"` into `open(2)` flags. On failure, a negative fd
- Internally in the runtime, use `read(2)` / `write(2)`, and on `EAGAIN` park on
  the netpoller if on a fiber. **A socket fd (non-blocking) is parked, and a file
  fd (which does not emit EAGAIN) becomes a synchronous read as-is** — handling
  both without writing kind-detection branch code (the core of this design)
- `tcp_listen` / `tcp_accept` are kept as-is (they are socket-specific)
- Rewrite existing TCP examples, tests, and specs to `read` / `write` / `close`
  (a breaking, full unification)

## Non-Goals

- Directory operations / path operations (`mkdir` / `readdir` / path join, etc.)
  — separately in #43
- `seek` / `tell` / `truncate` / `stat` and other file-position/metadata
  operations
- Buffering (issue a syscall every time)
- Text / binary distinction — `read` always returns `Bytes`, and stringification
  is delegated to the existing `str_from_bytes`
- Specifying file permissions — files created by `open` are fixed at `0644`
- Renaming `tcp_listen` / `tcp_accept` (they remain socket-specific)

## Built-in functions (language side)

| Function | Signature | Description |
|---|---|---|
| `file_open` | `(path: string, mode: string) -> int` | `"r"`→`O_RDONLY`, `"w"`→`O_WRONLY\|O_CREAT\|O_TRUNC`, `"a"`→`O_WRONLY\|O_CREAT\|O_APPEND`. Invalid mode / open failure is a negative fd |
| `read` | `(fd: int, max: int) -> Bytes` | Read at most max bytes. EOF/error is a Bytes with len=0 |
| `write` | `(fd: int, b: Bytes) -> int` | Number of bytes written. Error is negative |
| `close` | `(fd: int) -> int` | 0 on success / negative on failure |

The Bytes ABI of `read` / `write` is identical to the old `tcp_read` /
`tcp_write` (`{i64 len, i8* ptr}` passed by sret / by value), so the emit logic
of irgen can be reused. The convention of returning a negative value on failure
matches the existing `tcp_*`.

## Runtime (C) design

Place fd-generic helpers in `runtime/runtime.c` (or a new `runtime/io.c`):

- `rw_read(rw_str *out, int64_t fd, int64_t max)` — **`read(2)`**, not `recv`.
  Branch on the return value:
  - `n > 0`: len=n / ptr=buf into `out`
  - `n == 0`: EOF. len=0 / ptr=NULL
  - `n < 0` and `errno == EAGAIN/EWOULDBLOCK`: if on a fiber,
    `rw_net_park_read(fd)` and continue; otherwise return with len=0
  - Other errors: return with len=0
- `rw_write(int64_t fd, rw_str b)` — **`write(2)`**, not `send`. On `EAGAIN`, if
  on a fiber, `rw_net_park_write(fd)` and continue. Returns the number of bytes
  written
- `rw_close(int64_t fd)` — `close(2)`
- `rw_file_open(rw_str path, rw_str mode)` — copy path into a NUL-terminated
  buffer, convert the mode string into `O_*` flags, and `open(path, flags,
  0644)`. A negative fd indicates failure

### Why both are handled without branching

Socket fds are set to **non-blocking** by `tcp_accept` etc. (for the existing
netpoller integration). A non-blocking socket returns `EAGAIN` when there is no
data to read, so the above logic parks on the netpoller and yields the fiber. On
the other hand, **a regular file fd does not return `EAGAIN` on `read(2)`**; the
kernel blocks until the data is ready and then completes (disk I/O cannot be
made non-blocking). Therefore the same `rw_read` code behaves correctly as a
park for sockets and as a synchronous read for files. There is no branch that
determines the fd kind via `fstat`.

Delete the existing `rw_tcp_read` / `rw_tcp_write` / `rw_tcp_close` in `tcp.c`,
and consolidate/delegate their bodies into `rw_read` / `rw_write` / `rw_close`.

## Layers touched

| Layer | File | Change |
|---|---|---|
| Lexer | `rwc/lexer.py` | **Unchanged** (all are ordinary function calls) |
| Parser | `rwc/parser.py` | **Unchanged** |
| AST | `rwc/ast_nodes.py` | **Unchanged** (expressed as `Call`) |
| Sema | `rwc/sema.py` | Add `file_open` / `read` / `write` / `close` to the built-ins. Remove the `tcp_read` / `tcp_write` / `tcp_close` branches (in 2 places: the spawn rejection list and `_check_call`). `tcp_listen` / `tcp_accept` kept as-is |
| irgen | `rwc/irgen.py` | Declare `rw_read` / `rw_write` / `rw_close` / `rw_file_open`. Replace `tcp_read` etc. in `_emit_call` with `read` etc. (Bytes ABI reused) |
| Runtime | `runtime/runtime.c` and others, `runtime/net/tcp.c` | Implement `rw_read` / `rw_write` / `rw_close` / `rw_file_open`. Delete/delegate `rw_tcp_read` / `rw_tcp_write` / `rw_tcp_close` |
| Examples | `examples/file_io.rw` (+ `.expected`) new. Rewrite `tcp_echo.rw` / `tcp_chat.rw` | round-trip example + existing TCP switched to `read`/`write`/`close` |
| Tests | `tests/test_e2e.py` / `test_e2e_tcp.py` / `test_sema.py` / `test_irgen.py` | Add `file_io` to parametrize, rewrite TCP tests, add sema/irgen unit tests |

Against the "up to 4 layers per PR" of `incremental-language-extensions`, this PR
centers on sema / irgen / runtime + examples. Since lexer / parser / AST are
unchanged, the layer count stays within bounds. However, because it includes a
breaking rewrite of TCP, the commits are split to make the scope of impact
explicit.

## Verification

```sh
make -C runtime
uv run pytest -v                       # all green (including rewritten TCP tests)
uv run rwc run examples/file_io.rw     # round-trip output matches .expected
uv run rwc run examples/tcp_echo.rw    # TCP echo works after the rewrite too
```

- e2e (round-trip): a self-contained example of `file_open(path, "w")` →
  `write` → `close` → `file_open(path, "r")` → `read` → `print` that exercises
  both the write and read paths
- unit (sema): argument types/counts of `file_open`, types of
  `read`/`write`/`close` (`read` returns Bytes, the 2nd argument of `write` is
  Bytes, etc.); an invalid mode is a runtime matter (negative fd), so it is
  confirmed in e2e/manually rather than in sema
- unit (irgen): calls to `rw_read` / `rw_write` / `rw_file_open` appear in the
  generated IR
- TCP regression: pass `test_e2e_tcp.py` with `read`/`write`/`close`

## Risks and mitigations

- **Breaking change to TCP**: identify and rewrite all existing examples, tests,
  and specs that use `tcp_read`/`tcp_write`/`tcp_close` (comprehensively via
  grep). Split the commits into "runtime consolidation", "sema/irgen switch",
  "example/test rewrite", and "add file_open", and guarantee TCP regression in
  e2e
- **Concern about parking a file fd on the netpoller**: regular files do not
  return `EAGAIN`, so they do not enter the park path. Even if a special fd
  returns EAGAIN, it falls back to synchronous outside a fiber
- **NUL termination of path**: since rw's string is `{len, ptr}` with no NUL
  termination guarantee, `rw_file_open` copies the path and appends NUL
  termination
- **Invalid mode string value**: anything other than `"r"`/`"w"`/`"a"` returns a
  negative fd (no trap). The caller checks for fd < 0
- **The "while we're at it" temptation**: do not touch seek / dir operations /
  buffering (Non-Goals). Do not rename `tcp_listen`/`tcp_accept` either
