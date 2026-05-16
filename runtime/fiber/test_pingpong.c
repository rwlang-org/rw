/*
 * Ping-pong smoke test for the fiber primitives.
 *
 * We create two fibers (A and B) plus a "main" fiber that owns the
 * initial thread context. The flow:
 *
 *   main  -> A : A prints "ping 1", swaps back to main
 *   main  -> A : A continues, prints "ping 2", swaps back
 *   main  -> B : B prints "pong 1", swaps back
 *
 * Build:
 *   cc -O2 -Wall -Wextra -fPIC -std=c11 -c fiber.c -o fiber.o
 *   cc -O2 -Wall -Wextra -fPIC          -c fiber_arm64.S -o fiber_arm64.o
 *   cc -O2 -Wall -Wextra -fPIC -std=c11 test_pingpong.c fiber.o fiber_arm64.o -o test_pingpong
 *   ./test_pingpong
 */

#include "fiber.h"

#include <stdio.h>
#include <stdlib.h>

static rw_fiber_ctx ctx_main;
static rw_fiber_ctx ctx_a;
static rw_fiber_ctx ctx_b;

static void fiber_a(void *arg) {
    (void)arg;
    printf("A: ping 1\n");
    rw_fiber_swap(&ctx_a, &ctx_main);
    printf("A: ping 2\n");
    rw_fiber_swap(&ctx_a, &ctx_main);
    /* If we get back here it's an error: main should not re-enter A. */
    printf("A: should never reach here\n");
    abort();
}

static void fiber_b(void *arg) {
    (void)arg;
    printf("B: pong 1\n");
    rw_fiber_swap(&ctx_b, &ctx_main);
    printf("B: should never reach here\n");
    abort();
}

int main(void) {
    const size_t SZ = 64 * 1024;
    void *stack_a = malloc(SZ);
    void *stack_b = malloc(SZ);
    if (!stack_a || !stack_b) { perror("malloc"); return 1; }

    rw_fiber_ctx_init(&ctx_a, stack_a, SZ, fiber_a, NULL);
    rw_fiber_ctx_init(&ctx_b, stack_b, SZ, fiber_b, NULL);

    printf("main: before A\n");
    rw_fiber_swap(&ctx_main, &ctx_a);
    printf("main: between A1 and A2\n");
    rw_fiber_swap(&ctx_main, &ctx_a);
    printf("main: between A2 and B\n");
    rw_fiber_swap(&ctx_main, &ctx_b);
    printf("main: done\n");

    free(stack_a);
    free(stack_b);
    return 0;
}
