/*
 * Fiber reclamation test (spec 19).
 *
 * Detached fibers must be freed when they finish, joined fibers must be
 * freed exactly once, and in both cases the live-handle count has to
 * return to exactly 0. We sweep RW_WORKERS so the handoff between the
 * worker (fiber finished) and the owner (join / detach) races both ways.
 */

#include "sched.h"
#include "../runtime.h"

#include <assert.h>
#include <inttypes.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>

static _Atomic int64_t g_ran = 0;

static int64_t work(void *arg) {
    /* Yield a few times so detach/join often lands while the fiber is
     * still running, not only after it finished. */
    int64_t n = (int64_t)(intptr_t)arg;
    for (int i = 0; i < (int)(n % 3); i++) rw_sched_yield();
    atomic_fetch_add(&g_ran, 1);
    return n;
}

/* A fiber that itself spawns and detaches children, like a server's
 * accept loop running inside a fiber. */
static int64_t spawner(void *arg) {
    int64_t n = (int64_t)(intptr_t)arg;
    for (int64_t i = 0; i < n; i++) {
        rw_sched_detach(rw_sched_spawn_i64(work, (void *)(intptr_t)i));
    }
    return n;
}

static void wait_live_zero(const char *what) {
    /* Workers free asynchronously; give them up to ~5s. */
    for (int i = 0; i < 5000; i++) {
        if (rw_sched_live_fibers() == 0) return;
        usleep(1000);
    }
    fprintf(stderr, "%s: live fibers did not drop to 0 (%" PRId64 ")\n",
            what, rw_sched_live_fibers());
    abort();
}

static void cycle(const char *workers, int n) {
    setenv("RW_WORKERS", workers, 1);
    rw_sched_init();
    atomic_store(&g_ran, 0);

    /* 1. Detached from the main thread. */
    for (int i = 0; i < n; i++) {
        rw_sched_detach(rw_sched_spawn_i64(work, (void *)(intptr_t)i));
    }
    wait_live_zero("detach from main");
    assert(atomic_load(&g_ran) == n);

    /* 2. Detached from inside a fiber. */
    atomic_store(&g_ran, 0);
    int64_t got = rw_sched_join_i64(rw_sched_spawn_i64(spawner, (void *)(intptr_t)n));
    assert(got == n);
    wait_live_zero("detach from fiber");
    assert(atomic_load(&g_ran) == n);

    /* 3. Joined: every handle freed exactly once. */
    int64_t total = 0;
    for (int i = 0; i < n; i++) {
        total += rw_sched_join_i64(rw_sched_spawn_i64(work, (void *)(intptr_t)i));
    }
    assert(total == (int64_t)n * (n - 1) / 2);
    wait_live_zero("join");

    rw_sched_shutdown();
}

int main(void) {
    const char *workers[] = {"1", "2", "4"};
    for (size_t i = 0; i < sizeof workers / sizeof workers[0]; i++) {
        cycle(workers[i], 5000);
    }
    printf("test_detach: OK\n");
    return 0;
}
