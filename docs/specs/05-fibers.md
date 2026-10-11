# rw fiber runtime

This document describes the new runtime that replaces the original
**OS thread + Future** approach (`docs/specs/03-runtime-and-irgen.md`) with
**green threads (stackful coroutines + small stacks)**. The compiler-side ABI
(`rw_spawn_*` / `rw_await_*` / `rw_future_t`) is fully preserved, so the rw code
that users write does not change at all.

## Motivation

The old `pthread_create`-based implementation **breaks down beyond 10,000 spawns**:

- The default thread stack is 512KB on macOS and 8MB on Linux
- The kernel-side `task_struct` / `kthread` also costs several KB per thread
- On hosts where the default `ulimit -u` is a few thousand to ten thousand,
  `pthread_create` returns EAGAIN
- Context-switch cost is on the order of microseconds because it goes through
  the OS scheduler

This did not fit the use cases of "I want to write a web server (C10k) in rw"
or "I want to spawn thousands of workers." With the fiber version:

- **64KB + 2 guard pages per fiber** (16K + 16K on arm64)
- **A context switch is a single assembly function**: one call plus a few dozen
  load/store instructions
- **No OS resources are used** (it never calls `pthread_create`)
- Measured: **100,000 fibers finish in 437ms** on macOS arm64

## Terminology

| Term | Meaning |
|---|---|
| **fiber** | A lightweight thread. An execution context multiplexed on top of a single OS thread |
| **fiber context** | The save area for callee-saved registers (`rw_fiber_ctx`) |
| **scheduler** | A small loop that holds a READY queue and, on yield/completion, picks the next fiber and swaps to it |
| **trampoline** | A small assembly function that, when starting a new fiber for the first time, "sets up `x0 = arg` and then jumps to `entry`" |
| **handle** | An opaque pointer that identifies a spawned fiber (the actual form of `rw_future_t`) |

## Context-switch ABI

`runtime/fiber/fiber.h`:

```c
#define RW_FIBER_CTX_WORDS 21

typedef struct {
    uint64_t regs[RW_FIBER_CTX_WORDS];
} rw_fiber_ctx;

void rw_fiber_swap(rw_fiber_ctx *old, rw_fiber_ctx *new);
```

### arm64 register layout

| word index | contents |
|---|---|
| 0..9 | x19..x28 (integer callee-saved) |
| 10 | x29 (FP) |
| 11 | x30 (LR; return target) |
| 12 | sp |
| 13..20 | d8..d15 (lower half of the FP callee-saved registers) |

Using `stp` / `ldp` paired load/store instructions minimizes the instruction
count. See `runtime/fiber/fiber_arm64.S` for details.

### Starting a new fiber

`rw_fiber_swap` only "saves and restores callee-saved registers." The new
fiber's argument `arg` must be placed in `x0` per the ABI, but `x0` is not
callee-saved and so is not saved by swap.

Solution: **set up `x19 = entry`, `x20 = arg`, `lr = trampoline`**. The first
time `rw_fiber_swap` switches to this fiber, `ret` jumps to `trampoline`, and
`trampoline` executes `mov x0, x20; blr x19`.

## Scheduler

`runtime/fiber/sched.h`:

```c
void rw_sched_init(void);
void rw_sched_shutdown(void);
void rw_sched_yield(void);

rw_fiber_handle *rw_sched_spawn_i64 (int64_t (*fn)(void *), void *arg);
/* ... f64 / bool / str / void are the same ... */

int64_t rw_sched_join_i64 (rw_fiber_handle *h);
/* ... f64 / bool / str / void are the same ... */
```

### Behavior

- **There is still only one thread** (multicore support comes later in D-6)
- The ready queue is a FIFO singly linked list
- `rw_sched_yield()` pushes the current fiber onto the tail of the queue and
  swaps to the next fiber. If there is no next one, it returns to the caller
  (main or another fiber)
- `rw_sched_join_*(h)` repeatedly yields until the target fiber becomes DONE,
  then extracts the result and `free`s the handle

### Stack layout

| page | purpose |
|---|---|
| `[base, base + page]` | low guard (PROT_NONE) |
| `[base + page, base + page + 64K]` | usable stack (grows from low to high) |
| `[base + page + 64K, base + 2page + 64K]` | high guard (PROT_NONE) |

The guard pages are reserved with `mmap` + `mprotect(PROT_NONE)`. A stack
overflow becomes a SIGSEGV, so there is no silent corruption.

The page size is obtained at runtime with `sysconf(_SC_PAGESIZE)`
(macOS arm64 = 16K, Linux x86_64 = 4K).

## User ABI (unchanged)

The following in `runtime/runtime.h` is **fully signature-compatible**:

```c
rw_future_t *rw_spawn_i64 (int64_t (*fn)(void *), void *args);
int64_t      rw_await_i64 (rw_future_t *f);
/* ...other types are the same... */
```

The implementation is a shim in `runtime/runtime.c` that delegates to the fiber
scheduler. The LLVM IR emitted by the compiler (`rwc`) does not need to change
at all.

## await semantics

In the old pthread version, `rw_await_*` used `pthread_join` to **block the
calling thread**. In the fiber version:

- The calling fiber simply **repeats `rw_sched_yield()`**
- Meanwhile, other READY fibers keep running
- Once the target fiber completes, it extracts the result and returns

In other words, "await is now a cooperative wait," and other already-spawned
fibers are not blocked during an await.

However, **the current model is fully cooperative**, so a fiber that holds the
CPU for a long time starves the other fibers. Interrupt-based preemption is a
future task (to be considered in D-6).

## Per-target implementation

| OS / arch | assembly file | status |
|---|---|---|
| macOS arm64 | `fiber/fiber_arm64.S` | verified working |
| Linux aarch64 | `fiber/fiber_arm64.S` | same file; only the underscore prefix on symbol names is conditional |
| Linux x86_64 | `fiber/fiber_x86_64.S` | **verified working (all tests green on Docker linux/amd64)** |
| Windows | - | out of scope |

Implementation notes for x86_64 (System V AMD64 ABI):

- callee-saved integer registers: `rbx`, `rbp`, `r12`, `r13`, `r14`, `r15`, `rsp`
- there are no callee-saved floating-point registers (all XMM are caller-saved)
- the return address is on the stack. For a new fiber, `rw_fiber_ctx_init`
  writes `&rw_fiber_trampoline` at the top of the stack
- trampoline: from `r12 = entry`, `r13 = arg`, run `mov %r13, %rdi; call *%r12`

## Verification

- `runtime/fiber/test_pingpong.c`: verifies the swap itself with a round trip
  across 3 fibers
- `runtime/fiber/test_sched.c`: verifies that sum of squares is correct with
  1000 fibers
- `runtime/fiber/test_c10k.c`: verifies that 1+2+…+N is correct with
  100,000 fibers

In addition, `spawn_basic` / `spawn_many` / `spawn_string` in
`tests/test_e2e.py` keep working green on the fiber backend as well.

## Known limitations

1. **Only one thread**: CPU-bound parallel execution is not yet available.
   A work-stealing multicore scheduler is a separate phase.
2. **No preemption**: a fiber that runs an infinite loop or a heavy computation
   blocks the others.
3. **No automatic I/O yield**: if a syscall such as `read`/`recv` blocks
   directly, the entire scheduler stalls. epoll/kqueue integration is a
   separate phase.
4. **Debugger display**: an lldb stack trace can only see "the single current
   fiber." Inspecting the state of multiple fibers at once is not possible;
   you cannot examine other fibers individually via `info threads`.

## Future work

| ID | contents | effect |
|---|---|---|
| ~~D-4~~ | ~~Add Linux x86_64 assembly~~ | **done** |
| D-5 | I/O multiplexing (epoll/kqueue integration) | can write a C10k web server |
| D-6 | work-stealing multicore | true parallelism even for CPU-bound work |
| D-7 | preemption (timer or safepoints) | forcibly switch away from ill-behaved fibers |
