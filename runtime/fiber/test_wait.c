/*
 * Fiber-from-fiber await test.
 *
 * Scenario: fiber A spawns fiber B inside itself, awaits B's result,
 * then returns. Main awaits A. This exercises the wait-list / park
 * path (introduced in Commit 4):
 *   - A's join on B parks A on B's wait list (state = WAITING) and
 *     yields. The worker is free to run B in the meantime.
 *   - B finishes, finalize_fiber re-enqueues A as READY.
 *   - A is resumed by the worker, observes DONE on B, returns the
 *     value to its own caller (main).
 *
 * We also stack-test: A1 awaits A2 awaits A3. Each level must park
 * properly without deadlocking the single worker.
 */

#include "sched.h"
#include "../runtime.h"

#include <assert.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>

static int64_t leaf(void *arg) {
    return (int64_t)(intptr_t)arg * 10;
}

static int64_t middle(void *arg) {
    rw_fiber_handle *child = rw_sched_spawn_i64(leaf, arg);
    int64_t v = rw_sched_join_i64(child);
    return v + 1;
}

static int64_t top(void *arg) {
    rw_fiber_handle *child = rw_sched_spawn_i64(middle, arg);
    int64_t v = rw_sched_join_i64(child);
    return v + 100;
}

/* Concurrent: spawn 20 fibers from inside one fiber, join them all
 * from that same fiber, sum the results. Exercises a wait list with
 * many entries (well, sequentially — only one outer fiber at a time
 * is waiting on each target). The point is mainly that no deadlock
 * occurs even though we have only one worker M. */
static int64_t fan_out(void *arg) {
    (void)arg;
    const int N = 20;
    rw_fiber_handle *fs[N];
    for (int i = 0; i < N; i++) {
        fs[i] = rw_sched_spawn_i64(leaf, (void *)(intptr_t)(i + 1));
    }
    int64_t sum = 0;
    for (int i = 0; i < N; i++) {
        sum += rw_sched_join_i64(fs[i]);
    }
    return sum;
}

int main(void) {
    rw_sched_init();

    /* 1) Two-level nested await: main -> top -> middle -> leaf. */
    rw_fiber_handle *h = rw_sched_spawn_i64(top, (void *)(intptr_t)5);
    int64_t v = rw_sched_join_i64(h);
    /* leaf(5)=50, middle adds 1 => 51, top adds 100 => 151. */
    assert(v == 151);
    printf("nested OK (got %" PRId64 ")\n", v);

    /* 2) Fan-out: fiber awaits 20 sub-fibers. */
    rw_fiber_handle *fan = rw_sched_spawn_i64(fan_out, NULL);
    int64_t s = rw_sched_join_i64(fan);
    /* sum_{i=1..20}(i*10) = 10 * 20*21/2 = 2100. */
    assert(s == 2100);
    printf("fan_out OK (got %" PRId64 ")\n", s);

    rw_sched_shutdown();
    return 0;
}
