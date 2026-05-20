/*
 * Work-stealing parallel speedup test (Commit 6).
 *
 * Strategy: from a single "primer" fiber, spawn N CPU-bound child
 * fibers. The primer runs on some worker M0, so all N children land
 * on M0's local P. Then main awaits each child.
 *
 * Without stealing, only M0 makes progress and elapsed time is O(N).
 * With stealing, every other worker quickly grabs half of M0's
 * queue, then halves of those, etc. Elapsed time approaches
 * O(N / nworkers).
 *
 * We don't assert a fixed speedup ratio (system load is noisy);
 * we just print the elapsed time at each RW_WORKERS value the caller
 * sets. A regression-friendly correctness assertion lives at the end
 * (the result must be deterministic regardless of stealing).
 *
 * Run with:
 *   RW_WORKERS=1 ./test_steal     # baseline
 *   RW_WORKERS=4 ./test_steal     # expect ~3-4x faster on a multi-core box
 */

#include "sched.h"
#include "../runtime.h"

#include <assert.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>

/* Sink to keep the compiler from dead-code-eliminating cpu_burn's
 * loop. Each iteration's result is folded into a volatile global, so
 * the loop must execute. */
static volatile int64_t g_sink = 0;

/* Burn CPU for a while. We don't yield, so each fiber runs to
 * completion on whatever M picks it up. */
static int64_t cpu_burn(void *arg) {
    int64_t seed = (int64_t)(intptr_t)arg;
    int64_t x = seed;
    for (int i = 0; i < 2000000; i++) {
        x = x * 1103515245 + 12345;
        x ^= (x >> 17);
    }
    g_sink ^= x;
    return seed;
}

/* Primer fiber: spawns N CPU-bound children all onto its own M's P,
 * then joins them in order. Returns the sum. */
typedef struct {
    int n;
} primer_args;

static int64_t primer(void *arg) {
    primer_args *pa = (primer_args *)arg;
    int N = pa->n;
    rw_fiber_handle **fs = calloc(N, sizeof(*fs));
    for (int i = 0; i < N; i++) {
        fs[i] = rw_sched_spawn_i64(cpu_burn, (void *)(intptr_t)(i + 1));
    }
    int64_t total = 0;
    for (int i = 0; i < N; i++) {
        total += rw_sched_join_i64(fs[i]);
    }
    free(fs);
    return total;
}

int main(void) {
    rw_sched_init();

    const int N = 200;
    primer_args pa = { .n = N };

    struct timespec t0, t1;
    clock_gettime(CLOCK_MONOTONIC, &t0);

    rw_fiber_handle *ph = rw_sched_spawn_i64(primer, &pa);
    int64_t got = rw_sched_join_i64(ph);

    clock_gettime(CLOCK_MONOTONIC, &t1);
    double ms = (t1.tv_sec - t0.tv_sec) * 1000.0
              + (t1.tv_nsec - t0.tv_nsec) / 1.0e6;

    int64_t expected = (int64_t)N * (N + 1) / 2;
    const char *env = getenv("RW_WORKERS");
    printf("N=%d workers=%s elapsed=%.1fms total=%" PRId64
           " expected=%" PRId64 "\n",
           N, env ? env : "(default)", ms, got, expected);

    assert(got == expected);
    rw_sched_shutdown();
    return 0;
}
