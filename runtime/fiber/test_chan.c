/*
 * Channel test (spec 44): producers / consumers across workers with
 * rendezvous and buffered channels, close semantics, and the main thread
 * as a receiver. Every value is received exactly once.
 */

#include "sched.h"
#include "../runtime.h"
#include "../chan.h"

#include <assert.h>
#include <inttypes.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>

#define PRODUCERS 8
#define PER 2000

static rw_chan *g_ch;
static _Atomic int64_t g_sum = 0, g_count = 0;

static int64_t producer(void *arg) {
    int64_t base = (int64_t)(intptr_t)arg * PER;
    for (int64_t i = 0; i < PER; i++) {
        int64_t v = base + i;
        rw_chan_send(g_ch, &v);
        if (i % 97 == 0) rw_sched_yield();
    }
    return 0;
}

static int64_t consumer(void *arg) {
    (void)arg;
    int64_t v;
    while (rw_chan_recv(g_ch, &v)) {
        atomic_fetch_add(&g_sum, v);
        atomic_fetch_add(&g_count, 1);
    }
    return 0;
}

static void run(int64_t capacity) {
    atomic_store(&g_sum, 0);
    atomic_store(&g_count, 0);
    g_ch = rw_chan_new(capacity, sizeof(int64_t));
    rw_fiber_handle *cons[4], *prod[PRODUCERS];
    for (int i = 0; i < 4; i++) cons[i] = rw_sched_spawn_i64(consumer, NULL);
    for (int i = 0; i < PRODUCERS; i++) prod[i] = rw_sched_spawn_i64(producer, (void *)(intptr_t)i);
    for (int i = 0; i < PRODUCERS; i++) rw_sched_join_i64(prod[i]);
    rw_chan_close(g_ch);
    for (int i = 0; i < 4; i++) rw_sched_join_i64(cons[i]);
    int64_t n = PRODUCERS * PER;
    assert(atomic_load(&g_count) == n);
    assert(atomic_load(&g_sum) == n * (n - 1) / 2);
}

static int64_t send_three(void *arg) {
    rw_chan *c = (rw_chan *)arg;
    for (int64_t v = 1; v <= 3; v++) rw_chan_send(c, &v);
    rw_chan_close(c);
    return 0;
}

int main(void) {
    setenv("RW_WORKERS", "4", 1);
    rw_sched_init();
    run(0);    /* rendezvous */
    run(1);
    run(64);

    /* The main thread receives from a fiber; buffered values survive close. */
    rw_chan *c = rw_chan_new(0, sizeof(int64_t));
    rw_sched_detach(rw_sched_spawn_i64(send_three, c));
    int64_t v, total = 0;
    while (rw_chan_recv(c, &v)) total += v;
    assert(total == 6);
    rw_chan *b = rw_chan_new(4, sizeof(int64_t));
    for (int64_t i = 10; i < 13; i++) rw_chan_send(b, &i);
    rw_chan_close(b);
    assert(rw_chan_len(b) == 3);
    total = 0;
    while (rw_chan_recv(b, &v)) total += v;
    assert(total == 33);
    rw_sched_shutdown();
    printf("test_chan: OK\n");
    return 0;
}
