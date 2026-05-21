/*
 * Unit test for rw_str_len / rw_str_eq / rw_str_concat.
 *
 * These helpers are pure functions of their inputs; we don't need
 * the scheduler at all here.
 */

#include "../runtime.h"

#include <assert.h>
#include <stdio.h>
#include <string.h>

static rw_str lit(const char *s) {
    rw_str r;
    r.len = (int64_t)strlen(s);
    r.ptr = s;
    return r;
}

static rw_str empty(void) {
    rw_str r = { .len = 0, .ptr = NULL };
    return r;
}

int main(void) {
    /* len */
    assert(rw_str_len(lit("hello")) == 5);
    assert(rw_str_len(empty()) == 0);
    assert(rw_str_len(lit("")) == 0);

    /* eq */
    assert(rw_str_eq(lit("hello"), lit("hello")) == 1);
    assert(rw_str_eq(lit("hello"), lit("world")) == 0);
    assert(rw_str_eq(lit("hello"), lit("hell")) == 0);
    assert(rw_str_eq(empty(), empty()) == 1);
    assert(rw_str_eq(empty(), lit("")) == 1);
    assert(rw_str_eq(lit("hello"), empty()) == 0);

    /* concat */
    {
        rw_str c = rw_str_concat(lit("foo"), lit("bar"));
        assert(c.len == 6);
        assert(memcmp(c.ptr, "foobar", 6) == 0);
        /* leak: not freed (by design) */
    }
    {
        rw_str c = rw_str_concat(empty(), lit("x"));
        assert(c.len == 1);
        assert(c.ptr[0] == 'x');
    }
    {
        rw_str c = rw_str_concat(lit("x"), empty());
        assert(c.len == 1);
        assert(c.ptr[0] == 'x');
    }
    {
        rw_str c = rw_str_concat(empty(), empty());
        assert(c.len == 0);
        assert(c.ptr == NULL);
    }
    printf("all str_ops tests passed\n");
    return 0;
}
