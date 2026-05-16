/*
 * Smoke test for the fiber scheduler.
 *
 *   - Spawn N fibers, each squaring an integer captured via its arg.
 *   - Join them all and sum the results. Compare against the
 *     closed-form sum.
 *
 * Build:
 *   cc -O2 -Wall -Wextra -std=c11 -c fiber.c     -o fiber.o
 *   cc -O2 -Wall -Wextra        -c fiber_arm64.S -o fiber_arm64.o
 *   cc -O2 -Wall -Wextra -std=c11 -c sched.c     -o sched.o
 *   cc -O2 -Wall -Wextra -std=c11 test_sched.c fiber.o fiber_arm64.o sched.o -o test_sched
 *   ./test_sched
 */

#include "sched.h"
#include "../runtime.h"

#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>

static int64_t squarer(void *arg) {
    int64_t n = (int64_t)(intptr_t)arg;
    /* Yield once to exercise scheduling. */
    rw_sched_yield();
    return n * n;
}

int main(void) {
    rw_sched_init();

    const int N = 1000;
    rw_fiber_handle **fs = calloc(N, sizeof(*fs));
    for (int i = 0; i < N; i++) {
        fs[i] = rw_sched_spawn_i64(squarer, (void *)(intptr_t)(i + 1));
    }

    int64_t total = 0;
    for (int i = 0; i < N; i++) {
        total += rw_sched_join_i64(fs[i]);
    }
    free(fs);

    /* Sum of squares from 1 to N = N*(N+1)*(2N+1)/6 */
    int64_t expected = (int64_t)N * (N + 1) * (2 * N + 1) / 6;
    printf("total    = %" PRId64 "\n", total);
    printf("expected = %" PRId64 "\n", expected);

    rw_sched_shutdown();
    return total == expected ? 0 : 1;
}
