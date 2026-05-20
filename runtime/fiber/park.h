#ifndef RW_PARK_H
#define RW_PARK_H
/*
 * Per-handle wait list, used when a fiber on a worker thread awaits
 * another fiber.
 *
 * The "park" path (fiber-internal await) looks like:
 *
 *   joiner:                                  target trampoline:
 *   -------                                  ------------------
 *   acquire spinlock
 *   if state == DONE: unlock; return         <runs to completion>
 *   set joiner->state = WAITING
 *   push joiner onto target->waiters
 *   unlock spinlock
 *   yield                                    publish result
 *                                            atomic_store(state, DONE, rel)
 *                                            broadcast join_cv
 *                                            acquire spinlock
 *                                            atomically take waiter list
 *                                            unlock spinlock
 *                                            for w in list: w->state = READY
 *                                                           runq_put(w)
 *                                            swap to sched_ctx
 *
 *   <eventually resumed by worker pulling
 *    us off the run queue>
 *   loop: re-check state, return result
 *
 * The spinlock keeps the "decide whether to park" and the trampoline's
 * "take the wait list" mutually exclusive. The release-store of state =
 * DONE happens-before the spinlock acquire on the trampoline side
 * (program order); the joiner that loses the race reads DONE under the
 * lock and never parks. The joiner that wins parks before the
 * trampoline grabs the list, so it is on the list when the trampoline
 * takes it.
 *
 * The spinlock is implemented with `atomic_flag` because the critical
 * section is three pointer operations and macOS lacks
 * pthread_spinlock_t.
 */

#include <stdatomic.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct rw_fiber_handle rw_fiber_handle;

typedef struct rw_wait_lock {
    atomic_flag flag;
} rw_wait_lock;

static inline void rw_wait_lock_init(rw_wait_lock *l) {
    atomic_flag_clear_explicit(&l->flag, memory_order_relaxed);
}

void rw_wait_lock_acquire(rw_wait_lock *l);
void rw_wait_lock_release(rw_wait_lock *l);

#ifdef __cplusplus
}
#endif

#endif /* RW_PARK_H */
