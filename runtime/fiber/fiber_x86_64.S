// System V AMD64 context switch for rw fibers.
//
// void rw_fiber_swap(rw_fiber_ctx *old, rw_fiber_ctx *new)
//   %rdi = old, %rsi = new
//
// Layout (matches RW_FIBER_CTX_WORDS in fiber.h):
//   [ 0 ] = rbx
//   [ 1 ] = rbp
//   [ 2 ] = r12  (used to carry entry pointer for fresh fibers)
//   [ 3 ] = r13  (used to carry arg for fresh fibers)
//   [ 4 ] = r14
//   [ 5 ] = r15
//   [ 6 ] = rsp
//   [ 7..20 ] = unused (kept identical to the arm64 layout)
//
// Each word is 8 bytes, so byte offsets are word_index * 8.
//
// Unlike arm64, x86_64 has no link register: the return address lives on
// the stack. For freshly-created fibers, the C init code writes the
// trampoline address to the top of the prepared stack, so the first
// `ret` issued by swap pops it and jumps to the trampoline.
//
// Linux conventionally uses bare symbol names (no underscore prefix).

#if defined(__APPLE__)
#  define RW_SYM(x) _##x
#else
#  define RW_SYM(x) x
#endif

    .text
    .globl RW_SYM(rw_fiber_swap)
    .p2align 4

RW_SYM(rw_fiber_swap):
    // Save callee-saved general regs into *old (%rdi).
    movq %rbx,  0(%rdi)
    movq %rbp,  8(%rdi)
    movq %r12, 16(%rdi)
    movq %r13, 24(%rdi)
    movq %r14, 32(%rdi)
    movq %r15, 40(%rdi)

    // Save rsp.
    movq %rsp, 48(%rdi)

    // Restore callee-saved general regs from *new (%rsi).
    movq  0(%rsi), %rbx
    movq  8(%rsi), %rbp
    movq 16(%rsi), %r12
    movq 24(%rsi), %r13
    movq 32(%rsi), %r14
    movq 40(%rsi), %r15

    // Restore rsp.
    movq 48(%rsi), %rsp

    // Return: pops the saved return address from the new stack and
    // resumes execution there.
    ret

// Trampoline used for freshly-created fibers.
//   %r12 = entry function pointer
//   %r13 = argument
// Move the argument into %rdi and jump to entry.
// If entry returns we trap.
    .globl RW_SYM(rw_fiber_trampoline)
    .p2align 4
RW_SYM(rw_fiber_trampoline):
    movq %r13, %rdi
    callq *%r12
    // entry returned: this is a programming error; trap.
    ud2

#if defined(__linux__)
// Tell the linker the stack does not need to be executable.
.section .note.GNU-stack,"",%progbits
#endif
