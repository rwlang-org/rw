/*
 * rw fiber context-init logic.
 *
 * The asm routine `rw_fiber_swap` lives in either fiber_arm64.S or
 * fiber_x86_64.S; this file only initializes the saved register file
 * for a freshly-created fiber and links against the asm-side trampoline
 * symbol.
 *
 * The trampoline trick is the same on both archs: the caller-saved
 * argument register (x0 / rdi) can't be saved by swap, so we stuff the
 * user `entry` and `arg` into callee-saved registers and have a small
 * asm shim move them into place at first entry.
 */

#include "fiber.h"

#include <stdint.h>
#include <stdlib.h>
#include <string.h>

/* Defined in fiber_arm64.S or fiber_x86_64.S. */
extern void rw_fiber_trampoline(void);

#if defined(__aarch64__)

/* arm64 register slots (matches fiber_arm64.S):
 *   [0..9]  = x19..x28
 *   [10]    = x29 (fp)
 *   [11]    = x30 (lr)   -> first resume target
 *   [12]    = sp
 *   [13..20]= d8..d15
 *
 * We park `entry` in x19 and `arg` in x20 so the trampoline can recover
 * them and tail-call entry(arg).
 */
void rw_fiber_ctx_init(rw_fiber_ctx *ctx,
                       void *stack_lo,
                       size_t stack_size,
                       void (*entry)(void *),
                       void *arg) {
    memset(ctx, 0, sizeof(*ctx));

    uintptr_t top = (uintptr_t)stack_lo + stack_size;
    top &= ~(uintptr_t)0xF;  /* AArch64 SP must stay 16-byte aligned. */

    ctx->regs[0]  = (uint64_t)(uintptr_t)entry;                    /* x19 */
    ctx->regs[1]  = (uint64_t)(uintptr_t)arg;                      /* x20 */
    ctx->regs[11] = (uint64_t)(uintptr_t)&rw_fiber_trampoline;     /* x30 (LR) */
    ctx->regs[12] = (uint64_t)top;                                  /* sp */
}

#elif defined(__x86_64__)

/* x86_64 register slots (matches fiber_x86_64.S):
 *   [0] = rbx
 *   [1] = rbp
 *   [2] = r12  -> entry pointer
 *   [3] = r13  -> arg
 *   [4] = r14
 *   [5] = r15
 *   [6] = rsp
 *
 * Unlike arm64, the return address lives on the stack on x86_64, so we
 * write `&rw_fiber_trampoline` to the very top of the prepared stack
 * and point rsp there. The first `ret` in swap pops it and jumps.
 *
 * SysV AMD64 also requires that at function entry rsp is aligned to
 * 16 + 8 (i.e., (rsp % 16) == 8 just after a CALL). We achieve the
 * same effect by aligning the slot we *write the address to* on a
 * 16-byte boundary.
 */
void rw_fiber_ctx_init(rw_fiber_ctx *ctx,
                       void *stack_lo,
                       size_t stack_size,
                       void (*entry)(void *),
                       void *arg) {
    memset(ctx, 0, sizeof(*ctx));

    uintptr_t top = (uintptr_t)stack_lo + stack_size;
    top &= ~(uintptr_t)0xF;   /* 16-byte align top */
    top -= 8;                 /* make room for the return address */

    *(uint64_t *)top = (uint64_t)(uintptr_t)&rw_fiber_trampoline;

    ctx->regs[2] = (uint64_t)(uintptr_t)entry;   /* r12 */
    ctx->regs[3] = (uint64_t)(uintptr_t)arg;     /* r13 */
    ctx->regs[6] = (uint64_t)top;                /* rsp */
}

#else
#  error "rw fiber runtime: unsupported architecture"
#endif
