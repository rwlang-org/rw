/*
 * Async file I/O via a fixed thread pool. See docs/specs/16-async-file-io.md.
 *
 * rw_read/rw_write (io.c) route regular files here. A worker thread runs
 * the blocking read(2)/write(2) while the calling fiber is parked, so the
 * worker M is free to run other fibers. Protocol mirrors the netpoller:
 * the caller fills a task + its own fiber handle, then parks; the pool
 * worker stores the result and calls rw_sched_enqueue_ready(handle).
 *
 * io_uring (Linux) will later replace this backend behind rw_aio_*.
 */

#include "aio.h"
#include "fiber/sched.h"
#include "runtime.h"

#include <pthread.h>
#include <stdint.h>
#include <stdlib.h>
#include <unistd.h>

#define RW_AIO_DEFAULT_THREADS 4
#define RW_AIO_MAX_THREADS 64

typedef enum { RW_AIO_READ, RW_AIO_WRITE } rw_aio_op;

typedef struct rw_aio_task {
    rw_aio_op op;
    int64_t   fd;
    rw_str   *out;           /* read: worker fills this */
    int64_t   max;           /* read */
    rw_str    in;            /* write: caller-provided buffer */
    int64_t   wret;          /* write result */
    rw_fiber_handle *waiter; /* fiber to wake on completion */
    struct rw_aio_task *next;
} rw_aio_task;

static pthread_t        g_threads[RW_AIO_MAX_THREADS];
static int              g_nthreads = 0;
static pthread_mutex_t  g_mu  = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t   g_cv  = PTHREAD_COND_INITIALIZER;
static rw_aio_task     *g_head = NULL;
static rw_aio_task     *g_tail = NULL;
static int              g_shutdown = 0;

static void queue_push(rw_aio_task *t) {
    t->next = NULL;
    pthread_mutex_lock(&g_mu);
    if (g_tail) g_tail->next = t; else g_head = t;
    g_tail = t;
    pthread_cond_signal(&g_cv);
    pthread_mutex_unlock(&g_mu);
}

static rw_aio_task *queue_pop(void) {
    pthread_mutex_lock(&g_mu);
    while (g_head == NULL && !g_shutdown) {
        pthread_cond_wait(&g_cv, &g_mu);
    }
    if (g_shutdown && g_head == NULL) {
        pthread_mutex_unlock(&g_mu);
        return NULL;
    }
    rw_aio_task *t = g_head;
    g_head = t->next;
    if (g_head == NULL) g_tail = NULL;
    pthread_mutex_unlock(&g_mu);
    return t;
}

static void run_task(rw_aio_task *t) {
    if (t->op == RW_AIO_READ) {
        char *buf = (char *)malloc((size_t)t->max);
        if (!buf) { t->out->len = 0; t->out->ptr = NULL; return; }
        ssize_t n = read((int)t->fd, buf, (size_t)t->max);
        if (n > 0) { t->out->len = n; t->out->ptr = buf; }
        else       { free(buf); t->out->len = 0; t->out->ptr = NULL; }
    } else { /* RW_AIO_WRITE */
        ssize_t n = write((int)t->fd, t->in.ptr, (size_t)t->in.len);
        t->wret = (int64_t)n;
    }
}

static void *worker_main(void *arg) {
    (void)arg;
    for (;;) {
        rw_aio_task *t = queue_pop();
        if (t == NULL) return NULL;   /* shutdown */
        run_task(t);
        /* Result store above happens-before this enqueue; the woken
         * fiber reads the result after resuming. Same edge as netpoller. */
        rw_sched_enqueue_ready(t->waiter);
    }
}

void rw_aio_init(void) {
    g_shutdown = 0;
    int n = RW_AIO_DEFAULT_THREADS;
    const char *env = getenv("RW_AIO_THREADS");
    if (env) {
        int v = atoi(env);
        if (v >= 1 && v <= RW_AIO_MAX_THREADS) n = v;
    }
    g_nthreads = 0;
    for (int i = 0; i < n; i++) {
        if (pthread_create(&g_threads[i], NULL, worker_main, NULL) == 0) {
            g_nthreads++;
        }
    }
}

void rw_aio_shutdown(void) {
    pthread_mutex_lock(&g_mu);
    g_shutdown = 1;
    pthread_cond_broadcast(&g_cv);
    pthread_mutex_unlock(&g_mu);
    for (int i = 0; i < g_nthreads; i++) {
        pthread_join(g_threads[i], NULL);
    }
    g_nthreads = 0;
}

void rw_aio_read(rw_str *out, int64_t fd, int64_t max) {
    if (max <= 0) { out->len = 0; out->ptr = NULL; return; }
    rw_fiber_handle *me = rw_sched_current_fiber();
    if (!me) {
        char *buf = (char *)malloc((size_t)max);
        if (!buf) { out->len = 0; out->ptr = NULL; return; }
        ssize_t n = read((int)fd, buf, (size_t)max);
        if (n > 0) { out->len = n; out->ptr = buf; }
        else       { free(buf); out->len = 0; out->ptr = NULL; }
        return;
    }
    rw_aio_task t;
    t.op = RW_AIO_READ; t.fd = fd; t.out = out; t.max = max;
    t.waiter = me; t.next = NULL;
    queue_push(&t);
    rw_sched_park_current();
}

int64_t rw_aio_write(int64_t fd, rw_str b) {
    if (b.len <= 0) return 0;
    rw_fiber_handle *me = rw_sched_current_fiber();
    if (!me) {
        ssize_t n = write((int)fd, b.ptr, (size_t)b.len);
        return (int64_t)n;
    }
    rw_aio_task t;
    t.op = RW_AIO_WRITE; t.fd = fd; t.in = b; t.wret = 0;
    t.waiter = me; t.next = NULL;
    queue_push(&t);
    rw_sched_park_current();
    return t.wret;
}
