/*
 * Common netpoller logic: thread lifecycle, nonblocking setup, park
 * helpers. Platform-specific event-loop body lives in netpoller_kqueue.c
 * (macOS) or netpoller_epoll.c (Linux).
 */

#include "netpoller.h"
#include "../fiber/park.h"
#include "../timer.h"

#include <errno.h>
#include <fcntl.h>
#include <poll.h>
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

/* ---- per-fd poll descriptors (spec 53) ----
 *
 * Every fd that a fiber waits on gets a descriptor that is never freed (fd
 * numbers are reused, descriptors with them). It holds, per direction, the
 * list of waiting fibers. The parking fiber, the netpoller thread, and the
 * timer thread touch a descriptor only under its lock, so exactly one of
 * "ready" and "timed out" resumes a waiter. A waiter is a node on its
 * fiber's stack, unlinked (under the lock) before the fiber returns. The
 * timer finds it by id, so a timer firing after the wait ended is a no-op.
 */

typedef struct rw_net_waiter {
    uint64_t               id;
    rw_fiber_handle       *g;         /* NULL until the park commit runs */
    int                    timedout;
    int                    closed;     /* woken because the fd was closed */
    int                    linked;
    struct rw_net_waiter  *next;
} rw_net_waiter;

struct rw_pollfd {
    rw_wait_lock   lock;
    int            fd;
    uint64_t       next_id;
    rw_net_waiter *head[2];
    int64_t        timeout_ms[2];
    int            closed;   /* closed by rw_close; cleared for a new handle */
};

#define PD_CHUNK  1024
#define PD_CHUNKS 4096   /* fds up to 4M */

static _Atomic(rw_pollfd *) g_pd_chunks[PD_CHUNKS];

static rw_pollfd *pd_get(int fd) {
    if (fd < 0 || fd >= PD_CHUNK * PD_CHUNKS) return NULL;
    size_t ci = (size_t)fd / PD_CHUNK;
    rw_pollfd *chunk = atomic_load_explicit(&g_pd_chunks[ci], memory_order_acquire);
    if (!chunk) {
        rw_pollfd *fresh = (rw_pollfd *)calloc(PD_CHUNK, sizeof *fresh);
        if (!fresh) return NULL;
        for (int i = 0; i < PD_CHUNK; i++) {
            rw_wait_lock_init(&fresh[i].lock);
            fresh[i].fd = (int)(ci * PD_CHUNK) + i;
        }
        rw_pollfd *expected = NULL;
        if (atomic_compare_exchange_strong_explicit(&g_pd_chunks[ci], &expected, fresh,
                                                    memory_order_acq_rel, memory_order_acquire)) {
            chunk = fresh;
        } else {
            free(fresh);
            chunk = expected;
        }
    }
    return &chunk[fd % PD_CHUNK];
}

static void unlink_waiter(rw_pollfd *pd, int dir, rw_net_waiter *w) {
    for (rw_net_waiter **pp = &pd->head[dir]; *pp; pp = &(*pp)->next) {
        if (*pp == w) { *pp = w->next; break; }
    }
    w->linked = 0;
    w->next = NULL;
}

static int interest(rw_pollfd *pd) {
    return (pd->head[RW_NET_READ] ? 1 << RW_NET_READ : 0) |
           (pd->head[RW_NET_WRITE] ? 1 << RW_NET_WRITE : 0);
}

/* Called by the platform loop. Wakes every parked waiter in the ready
 * directions (they retry their syscall); waiters still arming keep their
 * place and register for themselves. Re-arms interest that is left. */
void rw_netpoller_ready(rw_pollfd *pd, int events) {
    rw_wait_lock_acquire(&pd->lock);
    for (int dir = 0; dir < 2; dir++) {
        if (!(events & (1 << dir))) continue;
        rw_net_waiter **pp = &pd->head[dir];
        while (*pp) {
            rw_net_waiter *w = *pp;
            if (w->g) {
                rw_fiber_handle *g = w->g;
                *pp = w->next;
                w->linked = 0;
                w->next = NULL;
                w->g = NULL;
                rw_sched_enqueue_ready(g);
            } else {
                pp = &w->next;
            }
        }
    }
    int left = interest(pd);
    if (left) rw_netpoller_register(pd->fd, pd, left);
    rw_wait_lock_release(&pd->lock);
}

/* Timer callback: tag = waiter id << 1 | dir. */
static void wait_timeout(void *arg, uint64_t tag) {
    rw_pollfd *pd = (rw_pollfd *)arg;
    int dir = (int)(tag & 1);
    uint64_t id = tag >> 1;
    rw_wait_lock_acquire(&pd->lock);
    for (rw_net_waiter *w = pd->head[dir]; w; w = w->next) {
        if (w->id != id) continue;
        w->timedout = 1;
        if (w->g) {          /* parked: resume it now */
            rw_fiber_handle *g = w->g;
            w->g = NULL;
            unlink_waiter(pd, dir, w);
            rw_sched_enqueue_ready(g);
        }                    /* else still arming: the commit sees timedout */
        break;
    }
    rw_wait_lock_release(&pd->lock);
}

typedef struct {
    rw_pollfd     *pd;
    int            dir;
    rw_net_waiter *w;
} wait_ctx;

/* Commit for rw_sched_park_commit (spec 24): publish the waiter only once
 * the fiber's context is saved. */
static void commit_wait(rw_fiber_handle *self, void *arg) {
    wait_ctx *c = (wait_ctx *)arg;
    rw_wait_lock_acquire(&c->pd->lock);
    if (c->w->timedout || c->w->closed || !c->w->linked) {
        if (c->w->linked) unlink_waiter(c->pd, c->dir, c->w);
        rw_wait_lock_release(&c->pd->lock);
        rw_sched_enqueue_ready(self);
        return;
    }
    c->w->g = self;
    if (rw_netpoller_register(c->pd->fd, c->pd, interest(c->pd)) != 0) {
        c->w->g = NULL;
        unlink_waiter(c->pd, c->dir, c->w);
        rw_wait_lock_release(&c->pd->lock);
        rw_sched_enqueue_ready(self);
        return;
    }
    rw_wait_lock_release(&c->pd->lock);
}

int rw_net_wait(int fd, int dir, int64_t deadline_ns) {
    if (!rw_sched_current_fiber()) {
        fputs("rw: rw_net_wait called outside a fiber\n", stderr);
        abort();
    }
    rw_pollfd *pd = pd_get(fd);
    if (!pd) { rw_sched_yield(); return 0; }
    if (deadline_ns > 0 && deadline_ns <= rw_mono_ns()) return RW_NET_TIMEDOUT;
    rw_net_waiter w = { 0, NULL, 0, 0, 1, NULL };
    rw_wait_lock_acquire(&pd->lock);
    if (pd->closed) {
        rw_wait_lock_release(&pd->lock);
        return RW_NET_CLOSED;
    }
    w.id = ++pd->next_id;
    w.next = pd->head[dir];
    pd->head[dir] = &w;
    rw_wait_lock_release(&pd->lock);
    if (deadline_ns > 0) {
        rw_timer_call_at(deadline_ns, wait_timeout, pd, (w.id << 1) | (uint64_t)dir);
    }
    wait_ctx c = { pd, dir, &w };
    rw_sched_park_commit(commit_wait, &c);
    rw_wait_lock_acquire(&pd->lock);
    if (w.linked) unlink_waiter(pd, dir, &w);
    int result = w.closed ? RW_NET_CLOSED : w.timedout ? RW_NET_TIMEDOUT : 0;
    rw_wait_lock_release(&pd->lock);
    return result;
}

/* Called by rw_close before close(2): wake every fiber parked on fd so it
 * returns EBADF instead of sleeping forever (the kernel drops the
 * registration silently on close). Later waits fail at once until the fd
 * number is reused for a new handle (rw_net_reset_fd). */
void rw_net_close_fd(int fd) {
    rw_pollfd *pd = pd_get(fd);
    if (!pd) return;
    rw_wait_lock_acquire(&pd->lock);
    pd->closed = 1;
    for (int dir = 0; dir < 2; dir++) {
        rw_net_waiter **pp = &pd->head[dir];
        while (*pp) {
            rw_net_waiter *w = *pp;
            w->closed = 1;
            if (w->g) {
                rw_fiber_handle *g = w->g;
                *pp = w->next;
                w->linked = 0;
                w->next = NULL;
                w->g = NULL;
                rw_sched_enqueue_ready(g);
            } else {
                pp = &w->next;   /* still arming: its commit sees `closed` */
            }
        }
    }
    rw_wait_lock_release(&pd->lock);
}

static int pd_is_closed(int fd) {
    if (fd < 0 || fd >= PD_CHUNK * PD_CHUNKS) return 0;
    rw_pollfd *chunk = atomic_load_explicit(&g_pd_chunks[fd / PD_CHUNK], memory_order_acquire);
    if (!chunk) return 0;
    rw_pollfd *pd = &chunk[fd % PD_CHUNK];
    rw_wait_lock_acquire(&pd->lock);
    int closed = pd->closed;
    rw_wait_lock_release(&pd->lock);
    return closed;
}

void rw_net_park_read(int fd)  { (void)rw_net_wait(fd, RW_NET_READ, 0); }
void rw_net_park_write(int fd) { (void)rw_net_wait(fd, RW_NET_WRITE, 0); }

void rw_net_set_timeout(int fd, int dir, int64_t ms) {
    rw_pollfd *pd = pd_get(fd);
    if (!pd) return;
    rw_wait_lock_acquire(&pd->lock);
    pd->timeout_ms[dir] = ms > 0 ? ms : 0;
    rw_wait_lock_release(&pd->lock);
}

int64_t rw_net_get_timeout(int fd, int dir) {
    if (fd < 0 || fd >= PD_CHUNK * PD_CHUNKS) return 0;
    rw_pollfd *chunk = atomic_load_explicit(&g_pd_chunks[fd / PD_CHUNK], memory_order_acquire);
    if (!chunk) return 0;
    rw_pollfd *pd = &chunk[fd % PD_CHUNK];
    rw_wait_lock_acquire(&pd->lock);
    int64_t ms = pd->timeout_ms[dir];
    rw_wait_lock_release(&pd->lock);
    return ms;
}

void rw_net_reset_fd(int fd) {
    if (fd < 0 || fd >= PD_CHUNK * PD_CHUNKS) return;
    rw_pollfd *chunk = atomic_load_explicit(&g_pd_chunks[fd / PD_CHUNK], memory_order_acquire);
    if (!chunk) return;
    rw_pollfd *pd = &chunk[fd % PD_CHUNK];
    rw_wait_lock_acquire(&pd->lock);
    pd->timeout_ms[0] = pd->timeout_ms[1] = 0;
    pd->closed = 0;
    rw_wait_lock_release(&pd->lock);
}

int64_t rw_net_deadline(int fd, int dir) {
    int64_t ms = rw_net_get_timeout(fd, dir);
    return ms > 0 ? rw_mono_ns() + ms * 1000000 : 0;
}

/* Off-fiber waits poll in slices of at most this long, because closing an
 * fd from another thread does not wake a poll(2) on it. */
#define BLOCK_SLICE_MS 50

int rw_net_block(int fd, int dir, int64_t deadline_ns) {
    if (rw_sched_current_fiber()) return rw_net_wait(fd, dir, deadline_ns);
    struct pollfd p = { fd, (short)(dir == RW_NET_READ ? POLLIN : POLLOUT), 0 };
    for (;;) {
        if (pd_is_closed(fd)) return RW_NET_CLOSED;
        int ms = BLOCK_SLICE_MS;
        if (deadline_ns > 0) {
            int64_t left = deadline_ns - rw_mono_ns();
            if (left <= 0) return RW_NET_TIMEDOUT;
            int64_t left_ms = (left + 999999) / 1000000;
            if (left_ms < ms) ms = (int)left_ms;
        }
        int n = poll(&p, 1, ms);
        if (n > 0) return 0;
        if (n < 0 && errno != EINTR) return 0;
    }
}
