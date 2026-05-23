#include "runtime.h"
#include "fiber/sched.h"
#include "net/netpoller.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ---------- print ---------- */

void rw_print_i64(int64_t v) {
    printf("%lld\n", (long long)v);
}

void rw_print_f64(double v) {
    printf("%g\n", v);
}

void rw_print_bool(int8_t v) {
    fputs(v ? "true\n" : "false\n", stdout);
}

void rw_print_str(rw_str s) {
    if (s.ptr && s.len > 0) {
        fwrite(s.ptr, 1, (size_t)s.len, stdout);
    }
    fputc('\n', stdout);
}

/* ---------- string helper ---------- */

rw_str rw_str_from_cstr(const char *cstr, int64_t len) {
    rw_str s;
    s.ptr = cstr;
    s.len = len;
    return s;
}

/* ---------- string ops ---------- */

int64_t rw_str_len(rw_str s) {
    return s.len;
}

int8_t rw_str_eq(rw_str a, rw_str b) {
    if (a.len != b.len) return 0;
    if (a.len == 0) return 1;  /* both empty: equal */
    return (int8_t)(memcmp(a.ptr, b.ptr, (size_t)a.len) == 0);
}

rw_str rw_str_concat(rw_str a, rw_str b) {
    rw_str out;
    out.len = a.len + b.len;
    if (out.len == 0) {
        out.ptr = NULL;
        return out;
    }
    char *p = (char *)malloc((size_t)out.len);
    if (!p) {
        /* OOM: degrade to empty string rather than crash. */
        out.len = 0;
        out.ptr = NULL;
        return out;
    }
    if (a.len > 0) memcpy(p, a.ptr, (size_t)a.len);
    if (b.len > 0) memcpy(p + a.len, b.ptr, (size_t)b.len);
    out.ptr = p;
    return out;
}

/* ---------- List[int] ops ----------
 *
 * All helpers use pointer arguments instead of passing the 24-byte
 * struct by value. See runtime.h for the rationale (avoids sret /
 * ABI mismatches between clang-compiled callees and llvmlite-emitted
 * call sites). */

void rw_list_int_new(rw_list_int *out) {
    out->len = 0;
    out->cap = 0;
    out->data = NULL;
}

void rw_list_int_push(rw_list_int *out, const rw_list_int *l, int64_t v) {
    /* If the existing cap can hold one more element, reuse it. We still
     * allocate a fresh buffer (immutability), but the new cap matches
     * the old one. Only when the old cap is exhausted do we double. */
    int64_t new_cap;
    if (l->cap == 0) {
        new_cap = 4;
    } else if (l->len + 1 <= l->cap) {
        new_cap = l->cap;
    } else {
        new_cap = l->cap * 2;
        while (new_cap < l->len + 1) new_cap *= 2;
    }
    int64_t *new_data = (int64_t *)malloc((size_t)new_cap * sizeof(int64_t));
    if (!new_data) {
        /* OOM: degrade to an empty list rather than crash. */
        out->len = 0;
        out->cap = 0;
        out->data = NULL;
        return;
    }
    if (l->len > 0) {
        memcpy(new_data, l->data, (size_t)l->len * sizeof(int64_t));
    }
    new_data[l->len] = v;
    out->len = l->len + 1;
    out->cap = new_cap;
    out->data = new_data;
    /* Note: l->data is intentionally NOT freed; another caller may still
     * hold the old List. Leak is acceptable for the learning runtime. */
}

int64_t rw_list_int_at(const rw_list_int *l, int64_t i) {
    if (i < 0 || i >= l->len) {
        fputs("rw: list_at: index out of bounds\n", stderr);
        abort();
    }
    return l->data[i];
}

int64_t rw_list_int_len(const rw_list_int *l) {
    return l->len;
}

void rw_list_int_at_opt(rw_option_int *out, const rw_list_int *l, int64_t i) {
    if (i < 0 || i >= l->len) {
        out->tag = 0;
        out->payload = 0;
        return;
    }
    out->tag = 1;
    out->payload = l->data[i];
}

/* ---------- lifecycle ---------- */

void rw_init(void) {
    rw_sched_init();
    rw_netpoller_init();
}

void rw_shutdown(void) {
    rw_netpoller_shutdown();
    rw_sched_shutdown();
}

/* ---------- spawn / await (fiber-backed) ----------
 *
 * Each rw_spawn_<T> creates a fiber that will run `fn(args)` on its own
 * stack and store the result in the handle. The fiber is scheduled but
 * does not run immediately. rw_await_<T> drives the scheduler until the
 * fiber finishes, then returns its value.
 *
 * The public `rw_future_t` type from runtime.h is just an opaque alias
 * for `rw_fiber_handle`.
 */

struct rw_future {
    /* This is intentionally an incomplete declaration in the header.
     * In practice we only ever traffic in pointers, and we cast between
     * `rw_future_t*` and `rw_fiber_handle*` at the boundary. */
    int _placeholder;
};

rw_future_t *rw_spawn_i64 (int64_t (*fn)(void *), void *args) {
    return (rw_future_t *)rw_sched_spawn_i64(fn, args);
}
rw_future_t *rw_spawn_f64 (double  (*fn)(void *), void *args) {
    return (rw_future_t *)rw_sched_spawn_f64(fn, args);
}
rw_future_t *rw_spawn_bool(int8_t  (*fn)(void *), void *args) {
    return (rw_future_t *)rw_sched_spawn_bool(fn, args);
}
rw_future_t *rw_spawn_str (rw_str  (*fn)(void *), void *args) {
    return (rw_future_t *)rw_sched_spawn_str(fn, args);
}
rw_future_t *rw_spawn_void(void    (*fn)(void *), void *args) {
    return (rw_future_t *)rw_sched_spawn_void(fn, args);
}

int64_t rw_await_i64 (rw_future_t *f) {
    return rw_sched_join_i64((rw_fiber_handle *)f);
}
double  rw_await_f64 (rw_future_t *f) {
    return rw_sched_join_f64((rw_fiber_handle *)f);
}
int8_t  rw_await_bool(rw_future_t *f) {
    return rw_sched_join_bool((rw_fiber_handle *)f);
}
rw_str  rw_await_str (rw_future_t *f) {
    return rw_sched_join_str((rw_fiber_handle *)f);
}
void    rw_await_void(rw_future_t *f) {
    rw_sched_join_void((rw_fiber_handle *)f);
}
