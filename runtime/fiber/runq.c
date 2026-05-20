/*
 * Implementation of the Go-style bounded ring run queue (see runq.h).
 *
 * The owner P is the only thread that writes `tail` and is the only
 * caller of rw_runq_get; stealers compete with the owner only on `head`
 * via CAS.
 *
 * In Commit 2 we are still single-threaded, so the atomic operations
 * are effectively just memory_order_relaxed. The atomic typing buys us
 * portability into the multi-threaded commits without touching this
 * file again.
 */

#include "runq.h"

#include <stdlib.h>
#include <string.h>

/* Forward declaration of the opaque fiber handle so we can chain
 * via ->next without pulling sched.c's internal layout in here.
 * The actual struct is defined in sched.c; the linker resolves it. */
struct rw_fiber_handle;
/* We do need to read/write h->next though. Declare it here with the
 * same layout assumption: rw_fiber_handle has a `struct rw_fiber_handle
 * *next` field somewhere. To avoid leaking the layout, we use a tiny
 * helper from sched.c instead. See rw_fiber_handle_get_next /
 * rw_fiber_handle_set_next below. */
extern struct rw_fiber_handle *rw_fiber_handle_get_next(struct rw_fiber_handle *h);
extern void rw_fiber_handle_set_next(struct rw_fiber_handle *h,
                                     struct rw_fiber_handle *next);

void rw_runq_init(rw_P *p) {
    atomic_store_explicit(&p->head, 0, memory_order_relaxed);
    atomic_store_explicit(&p->tail, 0, memory_order_relaxed);
    memset(p->ring, 0, sizeof(p->ring));
}

/* Owner pop. Acquire load on head so we see any prior stealer's CAS
 * before reading ring[head]. */
rw_fiber_handle *rw_runq_get(rw_P *p) {
    for (;;) {
        uint32_t h = atomic_load_explicit(&p->head, memory_order_acquire);
        uint32_t t = atomic_load_explicit(&p->tail, memory_order_relaxed);
        if (h == t) return NULL;
        rw_fiber_handle *g = p->ring[h % RW_RUNQ_CAP];
        /* Try to claim slot h. If a stealer beat us, retry. */
        if (atomic_compare_exchange_strong_explicit(
                &p->head, &h, h + 1,
                memory_order_release, memory_order_relaxed)) {
            return g;
        }
        /* CAS failed: head moved (a stealer took it). Loop. */
    }
}

/* Owner push. If full, move half + g to globq (one critical section). */
void rw_runq_put(rw_P *p, rw_fiber_handle *g, rw_globq *globq) {
    for (;;) {
        uint32_t h = atomic_load_explicit(&p->head, memory_order_acquire);
        uint32_t t = atomic_load_explicit(&p->tail, memory_order_relaxed);
        if (t - h < RW_RUNQ_CAP) {
            p->ring[t % RW_RUNQ_CAP] = g;
            /* Publish the slot before bumping tail (release ordering). */
            atomic_store_explicit(&p->tail, t + 1, memory_order_release);
            return;
        }
        /* Full. Try to evict half to globq. */
        uint32_t n = RW_RUNQ_CAP / 2;
        /* Atomically advance head by n; if a stealer grabs first, retry. */
        if (!atomic_compare_exchange_strong_explicit(
                &p->head, &h, h + n,
                memory_order_acquire, memory_order_relaxed)) {
            continue;
        }
        /* We own slots [h, h+n). Build a linked chain plus the new g. */
        rw_fiber_handle *first = p->ring[h % RW_RUNQ_CAP];
        rw_fiber_handle *prev = first;
        for (uint32_t i = 1; i < n; i++) {
            rw_fiber_handle *cur = p->ring[(h + i) % RW_RUNQ_CAP];
            rw_fiber_handle_set_next(prev, cur);
            prev = cur;
        }
        rw_fiber_handle_set_next(prev, g);
        rw_fiber_handle_set_next(g, NULL);
        rw_globq_push_batch(globq, first, g, n + 1);
        return;
    }
}

/* Stealer: take up to half of src's items into out. */
uint32_t rw_runq_grab(rw_P *src, rw_fiber_handle **out, uint32_t max) {
    for (;;) {
        uint32_t h = atomic_load_explicit(&src->head, memory_order_acquire);
        uint32_t t = atomic_load_explicit(&src->tail, memory_order_acquire);
        uint32_t n = t - h;
        n = n - (n / 2);  /* take ceil(n/2): leaves the victim with floor(n/2) */
        if (n == 0) return 0;
        if (n > max) n = max;
        for (uint32_t i = 0; i < n; i++) {
            out[i] = src->ring[(h + i) % RW_RUNQ_CAP];
        }
        if (atomic_compare_exchange_strong_explicit(
                &src->head, &h, h + n,
                memory_order_release, memory_order_relaxed)) {
            return n;
        }
        /* Retry on conflict. */
    }
}

/* ----- globq ----- */

void rw_globq_init(rw_globq *q) {
    pthread_mutex_init(&q->mu, NULL);
    q->head = q->tail = NULL;
    q->count = 0;
}

void rw_globq_destroy(rw_globq *q) {
    pthread_mutex_destroy(&q->mu);
    q->head = q->tail = NULL;
    q->count = 0;
}

void rw_globq_push(rw_globq *q, rw_fiber_handle *g) {
    rw_fiber_handle_set_next(g, NULL);
    pthread_mutex_lock(&q->mu);
    if (q->tail) {
        rw_fiber_handle_set_next(q->tail, g);
        q->tail = g;
    } else {
        q->head = q->tail = g;
    }
    q->count++;
    pthread_mutex_unlock(&q->mu);
}

rw_fiber_handle *rw_globq_pop(rw_globq *q) {
    pthread_mutex_lock(&q->mu);
    rw_fiber_handle *g = q->head;
    if (g) {
        q->head = rw_fiber_handle_get_next(g);
        if (!q->head) q->tail = NULL;
        q->count--;
        rw_fiber_handle_set_next(g, NULL);
    }
    pthread_mutex_unlock(&q->mu);
    return g;
}

uint32_t rw_globq_pop_batch(rw_globq *q, rw_fiber_handle **out, uint32_t max) {
    uint32_t n = 0;
    pthread_mutex_lock(&q->mu);
    while (n < max && q->head) {
        rw_fiber_handle *g = q->head;
        q->head = rw_fiber_handle_get_next(g);
        if (!q->head) q->tail = NULL;
        q->count--;
        rw_fiber_handle_set_next(g, NULL);
        out[n++] = g;
    }
    pthread_mutex_unlock(&q->mu);
    return n;
}

void rw_globq_push_batch(rw_globq *q, rw_fiber_handle *head,
                         rw_fiber_handle *tail, uint32_t n) {
    rw_fiber_handle_set_next(tail, NULL);
    pthread_mutex_lock(&q->mu);
    if (q->tail) {
        rw_fiber_handle_set_next(q->tail, head);
        q->tail = tail;
    } else {
        q->head = head;
        q->tail = tail;
    }
    q->count += n;
    pthread_mutex_unlock(&q->mu);
}
