/*
 * Cooperative single-threaded fiber scheduler for rw.
 *
 * See sched.h for the API surface.
 *
 * Implementation notes
 * --------------------
 *
 * Each fiber lives in a `rw_fiber_handle`. Key state:
 *   - ctx        : the saved callee-saved register file
 *   - stack_base : start of the mmap'd region (low address)
 *   - stack_size : usable bytes between guard pages
 *   - state      : READY | RUNNING | DONE
 *   - result     : union typed by result_kind
 *
 * A single thread-local "scheduler" context lives in `g_sched_ctx`.
 * When a fiber yields, it swaps into the scheduler. The scheduler
 * pops the next READY handle from `g_ready_head/tail` and swaps into it.
 * Fibers that finish set their state to DONE and swap back to the
 * scheduler; the scheduler then picks the next READY one. If the ready
 * queue is empty, the scheduler swaps back to whatever fiber called
 * `rw_sched_yield` originally (i.e. the calling fiber resumes).
 *
 * Joining a fiber:
 *   while (!done(h)) rw_sched_yield();
 *
 * That is, the joining fiber repeatedly yields. Other fibers run in
 * between and eventually our target finishes. Then we return its value.
 *
 * Termination wrapper:
 *   `fiber_trampoline_<T>` is the entry function bound to every spawned
 *   fiber. It calls `user_fn(arg)`, stores the result in the handle,
 *   marks it DONE, and yields back to the scheduler permanently.
 *
 * Stack layout:
 *   [guard page | usable stack | guard page]
 *   The guard pages are PROT_NONE so any over/underflow becomes SIGSEGV.
 */

#include "sched.h"
#include "fiber.h"
#include "park.h"
#include "runq.h"

#include <errno.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

/* macOS exposes MAP_ANON, Linux glibc uses MAP_ANONYMOUS unless
 * _DEFAULT_SOURCE / _GNU_SOURCE is set (we set the latter via
 * -D_GNU_SOURCE in the Makefile). Cover both spellings. */
#if !defined(MAP_ANON) && defined(MAP_ANONYMOUS)
#  define MAP_ANON MAP_ANONYMOUS
#endif

#define RW_FIBER_STACK_USABLE (64 * 1024)

/* The guard page size and total region size are derived from the host
 * page size at init time (macOS/arm64 uses 16K pages, Linux/x86_64 4K).
 * sysconf(_SC_PAGESIZE) is the portable accessor. */
static size_t g_page_size = 0;
static size_t g_region_size = 0;

static size_t rw_region_size(void) { return g_region_size; }
static size_t rw_guard_size(void)  { return g_page_size; }

/* Fiber lifecycle states.
 *
 * WAITING is reserved for the upcoming M:N work (a joining fiber parks on
 * another handle's wait list and is in WAITING until the target's
 * trampoline releases waiters). It is unused in the single-threaded
 * scheduler but added now so the atomic load/store sites don't change
 * shape when we wire up park.c. */
typedef enum {
    RW_FIBER_READY = 0,
    RW_FIBER_RUNNING,
    RW_FIBER_WAITING,
    RW_FIBER_DONE,
} rw_fiber_state;

typedef enum {
    RW_RES_I64,
    RW_RES_F64,
    RW_RES_BOOL,
    RW_RES_STR,
    RW_RES_VOID,
} rw_result_kind;

struct rw_fiber_handle {
    rw_fiber_ctx     ctx;
    void            *region;       /* full mmap'd region, including guards */
    /* `state` is the synchronization edge between the trampoline (which
     * publishes the result and stores DONE with release ordering) and any
     * joiner (which reads DONE with acquire ordering and then reads the
     * result field). Internal READY<->RUNNING transitions happen in the
     * scheduler's serialized path so they use relaxed ordering. */
    _Atomic int      state;        /* values are rw_fiber_state */
    rw_result_kind   kind;
    /* The user fn and arg are kept so the trampoline can find them. */
    void            *user_fn;
    void            *user_arg;
    union {
        int64_t i64;
        double  f64;
        int8_t  b;
        rw_str  s;
    } result;
    /* Singly-linked list pointers: doubles as the ready-queue link
     * AND the wait-list link. A handle is on at most one of {ready
     * queue, wait list of some target, currently RUNNING/DONE} at any
     * moment, so reusing the pointer is safe. */
    struct rw_fiber_handle *next;
    /* Per-handle mutex/condvar used by joiners that are NOT themselves
     * running on a worker thread (i.e., the main thread). A worker
     * fiber that wants to join another handle uses the wait-list path
     * below; the main thread, having no fiber ctx to yield from,
     * blocks here. The trampoline broadcasts on join_cv after
     * release-storing DONE. */
    pthread_mutex_t  join_mu;
    pthread_cond_t   join_cv;
    /* Wait list of joining fibers (Commit 4). Guarded by `wait_lock`.
     * The trampoline takes the lock once, splices the entire list
     * out, then re-enqueues each waiter onto a runnable queue. */
    rw_wait_lock     wait_lock;
    struct rw_fiber_handle *wait_head;
};

/* ---- M:N scheduler globals (single-worker variant, Commit 3) ----
 *
 * The single P's bounded ring is owned by the single worker M. The
 * main thread does NOT touch the ring directly; main-side spawns push
 * into the global queue, and the worker pulls from there into its
 * local ring on demand.
 *
 * We have exactly one M (`g_worker`) for now. Commit 5 will scale this
 * to N. Each M has its own scheduler ctx (the swap target when a fiber
 * yields or finishes) and a pointer to the fiber it is currently
 * running.
 */

typedef struct rw_M {
    pthread_t        thread;
    int              id;
    rw_fiber_ctx     sched_ctx;
    rw_fiber_handle *current;
    rw_P            *p;
    /* xorshift64 PRNG state used to pick a random starting victim for
     * work-stealing. Seeded per-M at init. */
    uint64_t         rng_state;
} rw_M;

#define RW_MAX_WORKERS 64

static rw_P     g_ps[RW_MAX_WORKERS];
static rw_M     g_workers[RW_MAX_WORKERS];
static int      g_nworkers;
static rw_globq g_globq;

/* Thread-local pointer to the worker that owns the current thread, or
 * NULL on the main / external thread. This is the single source of
 * truth for "am I inside a fiber on a worker M?" used by spawn and
 * join. */
static _Thread_local rw_M *tls_m = NULL;

/* Wakeup channel for workers. A worker that has no local work and
 * finds globq empty parks on g_sched_cv. main signals (or broadcasts
 * on shutdown) after publishing work into globq. We use signal
 * (single wakeup) on spawn so we don't thunder-herd N workers when
 * only one fiber is added. */
static pthread_mutex_t g_sched_mu = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t  g_sched_cv = PTHREAD_COND_INITIALIZER;

/* Shutdown signal. Set by rw_sched_shutdown, observed by every worker
 * loop at the top of each iteration. */
static _Atomic int g_shutdown = 0;

/* Have workers been started? Used to keep rw_sched_shutdown
 * idempotent and to skip pthread_join when init was never called. */
static int g_workers_started = 0;

/* Helpers exposed to runq.c so it can chain handles without depending
 * on the layout of rw_fiber_handle (which is private to this file). */
rw_fiber_handle *rw_fiber_handle_get_next(rw_fiber_handle *h) {
    return h ? h->next : NULL;
}
void rw_fiber_handle_set_next(rw_fiber_handle *h, rw_fiber_handle *next) {
    if (h) h->next = next;
}

static void rw_die(const char *msg) {
    perror(msg);
    abort();
}

/* Push a fiber onto a ready queue. Two callers:
 *   - main thread on spawn (tls_m == NULL): push to globq + signal.
 *   - worker thread (tls_m != NULL): push to its P's local ring.
 *
 * The state transition to READY is relaxed: state's only synchronizing
 * role is RUNNING -> DONE (Commit 1). */
static void enqueue_ready(rw_fiber_handle *h) {
    atomic_store_explicit(&h->state, RW_FIBER_READY, memory_order_relaxed);
    rw_M *m = tls_m;
    if (m) {
        rw_runq_put(m->p, h, &g_globq);
        /* If any other worker is parked, wake one so it can come
         * steal from us (or pull from globq if we overflowed). This
         * is a single-wakeup signal: thunder-herding all N workers
         * for one fiber wastes cycles. */
        if (g_nworkers > 1) {
            pthread_mutex_lock(&g_sched_mu);
            pthread_cond_signal(&g_sched_cv);
            pthread_mutex_unlock(&g_sched_mu);
        }
    } else {
        /* main -> globq. Take g_sched_mu so the signal can't race the
         * worker's "is globq empty?" check on its way to cond_wait. */
        rw_globq_push(&g_globq, h);
        pthread_mutex_lock(&g_sched_mu);
        pthread_cond_signal(&g_sched_cv);
        pthread_mutex_unlock(&g_sched_mu);
    }
}

/* xorshift64 — small, fast, good enough to pick a starting victim
 * for stealing. Each M owns its own state, so this needs no locks. */
static inline uint64_t xorshift64(uint64_t *state) {
    uint64_t x = *state;
    x ^= x << 13;
    x ^= x >> 7;
    x ^= x << 17;
    *state = x;
    return x;
}

/* Try to steal work from another worker's P. Walks all other Ps in a
 * round starting from a random offset, and returns the first fiber
 * we successfully grabbed. If the grab returned more than one, the
 * extras are pushed to our own P so subsequent find_runnable calls
 * (or other stealers) can pick them up cheaply. */
static rw_fiber_handle *try_steal(rw_M *m) {
    if (g_nworkers <= 1) return NULL;
    uint32_t offset = (uint32_t)(xorshift64(&m->rng_state) % (uint32_t)g_nworkers);
    rw_fiber_handle *batch[RW_RUNQ_CAP / 2];
    for (int i = 0; i < g_nworkers; i++) {
        int idx = (int)((offset + (uint32_t)i) % (uint32_t)g_nworkers);
        if (idx == m->id) continue;
        rw_P *victim = &g_ps[idx];
        uint32_t got = rw_runq_grab(victim, batch, RW_RUNQ_CAP / 2);
        if (got == 0) continue;
        /* Keep batch[0] for ourselves; push the rest onto our own P. */
        for (uint32_t k = 1; k < got; k++) {
            rw_runq_put(m->p, batch[k], &g_globq);
        }
        return batch[0];
    }
    return NULL;
}

/* Worker-side: find a runnable fiber. Returns NULL only when shutdown
 * has been requested. Otherwise blocks on g_sched_cv until work shows
 * up. Called from the worker's scheduler ctx only. */
static rw_fiber_handle *find_runnable(rw_M *m) {
    for (;;) {
        if (atomic_load_explicit(&g_shutdown, memory_order_acquire)) {
            return NULL;
        }
        rw_fiber_handle *h = rw_runq_get(m->p);
        if (h) return h;
        /* Refill from globq. */
        rw_fiber_handle *batch[RW_RUNQ_CAP / 2];
        uint32_t n = rw_globq_pop_batch(&g_globq, batch, RW_RUNQ_CAP / 2);
        if (n > 0) {
            for (uint32_t i = 1; i < n; i++) {
                rw_runq_put(m->p, batch[i], &g_globq);
            }
            return batch[0];
        }
        /* Local empty, globq empty. Try to steal half from another P. */
        h = try_steal(m);
        if (h) return h;
        /* Truly nothing to run. Park until something is pushed or
         * shutdown is set. */
        pthread_mutex_lock(&g_sched_mu);
        /* Re-check shutdown and globq under the lock to close the race
         * with concurrent spawn/shutdown. We do NOT re-check other Ps
         * here: if they got new work between our try_steal and now, a
         * spawn signal will arrive and wake us. */
        if (atomic_load_explicit(&g_shutdown, memory_order_acquire)) {
            pthread_mutex_unlock(&g_sched_mu);
            return NULL;
        }
        if (g_globq.count == 0) {
            pthread_cond_wait(&g_sched_cv, &g_sched_mu);
        }
        pthread_mutex_unlock(&g_sched_mu);
    }
}

/* Cooperative yield. Only meaningful when called from inside a fiber
 * running on a worker (tls_m != NULL). If called from the main thread
 * outside of any fiber, this is a no-op: main doesn't have a fiber ctx
 * to swap from.
 *
 * Important: we do NOT re-enqueue the fiber here. Doing so would make
 * the fiber's ctx visible to other workers (via work-stealing) BEFORE
 * rw_fiber_swap has finished saving the ctx, which would cause a
 * stealer that swaps-in to read a half-written register file. Instead,
 * we swap to the worker's scheduler ctx and let worker_main do the
 * re-enqueue from its own stack, AFTER our ctx has been fully saved. */
void rw_sched_yield(void) {
    rw_M *m = tls_m;
    if (!m) return;
    rw_fiber_handle *me = m->current;
    if (!me) return;
    rw_fiber_swap(&me->ctx, &m->sched_ctx);
}

/* Worker entry point. Sets tls_m and loops on find_runnable + execute
 * until shutdown. find_runnable's NULL return is the only way out.
 *
 * The "re-enqueue if still RUNNING" decision is made here, not inside
 * rw_sched_yield, because the fiber's ctx is only fully saved once
 * the inbound rw_fiber_swap returns into this loop. Re-enqueuing
 * earlier would let a stealer pick up the fiber while its ctx is
 * still being written. */
static void *worker_main(void *arg) {
    rw_M *m = (rw_M *)arg;
    tls_m = m;
    for (;;) {
        rw_fiber_handle *g = find_runnable(m);
        if (!g) break;
        atomic_store_explicit(&g->state, RW_FIBER_RUNNING,
                              memory_order_relaxed);
        m->current = g;
        rw_fiber_swap(&m->sched_ctx, &g->ctx);
        /* Control returns here once g either yielded (state still
         * RUNNING) or finished (state == DONE) or parked on a wait
         * list (state == WAITING). Only the RUNNING case puts g back
         * on the queue; DONE / WAITING fibers re-enter the queue via
         * a different path (finalize_fiber for DONE, the target
         * trampoline for WAITING). */
        if (atomic_load_explicit(&g->state, memory_order_relaxed)
            == RW_FIBER_RUNNING) {
            enqueue_ready(g);
        }
        m->current = NULL;
    }
    tls_m = NULL;
    return NULL;
}

/* Park the calling fiber on target->waiters and yield. Returns when
 * the trampoline puts us back on a run queue and a worker resumes us.
 * On entry: caller is running on a worker, has not yet observed DONE
 * on target, and wants to wait for it. */
static void park_on(rw_fiber_handle *target) {
    rw_M *m = tls_m;
    rw_fiber_handle *me = m->current;

    rw_wait_lock_acquire(&target->wait_lock);
    /* Re-check state under the lock to close the race with a
     * trampoline that is just about to take the wait list. */
    if (atomic_load_explicit(&target->state, memory_order_acquire)
        == RW_FIBER_DONE) {
        rw_wait_lock_release(&target->wait_lock);
        return;
    }
    me->next = target->wait_head;
    target->wait_head = me;
    atomic_store_explicit(&me->state, RW_FIBER_WAITING,
                          memory_order_relaxed);
    rw_wait_lock_release(&target->wait_lock);

    /* Yield to the scheduler. enqueue_ready will skip us because our
     * state is WAITING, not RUNNING. We resume only when the
     * trampoline of `target` re-enqueues us as READY. */
    rw_fiber_swap(&me->ctx, &m->sched_ctx);
}

/* Common fiber-completion path: publish result + DONE, broadcast main
 * waiters, release fiber waiters, swap back to the worker's scheduler
 * ctx. Called from every fiber_entry_<T> trampoline. The result must
 * already be written into h->result before calling this. */
static void finalize_fiber(rw_fiber_handle *h) {
    /* Publish: result write (already done by caller) happens-before
     * this release-store of DONE; any joiner that observes DONE with
     * acquire ordering will see the result. */
    atomic_store_explicit(&h->state, RW_FIBER_DONE, memory_order_release);

    /* Wake main-thread joiner if any. The mutex synchronizes with a
     * joiner just about to call cond_wait. */
    pthread_mutex_lock(&h->join_mu);
    pthread_cond_broadcast(&h->join_cv);
    pthread_mutex_unlock(&h->join_mu);

    /* Take the fiber wait list in one shot. */
    rw_wait_lock_acquire(&h->wait_lock);
    rw_fiber_handle *waiters = h->wait_head;
    h->wait_head = NULL;
    rw_wait_lock_release(&h->wait_lock);

    /* Re-enqueue each waiter onto a ready queue. They will resume
     * inside park_on, observe DONE, and return. */
    while (waiters) {
        rw_fiber_handle *next = waiters->next;
        waiters->next = NULL;
        /* enqueue_ready sets state = READY and pushes. */
        enqueue_ready(waiters);
        waiters = next;
    }

    rw_M *m = tls_m;
    rw_fiber_swap(&h->ctx, &m->sched_ctx);
}

/* The fiber's entry point. Calls user fn, stores result, marks DONE,
 * and yields permanently. We must NEVER let the fiber return into the
 * trampoline (the trampoline would brk #0xdead).
 *
 * The store of `result` is plain (non-atomic). The release-store of
 * `state = DONE` synchronizes with the acquire-load in the joiner; the
 * joiner only reads `result` *after* observing DONE, so the result write
 * happens-before that read. */
#define DEFINE_TRAMP(NAME, RETTY, FIELD)                                    \
    static void fiber_entry_##NAME(void *arg) {                             \
        rw_fiber_handle *h = (rw_fiber_handle *)arg;                        \
        RETTY (*fn)(void *) = (RETTY (*)(void *))h->user_fn;                \
        h->result.FIELD = fn(h->user_arg);                                  \
        finalize_fiber(h);                                                  \
        /* unreachable */                                                   \
        abort();                                                            \
    }

DEFINE_TRAMP(i64,  int64_t, i64)
DEFINE_TRAMP(f64,  double,  f64)
DEFINE_TRAMP(bool, int8_t,  b)
DEFINE_TRAMP(str,  rw_str,  s)

/* void has no field; specialize manually. */
static void fiber_entry_void(void *arg) {
    rw_fiber_handle *h = (rw_fiber_handle *)arg;
    void (*fn)(void *) = (void (*)(void *))h->user_fn;
    fn(h->user_arg);
    finalize_fiber(h);
    abort();
}

static rw_fiber_handle *spawn_common(rw_result_kind kind,
                                     void *user_fn,
                                     void *user_arg,
                                     void (*tramp)(void *)) {
    rw_fiber_handle *h = (rw_fiber_handle *)calloc(1, sizeof(*h));
    if (!h) rw_die("calloc fiber handle");
    h->kind = kind;
    h->user_fn = user_fn;
    h->user_arg = user_arg;
    /* calloc zeros the storage so state is RW_FIBER_READY (0) already;
     * make the initialization explicit since the field is _Atomic. */
    atomic_store_explicit(&h->state, RW_FIBER_READY, memory_order_relaxed);
    /* Per-handle join mutex/condvar for main-thread joiners. */
    pthread_mutex_init(&h->join_mu, NULL);
    pthread_cond_init(&h->join_cv, NULL);
    /* Empty wait list for fiber-thread joiners. */
    rw_wait_lock_init(&h->wait_lock);
    h->wait_head = NULL;

    void *region = mmap(NULL, rw_region_size(),
                        PROT_READ | PROT_WRITE,
                        MAP_PRIVATE | MAP_ANON, -1, 0);
    if (region == MAP_FAILED) rw_die("mmap fiber stack");
    /* Protect the guard pages. */
    if (mprotect(region, rw_guard_size(), PROT_NONE) != 0) {
        rw_die("mprotect low guard");
    }
    if (mprotect((char *)region + rw_guard_size() + RW_FIBER_STACK_USABLE,
                 rw_guard_size(), PROT_NONE) != 0) {
        rw_die("mprotect high guard");
    }
    h->region = region;
    void *stack_lo = (char *)region + rw_guard_size();
    rw_fiber_ctx_init(&h->ctx, stack_lo, RW_FIBER_STACK_USABLE, tramp, h);
    enqueue_ready(h);
    return h;
}

rw_fiber_handle *rw_sched_spawn_i64 (int64_t (*fn)(void *), void *arg) {
    return spawn_common(RW_RES_I64,  (void *)fn, arg, fiber_entry_i64);
}
rw_fiber_handle *rw_sched_spawn_f64 (double  (*fn)(void *), void *arg) {
    return spawn_common(RW_RES_F64,  (void *)fn, arg, fiber_entry_f64);
}
rw_fiber_handle *rw_sched_spawn_bool(int8_t  (*fn)(void *), void *arg) {
    return spawn_common(RW_RES_BOOL, (void *)fn, arg, fiber_entry_bool);
}
rw_fiber_handle *rw_sched_spawn_str (rw_str  (*fn)(void *), void *arg) {
    return spawn_common(RW_RES_STR,  (void *)fn, arg, fiber_entry_str);
}
rw_fiber_handle *rw_sched_spawn_void(void    (*fn)(void *), void *arg) {
    return spawn_common(RW_RES_VOID, (void *)fn, arg, fiber_entry_void);
}

static void free_handle(rw_fiber_handle *h) {
    pthread_mutex_destroy(&h->join_mu);
    pthread_cond_destroy(&h->join_cv);
    if (h->region) {
        munmap(h->region, rw_region_size());
    }
    free(h);
}

/* Wait until h is DONE.
 *
 *   - Inside a fiber on a worker (tls_m != NULL): park on h's wait
 *     list, yield, resume when the trampoline of h has re-enqueued
 *     us. This makes "fiber awaits fiber" a true cooperative block:
 *     while we are WAITING, the worker is free to run other fibers.
 *   - From the main thread (tls_m == NULL): block on the per-handle
 *     condvar. The trampoline broadcasts after release-storing DONE.
 *     We re-check `state` under the mutex to close the lost-wakeup
 *     race with a trampoline that finishes just before we sleep. */
static void wait_done(rw_fiber_handle *h) {
    if (tls_m) {
        while (atomic_load_explicit(&h->state, memory_order_acquire)
               != RW_FIBER_DONE) {
            park_on(h);
        }
        return;
    }
    pthread_mutex_lock(&h->join_mu);
    while (atomic_load_explicit(&h->state, memory_order_acquire)
           != RW_FIBER_DONE) {
        pthread_cond_wait(&h->join_cv, &h->join_mu);
    }
    pthread_mutex_unlock(&h->join_mu);
}

#define DEFINE_JOIN(NAME, RETTY, FIELD)                                    \
    RETTY rw_sched_join_##NAME(rw_fiber_handle *h) {                       \
        wait_done(h);                                                      \
        RETTY r = h->result.FIELD;                                         \
        free_handle(h);                                                    \
        return r;                                                          \
    }

DEFINE_JOIN(i64,  int64_t, i64)
DEFINE_JOIN(f64,  double,  f64)
DEFINE_JOIN(bool, int8_t,  b)
DEFINE_JOIN(str,  rw_str,  s)

void rw_sched_join_void(rw_fiber_handle *h) {
    wait_done(h);
    free_handle(h);
}

static int parse_workers(void) {
    long online = sysconf(_SC_NPROCESSORS_ONLN);
    int n = (online > 0) ? (int)online : 2;
    const char *env = getenv("RW_WORKERS");
    if (env && *env) {
        char *endp = NULL;
        long v = strtol(env, &endp, 10);
        if (endp && *endp == '\0' && v >= 1 && v <= RW_MAX_WORKERS) {
            n = (int)v;
        }
    }
    if (n < 1) n = 1;
    if (n > RW_MAX_WORKERS) n = RW_MAX_WORKERS;
    return n;
}

void rw_sched_init(void) {
    long ps = sysconf(_SC_PAGESIZE);
    if (ps <= 0) ps = 4096;  /* fallback */
    /* Round usable stack up to a page boundary so the layout stays clean. */
    size_t usable = RW_FIBER_STACK_USABLE;
    if (usable % (size_t)ps != 0) {
        usable = ((usable / (size_t)ps) + 1) * (size_t)ps;
    }
    g_page_size = (size_t)ps;
    g_region_size = (size_t)ps + usable + (size_t)ps;
    rw_globq_init(&g_globq);
    atomic_store_explicit(&g_shutdown, 0, memory_order_release);

    g_nworkers = parse_workers();
    for (int i = 0; i < g_nworkers; i++) {
        rw_runq_init(&g_ps[i]);
        memset(&g_workers[i], 0, sizeof(g_workers[i]));
        g_workers[i].id = i;
        g_workers[i].p = &g_ps[i];
        /* Seed xorshift64 with a distinct nonzero value per M.
         * The exact seed doesn't matter; only that the streams diverge
         * so two workers don't pick the same first victim. */
        g_workers[i].rng_state = 0x9E3779B97F4A7C15ull ^ ((uint64_t)i + 1);
    }
    /* Spawn all workers AFTER all Ps are initialised so a worker that
     * starts fast can't see a half-initialised neighbour P. */
    for (int i = 0; i < g_nworkers; i++) {
        if (pthread_create(&g_workers[i].thread, NULL,
                           worker_main, &g_workers[i]) != 0) {
            rw_die("pthread_create worker");
        }
    }
    g_workers_started = 1;
}

void rw_sched_shutdown(void) {
    if (!g_workers_started) {
        rw_globq_destroy(&g_globq);
        return;
    }
    /* Signal shutdown and wake every worker so it can observe it. */
    pthread_mutex_lock(&g_sched_mu);
    atomic_store_explicit(&g_shutdown, 1, memory_order_release);
    pthread_cond_broadcast(&g_sched_cv);
    pthread_mutex_unlock(&g_sched_mu);
    for (int i = 0; i < g_nworkers; i++) {
        pthread_join(g_workers[i].thread, NULL);
    }
    g_workers_started = 0;

    /* Reclaim any unjoined fibers still on any queue. */
    rw_fiber_handle *h;
    for (int i = 0; i < g_nworkers; i++) {
        while ((h = rw_runq_get(&g_ps[i])) != NULL) free_handle(h);
    }
    while ((h = rw_globq_pop(&g_globq)) != NULL) free_handle(h);
    rw_globq_destroy(&g_globq);
}
