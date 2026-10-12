/*
 * Boehm GC integration (spec 67).
 *
 * Fibers run on their own mmap'd stacks, which the collector knows nothing
 * about. Two things make it see them:
 *
 * - The thread running a fiber tells the GC its stack is the fiber's stack
 *   (GC_set_stackbottom) at every switch, so the collector scans the right
 *   range of each running fiber.
 * - Fibers that are not running are pushed from a push-other-roots hook
 *   (see sched.c), [saved sp, stack top] plus their saved registers.
 *
 * A switch changes the stack pointer and the recorded stack bottom in two
 * steps. If the world stopped in between, the collector would scan a bogus
 * range, so switches take a shared "switching" count and a collection
 * waits for it to drain before stopping the world (and blocks new
 * switches until the world restarts).
 */

#define GC_THREADS 1
#define GC_NO_THREAD_REDIRECTS 1
#include <gc/gc.h>
#include <gc/gc_mark.h>

#include "rwgc.h"

#include <pthread.h>
#include <sched.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

static pthread_once_t g_once = PTHREAD_ONCE_INIT;
static _Atomic int g_switching = 0;    /* threads between begin and end */
static _Atomic int g_stopping = 0;     /* a collection is stopping the world */
static void (*g_fiber_roots)(void) = NULL;
static GC_push_other_roots_proc g_prev_push = NULL;

static void on_event(GC_EventType e) {
    if (e == GC_EVENT_PRE_STOP_WORLD) {
        atomic_store(&g_stopping, 1);
        while (atomic_load(&g_switching) != 0) sched_yield();
    } else if (e == GC_EVENT_POST_START_WORLD) {
        atomic_store(&g_stopping, 0);
    }
}

static void push_roots(void) {
    if (g_prev_push) g_prev_push();   /* thread stacks */
    if (g_fiber_roots) g_fiber_roots();
}

static void init_once(void) {
    GC_INIT();
    GC_allow_register_threads();
    GC_set_on_collection_event(on_event);
    g_prev_push = GC_get_push_other_roots();
    GC_set_push_other_roots(push_roots);
}

void rw_gc_init(void) { pthread_once(&g_once, init_once); }

static void *check(void *p, size_t n) {
    if (!p && n > 0) {
        fputs("rw: out of memory\n", stderr);
        abort();
    }
    return p;
}

void *rw_gc_alloc(size_t n)               { return check(GC_MALLOC(n), n); }
void *rw_gc_alloc_atomic(size_t n)        { return check(GC_MALLOC_ATOMIC(n), n); }
void *rw_gc_realloc(void *p, size_t n)    { return check(GC_REALLOC(p, n), n); }
void *rw_gc_alloc_uncollectable(size_t n) { return check(GC_MALLOC_UNCOLLECTABLE(n), n); }
void  rw_gc_free(void *p)                 { GC_FREE(p); }

void *rw_gc_register_thread(void **stack_base) {
    struct GC_stack_base sb;
    GC_get_stack_base(&sb);
    GC_register_my_thread(&sb);
    void *handle = GC_get_my_stackbottom(&sb);
    if (stack_base) *stack_base = sb.mem_base;
    return handle;
}

void rw_gc_unregister_thread(void) { GC_unregister_my_thread(); }

void rw_gc_enter(void) {
    for (;;) {
        while (atomic_load(&g_stopping)) sched_yield();
        atomic_fetch_add(&g_switching, 1);
        if (!atomic_load(&g_stopping)) break;
        atomic_fetch_sub(&g_switching, 1);
    }
}

void rw_gc_leave(void) { atomic_fetch_sub(&g_switching, 1); }

void rw_gc_switch_begin(void *thread_handle, void *new_stack_base) {
    rw_gc_enter();
    /* No collection can be stopping the world now, so updating this
     * thread's record without the allocation lock is safe. */
    struct GC_stack_base sb = { 0 };
    sb.mem_base = new_stack_base;
    GC_set_stackbottom(thread_handle, &sb);
}

void rw_gc_switch_end(void) { rw_gc_leave(); }

void rw_gc_set_fiber_roots(void (*push)(void)) { g_fiber_roots = push; }

/* Builtins (spec 67). */
void    rw_gc_collect(void)   { GC_gcollect(); }
int64_t rw_gc_heap_size(void) { return (int64_t)GC_get_heap_size(); }

void rw_gc_push_range(void *lo, void *hi) {
    if (lo < hi) GC_push_all_eager(lo, hi);
}
