/*
 * await_any order (spec 45): when several futures have finished before the
 * caller looks, the one that finished first wins — not the lowest index.
 */

#include "sched.h"
#include "../runtime.h"
#include "../timer.h"

#include <assert.h>
#include <stdio.h>
#include <stdlib.h>

static int64_t nap(void *arg) {
    rw_sleep_ms((int64_t)(intptr_t)arg);
    return (int64_t)(intptr_t)arg;
}

int main(void) {
    setenv("RW_WORKERS", "1", 1);
    rw_sched_init();
    rw_timer_init();
    /* Index 0 finishes last; index 1 first. */
    rw_future_t *fs[3];
    fs[0] = (rw_future_t *)rw_sched_spawn_i64(nap, (void *)(intptr_t)60);
    fs[1] = (rw_future_t *)rw_sched_spawn_i64(nap, (void *)(intptr_t)10);
    fs[2] = (rw_future_t *)rw_sched_spawn_i64(nap, (void *)(intptr_t)30);
    rw_sleep_ms(200);  /* main thread: let all three finish before asking */
    rw_list l = {3, 3, (char *)fs};
    assert(rw_await_any(&l) == 1);
    for (int i = 0; i < 3; i++) rw_await_i64(fs[i]);
    rw_timer_shutdown();
    rw_sched_shutdown();
    printf("test_await_any: OK\n");
    return 0;
}
