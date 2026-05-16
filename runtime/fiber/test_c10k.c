/*
 * "C10k smoke test": spawn 10,000 fibers, each does a trivial bit of
 * work, yields once, then returns. The full process must:
 *
 *   - not crash on pthread_create-style EAGAIN (we're not using pthreads)
 *   - finish in well under a second on a modern laptop
 *   - sum the results correctly
 */

#include "sched.h"
#include "../runtime.h"

#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>

static int64_t worker(void *arg) {
    int64_t n = (int64_t)(intptr_t)arg;
    rw_sched_yield();
    return n;
}

int main(void) {
    rw_sched_init();

    const int N = 100000;
    rw_fiber_handle **fs = calloc(N, sizeof(*fs));
    if (!fs) { perror("calloc"); return 1; }

    struct timespec t0, t1;
    clock_gettime(CLOCK_MONOTONIC, &t0);

    for (int i = 0; i < N; i++) {
        fs[i] = rw_sched_spawn_i64(worker, (void *)(intptr_t)(i + 1));
    }
    int64_t total = 0;
    for (int i = 0; i < N; i++) {
        total += rw_sched_join_i64(fs[i]);
    }

    clock_gettime(CLOCK_MONOTONIC, &t1);
    double ms = (t1.tv_sec - t0.tv_sec) * 1000.0
              + (t1.tv_nsec - t0.tv_nsec) / 1.0e6;

    free(fs);
    rw_sched_shutdown();

    int64_t expected = (int64_t)N * (N + 1) / 2;
    printf("N=%d total=%" PRId64 " expected=%" PRId64 " elapsed=%.1fms\n",
           N, total, expected, ms);
    return total == expected ? 0 : 1;
}
