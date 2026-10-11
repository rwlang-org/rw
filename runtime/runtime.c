#include "runtime.h"
#include "aio.h"
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

/* ---------- assertion failure ---------- */

void rw_panic(rw_str msg) {
    fputs("rw: assertion failed: ", stderr);
    if (msg.ptr && msg.len > 0) {
        fwrite(msg.ptr, 1, (size_t)msg.len, stderr);
    }
    fputc('\n', stderr);
    exit(1);
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

/* ---------- string search / conversion (spec 18) ---------- */

int64_t rw_str_find(rw_str s, rw_str sub) {
    if (sub.len == 0) return 0;
    if (sub.len > s.len) return -1;
    for (int64_t i = 0; i + sub.len <= s.len; i++) {
        if (memcmp(s.ptr + i, sub.ptr, (size_t)sub.len) == 0) return i;
    }
    return -1;
}

rw_str rw_str_slice(rw_str s, int64_t start, int64_t end) {
    /* Clamp both bounds to [0, len]; strings are immutable, so the slice
     * shares the source buffer (zero-copy). */
    if (start < 0) start = 0;
    if (end > s.len) end = s.len;
    rw_str out;
    if (start >= end) {
        out.len = 0;
        out.ptr = NULL;
        return out;
    }
    out.len = end - start;
    out.ptr = s.ptr + start;
    return out;
}

int8_t rw_str_starts_with(rw_str s, rw_str prefix) {
    if (prefix.len > s.len) return 0;
    if (prefix.len == 0) return 1;
    return memcmp(s.ptr, prefix.ptr, (size_t)prefix.len) == 0 ? 1 : 0;
}

rw_str rw_str_from_int(int64_t n) {
    char buf[32];
    int len = snprintf(buf, sizeof buf, "%lld", (long long)n);
    rw_str out;
    out.len = 0;
    out.ptr = NULL;
    if (len <= 0) return out;
    char *p = (char *)malloc((size_t)len);
    if (!p) return out;  /* OOM: degrade to empty string, like concat. */
    memcpy(p, buf, (size_t)len);
    out.len = len;
    out.ptr = p;
    return out;
}

rw_str rw_str_from_float(double v) {
    char buf[40];
    /* The fewest significant digits (15..17) that read back as the same
     * double; 17 always does. */
    int len = 0;
    for (int prec = 15; prec <= 17; prec++) {
        len = snprintf(buf, sizeof buf, "%.*g", prec, v);
        if (strtod(buf, NULL) == v) break;
    }
    rw_str out;
    out.len = 0;
    out.ptr = NULL;
    if (len <= 0) return out;
    char *p = (char *)malloc((size_t)len);
    if (!p) return out;
    memcpy(p, buf, (size_t)len);
    out.len = len;
    out.ptr = p;
    return out;
}

int64_t rw_str_cmp(rw_str a, rw_str b) {
    int64_t n = a.len < b.len ? a.len : b.len;
    int c = n > 0 ? memcmp(a.ptr, b.ptr, (size_t)n) : 0;
    if (c != 0) return c < 0 ? -1 : 1;
    if (a.len == b.len) return 0;
    return a.len < b.len ? -1 : 1;
}

void rw_str_to_int(rw_option_int *out, rw_str s) {
    out->tag = 0;
    out->payload = 0;
    int64_t i = 0;
    int neg = 0;
    if (i < s.len && s.ptr[i] == '-') {
        neg = 1;
        i++;
    }
    if (i >= s.len) return;  /* empty or a lone "-" */
    /* Accumulate as a negative number so INT64_MIN parses too. */
    int64_t acc = 0;
    for (; i < s.len; i++) {
        char c = s.ptr[i];
        if (c < '0' || c > '9') return;
        int64_t d = c - '0';
        if (acc < (INT64_MIN + d) / 10) return;  /* overflow */
        acc = acc * 10 - d;
    }
    if (!neg) {
        if (acc == INT64_MIN) return;  /* overflow: 9223372036854775808 */
        acc = -acc;
    }
    out->tag = 1;
    out->payload = acc;
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

/* ---------- List[T] (spec 22) ---------- */

void rw_list_new(rw_list *out) {
    out->len = 0;
    out->cap = 0;
    out->data = NULL;
}

void rw_list_push(rw_list *out, const rw_list *l, const void *elem, int64_t elem_size) {
    /* Same growth policy and copy-on-push semantics as rw_list_int_push. */
    int64_t new_cap;
    if (l->cap == 0) {
        new_cap = 4;
    } else if (l->len + 1 <= l->cap) {
        new_cap = l->cap;
    } else {
        new_cap = l->cap * 2;
        while (new_cap < l->len + 1) new_cap *= 2;
    }
    char *new_data = (char *)malloc((size_t)(new_cap * elem_size));
    if (!new_data) {
        out->len = 0;
        out->cap = 0;
        out->data = NULL;
        return;
    }
    if (l->len > 0) {
        memcpy(new_data, l->data, (size_t)(l->len * elem_size));
    }
    memcpy(new_data + l->len * elem_size, elem, (size_t)elem_size);
    out->len = l->len + 1;
    out->cap = new_cap;
    out->data = new_data;
}

void rw_list_at(void *elem_out, const rw_list *l, int64_t i, int64_t elem_size) {
    if (i < 0 || i >= l->len) {
        fputs("rw: list_at: index out of bounds\n", stderr);
        abort();
    }
    memcpy(elem_out, l->data + i * elem_size, (size_t)elem_size);
}

/* ---------- string builtins (spec 28) ---------- */

static rw_str str_alloc(int64_t len, char **buf) {
    rw_str out;
    out.len = 0;
    out.ptr = NULL;
    *buf = NULL;
    if (len <= 0) return out;
    char *p = (char *)malloc((size_t)len);
    if (!p) return out;  /* OOM: degrade to "" like concat */
    *buf = p;
    out.len = len;
    out.ptr = p;
    return out;
}

void rw_str_split(rw_list *out, rw_str s, rw_str sep) {
    out->len = 0;
    out->cap = 0;
    out->data = NULL;
    int64_t n = 1;
    if (sep.len > 0) {
        for (int64_t i = 0; i + sep.len <= s.len;) {
            if (memcmp(s.ptr + i, sep.ptr, (size_t)sep.len) == 0) { n++; i += sep.len; }
            else i++;
        }
    }
    rw_str *parts = (rw_str *)malloc((size_t)n * sizeof(rw_str));
    if (!parts) return;
    int64_t k = 0, start = 0;
    if (sep.len > 0) {
        for (int64_t i = 0; i + sep.len <= s.len;) {
            if (memcmp(s.ptr + i, sep.ptr, (size_t)sep.len) == 0) {
                parts[k++] = rw_str_slice(s, start, i);
                i += sep.len;
                start = i;
            } else {
                i++;
            }
        }
    }
    parts[k++] = rw_str_slice(s, start, s.len);
    out->len = k;
    out->cap = n;
    out->data = (char *)parts;
}

rw_str rw_str_join(const rw_list *parts, rw_str sep) {
    const rw_str *ps = (const rw_str *)parts->data;
    int64_t total = 0;
    for (int64_t i = 0; i < parts->len; i++) total += ps[i].len;
    if (parts->len > 1) total += sep.len * (parts->len - 1);
    char *p;
    rw_str out = str_alloc(total, &p);
    if (!p) return out;
    for (int64_t i = 0; i < parts->len; i++) {
        if (i > 0 && sep.len > 0) { memcpy(p, sep.ptr, (size_t)sep.len); p += sep.len; }
        if (ps[i].len > 0) { memcpy(p, ps[i].ptr, (size_t)ps[i].len); p += ps[i].len; }
    }
    return out;
}

static int is_ascii_space(char c) {
    return c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\v' || c == '\f';
}

rw_str rw_str_strip(rw_str s) {
    int64_t a = 0, b = s.len;
    while (a < b && is_ascii_space(s.ptr[a])) a++;
    while (b > a && is_ascii_space(s.ptr[b - 1])) b--;
    return rw_str_slice(s, a, b);
}

rw_str rw_str_replace(rw_str s, rw_str old, rw_str new_) {
    if (old.len == 0) return s;
    int64_t count = 0;
    for (int64_t i = 0; i + old.len <= s.len;) {
        if (memcmp(s.ptr + i, old.ptr, (size_t)old.len) == 0) { count++; i += old.len; }
        else i++;
    }
    if (count == 0) return s;
    char *p;
    rw_str out = str_alloc(s.len + count * (new_.len - old.len), &p);
    if (!p) return out;
    for (int64_t i = 0; i < s.len;) {
        if (i + old.len <= s.len && memcmp(s.ptr + i, old.ptr, (size_t)old.len) == 0) {
            if (new_.len > 0) { memcpy(p, new_.ptr, (size_t)new_.len); p += new_.len; }
            i += old.len;
        } else {
            *p++ = s.ptr[i++];
        }
    }
    return out;
}

static rw_str map_ascii(rw_str s, char lo, char hi, int delta) {
    char *p;
    rw_str out = str_alloc(s.len, &p);
    if (!p) return out;
    for (int64_t i = 0; i < s.len; i++) {
        char c = s.ptr[i];
        p[i] = (c >= lo && c <= hi) ? (char)(c + delta) : c;
    }
    return out;
}

rw_str rw_str_lower(rw_str s) { return map_ascii(s, 'A', 'Z', 'a' - 'A'); }
rw_str rw_str_upper(rw_str s) { return map_ascii(s, 'a', 'z', 'A' - 'a'); }

int8_t rw_str_contains(rw_str s, rw_str sub) {
    return rw_str_find(s, sub) >= 0 ? 1 : 0;
}

int8_t rw_str_ends_with(rw_str s, rw_str suffix) {
    if (suffix.len > s.len) return 0;
    if (suffix.len == 0) return 1;
    return memcmp(s.ptr + s.len - suffix.len, suffix.ptr, (size_t)suffix.len) == 0 ? 1 : 0;
}

/* ---------- lifecycle ---------- */

void rw_init(void) {
    rw_sched_init();
    rw_netpoller_init();
    rw_aio_init();
}

void rw_shutdown(void) {
    rw_aio_shutdown();
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
void    rw_detach    (rw_future_t *f) {
    rw_sched_detach((rw_fiber_handle *)f);
}
