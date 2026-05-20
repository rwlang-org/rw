/*
 * Implementation of the tiny atomic_flag spinlock used as the
 * per-handle wait-list lock. See park.h for the protocol.
 *
 * The critical sections this guards are O(1) pointer ops (push a
 * waiter onto a list, or take the whole list). Backoff on contention
 * is sched_yield(); the lock is uncontended on the common path.
 */

#include "park.h"

#include <sched.h>

void rw_wait_lock_acquire(rw_wait_lock *l) {
    while (atomic_flag_test_and_set_explicit(&l->flag, memory_order_acquire)) {
        sched_yield();  /* not the fiber yield: this is the OS-level hint */
    }
}

void rw_wait_lock_release(rw_wait_lock *l) {
    atomic_flag_clear_explicit(&l->flag, memory_order_release);
}
