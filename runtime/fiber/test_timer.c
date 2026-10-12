/*
 * Timer test (spec 39): sleeping fibers wake in deadline order, no earlier
 * than asked, and 10,000 concurrent sleepers all wake.
 */

#include "sched.h"
#include "../runtime.h"
#include "../timer.h"

#include <assert.h>
#include <inttypes.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>

static _Atomic int64_t g_order = 0;
static int64_t g_rank[3];
static _Atomic int64_t g_woken = 0;

static int64_t sleeper(void *arg) {
    int64_t i = (int64_t)(intptr_t)arg;
    int64_t ms = (3 - i) * 30;  /* 90, 60, 30 ms: reverse of spawn order */
    int64_t t0 = rw_mono_ns();
    rw_sleep_ms(ms);
    int64_t slept = rw_mono_ns() - t0;
    assert(slept >= ms * 1000000);
    g_rank[i] = atomic_fetch_add(&g_order, 1);
    return 0;
}

static int64_t many(void *arg) {
    rw_sleep_ms((int64_t)(intptr_t)arg % 50);
    atomic_fetch_add(&g_woken, 1);
    return 0;
}

int main(void) {
    setenv("RW_WORKERS", "2", 1);
    rw_sched_init();
    rw_timer_init();
    rw_fiber_handle *h[3];
    for (int i = 0; i < 3; i++) h[i] = rw_sched_spawn_i64(sleeper, (void *)(intptr_t)i);
    for (int i = 0; i < 3; i++) rw_sched_join_i64(h[i]);
    assert(g_rank[2] == 0 && g_rank[1] == 1 && g_rank[0] == 2);

    const int N = 10000;
    int64_t t0 = rw_mono_ns();
    for (int i = 0; i < N; i++) rw_sched_detach(rw_sched_spawn_i64(many, (void *)(intptr_t)i));
    while (atomic_load(&g_woken) < N) rw_sleep_ms(5);  /* main thread: nanosleep */
    double ms = (rw_mono_ns() - t0) / 1e6;
    assert(ms < 5000);
    assert(rw_time_ms() > 1700000000000LL);  /* after 2023: a real wall clock */
    rw_timer_shutdown();
    rw_sched_shutdown();
    printf("test_timer: OK (%d sleepers in %.0f ms)\n", N, ms);
    return 0;
}
