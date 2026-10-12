# rw channels

## Context

Fibers could only communicate through shared records (racy) or by
returning a value to `await` (#25). Channels are the message-passing
primitive Go programs are built on.

## Goals

- `Channel[T]` (a reference) created with `chan_new(capacity)`, typed from
  the expected type; capacity 0 is a rendezvous, N buffers up to N values
- `chan_send(ch, v)` blocks while the channel is full (or, for capacity 0,
  until a receiver takes the value); sending on a closed channel aborts
- `chan_recv(ch) -> Option[T]` blocks until a value arrives; after
  `chan_close(ch)` buffered values are still delivered, then `None`
- `for v in ch:` receives until the channel is closed and drained
- `len(ch)` is the number of buffered values
- Blocking parks the fiber (the worker runs others); the main thread can
  send and receive too

## Non-Goals

- `select` over several channels (#27), send / receive with timeouts
- Directional channel types (send-only / receive-only)
- Unbounded channels

## Design

`runtime/chan.c`: a ring buffer and two FIFO wait queues, all under one
mutex. An operation that cannot proceed appends a waiter (on its own stack)
and blocks. A fiber parks with `rw_sched_park_commit` whose commit unlocks
the channel mutex — only after the fiber's context is saved — so the party
that completes the operation (which must take the mutex) can never
re-enqueue a half-saved fiber (spec 24). A thread that is not a fiber waits
on a per-waiter condition variable. Values are copied directly between the
waiter and the other party: a receiver takes from a blocked sender, and a
slot freed in a full buffer goes to the first blocked sender, preserving
FIFO order.

Layers touched: parser / sema / irgen / runtime (4).
