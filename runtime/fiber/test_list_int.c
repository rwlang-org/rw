/*
 * Unit test for rw_list_int_{new,push,at,len}.
 *
 * Pure functions of their inputs (push allocates but doesn't touch
 * globals); no scheduler required.
 */

#include "../runtime.h"

#include <assert.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>

int main(void) {
    /* new: empty list */
    rw_list_int l = rw_list_int_new();
    assert(l.len == 0);
    assert(l.cap == 0);
    assert(l.data == NULL);
    assert(rw_list_int_len(l) == 0);

    /* push: grow from cap=0 to cap=4 */
    l = rw_list_int_push(l, 10);
    assert(l.len == 1);
    assert(l.cap == 4);
    assert(l.data != NULL);
    assert(rw_list_int_at(l, 0) == 10);
    assert(rw_list_int_len(l) == 1);

    /* push three more so cap stays at 4 */
    l = rw_list_int_push(l, 20);
    l = rw_list_int_push(l, 30);
    l = rw_list_int_push(l, 40);
    assert(l.len == 4);
    assert(l.cap == 4);
    assert(rw_list_int_at(l, 0) == 10);
    assert(rw_list_int_at(l, 1) == 20);
    assert(rw_list_int_at(l, 2) == 30);
    assert(rw_list_int_at(l, 3) == 40);

    /* 5th push: cap should double to 8 */
    l = rw_list_int_push(l, 50);
    assert(l.len == 5);
    assert(l.cap == 8);
    assert(rw_list_int_at(l, 4) == 50);
    /* and earlier elements survived the copy */
    assert(rw_list_int_at(l, 0) == 10);

    /* Immutability: a prior snapshot should still be readable. */
    rw_list_int a = rw_list_int_new();
    a = rw_list_int_push(a, 1);
    rw_list_int b = rw_list_int_push(a, 2);
    assert(a.len == 1);
    assert(rw_list_int_at(a, 0) == 1);
    assert(b.len == 2);
    assert(rw_list_int_at(b, 0) == 1);
    assert(rw_list_int_at(b, 1) == 2);

    /* Stress: push 100 elements, read them all back */
    rw_list_int s = rw_list_int_new();
    for (int i = 0; i < 100; i++) {
        s = rw_list_int_push(s, (int64_t)i * 7);
    }
    assert(s.len == 100);
    for (int i = 0; i < 100; i++) {
        assert(rw_list_int_at(s, i) == (int64_t)i * 7);
    }

    printf("all list_int tests passed\n");
    return 0;
}
