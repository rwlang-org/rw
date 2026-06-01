/*
 * Generic fd I/O: read/write/close work on any file descriptor
 * (sockets, files, pipes). See docs/specs/15-file-io.md.
 *
 * The EAGAIN+park path handles nonblocking sockets (park on the
 * netpoller until ready); regular files never return EAGAIN on read(2)
 * so they fall through to a synchronous read. No fd-type branching.
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

void rw_read(rw_str *out, int64_t fd, int64_t max) {
    if (max <= 0) { out->len = 0; out->ptr = NULL; return; }
    char *buf = (char *)malloc((size_t)max);
    if (!buf)     { out->len = 0; out->ptr = NULL; return; }
    for (;;) {
        ssize_t n = read((int)fd, buf, (size_t)max);
        if (n > 0)  { out->len = n; out->ptr = buf; return; }
        if (n == 0) { free(buf); out->len = 0; out->ptr = NULL; return; }
        if (errno == EAGAIN || errno == EWOULDBLOCK) {
            if (rw_sched_current_fiber()) {
                rw_net_park_read((int)fd);
                continue;
            }
            free(buf); out->len = 0; out->ptr = NULL; return;
        }
        free(buf); out->len = 0; out->ptr = NULL; return;
    }
}

int64_t rw_write(int64_t fd, rw_str b) {
    if (b.len <= 0) return 0;
    for (;;) {
        ssize_t n = write((int)fd, b.ptr, (size_t)b.len);
        if (n >= 0) return (int64_t)n;
        if (errno == EAGAIN || errno == EWOULDBLOCK) {
            if (rw_sched_current_fiber()) {
                rw_net_park_write((int)fd);
                continue;
            }
            return -1;
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
