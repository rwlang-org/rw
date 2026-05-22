#ifndef RW_RUNTIME_H
#define RW_RUNTIME_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    int64_t len;
    const char *ptr;
} rw_str;

typedef struct rw_future rw_future_t;

/* print */
void rw_print_i64(int64_t v);
void rw_print_f64(double v);
void rw_print_bool(int8_t v);
void rw_print_str(rw_str s);

/* string helper */
rw_str rw_str_from_cstr(const char *cstr, int64_t len);

/* string ops */
int64_t rw_str_len   (rw_str s);
int8_t  rw_str_eq    (rw_str a, rw_str b);
rw_str  rw_str_concat(rw_str a, rw_str b);

/* List[int] type and ops.
 *
 * The struct is 3 words (24 bytes) which puts it past arm64's
 * "return in registers" threshold and triggers sret on most ABIs.
 * To keep llvmlite-emitted IR compatible with the C ABI without
 * having to tag every declare with sret, we expose all helpers as
 * pointer-out functions: the caller allocates a stack slot and
 * passes its address as the first argument. Per-value reads
 * (len/at) still return their results by value (i64). */
typedef struct {
    int64_t  len;
    int64_t  cap;
    int64_t *data;
} rw_list_int;

void    rw_list_int_new (rw_list_int *out);
void    rw_list_int_push(rw_list_int *out, const rw_list_int *l, int64_t v);
int64_t rw_list_int_at  (const rw_list_int *l, int64_t i);
int64_t rw_list_int_len (const rw_list_int *l);

/* spawn (one per return type) */
rw_future_t *rw_spawn_i64 (int64_t (*fn)(void *), void *args);
rw_future_t *rw_spawn_f64 (double  (*fn)(void *), void *args);
rw_future_t *rw_spawn_bool(int8_t  (*fn)(void *), void *args);
rw_future_t *rw_spawn_str (rw_str  (*fn)(void *), void *args);
rw_future_t *rw_spawn_void(void    (*fn)(void *), void *args);

/* await (one per return type) */
int64_t rw_await_i64 (rw_future_t *f);
double  rw_await_f64 (rw_future_t *f);
int8_t  rw_await_bool(rw_future_t *f);
rw_str  rw_await_str (rw_future_t *f);
void    rw_await_void(rw_future_t *f);

/* process lifecycle (rwc inserts calls in @main) */
void rw_init(void);
void rw_shutdown(void);

#ifdef __cplusplus
}
#endif

#endif /* RW_RUNTIME_H */
