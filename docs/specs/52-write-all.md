# 52. `write_all`

Issue: #161

## Motivation

`write` returns the number of bytes the kernel accepted, which can be less
than the buffer on a socket. Every program that sends more than a socket
buffer has to write the retry loop itself (`examples/web.rw` had its own
`write_all`).

## Builtin

`write_all(h: int, b: Bytes) -> int` writes all of `b`.

- Returns `len(b)` on success. Returns -1 if any underlying write fails; some
  prefix may already have been sent. `last_error` (spec 51) then holds the
  cause (`EPIPE`, `ECONNRESET`, …). A write that makes no progress is `EIO`.
- It works on every handle kind and uses the same paths as `write`:
  - **socket on a fiber**: on `EAGAIN` the fiber parks on the netpoller, then
    sends the rest
  - **socket on the main thread**: waits with `poll(2)`
  - **files, pipes, ttys**: each chunk is offloaded to the aio pool
- Writes from different fibers to the same handle may interleave between
  chunks. Use a `Mutex` if the bytes must stay contiguous.

## Changes

- `runtime/io.c`: `rw_write_all` loops over the internal write path.
- `examples/web.rw`: drops its hand-written `write_all` in favour of the
  builtin.

## Tests

`tests/test_e2e_write_all.py`:
- 8 MiB sent from the main thread and from a fiber to a reader with a 4 KiB
  receive buffer that pauses after its first chunk. Every byte arrives in
  order.
- A peer that closes at once gives -1 with `EPIPE` / `ECONNRESET`.
