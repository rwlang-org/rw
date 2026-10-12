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
    while (poll(&p, 1, -1) < 0 && errno == EINTR) {}
}

/* True for fds whose read/write may block in the kernel: anything that is
 * not a socket. Sockets accepted by rw are nonblocking (tcp.c). */
static int needs_offload(int64_t fd) {
    struct stat st;
    return fstat((int)fd, &st) == 0 && !S_ISSOCK(st.st_mode);
}

void rw_read(rw_str *out, int64_t fd, int64_t max) {
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
        if (n == 0) { free(buf); out->len = 0; out->ptr = NULL; return; }
        if (errno == EAGAIN || errno == EWOULDBLOCK) {
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

int64_t rw_write(int64_t fd, rw_str b) {
    if (b.len <= 0) return 0;

    if (needs_offload(fd)) {
        return rw_aio_write(fd, b);
    }

    for (;;) {
        ssize_t n = write((int)fd, b.ptr, (size_t)b.len);
        if (n >= 0) return (int64_t)n;
        if (errno == EAGAIN || errno == EWOULDBLOCK) {
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

int64_t rw_close(int64_t fd) {
    return (int64_t)close((int)fd);
}

int64_t rw_file_open(rw_str path, rw_str mode) {
    /* rw strings are not NUL-terminated; copy path with a terminator. */
    if (path.len < 0) return -1;
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
        return -1;  /* unsupported mode */
    }

    int fd = open(cpath, flags, 0644);
    free(cpath);
    return (int64_t)fd;
}
