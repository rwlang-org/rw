/*
 * Filesystem builtins (spec 59). Each call runs on the aio pool when made
 * from a fiber (rw_aio_call), because directory and metadata syscalls can
 * block on slow or network filesystems. All of them record last_error
 * (spec 51).
 */

#include "runtime.h"
#include "aio.h"

#include <dirent.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

static char *cpath(rw_str s) {
    if (s.len < 0 || memchr(s.ptr, '\0', (size_t)s.len)) return NULL;
    char *p = (char *)malloc((size_t)s.len + 1);
    if (!p) return NULL;
    if (s.len > 0) memcpy(p, s.ptr, (size_t)s.len);
    p[s.len] = '\0';
    return p;
}

/* ---- stat ---- */

typedef struct {
    const char *path;
    int rc;
    int err;
    struct stat st;
} stat_req;

static void do_stat(void *arg) {
    stat_req *r = (stat_req *)arg;
    r->rc = stat(r->path, &r->st);
    r->err = r->rc != 0 ? errno : 0;
}

/* stat(2) on the pool; 0 on success. */
static int stat_path(rw_str path, struct stat *st) {
    char *p = cpath(path);
    if (!p) { rw_set_last_error(EINVAL); return -1; }
    stat_req r;
    r.path = p;
    rw_aio_call(do_stat, &r);
    free(p);
    rw_set_last_error(r.err);
    if (r.rc != 0) return -1;
    *st = r.st;
    return 0;
}

rw_str rw_fs_kind(rw_str path) {
    struct stat st;
    if (stat_path(path, &st) != 0) return rw_str_from_cstr("", 0);
    const char *k = S_ISREG(st.st_mode) ? "file" : S_ISDIR(st.st_mode) ? "dir" : "other";
    return rw_str_from_cstr(k, (int64_t)strlen(k));
}

int64_t rw_fs_size(rw_str path) {
    struct stat st;
    if (stat_path(path, &st) != 0) return -1;
    return (int64_t)st.st_size;
}

int64_t rw_fs_mtime_ms(rw_str path) {
    struct stat st;
    if (stat_path(path, &st) != 0) return -1;
#if defined(__APPLE__)
    return (int64_t)st.st_mtimespec.tv_sec * 1000 + st.st_mtimespec.tv_nsec / 1000000;
#else
    return (int64_t)st.st_mtim.tv_sec * 1000 + st.st_mtim.tv_nsec / 1000000;
#endif
}

/* ---- read_dir ---- */

typedef struct {
    const char *path;
    rw_list *out;
    int err;
} dir_req;

static int cmp_str(const void *a, const void *b) {
    const rw_str *x = (const rw_str *)a, *y = (const rw_str *)b;
    int64_t n = x->len < y->len ? x->len : y->len;
    int c = memcmp(x->ptr, y->ptr, (size_t)n);
    if (c != 0) return c;
    return (x->len > y->len) - (x->len < y->len);
}

static void do_read_dir(void *arg) {
    dir_req *r = (dir_req *)arg;
    r->err = 0;
    DIR *d = opendir(r->path);
    if (!d) { r->err = errno; return; }
    int64_t n = 0, cap = 16;
    rw_str *items = (rw_str *)malloc((size_t)cap * sizeof(rw_str));
    struct dirent *e;
    while (items && (e = readdir(d)) != NULL) {
        if (strcmp(e->d_name, ".") == 0 || strcmp(e->d_name, "..") == 0) continue;
        if (n == cap) {
            cap *= 2;
            rw_str *grown = (rw_str *)realloc(items, (size_t)cap * sizeof(rw_str));
            if (!grown) break;
            items = grown;
        }
        items[n++] = rw_str_copy_cstr(e->d_name);
    }
    closedir(d);
    if (!items) { r->err = ENOMEM; return; }
    qsort(items, (size_t)n, sizeof(rw_str), cmp_str);
    r->out->len = n;
    r->out->cap = cap;
    r->out->data = (char *)items;
}

void rw_fs_read_dir(rw_list *out, rw_str path) {
    out->len = 0;
    out->cap = 0;
    out->data = NULL;
    char *p = cpath(path);
    if (!p) { rw_set_last_error(EINVAL); return; }
    dir_req r = { p, out, 0 };
    rw_aio_call(do_read_dir, &r);
    free(p);
    rw_set_last_error(r.err);
}

/* ---- rename / remove / mkdir ---- */

typedef struct {
    int op;   /* 0 rename, 1 remove, 2 mkdir */
    const char *a;
    const char *b;
    int rc;
    int err;
} mod_req;

static void do_modify(void *arg) {
    mod_req *r = (mod_req *)arg;
    if (r->op == 0) {
        r->rc = rename(r->a, r->b);
    } else if (r->op == 1) {
        r->rc = unlink(r->a);
        if (r->rc != 0 && (errno == EISDIR || errno == EPERM)) r->rc = rmdir(r->a);
    } else {
        r->rc = mkdir(r->a, 0755);
    }
    r->err = r->rc != 0 ? errno : 0;
}

static int64_t modify(int op, rw_str a, rw_str b) {
    char *pa = cpath(a);
    char *pb = op == 0 ? cpath(b) : NULL;
    if (!pa || (op == 0 && !pb)) {
        free(pa);
        free(pb);
        rw_set_last_error(EINVAL);
        return -1;
    }
    mod_req r = { op, pa, pb, 0, 0 };
    rw_aio_call(do_modify, &r);
    free(pa);
    free(pb);
    rw_set_last_error(r.err);
    return r.rc == 0 ? 0 : -1;
}

int64_t rw_fs_rename(rw_str from, rw_str to) { return modify(0, from, to); }
int64_t rw_fs_remove(rw_str path)            { return modify(1, path, path); }
int64_t rw_fs_mkdir(rw_str path)             { return modify(2, path, path); }
