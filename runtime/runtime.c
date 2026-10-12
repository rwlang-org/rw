#include "runtime.h"
#include "rwgc.h"
#include "aio.h"
#include "timer.h"
#include "fiber/sched.h"
#include "net/netpoller.h"

#include <signal.h>
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

/* ---------- assert_eq (spec 68) ---------- */

static void put_where(rw_str where) {
    fputs("rw: assert_eq failed ", stderr);
    fwrite(where.ptr, 1, (size_t)where.len, stderr);
    fputc('\n', stderr);
}

void rw_assert_eq_fail_i64(int64_t a, int64_t b, rw_str where) {
    put_where(where);
    fprintf(stderr, "  left:  %lld\n  right: %lld\n", (long long)a, (long long)b);
    exit(1);
}

void rw_assert_eq_fail_f64(double a, double b, rw_str where) {
    put_where(where);
    fprintf(stderr, "  left:  %.17g\n  right: %.17g\n", a, b);
    exit(1);
}

void rw_assert_eq_fail_bool(int8_t a, int8_t b, rw_str where) {
    put_where(where);
    fprintf(stderr, "  left:  %s\n  right: %s\n", a ? "true" : "false", b ? "true" : "false");
    exit(1);
}

/* A string as a quoted literal, escaping quotes, backslashes and controls. */
static void put_quoted(const char *p, int64_t n) {
    fputc('"', stderr);
    for (int64_t i = 0; i < n; i++) {
        unsigned char c = (unsigned char)p[i];
        if (c == '"' || c == '\\') fprintf(stderr, "\\%c", c);
        else if (c == '\n') fputs("\\n", stderr);
        else if (c == '\t') fputs("\\t", stderr);
        else if (c == '\r') fputs("\\r", stderr);
        else if (c < 32 || c == 127) fprintf(stderr, "\\x%02x", c);
        else fputc(c, stderr);
    }
    fputc('"', stderr);
}

static int64_t line_end(rw_str s, int64_t from) {
    while (from < s.len && s.ptr[from] != '\n') from++;
    return from;
}

void rw_assert_eq_fail_str(rw_str a, rw_str b, rw_str where) {
    put_where(where);
    fputs("  left:  ", stderr);
    put_quoted(a.ptr, a.len);
    fputs("\n  right: ", stderr);
    put_quoted(b.ptr, b.len);
    fputc('\n', stderr);
    /* Multi-line values: point at the first line that differs. */
    if (memchr(a.ptr, '\n', (size_t)a.len) || memchr(b.ptr, '\n', (size_t)b.len)) {
        int64_t ia = 0, ib = 0, line = 1;
        for (;;) {
            int64_t ea = line_end(a, ia), eb = line_end(b, ib);
            int same = (ea - ia == eb - ib) && memcmp(a.ptr + ia, b.ptr + ib, (size_t)(ea - ia)) == 0;
            int a_done = ea >= a.len, b_done = eb >= b.len;
            if (!same || a_done != b_done) {
                fprintf(stderr, "  first difference at line %lld:\n  - ", (long long)line);
                if (ia <= a.len) put_quoted(a.ptr + ia, ea - ia); else fputs("(no line)", stderr);
                fputs("\n  + ", stderr);
                if (ib <= b.len) put_quoted(b.ptr + ib, eb - ib); else fputs("(no line)", stderr);
                fputc('\n', stderr);
                break;
            }
            if (a_done && b_done) break;
            ia = ea + 1;
            ib = eb + 1;
            line++;
        }
    }
    exit(1);
}

/* ---------- string helper ---------- */

rw_str rw_str_from_cstr(const char *cstr, int64_t len) {
    rw_str s;
    s.ptr = cstr;
    s.len = len;
    return s;
}

rw_str rw_str_copy_cstr(const char *cstr) {
    size_t n = strlen(cstr);
    char *p = (char *)rw_gc_alloc_atomic(n + 1);
    memcpy(p, cstr, n + 1);
    return rw_str_from_cstr(p, (int64_t)n);
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
    char *p = (char *)rw_gc_alloc_atomic((size_t)out.len);
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
    char *p = (char *)rw_gc_alloc_atomic((size_t)len);
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
    char *p = (char *)rw_gc_alloc_atomic((size_t)len);
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

static rw_str str_alloc(int64_t len, char **buf);

/* spec 58 */
int64_t rw_byte_at(rw_str s, int64_t i) {
    if (i < 0 || i >= s.len) return -1;
    return (int64_t)(unsigned char)s.ptr[i];
}

rw_str rw_chr(int64_t cp) {
    if (cp < 0 || cp > 0x10FFFF || (cp >= 0xD800 && cp <= 0xDFFF)) cp = 0xFFFD;
    char buf[4];
    int n;
    if (cp < 0x80) { buf[0] = (char)cp; n = 1; }
    else if (cp < 0x800) { buf[0] = (char)(0xC0 | (cp >> 6)); buf[1] = (char)(0x80 | (cp & 0x3F)); n = 2; }
    else if (cp < 0x10000) {
        buf[0] = (char)(0xE0 | (cp >> 12)); buf[1] = (char)(0x80 | ((cp >> 6) & 0x3F));
        buf[2] = (char)(0x80 | (cp & 0x3F)); n = 3;
    } else {
        buf[0] = (char)(0xF0 | (cp >> 18)); buf[1] = (char)(0x80 | ((cp >> 12) & 0x3F));
        buf[2] = (char)(0x80 | ((cp >> 6) & 0x3F)); buf[3] = (char)(0x80 | (cp & 0x3F)); n = 4;
    }
    char *p;
    rw_str out = str_alloc(n, &p);
    if (p) memcpy(p, buf, (size_t)n);
    return out;
}

/* spec 62: one raw byte (the low 8 bits of b). */
rw_str rw_str_from_byte(int64_t b) {
    char *p;
    rw_str out = str_alloc(1, &p);
    if (p) p[0] = (char)(unsigned char)(b & 0xFF);
    return out;
}

void rw_str_to_float(rw_option_float *out, rw_str s) {
    out->tag = 0;
    out->payload = 0.0;
    if (s.len <= 0 || s.len > 512) return;
    char c0 = s.ptr[0];
    if (c0 == ' ' || c0 == '\t' || c0 == '\n' || c0 == '\r' || c0 == '\v' || c0 == '\f') return;
    char buf[513];
    memcpy(buf, s.ptr, (size_t)s.len);
    buf[s.len] = '\0';
    char *end = NULL;
    double v = strtod(buf, &end);
    if (end != buf + s.len) return;   /* trailing junk, or nothing parsed */
    out->tag = 1;
    out->payload = v;
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
    int64_t *new_data = (int64_t *)rw_gc_alloc_atomic((size_t)new_cap * sizeof(int64_t));
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
    char *new_data = (char *)rw_gc_alloc((size_t)(new_cap * elem_size));
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
    char *p = (char *)rw_gc_alloc_atomic((size_t)len);
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
    rw_str *parts = (rw_str *)rw_gc_alloc((size_t)n * sizeof(rw_str));
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

/* spec 55 */
rw_str rw_str_lstrip(rw_str s) {
    int64_t a = 0;
    while (a < s.len && is_ascii_space(s.ptr[a])) a++;
    return rw_str_slice(s, a, s.len);
}

rw_str rw_str_rstrip(rw_str s) {
    int64_t b = s.len;
    while (b > 0 && is_ascii_space(s.ptr[b - 1])) b--;
    return rw_str_slice(s, 0, b);
}

int64_t rw_str_rfind(rw_str s, rw_str sub) {
    if (sub.len > s.len) return -1;
    for (int64_t i = s.len - sub.len; i >= 0; i--) {
        if (sub.len == 0 || memcmp(s.ptr + i, sub.ptr, (size_t)sub.len) == 0) return i;
    }
    return -1;
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

/* ---------- indexing and slicing (spec 29) ---------- */

/* Resolve Python-style slice bounds against len into 0 <= *a <= *b <= len. */
static void slice_bounds(int64_t len, int64_t *a, int64_t *b) {
    int64_t s = *a, e = *b;
    if (s == RW_SLICE_NONE) s = 0;
    else if (s < 0) s += len;
    if (e == RW_SLICE_NONE) e = len;
    else if (e < 0) e += len;
    if (s < 0) s = 0;
    if (s > len) s = len;
    if (e < 0) e = 0;
    if (e > len) e = len;
    if (e < s) e = s;
    *a = s;
    *b = e;
}

rw_str rw_str_index(rw_str s, int64_t i) {
    if (i < 0) i += s.len;
    if (i < 0 || i >= s.len) {
        fputs("rw: string index out of range\n", stderr);
        abort();
    }
    return rw_str_slice(s, i, i + 1);
}

rw_str rw_str_slice_py(rw_str s, int64_t start, int64_t stop) {
    slice_bounds(s.len, &start, &stop);
    return rw_str_slice(s, start, stop);
}

void rw_list_slice(rw_list *out, const rw_list *l, int64_t start, int64_t stop, int64_t elem_size) {
    slice_bounds(l->len, &start, &stop);
    int64_t n = stop - start;
    out->len = 0;
    out->cap = 0;
    out->data = NULL;
    if (n <= 0) return;
    char *data = (char *)rw_gc_alloc((size_t)(n * elem_size));
    if (!data) return;
    memcpy(data, l->data + start * elem_size, (size_t)(n * elem_size));
    out->len = n;
    out->cap = n;
    out->data = data;
}

/* ---------- Dict[K, V] (spec 31) ----------
 *
 * Like CPython's compact dict: `entries` holds {hash, live, key, value} in
 * insertion order; `index` is an open-addressing table (linear probing,
 * power-of-two size, load <= 2/3) of entry positions, -1 = empty. Removing
 * clears `live` and leaves the index slot as a tombstone (probing continues
 * past it); the table is compacted when `entries` fills up. */

struct rw_dict {
    int64_t  key_kind;    /* 0 = int64_t, 1 = rw_str */
    int64_t  key_size;
    int64_t  val_size;
    int64_t  entry_size;  /* 16 + key_size + val_size rounded up to 8 */
    int64_t  len;         /* live entries */
    int64_t  used;        /* entries in use, live or removed */
    int64_t  cap;         /* entry capacity */
    char    *entries;
    int64_t *index;
    int64_t  index_cap;   /* power of two */
};

#define RW_DICT_ENTRY(d, i) ((d)->entries + (i) * (d)->entry_size)

static uint64_t dict_hash(const rw_dict *d, const void *key) {
    if (d->key_kind == 0) {
        uint64_t x = (uint64_t)*(const int64_t *)key;  /* splitmix64 */
        x += 0x9E3779B97F4A7C15ull;
        x = (x ^ (x >> 30)) * 0xBF58476D1CE4E5B9ull;
        x = (x ^ (x >> 27)) * 0x94D049BB133111EBull;
        return x ^ (x >> 31);
    }
    const rw_str *s = (const rw_str *)key;  /* FNV-1a */
    uint64_t h = 0xCBF29CE484222325ull;
    for (int64_t i = 0; i < s->len; i++) {
        h ^= (unsigned char)s->ptr[i];
        h *= 0x100000001B3ull;
    }
    return h;
}

static int dict_key_eq(const rw_dict *d, const void *a, const void *b) {
    if (d->key_kind == 0) return *(const int64_t *)a == *(const int64_t *)b;
    return rw_str_eq(*(const rw_str *)a, *(const rw_str *)b);
}

static char *entry_key(const rw_dict *d, char *e) { (void)d; return e + 16; }
static char *entry_val(const rw_dict *d, char *e) { return e + 16 + d->key_size; }

rw_dict *rw_dict_new(int64_t key_kind, int64_t val_size) {
    rw_dict *d = (rw_dict *)rw_gc_alloc(sizeof *d);   /* zero-filled */
    if (!d) { fputs("rw: out of memory (dict)\n", stderr); abort(); }
    d->key_kind = key_kind;
    d->key_size = key_kind == 0 ? (int64_t)sizeof(int64_t) : (int64_t)sizeof(rw_str);
    d->val_size = val_size;
    d->entry_size = 16 + d->key_size + ((val_size + 7) / 8) * 8;
    return d;
}

/* Index slot holding `key`, or the empty slot where it would go. */
static int64_t dict_find_slot(const rw_dict *d, const void *key, uint64_t h) {
    uint64_t mask = (uint64_t)d->index_cap - 1;
    for (uint64_t i = h & mask;; i = (i + 1) & mask) {
        int64_t pos = d->index[i];
        if (pos < 0) return (int64_t)i;
        char *e = RW_DICT_ENTRY(d, pos);
        if (*(uint64_t *)(e + 8) && *(uint64_t *)e == h && dict_key_eq(d, entry_key(d, e), key)) {
            return (int64_t)i;
        }
    }
}

static void dict_rebuild(rw_dict *d, int64_t new_cap) {
    /* Compact live entries into a new array and rebuild the index. */
    char *entries = (char *)rw_gc_alloc((size_t)(new_cap * d->entry_size));
    int64_t index_cap = 8;
    while (index_cap * 2 < new_cap * 3) index_cap *= 2;  /* load <= 2/3 */
    int64_t *index = (int64_t *)rw_gc_alloc_atomic((size_t)index_cap * sizeof(int64_t));
    if (!entries || !index) { fputs("rw: out of memory (dict)\n", stderr); abort(); }
    for (int64_t i = 0; i < index_cap; i++) index[i] = -1;
    int64_t n = 0;
    for (int64_t i = 0; i < d->used; i++) {
        char *e = RW_DICT_ENTRY(d, i);
        if (!*(uint64_t *)(e + 8)) continue;
        memcpy(entries + n * d->entry_size, e, (size_t)d->entry_size);
        uint64_t mask = (uint64_t)index_cap - 1;
        uint64_t j = *(uint64_t *)e & mask;
        while (index[j] >= 0) j = (j + 1) & mask;
        index[j] = n++;
    }
    /* The old arrays become garbage (spec 67). */
    d->entries = entries;
    d->index = index;
    d->index_cap = index_cap;
    d->cap = new_cap;
    d->used = n;
}

void rw_dict_set(rw_dict *d, const void *key, const void *val) {
    if (d->index_cap == 0) dict_rebuild(d, 8);
    uint64_t h = dict_hash(d, key);
    int64_t slot = dict_find_slot(d, key, h);
    if (d->index[slot] >= 0) {  /* existing key: update in place */
        memcpy(entry_val(d, RW_DICT_ENTRY(d, d->index[slot])), val, (size_t)d->val_size);
        return;
    }
    if (d->used == d->cap) {
        /* Grow if mostly live; otherwise compaction alone makes room. */
        dict_rebuild(d, d->len * 2 >= d->cap ? d->cap * 2 : d->cap);
        slot = dict_find_slot(d, key, h);
    }
    char *e = RW_DICT_ENTRY(d, d->used);
    *(uint64_t *)e = h;
    *(uint64_t *)(e + 8) = 1;
    memcpy(entry_key(d, e), key, (size_t)d->key_size);
    memcpy(entry_val(d, e), val, (size_t)d->val_size);
    d->index[slot] = d->used++;
    d->len++;
}

static char *dict_lookup(rw_dict *d, const void *key) {
    if (d->len == 0) return NULL;
    int64_t slot = dict_find_slot(d, key, dict_hash(d, key));
    return d->index[slot] >= 0 ? RW_DICT_ENTRY(d, d->index[slot]) : NULL;
}

int8_t rw_dict_get(rw_dict *d, const void *key, void *val_out) {
    char *e = dict_lookup(d, key);
    if (!e) return 0;
    memcpy(val_out, entry_val(d, e), (size_t)d->val_size);
    return 1;
}

void rw_dict_at(rw_dict *d, const void *key, void *val_out) {
    if (!rw_dict_get(d, key, val_out)) {
        if (d->key_kind == 0) {
            fprintf(stderr, "rw: dict key not found: %lld\n", (long long)*(const int64_t *)key);
        } else {
            const rw_str *s = (const rw_str *)key;
            fprintf(stderr, "rw: dict key not found: \"%.*s\"\n", (int)s->len, s->ptr);
        }
        abort();
    }
}

int8_t rw_dict_has(rw_dict *d, const void *key) {
    return dict_lookup(d, key) ? 1 : 0;
}

int8_t rw_dict_remove(rw_dict *d, const void *key) {
    char *e = dict_lookup(d, key);
    if (!e) return 0;
    *(uint64_t *)(e + 8) = 0;  /* tombstone: the index slot stays occupied */
    d->len--;
    return 1;
}

int64_t rw_dict_len(rw_dict *d) {
    return d->len;
}

static void dict_collect(rw_list *out, rw_dict *d, int keys) {
    int64_t size = keys ? d->key_size : d->val_size;
    out->len = 0;
    out->cap = 0;
    out->data = NULL;
    if (d->len == 0) return;
    char *data = (char *)rw_gc_alloc((size_t)(d->len * size));
    if (!data) return;
    int64_t n = 0;
    for (int64_t i = 0; i < d->used; i++) {
        char *e = RW_DICT_ENTRY(d, i);
        if (!*(uint64_t *)(e + 8)) continue;
        memcpy(data + n * size, keys ? entry_key(d, e) : entry_val(d, e), (size_t)size);
        n++;
    }
    out->len = n;
    out->cap = n;
    out->data = data;
}

void rw_dict_keys(rw_list *out, rw_dict *d) { dict_collect(out, d, 1); }
void rw_dict_values(rw_list *out, rw_dict *d) { dict_collect(out, d, 0); }

/* ---------- OS API (spec 40) ---------- */

#include <unistd.h>

static int32_t g_argc = 0;
static char  **g_argv = NULL;

void rw_set_args(int32_t argc, char **argv) {
    g_argc = argc;
    g_argv = argv;
}

/* A NUL-terminated copy of s for C APIs (caller frees). */
static char *cstr_dup(rw_str s) {
    char *p = (char *)malloc((size_t)s.len + 1);
    if (!p) { fputs("rw: out of memory\n", stderr); abort(); }
    if (s.len > 0) memcpy(p, s.ptr, (size_t)s.len);
    p[s.len] = '\0';
    return p;
}

void rw_os_args(rw_list *out) {
    out->len = 0;
    out->cap = 0;
    out->data = NULL;
    if (g_argc <= 0) return;
    rw_str *items = (rw_str *)rw_gc_alloc((size_t)g_argc * sizeof(rw_str));
    if (!items) return;
    for (int32_t i = 0; i < g_argc; i++) {
        items[i] = rw_str_from_cstr(g_argv[i], (int64_t)strlen(g_argv[i]));
    }
    out->len = g_argc;
    out->cap = g_argc;
    out->data = (char *)items;
}

void rw_os_getenv(rw_option_str *out, rw_str name) {
    char *n = cstr_dup(name);
    const char *v = getenv(n);
    free(n);
    out->tag = v ? 1 : 0;
    out->value = v ? rw_str_from_cstr(v, (int64_t)strlen(v)) : rw_str_from_cstr("", 0);
}

int8_t rw_os_setenv(rw_str name, rw_str value) {
    char *n = cstr_dup(name), *v = cstr_dup(value);
    int rc = setenv(n, v, 1);
    free(n);
    free(v);
    return rc == 0 ? 1 : 0;
}

void rw_os_exit(int64_t code) {
    fflush(NULL);
    exit((int)code);
}

rw_str rw_os_getcwd(void) {
    char buf[4096];
    if (!getcwd(buf, sizeof buf)) return rw_str_from_cstr("", 0);
    return rw_str_copy_cstr(buf);
}

int8_t rw_os_chdir(rw_str path) {
    char *p = cstr_dup(path);
    int rc = chdir(p);
    free(p);
    return rc == 0 ? 1 : 0;
}

/* ---------- random numbers (spec 43) ----------
 *
 * xoshiro256** behind a mutex (fibers on several workers share one stream),
 * seeded from the OS on first use unless rw_rand_seed was called. Bytes for
 * secrets come straight from the OS CSPRNG. */

#include <pthread.h>
#if defined(__APPLE__)
#  include <stdlib.h>   /* arc4random_buf */
#else
#  include <sys/random.h>
#endif

static pthread_mutex_t g_rand_mu = PTHREAD_MUTEX_INITIALIZER;
static uint64_t g_rand_s[4];
static int g_rand_ready = 0;

static void os_random(void *buf, size_t n) {
#if defined(__APPLE__)
    arc4random_buf(buf, n);
#else
    char *p = (char *)buf;
    while (n > 0) {
        ssize_t got = getrandom(p, n, 0);
        if (got < 0) { fputs("rw: getrandom failed\n", stderr); abort(); }
        p += got;
        n -= (size_t)got;
    }
#endif
}

static uint64_t splitmix64(uint64_t *x) {
    uint64_t z = (*x += 0x9E3779B97F4A7C15ull);
    z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ull;
    z = (z ^ (z >> 27)) * 0x94D049BB133111EBull;
    return z ^ (z >> 31);
}

static void rand_seed_locked(uint64_t seed) {
    for (int i = 0; i < 4; i++) g_rand_s[i] = splitmix64(&seed);
    g_rand_ready = 1;
}

static uint64_t rotl(uint64_t x, int k) { return (x << k) | (x >> (64 - k)); }

/* Caller holds g_rand_mu. */
static uint64_t rand_next_locked(void) {
    if (!g_rand_ready) {
        uint64_t seed;
        os_random(&seed, sizeof seed);
        rand_seed_locked(seed);
    }
    uint64_t *s = g_rand_s;
    uint64_t result = rotl(s[1] * 5, 7) * 9;
    uint64_t t = s[1] << 17;
    s[2] ^= s[0];
    s[3] ^= s[1];
    s[1] ^= s[2];
    s[0] ^= s[3];
    s[2] ^= t;
    s[3] = rotl(s[3], 45);
    return result;
}

void rw_rand_seed(int64_t seed) {
    pthread_mutex_lock(&g_rand_mu);
    rand_seed_locked((uint64_t)seed);
    pthread_mutex_unlock(&g_rand_mu);
}

int64_t rw_rand_int(int64_t lo, int64_t hi) {
    if (lo > hi) {
        fputs("rw: rand_int: lo is greater than hi\n", stderr);
        abort();
    }
    uint64_t span = (uint64_t)hi - (uint64_t)lo + 1;  /* 0 means the full range */
    pthread_mutex_lock(&g_rand_mu);
    uint64_t r = rand_next_locked();
    if (span != 0) {
        /* Rejection sampling: drop the top partial bucket so every value in
         * the range is equally likely. */
        uint64_t limit = UINT64_MAX - UINT64_MAX % span;
        while (r >= limit) r = rand_next_locked();
        r %= span;
    }
    pthread_mutex_unlock(&g_rand_mu);
    return (int64_t)((uint64_t)lo + r);
}

double rw_rand_float(void) {
    pthread_mutex_lock(&g_rand_mu);
    uint64_t r = rand_next_locked();
    pthread_mutex_unlock(&g_rand_mu);
    return (double)(r >> 11) * 0x1.0p-53;  /* 53 random bits -> [0, 1) */
}

rw_str rw_rand_bytes(int64_t n) {
    if (n <= 0) return rw_str_from_cstr("", 0);
    char *p = (char *)rw_gc_alloc_atomic((size_t)n);
    os_random(p, (size_t)n);
    rw_str out;
    out.len = n;
    out.ptr = p;
    return out;
}

/* ---------- lifecycle ---------- */

void rw_init(void) {
    /* A write to a peer that hung up must fail with EPIPE (spec 51), not
     * kill the process. */
    signal(SIGPIPE, SIG_IGN);
    rw_sched_init();
    rw_netpoller_init();
    rw_aio_init();
    rw_timer_init();
}

void rw_shutdown(void) {
    rw_timer_shutdown();
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
int64_t rw_await_any(const rw_list *futures) {
    if (futures->len == 0) {
        fputs("rw: await_any of an empty list\n", stderr);
        abort();
    }
    rw_future_t **fs = (rw_future_t **)futures->data;
    for (;;) {
        /* Several may have finished since the last check: pick the one that
         * finished first, not the one with the lowest index. */
        int64_t best = -1;
        uint64_t best_seq = 0;
        for (int64_t i = 0; i < futures->len; i++) {
            rw_fiber_handle *h = (rw_fiber_handle *)fs[i];
            if (!rw_sched_is_done(h)) continue;
            uint64_t seq = rw_sched_finish_seq(h);
            if (best < 0 || seq < best_seq) {
                best = i;
                best_seq = seq;
            }
        }
        if (best >= 0) return best;
        /* A fiber cannot sit on several wait lists at once, so poll; the
         * sleep parks a fiber (or sleeps the main thread) between checks. */
        rw_sleep_ms(1);
    }
}

void    rw_detach    (rw_future_t *f) {
    rw_sched_detach((rw_fiber_handle *)f);
}
