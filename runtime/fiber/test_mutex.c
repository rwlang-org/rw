/*
 * Mutex test (spec 46): 100 fibers on 4 workers increment a shared counter
 * with a yield inside the critical section; no update is lost. The main
 * thread can contend too.
 */

#include "sched.h"
#include "../runtime.h"
#include "../chan.h"

#include <assert.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>

#define FIBERS 100
#define ITERS 200

static rw_mutex *g_mu;
static volatile int64_t g_counter = 0;

static int64_t bump(void *arg) {
    (void)arg;
    for (int i = 0; i < ITERS; i++) {
        rw_mutex_lock(g_mu);
        int64_t v = g_counter;
        if (i % 7 == 0) rw_sched_yield();   /* widen the race window */
        g_counter = v + 1;
        rw_mutex_unlock(g_mu);
    }
    return 0;
}

int main(void) {
    setenv("RW_WORKERS", "4", 1);
    rw_sched_init();
    g_mu = rw_mutex_new();
    rw_fiber_handle *hs[FIBERS];
    for (int i = 0; i < FIBERS; i++) hs[i] = rw_sched_spawn_i64(bump, NULL);
    for (int i = 0; i < ITERS; i++) {       /* the main thread joins the race */
        rw_mutex_lock(g_mu);
        g_counter++;
        rw_mutex_unlock(g_mu);
    }
    for (int i = 0; i < FIBERS; i++) rw_sched_join_i64(hs[i]);
    assert(g_counter == (int64_t)(FIBERS + 1) * ITERS);
    assert(rw_mutex_try_lock(g_mu) == 1);
    assert(rw_mutex_try_lock(g_mu) == 0);
    rw_mutex_unlock(g_mu);
    rw_sched_shutdown();
    printf("test_mutex: OK (%" PRId64 ")\n", g_counter);
    return 0;
}
