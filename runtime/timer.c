/*
 * Timers (spec 39): a dedicated thread owns a binary min-heap of
 * (deadline, fiber). rw_sleep_ms parks the fiber with a commit that pushes
 * it onto the heap only after its context is saved (spec 24), so the timer
 * thread can never wake it early. Insertion and wake-up are O(log N).
 */

#include "timer.h"
#include "fiber/sched.h"

#include <errno.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>

typedef struct {
    int64_t          deadline_ns;
    uint64_t         seq;     /* FIFO among equal deadlines */
    rw_fiber_handle *fiber;   /* sleep: wake this fiber */
    void           (*fn)(void *, uint64_t);   /* otherwise: call fn */
    void            *arg;
    uint64_t         tag;
} rw_timer_entry;

static pthread_mutex_t g_mu = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t  g_cv;
static rw_timer_entry *g_heap = NULL;
static size_t          g_len = 0, g_cap = 0;
static uint64_t        g_seq = 0;
static int             g_stop = 0;
static int             g_started = 0;
static pthread_t       g_thread;

int64_t rw_mono_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000000000 + ts.tv_nsec;
}

int64_t rw_time_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static int entry_less(const rw_timer_entry *a, const rw_timer_entry *b) {
    return a->deadline_ns < b->deadline_ns ||
           (a->deadline_ns == b->deadline_ns && a->seq < b->seq);
}

static void heap_push(rw_timer_entry e) {
    if (g_len == g_cap) {
        g_cap = g_cap ? g_cap * 2 : 64;
        g_heap = (rw_timer_entry *)realloc(g_heap, g_cap * sizeof *g_heap);
        if (!g_heap) { fputs("rw: out of memory (timers)\n", stderr); abort(); }
    }
    size_t i = g_len++;
    while (i > 0) {
        size_t parent = (i - 1) / 2;
        if (!entry_less(&e, &g_heap[parent])) break;
        g_heap[i] = g_heap[parent];
        i = parent;
    }
    g_heap[i] = e;
}

static rw_timer_entry heap_pop(void) {
    rw_timer_entry top = g_heap[0];
    rw_timer_entry last = g_heap[--g_len];
    size_t i = 0;
    for (;;) {
        size_t l = 2 * i + 1, r = l + 1, m = i;
        const rw_timer_entry *best = &last;
        if (l < g_len && entry_less(&g_heap[l], best)) { m = l; best = &g_heap[l]; }
        if (r < g_len && entry_less(&g_heap[r], best)) { m = r; }
        if (m == i) break;
        g_heap[i] = g_heap[m];
        i = m;
    }
    if (g_len > 0) g_heap[i] = last;
    return top;
}

static void *timer_main(void *arg) {
    (void)arg;
    pthread_mutex_lock(&g_mu);
    while (!g_stop) {
        if (g_len == 0) {
            pthread_cond_wait(&g_cv, &g_mu);
            continue;
        }
        int64_t now = rw_mono_ns();
        if (g_heap[0].deadline_ns <= now) {
            rw_timer_entry e = heap_pop();
            pthread_mutex_unlock(&g_mu);
            if (e.fiber) rw_sched_enqueue_ready(e.fiber);
            else e.fn(e.arg, e.tag);
            pthread_mutex_lock(&g_mu);
            continue;
        }
        /* Linux: g_cv uses CLOCK_MONOTONIC (rw_timer_init). macOS has no
         * pthread_condattr_setclock, so convert to a CLOCK_REALTIME deadline. */
        int64_t d = g_heap[0].deadline_ns;
#if defined(__APPLE__)
        struct timespec rt;
        clock_gettime(CLOCK_REALTIME, &rt);
        d = d - now + (int64_t)rt.tv_sec * 1000000000 + rt.tv_nsec;
#endif
        struct timespec ts = {(time_t)(d / 1000000000), (long)(d % 1000000000)};
        pthread_cond_timedwait(&g_cv, &g_mu, &ts);
    }
    pthread_mutex_unlock(&g_mu);
    return NULL;
}

void rw_timer_init(void) {
    pthread_condattr_t attr;
    pthread_condattr_init(&attr);
#if !defined(__APPLE__)
    pthread_condattr_setclock(&attr, CLOCK_MONOTONIC);
#endif
    pthread_cond_init(&g_cv, &attr);
    pthread_condattr_destroy(&attr);
    g_stop = 0;
    g_len = 0;
    if (pthread_create(&g_thread, NULL, timer_main, NULL) != 0) {
        perror("pthread_create timer");
        abort();
    }
    g_started = 1;
}

void rw_timer_shutdown(void) {
    if (!g_started) return;
    pthread_mutex_lock(&g_mu);
    g_stop = 1;
    pthread_cond_broadcast(&g_cv);
    pthread_mutex_unlock(&g_mu);
    pthread_join(g_thread, NULL);
    g_started = 0;
    free(g_heap);
    g_heap = NULL;
    g_len = g_cap = 0;
    pthread_cond_destroy(&g_cv);
}

static void commit_sleep(rw_fiber_handle *self, void *arg) {
    rw_timer_entry e;
    e.deadline_ns = *(int64_t *)arg;
    e.fiber = self;
    e.fn = NULL;
    e.arg = NULL;
    e.tag = 0;
    pthread_mutex_lock(&g_mu);
    e.seq = g_seq++;
    heap_push(e);
    pthread_cond_signal(&g_cv);
    pthread_mutex_unlock(&g_mu);
}

void rw_sleep_ms(int64_t ms) {
    if (ms < 0) ms = 0;
    if (!rw_sched_current_fiber()) {
        struct timespec ts = {(time_t)(ms / 1000), (long)(ms % 1000) * 1000000};
        while (nanosleep(&ts, &ts) != 0 && errno == EINTR) {}
        return;
    }
    int64_t deadline = rw_mono_ns() + ms * 1000000;  /* on this fiber's stack */
    rw_sched_park_commit(commit_sleep, &deadline);
}

void rw_timer_call_at(int64_t deadline_ns, void (*fn)(void *, uint64_t), void *arg, uint64_t tag) {
    rw_timer_entry e;
    e.deadline_ns = deadline_ns;
    e.fiber = NULL;
    e.fn = fn;
    e.arg = arg;
    e.tag = tag;
    pthread_mutex_lock(&g_mu);
    e.seq = g_seq++;
    heap_push(e);
    pthread_cond_signal(&g_cv);
    pthread_mutex_unlock(&g_mu);
}
