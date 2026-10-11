/*
 * Park/wake publication race (spec 24).
 *
 * A fiber that parks must not become runnable before its context is saved.
 * Regular-file reads go through the aio pool, which finishes almost
 * immediately, and short-lived children finish while their parent is
 * joining them; both wake the parked fiber as soon as possible. Many such
 * fibers across several workers make an early wake-up crash, hang, or run
 * a fiber twice.
 */

#include "sched.h"
#include "../runtime.h"
#include "../aio.h"

#include <assert.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static int g_fd = -1;
static _Atomic int64_t g_reads = 0;

static int64_t child(void *arg) {
    return (int64_t)(intptr_t)arg;
}

static int64_t reader(void *arg) {
    int64_t iters = (int64_t)(intptr_t)arg;
    int64_t sum = 0;
    for (int64_t i = 0; i < iters; i++) {
        rw_str s;
        lseek(g_fd, 0, SEEK_SET);  /* shared offset; only the length matters */
        rw_read(&s, g_fd, 4);
        atomic_fetch_add(&g_reads, 1);
        /* Join a child that finishes immediately: the joiner parks on its
         * wait list and is woken by the child's finalize. */
        sum += rw_sched_join_i64(rw_sched_spawn_i64(child, (void *)(intptr_t)1));
    }
    return sum;
}

int main(void) {
    char path[] = "/tmp/rw_park_race_XXXXXX";
    g_fd = mkstemp(path);
    assert(g_fd >= 0);
    assert(write(g_fd, "abcd", 4) == 4);
    setenv("RW_WORKERS", "4", 1);
    rw_sched_init();
    rw_aio_init();
    const int N = 64, ITERS = 300;
    rw_fiber_handle *hs[64];
    for (int i = 0; i < N; i++) hs[i] = rw_sched_spawn_i64(reader, (void *)(intptr_t)ITERS);
    int64_t total = 0;
    for (int i = 0; i < N; i++) total += rw_sched_join_i64(hs[i]);
    assert(total == (int64_t)N * ITERS);
    assert(atomic_load(&g_reads) == (int64_t)N * ITERS);
    rw_aio_shutdown();
    rw_sched_shutdown();
    close(g_fd);
    unlink(path);
    printf("test_park_race: OK\n");
    return 0;
}
