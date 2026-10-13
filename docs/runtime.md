# rw runtime design

This document describes the rw runtime (the C code under `runtime/` and `runtime/librw.a`) and what the compiler `rwc` promises the runtime. It is written for people who read the runtime, add features to it, or chase bugs in it.

The code is the source of truth. Design history and rejected alternatives live in the specs under `docs/specs/`, referenced in the text as "Details: spec NN". Places where a spec and the code disagree are collected in "Appendix: differences from specs and comments" at the end.

A Japanese version of this document is at [`docs/ja/runtime.md`](ja/runtime.md).

## Contents

1. [Big picture](#1-big-picture)
2. [Value representation and ABI](#2-value-representation-and-abi)
3. [Fibers and the scheduler](#3-fibers-and-the-scheduler)
4. [I/O](#4-io)
5. [Timers, synchronization, signals, processes, filesystem](#5-timers-synchronization-signals-processes-filesystem)
6. [Memory management](#6-memory-management)
7. [Compiler-side guarantees](#7-compiler-side-guarantees)
8. [Checklist for extending the runtime](#8-checklist-for-extending-the-runtime)
9. [Tests and debugging](#9-tests-and-debugging)
10. [Appendix: differences from specs and comments](#appendix-differences-from-specs-and-comments)

---

## 1. Big picture

### 1.1 Compilation pipeline

```
 .rw source
    │  rwc/lexer.py        tokens
    │  rwc/parser.py       AST
    │  rwc/desugar.py      remove syntactic sugar (range for → VarDecl + While + Assign)
    │      * rwc/loader.py runs the same three stages on imported modules
    │  rwc/sema.py         type checking, call resolution, closure capture analysis
    │  rwc/irgen.py        LLVM IR via llvmlite
    ▼
 temporary *.ll file
    │  clang -O<level> x.ll runtime/librw.a -o out -L<gc>/lib -lgc -lpthread -lm
    ▼
 native executable
```

`compile_source` in `rwc/driver.py` drives this flow.

- If `librw.a` is missing, the first build runs `make -C runtime` to create it.
- The optimization level comes from the `-O` flag, then `$RW_OPT_LEVEL`, then the default 2, and is passed straight to clang (Details: spec 70).
- The Boehm GC link flags are built from the prefix found by `scripts/gc-prefix.sh`. If the prefix is neither `/usr` nor `/usr/local`, `-L<prefix>/lib -Wl,-rpath,<prefix>/lib` is added, followed by `-lgc` (Details: spec 67).
- `-lm` covers intrinsics such as `llvm.pow` that the backend may lower to libm calls. Linux needs it linked explicitly.

### 1.2 What librw.a contains

`runtime/Makefile` bundles the following into a static library. CFLAGS are `-O2 -Wall -Wextra -fPIC -std=c11 -D_GNU_SOURCE` plus the GC include path.

| File | Role |
|---|---|
| `runtime.c` / `runtime.h` | Declarations of the public ABI. print, assert, strings, lists, dicts, OS API, random numbers, lifecycle (`rw_init` / `rw_shutdown`), thin spawn / await wrappers |
| `io.c` | Generic `read` / `write` / `write_all` / `close` / `file_open`, the error-name table, timeout setters |
| `aio.c` | Thread pool that runs blocking syscalls on behalf of fibers |
| `timer.c` | Timer thread and min-heap, `sleep`, clocks |
| `chan.c` | Channels and mutexes |
| `signal.c` | `signal_wait` built on a self-pipe |
| `fs.c` | Filesystem builtins |
| `process.c` | Child processes (`posix_spawnp`) |
| `gc.c` / `rwgc.h` | Boehm GC wrapper. Only this file includes `<gc/gc.h>` |
| `fiber/fiber.c`, `fiber/fiber_{arm64,x86_64}.S` | Context initialization and switching |
| `fiber/sched.c` | M:N scheduler, preemption, fiber lifetime |
| `fiber/runq.c` | Per-P bounded ring and global queue |
| `fiber/park.c` | `atomic_flag` spinlock for wait lists |
| `net/netpoller.c` | Common netpoller code, per-fd poll descriptors |
| `net/netpoller_kqueue.c` / `net/netpoller_epoll.c` | Per-OS event loop (macOS / Linux) |
| `net/tcp.c` | `tcp_listen` / `tcp_accept` |
| `net/addr.c` | Address-string `dial` / `listen` / `accept` / `open`, UDP, socket options |

The assembly file is chosen by `uname -m` and the netpoller backend by `uname -s`. Supported are arm64 / aarch64 / x86_64 and Darwin / Linux.

### 1.3 The generated C main

irgen renames the user's `main` to `@rw_user_main` and synthesizes a separate C `main` (`IRGen._emit_c_main`).

```c
int main(int argc, char **argv) {
    rw_set_args(argc, argv);   /* for os_args() (spec 40) */
    rw_init();
    int64_t r = rw_user_main();
    rw_shutdown();
    return (int32_t)r;         /* i64 truncated to i32 */
}
```

`rw_init` and `rw_shutdown` start and stop the subsystems in the following order. Shutdown is the reverse of startup.

```
rw_init:      signal(SIGPIPE, SIG_IGN)
              rw_sched_init      GC init → start workers → start monitor
              rw_netpoller_init  open kqueue/epoll, start the netpoller thread
              rw_aio_init        start the aio pool
              rw_timer_init      start the timer thread

rw_shutdown:  rw_timer_shutdown → rw_aio_shutdown → rw_netpoller_shutdown → rw_sched_shutdown
```

`rw_user_main` runs directly on the main thread, not on a fiber. The main thread is not a worker, so a blocking operation called from main blocks the whole thread instead of parking (section 4.7).

`rw_sched_shutdown` sets the shutdown flag, wakes the workers, `pthread_join`s all workers and the monitor, and then frees fibers that are still in the queues and never ran. Fibers that remain parked or running are not freed (process exit reclaims them; Details: spec 19). Workers see the shutdown flag only between fibers, so shutdown waits for a fiber that keeps running without passing a safe point.

### 1.4 Threads at run time

```
                      ┌──────────────────────────────────────┐
  main ───────────────┤ rw_user_main (not a fiber)           │  GC registered: yes (GC_INIT)
                      └──────────────────────────────────────┘
                        │ spawn → globq
                        ▼
  worker M0..Mn-1   ┌────────┐ ┌────────┐     ┌────────┐       GC registered: yes
  (RW_WORKERS)      │ M0 / P0│ │ M1 / P1│ ... │ Mn / Pn│       each M runs fibers
                    └────────┘ └────────┘     └────────┘
                        ▲  rw_sched_enqueue_ready (push to globq + cond_signal)
        ┌───────────────┼──────────────┬──────────────┬───────────────┐
  monitor            netpoller        aio pool        timer           signal
  requests preempt   waits in         RW_AIO_THREADS  min-heap,       reads the self-pipe
  every 1 ms         kqueue/epoll     blocking calls  wakes on        (started by the first
                                                      deadlines        signal_wait)
  GC registered: no  GC registered: no GC registered: yes GC registered: no GC registered: no
```

| Thread | Count | Created by | Role | GC registered |
|---|---|---|---|---|
| main | 1 | the process | Runs `rw_user_main`. Not a fiber | yes (`GC_INIT`) |
| worker (M) | `RW_WORKERS`, default the number of online CPUs, at most 64 | `rw_sched_init` | Takes fibers from the P queues and runs them | yes (start of `worker_main`) |
| monitor | 0 or 1 (not started when `RW_PREEMPT_MS=0`) | `rw_sched_init` | Asks long-running fibers to yield | no |
| netpoller | 1 | `rw_netpoller_init` | Waits for fd readiness and wakes waiters | no |
| aio pool | `RW_AIO_THREADS`, default 4, at most 64 | `rw_aio_init` | Blocking read/write, `getaddrinfo`, `stat`, `waitpid`, and so on | yes (it allocates read buffers and directory listings) |
| timer | 1 | `rw_timer_init` | `sleep` and timeout callbacks | no |
| signal | 0 or 1 | first `rw_signal_wait` (`pthread_once`), detached | Reads the self-pipe and wakes waiters | no |

A thread that is not registered with the GC must not touch the GC heap. This is also a rule for extending the runtime (chapter 8).

---

## 2. Value representation and ABI

### 2.1 Types and their LLVM representation

`llvm_type_of` in `rwc/irgen.py` maps rw types to LLVM types.

| rw type | LLVM representation | C type | Where it lives |
|---|---|---|---|
| `int` | `i64` | `int64_t` | value |
| `float` | `double` | `double` | value |
| `bool` | `i8` | `int8_t` | value (0 / 1) |
| `string`, `Bytes` | `{i64 len, i8* ptr}` | `rw_str` | value. The bytes are on the GC heap (atomic) or a constant |
| `List[T]` | `{i64 len, i64 cap, i64* data}` | `rw_list_int` / `rw_list` | the struct is a value, `data` is on the GC heap |
| `Option[T]` | `{i64 tag, T value}` | `rw_option_int` and others | value. tag 0 = None, 1 = Some |
| `Result[T, E]` | `{i64 tag, T ok, E err}` | — | value. tag 1 = Ok, 0 = Err |
| tuple | struct of the elements | — | value |
| record (class) | `i8*` | — | pointer to a struct on the GC heap |
| `Dict[K, V]` | `i8*` | `rw_dict *` | GC heap |
| `Channel[T]` | `i8*` | `rw_chan *` | GC heap |
| `Mutex` | `i8*` | `rw_mutex *` | GC heap |
| `Future[T]` | `i8*` | `rw_future_t *` (really `rw_fiber_handle *`) | uncollectable GC memory |
| function value | `{i8* code, i8* env}` | — | value. `env` is on the GC heap or null |
| `void` | `void` | `void` | — |

### 2.2 Strings and Bytes

`rw_str` is the two-word struct `{int64_t len; const char *ptr;}` and is not NUL-terminated. `string` and `Bytes` share this representation (Details: spec 08).

- Strings are immutable. `rw_str_slice`, the `str_strip` family, and indexing (`rw_str_index`) therefore return zero-copy slices that share the source buffer. Boehm GC recognizes interior pointers, so the source buffer survives as long as any slice of it does.
- New byte sequences (concatenation, conversions, read buffers, `rand_bytes`) are allocated with `rw_gc_alloc_atomic`. They contain no pointers, so the GC does not scan them.
- String literals point to private constant globals named `.str.N`. These are outside the GC heap and never collected.
- An empty string may have `ptr == NULL`. C code must check `len` before dereferencing `ptr`.
- To pass a string to a C API, make a NUL-terminated copy with `malloc` and `free` it afterwards (`file_open_impl` in `io.c`, `cpath` in `fs.c`, `cstr` in `process.c`).

### 2.3 Lists

`List[T]` is three words, `{len, cap, data}`, for every element type (Details: spec 09, 22). `List[int]` uses the `rw_list_int` functions; every other element type uses the `rw_list` functions, which take the element size.

- Lists are immutable. `list_push` allocates a new buffer every time, copies the existing elements, and returns a new list (copy-on-push). Capacity starts at 4 and doubles, but because the buffer is rebuilt on every push, building a list of length n by repeated pushes costs O(n²).
- `List[int]` data is atomic; data for other element types is scanned memory, because the elements may point to strings or records.
- `rw_list_int_at` / `rw_list_at` `abort` on an out-of-range index. `list_at_opt` returns an Option.
- A three-word struct exceeds arm64's limit for returning in registers and becomes sret. To avoid tagging llvmlite declarations with sret, every list function takes **pointers** (section 2.10).

### 2.4 Dicts

`rw_dict` is an insertion-ordered hash table laid out like CPython's compact dict (Details: spec 31, 36).

```
rw_dict ─┬─ entries: [ {hash, live, key, value} ... ]   insertion order. scanned by the GC
         └─ index:   [ -1 | position in entries ... ]   linear probing, power of two, load ≤ 2/3. atomic
```

- `key_kind` distinguishes key kinds: 0 is `int` (hashed with splitmix64), 1 is `string` (FNV-1a).
- An entry is `16 + key_size + val_size` bytes (the value rounded up to 8 bytes).
- Removal only clears `live`; the index slot stays as a tombstone. When `entries` fills up, the capacity doubles if at least half the entries are live; otherwise the table is compacted at the same capacity.
- Keys and values are always passed by pointer (`rw_dict_set(d, &key, &val)`).

### 2.5 Option / Result / tuples

All are structs passed by value; none uses the heap (Details: spec 10, 11, 30, 37).

- `Option[T]` is `{i64 tag, T value}`. `rw_option_int` for `Option[int]` and `rw_option_float` for `Option[float]` are 16 bytes and could be returned by value, but runtime functions use an output pointer for consistency with the rest.
- `Result[T, E]` is `{i64 tag, T ok, E err}`. `match` takes the Ok arm when tag is 1 and the Err arm otherwise.
- A tuple is a struct of its element types. `await_any` returns an `(int, T)` tuple.

### 2.6 Records

A record is a pointer (`i8*`) to a struct on the GC heap (Details: spec 20).

- The struct holds the fields in declaration order, each embedded as `llvm_type_of` gives it. A string field holds a 16-byte `rw_str`, and a record-typed field is a pointer, so layouts are never recursive.
- The constructor allocates with `rw_gc_alloc(sizeof)` (1 byte if the size is 0) and stores each field. The size is computed with a GEP on null.
- Field access uses `bitcast` and `gep` (`IRGen._field_ptr`).

### 2.7 Function values and closures

A function value is the two words `{i8* code, i8* env}` (Details: spec 21, 71).

- `code` has type `R (i8* env, P1, ..., Pn)`: the first argument is always the environment.
- Using a **top-level function** as a value creates an internal thunk `rw_fnval_<name>` that ignores the environment and calls the function; the value is `{thunk, null}`.
- A **closure** gets a function named `rw_closure_<name>`. Captured variables are copied, with their values at creation time, into an environment struct on the GC heap (capture by value). Without captures, `env` is null.
- The closure body starts by copying each variable from the environment into a local alloca. A local function that refers to itself by name gets a slot holding the same `{code, env}`.
- A call casts `code` to the right function type and passes `env` as the first argument (`IRGen._call_fn_value`).

### 2.8 Handle types

`Future`, `Channel`, `Mutex`, and `Dict` are opaque pointers.

- A `Future[T]` is really a `rw_fiber_handle *`. `runtime.c` casts between it and `rw_future_t *`.
- A `Channel[T]` receives the element size at creation and passes values by pointer.
- `Mutex` is a fiber-aware lock (section 5.3).
- `rw_chan` and `rw_mutex` contain a `pthread_mutex_t`, and nothing calls `pthread_mutex_destroy` when the GC collects them. That leaks nothing on macOS and Linux, but keep it in mind when porting.

### 2.9 Symbol naming

| Symbol | Meaning |
|---|---|
| `rw_*` | C functions and variables exported by the runtime (`rw_str_concat`, `rw_preempt_flag`, ...) |
| `rw_user_<name>` | user function in the entry module |
| `rw_user_<module>.<name>` | function in an imported module. `.` cannot appear in an rw identifier, so it never collides |
| `rw_trampoline_<callee>` | spawn trampoline, one per callee |
| `rw_closure_<name>` | closure code |
| `rw_fnval_<name>` | thunk that turns a top-level function into a value (internal) |
| `.str.N` | string literal (private constant) |
| `main` | the synthesized C entry point |

Runtime-internal functions are either `static` or carry a subsystem prefix such as `rw_sched_*`, `rw_net_*`, `rw_aio_*`, `rw_gc_*`, or `rw_timer_*`.

### 2.10 Calling conventions used by irgen

irgen declares the runtime functions in `_declare_runtime` and calls them by these rules.

| Kind | Convention | Example |
|---|---|---|
| Scalars (int/float/bool) and `rw_str` | passed and returned by value. `rw_str` is 16 bytes and fits in two registers | `rw_str rw_str_concat(rw_str, rw_str)` |
| Lists (24 bytes) | input as `const rw_list *`; output as `rw_list *out` (first argument) pointing at a caller alloca | `void rw_list_push(rw_list *out, const rw_list *l, const void *elem, int64_t elem_size)` |
| Option / compound results | output pointer | `void rw_str_to_int(rw_option_int *out, rw_str)`, `void rw_recv_from(rw_datagram *out, ...)` |
| Generic elements (any type T) | pointer to the element plus its size | `rw_list_at(void *out, const rw_list *, i, elem_size)`, `rw_chan_send(c, const void *)` |
| Handles | `i8*` | `rw_dict *`, `rw_chan *`, `rw_future_t *` |

Output targets and temporary inputs are always slots allocated in the entry block by `FunctionCtx.alloca` (section 7.4). The rationale is in the comment on `rw_list_int` in `runtime.h`.

**Direct-call table**: builtins that only need their arguments evaluated and passed through are listed in the `direct` dict in `IRGen._emit_call` as `"rw name": declared function` (`str_find`, `sleep`, `dial`, `fs_kind`, `gc_collect`, ...). Builtins that take lists, need an output pointer, or are generic are handled by dedicated branches in `_emit_call`, `_emit_generic_list_op`, `_emit_dict_builtin`, `_emit_chan_builtin`, and so on.

**Spawn ABI**: `spawn f(a, b)` expands to:

```
caller:                                        rw_trampoline_f(i8* args):
  args = rw_gc_alloc(sizeof {A, B})              a, b = load args
  store a, b → args                              r = rw_user_f(a, b)
  fut = rw_spawn_<T>(rw_trampoline_f, args)      rw_gc_free(args)
                                                 return r
```

`rw_spawn_<T>` and `rw_await_<T>` come in five flavors by return type: `i64` / `f64` / `bool` / `str` / `void`. `Bytes` uses `str`. A function returning any other type (record, list, ...) cannot be spawned; irgen raises `RuntimeError`. A `spawn` used as an expression statement (its Future discarded) is immediately followed by `rw_detach(fut)` (Details: spec 19).

---

## 3. Fibers and the scheduler

### 3.1 Fiber stacks

Each fiber runs in its own `mmap`'d region (Details: spec 05).

```
 low address                                                  high address
 ┌────────────┬────────────────────────────────────┬────────────┐
 │ guard page │ usable stack 64 KiB                │ guard page │
 │ PROT_NONE  │ ← sp grows downward from here  top ─┤ PROT_NONE  │
 └────────────┴────────────────────────────────────┴────────────┘
 region       region+page                          region+page+64K
```

- The usable part is `RW_FIBER_STACK_USABLE` = 64 KiB, rounded up to a multiple of the page size. The page size comes from `sysconf(_SC_PAGESIZE)` (16 KiB on macOS/arm64, 4 KiB on Linux/x86_64).
- The guard pages at both ends are protected with `mprotect(PROT_NONE)`. A stack overflow becomes SIGSEGV instead of silently corrupting memory.
- Stacks do not grow. Deep recursion and large locals must fit in 64 KiB. This is also why irgen puts every alloca in the entry block (section 7.4).
- `fiber_stack_top(g)` is the highest address of the usable part, which the GC treats as the stack bottom.

### 3.2 Context switch

`rw_fiber_swap(old, new)` saves the callee-saved registers into `*old`, restores them from `*new`, and `ret`s. `rw_fiber_ctx` is an array of 21 words.

| Index | arm64 | x86_64 |
|---|---|---|
| 0..9 | x19..x28 | 0: rbx, 1: rbp, 2: r12, 3: r13, 4: r14, 5: r15 |
| 10 | x29 (fp) | — |
| 11 | x30 (lr), the resume target | — |
| 12 | **sp** | — |
| 6 | — | **rsp** |
| 13..20 | d8..d15 | — |

The sp index differs by architecture (12 on arm64, 6 on x86_64). `saved_sp()` in `sched.c` branches accordingly; the GC uses it to scan the stacks of suspended fibers.

`rw_fiber_ctx_init` prepares a new fiber.

- arm64: `entry` goes in x19, `arg` in x20, and lr is set to `rw_fiber_trampoline`. The trampoline sets `x0 = x20` and runs `blr x19`; if entry returns, it stops at `brk #0xdead`.
- x86_64: the return address lives on the stack, so `&rw_fiber_trampoline` is written just below the 16-byte-aligned stack top and rsp points there. `entry` is in r12 and `arg` in r13. If entry returns, the trampoline stops at `ud2`.

A fiber's entry function (`fiber_entry_<T>`) never returns. It ends by switching to the scheduler from `finalize_fiber`.

### 3.3 Structure of the M:N scheduler

The structure is a simplified version of Go's G-M-P model (Details: spec 06).

```
          spawn (from main) / preempted fibers / wake-ups from other threads
                                   │
                                   ▼
                    ┌──────────────────────────────┐
                    │ globq (linked list + mutex)  │
                    └──────────────────────────────┘
                       ▲ overflow (half+1) │ refill (up to 128)
       ┌───────────────┴──┐     ┌──────▼───────────┐
       │ P0: ring[256]    │◀───▶│ P1: ring[256]    │ ...   ← try_steal takes half from another P
       └───────┬──────────┘steal└──────┬───────────┘
               │                       │
           M0 (worker)              M1 (worker)
           sched_ctx               sched_ctx
           current                 current
```

| Term | Type | Description |
|---|---|---|
| G | `rw_fiber_handle` | a fiber. Holds ctx, stack region, state, result, wait list, reap flag, `last_error` |
| M | `rw_M` | a worker thread. Holds its own scheduler ctx (`sched_ctx`), the running G, a pointer to its P, preemption counters, and the park commit |
| P | `rw_P` | a bounded ring of 256 entries. Only the owning M puts and gets; other Ms can grab (steal) with a CAS |
| globq | `rw_globq` | a mutex-protected linked list. Receives overflow, spawns from main, wake-ups from other threads, and preempted fibers |

Workers and Ps are one-to-one; there are `RW_WORKERS` of them (1–64). The thread-local `tls_m` is the worker's own `rw_M` on a worker, and NULL elsewhere (main, netpoller, ...). It is the single source of truth for "am I inside a fiber". `rw_sched_current_fiber()` returns the running fiber's handle on a worker that is running one, and NULL otherwise.

**Enqueueing (`enqueue_ready`)**:

- Called on a worker (`tls_m != NULL`), it puts the fiber on that worker's P ring. When the ring is full, half of it plus the fiber move to globq in one batch. With more than one worker, it wakes one sleeping worker with `pthread_cond_signal`.
- Called anywhere else (the main, netpoller, aio, timer, and signal threads), it pushes to globq and wakes one worker.

### 3.4 Picking the next fiber

`find_runnable(m)` searches in this order.

1. If the shutdown flag is set, return NULL (the worker exits).
2. If `m->tick % 61 == 0`, take one fiber from globq first.
3. Take one from the worker's own P ring.
4. Take up to 128 from globq in one batch, run one, and put the rest on the worker's ring.
5. Starting from a random position (xorshift64), walk the other Ps and steal half of one.
6. If nothing is found, take `g_sched_mu`, re-check the shutdown flag and globq, and sleep on `g_sched_cv`.

The once-every-61 interval in step 2 follows Go's `schedule()`. Without it, as long as some fiber keeps being re-enqueued locally after preemption, or a worker never drains its ring, fibers that main spawned into globq would never run (Details: spec 23).

### 3.5 State transitions

`rw_fiber_handle.state` takes four values: `READY` / `RUNNING` / `WAITING` / `DONE`.

```
              spawn_common
                  │ enqueue_ready
                  ▼
   ┌──────────▶ READY ◀─────────────────────────────────┐
   │              │ worker_main takes it                  │
   │              ▼                                       │ waker calls rw_sched_enqueue_ready
   │           RUNNING ──── rw_sched_park_commit ───▶ WAITING
   │              │                                  (commit registers the wake-up path)
   │ yield /      │
   │ preempt      │ fn(arg) returns → finalize_fiber
   └──────────────┤
                  ▼
                 DONE ──(both REAP_FINISHED and REAP_RELEASED)──▶ free_handle
```

- `READY ⇄ RUNNING` transitions happen on the scheduler's serialized path, so relaxed ordering suffices.
- Only `RUNNING → DONE` synchronizes. `finalize_fiber` writes the result and then release-stores `DONE`; a joiner acquire-loads `DONE` before reading the result.

When a fiber returns to the scheduler, `worker_main` tells why as follows.

| Check | Meaning | Action |
|---|---|---|
| `m->park_commit != NULL` | it parked | call the commit; never touch the fiber afterwards |
| `m->finished == g` | it finished | exchange `reap` with `REAP_FINISHED`; free if it was already `REAP_RELEASED` |
| `state == RUNNING` and `m->preempted` | it was preempted at a safe point | push to the tail of globq |
| `state == RUNNING` | it yielded voluntarily | put back on the worker's own P |

Completion is detected with the M-local `m->finished` rather than `g->state` because other threads may be changing `state`.

### 3.6 spawn / await / detach and reclamation

**spawn** (`spawn_common`) proceeds as follows.

1. Allocate the handle with `rw_gc_alloc_uncollectable` (section 6.2).
2. Initialize `join_mu` / `join_cv` / `wait_lock`.
3. `mmap` the stack region and protect the guard pages.
4. Initialize ctx.
5. Add the fiber to the GC's list of live fibers (`track_fiber`).
6. Queue it with `enqueue_ready`.

A new fiber does not run immediately.

**await** (`rw_sched_join_<T>` → `wait_done`) behaves differently depending on the caller.

- From a fiber, it parks on the target's wait list (`park_on` → `join_commit`). The commit takes the target's `wait_lock` and re-checks: if the target is already `DONE`, it re-enqueues the joiner at once; otherwise it pushes the joiner onto the wait list. After publishing `DONE`, `finalize_fiber` takes `wait_lock`, detaches the whole wait list, and `enqueue_ready`s each waiter.
- From the main thread, it sleeps on the handle's `join_cv`. `finalize_fiber` broadcasts `join_cv` after `DONE`.

After reading the result, it calls `release_handle`.

**detach** discards a Future and only calls `release_handle`.

**Reclamation protocol** (Details: spec 19): the handle is freed by whichever of the worker and the owner arrives second.

```
worker_main (after the fiber finished and left its stack):
    if (exchange(&g->reap, REAP_FINISHED) == REAP_RELEASED) free_handle(g);

release_handle (after join read the result, or on detach):
    if (exchange(&h->reap, REAP_RELEASED) == REAP_FINISHED) free_handle(h);
```

Both are atomic exchanges, so exactly one side sees the other's value. The worker writes `REAP_FINISHED` only after the fiber has fully left its stack through `rw_fiber_swap`, so a stack in use is never `munmap`ped. `free_handle` removes the fiber from the live list, destroys the mutex/cond, `munmap`s the stack, and `rw_gc_free`s the handle.

`rw_sched_live_fibers()` is a spawned-minus-freed counter for tests only.

### 3.7 park and park_commit (publish after save)

Every operation that stops a fiber to be woken later goes through `rw_sched_park_commit(commit, arg)` (Details: spec 24).

```
on the fiber's stack:                         on the worker's scheduler stack:
  state = WAITING
  m->park_commit = commit, m->park_arg = arg
  rw_gc_switch_begin
  rw_fiber_swap(&me->ctx, &m->sched_ctx) ───▶ (the swap returned = ctx fully saved)
                                              rw_gc_switch_end
                                              commit(me, arg)
                                                └ only now is me published to wakers
                                                  (register with the netpoller, queue an
                                                   aio task, join a wait list, unlock)
                                              never touch me afterwards
```

**Why it is needed**: with the order "register with the wake-up path, then swap", an event that fires between the two lets the waker re-enqueue a fiber whose ctx is still being saved. Another worker may steal and resume it with half-written registers and crash near PC=0. Or it may resume it fully and mark it RUNNING, after which the original worker sees RUNNING and enqueues it again, so the same fiber runs twice in parallel. `runtime/fiber/test_park_race.c` reproduces this race.

For the same reason, `rw_sched_yield` does not re-enqueue the fiber itself. `worker_main` does that after the swap returns.

**What a commit promises**:

- A commit runs on the worker's scheduler stack, on the same OS thread as the fiber that parked. Unlocking in the commit a `pthread_mutex` that the fiber locked is therefore legitimate (channels, mutexes, and signals rely on this).
- If a commit cannot arrange a wake-up, it must call `rw_sched_enqueue_ready(self)` itself.
- A commit must not block.

The current commits:

| Caller | Commit |
|---|---|
| `await` (from a fiber) | `join_commit`: push onto the target's wait list; enqueue at once if already DONE |
| netpoller wait | `commit_wait`: set the waiter's fiber and register with kqueue/epoll; enqueue at once if timed out, closed, or registration failed |
| aio | `commit_task`: push the task on the fiber's stack to the pool queue |
| `sleep` | `commit_sleep`: push onto the timer heap |
| channel | `unlock_commit`: unlock the channel mutex |
| mutex | `mutex_unlock_commit`: unlock the internal mutex |
| `signal_wait` | `commit_wait`: push onto the waiter list and unlock `g_mu` |

### 3.8 Preemption at safe points

Fibers are scheduled cooperatively. So that a fiber computing without I/O cannot monopolize its worker, the runtime switches at safe points inserted by the compiler (Details: spec 23).

**Compiler side** (`IRGen._emit_safepoint`): at function entry, closure entry, the head of every `while` loop (passed on every iteration, `continue` included; a range `for` becomes a `while` in desugar), and the loop head of `for x in list` and `for` over a channel, it emits:

```llvm
%p = load atomic i32, i32* @rw_preempt_flag monotonic, align 4
%req = icmp ne i32 %p, 0
br i1 %req, label %preempt, label %preempt.cont
preempt:
  call void @rw_safepoint()
  br label %preempt.cont
```

**Runtime side**:

```
monitor (every 1 ms)                          worker M_i
  for each M:                                   worker_main: tick++ on every fiber resume,
    tick unchanged and running == 1               running = 1
    for at least RW_PREEMPT_MS
      M_i.preempt = 1                        safe point: rw_preempt_flag != 0
      rw_preempt_flag++  ─────────────────────▶ rw_safepoint():
    (asks again every slice for the same fiber)    if exchange(M_i.preempt, 0) was 1:
                                                  rw_preempt_flag--, preempted = 1
                                                  rw_sched_yield()
                                               worker_main: if preempted, tail of globq
```

- `rw_preempt_flag` is a global count of pending requests. A request for any one M sends the safe points of all workers down the slow path, but `rw_safepoint` looks only at its own M's `preempt` and returns at once if it is clear.
- If a fiber leaves the M by parking or finishing before it reaches a safe point, `worker_main` clears the M's `preempt` and decrements `rw_preempt_flag`. A stale request never preempts the next fiber early.
- As in Go, a preempted fiber goes to the tail of globq.
- The slice is set by `RW_PREEMPT_MS` (default 10, range 0–60000); 0 means the monitor thread is not started.
- A safe point is an ordinary C function call, so LLVM treats the stack and callee-saved registers as consistent around it. There is no asynchronous (signal-based) preemption. A long stretch of straight-line code, or code that stays inside a runtime C function for a long time, is not interrupted. The main thread is never preempted.

---

## 4. I/O

### 4.1 Generic read / write / close

I/O uses generic `read` / `write` / `close` on fds, whatever the handle kind (`io.c`; Details: spec 15). There are no TCP-specific I/O functions.

| Builtin | Runtime function | Result |
|---|---|---|
| `read(fd, max)` | `void rw_read(rw_str *out, fd, max)` | the bytes read. Empty on EOF and on error; `last_error()` tells them apart |
| `write(fd, b)` | `int64_t rw_write(fd, b)` | bytes written (may be partial) or -1 |
| `write_all(fd, b)` | `int64_t rw_write_all(fd, b)` | `b.len` or -1. Retries partial writes, with one timeout deadline for the whole call (Details: spec 52) |
| `close(fd)` | `int64_t rw_close(fd)` | 0 or -1. Wakes waiters first with `rw_net_close_fd` |
| `file_open(path, mode)` | `int64_t rw_file_open(path, mode)` | fd or -1. mode is `r` / `w` / `a` |

An empty `write` on a datagram socket sends an empty datagram (Details: spec 69).

### 4.2 Routing rules

```
                    rw_read / rw_write(fd)
                             │
                   needs_offload(fd)?  ── fstat ──┐
                             │                    │
         ┌───────────────────┴───────────────┐    │
   socket, or                               anything else (regular files,
   FIFO with O_NONBLOCK                     blocking pipes / ttys / character devices,
         │                                  a stdin rw did not create, ...)
         ▼                                        ▼
   read(2)/write(2) directly                rw_aio_read / rw_aio_write
   on EAGAIN: rw_net_block                         │
         │                                  on a fiber? ─ yes → queue a task and park
   on a fiber? ─ yes → rw_net_wait (park)            │                (aio pool)
         │                                        no → synchronous read/write in place
         no → poll(2) in 50 ms slices (main thread)
```

- **Sockets** are always nonblocking and use the netpoller.
- **Nonblocking FIFOs** (the pipes rw creates for child processes) also use the netpoller. Sending them to the bounded aio pool could deadlock: a blocked read can wait for a write queued behind it (Details: spec 61).
- **Every other fd** would block in the kernel and pin the worker, so it goes to the aio pool (Details: spec 16, 25).
- `print` writes through C stdio on the calling thread. A full stdout pipe stops the worker (a Non-Goal of spec 25).

### 4.3 The netpoller

The netpoller is one dedicated thread that waits for OS event notifications (Details: spec 12).

| | macOS (`netpoller_kqueue.c`) | Linux (`netpoller_epoll.c`) |
|---|---|---|
| Wait | `kevent` | `epoll_wait` |
| Register | `EV_ADD \| EV_ONESHOT`, one knote per (fd, filter) | `EPOLLONESHOT \| EPOLLRDHUP`, try `EPOLL_CTL_MOD`, fall back to `ADD` |
| Direction | `EVFILT_READ` / `EVFILT_WRITE`. EOF and errors arrive on the same filter with flags set | `EPOLLIN/ERR/HUP/RDHUP` → read, `EPOLLOUT/ERR/HUP` → write |
| Shutdown wake-up | `EVFILT_USER` + `NOTE_TRIGGER` | `eventfd` |

Both register ONESHOT and call `rw_netpoller_ready(pd, mask)` for each notification. `rw_netpoller_ready` re-registers any interest that remains.

### 4.4 Poll descriptors and waiters

Each fd has one `rw_pollfd`, which is never freed once created. fd numbers are reused, and the descriptor with them.

```c
struct rw_pollfd {
    rw_wait_lock   lock;
    int            fd;
    uint64_t       next_id;
    rw_net_waiter *head[2];        /* waiters for [RW_NET_READ], [RW_NET_WRITE] */
    int64_t        timeout_ms[2];  /* per-direction timeout (spec 53) */
    int            closed;         /* closed by rw_close */
};
```

- Descriptors are allocated lazily in chunks of 1024. There are up to 4096 chunks, so fd numbers go up to about 4 million. Chunks are published with a CAS.
- `rw_net_waiter` is a node on the waiting fiber's stack holding `{id, g, timedout, closed, linked, next}`. `g` stays NULL until the park commit runs.
- Three parties touch a descriptor — the parking fiber, the netpoller thread, and the timer thread — and each takes `lock` first. So exactly one of "ready" and "timed out" resumes a waiter.

`rw_net_wait(fd, dir, deadline)` proceeds as follows.

```
1. Take lock. If closed, return RW_NET_CLOSED.
2. Give the waiter an id, push it on the list, release lock (g is still NULL).
3. If there is a deadline, register rw_timer_call_at(deadline, wait_timeout, pd, id<<1|dir).
4. rw_sched_park_commit(commit_wait)
     commit_wait (after ctx is saved): take lock
       timedout / closed / no longer linked → unlink and enqueue at once
       otherwise set w->g = self and register with kqueue/epoll (enqueue at once on failure)
5. After resuming: take lock, unlink if still linked.
   Return closed → RW_NET_CLOSED, timedout → RW_NET_TIMEDOUT, otherwise 0
```

A return of 0 means "may be ready" and can be spurious. Callers retry the syscall and wait again on another `EAGAIN`. `rw_netpoller_ready` leaves waiters that have not committed yet (`g == NULL`) on the list; they register for themselves in their commit.

### 4.5 Timeouts

`set_read_timeout` / `set_write_timeout` set the descriptor's `timeout_ms` (Details: spec 53). An I/O builtin computes its deadline with `rw_net_deadline` on the first `EAGAIN` and uses the same deadline for the rest of the call.

The timer callback `wait_timeout(pd, tag)` extracts the waiter id and direction from the tag and searches the list for that id under the lock.

- **Not found**: the wait has already ended; do nothing. Because the waiter is a node on a stack, searching by id rather than by pointer avoids touching the node of a finished wait (stack memory that is no longer valid).
- **Found with `g != NULL`** (parked): set `timedout = 1`, unlink it, and enqueue it.
- **Found with `g == NULL`** (not committed yet): only set `timedout = 1`. The commit sees it later and re-enqueues the fiber at once.

Every wake-up — ready, timeout, close — thus takes the form "only the side that takes `g` and sets it to NULL under the lock enqueues the fiber". The same fiber is never resumed twice.

The `dial_timeout` deadline covers connecting only, not name resolution.

### 4.6 Waking on close and rw_net_reset_fd

The kernel silently drops registrations of a closed fd, so fibers waiting on it would sleep forever. `rw_close` calls `rw_net_close_fd(fd)` before `close(2)`: it sets `closed = 1` on the descriptor and on every waiter in both directions and wakes them (waiters that have not committed notice in their commit). The woken operations fail with `EBADF`. This is how one fiber stops another's accept loop by closing the listening socket (Details: spec 57).

The `closed` mark and the timeouts belong to the fd number, so they must be cleared when the number is reused for a new handle. **Every runtime path that creates an fd must call `rw_net_reset_fd(fd)`.** Otherwise the new handle inherits the old handle's closed mark and timeouts. The current call sites are `rw_file_open`, `rw_tcp_listen`, `rw_tcp_accept`, `rw_dial_timeout` (and `connect_fd`), `rw_listen`, and `rw_proc_start` (the parent's three pipe ends).

### 4.7 The main-thread path

The main thread is not a fiber and cannot park. Each operation behaves as follows there.

| Operation | Behavior on the main thread |
|---|---|
| netpoller-based (`rw_net_block`) | `poll(2)` repeatedly in slices of at most 50 ms, checking the descriptor's `closed` each time, because a close on another thread does not wake `poll` |
| aio-based | synchronous `read` / `write` / function call in place |
| `connect` | blocking connect without a deadline; with a deadline, nonblocking plus `poll` |
| `sleep` | `nanosleep` (retried with the remaining time on `EINTR`) |
| `await` | sleep on the handle's `join_cv` |
| channels, mutexes | sleep on a per-waiter `pthread_cond_t` |
| `signal_wait` | sleep on the shared `g_cv` |

Calling `rw_net_wait` itself off a fiber `abort`s. Off-fiber code always goes through `rw_net_block`.

### 4.8 The aio pool

`aio.c` is a fixed-size thread pool that runs blocking syscalls on behalf of fibers (Details: spec 16).

```
fiber:                               aio thread:
  rw_aio_task t (on the fiber stack)   queue_pop()
  t.waiter = me                        run_task(t)
  park_commit(commit_task, &t) ──────▶   READ:  buf = rw_gc_alloc_atomic(max); read()
     commit: queue_push(&t)               WRITE: write()
                                          CALL:  t->fn(t->arg)
                                        t->err = errno
  after resume: rw_errno = t.err ◀──── rw_sched_enqueue_ready(t->waiter)
```

- The task lives on the parked fiber's stack. The fiber stays parked until completion, so the stack stays alive.
- The result is stored before `rw_sched_enqueue_ready`, and the resumed fiber reads it afterwards — the same ordering as the netpoller.
- `rw_aio_call(fn, arg)` runs an arbitrary function on the pool. Name resolution (`getaddrinfo`), the filesystem operations, and `waitpid` use it. Off a fiber it runs `fn(arg)` in place.
- Each fd blocked in the pool occupies one pool thread. More concurrent blocking reads than `RW_AIO_THREADS` (default 4) queue up behind each other.

### 4.9 Error handling: rw_errno and last_error

I/O builtins record 0 on success and an errno value on failure with `rw_set_last_error`; `last_error()` returns it (Details: spec 51).

- The value is stored **per fiber** (`rw_fiber_handle.last_error`). Fibers migrate between Ms, so it cannot be thread-local. Off a fiber (main thread), the thread-local `tls_last_error` is used.
- Name-resolution failure is `RW_ENOHOST` (10001), outside the errno range. `error_name` / `error_message` return a name and a description. errno numbers differ between OSes, so programs compare names.
- **Code that can park never touches `errno` directly; it uses `rw_errno`.** `rw_errno` is `(*rw_errno_ptr())`, where `rw_errno_ptr` is a non-const function in another translation unit (`sched.c`). glibc declares `__errno_location` const, which lets the compiler reuse an errno address computed before a park; after the fiber resumes on another thread, it would read the old thread's errno.
- For work run on the aio pool, the pool thread's errno is copied into `t.err` and restored into the caller's `rw_errno` after resuming.
- Public entry points follow one shape: `rw_errno = 0` on entry and a single `rw_set_last_error` on exit. Failure paths that call `close` and the like save and restore errno, as `close_keep_errno` does.

### 4.10 Address-based dial / listen / accept / open

`net/addr.c` provides common entry points that choose the handle kind from an address string (Details: spec 50, 69).

| Address | dial | listen | open |
|---|---|---|---|
| `tcp://host:port` | resolve and connect to each candidate in turn | all IPv4 interfaces when `host` is empty; otherwise resolve, bind + listen(128) | — |
| `udp://host:port` | connected UDP socket | bind only (nonblocking) | — |
| `unix:///path` | stream Unix-domain socket | bind + listen | — |
| `file:///path` or a bare path | — | — | `rw_file_open` |

- IPv6 literals are written `[v6]:port`. The port is at most five digits and at most 65535.
- Sockets from dial / accept are nonblocking. A listening socket is blocking when created; `tcp_accept_impl` makes it nonblocking on the first accept.
- On a fiber, or with a deadline, `connect_fd` does a nonblocking connect, waits until writable, and checks the result with `SO_ERROR`. A connect interrupted by `EINTR` keeps going in the background, so it waits for completion like a nonblocking connect instead of calling connect again (section 6.7).
- `recv_from` / `send_to` express the source and destination as `udp://` address strings. `send_to` resolves the destination and uses the first candidate whose address family matches the socket.
- `local_addr` / `peer_addr` return strings of the form `tcp://`, `udp://`, or `unix://`.
- Socket options (`shutdown`, `set_nodelay`, `set_keepalive`, `set_backlog`) return 0 or -1 and record `last_error` (Details: spec 56).

**Name resolution**: `getaddrinfo` blocks, so on a fiber it runs on the pool via `rw_aio_call` while the fiber parks (`resolve` → `do_resolve`). Failure sets `rw_errno = RW_ENOHOST`.

### 4.11 SIGPIPE

`rw_init` runs `signal(SIGPIPE, SIG_IGN)`. A write to a peer that hung up returns -1 / `EPIPE` instead of killing the process. On macOS, sockets from dial / accept also get `SO_NOSIGPIPE`. Child processes start with SIGPIPE back at its default disposition via `posix_spawnattr_setsigdefault` (Details: spec 51, 61).

---

## 5. Timers, synchronization, signals, processes, filesystem

### 5.1 Timers

`timer.c` owns one dedicated thread and a binary min-heap ordered by `(deadline, seq)` (Details: spec 39).

- An entry either wakes a fiber (`sleep`) or calls `fn(arg, tag)` (`rw_timer_call_at`, used by netpoller timeouts). Entries with equal deadlines fire in `seq` order (FIFO).
- `rw_sleep_ms` keeps the deadline on the fiber's stack and pushes it onto the heap in `commit_sleep`. Off a fiber it calls `nanosleep`.
- The timer thread pops due entries and wakes the fiber or calls the callback **after releasing the lock**. Callbacks must not block.
- Waiting uses `pthread_cond_timedwait`. On Linux the condition variable uses `CLOCK_MONOTONIC`. macOS lacks `pthread_condattr_setclock`, so the monotonic deadline is converted to `CLOCK_REALTIME`.
- The heap is C memory grown with `realloc`, outside the GC heap.
- `time_ms()` is the wall clock (Unix time in milliseconds); `mono_ns()` is the monotonic clock (nanoseconds).

### 5.2 Channels

A `Channel[T]` is a FIFO with capacity 0 (rendezvous) or N; its state is guarded by a per-channel `pthread_mutex_t` (Details: spec 44).

```
send:  a receiver is waiting   → copy the value straight into the receiver and wake it
       the buffer has room     → put it in the ring buffer
       otherwise               → push a sender waiter (on its own stack) and block
recv:  the buffer has a value  → take it; if a sender is waiting, move its value into the freed slot and wake it
       a sender is waiting     → take the value straight from the sender (rendezvous)
       closed                  → return 0
       otherwise               → push a receiver waiter and block
close: wake every waiting receiver and sender with ok = 0
```

- To block, a fiber parks with `rw_sched_park_commit(unlock_commit, c)`, and the commit unlocks the channel mutex. The waking side needs the mutex to complete a waiter, so it can never re-enqueue a half-saved fiber. Off a fiber, the caller sleeps on a per-waiter condition variable.
- A send on a closed channel, and a send whose channel was closed while it waited, `abort`.
- The buffer and the channel itself are scanned GC memory.

### 5.3 Mutexes

`Mutex` is a fiber-aware lock built on the same waiter machinery as channels (Details: spec 46).

- `unlock` hands the lock directly to the first waiter. There is no barging, so waiters acquire it in FIFO order.
- `unlock` on a mutex that is not locked `abort`s.
- `with m:` calls `mutex_lock` on entry and `mutex_unlock` on every exit path (section 7.5).

### 5.4 await_all / await_any

- `await_all(futures)` is a loop expanded by irgen: it `rw_await_<T>`s each Future in order and pushes the results onto a list.
- `await_any(futures)` finds the index of the first Future to finish with `rw_await_any`, `rw_await_<T>`s that Future, and returns `(index, value)` (Details: spec 45).

`rw_await_any` picks, among the finished Futures, the one with the smallest `finish_seq` — the one that actually finished first, not the one with the lowest index. `finish_seq` is a number that `finalize_fiber` draws from a global counter just before publishing `DONE`. A fiber cannot be on several wait lists at once, so if none has finished, `rw_await_any` polls with `rw_sleep_ms(1)` in between. On a fiber it parks during that time.

`await_any` awaits (and frees) only the chosen Future. The others are left as they are.

### 5.5 Signals

`signal_wait(names)` waits until one of the named signals (`SIGINT`, `SIGTERM`, `SIGHUP`, `SIGQUIT`, `SIGUSR1`, `SIGUSR2`) arrives and returns its name (Details: spec 57).

```
signal handler (SA_RESTART)            signal thread                         waiters
  write(self_pipe[1], signo) ───────▶ read(self_pipe[0])
  (saves and restores errno)            lock g_mu
                                        g_pending[sig]++
                                        take the whole waiter list
                                        broadcast g_cv (off-fiber waiters)
                                        unlock
                                        enqueue_ready each fiber ────────▶ resume and check pending
                                                                           for its own set; consume one
                                                                           or wait again
```

- The first call creates the self-pipe and a detached thread via `pthread_once`.
- The handler for a signal is installed with `sigaction` the first time that signal is waited for; from then on the signal no longer has its default action.
- A signal that arrives while nobody waits stays in `g_pending`, so none is lost between two waits.
- A fiber waiter joins the list in `commit_wait`, which also unlocks `g_mu`.

### 5.6 Child processes

`process.c` starts child processes with `posix_spawnp` (Details: spec 61). Running arbitrary code after `fork` in a multi-threaded process is not safe, hence `posix_spawnp`.

- `proc_start(argv)` returns `[pid, stdin_w, stdout_r, stderr_r]`. On failure it returns an empty list and records `last_error`.
- Pipe creation through spawning is serialized by `g_spawn_mu`. macOS has no portable `pipe2(O_CLOEXEC)`, so every pipe end gets `FD_CLOEXEC`. A child started by one fiber therefore never inherits the pipes another fiber is setting up. In the child, only the copies `dup2`'d onto 0/1/2 survive.
- The child starts with SIGPIPE and the signals rw may have handlers for reset to their defaults, and with an empty signal mask.
- The parent's three ends get `rw_net_reset_fd` and are then made nonblocking, so they are read and written through the netpoller (section 4.2).
- `proc_wait(pid)` runs `waitpid` on the aio pool (retrying on `EINTR`). It returns the exit status, 128 + the signal number if the child was killed by a signal, or -1 on error.
- `proc_kill(pid, sig)` sends a signal by name with `kill`.

### 5.7 Filesystem

The `fs.c` builtins (`fs_kind`, `fs_size`, `fs_mtime_ms`, `fs_read_dir`, `fs_rename`, `fs_remove`, `fs_mkdir`) run on the pool via `rw_aio_call` when called from a fiber (Details: spec 59), because directory and metadata syscalls also block on slow or network filesystems.

- All of them record `last_error`. On failure they return -1, an empty string, or an empty list.
- A path containing NUL is `EINVAL`.
- `fs_read_dir` returns a sorted list of names, allocated on the GC heap on the aio thread. This is one reason aio threads are registered with the GC.

---

## 6. Memory management

### 6.1 Why Boehm GC

rw values are managed by the Boehm-Demers-Weiser conservative collector (bdw-gc 8.0 or later) (Details: spec 67), for three reasons:

- The compiler does not have to emit type maps, stack maps, or write barriers.
- Interior pointers such as string slices work as they are.
- It supports threads.

The collector is wrapped by the functions in `rwgc.h`, and only `gc.c` includes `<gc/gc.h>`. `gc.c` defines `GC_THREADS` and `GC_NO_THREAD_REDIRECTS` before including it; threads are registered explicitly.

### 6.2 Kinds of allocation

| Function | GC treatment | What goes there |
|---|---|---|
| `rw_gc_alloc_atomic` | not scanned, not initialized | string bytes (concatenation, conversions, `read` buffers, `rand_bytes`, `str_from_*`), `List[int]` data, dict index arrays, `proc_start` results |
| `rw_gc_alloc` | scanned, zero-filled | generic list data, `str_split` results, dicts and their entries, channels and their buffers, mutexes, `os_args`, directory listings, records (irgen), spawn argument blocks (irgen), closure environments (irgen) |
| `rw_gc_alloc_uncollectable` | scanned but never collected; freed explicitly with `rw_gc_free` | fiber handles. They hold the result, the pointer to the argument block, and the registers saved at a switch |
| `rw_gc_realloc` / `rw_gc_free` | — | explicit reallocation (growing the `fs_read_dir` listing) and freeing (the buffer of a failed `read`, spawn argument blocks, fiber handles) |

All of them print `"rw: out of memory"` and `abort` when memory runs out.

### 6.3 What lives outside the GC heap

The following live outside the GC heap. None of them is the only reference to an rw value.

- C temporaries (NUL-terminated paths, argv for `posix_spawn`): `malloc` / `free`
- the netpoller descriptor table: `calloc`, never freed
- the timer heap: `realloc`
- fiber stacks: `mmap`
- aio tasks, netpoller waiters, channel waiters, signal waiters: on the waiting fiber's stack

### 6.4 Thread registration

| Thread | Registration |
|---|---|
| main | registered by `GC_INIT` in `rw_sched_init` → `rw_gc_init`. The GC must be initialized on the main thread before any worker |
| worker | `rw_gc_register_thread(&m->stack_base)` at the start of `worker_main`; unregistered on exit |
| aio pool | registered at the start of the aio `worker_main`; unregistered on exit |
| netpoller / timer / monitor / signal | not registered. They never touch the GC heap |

`rw_gc_register_thread` returns the GC's handle for the thread and the bottom (highest address) of its pthread stack. A worker keeps both in its `rw_M` and uses them at every switch.

### 6.5 Scanning fiber stacks

The GC scans only the stacks of threads it knows. Fibers run on their own `mmap`'d stacks, so two mechanisms make them visible.

1. **Running fibers**: at every switch the thread resets its stack bottom with `GC_set_stackbottom` (`rw_gc_switch_begin`) to the `fiber_stack_top` of the fiber about to run, or to the bottom of the worker's own pthread stack. The GC scans `[sp, bottom]` of whatever stack the thread is running on.
2. **Suspended fibers**: a `GC_set_push_other_roots` hook, chained after the default hook (which pushes thread stacks), walks the live-fiber list (`g_all`) and pushes `[saved sp, stack top]` of every unfinished fiber with `GC_push_all_eager` (`push_fiber_stacks`). For a fiber that is running, the saved sp is stale but still inside its stack, so the extra range is merely conservative.

The registers a fiber saved are in `ctx` inside its uncollectable handle. The handle itself is scanned, so pointers held only in registers are roots too.

### 6.6 Switches versus stop-the-world

A switch changes the stack pointer and the recorded stack bottom in two steps. If the GC stopped the world in between, it would scan a bogus range. `gc.c` therefore excludes the two with a "switching" counter.

```
rw_gc_switch_begin(handle, new_base)      ← on the old stack, right before rw_fiber_swap
    rw_gc_enter():  wait while g_stopping → g_switching++ → re-check g_stopping
    GC_set_stackbottom(handle, new_base)
rw_fiber_swap(...)
rw_gc_switch_end()                        ← on the new stack, right after the swap
    rw_gc_leave():  g_switching--

GC_EVENT_PRE_STOP_WORLD:   g_stopping = 1; sched_yield until g_switching is 0
GC_EVENT_POST_START_WORLD: g_stopping = 0
```

- Where `rw_gc_switch_end` is called depends on where the swap lands: worker_main (returning from a fiber), `rw_sched_yield` and `rw_sched_park_commit` (when the fiber resumes), and the start of a new fiber's entry function `fiber_entry_<T>`. `finalize_fiber` calls only begin; the matching end is in worker_main.
- A thread that calls begin while the GC has the world stopped waits until the world restarts.
- The switch path does not allocate, so it never needs the allocation lock held by the collecting thread, and cannot deadlock on it.
- The same exclusion (`rw_gc_enter` / `rw_gc_leave`) guards changes to the live-fiber list (`track_fiber` / `untrack_fiber`), so the hook never sees it half linked.

**Every call to rw_fiber_swap must be bracketed by `rw_gc_switch_begin` and `rw_gc_switch_end`.**

### 6.7 EINTR

On Linux, Boehm GC stops threads with signals. Its handlers use `SA_RESTART`, so most blocking syscalls restart automatically. Calls that can still return `EINTR` are retried explicitly by the runtime.

| Place | Handling |
|---|---|
| `poll` in `rw_net_block` | keep looping on `EINTR` |
| `waitpid` in `do_wait` | retry |
| `nanosleep` in `rw_sleep_ms` | retry with the remaining time |
| `connect` in `connect_fd` | an interrupted connect keeps going in the background, so wait until writable instead of calling connect again |
| `accept` / `recvfrom` / `sendto` | retry on `EINTR` |
| the kqueue / epoll loops, `read` in the signal thread | continue on `EINTR` |

When you add a blocking syscall to the runtime, decide how it handles `EINTR`.

### 6.8 Builtins

| Builtin | Runtime function | Behavior |
|---|---|---|
| `gc_collect()` | `rw_gc_collect` → `GC_gcollect` | run a full collection now |
| `gc_heap_size() -> int` | `rw_gc_heap_size` → `GC_get_heap_size` | size of the GC heap in bytes |

### 6.9 Finding the dependency

bdw-gc is a system dependency (`brew install bdw-gc` / `apt install libgc-dev`). `scripts/gc-prefix.sh` prints the first prefix containing `include/gc/gc.h`, searching in this order:

1. `$RW_GC_PREFIX` (used as is when set)
2. `pkg-config --variable=prefix bdw-gc`
3. `brew --prefix bdw-gc`
4. `/opt/homebrew/opt/bdw-gc`, `/usr/local/opt/bdw-gc`, `/usr/local`, `/usr`

`runtime/Makefile` uses it for the include path; `rwc` (`driver.gc_link_flags`) and the C tests use it for link flags.

---

## 7. Compiler-side guarantees

The runtime's correctness assumes that the IR irgen emits has a few properties.

### 7.1 Optimization levels

The default is `-O2` (Details: spec 70). The IR is safe to optimize because:

- Safe points read `rw_preempt_flag` with a monotonic atomic load, which LLVM cannot hoist out of a loop.
- irgen emits no `nsw` / `nuw`. Signed integer overflow wraps, and the optimizer never assumes it cannot happen.
- The conservative GC tolerates what the optimizer does with pointers (interior pointers, values held only in registers).

`-O0` is mainly for debugging the IR. In CI, one job runs the full suite with `RW_OPT_LEVEL=0`.

### 7.2 Safe-point placement

Safe points go at function entry, closure entry, and every loop head (section 3.8). Any non-terminating rw computation passes through one.

### 7.3 Checked integer division

Integer `/` and `%` are checked to avoid LLVM undefined behavior (Details: spec 42).

- Division by zero: `rw_panic("division by zero in <function> (line N)")`
- `INT64_MIN / -1`: the quotient does not fit, so `rw_panic("integer overflow in division ...")`
- `x % -1`: always 0 (avoids `srem`'s trap on `INT64_MIN % -1`)

`rwc build --unchecked` / `rwc run --unchecked` / `rwc emit-ir --unchecked` remove these checks.

### 7.4 Entry-block allocas

`FunctionCtx.alloca` places every stack slot at the start of the function's entry block. An alloca emitted where a local is declared would run on every loop iteration, grow the stack without bound, and quickly exhaust the 64 KiB fiber stack. Temporary slots for output pointers follow the same rule.

### 7.5 Cleanup stack for with / defer

`FunctionCtx.cleanups` is a stack of pending cleanups, innermost last (Details: spec 47).

- An entry is `("defer", stmt, depth)` or `("with", (value, type), depth)`, where depth is the loop depth when it was pushed.
- `return` emits all cleanups, innermost first, **after** evaluating the return value.
- `break` / `continue` emit only the cleanups pushed at or deeper than the current loop depth.
- Leaving a `with` block normally emits its own cleanup.
- If the `with` target is a `Mutex`, the cleanup calls `mutex_unlock`; for any other type it calls the `close` method.

Cleanups are only "emitted", never popped by those paths, because other paths still need to emit them. There is no exception mechanism, so cleanups do not run on paths that end in `abort` or `rw_panic`.

---

## 8. Checklist for extending the runtime

When adding a builtin or a subsystem, check the following.

**Adding a builtin**

- [ ] Add the name to `_BUILTIN_FUNCS` in `rwc/sema.py`. For a simple signature, add `(param types, return type)` to `_SIMPLE_BUILTIN_SIGS`; otherwise write a dedicated type check in sema
- [ ] Declare the C function in `_declare_runtime` in `rwc/irgen.py`, and add it to the `direct` table in `_emit_call` if the arguments are passed through unchanged. Write a dedicated branch if it uses lists or an output pointer
- [ ] Add the prototype to `runtime/runtime.h` (or the subsystem header if it is internal)
- [ ] When you add a new `.c` file, add it to `OBJS` and the dependency rules in `runtime/Makefile`
- [ ] Do not pass structs of 24 bytes or more by value; use pointers and output pointers (section 2.10)

**fds and memory**

- [ ] Every path that creates an fd calls `rw_net_reset_fd(fd)` before a fiber can use it (section 4.6)
- [ ] Call `rw_set_nonblocking` on sockets and on fds that should be nonblocking. fds left blocking go to the aio pool
- [ ] Allocate memory that becomes an rw value with `rw_gc_alloc*`: `rw_gc_alloc_atomic` for pointer-free data, `rw_gc_alloc` otherwise. C temporaries may use `malloc` / `free`
- [ ] Never keep an rw value only where the GC cannot see it (`malloc`'d memory, variables of unregistered threads, ...)

**Threads**

- [ ] A new thread that touches the GC heap registers with `rw_gc_register_thread` and calls `rw_gc_unregister_thread` on exit
- [ ] Fit the new thread's startup and shutdown into the `rw_init` / `rw_shutdown` order
- [ ] Wake fibers from other threads with `rw_sched_enqueue_ready`

**Fibers and blocking**

- [ ] Bracket every `rw_fiber_swap` with `rw_gc_switch_begin` / `rw_gc_switch_end`
- [ ] Stop a fiber with `rw_sched_park_commit`, and publish it to wakers (register, enqueue, unlock) inside the commit. If the commit cannot arrange a wake-up, it calls `rw_sched_enqueue_ready(self)` itself
- [ ] Never call a blocking syscall directly on a worker: use `rw_net_block` for nonblocking fds and `rw_aio_call` for everything else
- [ ] Decide the off-fiber (main thread) behavior (the `rw_sched_current_fiber() == NULL` branch)
- [ ] If wake-ups can come from several paths (ready, timeout, close, ...), make "only the side that takes the fiber under the lock enqueues it" hold

**Errors**

- [ ] Public functions call `rw_set_last_error` on both success and failure
- [ ] Code that can park uses `rw_errno`, not `errno`
- [ ] For work run on the aio pool, copy errno into the task and restore it on the caller's side
- [ ] Decide how `EINTR` is handled (section 6.7)

**Tests**

- [ ] Write C-level tests in `runtime/fiber/test_*.c` and register them in `C_TESTS` in `tests/test_runtime_c.py`
- [ ] Check rw-level behavior with `examples/*.rw` plus `.rw.expected`, or with `tests/test_e2e_*.py`
- [ ] Run changes that involve races many times with `RW_WORKERS` of 2 or more

---

## 9. Tests and debugging

### 9.1 Test layout

| Location | Contents |
|---|---|
| `runtime/fiber/test_*.c` | tests that drive the runtime directly from C |
| `tests/test_runtime_c.py` | runs `make -C runtime`, then builds and runs the C tests listed in `C_TESTS`: `test_detach`, `test_shutdown`, `test_preempt`, `test_park_race`, `test_dict`, `test_timer`, `test_chan`, `test_mutex`, `test_await_any`, `test_net_timeout` |
| `tests/test_e2e.py` | compiles and runs `examples/*.rw` and compares with `.rw.expected`. Runs with `RW_WORKERS=1` for deterministic output, with a timeout so a scheduling bug fails instead of hanging |
| `tests/test_e2e_*.py` | e2e tests for servers, timeouts, GC, processes, UDP, and more. `tests/_net.py` has helpers that make a program listen on port 0 and print the bound address |
| `tests/test_irgen.py` and others | unit tests for each compiler stage |

`runtime/fiber/` also contains C tests that are not in `C_TESTS`, such as `test_runq`, `test_steal`, `test_sched`, `test_pingpong`, `test_wait`, `test_c10k`, `test_tcp_loopback`, `test_netpoller_pipe`, `test_list_int`, `test_option`, and `test_str_ops`. pytest does not run them; build them by hand (the compile command is the same as in `tests/test_runtime_c.py`).

Common commands:

```sh
make -C runtime
uv run --frozen pytest tests/test_runtime_c.py -q
uv run --frozen pytest -q
```

### 9.2 Environment variables

Each of these variables has been confirmed to be read by the code.

| Variable | Read in | Default | Meaning |
|---|---|---|---|
| `RW_WORKERS` | `sched.c` `parse_workers` | number of online CPUs | number of workers (1–64). Out-of-range or non-numeric values are ignored |
| `RW_PREEMPT_MS` | `sched.c` `parse_preempt_ns` | 10 | preemption slice (0–60000 ms). 0 disables it (no monitor thread) |
| `RW_AIO_THREADS` | `aio.c` `rw_aio_init` | 4 | number of aio pool threads (1–64) |
| `RW_OPT_LEVEL` | `driver.py` `resolve_opt_level` | 2 | optimization level (0–3) when `-O` is not given |
| `RW_GC_PREFIX` | `scripts/gc-prefix.sh` | auto-detected | install prefix of bdw-gc |

The first three take effect at run time (in the generated program); the last two at compile time (in `rwc` and `make`).

### 9.3 Debugging hints

- **Make runs reproducible**: `RW_WORKERS=1` removes parallelism and `RW_PREEMPT_MS=0` stops preemption, which nearly fixes the execution order. Conversely, to hunt races, raise `RW_WORKERS` and run repeatedly.
- **Read the IR**: `rwc emit-ir x.rw` prints the generated IR. Building with `RW_OPT_LEVEL=0` (or `-O0`) keeps the code you see in a debugger close to the IR.
- **SIGSEGV near PC=0, hangs, or the same fiber running twice**: suspect a publication-order bug (publish before save). Check whether a new parking path publishes the fiber outside the commit of `rw_sched_park_commit` (section 3.7).
- **SIGSEGV near a guard page**: the 64 KiB fiber stack overflowed. Look for deep recursion or allocas emitted outside the entry block.
- **Values corrupted after a collection**: check for pointers stored in unscanned memory (`malloc`, atomic blocks), unregistered threads touching the GC heap, and `rw_fiber_swap` calls not bracketed by `rw_gc_switch_begin/end`.
- **I/O that never returns**: check that the path that created the fd calls `rw_net_reset_fd`, and that no blocking fd is sent to the netpoller.
- **Leaked fibers**: in C tests, check that `rw_sched_live_fibers()` returns to 0.

---

## Appendix: differences from specs and comments

Specs that disagree with the code, and source comments that are out of date. The main text follows the code.

| Where | What it says | Current code |
|---|---|---|
| spec 12 | `tcp_accept` on the main thread does a blocking accept that sleeps in the kernel | always a nonblocking accept; on the main thread, `poll` in 50 ms slices via `rw_net_block` (changed in spec 57). The header comment of `net/tcp.c` still describes the old behavior |
| spec 05 | the usable stack "grows from low to high" | sp starts at the highest address of the region and grows downward |
| spec 05 / 06 | handles are `calloc`'d and `free`d after join | allocated with `rw_gc_alloc_uncollectable` and `rw_gc_free`d in `free_handle` (spec 67) |
| spec 16, `aio.h` | read buffers are `malloc`'d | `rw_gc_alloc_atomic` (spec 67) |
| spec 20 | the constructor uses `malloc` | `rw_gc_alloc` |
| spec 25 | every non-socket fd goes to the aio pool | nonblocking FIFOs use the netpoller (as spec 61's update says) |
| spec 23 | safe points at function entry and loop heads (`for` desugars to `while`) | range `for` is desugared, but `for x in list` and `for` over a channel are emitted separately by irgen, each with its own safe point. Closure entry has one too |
| `sched.h`, top of `sched.c`, `runq.h` | "one OS thread, cooperative, no locks needed", "single-threaded in Commit 2" | M:N with multiple workers and work stealing |
| `sched.h` | the spawn `arg` is a `malloc`'d struct | irgen allocates it with `rw_gc_alloc`; the trampoline `rw_gc_free`s it |
| top of `gc.c` | suspended fibers are pushed as `[sp, top]` plus their saved registers | the hook pushes only the stack range; saved registers are picked up by scanning the uncollectable handle (same effect) |
| `tests/test_runtime_c.py` | — | only 10 of the 22 `runtime/fiber/test_*.c` files are registered |
