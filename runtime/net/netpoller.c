/*
 * Common netpoller logic: thread lifecycle, nonblocking setup, park
 * helpers. Platform-specific event-loop body lives in netpoller_kqueue.c
 * (macOS) or netpoller_epoll.c (Linux).
 */

#include "netpoller.h"

#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

static pthread_t   g_netpoller_thread;
static int         g_netpoller_started = 0;
static _Atomic int g_netpoller_shutdown = 0;

int rw_netpoller_is_shutdown(void) {
    return atomic_load_explicit(&g_netpoller_shutdown, memory_order_acquire);
}

int rw_set_nonblocking(int fd) {
    int flags = fcntl(fd, F_GETFL, 0);
    if (flags < 0) return -1;
    if (flags & O_NONBLOCK) return 0;
    return fcntl(fd, F_SETFL, flags | O_NONBLOCK);
}

static void *netpoller_main(void *arg) {
    (void)arg;
    rw_netpoller_platform_run();
    return NULL;
}

void rw_netpoller_init(void) {
    atomic_store_explicit(&g_netpoller_shutdown, 0, memory_order_release);
    if (rw_netpoller_platform_init() != 0) {
        perror("rw_netpoller_platform_init");
        abort();
    }
    if (pthread_create(&g_netpoller_thread, NULL, netpoller_main, NULL) != 0) {
        perror("pthread_create netpoller");
        abort();
    }
    g_netpoller_started = 1;
}

void rw_netpoller_shutdown(void) {
    if (!g_netpoller_started) return;
    atomic_store_explicit(&g_netpoller_shutdown, 1, memory_order_release);
    rw_netpoller_platform_shutdown();   /* wake the poll loop */
    pthread_join(g_netpoller_thread, NULL);
    g_netpoller_started = 0;
}

/* Commits for rw_sched_park_commit (spec 24): register the parked fiber
 * only once its context is saved. If registration fails, wake it at once
 * so the caller's retry loop does not deadlock. */
static void commit_read(rw_fiber_handle *self, void *arg) {
    if (rw_netpoller_register_read((int)(intptr_t)arg, self) != 0) {
        rw_sched_enqueue_ready(self);
    }
}

static void commit_write(rw_fiber_handle *self, void *arg) {
    if (rw_netpoller_register_write((int)(intptr_t)arg, self) != 0) {
        rw_sched_enqueue_ready(self);
    }
}

void rw_net_park_read(int fd) {
    if (!rw_sched_current_fiber()) {
        fputs("rw: rw_net_park_read called outside a fiber\n", stderr);
        abort();
    }
    rw_sched_park_commit(commit_read, (void *)(intptr_t)fd);
}

void rw_net_park_write(int fd) {
    if (!rw_sched_current_fiber()) {
        fputs("rw: rw_net_park_write called outside a fiber\n", stderr);
        abort();
    }
    rw_sched_park_commit(commit_write, (void *)(intptr_t)fd);
}
