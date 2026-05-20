/*
 * init / shutdown cycle test.
 *
 * Repeatedly init the scheduler, spawn + join a handful of fibers,
 * shut it down. If anything leaks (pthread handle, mmap region,
 * mutex/condvar) or deadlocks, this catches it long before a real
 * program would. We also sweep RW_WORKERS values so we exercise both
 * the single-M and multi-M paths.
 */

#include "sched.h"
#include "../runtime.h"

#include <assert.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int64_t triv(void *arg) {
    return (int64_t)(intptr_t)arg;
}

static void one_cycle(int n_fibers) {
    rw_sched_init();
    const int N = n_fibers;
    rw_fiber_handle **fs = calloc(N, sizeof(*fs));
    for (int i = 0; i < N; i++) {
        fs[i] = rw_sched_spawn_i64(triv, (void *)(intptr_t)(i + 1));
    }
    int64_t total = 0;
    for (int i = 0; i < N; i++) {
        total += rw_sched_join_i64(fs[i]);
    }
    free(fs);
    int64_t expected = (int64_t)N * (N + 1) / 2;
    assert(total == expected);
    rw_sched_shutdown();
}

int main(void) {
    /* Default RW_WORKERS, many cycles. */
    for (int i = 0; i < 10; i++) {
        one_cycle(50);
    }
    printf("default RW_WORKERS: 10 cycles OK\n");

    /* RW_WORKERS=1 stress: tiny scheduler, lots of cycles. */
    setenv("RW_WORKERS", "1", 1);
    for (int i = 0; i < 20; i++) {
        one_cycle(20);
    }
    printf("RW_WORKERS=1: 20 cycles OK\n");

    /* RW_WORKERS=8. */
    setenv("RW_WORKERS", "8", 1);
    for (int i = 0; i < 5; i++) {
        one_cycle(200);
    }
    printf("RW_WORKERS=8: 5 cycles OK\n");

    return 0;
}
