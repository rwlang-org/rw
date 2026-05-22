/*
 * Unit test for rw_list_int_at_opt and the rw_option_int struct.
 */

#include "../runtime.h"

#include <assert.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>

int main(void) {
    /* Build a small list: [10, 20, 30] */
    rw_list_int l, l1, l2;
    rw_list_int_new(&l);
    rw_list_int_push(&l1, &l, 10);
    rw_list_int_push(&l2, &l1, 20);
    rw_list_int l3;
    rw_list_int_push(&l3, &l2, 30);

    /* In-range: Some */
    rw_option_int o;
    rw_list_int_at_opt(&o, &l3, 0);
    assert(o.tag == 1);
    assert(o.payload == 10);

    rw_list_int_at_opt(&o, &l3, 2);
    assert(o.tag == 1);
    assert(o.payload == 30);

    /* Out-of-range: None */
    rw_list_int_at_opt(&o, &l3, 3);
    assert(o.tag == 0);

    rw_list_int_at_opt(&o, &l3, -1);
    assert(o.tag == 0);

    /* Empty list: None for any index */
    rw_list_int empty;
    rw_list_int_new(&empty);
    rw_list_int_at_opt(&o, &empty, 0);
    assert(o.tag == 0);

    printf("all option tests passed\n");
    return 0;
}
