/*
 * Unit test for rw_list_int_{new,push,at,len}.
 *
 * Pure functions of their inputs (push allocates but doesn't touch
 * globals); no scheduler required. Pointer-out helpers (see
 * runtime.h for ABI rationale).
 */

#include "../runtime.h"

#include <assert.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>

int main(void) {
    /* new: empty list */
    rw_list_int l;
    rw_list_int_new(&l);
    assert(l.len == 0);
    assert(l.cap == 0);
    assert(l.data == NULL);
    assert(rw_list_int_len(&l) == 0);

    /* push: grow from cap=0 to cap=4 */
    rw_list_int l2;
    rw_list_int_push(&l2, &l, 10);
    assert(l2.len == 1);
    assert(l2.cap == 4);
    assert(l2.data != NULL);
    assert(rw_list_int_at(&l2, 0) == 10);
    assert(rw_list_int_len(&l2) == 1);

    /* push three more so cap stays at 4 */
    rw_list_int l3, l4, l5;
    rw_list_int_push(&l3, &l2, 20);
    rw_list_int_push(&l4, &l3, 30);
    rw_list_int_push(&l5, &l4, 40);
    assert(l5.len == 4);
    assert(l5.cap == 4);
    assert(rw_list_int_at(&l5, 0) == 10);
    assert(rw_list_int_at(&l5, 1) == 20);
    assert(rw_list_int_at(&l5, 2) == 30);
    assert(rw_list_int_at(&l5, 3) == 40);

    /* 5th push: cap should double to 8 */
    rw_list_int l6;
    rw_list_int_push(&l6, &l5, 50);
    assert(l6.len == 5);
    assert(l6.cap == 8);
    assert(rw_list_int_at(&l6, 4) == 50);
    /* and earlier elements survived the copy */
    assert(rw_list_int_at(&l6, 0) == 10);

    /* Immutability: a prior snapshot should still be readable. */
    rw_list_int a, a1, b;
    rw_list_int_new(&a);
    rw_list_int_push(&a1, &a, 1);
    rw_list_int_push(&b, &a1, 2);
    assert(a1.len == 1);
    assert(rw_list_int_at(&a1, 0) == 1);
    assert(b.len == 2);
    assert(rw_list_int_at(&b, 0) == 1);
    assert(rw_list_int_at(&b, 1) == 2);

    /* Stress: push 100 elements, read them all back */
    rw_list_int s;
    rw_list_int_new(&s);
    for (int i = 0; i < 100; i++) {
        rw_list_int next;
        rw_list_int_push(&next, &s, (int64_t)i * 7);
        s = next;
    }
    assert(s.len == 100);
    for (int i = 0; i < 100; i++) {
        assert(rw_list_int_at(&s, i) == (int64_t)i * 7);
    }

    printf("all list_int tests passed\n");
    return 0;
}
