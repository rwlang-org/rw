/*
 * Channels (spec 44).
 *
 * All state is guarded by the channel's mutex. A sender or receiver that
 * cannot proceed appends a waiter (living on its own stack) to the channel
 * and blocks:
 *   - on a worker fiber, it parks with rw_sched_park_commit; the commit
 *     releases the channel mutex only after the fiber's context is saved,
 *     so the side that completes the operation (which needs the mutex) can
 *     never re-enqueue a half-saved fiber (spec 24);
 *   - on a thread that is not a fiber (main), it waits on a per-waiter
 *     condition variable.
 * The completing side copies the value directly into / out of the waiter,
 * marks it done, and wakes it.
 */

#include "chan.h"
#include "fiber/sched.h"

#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct rw_waiter {
    struct rw_waiter *next;
    rw_fiber_handle  *fiber;   /* NULL: a non-fiber thread waiting on cv */
    pthread_cond_t    cv;
    void             *value;   /* send: source; recv: destination */
    int               done;
    int8_t            ok;      /* recv: 1 = got a value, 0 = closed */
} rw_waiter;

typedef struct {
    rw_waiter *head, *tail;
} rw_waitq;

struct rw_chan {
    pthread_mutex_t mu;
    int64_t  cap, elem_size;
    char    *buf;
    int64_t  head, len;        /* ring buffer of cap elements */
    int      closed;
    rw_waitq senders, receivers;
};

static void q_push(rw_waitq *q, rw_waiter *w) {
    w->next = NULL;
    if (q->tail) q->tail->next = w; else q->head = w;
    q->tail = w;
}

static rw_waiter *q_pop(rw_waitq *q) {
    rw_waiter *w = q->head;
    if (w) {
        q->head = w->next;
        if (!q->head) q->tail = NULL;
    }
    return w;
}

rw_chan *rw_chan_new(int64_t capacity, int64_t elem_size) {
    if (capacity < 0) {
        fputs("rw: chan_new: negative capacity\n", stderr);
        abort();
    }
    rw_chan *c = (rw_chan *)calloc(1, sizeof *c);
    if (!c) { fputs("rw: out of memory (channel)\n", stderr); abort(); }
    pthread_mutex_init(&c->mu, NULL);
    c->cap = capacity;
    c->elem_size = elem_size;
    if (capacity > 0) {
        c->buf = (char *)malloc((size_t)(capacity * elem_size));
        if (!c->buf) { fputs("rw: out of memory (channel)\n", stderr); abort(); }
    }
    return c;
}

/* Wake a waiter whose operation the caller (holding c->mu) completed. */
static void wake(rw_waiter *w) {
    w->done = 1;
    if (w->fiber) rw_sched_enqueue_ready(w->fiber);
    else pthread_cond_signal(&w->cv);
}

static void unlock_commit(rw_fiber_handle *self, void *arg) {
    (void)self;
    pthread_mutex_unlock(&((rw_chan *)arg)->mu);
}

/* Block until another party completes w. Called with c->mu held; returns
 * with it released. */
static void block_on(rw_chan *c, rw_waiter *w) {
    w->fiber = rw_sched_current_fiber();
    w->done = 0;
    if (w->fiber) {
        rw_sched_park_commit(unlock_commit, c);   /* releases c->mu */
        return;                                   /* woken with done == 1 */
    }
    pthread_cond_init(&w->cv, NULL);
    while (!w->done) pthread_cond_wait(&w->cv, &c->mu);
    pthread_cond_destroy(&w->cv);
    pthread_mutex_unlock(&c->mu);
}

void rw_chan_send(rw_chan *c, const void *value) {
    pthread_mutex_lock(&c->mu);
    if (c->closed) {
        pthread_mutex_unlock(&c->mu);
        fputs("rw: send on a closed channel\n", stderr);
        abort();
    }
    rw_waiter *r = q_pop(&c->receivers);
    if (r) {  /* hand the value straight to a waiting receiver */
        memcpy(r->value, value, (size_t)c->elem_size);
        r->ok = 1;
        wake(r);
        pthread_mutex_unlock(&c->mu);
        return;
    }
    if (c->len < c->cap) {
        memcpy(c->buf + ((c->head + c->len) % c->cap) * c->elem_size, value, (size_t)c->elem_size);
        c->len++;
        pthread_mutex_unlock(&c->mu);
        return;
    }
    rw_waiter w;
    w.value = (void *)value;
    q_push(&c->senders, &w);
    block_on(c, &w);
    if (!w.ok) {  /* the channel was closed while we waited */
        fputs("rw: send on a closed channel\n", stderr);
        abort();
    }
}

int8_t rw_chan_recv(rw_chan *c, void *value_out) {
    pthread_mutex_lock(&c->mu);
    if (c->len > 0) {
        memcpy(value_out, c->buf + c->head * c->elem_size, (size_t)c->elem_size);
        c->head = (c->head + 1) % c->cap;
        c->len--;
        rw_waiter *s = q_pop(&c->senders);
        if (s) {  /* the freed slot goes to the first blocked sender */
            memcpy(c->buf + ((c->head + c->len) % c->cap) * c->elem_size, s->value,
                   (size_t)c->elem_size);
            c->len++;
            s->ok = 1;
            wake(s);
        }
        pthread_mutex_unlock(&c->mu);
        return 1;
    }
    rw_waiter *s = q_pop(&c->senders);
    if (s) {  /* rendezvous: take the value straight from a waiting sender */
        memcpy(value_out, s->value, (size_t)c->elem_size);
        s->ok = 1;
        wake(s);
        pthread_mutex_unlock(&c->mu);
        return 1;
    }
    if (c->closed) {
        pthread_mutex_unlock(&c->mu);
        return 0;
    }
    rw_waiter w;
    w.value = value_out;
    w.ok = 0;
    q_push(&c->receivers, &w);
    block_on(c, &w);
    return w.ok;
}

void rw_chan_close(rw_chan *c) {
    pthread_mutex_lock(&c->mu);
    if (!c->closed) {
        c->closed = 1;
        rw_waiter *w;
        while ((w = q_pop(&c->receivers)) != NULL) { w->ok = 0; wake(w); }
        while ((w = q_pop(&c->senders)) != NULL) { w->ok = 0; wake(w); }
    }
    pthread_mutex_unlock(&c->mu);
}

int64_t rw_chan_len(rw_chan *c) {
    pthread_mutex_lock(&c->mu);
    int64_t n = c->len;
    pthread_mutex_unlock(&c->mu);
    return n;
}

/* ---------- Mutex (spec 46) ----------
 *
 * Same waiter machinery as channels. unlock hands the lock directly to the
 * first waiter (no barging), so waiters acquire it in FIFO order. */

struct rw_mutex {
    pthread_mutex_t mu;
    int      locked;
    rw_waitq waiters;
};

rw_mutex *rw_mutex_new(void) {
    rw_mutex *m = (rw_mutex *)calloc(1, sizeof *m);
    if (!m) { fputs("rw: out of memory (mutex)\n", stderr); abort(); }
    pthread_mutex_init(&m->mu, NULL);
    return m;
}

static void mutex_unlock_commit(rw_fiber_handle *self, void *arg) {
    (void)self;
    pthread_mutex_unlock(&((rw_mutex *)arg)->mu);
}

void rw_mutex_lock(rw_mutex *m) {
    pthread_mutex_lock(&m->mu);
    if (!m->locked) {
        m->locked = 1;
        pthread_mutex_unlock(&m->mu);
        return;
    }
    rw_waiter w;
    w.value = NULL;
    q_push(&m->waiters, &w);
    w.fiber = rw_sched_current_fiber();
    w.done = 0;
    if (w.fiber) {
        rw_sched_park_commit(mutex_unlock_commit, m);  /* releases m->mu */
        return;  /* the unlocker handed us the lock */
    }
    pthread_cond_init(&w.cv, NULL);
    while (!w.done) pthread_cond_wait(&w.cv, &m->mu);
    pthread_cond_destroy(&w.cv);
    pthread_mutex_unlock(&m->mu);
}

int8_t rw_mutex_try_lock(rw_mutex *m) {
    pthread_mutex_lock(&m->mu);
    int8_t got = !m->locked;
    if (got) m->locked = 1;
    pthread_mutex_unlock(&m->mu);
    return got;
}

void rw_mutex_unlock(rw_mutex *m) {
    pthread_mutex_lock(&m->mu);
    if (!m->locked) {
        pthread_mutex_unlock(&m->mu);
        fputs("rw: unlock of a mutex that is not locked\n", stderr);
        abort();
    }
    rw_waiter *w = q_pop(&m->waiters);
    if (w) {
        wake(w);          /* ownership passes to w; stays locked */
    } else {
        m->locked = 0;
    }
    pthread_mutex_unlock(&m->mu);
}
