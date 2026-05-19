/*
 * Unit test for the bounded run queue (runq.c).
 *
 * We construct fake `rw_fiber_handle` stand-ins (just an int id + next
 * pointer at the right offset) so we can exercise the queue without
 * dragging in the full scheduler. This is purely sequential — multi-
 * threaded contention is exercised in test_steal.c later.
 */

#include "runq.h"

#include <assert.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

/* Fake handle: the runq only chains via the helper functions below, so
 * we control the layout entirely. */
typedef struct fake_handle {
    int               id;
    struct fake_handle *next;
} fake_handle;

/* These two symbols are referenced by runq.c via extern declarations.
 * In the real build, sched.c provides them against the real handle.
 * Here, we redefine them for the fake handle and link against runq.o
 * standalone. */
struct rw_fiber_handle;  /* opaque to the linker */

struct rw_fiber_handle *rw_fiber_handle_get_next(struct rw_fiber_handle *h) {
    fake_handle *f = (fake_handle *)h;
    return (struct rw_fiber_handle *)(f ? f->next : NULL);
}

void rw_fiber_handle_set_next(struct rw_fiber_handle *h,
                              struct rw_fiber_handle *next) {
    fake_handle *f = (fake_handle *)h;
    if (f) f->next = (fake_handle *)next;
}

static fake_handle *mk(int id) {
    fake_handle *f = calloc(1, sizeof(*f));
    f->id = id;
    return f;
}

static int idof(struct rw_fiber_handle *h) {
    return ((fake_handle *)h)->id;
}

/* Test 1: push N (N < CAP), pop N — FIFO ordering. */
static void test_push_pop_fifo(void) {
    rw_P p;
    rw_runq_init(&p);
    rw_globq gq;
    rw_globq_init(&gq);

    const int N = 100;
    for (int i = 0; i < N; i++) {
        rw_runq_put(&p, (struct rw_fiber_handle *)mk(i), &gq);
    }
    for (int i = 0; i < N; i++) {
        struct rw_fiber_handle *h = rw_runq_get(&p);
        assert(h && idof(h) == i);
        free(h);
    }
    assert(rw_runq_get(&p) == NULL);
    /* globq should be empty (we never overflowed). */
    assert(gq.count == 0);
    rw_globq_destroy(&gq);
    printf("test_push_pop_fifo OK\n");
}

/* Test 2: overflow into globq. */
static void test_overflow(void) {
    rw_P p;
    rw_runq_init(&p);
    rw_globq gq;
    rw_globq_init(&gq);

    /* Fill to CAP. */
    for (uint32_t i = 0; i < RW_RUNQ_CAP; i++) {
        rw_runq_put(&p, (struct rw_fiber_handle *)mk((int)i), &gq);
    }
    /* The next put forces overflow: half + this one (= CAP/2 + 1) move
     * to globq. */
    rw_runq_put(&p, (struct rw_fiber_handle *)mk(1000), &gq);
    assert(gq.count == RW_RUNQ_CAP / 2 + 1);
    /* Local ring still has CAP/2 items. */
    uint32_t in_local = atomic_load_explicit(&p.tail, memory_order_relaxed)
                      - atomic_load_explicit(&p.head, memory_order_relaxed);
    assert(in_local == RW_RUNQ_CAP / 2);

    /* Drain local then globq; ids should be in original push order:
     * local pop: ids = [CAP/2 .. CAP-1] (the back half that stayed).
     * globq pop: ids = [0..CAP/2-1, 1000]. */
    for (uint32_t i = RW_RUNQ_CAP / 2; i < RW_RUNQ_CAP; i++) {
        struct rw_fiber_handle *h = rw_runq_get(&p);
        assert(h && (uint32_t)idof(h) == i);
        free(h);
    }
    for (uint32_t i = 0; i < RW_RUNQ_CAP / 2; i++) {
        struct rw_fiber_handle *h = rw_globq_pop(&gq);
        assert(h && (uint32_t)idof(h) == i);
        free(h);
    }
    struct rw_fiber_handle *h = rw_globq_pop(&gq);
    assert(h && idof(h) == 1000);
    free(h);
    assert(rw_globq_pop(&gq) == NULL);
    rw_globq_destroy(&gq);
    printf("test_overflow OK\n");
}

/* Test 3: rw_runq_grab takes ceil(n/2). */
static void test_grab_half(void) {
    rw_P p;
    rw_runq_init(&p);
    rw_globq gq;
    rw_globq_init(&gq);

    const int N = 10;  /* expect grab to take 5 */
    for (int i = 0; i < N; i++) {
        rw_runq_put(&p, (struct rw_fiber_handle *)mk(i), &gq);
    }
    struct rw_fiber_handle *out[N];
    uint32_t got = rw_runq_grab(&p, out, N);
    assert(got == 5);
    /* Stolen items are the *front half*: ids 0..4 */
    for (uint32_t i = 0; i < got; i++) {
        assert((uint32_t)idof(out[i]) == i);
        free(out[i]);
    }
    /* Remaining local: ids 5..9 */
    for (int i = 5; i < N; i++) {
        struct rw_fiber_handle *h = rw_runq_get(&p);
        assert(h && idof(h) == i);
        free(h);
    }
    rw_globq_destroy(&gq);
    printf("test_grab_half OK\n");
}

/* Test 4: globq batch pop. */
static void test_globq_batch(void) {
    rw_globq gq;
    rw_globq_init(&gq);

    const int N = 12;
    for (int i = 0; i < N; i++) {
        rw_globq_push(&gq, (struct rw_fiber_handle *)mk(i));
    }
    struct rw_fiber_handle *out[5];
    uint32_t n = rw_globq_pop_batch(&gq, out, 5);
    assert(n == 5);
    for (uint32_t i = 0; i < n; i++) {
        assert((uint32_t)idof(out[i]) == i);
        free(out[i]);
    }
    assert(gq.count == 7);
    for (int i = 5; i < N; i++) {
        struct rw_fiber_handle *h = rw_globq_pop(&gq);
        assert(h && idof(h) == i);
        free(h);
    }
    rw_globq_destroy(&gq);
    printf("test_globq_batch OK\n");
}

int main(void) {
    test_push_pop_fifo();
    test_overflow();
    test_grab_half();
    test_globq_batch();
    printf("all runq tests passed\n");
    return 0;
}
