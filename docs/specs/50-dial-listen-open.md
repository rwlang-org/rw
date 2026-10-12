# 50. `dial` / `listen` / `accept` / `open`: one I/O API for every kind

Issues: #175 (RFC), #159 (TCP client), #163 (listen address, local/peer address)

## Motivation

`read` / `write` / `close` already work on any handle (`runtime/io.c`), but
the ways to get a handle were per kind: `tcp_listen(port)`, `tcp_accept`,
`file_open`. Adding a TCP client, Unix sockets, UDP, and TLS that way would
keep adding `*_connect` / `*_bind` functions. This spec fixes the entry
points instead. The kind is chosen by an address string, in the style of Go's
`net.Dial` and Plan 9 dial strings.

```python
c  = dial("tcp://example.com:80")
ln = listen("tcp://127.0.0.1:0")
s  = accept(ln)                      # tcp and unix
f  = open("file:///var/log/app.log", "a")
print(local_addr(ln))                # tcp://127.0.0.1:54321
print(peer_addr(s))
# then read / write / close, whatever the kind
```

## Builtins

| Builtin | Type | Meaning |
| --- | --- | --- |
| `dial(addr)` | `(string) -> int` | connect; `tcp://` or `unix://` |
| `listen(addr)` | `(string) -> int` | bind + listen (backlog 128); `tcp://` or `unix://` |
| `accept(ln)` | `(int) -> int` | next connection on a listening handle |
| `open(addr, mode)` | `(string, string) -> int` | `file://` URI or a bare path; mode `r` / `w` / `a` |
| `local_addr(h)` | `(int) -> string` | bound address (`getsockname`), `""` on error |
| `peer_addr(h)` | `(int) -> string` | remote address (`getpeername`), `""` on error |

All handle-returning builtins return -1 on failure. Telling failures apart
(errno, `Result`) is #160.

## Decisions (RFC #175)

1. **Address format**
   - `tcp://host:port`: `host` is a name, an IPv4 literal, or an IPv6 literal
     in brackets (`tcp://[::1]:80`). `port` is 0–65535; 0 asks `listen` for
     an ephemeral port.
   - `tcp://:port` (empty host) is valid for `listen` only and binds every
     IPv4 interface, as `tcp_listen` does. Use `tcp://[::]:port` for IPv6.
   - `unix:///abs/path`: a stream Unix domain socket. The path must be
     absolute and fit in `sun_path`. `listen` does not unlink an existing
     file.
   - `file:///abs/path`, or a bare path with no `:`, is accepted by `open`
     only.
   - Anything else (a missing scheme or port, an unbracketed IPv6 address, a
     relative `unix://` path) is malformed and yields -1.
2. **Handle meaning**: handles stay `int`. Today they are OS fds, but
   programs must treat them as opaque. A later runtime-managed handle table
   (needed for TLS, #167) can replace them without changing this API.
3. **UDP**: out of scope here (#167). `dial("udp://…")` currently returns -1.
4. **Kind-specific operations** (seek, shutdown, socket options #165) stay as
   separate builtins.
5. **Existing builtins**: `tcp_listen`, `tcp_accept`, and `file_open` stay for
   compatibility. New code should use the forms above.
6. **Name resolution**: `getaddrinfo` blocks, so a fiber runs it on the aio
   pool (`rw_aio_call`) while it parks. The main thread calls it inline.
   `dial` tries each resolved address in order (`localhost` may resolve to
   `::1` first).

## Runtime behaviour

- **dial on a fiber**: nonblocking `connect`. On `EINPROGRESS` the fiber parks
  on the netpoller for writability, then reads `SO_ERROR`. On the main thread
  `connect` blocks.
- **Socket mode**: connected sockets are nonblocking, so read and write take
  the netpoller path. On macOS `SO_NOSIGPIPE` is set; Linux SIGPIPE handling
  is part of #160.
- **Main-thread I/O**: `read` / `write` on a nonblocking socket used to treat
  `EAGAIN` as EOF / failure when called off-fiber. They now wait with
  `poll(2)`, so a client written entirely in `main` works.
- **Address strings**: `local_addr` / `peer_addr` return strings that `dial`
  accepts (`tcp://127.0.0.1:54321`, `tcp://[::1]:8080`, `unix:///tmp/s`). An
  unnamed Unix peer is `unix://`.

## Fixes found on the way

`rw_str_from_cstr` wraps its argument without copying, and `rw_os_getcwd`
returned a pointer into its own stack frame. The new `rw_str_copy_cstr`
heap-copies; both `os_getcwd` and the address builtins use it.

## Tests

- `examples/dial_loopback.rw`: listen on port 0, dial it, echo, refused and
  malformed addresses, and `open`.
- `tests/test_e2e_dial.py`: an rw client (main thread and fiber) against
  Python TCP, IPv6, and Unix servers; an rw server on `tcp://127.0.0.1:0` and
  `unix://` reporting `local_addr` / `peer_addr`.
