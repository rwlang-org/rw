# 56. Socket operations: `shutdown`, `set_nodelay`, `set_keepalive`, `set_backlog`

Issue: #165

## Builtins

All return 0 on success, or -1 with `last_error` set (spec 51). Calling them
on a non-socket gives `ENOTSOCK`; on a closed handle, `EBADF`.

| Builtin | Effect |
| --- | --- |
| `shutdown(h, how)` | half-close; `how` is `"read"`, `"write"`, or `"both"` (anything else is `EINVAL`) |
| `set_nodelay(h, on: bool)` | `TCP_NODELAY`: send small writes immediately instead of batching (Nagle) |
| `set_keepalive(h, on: bool)` | `SO_KEEPALIVE`: let the kernel probe idle connections |
| `set_backlog(ln, n)` | pending-connection queue length of a listening handle (`listen` uses 128); `n < 1` is `EINVAL`, values above 65535 are capped |

`shutdown(h, "write")` sends FIN: the peer reads EOF but can still reply, and
`read` on `h` keeps working. The handle still has to be closed. `set_backlog`
calls `listen(2)` again, which on Linux and the BSDs only changes the
backlog of a listening socket.

The runtime symbol for `shutdown` is `rw_sock_shutdown`, because
`rw_shutdown` is the runtime's own teardown.

## `examples/web.rw`: lingering close

After writing a response, the framework used to `close` at once. If the
client had sent more than the server read (a request head over the limit,
or a body it refused), the kernel answered the close with RST. The RST can
discard the response before the client reads it, so the client saw "connection
reset" instead of `413`.

`linger_close` now:
1. calls `shutdown(fd, "write")`, so the response is complete and the client
   sees EOF;
2. reads and discards up to 1 MiB, with a 500 ms read timeout (spec 53);
3. then closes.

A well-behaved client closes as soon as it has the response, so step 2 ends
at once.

## Tests

- `tests/test_e2e_sockopts.py`:
  - half-close, then the peer reads EOF and still replies
  - nodelay / keepalive succeed
  - EINVAL / EBADF error cases
- `tests/test_e2e_web.py`: a 200 KB request head gets `413` instead of a reset.
