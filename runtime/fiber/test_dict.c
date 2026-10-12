/*
 * Dict runtime test (spec 31): random inserts / updates / removals checked
 * against a naive reference, for int and string keys, across resizes and
 * compactions; plus insertion order.
 */

#include "../runtime.h"

#include <assert.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define N 4000
#define KEYS 1500

static uint64_t rng = 0x12345678;
static uint64_t next(void) { rng ^= rng << 13; rng ^= rng >> 7; rng ^= rng << 17; return rng; }

static void test_int_keys(void) {
    rw_dict *d = rw_dict_new(0, sizeof(int64_t));
    int64_t ref[KEYS];
    int8_t present[KEYS] = {0};
    for (int step = 0; step < N * 10; step++) {
        int64_t k = (int64_t)(next() % KEYS) - KEYS / 2;  /* negative keys too */
        int idx = (int)(k + KEYS / 2);
        int op = (int)(next() % 3);
        if (op < 2) {
            int64_t v = (int64_t)next();
            rw_dict_set(d, &k, &v);
            ref[idx] = v;
            present[idx] = 1;
        } else {
            int8_t was = rw_dict_remove(d, &k);
            assert(was == present[idx]);
            present[idx] = 0;
        }
    }
    int64_t live = 0;
    for (int i = 0; i < KEYS; i++) {
        int64_t k = (int64_t)i - KEYS / 2, v = 0;
        int8_t found = rw_dict_get(d, &k, &v);
        assert(found == present[i]);
        assert(rw_dict_has(d, &k) == present[i]);
        if (found) { assert(v == ref[i]); live++; }
    }
    assert(rw_dict_len(d) == live);
    rw_list keys;
    rw_dict_keys(&keys, d);
    assert(keys.len == live);
}

static void test_string_keys_and_order(void) {
    rw_dict *d = rw_dict_new(1, sizeof(int64_t));
    const char *words[] = {"a", "b", "a", "c", "a", "b"};
    for (int i = 0; i < 6; i++) {
        rw_str k = rw_str_from_cstr(words[i], (int64_t)strlen(words[i]));
        int64_t v = 0;
        if (rw_dict_get(d, &k, &v)) v++; else v = 1;
        rw_dict_set(d, &k, &v);
    }
    assert(rw_dict_len(d) == 3);
    rw_list keys, vals;
    rw_dict_keys(&keys, d);
    rw_dict_values(&vals, d);
    rw_str *ks = (rw_str *)keys.data;
    int64_t *vs = (int64_t *)vals.data;
    assert(keys.len == 3 && vals.len == 3);
    assert(ks[0].len == 1 && ks[0].ptr[0] == 'a' && vs[0] == 3);
    assert(ks[1].ptr[0] == 'b' && vs[1] == 2);
    assert(ks[2].ptr[0] == 'c' && vs[2] == 1);
    /* Remove "a" and add it back: it moves to the end. */
    rw_str a = rw_str_from_cstr("a", 1);
    assert(rw_dict_remove(d, &a) == 1);
    assert(rw_dict_remove(d, &a) == 0);
    int64_t one = 1;
    rw_dict_set(d, &a, &one);
    rw_dict_keys(&keys, d);
    ks = (rw_str *)keys.data;
    assert(keys.len == 3 && ks[0].ptr[0] == 'b' && ks[2].ptr[0] == 'a');
    /* Many distinct string keys force resizes. */
    char buf[32];
    for (int i = 0; i < N; i++) {
        int n = snprintf(buf, sizeof buf, "key-%d", i);
        char *p = malloc((size_t)n);
        memcpy(p, buf, (size_t)n);
        rw_str k = {n, p};
        int64_t v = i;
        rw_dict_set(d, &k, &v);
    }
    for (int i = 0; i < N; i += 97) {
        int n = snprintf(buf, sizeof buf, "key-%d", i);
        rw_str k = {n, buf};
        int64_t v = -1;
        assert(rw_dict_get(d, &k, &v) && v == i);
    }
    assert(rw_dict_len(d) == 3 + N);
}

int main(void) {
    test_int_keys();
    test_string_keys_and_order();
    printf("test_dict: OK\n");
    return 0;
}
