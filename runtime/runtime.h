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

/* assertion failure: print message to stderr and exit(1) */
void rw_panic(rw_str msg);

/* string helper */
rw_str rw_str_from_cstr(const char *cstr, int64_t len);

/* string ops */
int64_t rw_str_len   (rw_str s);
int8_t  rw_str_eq    (rw_str a, rw_str b);
rw_str  rw_str_concat(rw_str a, rw_str b);

/* string search / conversion (spec 18). Indices are byte offsets. */
int64_t rw_str_find       (rw_str s, rw_str sub);
rw_str  rw_str_slice      (rw_str s, int64_t start, int64_t end);
int8_t  rw_str_starts_with(rw_str s, rw_str prefix);
rw_str  rw_str_from_int   (int64_t n);

/* Conversions and ordering (spec 26). */
rw_str  rw_str_from_float(double v);       /* shortest round-trip text */
int64_t rw_str_cmp       (rw_str a, rw_str b);  /* <0, 0, >0 by bytes */

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

/* Option[int] type. Two-word fat struct: tag (0=None, 1=Some) +
 * payload (int value when Some). 16 bytes — fits in two registers,
 * so value-return ABI is safe (no pointer-out needed for the type
 * itself). The list_at_opt helper still uses pointer-out for the
 * output to match the List helpers' calling convention. */
typedef struct {
    int64_t tag;       /* 0 = None, 1 = Some */
    int64_t payload;
} rw_option_int;

void    rw_list_int_new (rw_list_int *out);
void    rw_list_int_push(rw_list_int *out, const rw_list_int *l, int64_t v);
int64_t rw_list_int_at  (const rw_list_int *l, int64_t i);
int64_t rw_list_int_len (const rw_list_int *l);

/* Parse a decimal int ("-"? digit+); None on any other input or overflow. */
void rw_str_to_int(rw_option_int *out, rw_str s);

/* List[T] for element types other than int (spec 22). Same three-word
 * layout as rw_list_int, with elements of `elem_size` bytes. Immutable:
 * push returns a new list (copy-on-push), like rw_list_int_push. */
typedef struct {
    int64_t len;
    int64_t cap;
    char   *data;
} rw_list;

void    rw_list_new (rw_list *out);
void    rw_list_push(rw_list *out, const rw_list *l, const void *elem, int64_t elem_size);
void    rw_list_at  (void *elem_out, const rw_list *l, int64_t i, int64_t elem_size);

/* String builtins (spec 28). ASCII only; indices are bytes. */
void    rw_str_split    (rw_list *out, rw_str s, rw_str sep);
rw_str  rw_str_join     (const rw_list *parts, rw_str sep);
rw_str  rw_str_strip    (rw_str s);
rw_str  rw_str_replace  (rw_str s, rw_str old, rw_str new_);
rw_str  rw_str_lower    (rw_str s);
rw_str  rw_str_upper    (rw_str s);
int8_t  rw_str_contains (rw_str s, rw_str sub);
int8_t  rw_str_ends_with(rw_str s, rw_str suffix);

/* Indexing and slicing (spec 29). A missing slice bound is RW_SLICE_NONE;
 * negative indices and bounds count from the end; slice bounds are
 * clamped to [0, len], an out-of-range index aborts. */
#define RW_SLICE_NONE INT64_MIN
rw_str rw_str_index   (rw_str s, int64_t i);
rw_str rw_str_slice_py(rw_str s, int64_t start, int64_t stop);
void   rw_list_slice  (rw_list *out, const rw_list *l, int64_t start, int64_t stop, int64_t elem_size);

/* Dict[K, V] (spec 31): an insertion-ordered hash table. key_kind 0 = int
 * (int64_t keys), 1 = string (rw_str keys). Keys and values are passed by
 * pointer; values are val_size bytes. */
typedef struct rw_dict rw_dict;
rw_dict *rw_dict_new   (int64_t key_kind, int64_t val_size);
void     rw_dict_set   (rw_dict *d, const void *key, const void *val);
int8_t   rw_dict_get   (rw_dict *d, const void *key, void *val_out);
void     rw_dict_at    (rw_dict *d, const void *key, void *val_out);
int8_t   rw_dict_has   (rw_dict *d, const void *key);
int8_t   rw_dict_remove(rw_dict *d, const void *key);
int64_t  rw_dict_len   (rw_dict *d);
void     rw_dict_keys  (rw_list *out, rw_dict *d);
void     rw_dict_values(rw_list *out, rw_dict *d);

/* OS API (spec 40). */
typedef struct {
    int64_t tag;    /* 0 = None, 1 = Some */
    rw_str  value;
} rw_option_str;
void    rw_set_args (int32_t argc, char **argv);   /* called by the C main */
void    rw_os_args  (rw_list *out);                /* List[string] */
void    rw_os_getenv(rw_option_str *out, rw_str name);
int8_t  rw_os_setenv(rw_str name, rw_str value);
void    rw_os_exit  (int64_t code);
rw_str  rw_os_getcwd(void);
int8_t  rw_os_chdir (rw_str path);

/* Random numbers (spec 43). */
int64_t rw_rand_int  (int64_t lo, int64_t hi);   /* uniform in [lo, hi] */
double  rw_rand_float(void);                     /* uniform in [0, 1) */
void    rw_rand_seed (int64_t seed);
rw_str  rw_rand_bytes(int64_t n);                /* OS CSPRNG */

/* List[int]: range-checked accessor returning Option[int]. */
void rw_list_int_at_opt(rw_option_int *out, const rw_list_int *l, int64_t i);

/* TCP API (runtime/net/tcp.c). */
int64_t rw_tcp_listen(int64_t port);
int64_t rw_tcp_accept(int64_t listen_fd);

/* Generic fd I/O (runtime/io.c): works on sockets, files, pipes. */
void    rw_read     (rw_str *out, int64_t fd, int64_t max);
int64_t rw_write    (int64_t fd, rw_str b);
int64_t rw_close    (int64_t fd);
int64_t rw_file_open(rw_str path, rw_str mode);

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
/* await_any (spec 45): index of the first finished Future in a
 * List[Future[T]] (elements are rw_future_t*); polls every millisecond. */
int64_t rw_await_any(const rw_list *futures);
/* Drop a Future without awaiting it; the fiber is freed when it finishes. */
void    rw_detach    (rw_future_t *f);
void    rw_await_void(rw_future_t *f);

/* process lifecycle (rwc inserts calls in @main) */
void rw_init(void);
void rw_shutdown(void);

#ifdef __cplusplus
}
#endif

#endif /* RW_RUNTIME_H */
