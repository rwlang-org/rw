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
#include <time.h>
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
    /* Ownership handoff for freeing the handle (spec 19). The worker sets
     * REAP_FINISHED once the fiber is DONE *and* has swapped off its stack;
     * the owner sets REAP_RELEASED after join read the result, or on
     * detach. Both use an atomic exchange and whoever arrives second frees,
     * so each handle is freed exactly once and never while in use. */
    _Atomic int      reap;
    /* Position in the global completion order, set just before DONE is
     * published (spec 45: await_any returns the first future to finish). */
    uint64_t         finish_seq;
};

static _Atomic uint64_t g_finish_seq = 0;

enum {
    REAP_NONE = 0,
    REAP_FINISHED,
    REAP_RELEASED,
};

/* Spawned-minus-freed handles; read by tests via rw_sched_live_fibers(). */
static _Atomic int64_t g_live_fibers = 0;

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
    /* Set by finalize_fiber just before the finishing fiber swaps back to
     * this M. A finished fiber always returns to the M that ran it, so this
     * M-local flag (not g->state, which other threads may be changing) is
     * how worker_main knows the fiber it resumed has finished. */
    rw_fiber_handle *finished;
    rw_P            *p;
    /* xorshift64 PRNG state used to pick a random starting victim for
     * work-stealing. Seeded per-M at init. */
    uint64_t         rng_state;
    /* Preemption (spec 23). `tick` counts fiber resumptions and `running`
     * is 1 while a fiber is on this M; the monitor thread reads both to
     * find a fiber that has outrun its slice and then sets `preempt`. */
    _Atomic uint64_t tick;
    _Atomic int      running;
    _Atomic int      preempt;
    /* Set by rw_safepoint right before it yields, so worker_main sends the
     * preempted fiber to the back of globq rather than the local runq. */
    int              preempted;
    /* Deferred publication for a parking fiber (spec 24): set by
     * rw_sched_park_commit, run by worker_main after the swap has saved
     * the fiber's ctx, so no waker can see the fiber before then. */
    void           (*park_commit)(rw_fiber_handle *, void *);
    void            *park_arg;
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

/* Number of pending preemption requests across all workers. Compiled code
 * loads it at every safe point and calls rw_safepoint() when it is nonzero
 * (spec 23). */
_Atomic int32_t rw_preempt_flag = 0;

/* Monitor thread (spec 23). g_preempt_ns == 0 means preemption is off. */
static pthread_t g_monitor;
static int       g_monitor_started = 0;
static int64_t   g_preempt_ns = 0;

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

/* Public wrapper for the netpoller (see sched.h). */
void rw_sched_enqueue_ready(rw_fiber_handle *h) {
    enqueue_ready(h);
}

/* Public wrapper for the netpoller (see sched.h). */
rw_fiber_handle *rw_sched_current_fiber(void) {
    rw_M *m = tls_m;
    return m ? m->current : NULL;
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
        /* Fairness: every 61st scheduling round, take from globq first.
         * Otherwise a fiber that keeps yielding (e.g. when preempted,
         * spec 23) is re-enqueued locally and picked again forever, and
         * fibers spawned from the main thread into globq never run. The
         * interval is Go's (schedule() checks the global queue every 61
         * schedticks). */
        if (atomic_load_explicit(&m->tick, memory_order_relaxed) % 61 == 0) {
            rw_fiber_handle *gh = rw_globq_pop(&g_globq);
            if (gh) return gh;
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

/* Called by compiled code at a safe point when rw_preempt_flag != 0
 * (spec 23). Yields if this worker has been asked to preempt its fiber;
 * otherwise (another worker's request, or the main thread) returns. */
void rw_safepoint(void) {
    rw_M *m = tls_m;
    if (!m || !m->current) return;
    if (atomic_exchange_explicit(&m->preempt, 0, memory_order_relaxed)) {
        atomic_fetch_sub_explicit(&rw_preempt_flag, 1, memory_order_relaxed);
        m->preempted = 1;
        rw_sched_yield();
    }
}

static int64_t monotonic_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000000000 + ts.tv_nsec;
}

/* Every millisecond, look for a worker whose current fiber has been running
 * for at least one slice (same `tick` since we first saw it) and ask it to
 * yield at its next safe point. */
static void *monitor_main(void *arg) {
    (void)arg;
    uint64_t last_tick[RW_MAX_WORKERS] = {0};
    int64_t  since[RW_MAX_WORKERS] = {0};
    const struct timespec period = {0, 1000000};
    while (!atomic_load_explicit(&g_shutdown, memory_order_acquire)) {
        nanosleep(&period, NULL);
        int64_t now = monotonic_ns();
        for (int i = 0; i < g_nworkers; i++) {
            rw_M *m = &g_workers[i];
            uint64_t t = atomic_load_explicit(&m->tick, memory_order_relaxed);
            if (t != last_tick[i] || !atomic_load_explicit(&m->running, memory_order_relaxed)) {
                last_tick[i] = t;
                since[i] = now;
                continue;
            }
            if (now - since[i] >= g_preempt_ns) {
                if (!atomic_exchange_explicit(&m->preempt, 1, memory_order_relaxed)) {
                    atomic_fetch_add_explicit(&rw_preempt_flag, 1, memory_order_relaxed);
                }
                since[i] = now;  /* re-arm: ask again after another slice */
            }
        }
    }
    return NULL;
}

static int64_t parse_preempt_ns(void) {
    long ms = 10;
    const char *env = getenv("RW_PREEMPT_MS");
    if (env && *env) {
        char *endp = NULL;
        long v = strtol(env, &endp, 10);
        if (endp && *endp == '\0' && v >= 0 && v <= 60000) ms = v;
    }
    return (int64_t)ms * 1000000;
}

/* Park the current fiber (spec 24): mark it WAITING and swap to sched_ctx;
 * `commit(self, arg)` then runs on the worker's scheduler stack after the
 * fiber's ctx is fully saved. `commit` is what makes the fiber wakeable
 * (registers it with the netpoller, queues an aio task, joins a wait
 * list) — doing that before the swap would let a waker resume a fiber
 * whose registers are only half saved. If `commit` cannot arrange a
 * wake-up it must call rw_sched_enqueue_ready(self) itself. */
void rw_sched_park_commit(void (*commit)(rw_fiber_handle *, void *), void *arg) {
    rw_M *m = tls_m;
    if (!m) return;
    rw_fiber_handle *me = m->current;
    if (!me) return;
    atomic_store_explicit(&me->state, RW_FIBER_WAITING,
                          memory_order_relaxed);
    m->park_commit = commit;
    m->park_arg = arg;
    rw_fiber_swap(&me->ctx, &m->sched_ctx);
}

static void free_handle(rw_fiber_handle *h);

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
        atomic_fetch_add_explicit(&m->tick, 1, memory_order_relaxed);
        atomic_store_explicit(&m->running, 1, memory_order_relaxed);
        rw_fiber_swap(&m->sched_ctx, &g->ctx);
        atomic_store_explicit(&m->running, 0, memory_order_relaxed);
        /* A request meant for g is void once g has left this M (it parked,
         * finished, or yielded at a safe point that already cleared it). */
        if (atomic_exchange_explicit(&m->preempt, 0, memory_order_relaxed)) {
            atomic_fetch_sub_explicit(&rw_preempt_flag, 1, memory_order_relaxed);
        }
        /* Control returns here once g either yielded (state still
         * RUNNING) or finished (state == DONE) or parked on a wait
         * list (state == WAITING). Only the RUNNING case puts g back
         * on the queue; DONE / WAITING fibers re-enter the queue via
         * a different path (finalize_fiber for DONE, the target
         * trampoline for WAITING). */
        m->current = NULL;
        if (m->park_commit) {
            /* g parked: its ctx is saved, so it is now safe to make it
             * wakeable (register with the netpoller, queue an aio task,
             * join a wait list). It may be resumed elsewhere right away,
             * so g must not be touched after this. */
            void (*commit)(rw_fiber_handle *, void *) = m->park_commit;
            void *arg = m->park_arg;
            m->park_commit = NULL;
            m->park_arg = NULL;
            m->preempted = 0;
            commit(g, arg);
            continue;
        }
        if (m->finished == g) {
            /* g finished on this M: its ctx is fully saved and its stack
             * is no longer in use. Hand it to its owner, or free it if the
             * owner already let go (spec 19). */
            m->finished = NULL;
            if (atomic_exchange_explicit(&g->reap, REAP_FINISHED,
                                         memory_order_acq_rel)
                == REAP_RELEASED) {
                free_handle(g);
            }
        } else if (atomic_load_explicit(&g->state, memory_order_relaxed)
                   == RW_FIBER_RUNNING) {
            if (m->preempted) {
                /* Like Go, a preempted fiber goes to the back of the
                 * global queue, so the fibers it held up run first. */
                atomic_store_explicit(&g->state, RW_FIBER_READY, memory_order_relaxed);
                rw_globq_push(&g_globq, g);
                pthread_mutex_lock(&g_sched_mu);
                pthread_cond_signal(&g_sched_cv);
                pthread_mutex_unlock(&g_sched_mu);
            } else {
                enqueue_ready(g);
            }
        }
        m->preempted = 0;
    }
    tls_m = NULL;
    return NULL;
}

/* Park the calling fiber on target->waiters and yield. Returns when
 * the trampoline puts us back on a run queue and a worker resumes us.
 * On entry: caller is running on a worker, has not yet observed DONE
 * on target, and wants to wait for it. */
/* Commit for park_on, run after the joiner's ctx is saved: join target's
 * wait list, or wake right away if target finished in the meantime. */
static void join_commit(rw_fiber_handle *self, void *arg) {
    rw_fiber_handle *target = (rw_fiber_handle *)arg;
    rw_wait_lock_acquire(&target->wait_lock);
    /* Re-check under the lock to close the race with a trampoline that
     * is just about to take the wait list. */
    if (atomic_load_explicit(&target->state, memory_order_acquire)
        == RW_FIBER_DONE) {
        rw_wait_lock_release(&target->wait_lock);
        enqueue_ready(self);
        return;
    }
    self->next = target->wait_head;
    target->wait_head = self;
    rw_wait_lock_release(&target->wait_lock);
}

/* Park the calling fiber until target finishes. The caller re-checks
 * target's state after we return. */
static void park_on(rw_fiber_handle *target) {
    rw_sched_park_commit(join_commit, target);
}

/* Common fiber-completion path: publish result + DONE, broadcast main
 * waiters, release fiber waiters, swap back to the worker's scheduler
 * ctx. Called from every fiber_entry_<T> trampoline. The result must
 * already be written into h->result before calling this. */
static void finalize_fiber(rw_fiber_handle *h) {
    h->finish_seq = atomic_fetch_add_explicit(&g_finish_seq, 1, memory_order_relaxed);
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
    m->finished = h;
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
    atomic_store_explicit(&h->reap, REAP_NONE, memory_order_relaxed);
    atomic_fetch_add_explicit(&g_live_fibers, 1, memory_order_relaxed);

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
    atomic_fetch_sub_explicit(&g_live_fibers, 1, memory_order_relaxed);
}

/* The owner is done with h (join read the result, or detach). Free it now
 * if the worker already finished with it; otherwise the worker frees it. */
static void release_handle(rw_fiber_handle *h) {
    if (atomic_exchange_explicit(&h->reap, REAP_RELEASED, memory_order_acq_rel)
        == REAP_FINISHED) {
        free_handle(h);
    }
}

void rw_sched_detach(rw_fiber_handle *h) {
    if (h) release_handle(h);
}

int8_t rw_sched_is_done(rw_fiber_handle *h) {
    return atomic_load_explicit(&h->state, memory_order_acquire) == RW_FIBER_DONE;
}

uint64_t rw_sched_finish_seq(rw_fiber_handle *h) {
    return h->finish_seq;  /* valid once rw_sched_is_done(h) */
}

int64_t rw_sched_live_fibers(void) {
    return atomic_load_explicit(&g_live_fibers, memory_order_relaxed);
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
        release_handle(h);                                                 \
        return r;                                                          \
    }

DEFINE_JOIN(i64,  int64_t, i64)
DEFINE_JOIN(f64,  double,  f64)
DEFINE_JOIN(bool, int8_t,  b)
DEFINE_JOIN(str,  rw_str,  s)

void rw_sched_join_void(rw_fiber_handle *h) {
    wait_done(h);
    release_handle(h);
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

    g_preempt_ns = parse_preempt_ns();
    atomic_store_explicit(&rw_preempt_flag, 0, memory_order_relaxed);
    if (g_preempt_ns > 0) {
        if (pthread_create(&g_monitor, NULL, monitor_main, NULL) != 0) {
            rw_die("pthread_create monitor");
        }
        g_monitor_started = 1;
    }
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
    if (g_monitor_started) {
        pthread_join(g_monitor, NULL);
        g_monitor_started = 0;
    }

    /* Reclaim any unjoined fibers still on any queue. */
    rw_fiber_handle *h;
    for (int i = 0; i < g_nworkers; i++) {
        while ((h = rw_runq_get(&g_ps[i])) != NULL) free_handle(h);
    }
    while ((h = rw_globq_pop(&g_globq)) != NULL) free_handle(h);
    rw_globq_destroy(&g_globq);
}
