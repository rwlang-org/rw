/*
 * Netpoller waits with deadlines (spec 53):
 *   - a wait on an idle fd times out no earlier than its deadline
 *   - readiness before the deadline wins and the late timer is a no-op
 *   - readiness and timeouts racing on 64 socket pairs never resume a fiber
 *     twice (a double resume corrupts the fiber and crashes or hangs)
 *   - several fibers waiting on one fd all get woken
 */

#include "sched.h"
#include "../runtime.h"
#include "../timer.h"
#include "../net/netpoller.h"

#include <assert.h>
#include <errno.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/socket.h>
#include <unistd.h>

static int pair(int sv[2]) {
    if (socketpair(AF_UNIX, SOCK_STREAM, 0, sv) != 0) return -1;
    rw_set_nonblocking(sv[0]);
    rw_set_nonblocking(sv[1]);
    return 0;
}

static int64_t ms_since(int64_t t0) { return (rw_mono_ns() - t0) / 1000000; }

/* ---- timeout ---- */
static int64_t times_out(void *arg) {
    int fd = (int)(intptr_t)arg;
    int64_t t0 = rw_mono_ns();
    int r = rw_net_wait(fd, RW_NET_READ, t0 + 30 * 1000000LL);
    assert(r == 1);
    assert(rw_mono_ns() - t0 >= 30 * 1000000LL);
    return 0;
}

/* ---- readiness first ---- */
static int64_t ready_first(void *arg) {
    int fd = (int)(intptr_t)arg;
    int64_t t0 = rw_mono_ns();
    int r = rw_net_wait(fd, RW_NET_READ, t0 + 2000 * 1000000LL);
    assert(r == 0);
    assert(ms_since(t0) < 1500);
    char c;
    assert(read(fd, &c, 1) == 1);
    return 0;
}

static int64_t write_later(void *arg) {
    int fd = (int)(intptr_t)arg;
    rw_sleep_ms(10);
    assert(write(fd, "x", 1) == 1);
    return 0;
}

/* ---- race stress ---- */
#define PAIRS 64
#define ROUNDS 300
static int g_sv[PAIRS][2];
static _Atomic int g_in_wait[PAIRS];
static _Atomic int64_t g_timeouts, g_wakes;

static int64_t racer(void *arg) {
    int i = (int)(intptr_t)arg;
    unsigned seed = (unsigned)i * 7919u + 1;
    for (int k = 0; k < ROUNDS; k++) {
        int64_t deadline = rw_mono_ns() + (int64_t)(rand_r(&seed) % 2000) * 1000;
        assert(atomic_exchange(&g_in_wait[i], 1) == 0);
        int r = rw_net_wait(g_sv[i][0], RW_NET_READ, deadline);
        assert(atomic_exchange(&g_in_wait[i], 0) == 1);
        if (r) atomic_fetch_add(&g_timeouts, 1); else atomic_fetch_add(&g_wakes, 1);
        char buf[64];
        while (read(g_sv[i][0], buf, sizeof buf) > 0) {}
    }
    return 0;
}

static int64_t poker(void *arg) {
    int i = (int)(intptr_t)arg;
    unsigned seed = (unsigned)i * 104729u + 3;
    for (int k = 0; k < ROUNDS; k++) {
        ssize_t w = write(g_sv[i][1], "p", 1);
        (void)w;
        if (rand_r(&seed) % 4 == 0) rw_sleep_ms(1); else rw_sched_yield();
    }
    return 0;
}

/* ---- many waiters on one fd ---- */
#define WAITERS 8
static int g_shared[2];
static _Atomic int g_got;

static int64_t shared_reader(void *arg) {
    (void)arg;
    for (;;) {
        char c;
        if (read(g_shared[0], &c, 1) == 1) { atomic_fetch_add(&g_got, 1); return 0; }
        assert(errno == EAGAIN || errno == EWOULDBLOCK);
        rw_net_wait(g_shared[0], RW_NET_READ, rw_mono_ns() + 5000 * 1000000LL);
    }
}

static int64_t shared_writer(void *arg) {
    (void)arg;
    rw_sleep_ms(20);   /* let every reader park first */
    for (int k = 0; k < WAITERS; k++) {
        assert(write(g_shared[1], "s", 1) == 1);
        rw_sleep_ms(2);
    }
    return 0;
}

int main(void) {
    setenv("RW_WORKERS", "4", 1);
    rw_sched_init();
    rw_netpoller_init();
    rw_timer_init();

    int a[2], b[2];
    assert(pair(a) == 0 && pair(b) == 0);
    rw_sched_join_i64(rw_sched_spawn_i64(times_out, (void *)(intptr_t)a[0]));
    rw_fiber_handle *r = rw_sched_spawn_i64(ready_first, (void *)(intptr_t)b[0]);
    rw_fiber_handle *w = rw_sched_spawn_i64(write_later, (void *)(intptr_t)b[1]);
    rw_sched_join_i64(r);
    rw_sched_join_i64(w);

    rw_fiber_handle *hs[2 * PAIRS];
    for (int i = 0; i < PAIRS; i++) {
        assert(pair(g_sv[i]) == 0);
        hs[2 * i] = rw_sched_spawn_i64(racer, (void *)(intptr_t)i);
        hs[2 * i + 1] = rw_sched_spawn_i64(poker, (void *)(intptr_t)i);
    }
    for (int i = 0; i < 2 * PAIRS; i++) rw_sched_join_i64(hs[i]);
    assert(atomic_load(&g_timeouts) + atomic_load(&g_wakes) == (int64_t)PAIRS * ROUNDS);

    assert(pair(g_shared) == 0);
    rw_fiber_handle *rs[WAITERS];
    for (int i = 0; i < WAITERS; i++) rs[i] = rw_sched_spawn_i64(shared_reader, NULL);
    rw_fiber_handle *sw = rw_sched_spawn_i64(shared_writer, NULL);
    for (int i = 0; i < WAITERS; i++) rw_sched_join_i64(rs[i]);
    rw_sched_join_i64(sw);
    assert(atomic_load(&g_got) == WAITERS);

    rw_sleep_ms(10);   /* let late timers fire against finished waits */
    rw_timer_shutdown();
    rw_netpoller_shutdown();
    rw_sched_shutdown();
    printf("test_net_timeout: OK (%lld timeouts, %lld wakes)\n",
           (long long)atomic_load(&g_timeouts), (long long)atomic_load(&g_wakes));
    return 0;
}
