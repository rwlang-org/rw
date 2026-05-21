#include "runtime.h"
#include "fiber/sched.h"

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

/* ---------- lifecycle ---------- */

void rw_init(void) {
    rw_sched_init();
}

void rw_shutdown(void) {
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
