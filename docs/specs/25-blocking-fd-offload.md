# rw: offload every non-socket fd to the aio pool

## Context

`read` / `write` work on any fd (spec 15). Sockets are nonblocking and park
on the netpoller; regular files are offloaded to the aio thread pool
(spec 16). Everything else — stdin on a terminal or a pipe, pipes in
general, character devices — fell through to a plain blocking `read(2)` /
`write(2)` on the worker thread. One fiber waiting for input on stdin
therefore froze its whole worker: on `RW_WORKERS=1`, no other fiber ran
until a line was typed.

This is the counterpart, for system calls, of the preemption in spec 23:
Go hands a worker blocked in a system call to another thread; rw already
has a thread pool for blocking calls, so it routes them there.

## Goals

- `read` / `write` on any fd that is not a socket go through the aio pool
  (the fiber parks; the worker runs other fibers)
- Sockets keep the netpoller path; behavior from the main thread (not a
  fiber) is unchanged: a direct blocking call

## Non-Goals

- `print`: it writes through C stdio on the calling thread (buffered); a
  full pipe on stdout can still block a worker
- `file_open` / `close`: still direct calls (normally fast)
- Growing the pool on demand: each fd blocked in the pool holds one of its
  threads (`RW_AIO_THREADS`, default 4), so more concurrent blocking reads
  than threads queue up behind each other

## Design

`io.c` asks `fstat` whether the fd is a socket (`S_ISSOCK`). If not, the
call goes to `rw_aio_read` / `rw_aio_write`, which already park the fiber
until a pool thread has finished the system call (and, since spec 24, only
publish the task after the fiber's context is saved).

## Verification

`examples/stdin_offload.rw`: a fiber reads stdin while another fiber
writes to stdout. `tests/test_e2e_blocking_fd.py` runs it on
`RW_WORKERS=1` with stdin open but empty and requires the second fiber's
output before any input is sent.

```sh
make -C runtime
uv run --frozen pytest tests/test_e2e_blocking_fd.py -q
uv run --frozen pytest -q
```

## Update (spec 61)

A FIFO in nonblocking mode (the pipes rw creates for child processes) now
uses the netpoller instead of the pool; see spec 61.
