/*
 * Safe-point preemption test (spec 23).
 *
 * On a single worker, a fiber spins until a flag is set, passing only
 * through safe points (what the compiler emits at loop heads). The flag is
 * set by a second fiber, which can only run if the spinner is preempted.
 * Without preemption the join below never returns.
 */

#include "sched.h"
#include "../runtime.h"

#include <assert.h>
#include <inttypes.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>

static _Atomic int g_done = 0;

static int64_t spinner(void *arg) {
    (void)arg;
    int64_t n = 0;
    while (!atomic_load_explicit(&g_done, memory_order_relaxed)) {
        n++;
        if (atomic_load_explicit(&rw_preempt_flag, memory_order_relaxed)) rw_safepoint();
    }
    return n;
}

static int64_t setter(void *arg) {
    (void)arg;
    atomic_store(&g_done, 1);
    return 1;
}

static double now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1e3 + ts.tv_nsec / 1e6;
}

int main(void) {
    setenv("RW_WORKERS", "1", 1);
    setenv("RW_PREEMPT_MS", "10", 1);
    for (int round = 0; round < 5; round++) {
        atomic_store(&g_done, 0);
        rw_sched_init();
        double t0 = now_ms();
        rw_fiber_handle *a = rw_sched_spawn_i64(spinner, NULL);
        rw_fiber_handle *b = rw_sched_spawn_i64(setter, NULL);
        int64_t spins = rw_sched_join_i64(a);
        assert(rw_sched_join_i64(b) == 1);
        double ms = now_ms() - t0;
        assert(spins > 0);
        /* One slice (10 ms) plus monitor granularity; allow generous slack. */
        if (ms > 1000.0) {
            fprintf(stderr, "round %d: preemption took %.1f ms\n", round, ms);
            return 1;
        }
        rw_sched_shutdown();
    }
    printf("test_preempt: OK\n");
    return 0;
}
