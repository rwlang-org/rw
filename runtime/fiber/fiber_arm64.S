// arm64 context switch for rw fibers.
//
// void rw_fiber_swap(rw_fiber_ctx *old, rw_fiber_ctx *new)
//   x0 = old, x1 = new
//
// Layout (matches RW_FIBER_CTX_WORDS in fiber.h):
//   [ 0..9 ]    = x19..x28
//   [ 10  ]     = x29 (fp)
//   [ 11  ]     = x30 (lr)
//   [ 12  ]     = sp
//   [ 13..20 ]  = d8..d15
//
// Each word is 8 bytes, so byte offsets are word_index * 8.
// On macOS, exported C symbols are prefixed with an underscore.

#if defined(__APPLE__)
#  define RW_SYM(x) _##x
#else
#  define RW_SYM(x) x
#endif

    .text
    .globl RW_SYM(rw_fiber_swap)
    .p2align 2

RW_SYM(rw_fiber_swap):
    // Save callee-saved general regs into *old (x0).
    stp x19, x20, [x0, #0]
    stp x21, x22, [x0, #16]
    stp x23, x24, [x0, #32]
    stp x25, x26, [x0, #48]
    stp x27, x28, [x0, #64]
    stp x29, x30, [x0, #80]      // fp, lr -> word 10, 11

    // Save sp into word 12 (offset 96).
    mov x9, sp
    str x9, [x0, #96]

    // Save d8..d15 into words 13..20 (offsets 104..160).
    stp d8,  d9,  [x0, #104]
    stp d10, d11, [x0, #120]
    stp d12, d13, [x0, #136]
    stp d14, d15, [x0, #152]

    // Restore callee-saved general regs from *new (x1).
    ldp x19, x20, [x1, #0]
    ldp x21, x22, [x1, #16]
    ldp x23, x24, [x1, #32]
    ldp x25, x26, [x1, #48]
    ldp x27, x28, [x1, #64]
    ldp x29, x30, [x1, #80]      // fp, lr

    ldr x9, [x1, #96]
    mov sp, x9

    ldp d8,  d9,  [x1, #104]
    ldp d10, d11, [x1, #120]
    ldp d12, d13, [x1, #136]
    ldp d14, d15, [x1, #152]

    // Return -> jumps to the LR we just restored.
    ret

// Trampoline used for freshly-created fibers.
//   x19 = entry function pointer
//   x20 = argument
// Move the argument into x0 and branch to entry.
// If entry returns we trap.
    .globl RW_SYM(rw_fiber_trampoline)
    .p2align 2
RW_SYM(rw_fiber_trampoline):
    mov x0, x20
    blr x19
    brk #0xdead
