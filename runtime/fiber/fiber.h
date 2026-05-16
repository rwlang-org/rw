#ifndef RW_FIBER_H
#define RW_FIBER_H
/*
 * Minimal fiber primitives for rw's green-thread runtime.
 *
 * A `rw_fiber_ctx` is a flat array of machine words that holds the
 * callee-saved registers of a paused fiber: x19..x28, x29 (FP), x30 (LR),
 * sp, and d8..d15 (the lower half of the FP callee-saved regs).
 *
 * Layout (arm64):
 *   offset (in 8-byte words)
 *     0..9   = x19..x28
 *     10     = x29 (fp)
 *     11     = x30 (lr)  -> resume target; set this to the entry function
 *                          for a freshly created fiber.
 *     12     = sp
 *     13..20 = d8..d15
 *
 * `rw_fiber_swap(old, new)` atomically saves the current callee-saved
 * state into *old and restores from *new. After the call returns in the
 * old fiber, execution continues right after the call site.
 */

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define RW_FIBER_CTX_WORDS 21

typedef struct {
    uint64_t regs[RW_FIBER_CTX_WORDS];
} rw_fiber_ctx;

/*
 * Initialize `ctx` so that the first swap into it begins executing
 * `entry(arg)` on its own stack region [stack_lo, stack_hi).
 * The stack must be sized so that 16-byte alignment can be honored.
 */
void rw_fiber_ctx_init(rw_fiber_ctx *ctx,
                       void *stack_lo,
                       size_t stack_size,
                       void (*entry)(void *),
                       void *arg);

/*
 * Save current callee-saved state to *old; restore from *new.
 * After this returns, the *old* fiber is resumed by another call to
 * rw_fiber_swap(other, old).
 */
void rw_fiber_swap(rw_fiber_ctx *old, rw_fiber_ctx *new_);

#ifdef __cplusplus
}
#endif

#endif /* RW_FIBER_H */
