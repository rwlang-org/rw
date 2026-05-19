#ifndef RW_RUNQ_H
#define RW_RUNQ_H
/*
 * Go-style bounded ring buffer for ready fibers, plus a mutex-protected
 * global overflow queue.
 *
 * Why Go-style instead of a Chase-Lev deque?
 *   - The protocol fits on one page.
 *   - Owner pushes/pops are relaxed atomics; only stealers do a CAS.
 *   - Overflow has somewhere to go (the global queue), and `runq_grab`
 *     is a single CAS that takes up to half the queue.
 *
 * Invariants:
 *   - The owner P is the only thread that calls `rw_runq_put` and
 *     `rw_runq_get`. They use relaxed loads/stores on the local head/tail.
 *   - Any thread may call `rw_runq_grab` against any P (work-stealing).
 *     Grab is the only operation that competes with the owner; it uses
 *     a CAS on `head`.
 *   - `tail - head` is the count of items in the ring; this difference
 *     is always in [0, RW_RUNQ_CAP]. Producers (owner) must never push
 *     when full; on a full local queue the owner moves half + the new
 *     item to globq atomically (see `rw_runq_put`).
 *
 * In Commit 2 we run this single-threaded: only one P exists and only
 * the main thread touches it. The ring is still safe; we just won't
 * exercise the contention paths until Commit 5-6.
 */

#include <stdatomic.h>
#include <stdint.h>
#include <pthread.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct rw_fiber_handle rw_fiber_handle;

#define RW_RUNQ_CAP 256u

typedef struct rw_P {
    /* Owner-only counters. tail is bumped by `rw_runq_put`, head by
     * `rw_runq_get` (owner) or `rw_runq_grab` (stealer, via CAS). */
    _Atomic uint32_t head;
    _Atomic uint32_t tail;
    rw_fiber_handle *ring[RW_RUNQ_CAP];
} rw_P;

/* Mutex-protected linked-list of overflow / cross-P fibers. */
typedef struct rw_globq {
    pthread_mutex_t  mu;
    rw_fiber_handle *head;
    rw_fiber_handle *tail;
    uint32_t         count;
} rw_globq;

void rw_runq_init(rw_P *p);

/* Push g onto p's tail.
 *
 * If the local ring is full, the owner moves half of its local queue
 * (RW_RUNQ_CAP/2 items) plus g into `globq` in one shot, so the local
 * queue stays bounded and the global queue absorbs the burst.
 *
 * Owner-only. */
void rw_runq_put(rw_P *p, rw_fiber_handle *g, rw_globq *globq);

/* Pop one fiber from p's head. Returns NULL if empty. Owner-only. */
rw_fiber_handle *rw_runq_get(rw_P *p);

/* Steal up to half of the items currently in `src` into out[]. Returns
 * the number stolen (0..max). This is the only operation that competes
 * with the owner; it uses a CAS on src->head.
 *
 * Safe to call from any thread. */
uint32_t rw_runq_grab(rw_P *src, rw_fiber_handle **out, uint32_t max);

/* Global queue ops. Internally lock the mutex; safe from any thread. */
void rw_globq_init(rw_globq *q);
void rw_globq_destroy(rw_globq *q);
void rw_globq_push(rw_globq *q, rw_fiber_handle *g);
rw_fiber_handle *rw_globq_pop(rw_globq *q);
/* Pop up to `max` fibers in one critical section. Returns count moved. */
uint32_t rw_globq_pop_batch(rw_globq *q, rw_fiber_handle **out, uint32_t max);
/* Push a batch (already linked via ->next) of `n` fibers. Owner of the
 * batch must have set up the ->next chain; this just splices. */
void rw_globq_push_batch(rw_globq *q, rw_fiber_handle *head,
                         rw_fiber_handle *tail, uint32_t n);

#ifdef __cplusplus
}
#endif

#endif /* RW_RUNQ_H */
