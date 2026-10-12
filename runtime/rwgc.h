#ifndef RW_GC_H
#define RW_GC_H

/* Garbage collection (spec 67): the Boehm-Demers-Weiser conservative
 * collector, wrapped so only gc.c includes <gc/gc.h>. */

#include <stddef.h>

/* Initialize once, on the main thread (rw_sched_init calls it). */
void  rw_gc_init(void);

/* Memory owned by rw values. `rw_gc_alloc` is scanned for pointers;
 * `rw_gc_alloc_atomic` (string bytes, int arrays) is not. Both are
 * zero-filled except atomic memory, which is uninitialized. */
void *rw_gc_alloc(size_t n);
void *rw_gc_alloc_atomic(size_t n);
void *rw_gc_realloc(void *p, size_t n);
/* Scanned and never collected: runtime records that hold rw values
 * (fiber handles). Free explicitly. */
void *rw_gc_alloc_uncollectable(size_t n);
void  rw_gc_free(void *p);

/* Threads that run fibers or allocate must be registered. Returns the
 * GC's handle for the calling thread and its stack base (highest address). */
void *rw_gc_register_thread(void **stack_base);
void  rw_gc_unregister_thread(void);

/* Bracket every fiber context switch (spec 67): `begin` runs on the old
 * stack right before rw_fiber_swap and records the stack the thread is
 * about to run on (its highest address); `end` runs on the new stack right
 * after. A collection never stops the world in between. */
void  rw_gc_switch_begin(void *thread_handle, void *new_stack_base);
void  rw_gc_switch_end(void);

/* The same exclusion without touching stacks: hold it while changing data
 * the fiber-roots hook walks (the list of live fibers). */
void  rw_gc_enter(void);
void  rw_gc_leave(void);

/* Called during marking, with the world stopped, to push the stacks of
 * fibers that are not running (rw_gc_push_range for each). */
void  rw_gc_set_fiber_roots(void (*push)(void));

/* Builtins gc_collect() / gc_heap_size(). */
void    rw_gc_collect(void);
#include <stdint.h>
int64_t rw_gc_heap_size(void);
void  rw_gc_push_range(void *lo, void *hi);

#endif
