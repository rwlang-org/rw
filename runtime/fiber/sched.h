#ifndef RW_SCHED_H
#define RW_SCHED_H
/*
 * Minimal cooperative scheduler on top of rw_fiber_swap.
 *
 * Design:
 *   - One OS thread, one scheduler context. All fibers run on the same
 *     thread, so no locks are needed for the run queue.
 *   - Each fiber owns an mmap'd stack region with a guard page on
 *     either side (so stack overflow becomes SIGSEGV, not silent
 *     corruption).
 *   - rw_sched_spawn(entry, arg) creates a new fiber, pushes it onto
 *     the ready queue, and returns its handle. The new fiber does NOT
 *     run immediately - it will be picked up by the scheduler later.
 *   - rw_sched_yield() saves the current fiber, picks the next ready
 *     one, and swaps to it. The "scheduler" itself runs as a normal
 *     fiber (well: the main thread's context); ready fibers swap back
 *     to it on yield, and the scheduler picks the next ready fiber.
 *   - rw_sched_join_*(h) yields repeatedly until fiber `h` has finished,
 *     then returns its captured return value.
 *
 * Result types: spawn / join come in flavors for each return type used
 * by rw's runtime (i64, f64, bool, str, void), mirroring the existing
 * rw_spawn_* / rw_await_* ABI.
 */

#include <stdint.h>

#include "../runtime.h"  /* for rw_str */

#ifdef __cplusplus
extern "C" {
#endif

typedef struct rw_fiber_handle rw_fiber_handle;

/*
 * One-time scheduler initialization. Called from rw_init().
 */
void rw_sched_init(void);

/*
 * Shutdown - free any resources still around. Called from rw_shutdown().
 */
void rw_sched_shutdown(void);

/*
 * Cooperative yield. Returns when the scheduler resumes us.
 */
void rw_sched_yield(void);

/*
 * Typed spawn helpers. Each creates a fiber wrapping `fn(arg)`, schedules
 * it, and returns an opaque handle.
 *
 * The fiber owns the `arg` pointer until `fn` returns. In rw's IR-gen,
 * `arg` is a malloc'd closure struct freed by the trampoline; the
 * scheduler does not touch it.
 */
rw_fiber_handle *rw_sched_spawn_i64 (int64_t (*fn)(void *), void *arg);
rw_fiber_handle *rw_sched_spawn_f64 (double  (*fn)(void *), void *arg);
rw_fiber_handle *rw_sched_spawn_bool(int8_t  (*fn)(void *), void *arg);
rw_fiber_handle *rw_sched_spawn_str (rw_str  (*fn)(void *), void *arg);
rw_fiber_handle *rw_sched_spawn_void(void    (*fn)(void *), void *arg);

/*
 * Typed join helpers. Yield until the fiber finishes, then return the
 * captured value. Free the handle.
 */
int64_t rw_sched_join_i64 (rw_fiber_handle *h);
double  rw_sched_join_f64 (rw_fiber_handle *h);
int8_t  rw_sched_join_bool(rw_fiber_handle *h);
rw_str  rw_sched_join_str (rw_fiber_handle *h);
void    rw_sched_join_void(rw_fiber_handle *h);

/* ---- Exported for the netpoller (runtime/net/netpoller.c) ---- */

/* Move the given fiber handle to the ready queue. Safe to call from
 * any thread (uses the same mutex/cv as spawn). */
void rw_sched_enqueue_ready(rw_fiber_handle *h);

/* Return the fiber handle currently running on this worker thread,
 * or NULL if the calling thread is not a worker (main / netpoller). */
rw_fiber_handle *rw_sched_current_fiber(void);

/* Mark the current fiber as WAITING and swap out to the scheduler.
 * The fiber will NOT be re-enqueued by the scheduler; someone must
 * call rw_sched_enqueue_ready(handle) to wake it later. Used by the
 * netpoller to park fibers on fd readiness. */
void rw_sched_park_current(void);

#ifdef __cplusplus
}
#endif

#endif /* RW_SCHED_H */
