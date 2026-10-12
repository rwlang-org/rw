# rw `Mutex`

## Context

Records and dicts are shared by reference and fibers run in parallel on
several workers, so concurrent read-modify-write on shared state loses
updates (#26). Preemption (spec 23) makes the window larger.

## Goals

- `Mutex` (a reference) with `mutex_new()`, `mutex_lock(m)`,
  `mutex_unlock(m)`, and `mutex_try_lock(m) -> bool`
- Waiting parks the fiber (its worker runs others); the main thread can lock
  too; waiters acquire the lock in FIFO order
- Unlocking a mutex that is not locked aborts

## Non-Goals

- `Mutex[T]` wrapping a value and `with m.lock() as guard:` — they need the
  `with` statement (#102); the plain lock is the building block
- `RWLock`, `CondVar`, owner tracking, recursive locking, deadlock detection

## Design

`rw_mutex` reuses the channel's waiter queue (spec 44): `lock` either takes a
free mutex or enqueues a waiter and parks with `rw_sched_park_commit`, whose
commit releases the internal mutex after the fiber's context is saved
(spec 24). `unlock` hands ownership directly to the first waiter (the mutex
stays locked), so a newcomer cannot barge in front of waiting fibers.

Layers touched: sema / irgen / runtime (3).
