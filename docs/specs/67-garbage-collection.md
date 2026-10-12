# 67. Garbage collection (Boehm GC)

Issue: #67

## Motivation

The runtime never freed strings, lists, records, or dicts. A batch program
could live with that. A server could not: with keep-alive (spec 64), one
process handles requests forever, and it leaked a few KiB per request
(about 40 MiB per 5000 requests in the web e2e test).

## Choice

The Boehm-Demers-Weiser conservative collector (bdw-gc ≥ 8.0), linked into
every program:
- It needs no type maps, stack maps, or barriers from the compiler.
- Interior pointers (string slices) work out of the box.
- It handles threads.

Reference counting (cycles, and a lot of irgen work) and per-fiber arenas
(values escape fibers through `await`, records, and channels) were the
alternatives.

It is a system dependency: `brew install bdw-gc` / `apt install
libgc-dev`. `scripts/gc-prefix.sh` finds it through pkg-config, brew, or the
usual prefixes, and `RW_GC_PREFIX` overrides the search. The Makefile adds
its include directory; `rwc` and the C tests link `-lgc`.

## What lives on the GC heap

| Memory | Allocation |
|---|---|
| String bytes (`str_alloc`, concat, conversions, read buffers, `rand_bytes`), `List[int]` data, dict index arrays, `proc_start` results | `GC_MALLOC_ATOMIC`: never scanned |
| Generic list data, split pieces, dicts and their entries, channels and their buffers, mutexes, `os_args`, directory listings, records, spawn argument blocks (irgen) | `GC_MALLOC`: scanned |
| Fiber handles | `GC_MALLOC_UNCOLLECTABLE`: scanned, freed explicitly when reaped. The handle holds the result, the argument block, and the registers saved at a switch |

C temporaries (NUL-terminated paths, argv for `posix_spawn`), the netpoller
descriptor table, timer entries, and fiber stacks (mmap) stay outside the GC
heap. None of them is the only reference to an rw value.

`rw_gc_alloc` and friends (`runtime/rwgc.h`) wrap the collector; only
`runtime/gc.c` includes `<gc/gc.h>`.

## Fibers

The collector scans the stacks of the threads it knows. Fibers run on their
own 64 KiB mmap'd stacks, so:

1. **Threads**:
   - The main thread is registered by `GC_INIT` (from `rw_sched_init`).
   - Worker threads and aio pool threads register themselves when they
     start, because the pool allocates read buffers and directory listings.
   - The netpoller, timer, monitor, and signal threads never touch the GC
     heap and stay unregistered.
2. **Running fibers**: at every context switch the thread updates its stack
   bottom (`GC_set_stackbottom`) to the stack it is about to run on: the
   fiber's top, or the worker's own pthread stack. The collector then scans
   `[sp, top]` of whatever the thread is running.
3. **Suspended fibers**: a `GC_set_push_other_roots` hook, chained after the
   default one that pushes thread stacks, walks the list of live fibers and
   pushes `[saved sp, stack top]` of every unfinished fiber. For a fiber
   that is running, the saved sp is stale but still inside its stack, so the
   extra range is only conservative.
4. **Switch versus stop-the-world**: a switch moves the stack pointer and the
   recorded stack bottom in two steps, and a collection must not stop the
   world in between. `rw_gc_switch_begin` / `rw_gc_switch_end` bracket every
   `rw_fiber_swap` with a shared "switching" count:
   - Begin runs on the old stack and end on the new one: worker_main after
     resuming, the fiber after its swap returns, or a fresh fiber's entry
     function.
   - On `GC_EVENT_PRE_STOP_WORLD` the collector sets a "stopping" flag and
     waits for the count to drain.
   - New switches spin until `GC_EVENT_POST_START_WORLD` clears the flag.
   - The same exclusion (`rw_gc_enter` / `rw_gc_leave`) guards changes to the
     live-fiber list, so the hook never sees it half linked.
   - The switch path does not allocate, so it never needs the allocation
     lock that the collecting thread holds.

### Interrupted syscalls

On Linux the collector stops threads with signals. Its handlers use
`SA_RESTART`, and the runtime already retries `EINTR` around `poll`,
`waitpid`, and `nanosleep`. A blocking `connect` on the main thread was the
one call that would fail after `EINTR`; it now waits for the connection to
complete, like a nonblocking one.

## Builtins

| Builtin | Result |
|---|---|
| `gc_collect()` | run a full collection now |
| `gc_heap_size() -> int` | the GC heap size in bytes |

## Tests

`tests/test_e2e_gc.py`:
- 200 fibers each build records, lists, and dicts, then park (sleep, then a
  channel receive) while four fibers churn garbage and force collections.
  Every fiber's data is intact afterwards, and so are 50 records waiting in a
  channel buffer. Run three times.
- Building 100 MB of temporary strings keeps the heap under 64 MiB.
- The web app's RSS grows less than 10 MiB over 5000 keep-alive requests;
  it was about 40 MiB without the collector.

All existing tests run with the collector.
