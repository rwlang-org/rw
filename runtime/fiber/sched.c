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

#include <errno.h>
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

typedef enum {
    RW_FIBER_READY = 0,
    RW_FIBER_RUNNING,
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
    rw_fiber_state   state;
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
    /* Singly-linked list pointers for the ready queue. */
    struct rw_fiber_handle *next;
};

static rw_fiber_ctx g_sched_ctx;
/* The currently running fiber, NULL while the scheduler is running. */
static rw_fiber_handle *g_current = NULL;
/* Doubly-linked queue of ready fibers. */
static rw_fiber_handle *g_ready_head = NULL;
static rw_fiber_handle *g_ready_tail = NULL;

static void rw_die(const char *msg) {
    perror(msg);
    abort();
}

static void enqueue_ready(rw_fiber_handle *h) {
    h->state = RW_FIBER_READY;
    h->next = NULL;
    if (g_ready_tail) {
        g_ready_tail->next = h;
        g_ready_tail = h;
    } else {
        g_ready_head = g_ready_tail = h;
    }
}

static rw_fiber_handle *dequeue_ready(void) {
    rw_fiber_handle *h = g_ready_head;
    if (!h) return NULL;
    g_ready_head = h->next;
    if (!g_ready_head) g_ready_tail = NULL;
    h->next = NULL;
    return h;
}

/* The scheduler loop has a peculiar shape: it isn't really a loop running
 * on a dedicated stack. Instead, every time someone yields, we land back
 * here (in the calling thread's main stack), pick a fiber, and swap into
 * it. When that fiber yields or finishes it comes back here again.
 *
 * That means: rw_sched_yield() is a function call that "returns" once the
 * scheduler decides to resume this fiber. The actual scheduling decision
 * is small and inline. */
void rw_sched_yield(void) {
    rw_fiber_handle *me = g_current;
    if (me) {
        /* Calling fiber yields: put it back on the queue (if still RUNNING) */
        if (me->state == RW_FIBER_RUNNING) {
            enqueue_ready(me);
        }
    }
    /* Pick next. If none, swap back to the original (saved) main ctx. */
    rw_fiber_handle *next = dequeue_ready();
    if (!next) {
        /* No fibers to run; if we have a current, it must be DONE - we
         * still need to return to the main context. */
        rw_fiber_handle *prev = g_current;
        g_current = NULL;
        if (prev) {
            rw_fiber_swap(&prev->ctx, &g_sched_ctx);
        } else {
            /* nothing to do; called with empty queue and no current */
            return;
        }
        return;
    }
    next->state = RW_FIBER_RUNNING;
    g_current = next;
    /* Save into the *caller*'s ctx (either a fiber or the main thread). */
    rw_fiber_ctx *from = me ? &me->ctx : &g_sched_ctx;
    rw_fiber_swap(from, &next->ctx);
}

/* The fiber's entry point. Calls user fn, stores result, marks DONE,
 * and yields permanently. We must NEVER let the fiber return into the
 * trampoline (the trampoline would brk #0xdead). */
#define DEFINE_TRAMP(NAME, RETTY, FIELD)                                    \
    static void fiber_entry_##NAME(void *arg) {                             \
        rw_fiber_handle *h = (rw_fiber_handle *)arg;                        \
        RETTY (*fn)(void *) = (RETTY (*)(void *))h->user_fn;                \
        h->result.FIELD = fn(h->user_arg);                                  \
        h->state = RW_FIBER_DONE;                                           \
        /* Final yield: never returns. */                                   \
        g_current = h;                                                      \
        rw_fiber_handle *next = dequeue_ready();                            \
        rw_fiber_ctx *target = next ? &next->ctx : &g_sched_ctx;            \
        if (next) { next->state = RW_FIBER_RUNNING; g_current = next; }     \
        else      { g_current = NULL; }                                     \
        rw_fiber_swap(&h->ctx, target);                                     \
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
    h->state = RW_FIBER_DONE;
    g_current = h;
    rw_fiber_handle *next = dequeue_ready();
    rw_fiber_ctx *target = next ? &next->ctx : &g_sched_ctx;
    if (next) { next->state = RW_FIBER_RUNNING; g_current = next; }
    else      { g_current = NULL; }
    rw_fiber_swap(&h->ctx, target);
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
    if (h->region) {
        munmap(h->region, rw_region_size());
    }
    free(h);
}

#define DEFINE_JOIN(NAME, RETTY, FIELD)                                    \
    RETTY rw_sched_join_##NAME(rw_fiber_handle *h) {                       \
        while (h->state != RW_FIBER_DONE) {                                \
            rw_sched_yield();                                              \
        }                                                                  \
        RETTY r = h->result.FIELD;                                         \
        free_handle(h);                                                    \
        return r;                                                          \
    }

DEFINE_JOIN(i64,  int64_t, i64)
DEFINE_JOIN(f64,  double,  f64)
DEFINE_JOIN(bool, int8_t,  b)
DEFINE_JOIN(str,  rw_str,  s)

void rw_sched_join_void(rw_fiber_handle *h) {
    while (h->state != RW_FIBER_DONE) {
        rw_sched_yield();
    }
    free_handle(h);
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
    g_ready_head = NULL;
    g_ready_tail = NULL;
    g_current = NULL;
    memset(&g_sched_ctx, 0, sizeof(g_sched_ctx));
}

void rw_sched_shutdown(void) {
    /* In a well-formed program every spawned fiber has been joined.
     * Anything left here is a leak; reclaim it. */
    rw_fiber_handle *h;
    while ((h = dequeue_ready()) != NULL) {
        free_handle(h);
    }
}
