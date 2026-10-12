/*
 * Generic fd I/O: read/write/close work on any file descriptor
 * (sockets, files, pipes). See docs/specs/15-file-io.md.
 *
 * Sockets are nonblocking and use the EAGAIN+park path (park on the
 * netpoller until ready). Every other fd — regular files (spec 16), and
 * pipes, terminals, and other character devices (spec 25) — would block
 * read(2)/write(2) in the kernel and pin the worker M, so it is offloaded
 * to the aio thread pool while the fiber parks.
 */

#include "io.h"
#include "net/netpoller.h"
#include "fiber/sched.h"
#include "runtime.h"

#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <poll.h>
#include <sys/stat.h>

#include "aio.h"

/* Off-fiber (main thread) callers cannot park; wait in the kernel until the
 * nonblocking socket is ready instead of reporting EAGAIN as EOF. */
static void wait_fd(int64_t fd, short events) {
    struct pollfd p = { (int)fd, events, 0 };
    while (poll(&p, 1, -1) < 0 && rw_errno == EINTR) {}
}

/* True for fds whose read/write may block in the kernel: anything that is
 * not a socket. Sockets accepted by rw are nonblocking (tcp.c). */
static int needs_offload(int64_t fd) {
    struct stat st;
    return fstat((int)fd, &st) == 0 && !S_ISSOCK(st.st_mode);
}

static void read_impl(rw_str *out, int64_t fd, int64_t max) {
    if (max <= 0) { out->len = 0; out->ptr = NULL; return; }

    /* Blocking fds go to the aio pool so the fiber parks instead of
     * pinning the worker. Sockets keep the netpoller path below. */
    if (needs_offload(fd)) {
        rw_aio_read(out, fd, max);
        return;
    }

    char *buf = (char *)malloc((size_t)max);
    if (!buf)     { out->len = 0; out->ptr = NULL; return; }
    for (;;) {
        ssize_t n = read((int)fd, buf, (size_t)max);
        if (n > 0)  { out->len = n; out->ptr = buf; return; }
        if (n == 0) { free(buf); out->len = 0; out->ptr = NULL; rw_errno = 0; return; }
        if (rw_errno == EAGAIN || rw_errno == EWOULDBLOCK) {
            if (rw_sched_current_fiber()) {
                rw_net_park_read((int)fd);
            } else {
                wait_fd(fd, POLLIN);
            }
            continue;
        }
        free(buf); out->len = 0; out->ptr = NULL; return;
    }
}

static int64_t write_impl(int64_t fd, rw_str b) {
    if (b.len <= 0) return 0;

    if (needs_offload(fd)) {
        return rw_aio_write(fd, b);
    }

    for (;;) {
        ssize_t n = write((int)fd, b.ptr, (size_t)b.len);
        if (n >= 0) return (int64_t)n;
        if (rw_errno == EAGAIN || rw_errno == EWOULDBLOCK) {
            if (rw_sched_current_fiber()) {
                rw_net_park_write((int)fd);
            } else {
                wait_fd(fd, POLLOUT);
            }
            continue;
        }
        return -1;
    }
}

/* Public entry points record the outcome for last_error() (spec 51). The
 * impls leave rw_errno describing a failure; it is read before any park. */
void rw_read(rw_str *out, int64_t fd, int64_t max) {
    rw_errno = 0;
    read_impl(out, fd, max);
    rw_set_last_error(out->len > 0 ? 0 : rw_errno);
}

int64_t rw_write(int64_t fd, rw_str b) {
    rw_errno = 0;
    int64_t n = write_impl(fd, b);
    rw_set_last_error(n >= 0 ? 0 : (rw_errno ? rw_errno : EIO));
    return n;
}

/* Write all of `b`, retrying partial writes (spec 52). Returns b.len, or -1
 * with last_error set if any write fails. */
int64_t rw_write_all(int64_t fd, rw_str b) {
    int64_t off = 0;
    while (off < b.len) {
        rw_errno = 0;
        rw_str rest = { b.len - off, b.ptr + off };
        int64_t n = write_impl(fd, rest);
        if (n <= 0) {
            rw_set_last_error(n < 0 && rw_errno ? rw_errno : EIO);
            return -1;
        }
        off += n;
    }
    rw_set_last_error(0);
    return off;
}

int64_t rw_close(int64_t fd) {
    int rc = close((int)fd);
    rw_set_last_error(rc < 0 ? rw_errno : 0);
    return (int64_t)rc;
}

static int64_t file_open_impl(rw_str path, rw_str mode) {
    /* rw strings are not NUL-terminated; copy path with a terminator. */
    if (path.len < 0) { rw_errno = EINVAL; return -1; }
    char *cpath = (char *)malloc((size_t)path.len + 1);
    if (!cpath) return -1;
    if (path.len > 0) memcpy(cpath, path.ptr, (size_t)path.len);
    cpath[path.len] = '\0';

    int flags;
    if (mode.len == 1 && mode.ptr[0] == 'r') {
        flags = O_RDONLY;
    } else if (mode.len == 1 && mode.ptr[0] == 'w') {
        flags = O_WRONLY | O_CREAT | O_TRUNC;
    } else if (mode.len == 1 && mode.ptr[0] == 'a') {
        flags = O_WRONLY | O_CREAT | O_APPEND;
    } else {
        free(cpath);
        rw_errno = EINVAL;
        return -1;  /* unsupported mode */
    }

    int fd = open(cpath, flags, 0644);
    free(cpath);
    return (int64_t)fd;
}

int64_t rw_file_open(rw_str path, rw_str mode) {
    rw_errno = 0;
    int64_t fd = file_open_impl(path, mode);
    rw_set_last_error(fd >= 0 ? 0 : (rw_errno ? rw_errno : EINVAL));
    return fd;
}

/* ---- error names (spec 51) ---- */

static const struct { int code; const char *name; } k_errnames[] = {
    { EPERM, "EPERM" }, { ENOENT, "ENOENT" }, { EINTR, "EINTR" }, { EIO, "EIO" },
    { EBADF, "EBADF" }, { EAGAIN, "EAGAIN" }, { ENOMEM, "ENOMEM" }, { EACCES, "EACCES" },
    { EEXIST, "EEXIST" }, { ENOTDIR, "ENOTDIR" }, { EISDIR, "EISDIR" }, { EINVAL, "EINVAL" },
    { ENFILE, "ENFILE" }, { EMFILE, "EMFILE" }, { ENOSPC, "ENOSPC" }, { EPIPE, "EPIPE" },
    { ENAMETOOLONG, "ENAMETOOLONG" }, { ENOTSOCK, "ENOTSOCK" },
    { EAFNOSUPPORT, "EAFNOSUPPORT" }, { EADDRINUSE, "EADDRINUSE" },
    { EADDRNOTAVAIL, "EADDRNOTAVAIL" }, { ENETDOWN, "ENETDOWN" },
    { ENETUNREACH, "ENETUNREACH" }, { ECONNABORTED, "ECONNABORTED" },
    { ECONNRESET, "ECONNRESET" }, { ENOTCONN, "ENOTCONN" }, { ETIMEDOUT, "ETIMEDOUT" },
    { ECONNREFUSED, "ECONNREFUSED" }, { EHOSTUNREACH, "EHOSTUNREACH" },
    { EINPROGRESS, "EINPROGRESS" }, { RW_ENOHOST, "ENOHOST" },
};

rw_str rw_error_name(int64_t err) {
    if (err == 0) return rw_str_from_cstr("", 0);
    for (size_t i = 0; i < sizeof k_errnames / sizeof k_errnames[0]; i++) {
        if (k_errnames[i].code == err) return rw_str_copy_cstr(k_errnames[i].name);
    }
    char buf[32];
    snprintf(buf, sizeof buf, "E%lld", (long long)err);
    return rw_str_copy_cstr(buf);
}

rw_str rw_error_message(int64_t err) {
    if (err == 0) return rw_str_from_cstr("", 0);
    if (err == RW_ENOHOST) return rw_str_copy_cstr("host not found");
    return rw_str_copy_cstr(strerror((int)err));
}
